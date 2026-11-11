"""memory.py — each bot's memory: one markdown list per bot per workspace and scope.

Files stay the source of truth, at the paths `core/bots.py` documents:

  team/memory/<bot>.md                  HQ
  projects/<slug>/team/<bot>.md         that workspace's private memory
  projects/<slug>/team/shared/<bot>.md  its team memory

One entry per line: `- YYYY-MM-DD [type] text`, type one of fact, preference,
decision. A line without a type (older files, hand edits) reads as a fact. Every
write to a file holds `<file>.lock`, so a background tidy-up never loses an entry
that landed while it ran.
"""
import contextlib
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX: writes go unlocked
    fcntl = None

from core import paths

ROOT = paths.DATA
TYPES = ("fact", "preference", "decision")
PINNED = frozenset({"preference", "decision"})
MAX_ENTRIES = 400
CONSOLIDATE_AT = 300
LINE_MAX = 300
TAIL_LINES = 20

_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_LINE_RE = re.compile(r"^- (\d{4}-\d{2}-\d{2}) (?:\[(fact|preference|decision)\] )?(.*)$")
FULL = "memory is full of decisions and preferences; forget one first"


def _bots():
    # core.bots re-exports this module's names at its end, so import it lazily:
    # either module can then be imported first.
    from core import bots
    return bots


@dataclass(frozen=True)
class Entry:
    date: str
    type: str
    text: str
    line: str  # the exact file line


def parse(line):
    """One file line as an Entry; None for a blank line. Anything odd is a fact."""
    if not (line or "").strip():
        return None
    m = _LINE_RE.match(line)
    if m:
        return Entry(m.group(1), m.group(2) or "fact", m.group(3), line)
    s = line.strip()
    return Entry("", "fact", s[2:] if s.startswith("- ") else s, line)


def memory_path(bot_id, project=None, root=ROOT, shared=False):
    """`shared`: the team memory of a project workspace. HQ has none: nobody joins HQ."""
    if not _SLUG_RE.fullmatch(bot_id or ""):
        raise ValueError("invalid bot id")
    root = Path(root)
    if project:
        if not _SLUG_RE.fullmatch(project) or not (root / "projects" / project).is_dir():
            raise ValueError(f"unknown project: {project}")
        team_dir = root / "projects" / project / "team"
        return (team_dir / "shared" if shared else team_dir) / f"{bot_id}.md"
    if shared:
        raise ValueError("HQ has no team memory")
    return root / "team" / "memory" / f"{bot_id}.md"


def _read(path):
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    return [e for e in map(parse, lines) if e]


def _write(path, entries):
    _bots()._atomic_write(Path(path), "".join(e.line + "\n" for e in entries))


@contextlib.contextmanager
def locked(path):
    """Hold `<path>.lock` for one write to a memory file (threads and processes)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(f"{path}.lock", "a") as fh:
        if fcntl:
            fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            if fcntl:
                fcntl.flock(fh, fcntl.LOCK_UN)


def entries(bot_id, project=None, root=ROOT, shared=False):
    """Every entry in file order; [] when the file is missing or the scope invalid."""
    try:
        return _read(memory_path(bot_id, project, root, shared))
    except ValueError:
        return []


def read_memory(bot_id, project=None, root=ROOT, tail=TAIL_LINES, shared=False):
    lines = [e.line for e in entries(bot_id, project, root, shared)]
    return "\n".join(lines[-tail:]) if tail else "\n".join(lines)


def remember(bot_id, text, project=None, root=ROOT, today=None, shared=False, type="fact"):
    """Append one dated, typed entry. Past MAX_ENTRIES the oldest fact goes; a
    decision or preference is never dropped, so a file full of them refuses."""
    if bot_id not in _bots().load_defs():
        raise ValueError(f"unknown bot: {bot_id}")
    if type not in TYPES:
        raise ValueError(f"unknown memory type: {type}; use fact, preference or decision")
    clean = " ".join((text or "").split())
    if not clean:
        raise ValueError("empty memory")
    if len(clean) > LINE_MAX:
        raise ValueError(f"memory line over {LINE_MAX} chars; keep it to one fact")
    path = memory_path(bot_id, project, root, shared)
    new = parse(f"- {(today or date.today()).isoformat()} [{type}] {clean}")
    with locked(path):
        kept = _read(path)
        over = len(kept) + 1 - MAX_ENTRIES
        if over > 0:
            drop = {i for i, e in enumerate(kept) if e.type == "fact"}
            drop = set(sorted(drop)[:over])
            if len(drop) < over:
                raise ValueError(FULL)
            kept = [e for i, e in enumerate(kept) if i not in drop]
        _write(path, kept + [new])
    return str(path.relative_to(Path(root)))


def forget(bot_id, line, project=None, root=ROOT, shared=False):
    """Delete one entry (exact line, first match). True if removed."""
    path = memory_path(bot_id, project, root, shared)
    target = (line or "").strip()
    with locked(path):
        kept = _read(path)
        for i, e in enumerate(kept):
            if e.line.strip() == target:
                del kept[i]
                _write(path, kept)
                return True
    return False
