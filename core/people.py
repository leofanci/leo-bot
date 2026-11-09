"""people.py — people invited into project workspaces (gitignored team/ files).

  team/people.json   {"people": {pid: {name, token_hash, joined, ws: {slug: role}}}}
  team/invites.json  {"invites": [{id, code_hash, ws, role, by, created, expires, used}]}
  team/access.json   {"public_url": "https://<host>[:port]"}  the address a tunnel serves

The owner is whoever holds the server's owner token (dashboard/server.py); it is
not a row here. A person's identity is the random token in their device cookie,
handed out once when they open an invite link; only its sha256 is stored, like
invite codes. A link works once and expires. HQ (the holding) cannot be joined.

Roles per workspace: "admin" (invites members, removes members, manages channels)
or "member" (chats). Bots are denied Read on these files
(chat_session.SECRET_FILE_GLOBS).
"""
import hashlib
import hmac
import json
import re
import secrets
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

from core import bots

LOCK = threading.Lock()
ROLES = ("admin", "member")
_RANK = {"owner": 3, "admin": 2, "member": 1}
EXPIRY = {"1d": 86400, "7d": 7 * 86400, "30d": 30 * 86400}
NAME_MAX = 40
_NAME_RE = re.compile(r"^[^\x00-\x1f\x7f<>]{1,%d}$" % NAME_MAX)
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_HOST_RE = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$")
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0"}


def _hash(secret):
    return hashlib.sha256(secret.encode()).hexdigest()


def _read(path):
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _file(root, name):
    return Path(root) / "team" / name


def _save(root, name, data):
    # mkstemp creates the temp file 0600 and os.replace keeps that mode.
    bots._atomic_write(_file(root, name), json.dumps(data, indent=2) + "\n")


def _people(root):
    rows = _read(_file(root, "people.json")).get("people")
    return rows if isinstance(rows, dict) else {}


def _invites(root):
    rows = _read(_file(root, "invites.json")).get("invites")
    return [r for r in rows or [] if isinstance(r, dict)] if isinstance(rows, list) else []


def _project_ws(root, ws):
    """A joinable workspace: an existing project, never HQ or a path trick."""
    return (isinstance(ws, str) and ws != bots.HQ and bool(_SLUG_RE.fullmatch(ws))
            and (Path(root) / "projects" / ws).is_dir())


def clean_name(name):
    name = " ".join(str(name or "").split()) if isinstance(name, str) else ""
    if not _NAME_RE.fullmatch(name):
        raise ValueError(f"a name is 1-{NAME_MAX} characters, no < or >")
    return name


def can_invite(actor, role):
    return role in ROLES and actor in ("owner", "admin") and (actor == "owner" or role == "member")


def can_remove(actor, target):
    return actor == "owner" or (actor == "admin" and target == "member")


def _public_invite(r):
    return {k: r.get(k) for k in ("id", "ws", "role", "by", "created", "expires")}


def _live(r, now):
    return not r.get("used") and not r.get("revoked") and (r.get("expires") or 0) > now


# -- invites ---------------------------------------------------------------- #
def create_invite(root=bots.ROOT, *, ws, role, by, expiry="7d"):
    """New single-use link for `ws`. Returns (public record, code); only the code's hash is kept."""
    if not _project_ws(root, ws):
        raise ValueError("invite into a project workspace (not HQ)")
    if role not in ROLES:
        raise ValueError("role: admin or member")
    if expiry not in EXPIRY:
        raise ValueError("expiry: 1d, 7d or 30d")
    code = secrets.token_urlsafe(24)
    now = time.time()
    rec = {"id": "inv-" + secrets.token_hex(6), "code_hash": _hash(code), "ws": ws, "role": role,
           "by": str(by), "created": now, "expires": now + EXPIRY[expiry], "used": None}
    with LOCK:
        rows = [r for r in _invites(root) if _live(r, now)]  # drop dead rows as we go
        rows.append(rec)
        _save(root, "invites.json", {"invites": rows})
    return _public_invite(rec), code


def pending_invites(root=bots.ROOT, ws=None):
    now = time.time()
    return [_public_invite(r) for r in _invites(root)
            if _live(r, now) and (ws is None or r.get("ws") == ws)]


def revoke_invite(root=bots.ROOT, invite_id=None, *, ws):
    with LOCK:
        rows = _invites(root)
        hit = next((r for r in rows if r.get("id") == invite_id and r.get("ws") == ws
                    and _live(r, time.time())), None)
        if not hit:
            raise LookupError("unknown invite")
        hit["revoked"] = time.time()
        _save(root, "invites.json", {"invites": rows})


def _match(rows, code):
    if not isinstance(code, str) or not code or len(code) > 100:
        return None
    h = _hash(code)
    return next((r for r in rows if hmac.compare_digest(str(r.get("code_hash") or ""), h)), None)


def invite_for(root=bots.ROOT, code=None):
    """The live invite behind `code` (public fields), or None."""
    r = _match(_invites(root), code)
    return _public_invite(r) if r and _live(r, time.time()) and _project_ws(root, r.get("ws")) else None


