"""approvals.py — proposals waiting on the CEO (Slack-style approvals inbox).

A bot that proposes a write and waits for the user's OK ends its reply with one
line `APPROVAL: <what it will save, where>`. The server strips that line,
records a pending approval for the thread, and the UI shows Approve / Edit / No
on the reply plus an inbox (HQ lists every workspace, a company only its own).
Any new user message in that thread resolves it.

A second kind, `app_action`, is a connected-app call a bot made that is not a
read (send, post, create...). The relay stores it instead of running it, with a
preview the server built from the real arguments; only an Approve click runs
it, exactly once (pending -> running -> done/failed). Chat rules never touch
these rows: a new message does not resolve them and a newer proposal does not
supersede them. Rows with no `kind` are chat approvals.

A fourth kind, `contradiction`, says new evidence breaks a memo's assumption; Approve marks
the assumption contradicted, exactly once.

A third kind, `job`, is a scheduled-job proposal (create, change or delete), built by
the server from what a bot proposed (dashboard/job_cards.py). It is held like an app
action: only its own card answers it, and Approve applies it exactly once.

Stored in gitignored team/approvals.json (spans workspaces, so company bots
are denied Read on team/).
"""
import json
import re
import threading
import time
import uuid
from pathlib import Path

from core import app_preview, bots, integrations

MARKER_RE = re.compile(r"^[ \t>*_-]*APPROVAL:[*_ \t]*(.+?)[ \t*_]*$", re.M)
SUMMARY_MAX = 200
KEEP = 200
STATUSES = {"pending", "approved", "rejected", "answered", "superseded",
            "running", "done", "failed", "expired"}
APP = "app_action"
JOB = "job"
CONTRA = "contradiction"
HELD = (APP, JOB, CONTRA)  # answered only through their own card
APP_TTL = 7 * 86400
RUNNING_STALE = 600
STALE_NOTE = "May or may not have gone out. Check the app."
JOB_STALE_NOTE = "May or may not be saved. Check Team, Scheduled jobs."
# Every read-modify-write holds this: the relay thread writes here too.
LOCK = threading.RLock()


def extract(reply):
    """(reply without APPROVAL lines, last summary or None)."""
    found = MARKER_RE.findall(reply or "")
    if not found:
        return reply, None
    clean = MARKER_RE.sub("", reply).rstrip()
    return clean, found[-1].strip()[:SUMMARY_MAX] or None


def _path(root):
    return Path(root) / "team" / "approvals.json"


def load(root=bots.ROOT):
    try:
        data = json.loads(_path(root).read_text())
    except (OSError, ValueError):
        return []
    rows = data.get("approvals") if isinstance(data, dict) else None
    return [r for r in rows or [] if isinstance(r, dict) and r.get("id")]


def _is_held(r):
    return r.get("kind") in HELD


def _protected(r):
    return _is_held(r) and r.get("status") in ("pending", "running")


def _save(root, rows):
    """Keep every open app action plus the newest KEEP of the rest."""
    rest = {id(r) for r in [r for r in rows if not _protected(r)][-KEEP:]}
    rows = [r for r in rows if _protected(r) or id(r) in rest]
    bots._atomic_write(_path(root), json.dumps({"approvals": rows}, indent=2) + "\n")


def add(root=bots.ROOT, *, ws, room, chat, bot, summary):
    """Record a pending approval; an older pending one in the same thread is superseded."""
    with LOCK:
        rows = load(root)
        for r in rows:
            if r.get("chat") == chat and r.get("status") == "pending" and not _is_held(r):
                r["status"] = "superseded"
        rec = {"id": uuid.uuid4().hex[:12], "ws": ws, "room": room, "chat": chat, "bot": bot,
               "summary": summary, "created": time.time(), "status": "pending"}
        rows.append(rec)
        _save(root, rows)
        return rec


def resolve_chat(chat, status, root=bots.ROOT):
    """Close the thread's pending approval (the user answered it)."""
    if status not in STATUSES or not chat:
        return 0
    with LOCK:
        rows = load(root)
        n = 0
        for r in rows:
            if r.get("chat") == chat and r.get("status") == "pending" and not _is_held(r):
                r["status"], r["resolved"] = status, time.time()
                n += 1
        if n:
            _save(root, rows)
        return n


def forget_workspace(ws, root=bots.ROOT):
    """A deleted workspace: its approvals (and held app actions) go, so a project made later
    with the same slug starts with an empty inbox. A running app action is left to finish."""
    if not isinstance(ws, str) or not ws or ws == bots.HQ:
        return 0
    with LOCK:
        rows = load(root)
        keep = [r for r in rows if r.get("ws") != ws or r.get("status") == "running"]
        if len(keep) != len(rows):
            _save(root, keep)
        return len(rows) - len(keep)


def move_room(old, new, root=bots.ROOT):
    """A renamed channel keeps its approvals: repoint every row at `new`."""
    with LOCK:
        rows = load(root)
        n = 0
        for r in rows:
            if r.get("room") == old:
                r["room"] = new
                n += 1
        if n:
            _save(root, rows)
        return n


def pending(root=bots.ROOT, ws=None):
    """Open approvals, newest first; `ws` other than HQ narrows to one workspace."""
    with LOCK:
        rows = load(root)
        if _housekeep(rows):
            _save(root, rows)
    rows = [r for r in rows if r.get("status") == "pending"
            and (not ws or ws == bots.HQ or r.get("ws") == ws)]
    return sorted(rows, key=lambda r: r.get("created") or 0, reverse=True)


