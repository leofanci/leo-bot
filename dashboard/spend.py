"""spend.py: daily spending caps over the per-thread spend ledger.

Each saved thread keeps `spend` rows {"ts", "by", "cost", "ws"} (chat_store.record_turn).
A cap is dollars per rolling 24 hours, read from the environment on every check:
- LEOBOT_PERSON_DAILY_CAP: each invited person's turns. Default 5; a value that is not a
  number keeps the default, 0 or less turns it off.
- LEOBOT_WORKSPACE_DAILY_CAP: every turn in one workspace. Off unless set.
- LEOBOT_OWNER_DAILY_CAP: the owner's own turns. Off unless set.
For the last two, unset, empty, 0, negative or not a number means no cap.
"""
import json
import math
import os
import threading
import time
from pathlib import Path

from core import bots
from dashboard import chat_store

WINDOW = 86_400
NEAR = 0.8  # share of a cap that earns the once-a-day heads-up
ENV = {"person": "LEOBOT_PERSON_DAILY_CAP", "workspace": "LEOBOT_WORKSPACE_DAILY_CAP",
       "owner": "LEOBOT_OWNER_DAILY_CAP"}
DEFAULT = {"person": 5.0}  # an invited person is capped unless the owner turns it off
OWNER = "owner"
PERSON_TEXT = ("You have used today's allowance for this workspace. "
               "Try again tomorrow, or ask the owner to raise it.")
_LOCK = threading.Lock()


def cap_value(kind):
    """The cap in dollars, or 0.0 for no cap."""
    default = DEFAULT.get(kind, 0.0)
    raw = os.environ.get(ENV[kind])
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw or 0)
    except ValueError:
        return default
    if not math.isfinite(value):
        return default
    return value if value > 0 else 0.0


def _counts(kind, key, row):
    if kind == "workspace":
        return bool(key) and row.get("ws") == key
    return row.get("by") == key  # owner and person caps: whose turn it was


def _spent_many(caps, now, records=None):
    """[(total, oldest ts or None)] for each (kind, key, ...) in `caps`, in one pass over the
    saved threads. `records` = the saved-thread source (default chat_store.records; the server
    passes its own chat_store copy, imported as a top-level module)."""
    cutoff = now - WINDOW
    out = [[0.0, None] for _ in caps]
    if not caps:
        return out
    for rec in (records or chat_store.records)():
        for row in rec.get("spend") or ():
            if not isinstance(row, dict):
                continue
            try:
                ts, cost = float(row.get("ts") or 0), float(row.get("cost") or 0)
            except (TypeError, ValueError):
                continue
            if ts < cutoff or ts > now or cost <= 0:
                continue
            for slot, cap in zip(out, caps):
                if _counts(cap[0], cap[1], row):
                    slot[0] += cost
                    slot[1] = ts if slot[1] is None else min(slot[1], ts)
    return [tuple(x) for x in out]


def spent(kind, key=None, now=None, records=None):
    """(total over the last 24 hours, ts of the oldest counted row or None).
    `workspace` counts rows with ws == key; `owner` and `person` count rows by == key."""
    return _spent_many([(kind, key)], time.time() if now is None else now, records)[0]


def _money(value):
    return f"${value:.2f}" if value >= 0.01 else f"${value:.4f}".rstrip("0")


def _caps(ws, by):
    """(kind, key, cap) for each cap set for this turn: the person's allowance, then the
    workspace, then the owner."""
    out = []
    if by and by != OWNER and cap_value("person"):
        out.append(("person", by, cap_value("person")))
    if ws and cap_value("workspace"):
        out.append(("workspace", ws, cap_value("workspace")))
    if by == OWNER and cap_value("owner"):
        out.append(("owner", OWNER, cap_value("owner")))
    return out


def refusal(ws, by, now=None, pending=0.0, records=None):
    """Text that stops a run when a cap is reached, else None. `pending` is what this
    message already spent but has not saved yet (the turn is saved when it ends)."""
    now = time.time() if now is None else now
    caps = _caps(ws, by)
    for (kind, key, cap), (total, oldest) in zip(caps, _spent_many(caps, now, records)):
        if total + (pending or 0) < cap:
            continue
        if kind == "person":
            return PERSON_TEXT
        at = time.localtime((oldest if oldest is not None else now) + WINDOW)
        frees = ("from " if at[:3] == time.localtime(now)[:3] else "tomorrow at ") + time.strftime("%H:%M", at)
        who = "this workspace reached its" if kind == "workspace" else "you reached your"
        return (f"Paused: {who} daily cap ({_money(cap)} per 24 hours). Room frees up {frees}. "
                f"Raise {ENV[kind]} to go on now.")
    return None


def _notices_path(root):
    return Path(root) / "team" / "cap_notices.json"


def _load_notices(root):
    try:
        data = json.loads(_notices_path(root).read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def near_notice(ws, by, root, now=None, pending=0.0, records=None):
    """A heads-up when a cap is at 80% or more, at most once per cap per 24 hours.
    The last notice time is kept in team/cap_notices.json, so a restart does not repeat it.
    `pending` = this message's spend, not saved yet."""
    now = time.time() if now is None else now
    caps = _caps(ws, by)
    for (kind, key, cap), (total, _) in zip(caps, _spent_many(caps, now, records)):
        share = (total + (pending or 0)) / cap
        if share < NEAR:
            continue
        # Per cap and per person who sees it: the owner still hears of a workspace cap
        # an invited person brought near.
        slot = f"ws:{key}:{by}" if kind == "workspace" else f"{kind}:{key}"
        with _LOCK:
            seen = _load_notices(root)
            try:
                last = float(seen.get(slot) or 0)
            except (TypeError, ValueError):
                last = 0.0
            if now - last < WINDOW:
                continue
            seen[slot] = now
            bots._atomic_write(_notices_path(root), json.dumps(seen, indent=2) + "\n")
        pct = min(100, int(share * 100))
        what = {"workspace": "this workspace's daily cap", "owner": "your daily cap",
                "person": "your daily allowance"}[kind]
        return f"Heads up: {pct}% of {what} is used."
    return None
