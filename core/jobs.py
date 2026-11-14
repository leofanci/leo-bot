"""jobs.py: scheduled jobs, stored in `team/jobs.json` in the data folder.

A job is one bot's recurring work: a skill plus a message, run on a schedule by the
server (dashboard/job_runner.py) in its mode, with its own daily cap. The file spans
workspaces like team/approvals.json, so company bots cannot read it. A bot schedules work
itself from the owner's private thread (`osctl propose-job`, applied when its turn ends,
with Undo and Pause on the receipt); the owner also changes jobs on the Team screen. A
job's own run never schedules more.

Record: id, name, workspace, bot, skill, message, schedule, cap (dollars per rolling
24 hours, above 0), mode, enabled, created, last_run, last_status, last_chat.

Modes: `approval` (reads and drafts; every save waits for Approve), `auto` (creates and
updates in Leo Bot by itself; deletes, memory and app writes still wait), `report` (reads
and reports, saves nothing). A missing or unknown mode reads as `approval`.

The file also holds a proposal outbox: `osctl propose-job` drops a bot's proposal there,
keyed by its thread, and the server applies it when that turn ends (dashboard/job_cards.py),
only from the owner's private thread. Nothing in the outbox ever runs by itself.

Schedule text, local time:
  every <N>m|h|d       an interval, at least MIN_EVERY
  <days> HH:MM         days = "daily" or a comma list of mon..sun
  once YYYY-MM-DD HH:MM  one run, then the job turns itself off
A run that was missed (server off) runs once on wake, never once per missed slot:
the next one is counted from the last run.
"""
import json
import math
import re
import time
import uuid
from pathlib import Path

from core import bots, memory

RELPATH = Path("team") / "jobs.json"
LIVE_RELPATH = Path("team") / "job_live.json"  # the running job's spend so far (one at a time)
LIVE_MAX_AGE = 2 * 3600  # older than any job turn: a crash's leftover, never counted
MIN_EVERY = 300
MAX_JOBS = 50  # across workspaces: a runaway bot cannot pile up recurring spend
NAME_MAX = 60
MESSAGE_MAX = 2000
STATUSES = {"running", "done", "waiting", "capped", "failed", "timeout", "refused", "interrupted"}
DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_EVERY_RE = re.compile(r"^every\s+(\d{1,4})\s*([mhd])$")
_WEEKLY_RE = re.compile(r"^([a-z,\s]+?)\s+(\d{1,2}):(\d{2})$")
_ONCE_RE = re.compile(r"^once\s+(\d{4})-(\d{2})-(\d{2})\s+(\d{1,2}):(\d{2})$")
_UNIT = {"m": 60, "h": 3600, "d": 86400}
MODES = ("approval", "auto", "report")
_EDITABLE = {"name", "message", "schedule", "cap", "skill", "enabled", "mode"}
PROPOSALS_PER_CHAT = 5
PROPOSAL_TTL = 86400  # a proposal no turn collected (crash, spoofed thread) is dropped


def parse_schedule(text):
    """{"every": seconds}, {"days": [0..6], "at": (hour, minute)} or {"once": epoch}; ValueError
    otherwise."""
    t = " ".join(str(text or "").lower().split())
    m = _ONCE_RE.match(t)
    if m:
        y, mo, d, hour, minute = (int(x) for x in m.groups())
        if not (1 <= mo <= 12 and 1 <= d <= 31 and hour < 24 and minute < 60):
            raise ValueError("once: a real date and time, YYYY-MM-DD HH:MM")
        ts = time.mktime((y, mo, d, hour, minute, 0, 0, 0, -1))
        if time.localtime(ts)[:3] != (y, mo, d):
            raise ValueError("once: a real date and time, YYYY-MM-DD HH:MM")
        return {"once": ts}
    m = _EVERY_RE.match(t)
    if m:
        every = int(m.group(1)) * _UNIT[m.group(2)]
        if every < MIN_EVERY:
            raise ValueError("a job runs at most once every 5 minutes")
        return {"every": every}
    m = _WEEKLY_RE.match(t)
    if m:
        hour, minute = int(m.group(2)), int(m.group(3))
        words = [w.strip() for w in m.group(1).split(",") if w.strip()]
        if words == ["daily"]:
            days = list(range(7))
        elif words and all(w in DAYS for w in words):
            days = sorted({DAYS.index(w) for w in words})
        else:
            days = None
        if days is not None and hour < 24 and minute < 60:
            return {"days": days, "at": (hour, minute)}
    raise ValueError('schedule: "every 30m", "daily 09:00", "mon,thu 09:00" or "once 2026-12-01 09:00"')


