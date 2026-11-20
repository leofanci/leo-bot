"""halt.py: Stop everything, the owner's kill switch. Deterministic and local: no model reads it.

The owner presses Stop everything (top bar, or `leobot halt` in a terminal). The server then
ends every bot run in flight (the owner's, invited people's, scheduled jobs, runner turns,
browser children) and revokes every turn token, so a tool call already on its way is refused
at the relay. Until the owner presses Resume (`leobot resume`), no bot turn starts and no job
runs. The flag is a file, so a restart keeps it: a stop survives a crash or a reboot.

Stored in team/halt.json: {"since": epoch seconds, "by": "owner" | "cli"}.
"""
import json
import time
from pathlib import Path

from core import bots

MESSAGE = ("Leo Bot is stopped (Stop everything). No bot runs until the owner presses Resume "
           "in the top bar, or runs `leobot resume`.")


def _path(root=None):
    return Path(root or bots.ROOT) / "team" / "halt.json"


def state(root=None):
    """{"since", "by"} while stopped, else None. An unreadable file counts as stopped."""
    p = _path(root)
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text())
    except (OSError, ValueError):
        return {"since": None, "by": "unknown"}
    return data if isinstance(data, dict) else {"since": None, "by": "unknown"}


def halted(root=None):
    return state(root) is not None


def halt(root=None, by="owner"):
    if not halted(root):
        bots._atomic_write(_path(root), json.dumps({"since": time.time(), "by": by}) + "\n")
    return state(root)


def resume(root=None):
    _path(root).unlink(missing_ok=True)
