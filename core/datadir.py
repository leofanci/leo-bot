"""The data folder: its marker and the one-time move of data out of a clone.

Existing installs keep their data inside the checkout. move_legacy() carries it into the
data folder once, after a backup, and writes the marker last so a crash mid-move leaves the
legacy data detectable on the next start.
"""
import datetime
import json
import re
import shutil
import sys
import tarfile
from dataclasses import dataclass, field
from pathlib import Path

from core import migrations, paths

MARKER = migrations.MARKER
LEGACY_ITEMS: tuple = ("projects", "portfolio", "chats", "team", "database/data", "database/os.db",
                       "os.db", "dashboard/os.db", "dashboard/data.db", "dashboard/.auth-token",
                       "dashboard/server.log")
# Leftovers that are dropped, not moved: the token and log now live in RUN and LOGS.
DROPPED = ("dashboard/.auth-token", "dashboard/server.log")
PLACEHOLDER = ".gitkeep"  # tracked in the checkout; never moved, never deleted
_TEXT_SUFFIXES = (".json", ".jsonl", ".md")


@dataclass
class MoveResult:
    moved: list = field(default_factory=list)
    backup: "Path | None" = None
    refused: "str | None" = None


def _has_content(p: Path) -> bool:
    if p.is_dir():
        return any(f.name != PLACEHOLDER for f in p.rglob("*") if f.is_file() or f.is_symlink())
    return p.exists() or p.is_symlink()


def legacy_items(source: Path) -> list:
    return [i for i in LEGACY_ITEMS if _has_content(Path(source) / i)]


def data_is_empty(data: Path) -> bool:
    data = Path(data)
    return not data.exists() or all(p.name == MARKER for p in data.iterdir())


def _stamp() -> str:
    return datetime.datetime.now().strftime("%Y%m%d-%H%M%S-%f")


def _tar(items: list, base: Path, dest: Path) -> Path:
    dest.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    skip = lambda ti: None if Path(ti.name).name == PLACEHOLDER else ti  # noqa: E731
    with tarfile.open(dest, "w:gz") as t:
        for rel in items:
            t.add(base / rel, arcname=rel, filter=skip)
    dest.chmod(0o600)
    return dest


def _move_item(src: Path, dst: Path) -> None:
    dst.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if src.is_dir() and (src / PLACEHOLDER).exists():
        dst.mkdir(mode=0o700, parents=True, exist_ok=True)
        for child in sorted(src.iterdir()):
            if child.name != PLACEHOLDER:
                shutil.move(str(child), str(dst / child.name))
    else:
        shutil.move(str(src), str(dst))


def rewrite_paths(data: Path, source: Path, items: list) -> int:
    """Point absolute paths stored inside moved text files at the new place.

    Only `<source>/<moved item>` prefixes change; code paths (`<source>/dashboard/app.js`),
    folders outside the clone and look-alike names (`<source>-other/...`) stay as they are.
    Returns the number of files rewritten."""
    names = [i for i in items if i not in DROPPED]
    if not names:
        return 0
    alt = "|".join(re.escape(n) for n in sorted(names, key=len, reverse=True))
    n = 0
    pairs = {(str(source), str(data)),
             (json.dumps(str(source))[1:-1], json.dumps(str(data))[1:-1])}
    pats = [(re.compile(r"(?<![\w.~-])" + re.escape(o) + r"/(" + alt + r")(?![\w-]|\.\w)"), new)
            for o, new in sorted(pairs)]
    for name in names:
        root = Path(data) / name
        files = [f for f in root.rglob("*") if f.is_file()] if root.is_dir() else [root]
        for f in files:
            if f.suffix not in _TEXT_SUFFIXES or f.is_symlink():
                continue
            try:
                text = f.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            out = text
            for pat, new in pats:
                out = pat.sub(lambda m, new=new: f"{new}/{m.group(1)}", out)
            if out != text:
                f.write_text(out, encoding="utf-8")
                n += 1
    return n


def _new_marker(data: Path) -> None:
    migrations.write_marker(data, {"schema": 1, "created": datetime.datetime.now(datetime.timezone.utc)
                                   .isoformat(timespec="seconds")})


def move_legacy(source: Path, data: Path = None, *, replace: bool = False) -> MoveResult:
    source = Path(source)
    data = Path(paths.DATA if data is None else data)
    items = legacy_items(source)
    res = MoveResult()
    if not items:
        data.mkdir(mode=0o700, parents=True, exist_ok=True)
        if not (data / MARKER).exists():
            _new_marker(data)
        return res
    if not data_is_empty(data) and not replace:
        res.refused = (f"Leo Bot found data in two places: {source} (old, inside the app folder) and "
                       f"{data} (the home folder). Nothing was moved. Run `leobot doctor` to see the "
                       f"choices: keep the home folder and archive the old copy, or "
                       f"`leobot move-data --from {source} --replace`, which backs up {data} first.")
        return res
    stamp = _stamp()
    res.backup = _tar([i for i in items if i not in DROPPED], source, paths.BACKUPS / f"legacy-{stamp}.tar.gz")
    data.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not data_is_empty(data):  # replace: keep a copy, then clear
        names = sorted(p.name for p in data.iterdir())
        _tar(names, data, paths.BACKUPS / f"replaced-{stamp}.tar.gz")
        for name in names:
            p = data / name
            shutil.rmtree(p) if p.is_dir() and not p.is_symlink() else p.unlink()
    elif (data / MARKER).exists():
        (data / MARKER).unlink()
    for rel in items:
        src = source / rel
        if rel in DROPPED:
            src.unlink(missing_ok=True)
            continue
        _move_item(src, data / rel)
        res.moved.append(rel)
    rewrite_paths(data, source, res.moved)
    _new_marker(data)
    return res


def prepare() -> None:
    """Run at server start, under the single-server lock."""
    paths.ensure_dirs()
    if legacy_items(paths.CODE):
        r = move_legacy(paths.CODE)
        if r.refused:
            sys.exit(r.refused)
    elif not (paths.DATA / MARKER).exists():
        _new_marker(paths.DATA)
    migrations.run(paths.DATA)
