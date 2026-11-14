"""browser.py: bot browser settings, in gitignored team/browser.json.

  bots.<ws>.<bot>   {"level": "off"|"read"|"write"|"act", "mode": "bot"|"my_chrome"}
  sites.<host>      {"mode": None|"bot"|"my_chrome", "daily": None|1..500}

Bots default to level off, mode bot. Sites hold per-site overrides: a mode that
beats the bot's, and a daily action cap. A site with neither stays listed.
Hosts match by suffix, so www.linkedin.com belongs to linkedin.com but
evil-linkedin.com does not. Stored sites replace the defaults once saved.

The lock guards threads in this process only, same as core/integrations.py.
A file of the wrong shape reads as defaults and is overwritten by the next write.
"""
import json
import threading
from pathlib import Path
from urllib.parse import urlsplit

from core import bots

LOCK = threading.Lock()
LEVELS = ("off", "read", "write", "act")
MODES = ("bot", "my_chrome")
DEFAULT_SITES = {
    "linkedin.com": {"mode": None, "daily": 20},
    "instagram.com": {"mode": None, "daily": 10},
}
MAX_DAILY = 500


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


def _browser_normalize(url):
    """Parse a URL the way a browser does: trim C0 controls and spaces, drop tab and
    newlines anywhere, read a backslash as a slash."""
    url = url.strip("".join(chr(i) for i in range(0x21)))
    return url.replace("\t", "").replace("\n", "").replace("\r", "").replace("\\", "/")


def site_of(url, root=bots.ROOT):
    """The listed site a URL belongs to, or None. Never raises."""
    try:
        if not isinstance(url, str):
            return None
        parts = urlsplit(_browser_normalize(url))
        if parts.scheme.lower() not in ("http", "https"):
            return None
        host = (parts.hostname or "").rstrip(".")
    except ValueError:
        return None
    if not host:
        return None
    best = None
    for site in sites(root):
        if host == site or host.endswith("." + site):
            if best is None or len(site) > len(best):
                best = site
    return best


def forget_workspace(ws, root=bots.ROOT):
    """A deleted project: a new one with the same slug starts with browsing off."""
    if ws == bots.HQ:
        return
    with LOCK:
        data = load(root)
        bots_map = data.get("bots")
        if isinstance(bots_map, dict) and ws in bots_map:
            del bots_map[ws]
            _save(root, data)