def next_due(job):
    """When the job runs next: the interval after its last run (or creation), or the first
    weekly slot strictly after it."""
    sched = parse_schedule(job.get("schedule"))
    if "once" in sched:
        if job.get("last_run"):
            raise ValueError("ran already")
        return sched["once"]
    base = float(job.get("last_run") or job.get("created") or 0)
    if "every" in sched:
        return base + sched["every"]
    hour, minute = sched["at"]
    day = time.localtime(base)
    for i in range(9):
        t = time.mktime((day.tm_year, day.tm_mon, day.tm_mday + i, hour, minute, 0, 0, 0, -1))
        if t > base and time.localtime(t).tm_wday in sched["days"]:
            return t
    raise ValueError("no slot")  # unreachable: days is never empty


def is_due(job, now):
    if not job.get("enabled"):
        return False
    try:
        return now >= next_due(job)
    except ValueError:
        return False


def _path(root):
    return Path(root) / RELPATH


def _read(root, strict=False):
    """The file's rows. A missing file is empty; one that cannot be read or parsed is empty too,
    or ValueError when `strict` (a write must never replace it with nothing)."""
    try:
        data = json.loads(_path(root).read_text(encoding="utf-8"))
    except FileNotFoundError:
        data = {}
    except (OSError, ValueError):
        if strict:
            raise ValueError(f"{RELPATH} cannot be read: fix or remove it") from None
        data = {}
    if not isinstance(data, dict):
        if strict:
            raise ValueError(f"{RELPATH} cannot be read: fix or remove it")
        return {"jobs": [], "digest": {}, "proposals": [], "checks": []}
    rows, digest, props = data.get("jobs"), data.get("digest"), data.get("proposals")
    checks = data.get("checks")
    return {"jobs": [r for r in rows if isinstance(r, dict) and r.get("id")] if isinstance(rows, list) else [],
            "digest": digest if isinstance(digest, dict) else {},
            "proposals": [r for r in props if isinstance(r, dict)] if isinstance(props, list) else [],
            "checks": [r for r in checks if isinstance(r, dict)] if isinstance(checks, list) else []}


def _write(root, data):
    bots._atomic_write(_path(root), json.dumps(data, indent=2, ensure_ascii=False) + "\n")


def _change(root, fn):
    """Load, change and save under the file lock; returns what `fn` returns."""
    with memory.locked(_path(root)):
        data = _read(root, strict=True)
        out = fn(data)
        _write(root, data)
        return out


def load(root):
    return _read(root)["jobs"]


def get(root, job_id):
    return next((j for j in load(root) if j.get("id") == job_id), None)


def mode_of(job):
    mode = (job or {}).get("mode")
    return mode if mode in MODES else "approval"


def view(job):
    """A job as screens and list-jobs show it: its mode resolved and its next run (None when off
    or unreadable)."""
    try:
        due = next_due(job) if job.get("enabled") else None
    except ValueError:
        due = None
    return {**job, "mode": mode_of(job), "next_due": due}


