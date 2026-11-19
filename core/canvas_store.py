"""canvas_store.py: file store for canvases (documents of blocks) and lists (tables).

Layout under base(root, ws):
    canvases/<cvN>.json            canvas
    canvases/templates/<tpN>.json  saved templates
    canvases/history/<id>.jsonl    change log (rotates to <id>.1.jsonl at 2 MB)
    canvases/.trash/               trashed files, purged after 30 days
    canvases/pins.json             {room id: [file ids]}
    lists/<lsN>.json               list (same history/.trash layout)
    <dir>/.seq-<prefix>            highest number ever given: an id is never reused, even purged
    <file>.lock                    an empty lock file per path, never unlinked: a waiter on an
                                   unlinked lock file and a newcomer on its replacement would
                                   both enter (they stay through trash, restore and purge)

Every id is checked against a strict regex before any path is built.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import secrets
import shutil
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from core import bots

HISTORY_MAX_BYTES = 2 * 1024 * 1024
TRASH_DAYS = 30

_PATTERNS = {
    "canvas": r"cv\d{1,6}",
    "list": r"ls\d{1,6}",
    "template": r"tp\d{1,6}",
    "block": r"bk\d{1,6}",
    "row": r"rw\d{1,6}",
    "item": r"i\d{1,6}",
    "change": r"ch[0-9a-f]{12}",
}
_RES = {k: re.compile(f"^{v}$") for k, v in _PATTERNS.items()}
_FILE_RE = re.compile(r"^(cv|ls|tp)(\d{1,6})$")
_SHIPPED = Path(__file__).resolve().parent.parent / "templates" / "canvases"

_path_locks: dict[str, threading.Lock] = {}
_path_locks_guard = threading.Lock()
_history_lock = threading.Lock()  # separate: callers hold locked() while appending


def base(root: Path, ws: str) -> Path:
    root = Path(root)
    return root / "team" if ws == bots.HQ else root / "projects" / ws


def valid(kind: str, value: str) -> bool:
    rx = _RES.get(kind)
    return bool(rx and isinstance(value, str) and rx.match(value))


def _file_id(file_id: str) -> re.Match:
    m = _FILE_RE.match(file_id) if isinstance(file_id, str) else None
    if not m:
        raise ValueError(f"bad file id: {file_id!r}")
    return m


def _dir(root: Path, ws: str, prefix: str) -> Path:
    b = base(root, ws)
    if prefix == "ls":
        return b / "lists"
    if prefix == "tp":
        return b / "canvases" / "templates"
    if prefix == "cv":
        return b / "canvases"
    raise ValueError(f"bad prefix: {prefix!r}")


def _path(root: Path, ws: str, file_id: str) -> Path:
    m = _file_id(file_id)
    return _dir(root, ws, m.group(1)) / f"{file_id}.json"


def _trash_dir(root: Path, ws: str, prefix: str) -> Path:
    return _dir(root, ws, prefix) / ".trash"


def _history_dir(root: Path, ws: str, prefix: str) -> Path:
    """History sits beside canvases/lists; templates share the canvas one."""
    return (_dir(root, ws, "ls") if prefix == "ls" else _dir(root, ws, "cv")) / "history"


def _nums(d: Path, prefix: str) -> list[int]:
    out = []
    if d.is_dir():
        for p in d.glob(f"{prefix}*.json"):
            m = _FILE_RE.match(p.stem)
            if m and m.group(1) == prefix:
                out.append(int(m.group(2)))
    return out


def _seq_path(root: Path, ws: str, prefix: str) -> Path:
    return _dir(root, ws, prefix) / f".seq-{prefix}"


def _seq(root: Path, ws: str, prefix: str) -> int:
    try:
        return int(_seq_path(root, ws, prefix).read_text(encoding="utf-8").strip() or 0)
    except (OSError, ValueError):
        return 0


def _bump_seq(root: Path, ws: str, prefix: str, n: int) -> None:
    """Raise the high-water mark to `n` (never lowers it)."""
    if n > _seq(root, ws, prefix):
        _atomic_write(_seq_path(root, ws, prefix), f"{n}\n")


def next_id(root: Path, ws: str, prefix: str) -> str:
    d = _dir(root, ws, prefix)
    nums = _nums(d, prefix) + _nums(_trash_dir(root, ws, prefix), prefix) + [_seq(root, ws, prefix)]
    return f"{prefix}{max(nums, default=0) + 1}"


def exists(root: Path, ws: str, file_id: str) -> bool:
    return _path(root, ws, file_id).is_file()


def next_child_id(items: list[dict], prefix: str) -> str:
    rx = re.compile(rf"^{re.escape(prefix)}(\d+)$")
    nums = [int(m.group(1)) for it in items or []
            if (m := rx.match(str((it or {}).get("id", ""))))]
    return f"{prefix}{max(nums, default=0) + 1}"


def list_ids(root: Path, ws: str, prefix: str) -> list[str]:
    return [f"{prefix}{n}" for n in sorted(_nums(_dir(root, ws, prefix), prefix))]



def trashed(root: Path, ws: str, prefix: str) -> list[dict]:
    """Files in the trash, newest first: {id, doc, at} (at: when it went there, epoch seconds).
    An unreadable file is skipped."""
    td = _trash_dir(root, ws, prefix)
    out = []
    for n in _nums(td, prefix):
        f = td / f"{prefix}{n}.json"
        try:
            out.append({"id": f.stem, "doc": json.loads(f.read_text(encoding="utf-8")), "at": f.stat().st_mtime})
        except (OSError, ValueError):
            continue
    return sorted(out, key=lambda e: -e["at"])

def read(root: Path, ws: str, file_id: str) -> dict:
    p = _path(root, ws, file_id)
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise LookupError(file_id) from None
    except ValueError as e:
        raise LookupError(f"{file_id}: unreadable ({e})") from None


@contextlib.contextmanager
def _flock(target: Path):
    """Per-path threading.Lock plus fcntl.flock on `<target>.lock` (threads and processes)."""
    key = str(target)
    with _path_locks_guard:
        tl = _path_locks.setdefault(key, threading.Lock())
    with tl:
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(f"{target}.lock", "a") as lf:
            fcntl.flock(lf, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lf, fcntl.LOCK_UN)


def locked(root: Path, ws: str, file_id: str):
    return _flock(_path(root, ws, file_id))


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def write(root: Path, ws: str, doc: dict) -> dict:
    """Atomic write that bumps `version` by 1 and returns doc.

    Protocol: the caller holds `locked()` and has re-read the doc under that lock,
    so the bump is applied to the on-disk version. A doc older than the file on disk
    is refused (ValueError). A doc whose file does not exist yet is created
    exclusively: FileExistsError if the file appeared meanwhile (id collision).
    """
    p = _path(root, ws, doc.get("id", ""))
    cur = int(doc.get("version") or 0)
    exists = p.exists()
    if exists and cur == 0:
        raise FileExistsError(str(p))
    if exists:
        try:
            disk = int(json.loads(p.read_text(encoding="utf-8")).get("version") or 0)
        except (OSError, ValueError, AttributeError):
            disk = 0
        if cur < disk:
            raise ValueError(f"stale doc {doc.get('id')}: version {cur} < on disk {disk}")
    doc["version"] = cur + 1
    text = json.dumps(doc, ensure_ascii=False, indent=2) + "\n"
    if exists or cur > 0:
        _atomic_write(p, text)
    else:
        try:
            _create_exclusive(p, text)
        except FileExistsError:
            doc["version"] = cur
            raise
    return doc


def _create_exclusive(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.link(tmp, path)  # fails with FileExistsError if path exists
    finally:
        with contextlib.suppress(OSError):
            os.unlink(tmp)


def create(root: Path, ws: str, prefix: str, doc: dict) -> dict:
    """Allocate the next id for `prefix`, set doc["id"] and write it, race-free."""
    d = _dir(root, ws, prefix)
    with _flock(d / f".create-{prefix}"):
        doc["id"] = next_id(root, ws, prefix)
        doc["version"] = 0
        out = write(root, ws, doc)
        _bump_seq(root, ws, prefix, int(_file_id(doc["id"]).group(2)))
        return out


def _hist_path(root: Path, ws: str, file_id: str, rotated: bool = False) -> Path:
    m = _file_id(file_id)
    name = f"{file_id}.1.jsonl" if rotated else f"{file_id}.jsonl"
    return _history_dir(root, ws, m.group(1)) / name


def history_append(root: Path, ws: str, file_id: str, entry: dict) -> str:
    p = _hist_path(root, ws, file_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    cid = "ch" + secrets.token_hex(6)
    rec = {**entry, "id": cid,
           "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    with _history_lock, _flock(p):
        if p.exists() and p.stat().st_size >= HISTORY_MAX_BYTES:
            os.replace(p, _hist_path(root, ws, file_id, rotated=True))
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return cid


def history(root: Path, ws: str, file_id: str) -> list[dict]:
    out: list[dict] = []
    for p in (_hist_path(root, ws, file_id, rotated=True), _hist_path(root, ws, file_id)):
        try:
            lines = p.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for ln in lines:
            with contextlib.suppress(ValueError):
                out.append(json.loads(ln))
    return out


def to_trash(root: Path, ws: str, file_id: str, *, held: bool = False) -> None:
    """Move a file to .trash. `held`: the caller already holds `locked()` for it."""
    p = _path(root, ws, file_id)
    if not held and not p.is_file():
        raise LookupError(file_id)  # no lock file for an id that never existed
    td = _trash_dir(root, ws, _file_id(file_id).group(1))
    td.mkdir(parents=True, exist_ok=True)
    dest = td / p.name
    with contextlib.nullcontext() if held else locked(root, ws, file_id):
        if not p.is_file():
            raise LookupError(file_id)
        os.replace(p, dest)
        os.utime(dest, None)


def from_trash(root: Path, ws: str, file_id: str) -> None:
    """Restore a trashed file. LookupError: not in the trash (a concurrent restore took it);
    FileExistsError: the live path is taken."""
    p = _path(root, ws, file_id)
    src = _trash_dir(root, ws, _file_id(file_id).group(1)) / p.name
    if not src.is_file():
        raise LookupError(file_id)  # no lock file for an id that never existed
    with locked(root, ws, file_id):
        if not src.is_file():
            raise LookupError(file_id)
        if p.exists():
            raise FileExistsError(str(p))
        os.replace(src, p)


def purge_trash(root: Path, ws: str, days: int = TRASH_DAYS, now: float | None = None) -> list[str]:
    """Remove trashed files older than `days` with their history. Their numbers stay taken
    (`.seq-<prefix>`) and a purged canvas leaves every room it was still pinned in."""
    now = time.time() if now is None else now
    gone: list[str] = []
    for prefix in ("cv", "ls", "tp"):
        td = _trash_dir(root, ws, prefix)
        if not td.is_dir():
            continue
        for f in sorted(td.glob(f"{prefix}*.json")):
            m = _FILE_RE.match(f.stem)
            if not m or m.group(1) != prefix:
                continue
            with locked(root, ws, f.stem):
                try:
                    if now - f.stat().st_mtime < days * 86400:
                        continue
                except OSError:  # restored meanwhile
                    continue
                with _flock(_dir(root, ws, prefix) / f".create-{prefix}"):
                    _bump_seq(root, ws, prefix, int(m.group(2)))
                f.unlink()
                hdir = _history_dir(root, ws, prefix)
                for h in (hdir / f"{f.stem}.jsonl", hdir / f"{f.stem}.1.jsonl"):
                    with contextlib.suppress(OSError):
                        h.unlink()
            gone.append(f.stem)
    cvs = [g for g in gone if g.startswith("cv")]
    if cvs:
        with pins_lock(root, ws):
            cur = pins(root, ws)
            left = {rid: [c for c in ids if c not in cvs] for rid, ids in cur.items()}
            left = {rid: ids for rid, ids in left.items() if ids}
            if left != cur:
                set_pins(root, ws, left)
    return gone


_WS_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")


def purge_all(root: Path, days: int = TRASH_DAYS, now: float | None = None) -> dict[str, list[str]]:
    """purge_trash for every workspace and HQ: {ws: purged ids} (only those with any)."""
    root = Path(root)
    spaces = [bots.HQ]
    pdir = root / "projects"
    if pdir.is_dir():
        spaces += sorted(d.name for d in pdir.iterdir() if d.is_dir() and _WS_RE.match(d.name))
    out = {}
    for ws in spaces:
        try:
            gone = purge_trash(root, ws, days, now)
        except (OSError, ValueError) as e:  # One bad workspace never skips the others.
            print(f"canvas trash purge failed for {ws}: {e}", file=sys.stderr)
            continue
        if gone:
            out[ws] = gone
    return out


def _pins_path(root: Path, ws: str) -> Path:
    return base(root, ws) / "canvases" / "pins.json"


def pins(root: Path, ws: str) -> dict[str, list[str]]:
    try:
        data = json.loads(_pins_path(root, ws).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): [x for x in v if isinstance(x, str)]
            for k, v in data.items() if isinstance(v, list)}


def set_pins(root: Path, ws: str, pins: dict) -> None:
    _atomic_write(_pins_path(root, ws), json.dumps(pins, ensure_ascii=False, indent=2) + "\n")


def pins_lock(root: Path, ws: str):
    """Hold this around every read-modify-write of pins.json."""
    return _flock(_pins_path(root, ws))


def drop_room_pins(root: Path, ws: str, room_id: str) -> None:
    with pins_lock(root, ws):
        cur = pins(root, ws)
        if room_id in cur:
            del cur[room_id]
            set_pins(root, ws, cur)


def move_room_pins(root: Path, ws: str, old_id: str, new_id: str) -> None:
    """A renamed room keeps its pins: a later room under the old name starts empty."""
    with pins_lock(root, ws):
        cur = pins(root, ws)
        moved = cur.pop(old_id, None)
        if moved is None:
            return
        cur[new_id] = cur.get(new_id, []) + [c for c in moved if c not in cur.get(new_id, [])]
        set_pins(root, ws, cur)


def updated(root: Path, ws: str, file_id: str) -> str | None:
    """When a canvas or list file last changed (UTC ISO), or None."""
    try:
        ts = _path(root, ws, file_id).stat().st_mtime
    except (OSError, ValueError, LookupError):
        return None
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds")


def templates(root: Path, ws: str) -> list[dict]:
    out = []
    for p in sorted(_SHIPPED.glob("*.json")):
        with contextlib.suppress(OSError, ValueError):
            out.append({**json.loads(p.read_text(encoding="utf-8")), "id": p.stem, "shipped": True})
    for tid in list_ids(root, ws, "tp"):
        with contextlib.suppress(LookupError):
            out.append({**read(root, ws, tid), "shipped": False})
    return out
