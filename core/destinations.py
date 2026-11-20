"""destinations.py: where bots of a workspace have sent things before (the new-destination rule).

A bot set to "Sends without asking" runs app writes on its own, and a bot browser at "Acts on
its own" sends from pages on its own. Text the bot read (an email, a page, app data) can name
a recipient, so an injected line could pick where private data goes. The rule: once a request
holds outside content (dashboard/taint.py), a send that names a destination this workspace has
never used waits for one Approve click, at any autonomy level. A known destination still runs
on its own, as the owner chose.

A destination becomes known when:
  - the owner approves a held action or a planned browser send that names it;
  - a bot sends to it from a request that holds no outside content (the owner's own words);
  - the owner lists a site in Settings, Browser (browser sends only: core/browser.sites).

What counts as a destination in an app call's arguments (fail-safe: a false match only asks):
  - every email address, international phone number (+...) and web host found in any argument value, except
    message-body fields (body, text, subject...), whose words are content, not an address;
  - the whole value of a recipient-like field (to, cc, channel, chat_id, user, webhook, thread_id:
    a reply goes to whoever wrote the thread...).

Stored in gitignored team/destinations.json: {"<ws>": {"<kind>:<value>": first seen}}.
"""
import json
import re
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

from core import bots

_LOCK = threading.Lock()
MAX_PER_WS = 5000  # newest kept: an old destination falls out and asks once more
MAX_VALUE = 200
_MAX_NODES = 2000  # argument values walked per call; past it the call counts as naming a new one

_EMAIL = re.compile(r"[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9.-]{1,253}\.[A-Za-z]{2,63}")
_URL = re.compile(r"(?i)\b(?:https?|wss?|ftp)://[^\s\"'<>]+")
_PHONE = re.compile(r"(?<![\w+])\+\d[\d\s().-]{6,22}\d(?![\w])")  # international form only: dates never match
# Field names whose value is the message, not where it goes.
_BODY_KEYS = re.compile(r"(?i)(^|_)(body|text|message|content|html|markdown|subject|title|caption|comment|"
                        r"description|note|notes|summary|query|q|search|prompt|blocks|attachments?)$")
# Field names whose whole value says where something goes.
_RECIPIENT_KEYS = re.compile(
    r"(?i)(^|_)(to|cc|bcc|recipients?|receivers?|emails?|phones?|channels?|chats?|users?|usernames?|handles?|"
    r"webhooks?|urls?|endpoints?|address(es)?|targets?|destinations?|dest|conversations?|rooms?|spaces?|groups?|"
    r"lists?|audiences?|members?|attendees?|invitees?|share_with|grantees?|threads?|reply_to|in_reply_to|"
    r"messages?_id)"
    # what a field name may end with after the word: chat_id, recipient_email, channel_ids, phone_number
    r"(_?(ids?|emails?|address(es)?|names?|numbers?|urls?|handles?|keys?))?$")  # ends with it: not to_date


def _path(root):
    return Path(root) / "team" / "destinations.json"


def _load(root):
    try:
        data = json.loads(_path(root).read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _norm(value):
    return " ".join(str(value).split()).lower()[:MAX_VALUE]


def _host(url):
    try:
        host = urlsplit(url.strip()).hostname
    except ValueError:
        return None
    return host.lower().rstrip(".") if host else None


def _phone(raw):
    digits = re.sub(r"\D", "", raw)
    return digits if 8 <= len(digits) <= 15 else None


def _from_text(text, out):
    for m in _EMAIL.finditer(text):
        out.add("email:" + m.group(0).lower())
    for m in _URL.finditer(text):
        host = _host(m.group(0))
        if host:
            out.add("host:" + host)
    for m in _PHONE.finditer(text):
        digits = _phone(m.group(0))
        if digits:
            out.add("phone:" + digits)


def of_arguments(arguments):
    """The set of destinations an app call's arguments name, or None when they cannot all be
    read (too large or too deep): the caller treats None as naming a new destination."""
    out, seen = set(), [0]

    def walk(value, key, depth):
        seen[0] += 1
        if seen[0] > _MAX_NODES or depth > 12:
            raise OverflowError
        if isinstance(value, dict):
            for k, v in value.items():
                walk(v, str(k), depth + 1)
        elif isinstance(value, (list, tuple)):
            for v in value:
                walk(v, key, depth + 1)
        elif isinstance(value, (str, int)) and not isinstance(value, bool):
            text = str(value)
            if key and _BODY_KEYS.search(key):
                return
            found = set()
            _from_text(text, found)
            out.update(found)
            if not found and key and _RECIPIENT_KEYS.search(key) and text.strip():
                out.add("to:" + _norm(text))  # a channel, user id or handle: the value itself

    try:
        walk(arguments, None, 0)
    except OverflowError:
        return None
    return out


def site(url):
    """The destination of a browser send: the page's host."""
    host = _host(url) if isinstance(url, str) else None
    return {"host:" + host} if host else None


def unknown(ws, dests, root=bots.ROOT):
    """The destinations in `dests` this workspace has not sent to (all of them when `dests` is None
    stands for "could not read": returned as {"?"})."""
    if dests is None:
        return {"?"}
    known = _load(root).get(ws)
    known = known if isinstance(known, dict) else {}
    out = set()
    for d in dests:
        if d in known:
            continue
        if d.startswith("host:"):  # a known host covers its subdomains' pages, not the other way
            parts = d[5:].split(".")
            if any("host:" + ".".join(parts[i:]) in known for i in range(1, len(parts) - 1)):
                continue
        out.add(d)
    return out


def learn(ws, dests, root=bots.ROOT, now=None):
    """Mark `dests` as used by this workspace (owner approval, or a clean request's own send)."""
    if not dests or not isinstance(ws, str):
        return
    now = time.time() if now is None else now
    with _LOCK:
        data = _load(root)
        rows = data.get(ws) if isinstance(data.get(ws), dict) else {}
        for d in dests:
            if isinstance(d, str) and d != "?":
                rows.setdefault(d, now)
        if len(rows) > MAX_PER_WS:
            rows = dict(sorted(rows.items(), key=lambda kv: kv[1] if isinstance(kv[1], (int, float)) else 0)
                        [-MAX_PER_WS:])
        data[ws] = rows
        path = _path(root)
        path.parent.mkdir(parents=True, exist_ok=True)
        bots._atomic_write(path, json.dumps(data, indent=1, sort_keys=True) + "\n")


def forget_workspace(ws, root=bots.ROOT):
    """A deleted workspace's destinations go with it: a reused slug starts asking again."""
    with _LOCK:
        data = _load(root)
        if data.pop(ws, None) is not None:
            bots._atomic_write(_path(root), json.dumps(data, indent=1, sort_keys=True) + "\n")


def label(dests):
    """Short text naming new destinations for a card or a bot note."""
    shown = sorted(d.split(":", 1)[-1] for d in dests if d != "?")
    if not shown:
        return "a destination Leo Bot could not read"
    return ", ".join(shown[:5]) + (f" and {len(shown) - 5} more" if len(shown) > 5 else "")
