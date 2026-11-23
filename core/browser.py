"""browser.py: bot browser settings, in gitignored team/browser.json.

  bots.<ws>.<bot>   {"level": "off"|"read"|"write"|"act", "mode": "bot"|"my_chrome"}
  sites.<host>      {"mode": None|"bot"|"my_chrome", "daily": None|1..500, "proxy": optional URL}
                    the proxy (an http address: host and port, user:password@ before the host
                    when it needs one) is used by the bot browser only
                    (dashboard/browser_proxy.py) and is never shown: Settings reads mask_proxy
  epochs.<ws>       six hex, made on first use, removed with the workspace: part of every engine
                    session name there, so a recreated slug never reaches an old browser or login
  retired           epochs of deleted workspaces whose sessions may still be on disk: the only
                    names the start sweep touches (other Leo Bot data roots share the namespace)

Bots default to level off, mode bot. Sites hold per-site overrides: a mode the site opens
in only (a bot in the other mode is refused there, never moved to that mode), and a daily
action cap. A site with neither stays listed. Site names are stored as host_of reads a
host (NFKC, IDNA ascii), so a Unicode name matches the pages of that site.
Hosts match by suffix, so www.linkedin.com belongs to linkedin.com but
evil-linkedin.com does not. Stored sites replace the defaults once saved.

The lock guards threads in this process only, same as core/integrations.py.
A file of the wrong shape reads as defaults and is overwritten by the next write.
"""
import copy
import json
import os
import re
import secrets
import threading
import unicodedata
from pathlib import Path
from urllib.parse import unquote, urlsplit

from core import bots

LOCK = threading.Lock()
# Bumped by every write in this process: a reader caching browser.json by its stat also keys on
# it, so a rewrite that lands on a reused inode with the same size and times is still seen.
GENERATION = 0
LEVELS = ("off", "read", "write", "act")
MODES = ("bot", "my_chrome")
DEFAULT_SITES = {
    "linkedin.com": {"mode": None, "daily": 20, "gap": 120},
    "instagram.com": {"mode": None, "daily": 10, "gap": 180},
}
MAX_DAILY = 500
DEFAULT_GAP = 30  # seconds between two sends on one site when its row sets none
MAX_GAP = 3600
EPOCH_RE = re.compile(r"[0-9a-f]{6}")


def _path(root):
    return Path(root) / "team" / "browser.json"


_LOADED = {}  # str(path) -> (stat key, parsed browser.json)


