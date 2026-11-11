"""memory.py — each bot's memory: one markdown list per bot per workspace and scope.

Files stay the source of truth, at the paths `core/bots.py` documents:

  team/memory/<bot>.md                  HQ
  projects/<slug>/team/<bot>.md         that workspace's private memory
  projects/<slug>/team/shared/<bot>.md  its team memory

One entry per line: `- YYYY-MM-DD [type] text`, type one of fact, preference,
decision. A line without a type (older files, hand edits) reads as a fact. Every
write to a file holds `<file>.lock`, so a background tidy-up never loses an entry
that landed while it ran.

Recall searches a derived FTS5 index in its own file, `database/data/memory.db`
(never `os.db`, which every reindex swaps out). Each indexed file keeps its stat,
and recall re-reads a file whose stat changed, so a hand edit is never stale.
The index is disposable: missing or corrupt, it is rebuilt; without FTS5, recall
scans the file.
"""
import contextlib
import re
import sqlite3
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
RECALL_CHARS = 800
RECALL_MAX = 20
PINNED_IN_CARD = 10
HITS_IN_CARD = 5
INDEX_RELPATH = Path("database") / "data" / "memory.db"

_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_LINE_RE = re.compile(r"^- (\d{4}-\d{2}-\d{2}) (?:\[(fact|preference|decision)\] )?(.*)$")
FULL = "memory is full of decisions and preferences; forget one first"


def _probe_fts5():
    try:
        conn = sqlite3.connect(":memory:")
        try:
            conn.execute("CREATE VIRTUAL TABLE probe USING fts5(x)")
        finally:
            conn.close()
        return True
    except sqlite3.Error:
        return False


HAS_FTS5 = _probe_fts5()


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
            drop = set([i for i, e in enumerate(kept) if e.type == "fact"][:over])  # oldest facts
            if len(drop) < over:
                raise ValueError(FULL)
            kept = [e for i, e in enumerate(kept) if i not in drop]
        _write(path, kept + [new])
        sync_file(path, root, force=True)
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
                sync_file(path, root, force=True)
                return True
    return False


# --------------------------------------------------------------------------- #
# Recall: a derived FTS5 index, one row per entry.
# --------------------------------------------------------------------------- #
_SCHEMA = (
    "CREATE VIRTUAL TABLE IF NOT EXISTS entries USING fts5(path UNINDEXED, workspace UNINDEXED,"
    " scope UNINDEXED, bot UNINDEXED, date UNINDEXED, type UNINDEXED, line UNINDEXED, text,"
    " tokenize='unicode61 remove_diacritics 2')",
    "CREATE TABLE IF NOT EXISTS files(path TEXT PRIMARY KEY, mtime_ns INTEGER, size INTEGER)",
)


def _db_path(root):
    return Path(root) / INDEX_RELPATH


def _drop_index(root):
    db = _db_path(root)
    for f in (db, db.with_name(db.name + "-journal"), db.with_name(db.name + "-wal"),
              db.with_name(db.name + "-shm")):
        f.unlink(missing_ok=True)


def _connect(root):
    db = _db_path(root)
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db, timeout=5)
    try:
        for stmt in _SCHEMA:
            conn.execute(stmt)
        conn.execute("SELECT count(*) FROM files").fetchone()
    except BaseException:
        conn.close()
        raise
    return conn


def _with_index(root, fn):
    """Run fn(conn) on the index; a corrupt file is deleted and the call tried once more.
    An OperationalError (busy, locked, no FTS5) is raised as is: the file may be fine."""
    for attempt in (1, 2):
        try:
            conn = _connect(root)
            try:
                with conn:
                    return fn(conn)
            finally:
                conn.close()
        except sqlite3.OperationalError:
            raise
        except sqlite3.DatabaseError:
            if attempt == 2:
                raise
            _drop_index(root)


def _where(path, root):
    """(workspace, scope, bot) of a memory file, from its place under root."""
    parts = Path(path).relative_to(root).parts
    bot = Path(path).stem
    if parts[0] == "team":
        return "hq", "private", bot
    return parts[1], ("shared" if len(parts) == 5 else "private"), bot


