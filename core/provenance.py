"""provenance.py — saved content that came from outside content, until the owner trusts it.

A conversation that read outside content (mail, pages, app data, a guest's words: see
dashboard/taint.py) can still save things: a memo, a memory entry, a doc. Whatever it saves may
carry the outside words, and a later, clean conversation reads it as the owner's own. So the
label travels with the data (information-flow control, as in CaMeL and Microsoft's FIDES):

  - every work file a tainted request created or changed is listed here (path, workspace, when);
  - while a workspace holds a listed file the owner has not trusted, every conversation there
    counts as holding outside content: its bots do not act on their own (one Approve all);
  - the owner reads the whole saved file in Approvals and trusts it (or deletes it). Nothing
    else clears a label: no bot, no job, no time limit.

Work files: projects/<slug>/** (docs, memos, posts, briefs, per-project memory) and
team/memory/** (HQ bot memory). HQ conversations see every workspace, so any listed file counts.
"""
import json
import os
import re
import threading
import time
from pathlib import Path

from core import bots

LEDGER = Path("team") / "outside_content.json"
_LOCK = threading.Lock()
_SKIP_SUFFIXES = (".db", ".db-wal", ".db-shm", ".db-journal", ".sqlite", ".lock", ".tmp")
PREVIEW_CHARS = 20000


def _work_roots(root, ws):
    root = Path(root)
    if ws == bots.HQ:
        return [root / "projects", root / "team" / "memory"]
    return [root / "projects" / ws]


def scan(root, ws):
    """{data-relative path: (mtime_ns, size)} for every work file a run in `ws` could change."""
    root = Path(root)
    out = {}
    for base in _work_roots(root, ws):
        if not base.is_dir():
            continue
        for f in base.rglob("*"):
            try:
                if f.is_symlink() or not f.is_file() or f.name.startswith(".") or f.name.endswith(_SKIP_SUFFIXES):
                    continue
                st = f.stat()
            except OSError:
                continue
            out[f.relative_to(root).as_posix()] = (st.st_mtime_ns, st.st_size)
    return out


def _ws_of(rel):
    parts = rel.split("/")
    return parts[1] if parts[0] == "projects" and len(parts) > 2 else bots.HQ


def _load(root):
    try:
        data = json.loads((Path(root) / LEDGER).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    items = data.get("items") if isinstance(data, dict) else None
    return {k: v for k, v in (items or {}).items() if isinstance(k, str) and isinstance(v, dict)}


def _save(root, items):
    bots._atomic_write(Path(root) / LEDGER, json.dumps({"items": items}, indent=2) + "\n")


def mark(root, before, after, chat=None):
    """List every work file that is new or changed between two scans (a tainted request's)."""
    changed = [rel for rel, sig in after.items() if before.get(rel) != sig]
    if not changed:
        return []
    with _LOCK:
        items = _load(root)
        now = time.time()
        for rel in changed:
            items[rel] = {"ws": _ws_of(rel), "at": now, **({"chat": chat} if isinstance(chat, str) else {})}
        _save(root, items)
    return changed


def pending(root, ws):
    """Listed files the owner has not trusted that conversations in `ws` can see, newest first.
    A file deleted since is dropped from the list."""
    root = Path(root)
    with _LOCK:
        items = _load(root)
        gone = [rel for rel in items if not (root / rel).is_file()]
        if gone:
            for rel in gone:
                items.pop(rel, None)
            _save(root, items)
    rows = [{"path": rel, **meta} for rel, meta in items.items()
            if ws == bots.HQ or meta.get("ws") == ws]
    return sorted(rows, key=lambda r: r.get("at") or 0, reverse=True)


_REL_RE = re.compile(r"^(?:projects|team)/[A-Za-z0-9._ -]+(?:/[A-Za-z0-9._ -]+)*$")


def _listed_path(root, items, rel):
    """(ledger key, absolute path) for a listed file: the key comes from the ledger, never the
    caller, and must be a plain path inside the data folder."""
    key = next((k for k in items if k == rel), None)
    if key is None or not _REL_RE.fullmatch(key) or ".." in key.split("/"):
        raise LookupError("not a listed file")
    base = os.path.realpath(str(root))
    full = os.path.realpath(os.path.join(base, key))
    if not full.startswith(base + os.sep):
        raise LookupError("not a listed file")
    return key, Path(full)


def _sig(path):
    st = path.stat()
    return f"{st.st_mtime_ns}:{st.st_size}"


def preview(root, rel):
    """{"text", "sig", "cut"}: the whole saved text (capped), so the owner reads what a bot will
    read, and the version it is (trust names it: a later change is not trusted with it)."""
    _, path = _listed_path(root, _load(root), rel)
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
        return {"text": text[:PREVIEW_CHARS], "cut": len(text) > PREVIEW_CHARS, "sig": _sig(path)}
    except OSError as e:
        raise LookupError(str(e)) from None


def trust(root, rel, sig):
    """The owner read this version (`sig` from preview) and trusts it: its label goes. A file
    changed since, or one too long to show whole, keeps its label."""
    with _LOCK:
        items = _load(root)
        key, path = _listed_path(root, items, rel)
        try:
            now = _sig(path)
            whole = path.stat().st_size <= PREVIEW_CHARS * 4  # bytes; the text check is below
            text = path.read_text(encoding="utf-8", errors="replace") if whole else ""
        except OSError as e:
            raise LookupError(str(e)) from None
        if now != sig:
            raise ValueError("it changed since you opened it: open it again")
        if not whole or len(text) > PREVIEW_CHARS:
            raise ValueError("too long to read here whole: open the file, trim or delete it")
        items.pop(key)
        _save(root, items)
