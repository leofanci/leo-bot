"""job_ledger.py: spend of background runs that have no thread (memory tidy-ups,
later the scheduled jobs).

One row per run in `team/job_ledger.json` (user data, last KEEP rows). `records`
shapes each row like a saved thread, so the caps (spend.py) and the cost report
(costs.py) read it next to the threads: the server chains both
(`server._spend_records`).
"""
import json
from pathlib import Path

from core import bots, memory

RELPATH = Path("team") / "job_ledger.json"
KEEP = 500
_MESSAGE_KEYS = ("ts", "bot", "engine", "engine_label", "model", "usage", "cost", "tokens", "metered", "job")


def _path(root):
    return Path(root) / RELPATH


def _load(path):
    try:
        rows = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []


def append(root, row):
    """Add one run. The file lock covers threads and both copies of this module
    (the server imports its siblings as top-level modules)."""
    path = _path(root)
    with memory.locked(path):
        rows = _load(path)
        rows.append(dict(row))
        bots._atomic_write(path, json.dumps(rows[-KEEP:], ensure_ascii=False))


def _cost(value):
    try:
        cost = float(value)
    except (TypeError, ValueError):
        return 0.0
    return cost if cost > 0 else 0.0


def records(root):
    """Each run as a thread-shaped record: {"job_ws", "spend", "messages"}."""
    out = []
    for r in _load(_path(root)):
        ws = r.get("ws") if isinstance(r.get("ws"), str) and r.get("ws") else bots.HQ
        cost = _cost(r.get("cost"))
        msg = {"role": "assistant", **{k: r.get(k) for k in _MESSAGE_KEYS}}
        out.append({"job_ws": ws,
                    "spend": [{"ts": r.get("ts"), "by": r.get("by") or "owner", "cost": cost, "ws": ws}]
                    if cost else [],
                    "messages": [msg]})
    return out