def _housekeep(rows):
    """Expire week-old held rows; a run cut off by a restart is failed, never re-run."""
    now, changed = time.time(), False
    for r in rows:
        if not _is_held(r):
            continue
        if r.get("status") == "pending" and now - (r.get("created") or 0) > APP_TTL:
            r["status"], r["resolved"], changed = "expired", now, True
        elif r.get("status") == "running" and now - (r.get("started") or 0) > RUNNING_STALE:
            r.update(status="failed", result=JOB_STALE_NOTE if r.get("kind") in (JOB, CONTRA) else STALE_NOTE,
                     resolved=now)
            changed = True
    return changed


def add_app_action(root=bots.ROOT, *, ws, room, chat, bot, person, toolkit, slug, arguments, account=None):
    """Hold one app tool call for an Approve click, on the account it will run as."""
    label = (integrations.known(root).get(account) or {}).get("label") if account else None
    rec = {"id": uuid.uuid4().hex[:12], "kind": APP, "ws": ws, "room": room, "chat": chat,
           "bot": bot, "person": person, "status": "pending", "created": time.time(),
           "action": {"toolkit": toolkit, "slug": slug, "arguments": arguments,
                      "account": account, "account_label": label or account},
           "preview": app_preview.build(slug, arguments, toolkit, account=label or account)}
    with LOCK:
        rows = load(root)
        rows.append(rec)
        _save(root, rows)
    return rec


def _job_target(job):
    """What a job card acts on: the job for a change or deletion, the name for a new one."""
    job = job or {}
    if job.get("op") == "create":
        f = job.get("fields") or {}
        return ("create", f.get("workspace"), " ".join(str(f.get("name") or "").lower().split()))
    return (job.get("op"), job.get("job_id"))


def add_job(root=bots.ROOT, *, ws, room, chat, bot, job, summary, preview):
    """Hold one job proposal ({"op", "job_id", "fields"}) for an Approve click. A pending card
    in the same thread for the same target (that job, or a new job of that name) is
    superseded: a bot that proposes again never leaves two cards that would both apply."""
    rec = {"id": uuid.uuid4().hex[:12], "kind": JOB, "ws": ws, "room": room, "chat": chat,
           "bot": bot, "status": "pending", "created": time.time(), "job": job,
           "summary": str(summary)[:SUMMARY_MAX], "preview": preview}
    with LOCK:
        rows = load(root)
        for r in rows:
            if (r.get("kind") == JOB and r.get("status") == "pending" and r.get("chat") == chat
                    and _job_target(r.get("job")) == _job_target(job)):
                r.update(status="superseded", resolved=rec["created"])
        rows.append(rec)
        _save(root, rows)
    return rec


def add_contradiction(root=bots.ROOT, *, ws, room, chat, bot, contra, summary, preview):
    """Hold one finding ({"assumption", "evidence", "reason"}) for an Approve click. A pending
    card for the same (assumption, evidence) is not repeated."""
    key = (contra.get("assumption"), contra.get("evidence"))
    with LOCK:
        rows = load(root)
        for r in rows:
            c = r.get("contra") or {}
            if r.get("kind") == CONTRA and r.get("status") == "pending" \
                    and (c.get("assumption"), c.get("evidence")) == key:
                return r
        rec = {"id": uuid.uuid4().hex[:12], "kind": CONTRA, "ws": ws, "room": room, "chat": chat,
               "bot": bot, "status": "pending", "created": time.time(), "contra": contra,
               "summary": str(summary)[:SUMMARY_MAX], "preview": preview}
        rows.append(rec)
        _save(root, rows)
    return rec


def get(aid, root=bots.ROOT):
    return next((r for r in load(root) if r.get("id") == aid), None)


def _mutate_held(aid, root, fn, kinds=HELD):
    with LOCK:
        rows = load(root)
        _housekeep(rows)
        rec = next((r for r in rows if r.get("id") == aid and r.get("kind") in kinds), None)
        if rec is None:
            raise ValueError("no such app action" if kinds == (APP,) else "no such card")
        fn(rec)
        _save(root, rows)
        return dict(rec)


def _need(rec, status):
    if rec.get("status") != status:
        raise ValueError(f"not {status}")


def claim(aid, root=bots.ROOT):
    """pending -> running, once: a double click or a second tab cannot run it twice."""
    def fn(r):
        _need(r, "pending")
        r.update(status="running", started=time.time())
    return _mutate_held(aid, root, fn)


def finish(aid, status, result, root=bots.ROOT):
    if status not in ("done", "failed"):
        raise ValueError("bad status")
    def fn(r):
        _need(r, "running")
        r.update(status=status, result=result, resolved=time.time())
    return _mutate_held(aid, root, fn)


def reject(aid, root=bots.ROOT):
    def fn(r):
        _need(r, "pending")
        r.update(status="rejected", resolved=time.time())
    return _mutate_held(aid, root, fn)


def edit_arguments(aid, arguments, root=bots.ROOT):
    if not isinstance(arguments, dict):
        raise ValueError("arguments must be an object")
    def fn(r):
        _need(r, "pending")
        r["action"]["arguments"] = arguments
        r["preview"] = app_preview.build(r["action"]["slug"], arguments, r["action"].get("toolkit"),
                                         account=r["action"].get("account_label"))
    return _mutate_held(aid, root, fn, kinds=(APP,))


def edit_job(aid, job, summary, preview, root=bots.ROOT):
    """Replace a pending job card's proposal (already validated by the caller)."""
    def fn(r):
        _need(r, "pending")
        r.update(job=job, summary=str(summary)[:SUMMARY_MAX], preview=preview)
    return _mutate_held(aid, root, fn, kinds=(JOB,))