def _sync(conn, path, root, force):
    rel = str(path.relative_to(root))
    try:
        st = path.stat()
        sig = (st.st_mtime_ns, st.st_size)
    except OSError:
        sig = None
    row = conn.execute("SELECT mtime_ns, size FROM files WHERE path = ?", (rel,)).fetchone()
    if not force and (tuple(row) if row else None) == sig:
        return
    conn.execute("DELETE FROM entries WHERE path = ?", (rel,))
    conn.execute("DELETE FROM files WHERE path = ?", (rel,))
    if sig is None:
        return
    ws, scope, bot = _where(path, root)
    conn.executemany(
        "INSERT INTO entries (path, workspace, scope, bot, date, type, line, text)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [(rel, ws, scope, bot, e.date, e.type, e.line, e.text) for e in _read(path)])
    conn.execute("INSERT INTO files (path, mtime_ns, size) VALUES (?, ?, ?)", (rel, *sig))


def sync_file(path, root=ROOT, force=False):
    """Replace one file's rows when its stat changed (always with `force`, as writes do).
    Never raises: the index is derived, and a skipped sync only costs a later re-read
    (the file's stat no longer matches its row)."""
    if not HAS_FTS5:
        return
    root = Path(root)
    try:
        _with_index(root, lambda conn: _sync(conn, Path(path), root, force))
    except (sqlite3.Error, OSError):
        pass


def _memory_files(root):
    root = Path(root)
    return (sorted(root.glob("team/memory/*.md")) + sorted(root.glob("projects/*/team/*.md"))
            + sorted(root.glob("projects/*/team/shared/*.md")))


def rebuild_index(root=ROOT):
    """Rebuild the whole index from the files (index.py). Returns rows written."""
    if not HAS_FTS5:
        return 0
    root = Path(root)

    def build(conn):
        conn.execute("DELETE FROM entries")
        conn.execute("DELETE FROM files")
        for f in _memory_files(root):
            _sync(conn, f, root, force=True)
        return conn.execute("SELECT count(*) FROM entries").fetchone()[0]

    return _with_index(root, build)


def _words(query):
    return [w for w in re.findall(r"\w+", (query or "").lower()) if len(w) >= 2][:20]


def _fts(path, root, words, limit):
    rel = str(path.relative_to(root))
    match = " OR ".join(f'"{w}"' for w in words)

    def search(conn):
        _sync(conn, path, root, force=False)
        return [r[0] for r in conn.execute(
            "SELECT line FROM entries WHERE entries MATCH ? AND path = ?"
            " ORDER BY bm25(entries), rowid DESC LIMIT ?", (match, rel, limit))]

    return _with_index(root, search)


def _scan(path, words, limit):
    scored = []
    for i, e in enumerate(_read(path)):
        text = e.text.lower()
        score = sum(w in text for w in words)
        if score:
            scored.append((-score, -i, e.line))
    return [line for *_, line in sorted(scored)[:limit]]


def _fit(lines, budget=RECALL_CHARS):
    out, used = [], 0
    for line in lines:
        if used + len(line) > budget:
            break
        out.append(line)
        used += len(line)
    return out


def recall(bot_id, query, project=None, root=ROOT, shared=False, limit=8):
    """Entry lines from one memory file matching `query`, best first, RECALL_CHARS at most.
    Never raises for a bad query, a bad scope or a broken index."""
    words = _words(query)
    try:
        limit = max(1, min(RECALL_MAX, int(limit)))
        path = memory_path(bot_id, project, root, shared)
    except (TypeError, ValueError):
        return []
    if not words:
        return []
    root = Path(root)
    lines = None
    if HAS_FTS5:
        try:
            lines = _fts(path, root, words, limit)
        except (sqlite3.Error, OSError):
            lines = None
    if lines is None:
        lines = _scan(path, words, limit)
    return _fit(lines)


def card_block(bot_id, project=None, root=ROOT, shared=False, query=""):
    """The memory a bot card carries: pinned entries (decisions, preferences) newest
    first, then the top recall hits for the message that opened the card. "" if none."""
    pinned = [e.line for e in reversed(entries(bot_id, project, root, shared))
              if e.type in PINNED][:PINNED_IN_CARD]
    seen = set(pinned)
    hits = [ln for ln in recall(bot_id, query, project, root, shared,
                                limit=HITS_IN_CARD + PINNED_IN_CARD) if ln not in seen]
    return "\n".join(pinned + hits[:HITS_IN_CARD])
