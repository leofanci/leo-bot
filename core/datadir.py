"""The data folder: its marker and the one-time move of data out of a clone.

Existing installs keep their data inside the checkout. move_legacy() carries it into the
data folder once, after a backup. A journal (DATA/.leobot-moving) is written before the first
item moves and removed after the marker, so an interrupted move is finished on the next start
instead of being mistaken for two competing data folders.
"""
import datetime
import errno
import json
import os
import re
import shutil
import stat
import sys
import tarfile
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from core import migrations, paths

MARKER = migrations.MARKER
JOURNAL = ".leobot-moving"
LEGACY_ITEMS: tuple = ("projects", "portfolio", "chats", "team", "database/data", "database/os.db",
                       "os.db", "dashboard/os.db", "dashboard/data.db", "dashboard/.auth-token",
                       "dashboard/server.log")
# Leftovers that are dropped, not moved: the token and log now live in RUN and LOGS.
DROPPED = ("dashboard/.auth-token", "dashboard/server.log")
PLACEHOLDER = ".gitkeep"  # tracked in the checkout; never moved, never deleted
_TEXT_SUFFIXES = (".json", ".jsonl", ".md")
_TMP_PREFIX = ".marker-"  # temp files this module leaves at the top of the data folder
_REWRITE_PREFIX = ".rewrite-"


@dataclass
class MoveResult:
    moved: list = field(default_factory=list)
    backup: "Path | None" = None
    refused: "str | None" = None


def _has_content(p: Path) -> bool:
    if p.is_symlink():
        return True
    if p.is_dir():
        return any(f.name != PLACEHOLDER for f in p.rglob("*") if f.is_file() or f.is_symlink())
    return p.exists()


def legacy_items(source: Path) -> list:
    return [i for i in LEGACY_ITEMS if _has_content(Path(source) / i)]


def _junk(name: str) -> bool:
    return name == ".DS_Store" or name.startswith("._") or name.startswith(_TMP_PREFIX)


def data_is_empty(data: Path) -> bool:
    data = Path(data)
    return not data.exists() or all(p.name == MARKER or _junk(p.name) for p in data.iterdir())


def _stamp() -> str:
    return datetime.datetime.now().strftime("%Y%m%d-%H%M%S-%f")


def _atomic_write(path: Path, text: str, mode: int = 0o600) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=_TMP_PREFIX)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _tar(items: list, base: Path, dest: Path) -> Path:
    dest.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    skip = lambda ti: None if Path(ti.name).name == PLACEHOLDER else ti  # noqa: E731
    try:
        with tarfile.open(dest, "w:gz") as t:
            for rel in items:
                t.add(base / rel, arcname=rel, filter=skip)
        dest.chmod(0o600)
    except BaseException:
        dest.unlink(missing_ok=True)  # never leave a partial backup that looks real
        raise
    return dest


def _size(p: Path) -> int:
    if p.is_symlink() or p.is_file():
        try:
            return p.lstat().st_size
        except OSError:
            return 0
    total = 0
    for root, _dirs, files in os.walk(p):
        for name in files:
            try:
                total += os.lstat(os.path.join(root, name)).st_size
            except OSError:
                pass
    return total


def _anchor(p: Path) -> Path:
    p = Path(p)
    while not p.exists() and p != p.parent:
        p = p.parent
    return p


def _space_problem(source: Path, data: Path, items: list, replace: bool) -> "str | None":
    size = sum(_size(source / i) for i in items)
    need = {}  # device -> [bytes, a path on it]

    def add(path: Path, n: int) -> None:
        a = _anchor(path)
        slot = need.setdefault(os.stat(a).st_dev, [0, a])
        slot[0] += n

    add(paths.BACKUPS, size)  # the tar.gz is at most about the data's size
    if replace:
        add(paths.BACKUPS, _size(data) if data.exists() else 0)
    if os.stat(_anchor(source)).st_dev != os.stat(_anchor(data)).st_dev:
        add(data, size)  # across volumes the move copies first
    for n, a in need.values():
        free = shutil.disk_usage(a).free
        if free < n:
            return (f"Not enough free disk space to move your data safely: about {n // 1_000_000 + 1} MB "
                    f"needed on the volume holding {a}, {free // 1_000_000} MB free. Nothing was moved. "
                    f"Free some space and start Leo Bot again.")
    return None


