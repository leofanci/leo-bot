"""aes_gcm.py: AES-256-GCM in the standard library only, for the browser engine's own state files.

The engine (agent-browser 0.41.0) encrypts every saved state with AGENT_BROWSER_ENCRYPTION_KEY
and has no way to hand a keyed state out in plain text (docs/browser.md, "Moving a login to a
server"). Leo Bot reads its own bot's file with its own key to move that login to a server.

File layout, probed on 0.41.0: the AES key is the SHA-256 of the 64-hex key string (its ASCII
text, not the bytes it spells), then a 12-byte nonce, the ciphertext and the 16-byte tag (28
bytes over the plain JSON). No associated data. A wrong key, a changed byte or another layout
fails the tag check and raises ValueError: nothing is ever returned unverified.

Speed: pure Python, about 150 KB a second each way (1 MB, the cap, in about 7 s). Logins are a few KB.
"""
import hashlib
import hmac

NONCE, TAG = 12, 16

_SBOX = bytes.fromhex(
    "637c777bf26b6fc53001672bfed7ab76ca82c97dfa5947f0add4a2af9ca472c0"
    "b7fd9326363ff7cc34a5e5f171d8311504c723c31896059a071280e2eb27b275"
    "09832c1a1b6e5aa0523bd6b329e32f8453d100ed20fcb15b6acbbe394a4c58cf"
    "d0efaafb434d338545f9027f503c9fa851a3408f929d38f5bcb6da2110fff3d2"
    "cd0c13ec5f974417c4a77e3d645d197360814fdc222a908846eeb814de5e0bdb"
    "e0323a0a4906245cc2d3ac629195e479e7c8376d8dd54ea96c56f4ea657aae08"
    "ba78252e1ca6b4c6e8dd741f4bbd8b8a703eb5664803f60e613557b986c11d9e"
    "e1f8981169d98e949b1e87e9ce5528df8ca1890dbfe6426841992d0fb054bb16")


def _xtime(b):
    b <<= 1
    return (b ^ 0x11B) if b & 0x100 else b


def _expand(key):
    """The AES-256 round keys: 15 of 16 bytes."""
    if len(key) != 32:
        raise ValueError("AES-256 needs a 32-byte key")
    words = [list(key[i:i + 4]) for i in range(0, 32, 4)]
    rcon = 1
    for i in range(8, 60):
        t = list(words[i - 1])
        if i % 8 == 0:
            t = [_SBOX[x] for x in t[1:] + t[:1]]
            t[0] ^= rcon
            rcon = _xtime(rcon)
        elif i % 8 == 4:
            t = [_SBOX[x] for x in t]
        words.append([a ^ b for a, b in zip(words[i - 8], t)])
    return [bytes(sum(words[r * 4:r * 4 + 4], [])) for r in range(15)]


def _encrypt_block(rounds, block):
    s = [a ^ b for a, b in zip(block, rounds[0])]
    for r in range(1, 15):
        s = [_SBOX[x] for x in s]
        s = [s[(i + 4 * (i % 4)) % 16] for i in range(16)]  # ShiftRows (column-major state)
        if r != 14:
            out = []
            for c in range(4):
                a = s[4 * c:4 * c + 4]
                t = a[0] ^ a[1] ^ a[2] ^ a[3]
                out += [a[i] ^ t ^ _xtime(a[i] ^ a[(i + 1) % 4]) for i in range(4)]
            s = out
        s = [a ^ b for a, b in zip(s, rounds[r])]
    return bytes(s)


_R = 0xE1 << 120


def _gmul(x, y):
    z, v = 0, y
    for i in range(127, -1, -1):
        if (x >> i) & 1:
            z ^= v
        v = (v >> 1) ^ _R if v & 1 else v >> 1
    return z


def _ghash(h, data):
    y = 0
    for i in range(0, len(data), 16):
        block = data[i:i + 16]
        y = _gmul(y ^ int.from_bytes(block + bytes(16 - len(block)), "big"), h)
    return y


def _ctr(rounds, j0, data):
    out = bytearray()
    counter = int.from_bytes(j0, "big")
    for i in range(0, len(data), 16):
        counter = (counter & ~0xFFFFFFFF) | ((counter + 1) & 0xFFFFFFFF)
        stream = _encrypt_block(rounds, counter.to_bytes(16, "big"))
        out += bytes(a ^ b for a, b in zip(data[i:i + 16], stream))
    return bytes(out)


def _tag(rounds, h, j0, aad, ct):
    def pad(b):
        return b + bytes(-len(b) % 16)
    s = _ghash(h, pad(aad) + pad(ct) + (len(aad) * 8).to_bytes(8, "big") + (len(ct) * 8).to_bytes(8, "big"))
    return (s ^ int.from_bytes(_encrypt_block(rounds, j0), "big")).to_bytes(16, "big")


def _setup(key, nonce):
    if len(nonce) != NONCE:
        raise ValueError("a 12-byte nonce is needed")
    rounds = _expand(key)
    h = int.from_bytes(_encrypt_block(rounds, bytes(16)), "big")
    return rounds, h, nonce + b"\x00\x00\x00\x01"


def encrypt(key, nonce, plain, aad=b""):
    """ciphertext + tag (used by the tests and the known-answer check)."""
    rounds, h, j0 = _setup(key, nonce)
    ct = _ctr(rounds, j0, plain)
    return ct + _tag(rounds, h, j0, aad, ct)


def decrypt(key, nonce, data, aad=b""):
    """The plain bytes of ciphertext + tag, or ValueError when the tag does not match."""
    if len(data) < TAG:
        raise ValueError("too short")
    ct, tag = data[:-TAG], data[-TAG:]
    rounds, h, j0 = _setup(key, nonce)
    if not hmac.compare_digest(_tag(rounds, h, j0, aad, ct), tag):
        raise ValueError("the saved state did not decrypt with this key")
    return _ctr(rounds, j0, ct)


def file_key(key_hex):
    """The AES key the engine derives from AGENT_BROWSER_ENCRYPTION_KEY (probed on 0.41.0)."""
    return hashlib.sha256(key_hex.encode("ascii")).digest()


def open_file(key_hex, blob):
    """The engine's state file (nonce, ciphertext, tag) opened with its 64-hex key."""
    if len(blob) < NONCE + TAG:
        raise ValueError("too short")
    return decrypt(file_key(key_hex), blob[:NONCE], blob[NONCE:])


def seal_file(key_hex, nonce, plain):
    """A state file the engine opens with this key (the tests' stand-in for its save)."""
    return nonce + encrypt(file_key(key_hex), nonce, plain)
