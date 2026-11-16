"""canvas_store.py: file store for canvases (documents of blocks) and lists (tables).

Layout under base(root, ws):
    canvases/<cvN>.json            canvas
    canvases/templates/<tpN>.json  saved templates
    canvases/history/<id>.jsonl    change log (rotates to <id>.1.jsonl at 2 MB)
    canvases/.trash/               trashed files, purged after 30 days
    canvases/pins.json             {room id: [file ids]}
    lists/<lsN>.json               list (same history/.trash layout)

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


def next_id(root: Path, ws: str, prefix: str) -> str:
    d = _dir(root, ws, prefix)
    nums = _nums(d, prefix) + _nums(_trash_dir(root, ws, prefix), prefix)
    return f"{prefix}{max(nums, default=0) + 1}"


def next_child_id(items: list[dict], prefix: str) -> str:
    rx = re.compile(rf"^{re.escape(prefix)}(\d+)$")
    nums = [int(m.group(1)) for it in items or []
            if (m := rx.match(str((it or {}).get("id", ""))))]
    return f"{prefix}{max(nums, default=0) + 1}"


def list_ids(root: Path, ws: str, prefix: str) -> list[str]:
    return [f"{prefix}{n}" for n in sorted(_nums(_dir(root, ws, prefix), prefix))]


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


def _drop_lock(target: Path) -> None:
    with contextlib.suppress(OSError):
        Path(f"{target}.lock").unlink()


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
        return write(root, ws, doc)


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
    """Move a file to .trash. `held`: the caller already holds `locked()` for it
    (the lock file is then left in place for the caller to release)."""
    p = _path(root, ws, file_id)
    td = _trash_dir(root, ws, _file_id(file_id).group(1))
    td.mkdir(parents=True, exist_ok=True)
    dest = td / p.name
    try:
        with contextlib.nullcontext() if held else locked(root, ws, file_id):
            if not p.is_file():
                raise LookupError(file_id)
            os.replace(p, dest)
            os.utime(dest, None)
    finally:
        if not held:
            _drop_lock(p)


def from_trash(root: Path, ws: str, file_id: str) -> None:
    p = _path(root, ws, file_id)
    src = _trash_dir(root, ws, _file_id(file_id).group(1)) / p.name
    if not src.is_file():
        raise LookupError(file_id)
    with locked(root, ws, file_id):
        os.replace(src, p)


def purge_trash(root: Path, ws: str, days: int = TRASH_DAYS, now: float | None = None) -> list[str]:
    now = time.time() if now is None else now
    gone: list[str] = []
    for prefix in ("cv", "ls", "tp"):
        td = _trash_dir(root, ws, prefix)
        if not td.is_dir():
            continue
        for f in sorted(td.glob(f"{prefix}*.json")):
            if not _FILE_RE.match(f.stem) or now - f.stat().st_mtime < days * 86400:
                continue
            with locked(root, ws, f.stem):
                f.unlink()
                hdir = _history_dir(root, ws, prefix)
                for h in (hdir / f"{f.stem}.jsonl", hdir / f"{f.stem}.1.jsonl"):
                    with contextlib.suppress(OSError):
                        h.unlink()
                    _drop_lock(h)
            _drop_lock(_path(root, ws, f.stem))
            gone.append(f.stem)
    return gone


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


def templates(root: Path, ws: str) -> list[dict]:
    out = []
    for p in sorted(_SHIPPED.glob("*.json")):
        with contextlib.suppress(OSError, ValueError):
            out.append({**json.loads(p.read_text(encoding="utf-8")), "id": p.stem, "shipped": True})
    for tid in list_ids(root, ws, "tp"):
        with contextlib.suppress(LookupError):
            out.append({**read(root, ws, tid), "shipped": False})
    return out
