"""own_checks.py: a loop's check on Leo Bot's own data.

A job's `check` may be {"own": <source>, "id": <ref or None>} instead of a connected-app read.
The server hashes that data before the model runs (dashboard/server.py, _job_check); the run goes
ahead only when the hash changed since the last run, so a quiet run costs nothing.

Sources, always inside the job's own workspace:
  approvals    the workspace's pending cards (HQ: every workspace's)
  list         one canvas list (`id`: ls<N>): its rows and version
  memos        the project's memos, as the index lists them (not in HQ)
  experiments  the project's experiments, as the index lists them (not in HQ)
"""
import hashlib
import json

from core import approvals, bots, canvas_store, jobs

SOURCES = jobs.OWN_SOURCES
LABELS = {"approvals": "cards waiting for you", "list": "list {id}", "memos": "memos", "experiments": "experiments"}


def label(check):
    """How a card or prompt names what the check watches."""
    return LABELS.get(check.get("own"), "?").format(id=check.get("id") or "")


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def digest(root, ws, source, ref=None):
    """A hash of what `source` holds in `ws` now; ValueError when it cannot be read from there."""
    if source not in SOURCES:
        raise ValueError(f"own check: one of {', '.join(SOURCES)}")
    if source == "approvals":
        return _hash(sorted(str(r.get("id")) for r in approvals.pending(root, ws)))
    if source == "list":
        if not canvas_store.valid("list", ref):
            raise ValueError("own check: a list id like ls3")
        try:
            doc = canvas_store.read(root, ws, ref)
        except (LookupError, ValueError):
            raise ValueError(f"own check: no list {ref} in this workspace") from None
        return _hash([doc.get("version"), doc.get("rows")])
    if ws == bots.HQ:
        raise ValueError(f"own check: HQ has no {source}; watch them from a project workspace")
    from dashboard import db  # dashboard reads the index; core stays importable without it
    project = db.project(ws)
    if not project:
        raise ValueError(f"own check: no project {ws}")
    return _hash(project.get(source) or [])
