"""spend.py: owner and workspace daily caps over the per-thread spend ledger.

Each saved thread keeps `spend` rows {"ts", "by", "cost", "ws"} (chat_store.record_turn).
A cap is dollars per rolling 24 hours, read from the environment on every check:
LEOBOT_WORKSPACE_DAILY_CAP (every turn in one workspace) and LEOBOT_OWNER_DAILY_CAP
(the owner's own turns). Unset, empty, 0, negative or not a number means no cap.
The per-person cap (LEOBOT_PERSON_DAILY_CAP) stays in chat_store.over_cap.
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
ENV = {"workspace": "LEOBOT_WORKSPACE_DAILY_CAP", "owner": "LEOBOT_OWNER_DAILY_CAP"}
_LOCK = threading.Lock()


def cap_value(kind):
    """The cap in dollars, or 0.0 for no cap."""
    try:
        value = float(os.environ.get(ENV[kind], "") or 0)
    except ValueError:
        return 0.0
    return value if math.isfinite(value) and value > 0 else 0.0


def spent(kind, key=None, now=None, records=None):
    """(total over the last 24 hours, ts of the oldest counted row or None).
    `owner` counts rows by the owner; `workspace` counts rows with ws == key.
    `records` = the saved-thread source (default chat_store.records; the server passes
    its own chat_store copy, imported as a top-level module)."""
    now = time.time() if now is None else now
    cutoff = now - WINDOW
    total, oldest = 0.0, None
    for rec in (records or chat_store.records)():
        for row in rec.get("spend") or ():
            if not isinstance(row, dict):
                continue
            if kind == "owner" and row.get("by") != "owner":
                continue
            if kind == "workspace" and (not key or row.get("ws") != key):
                continue
            try:
                ts, cost = float(row.get("ts") or 0), float(row.get("cost") or 0)
            except (TypeError, ValueError):
                continue
            if ts < cutoff or ts > now or cost <= 0:
                continue
            total += cost
            oldest = ts if oldest is None else min(oldest, ts)
    return total, oldest


def _caps(ws, by):
    """(kind, key, cap) for each cap set for this turn, workspace first."""
    out = []
    if ws and cap_value("workspace"):
        out.append(("workspace", ws, cap_value("workspace")))
    if by == "owner" and cap_value("owner"):
        out.append(("owner", None, cap_value("owner")))
    return out


def refusal(ws, by, now=None, pending=0.0, records=None):
    """Text that stops a run when a cap is reached, else None. `pending` is what this
    message already spent but has not saved yet (the turn is saved when it ends)."""
    now = time.time() if now is None else now
    for kind, key, cap in _caps(ws, by):
        total, oldest = spent(kind, key, now, records)
        if total + (pending or 0) < cap:
            continue
        frees = time.strftime("%H:%M", time.localtime((oldest if oldest is not None else now) + WINDOW))
        who = "this workspace reached its" if kind == "workspace" else "you reached your"
        return (f"Paused: {who} daily cap (${cap:.2f} per 24 hours). Room frees up from {frees}. "
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
    for kind, key, cap in _caps(ws, by):
        total, _ = spent(kind, key, now, records)
        share = (total + (pending or 0)) / cap
        if share < NEAR:
            continue
        slot = "owner" if kind == "owner" else f"ws:{key}"
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
        whose = "this workspace's" if kind == "workspace" else "your"
        return f"Heads up: {pct}% of {whose} daily cap is used."
    return None