def clean(field, value):
    """One field as saved, or ValueError naming what is wrong."""
    if field == "name":
        v = " ".join(str(value or "").split())
        if not 1 <= len(v) <= NAME_MAX:
            raise ValueError(f"name: 1 to {NAME_MAX} characters")
        return v
    if field == "message":
        v = str(value or "").strip()
        if not 1 <= len(v) <= MESSAGE_MAX:
            raise ValueError(f"message: 1 to {MESSAGE_MAX} characters")
        return v
    if field == "schedule":
        v = " ".join(str(value or "").lower().split())
        parse_schedule(v)
        return v
    if field == "cap":  # optional: None, empty or 0 means no cap
        if value is None or (isinstance(value, str) and not value.strip()):
            return None
        try:
            v = float(value)
        except (TypeError, ValueError):
            raise ValueError("cap: dollars per day, 0 or empty for no cap") from None
        if not math.isfinite(v) or v < 0:
            raise ValueError("cap: dollars per day, 0 or empty for no cap")
        return v or None
    if field in ("skill", "bot", "workspace"):
        v = str(value or "").strip()
        if not v:
            raise ValueError(f"{field} is required")
        return v
    if field == "enabled":
        return bool(value)
    if field == "mode":
        v = str(value or "").strip().lower()
        if v not in MODES:
            raise ValueError(f"mode: one of {', '.join(MODES)}")
        return v
    raise ValueError(f"unknown field {field}")


def create(root, *, ws, bot, skill, name, message, schedule, cap, mode="approval", now=None):
    job = {"id": uuid.uuid4().hex[:8], "name": clean("name", name), "workspace": clean("workspace", ws),
           "bot": clean("bot", bot), "skill": clean("skill", skill), "message": clean("message", message),
           "schedule": clean("schedule", schedule), "cap": clean("cap", cap),
           "mode": clean("mode", mode), "enabled": True,
           "created": time.time() if now is None else now, "last_run": None, "last_status": None,
           "last_chat": None}

    def fn(data):
        if len(data["jobs"]) >= MAX_JOBS:
            raise ValueError(f"at most {MAX_JOBS} jobs: delete one first")
        data["jobs"].append(job)
    _change(root, fn)
    return dict(job)


def _find(data, job_id):
    job = next((j for j in data["jobs"] if j.get("id") == job_id), None)
    if job is None:
        raise LookupError(f"no job {job_id}")
    return job


def update(root, job_id, **fields):
    unknown = set(fields) - _EDITABLE
    if unknown:
        raise ValueError(f"not editable: {', '.join(sorted(unknown))}")
    # None leaves a field as it is, except the cap: None, "" or 0 there means no cap.
    changes = {k: clean(k, v) for k, v in fields.items() if v is not None or k == "cap"}

    def fn(data):
        job = _find(data, job_id)
        job.update(changes)
        return dict(job)
    return _change(root, fn)


def restore(root, job):
    """Put back a job deleted earlier (an Undo), as it was. ValueError when its id is taken or
    the list is full."""
    if not isinstance(job, dict) or not job.get("id"):
        raise ValueError("nothing to restore")

    def fn(data):
        if any(j.get("id") == job["id"] for j in data["jobs"]):
            raise ValueError("that job exists already")
        if len(data["jobs"]) >= MAX_JOBS:
            raise ValueError(f"at most {MAX_JOBS} jobs: delete one first")
        data["jobs"].append(dict(job))
    _change(root, fn)
    return dict(job)


def delete(root, job_id):
    def fn(data):
        before = len(data["jobs"])
        data["jobs"] = [j for j in data["jobs"] if j.get("id") != job_id]
        return len(data["jobs"]) != before
    return _change(root, fn)


def mark_start(root, job_id, now):
    """Written before the run: a restart then waits for the next due time. Returns the job as
    saved now (an edit made since the tick read the file counts), or None when it was turned
    off meanwhile (not marked). LookupError when it was deleted."""
    def fn(data):
        job = _find(data, job_id)
        if not job.get("enabled"):
            return None
        job.update(last_run=now, last_status="running")
        if str(job.get("schedule") or "").startswith("once "):
            job["enabled"] = False  # one run: off after it, kept for its record
        return dict(job)
    return _change(root, fn)


def mark_end(root, job_id, status, chat=None):
    if status not in STATUSES:
        raise ValueError(f"bad status {status}")

    def fn(data):
        try:
            job = _find(data, job_id)
        except LookupError:  # deleted while it ran
            return
        job.update(last_status=status, last_chat=chat)
    _change(root, fn)


def recover(root):
    """A run cut off by a stop is `interrupted`, never re-run before its next due time."""
    def fn(data):
        n = 0
        for job in data["jobs"]:
            if job.get("last_status") == "running":
                job["last_status"] = "interrupted"
                n += 1
        return n
    if not _path(root).exists():
        return 0
    return _change(root, fn)


