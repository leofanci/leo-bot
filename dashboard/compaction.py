"""compaction.py: when a thread's context has grown past the threshold.

Each engine reports `context` on its reply: the prompt size, in tokens, of the turn's last model
call. Past `LEOBOT_COMPACT_TOKENS` (default 60000; 0 turns it off) the next turn starts a fresh
session that gets the thread's visible messages as a recap (server), and the API engine cuts
the history it resends to the same size (engines/api.py).
"""
import os

DEFAULT_TOKENS = 60_000
CHARS_PER_TOKEN = 4  # rough, for sizing stored history by its JSON length


def threshold():
    """Tokens; 0 means off (also for a value that is not a whole number above 0)."""
    raw = os.environ.get("LEOBOT_COMPACT_TOKENS")
    if raw is None:
        return DEFAULT_TOKENS
    try:
        value = int(raw.strip())
    except ValueError:
        return 0
    return max(0, value)


def last_context(rec, engine_id):
    """The context of the thread's newest reply on `engine_id` from the thread's own session
    (a consulted teammate runs in its own), or None when that reply did not report one."""
    for m in reversed((rec or {}).get("messages") or []):
        if not isinstance(m, dict) or m.get("role") != "assistant" or m.get("asked_by"):
            continue
        if m.get("engine") != engine_id:
            continue
        ctx = m.get("context")
        return ctx if type(ctx) is int else None
    return None


def due(rec, engine_id):
    limit = threshold()
    ctx = last_context(rec, engine_id)
    return bool(limit) and ctx is not None and ctx >= limit
