"""jobs.py: scheduled jobs, stored in `team/jobs.json` in the data folder.

A job is one bot's recurring work: a skill plus a message, run on a schedule by the
server (dashboard/job_runner.py), read-only, with its own daily cap. The file spans
workspaces like team/approvals.json, so company bots cannot read it; osctl is the only
way to create or change a job.

Record: id, name, workspace, bot, skill, message, schedule, cap (dollars per rolling
24 hours, above 0), enabled, created, last_run, last_status, last_chat.

Schedule text, local time:
  every <N>m|h|d       an interval, at least MIN_EVERY
  <days> HH:MM         days = "daily" or a comma list of mon..sun
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
MIN_EVERY = 3600
NAME_MAX = 60
MESSAGE_MAX = 2000
STATUSES = {"running", "done", "waiting", "capped", "failed", "timeout", "refused", "interrupted"}
DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_EVERY_RE = re.compile(r"^every\s+(\d{1,4})\s*([mhd])$")
_WEEKLY_RE = re.compile(r"^([a-z,\s]+?)\s+(\d{1,2}):(\d{2})$")
_UNIT = {"m": 60, "h": 3600, "d": 86400}
_EDITABLE = {"name", "message", "schedule", "cap", "skill", "enabled"}


def parse_schedule(text):
    """{"every": seconds} or {"days": [0..6], "at": (hour, minute)}; ValueError otherwise."""
    t = " ".join(str(text or "").lower().split())
    m = _EVERY_RE.match(t)
    if m:
        every = int(m.group(1)) * _UNIT[m.group(2)]
        if every < MIN_EVERY:
            raise ValueError("a job runs at most once an hour")
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
    raise ValueError('schedule: "every 6h", "daily 09:00" or "mon,thu 09:00"')


def next_due(job):
    """When the job runs next: the interval after its last run (or creation), or the first
    weekly slot strictly after it."""
    sched = parse_schedule(job.get("schedule"))
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


def _read(root):
    try:
        data = json.loads(_path(root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"jobs": [], "digest": {}}
    if not isinstance(data, dict):
        return {"jobs": [], "digest": {}}
    rows = data.get("jobs")
    digest = data.get("digest")
    return {"jobs": [r for r in rows if isinstance(r, dict) and r.get("id")] if isinstance(rows, list) else [],
            "digest": digest if isinstance(digest, dict) else {}}


def _write(root, data):
    bots._atomic_write(_path(root), json.dumps(data, indent=2, ensure_ascii=False) + "\n")


def _change(root, fn):
    """Load, change and save under the file lock; returns what `fn` returns."""
    with memory.locked(_path(root)):
        data = _read(root)
        out = fn(data)
        _write(root, data)
        return out


def load(root):
    return _read(root)["jobs"]


def get(root, job_id):
    return next((j for j in load(root) if j.get("id") == job_id), None)


def _clean(field, value):
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
    if field == "cap":
        try:
            v = float(value)
        except (TypeError, ValueError):
            raise ValueError("cap: dollars per day, above 0") from None
        if not math.isfinite(v) or v <= 0:
            raise ValueError("cap: dollars per day, above 0")
        return v
    if field in ("skill", "bot", "workspace"):
        v = str(value or "").strip()
        if not v:
            raise ValueError(f"{field} is required")
        return v
    if field == "enabled":
        return bool(value)
    raise ValueError(f"unknown field {field}")


def create(root, *, ws, bot, skill, name, message, schedule, cap, now=None):
    job = {"id": uuid.uuid4().hex[:8], "name": _clean("name", name), "workspace": _clean("workspace", ws),
           "bot": _clean("bot", bot), "skill": _clean("skill", skill), "message": _clean("message", message),
           "schedule": _clean("schedule", schedule), "cap": _clean("cap", cap), "enabled": True,
           "created": time.time() if now is None else now, "last_run": None, "last_status": None,
           "last_chat": None}
    _change(root, lambda data: data["jobs"].append(job))
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
    clean = {k: _clean(k, v) for k, v in fields.items() if v is not None}

    def fn(data):
        job = _find(data, job_id)
        job.update(clean)
        return dict(job)
    return _change(root, fn)


def delete(root, job_id):
    def fn(data):
        before = len(data["jobs"])
        data["jobs"] = [j for j in data["jobs"] if j.get("id") != job_id]
        return len(data["jobs"]) != before
    return _change(root, fn)


def mark_start(root, job_id, now):
    """Written before the run: a restart then waits for the next due time."""
    def fn(data):
        job = _find(data, job_id)
        job.update(last_run=now, last_status="running")
    _change(root, fn)


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


def digest_state(root):
    return dict(_read(root)["digest"])


def set_digest(root, ts):
    def fn(data):
        data["digest"] = {"last": ts}
    _change(root, fn)