def _fresh(rows, now):
    def age(p):
        try:
            return now - float(p.get("created") or 0)
        except (TypeError, ValueError):
            return PROPOSAL_TTL + 1
    return [p for p in rows if age(p) <= PROPOSAL_TTL]


def add_proposal(root, rec, now=None):
    """Queue one bot proposal for its thread (`rec["chat"]`); the server collects it."""
    chat = rec.get("chat")
    if not isinstance(chat, str) or not chat:
        raise ValueError("a job is proposed from a chat thread")
    now = time.time() if now is None else now
    row = {**rec, "created": now}

    def fn(data):
        data["proposals"] = _fresh(data["proposals"], now)
        if sum(1 for p in data["proposals"] if p.get("chat") == chat) >= PROPOSALS_PER_CHAT:
            raise ValueError(f"at most {PROPOSALS_PER_CHAT} job proposals per thread")
        data["proposals"].append(row)
    _change(root, fn)
    return dict(row)


def take_proposals(root, chat, now=None):
    """Remove and return this thread's proposals; drop any older than PROPOSAL_TTL."""
    now = time.time() if now is None else now

    def fn(data):
        fresh = _fresh(data["proposals"], now)
        mine = [p for p in fresh if p.get("chat") == chat]
        data["proposals"] = [p for p in fresh if p.get("chat") != chat]
        return mine
    # Every chat turn ends here: read first, and lock and rewrite only when this thread has some.
    if not any(p.get("chat") == chat for p in _read(root)["proposals"]):
        return []
    return _change(root, fn)


CHECKS_PER_CHAT = 5


def add_check(root, rec, now=None):
    """Queue one evidence-contradiction check for its thread (`rec["chat"]`); the server collects it."""
    chat = rec.get("chat")
    if not isinstance(chat, str) or not chat:
        raise ValueError("a contradiction check comes from a chat thread")
    now = time.time() if now is None else now
    row = {**rec, "created": now}

    def fn(data):
        data["checks"] = _fresh(data["checks"], now)
        if sum(1 for p in data["checks"] if p.get("chat") == chat) >= CHECKS_PER_CHAT:
            raise ValueError(f"at most {CHECKS_PER_CHAT} contradiction checks per thread")
        data["checks"].append(row)
    _change(root, fn)
    return dict(row)


def take_checks(root, chat, now=None):
    """Remove and return this thread's queued checks; drop any older than PROPOSAL_TTL."""
    now = time.time() if now is None else now

    def fn(data):
        fresh = _fresh(data["checks"], now)
        mine = [p for p in fresh if p.get("chat") == chat]
        data["checks"] = [p for p in fresh if p.get("chat") != chat]
        return mine
    if not any(p.get("chat") == chat for p in _read(root)["checks"]):
        return []
    return _change(root, fn)


def set_live(root, job_id, spent, now=None):
    """What the running job's turn has spent so far, saved runs and the run in flight. A model
    run the turn starts (generate-*, revise-post) counts it against the job's cap: the turn's
    own spend is saved only when it ends. `job_id` None clears it."""
    path = Path(root) / LIVE_RELPATH
    if job_id is None:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        return
    bots._atomic_write(path, json.dumps({"job": job_id, "spent": round(float(spent), 6),
                                         "ts": time.time() if now is None else now}) + "\n")


def live_spent(root, job_id, now=None):
    """The spend set_live saved for this job's running turn, else 0."""
    try:
        data = json.loads((Path(root) / LIVE_RELPATH).read_text(encoding="utf-8"))
        spent, ts = float(data["spent"]), float(data["ts"])
    except (OSError, ValueError, TypeError, KeyError):
        return 0.0
    now = time.time() if now is None else now
    if data.get("job") != job_id or not math.isfinite(spent) or not 0 <= now - ts <= LIVE_MAX_AGE:
        return 0.0
    return max(0.0, spent)


def digest_state(root):
    return dict(_read(root)["digest"])


def set_digest(root, ts):
    def fn(data):
        data["digest"] = {"last": ts}
    _change(root, fn)
