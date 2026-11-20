"""action_log.py: the record of what bots did that reaches past the conversation. Always on.

Every app write that ran (on its own or after an Approve), every browser send, every shell
command a bot's gate let through, and every Stop everything is one line in
team/actions/<YYYY-MM>.jsonl. Lines are only ever appended (O_APPEND), never rewritten, and no
setting turns the log off: the content of conversations can be deleted, what bots did cannot.

Each line carries `prev` (the hash of the line before it, across months) and `h` (sha256 of
prev + this line's fields), so removing or editing a line breaks the chain: `verify` finds it
(`leobot actions --verify`). This shows tampering after the fact; it cannot stop someone who
holds the owner's account from rewriting the whole folder. Bots never reach team/ (osctl's
read-file refuses it, and edit folders never hold Leo Bot's data folder).

Arguments are kept, cut to MAX_ARGS characters: the log holds what went out, so it is as
private as the data folder (0700 folder, 0600 files).
"""
import fcntl
import hashlib
import json
import os
import threading
import time
from pathlib import Path

from core import bots

MAX_ARGS = 2000
MAX_FIELD = 500
ZERO = "0" * 64
_LOCK = threading.Lock()  # threads here; the lock file below serves other processes (leobot stop)


def _dir(root):
    return Path(root or bots.ROOT) / "team" / "actions"


def _files(root):
    d = _dir(root)
    return sorted(p for p in d.glob("*.jsonl") if p.is_file()) if d.is_dir() else []


def _last_hash(root):
    for p in reversed(_files(root)):
        try:
            with open(p, "rb") as f:
                f.seek(0, os.SEEK_END)
                size = f.tell()
                f.seek(max(0, size - 65536))
                lines = [ln for ln in f.read().splitlines() if ln.strip()]
        except OSError:
            continue
        for ln in reversed(lines):
            try:
                h = json.loads(ln).get("h")
            except (ValueError, AttributeError):
                continue
            if isinstance(h, str) and len(h) == 64:
                return h
    return ZERO


def _digest(prev, fields):
    body = json.dumps(fields, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256((prev + "\n" + body).encode("utf-8")).hexdigest()


def _clip(value):
    if isinstance(value, str):
        return value[:MAX_FIELD]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    text = json.dumps(value, ensure_ascii=False, default=str)
    return text if len(text) <= MAX_ARGS else text[:MAX_ARGS] + "..."


def record(root, kind, **fields):
    """Append one action. Never raises: a full disk must not stop a stop, but it is printed."""
    root = root or bots.ROOT
    entry = {"ts": round(time.time(), 3), "kind": str(kind)[:40]}
    for k, v in fields.items():
        if v is not None:
            entry[k] = _clip(v)
    try:
        with _LOCK:
            d = _dir(root)
            d.mkdir(parents=True, exist_ok=True)
            os.chmod(d, 0o700)
            lock = os.open(d / ".lock", os.O_WRONLY | os.O_CREAT, 0o600)
            try:
                fcntl.flock(lock, fcntl.LOCK_EX)
                prev = _last_hash(root)
                entry["prev"] = prev
                entry["h"] = _digest(prev, {k: v for k, v in entry.items() if k != "h"})
                path = d / (time.strftime("%Y-%m", time.gmtime(entry["ts"])) + ".jsonl")
                fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
                try:
                    os.write(fd, (json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8"))
                finally:
                    os.close(fd)
            finally:
                os.close(lock)  # closing drops the flock
    except OSError as e:
        print(f"[action log] could not write: {e}", flush=True)
    return entry


def entries(root=None, limit=None):
    """Logged actions, oldest first (`limit`: the newest that many)."""
    out = []
    for p in _files(root):
        try:
            lines = p.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for ln in lines:
            try:
                row = json.loads(ln)
            except ValueError:
                continue
            if isinstance(row, dict):
                out.append(row)
    return out[-limit:] if limit else out


def verify(root=None):
    """(ok, lines checked, where it broke or None). Each line must hash to its `h` and point at
    the line before it."""
    prev, n = ZERO, 0
    for p in _files(root):
        try:
            lines = p.read_text(encoding="utf-8").splitlines()
        except OSError as e:
            return False, n, f"{p.name}: unreadable ({e})"
        for i, ln in enumerate(lines, 1):
            if not ln.strip():
                continue
            try:
                row = json.loads(ln)
            except ValueError:
                return False, n, f"{p.name}:{i}: not a log line"
            if not isinstance(row, dict) or row.get("prev") != prev:
                return False, n, f"{p.name}:{i}: a line before it was removed or changed"
            if row.get("h") != _digest(prev, {k: v for k, v in row.items() if k != "h"}):
                return False, n, f"{p.name}:{i}: this line was changed"
            prev, n = row["h"], n + 1
    return True, n, None
