"""browser.py: bot browser settings, in gitignored team/browser.json.

  bots.<ws>.<bot>   {"level": "off"|"read"|"write"|"act", "mode": "bot"|"my_chrome"}
  sites.<host>      {"mode": None|"bot"|"my_chrome", "daily": None|1..500}
  epochs.<ws>       six hex, made on first use, removed with the workspace: part of every engine
                    session name there, so a recreated slug never reaches an old browser or login
  retired           epochs of deleted workspaces whose sessions may still be on disk: the only
                    names the start sweep touches (other Leo Bot data roots share the namespace)

Bots default to level off, mode bot. Sites hold per-site overrides: a mode that
beats the bot's, and a daily action cap. A site with neither stays listed.
Hosts match by suffix, so www.linkedin.com belongs to linkedin.com but
evil-linkedin.com does not. Stored sites replace the defaults once saved.

The lock guards threads in this process only, same as core/integrations.py.
A file of the wrong shape reads as defaults and is overwritten by the next write.
"""
import json
import re
import secrets
import threading
import unicodedata
from pathlib import Path
from urllib.parse import unquote, urlsplit

from core import bots

LOCK = threading.Lock()
LEVELS = ("off", "read", "write", "act")
MODES = ("bot", "my_chrome")
DEFAULT_SITES = {
    "linkedin.com": {"mode": None, "daily": 20},
    "instagram.com": {"mode": None, "daily": 10},
}
MAX_DAILY = 500
EPOCH_RE = re.compile(r"[0-9a-f]{6}")


def _path(root):
    return Path(root) / "team" / "browser.json"


def load(root=bots.ROOT):
    try:
        data = json.loads(_path(root).read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save(root, data):
    bots._atomic_write(_path(root), json.dumps(data, indent=2) + "\n")


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
    stored = load(root).get("sites")
    src = stored if isinstance(stored, dict) else DEFAULT_SITES
    src = {k: v for k, v in src.items() if isinstance(k, str)}
    out = {}
    for host, row in src.items():
        row = _dict(row)
        mode, daily = row.get("mode"), row.get("daily")
        out[host] = {
            "mode": mode if mode in MODES else None,
            "daily": daily if isinstance(daily, int) and not isinstance(daily, bool) and 1 <= daily <= MAX_DAILY else None,
        }
    return out


def _host(value):
    if not isinstance(value, str):
        return None
    text = value.strip().lower()
    if not text:
        return None
    try:
        host = urlsplit(text if "//" in text else "//" + text).hostname
    except ValueError:
        return None
    return host.rstrip(".") if host else None


def set_site(host, mode, daily, root=bots.ROOT):
    name = _host(host)
    if not name or "." not in name:
        raise ValueError("host required")
    if mode is not None and mode not in MODES:
        raise ValueError("unknown mode")
    if daily is not None and (isinstance(daily, bool) or not isinstance(daily, int) or not 1 <= daily <= MAX_DAILY):
        raise ValueError("daily must be 1 to %d" % MAX_DAILY)
    row = {"mode": mode, "daily": daily}

    def fn(d):
        if not isinstance(d.get("sites"), dict):
            d["sites"] = json.loads(json.dumps(DEFAULT_SITES))
        d["sites"][name] = row
    _update(root, fn)
    return dict(row)


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


def epoch(ws, root=bots.ROOT):
    """This workspace's browser epoch, made (unique among live ones) on first use."""
    have = epochs(root).get(ws)
    if have:
        return have

    def fn(d):
        rows = d.get("epochs") if isinstance(d.get("epochs"), dict) else {}
        d["epochs"] = rows
        e = rows.get(ws)
        if isinstance(e, str) and EPOCH_RE.fullmatch(e):
            return e
        taken = {v for v in rows.values() if isinstance(v, str)}
        e = secrets.token_hex(3)
        while e in taken:
            e = secrets.token_hex(3)
        rows[ws] = e
        return e
    return _update(root, fn)


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