def _stamp(path):
    """What changes when browser.json is replaced or rewritten (GENERATION: a write in this
    process, in case a reused inode keeps the other four), or None when it cannot be read."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    return st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns, GENERATION


def load(root=bots.ROOT):
    """team/browser.json as a dict ({} when missing or unreadable). One gated browser call asks
    for it many times, so it is parsed again only when its stat changed; the stat is taken
    before the read, so a write between them is read again next time. Each caller gets its
    own copy."""
    path = _path(root)
    stamp = _stamp(path)
    hit = _LOADED.get(str(path))
    if stamp is not None and hit is not None and hit[0] == stamp:
        return copy.deepcopy(hit[1])
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    data = data if isinstance(data, dict) else {}
    if stamp is not None:
        _LOADED[str(path)] = (stamp, data)
    return copy.deepcopy(data)


def _save(root, data):
    global GENERATION
    bots._atomic_write(_path(root), json.dumps(data, indent=2) + "\n")
    GENERATION += 1


def _update(root, fn):
    with LOCK:
        data = load(root)
        out = fn(data)
        _save(root, data)
        return out


def _dict(x):
    return x if isinstance(x, dict) else {}


def bot_setting(ws, bot, root=bots.ROOT):
    row = _dict(_dict(_dict(load(root).get("bots")).get(ws)).get(bot))
    level, mode = row.get("level"), row.get("mode")
    return {"level": level if level in LEVELS else "off", "mode": mode if mode in MODES else "bot"}


def set_bot(ws, bot, level, mode, root=bots.ROOT):
    if level not in LEVELS:
        raise ValueError("unknown level")
    if mode not in MODES:
        raise ValueError("unknown mode")
    row = {"level": level, "mode": mode}

    def fn(d):
        if not isinstance(d.get("bots"), dict):
            d["bots"] = {}
        if not isinstance(d["bots"].get(ws), dict):
            d["bots"][ws] = {}
        d["bots"][ws][bot] = row
    _update(root, fn)
    return dict(row)


def sites(root=bots.ROOT):
    return sites_from(load(root))


def sites_from(data):
    """sites() of one load() result, so a caller can take sites and proxies from one read."""
    stored = _dict(data).get("sites")
    src = stored if isinstance(stored, dict) else DEFAULT_SITES
    src = {k: v for k, v in src.items() if isinstance(k, str)}
    out = {}
    for host, row in src.items():
        row = _dict(row)
        mode, daily = row.get("mode"), row.get("daily")
        out[host] = {
            "mode": mode if mode in MODES else None,
            "daily": daily if isinstance(daily, int) and not isinstance(daily, bool) and 1 <= daily <= MAX_DAILY else None,
            "gap": row["gap"] if _good_gap(row.get("gap")) else None,
        }
    return out


def _good_gap(gap):
    return isinstance(gap, int) and not isinstance(gap, bool) and 0 <= gap <= MAX_GAP


def gap_of(site, root=bots.ROOT, listed=None):
    """Seconds a send on `site` waits after the last one there (any bot, any workspace): the
    site's own gap, else the shipped one for that site, else DEFAULT_GAP."""
    row = (sites(root) if listed is None else listed).get(site) if site else None
    if row and row.get("gap") is not None:
        return row["gap"]
    shipped = (DEFAULT_SITES.get(site) or {}).get("gap") if site else None
    return shipped if shipped is not None else DEFAULT_GAP


def _host(value):
    """A site name as stored: the host host_of reads from it (NFKC, IDNA ascii), so the name
    matches the pages of that site whatever form it was typed in."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if text.startswith("//"):
        text = "http:" + text
    elif "//" not in text:
        text = "http://" + text + "/"
    return host_of(text)


PROXY_MAX = 500
PROXY_ONLY_BOT = ("A proxy is used only by the bot browser. Set this site to Bot browser or leave its mode "
                  "empty, or remove the proxy.")
PROXY_FORM = ("Give the proxy as an http address with its host and port, and user:password@ before the "
              "host when it needs one.")


def _proxy(value):
    """A proxy URL as stored: http, a host and a port, nothing after it. None for no proxy.
    The error never repeats the value (it may hold a password)."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if not isinstance(value, str) or len(value) > PROXY_MAX:
        raise ValueError(PROXY_FORM)
    text = value.strip()
    if any(ord(c) < 0x21 or ord(c) == 0x7F for c in text):
        raise ValueError(PROXY_FORM)
    try:
        parts = urlsplit(text)
        port = parts.port
    except ValueError:
        raise ValueError(PROXY_FORM) from None
    if (parts.scheme.lower() != "http" or not parts.hostname or not port
            or parts.path not in ("", "/") or parts.query or parts.fragment):
        raise ValueError(PROXY_FORM)
    return text


def mask_proxy(url):
    """A proxy URL safe to show: any user and password replaced by *** (a user alone can be a
    token). None stays None."""
    if not isinstance(url, str) or not url:
        return None
    try:
        parts = urlsplit(url)
        host, port, user = parts.hostname or "", parts.port, parts.username
    except ValueError:
        return "(set)"
    host = f"[{host}]" if ":" in host else host
    who = "***@" if user is not None or parts.password is not None or "@" in parts.netloc else ""
    return f"{parts.scheme}://{who}{host}" + (f":{port}" if port else "")


def site_proxies(root=bots.ROOT):
    """{site: proxy URL} of the listed sites with a proxy, unmasked: for the proxy router only."""
    return site_proxies_from(load(root))


def site_proxies_from(data):
    """site_proxies() of one load() result."""
    stored = _dict(data).get("sites")
    if not isinstance(stored, dict):
        return {}
    out = {}
    for host, row in stored.items():
        row = _dict(row)
        if isinstance(host, str) and row.get("mode") != "my_chrome":
            try:
                p = _proxy(row.get("proxy"))
            except ValueError:
                p = None
            if p:
                out[host] = p
    return out