def _remove(p: Path) -> None:
    if p.is_dir() and not p.is_symlink():
        shutil.rmtree(p)
    else:
        p.unlink(missing_ok=True)


def _lexists(p: Path) -> bool:
    return os.path.lexists(p)


def _move_entry(src: Path, dst: Path) -> None:
    """Resumable move of one entry. Source gone: done. Both there: the source is the leftover of
    an interrupted delete, so drop it. Otherwise rename, or across volumes copy to a .partial
    name, put it in place atomically, then delete the source."""
    if not _lexists(src):
        return
    if _lexists(dst):
        _remove(src)
        return
    dst.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        os.rename(src, dst)
        return
    except OSError as e:
        if e.errno != errno.EXDEV:
            raise
    part = dst.with_name(dst.name + ".partial")
    if _lexists(part):
        _remove(part)
    if src.is_dir() and not src.is_symlink():
        shutil.copytree(src, part, symlinks=True)
    else:
        shutil.copy2(src, part, follow_symlinks=False)
    os.replace(part, dst)
    _remove(src)


def _move_item(src: Path, dst: Path) -> None:
    if src.is_dir() and not src.is_symlink() and (src / PLACEHOLDER).exists():
        dst.mkdir(mode=0o700, parents=True, exist_ok=True)
        for child in sorted(src.iterdir()):
            if child.name != PLACEHOLDER:
                _move_entry(child, dst / child.name)
    else:
        _move_entry(src, dst)


def _spellings(p: Path) -> list:
    out = [str(p), str(Path(os.path.abspath(p))), str(Path(p).resolve())]
    return list(dict.fromkeys(out))


def rewrite_paths(data: Path, source: Path, items: list) -> int:
    """Point absolute paths stored inside moved text files at the new place.

    Only `<source>/<moved item>` prefixes change (under every spelling of the source: as
    given, absolute, resolved); code paths (`<source>/dashboard/app.js`), folders outside the
    clone and look-alike names (`<source>-other/...`) stay as they are. Each file is replaced
    atomically and the pass is idempotent, so an interrupted one is simply run again.
    Returns the number of files rewritten."""
    names = [i for i in items if i not in DROPPED]
    if not names:
        return 0
    alt = "|".join(re.escape(n) for n in sorted(names, key=len, reverse=True))
    new = str(data)
    pairs = set()
    for sp in _spellings(Path(source)):
        pairs.add((sp, new))
        pairs.add((json.dumps(sp)[1:-1], json.dumps(new)[1:-1]))  # ascii-escaped, as json.dump writes
    pats = [(re.compile(r"(?<![\w.~-])" + re.escape(o) + r"/(" + alt + r")(?![\w-]|\.\w)"), n)
            for o, n in sorted(pairs)]
    count = 0
    for name in names:
        root = Path(data) / name
        if root.is_symlink():
            continue
        files = [f for f in root.rglob("*") if f.is_file() or f.is_symlink()] if root.is_dir() else [root]
        for f in files:
            if f.name.startswith(_REWRITE_PREFIX):  # temp left by an interrupted pass
                f.unlink(missing_ok=True)
                continue
            if f.suffix not in _TEXT_SUFFIXES or f.is_symlink():
                continue
            try:
                text = f.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            out = text
            for pat, n in pats:
                out = pat.sub(lambda m, n=n: f"{n}/{m.group(1)}", out)
            if out != text:
                _replace_text(f, out)
                count += 1
    return count


def _replace_text(f: Path, text: str) -> None:
    mode = stat.S_IMODE(f.stat().st_mode)
    fd, tmp = tempfile.mkstemp(dir=f.parent, prefix=_REWRITE_PREFIX)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as h:
            h.write(text)
            h.flush()
            os.fsync(h.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, f)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _new_marker(data: Path) -> None:
    migrations.write_marker(data, {"schema": 1, "created": datetime.datetime.now(datetime.timezone.utc)
                                   .isoformat(timespec="seconds")})


def read_journal(data: Path) -> "dict | None":
    try:
        rec = json.loads((Path(data) / JOURNAL).read_text())
        return rec if isinstance(rec, dict) else {"_bad": True}
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        return {"_bad": True}


