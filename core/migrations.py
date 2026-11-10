"""Numbered, idempotent data migrations. MIGRATIONS[i] brings the data to schema i + 1.

Schema 1 is the one-time move out of a clone (core.datadir), so its function is a no-op:
by the time the marker says 1, the move has already happened.
"""
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Callable

from core import version

MARKER = ".leobot-data"


def _initial(data: Path) -> None:
    """Schema 1: the move itself (core.datadir.move_legacy). Nothing to do."""


MIGRATIONS: list[Callable[[Path], None]] = [_initial]
LATEST = len(MIGRATIONS)


def read_marker(data: Path) -> dict:
    try:
        rec = json.loads((data / MARKER).read_text())
        return rec if isinstance(rec, dict) else {}
    except (OSError, ValueError):
        return {}


def write_marker(data: Path, rec: dict) -> None:
    data.mkdir(mode=0o700, parents=True, exist_ok=True)
    rec = {**rec, "version": version.current()}  # which app wrote it, for the newer-data message
    fd, tmp = tempfile.mkstemp(dir=data, prefix=".marker-")
    with os.fdopen(fd, "w") as f:
        json.dump(rec, f)
        f.flush()
        os.fsync(f.fileno())
    os.chmod(tmp, 0o600)
    os.replace(tmp, data / MARKER)


def run(data: Path) -> int:
    rec = read_marker(data)
    schema = rec.get("schema", 0)
    if not isinstance(schema, int):
        schema = 0
    if schema > LATEST:
        ver = rec.get("version")
        which = f"schema {schema}, version {ver}" if isinstance(ver, str) and ver else f"schema {schema}"
        sys.exit(f"Your data is from a newer Leo Bot ({which}). Run: leobot update")
    while schema < LATEST:
        MIGRATIONS[schema](data)
        schema += 1
        rec["schema"] = schema
        write_marker(data, rec)
    return schema