def set_site(host, mode, daily, root=bots.ROOT, new=False, proxy=None, gap=None):
    """Set a site's mode, daily limit, gap between sends in seconds (None: the default) and
    proxy (None: no proxy). `new`: adding it, refused
    when it is listed already (so an add never wipes an existing row). A proxy is refused on a
    site bound to My Chrome: only the bot browser uses it."""
    name = _host(host)
    if not name or "." not in name:
        raise ValueError("host required")
    if mode is not None and mode not in MODES:
        raise ValueError("unknown mode")
    if daily is not None and (isinstance(daily, bool) or not isinstance(daily, int) or not 1 <= daily <= MAX_DAILY):
        raise ValueError("daily must be 1 to %d" % MAX_DAILY)
    if gap is not None and not _good_gap(gap):
        raise ValueError("seconds between sends must be 0 to %d" % MAX_GAP)
    proxy = _proxy(proxy)
    if proxy and mode == "my_chrome":
        raise ValueError(PROXY_ONLY_BOT)
    row = {"mode": mode, "daily": daily, "gap": gap}
    if proxy:
        row["proxy"] = proxy

    def fn(d):
        if not isinstance(d.get("sites"), dict):
            d["sites"] = json.loads(json.dumps(DEFAULT_SITES))
        if new and isinstance(d["sites"].get(name), dict):
            raise ValueError(f"{name} is already listed. Change it in its row.")
        d["sites"][name] = row
    _update(root, fn)
    out = dict(row)
    if proxy:
        out["proxy"] = mask_proxy(proxy)
    return out


def remove_site(host, root=bots.ROOT):
    """Take a site off the list: its mode and daily limit go. True when it was listed."""
    name = _host(host)
    if not name:
        raise ValueError("host required")

    def fn(d):
        if not isinstance(d.get("sites"), dict):
            d["sites"] = json.loads(json.dumps(DEFAULT_SITES))
        return d["sites"].pop(name, None) is not None
    return _update(root, fn)


_IGNORABLE = dict.fromkeys(
    [0x00AD, 0x034F, 0x180B, 0x180C, 0x180D, 0x180E, 0x180F, 0x2060, 0x2061, 0x2062, 0x2063, 0x2064, 0xFEFF]
    + list(range(0x200B, 0x2010)) + list(range(0xFE00, 0xFE10)))
_FORBIDDEN_HOST = frozenset(map(chr, range(0x20))) | frozenset(' #/:<>?@[\\]^|%\x7f')
_DOTS = {0x3002: ".", 0xFF0E: ".", 0xFF61: "."}


def _browser_normalize(url):
    """Shape a URL the way a browser reads it: trim C0 controls and spaces, drop tab and
    newlines anywhere, read a backslash as a slash, and for http(s) force "scheme://"
    whatever run of slashes follows the colon."""
    url = url.strip("".join(chr(i) for i in range(0x21)))
    url = url.replace("\t", "").replace("\n", "").replace("\r", "").replace("\\", "/")
    scheme, colon, rest = url.partition(":")
    if colon and scheme.lower() in ("http", "https"):
        url = scheme + "://" + rest.lstrip("/")
    return url


def _split(url):
    if not isinstance(url, str):
        return None
    try:
        parts = urlsplit(_browser_normalize(url))
    except ValueError:
        return None
    return parts if parts.scheme.lower() in ("http", "https") else None


def is_web_url(url):
    """True when the URL is http or https once read like a browser reads it."""
    return _split(url) is not None