def _write_journal(data: Path, rec: dict) -> None:
    _atomic_write(Path(data) / JOURNAL, json.dumps(rec))


def _same_folder(a: str, b: Path) -> bool:
    try:
        return Path(a).resolve() == Path(b).resolve()
    except (OSError, RuntimeError):
        return False


def _finish(source: Path, data: Path, journal: dict) -> MoveResult:
    """Do (or finish) the moving, rewriting and marker. Safe to run again after any crash."""
    res = MoveResult(backup=Path(journal["backup"]) if journal.get("backup") else None)
    items = list(journal["items"])
    if journal.get("stage") == "moving":
        for rel in items:
            _move_item(source / rel, data / rel)
        for rel in journal.get("dropped", []):
            (source / rel).unlink(missing_ok=True)
        journal["stage"] = "rewriting"
        _write_journal(data, journal)
    rewrite_paths(data, source, items)
    _new_marker(data)
    (data / JOURNAL).unlink(missing_ok=True)
    res.moved = items
    return res


def move_legacy(source: Path, data: Path = None, *, replace: bool = False) -> MoveResult:
    source = Path(source)
    data = Path(paths.DATA if data is None else data)
    data.mkdir(mode=0o700, parents=True, exist_ok=True)
    res = MoveResult()

    journal = read_journal(data)
    if journal is not None:
        if replace:
            res.refused = (f"An earlier move into {data} did not finish. Start Leo Bot, or run "
                           f"`leobot doctor`, to finish it first; --replace is refused until then.")
        elif journal.get("_bad") or not _same_folder(journal.get("source", ""), source):
            res.refused = (f"{data} holds an unfinished move from another folder "
                           f"({journal.get('source', 'unknown')}). Nothing was changed. "
                           f"Run `leobot doctor` to see the choices.")
        else:
            return _finish(source, data, journal)
        return res

    found = legacy_items(source)
    real = [i for i in found if i not in DROPPED]
    for rel in real:
        if (source / rel).is_symlink():
            res.refused = (f"{rel} in {source} is a symbolic link, and Leo Bot will not follow it. "
                           f"Replace it with the real folder (or move it into {data} by hand), then start again.")
            return res
    if not real:  # nothing, or only a stale token or log: drop those, never refuse over them
        for rel in found:
            (source / rel).unlink(missing_ok=True)
        if not (data / MARKER).exists():
            _new_marker(data)
        return res
    if not data_is_empty(data) and not replace:
        res.refused = (f"Leo Bot found data in two places: {source} (old, inside the app folder) and "
                       f"{data} (the home folder). Nothing was moved. Run `leobot doctor` to see the "
                       f"choices: keep the home folder and archive the old copy, or "
                       f"`leobot move-data --from {source} --replace`, which backs up {data} first.")
        return res
    problem = _space_problem(source, data, real, replace)
    if problem:
        res.refused = problem
        return res

    stamp = _stamp()
    made = []
    try:
        legacy_tar = _tar(real, source, paths.BACKUPS / f"legacy-{stamp}.tar.gz")
        made.append(legacy_tar)
        if not data_is_empty(data):  # replace: keep a copy, then clear
            names = sorted(p.name for p in data.iterdir())
            made.append(_tar(names, data, paths.BACKUPS / f"replaced-{stamp}.tar.gz"))
    except BaseException:
        for t in made:
            t.unlink(missing_ok=True)
        raise
    for p in sorted(data.iterdir()):  # empty (or only junk and the marker) by now
        _remove(p)
    journal = {"source": str(source), "items": real, "dropped": [i for i in found if i in DROPPED],
               "stamp": stamp, "backup": str(legacy_tar), "stage": "moving"}
    _write_journal(data, journal)
    return _finish(source, data, journal)


def prepare() -> None:
    """Run at server start, under the single-server lock."""
    paths.ensure_dirs()
    if read_journal(paths.DATA) is not None or legacy_items(paths.CODE):
        r = move_legacy(paths.CODE)
        if r.refused:
            sys.exit(r.refused)
    elif not (paths.DATA / MARKER).exists():
        _new_marker(paths.DATA)
    migrations.run(paths.DATA)