def join(root=bots.ROOT, code=None, name=None, *, token=None):
    """Use an invite. `token` = the device's member cookie, if any: the same person
    gains the workspace. Returns (pid, token)."""
    with LOCK:
        rows = _invites(root)
        inv = _match(rows, code)
        if not inv or not _live(inv, time.time()) or not _project_ws(root, inv.get("ws")):
            raise ValueError("this invite link has expired or was already used")
        found = person_by_token(root, token)
        if not found:
            name = clean_name(name)
        everyone = _people(root)
        if found:
            pid = found["id"]
            token_out = token  # the device keeps its token, so its other browsers stay signed in
        else:
            pid = "p-" + secrets.token_hex(6)
            token_out = secrets.token_urlsafe(32)
            everyone[pid] = {"name": name, "token_hash": _hash(token_out), "joined": time.time(), "ws": {}}
        mine = everyone[pid].setdefault("ws", {})
        old = mine.get(inv["ws"])
        if not old or _RANK[inv["role"]] > _RANK.get(old, 0):
            mine[inv["ws"]] = inv["role"]
        inv["used"] = {"by": pid, "at": time.time()}
        # The invite is marked used first: if the second write fails, nobody holds a
        # workspace seat that the invite could still hand out again.
        _save(root, "invites.json", {"invites": rows})
        _save(root, "people.json", {"people": everyone})
        return pid, token_out


# -- people ----------------------------------------------------------------- #
def _view(root, pid, rec):
    ws = {k: v for k, v in (rec.get("ws") or {}).items() if v in ROLES and _project_ws(root, k)}
    return {"id": pid, "name": rec.get("name") or "", "joined": rec.get("joined"), "ws": ws}


def person_by_token(root=bots.ROOT, token=None):
    """{id, name, joined, ws} for a device token, or None."""
    if not isinstance(token, str) or not token or len(token) > 200:
        return None
    h = _hash(token)
    for pid, rec in _people(root).items():
        if isinstance(rec, dict) and hmac.compare_digest(str(rec.get("token_hash") or ""), h):
            return _view(root, pid, rec)
    return None


def get(root=bots.ROOT, pid=None):
    rec = _people(root).get(pid)
    return _view(root, pid, rec) if isinstance(rec, dict) else None


def people_in(root=bots.ROOT, ws=None):
    """Who joined `ws`: admins first, then members, each by join date."""
    rows = []
    for pid, rec in _people(root).items():
        if not isinstance(rec, dict):
            continue
        v = _view(root, pid, rec)
        if ws in v["ws"]:
            rows.append({"id": pid, "name": v["name"], "joined": v["joined"], "role": v["ws"][ws]})
    return sorted(rows, key=lambda r: (-_RANK[r["role"]], r["joined"] or 0))


def revoke_invites_by(root=bots.ROOT, ws=None, pid=None):
    """Every live invite `pid` made for `ws` dies: a removed person cannot let themselves back in."""
    with LOCK:
        rows = _invites(root)
        n = 0
        for r in rows:
            if r.get("by") == pid and r.get("ws") == ws and _live(r, time.time()):
                r["revoked"] = time.time()
                n += 1
        if n:
            _save(root, "invites.json", {"invites": rows})
        return n


def remove(root=bots.ROOT, ws=None, pid=None):
    """Drop `pid` from `ws`; a person left with no workspace is deleted (cookie dies)."""
    with LOCK:
        everyone = _people(root)
        rec = everyone.get(pid)
        if not isinstance(rec, dict) or ws not in (rec.get("ws") or {}):
            raise LookupError("not in this workspace")
        del rec["ws"][ws]
        if not any(_project_ws(root, k) for k in rec["ws"]):
            del everyone[pid]
        _save(root, "people.json", {"people": everyone})


def forget_workspace(root=bots.ROOT, ws=None):
    """A deleted workspace: nobody keeps it and its invites die, so a project made later
    with the same slug starts with no people. Returns the ids of people left with none."""
    if not isinstance(ws, str) or not ws or ws == bots.HQ:
        return []
    gone = []
    with LOCK:
        everyone = _people(root)
        for pid, rec in list(everyone.items()):
            if isinstance(rec, dict) and ws in (rec.get("ws") or {}):
                del rec["ws"][ws]
                if not any(_project_ws(root, k) for k in rec["ws"]):
                    del everyone[pid]
                    gone.append(pid)
        _save(root, "people.json", {"people": everyone})
        _save(root, "invites.json", {"invites": [r for r in _invites(root) if r.get("ws") != ws]})
    return gone


# -- sharing address --------------------------------------------------------- #
def _normalize_url(url):
    url = str(url or "").strip().rstrip("/")
    if not url:
        return None
    parts = urlsplit(url)
    try:
        port = parts.port
    except ValueError:
        raise ValueError("that port is not valid") from None
    host = (parts.hostname or "").lower()
    if (parts.scheme != "https" or parts.username or parts.password or parts.path
            or parts.query or parts.fragment or not _HOST_RE.fullmatch(host)
            or host in _LOCAL_HOSTS):
        raise ValueError("use the https address your tunnel gives, like https://name.example")
    return f"https://{host}" + (f":{port}" if port and port != 443 else "")


def public_url(root=bots.ROOT):
    try:
        return _normalize_url(_read(_file(root, "access.json")).get("public_url"))
    except ValueError:
        return None


def public_host(root=bots.ROOT):
    url = public_url(root)
    return url[len("https://"):] if url else None


def set_public_url(root=bots.ROOT, url=None):
    clean = _normalize_url(url)
    with LOCK:
        _save(root, "access.json", {"public_url": clean})
    return clean