def host_of(url):
    """The fully normalized host of a web URL (percent-decoded, NFKC, ignorables dropped,
    dots mapped, lowercase, IDNA ascii when it converts), or None when there is no usable
    host or the URL is not http(s). Never raises."""
    parts = _split(url)
    if parts is None:
        return None
    try:
        host = parts.hostname or ""
    except ValueError:
        return None
    bracketed = parts.netloc.rpartition("@")[2].startswith("[")
    forbidden = _FORBIDDEN_HOST - {":"} if bracketed else _FORBIDDEN_HOST
    host = unquote(host)
    if forbidden & set(host):
        return None
    host = unicodedata.normalize("NFKC", host)
    host = host.translate(_IGNORABLE).translate(_DOTS).lower().rstrip(".")
    if forbidden & set(host):
        return None
    if not host:
        return None
    try:
        return host.encode("idna").decode("ascii")
    except UnicodeError:
        return host


def site_of(url, root=bots.ROOT, listed=None):
    """The listed site a URL belongs to, or None. Never raises. `listed` is a sites()
    result to reuse when judging many URLs at once.

    It judges only the string it is given. The relay must deny a web URL (is_web_url)
    whose host_of is None, and must re-check the engine's resolved current URL after
    navigation, because a redirect or script can land elsewhere."""
    host = host_of(url)
    if not host:
        return None
    best = None
    for site in (sites(root) if listed is None else listed):
        if host == site or host.endswith("." + site):
            if best is None or len(site) > len(best):
                best = site
    return best


def epochs(root=bots.ROOT):
    """{ws: epoch} of every workspace with one."""
    return {ws: e for ws, e in _dict(load(root).get("epochs")).items()
            if isinstance(ws, str) and isinstance(e, str) and EPOCH_RE.fullmatch(e)}


def _ensure_epoch(d, ws):
    rows = d.get("epochs") if isinstance(d.get("epochs"), dict) else {}
    d["epochs"] = rows
    e = rows.get(ws)
    if isinstance(e, str) and EPOCH_RE.fullmatch(e):
        return e
    old = d.get("retired") if isinstance(d.get("retired"), list) else []
    taken = {v for v in list(rows.values()) + old if isinstance(v, str)}  # never a retired one
    e = secrets.token_hex(3)
    while e in taken:
        e = secrets.token_hex(3)
    rows[ws] = e
    return e


def with_epoch(ws, exists, then, root=bots.ROOT):
    """then(epoch) under LOCK, only while exists() holds: forget_workspace takes the same lock,
    so a workspace deleted meanwhile never gets an epoch (or what `then` makes). None if gone."""
    with LOCK:
        if not exists():
            return None
        data = load(root)
        had = _dict(data.get("epochs")).get(ws)
        e = _ensure_epoch(data, ws)
        if e != had:
            _save(root, data)
        return then(e)


def epoch(ws, root=bots.ROOT):
    """This workspace's browser epoch, made (unique among live ones) on first use."""
    have = epochs(root).get(ws)
    if have:
        return have

    return _update(root, lambda d: _ensure_epoch(d, ws))


def retired(root=bots.ROOT):
    """Epochs of deleted workspaces not yet tidied away, never a live one."""
    rows = load(root).get("retired")
    live = set(epochs(root).values())
    return [e for e in rows if isinstance(e, str) and EPOCH_RE.fullmatch(e) and e not in live] \
        if isinstance(rows, list) else []


def clear_retired(done, root=bots.ROOT):
    """These retired epochs have nothing left on disk."""
    done = set(done)

    def fn(d):
        rows = d.get("retired") if isinstance(d.get("retired"), list) else []
        d["retired"] = [e for e in rows if e not in done]
    if done:
        _update(root, fn)


def forget_workspace(ws, root=bots.ROOT):
    """A deleted project: a new one with the same slug starts with browsing off, and a new
    epoch (new engine session names) on its first browse."""
    if ws == bots.HQ:
        return
    with LOCK:
        data = load(root)
        changed = False
        old = _dict(data.get("epochs")).get(ws)
        if isinstance(old, str) and EPOCH_RE.fullmatch(old):
            gone = [e for e in data.get("retired") or [] if isinstance(e, str)] if isinstance(
                data.get("retired"), list) else []
            data["retired"] = gone + ([old] if old not in gone else [])
            changed = True
        for key in ("bots", "epochs"):
            rows = data.get(key)
            if isinstance(rows, dict) and ws in rows:
                del rows[ws]
                changed = True
        if changed:
            _save(root, data)
