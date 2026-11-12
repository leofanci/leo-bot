"""job_cards.py: scheduled-job proposals, from a bot's words to the owner's click.

A bot never creates, changes or deletes a job. It runs `osctl propose-job`, which checks
the proposal here and queues it in the outbox (core/jobs.py). When that turn ends the
server checks it again, from the thread's own workspace, and holds it as a `job` card
(core/approvals.py). Approve calls `apply`, which checks it a third time against the jobs
and team as they are then, and only then writes the job.

A proposal is {"op": "create" | "update" | "delete", "job_id", "fields"}. Every field is
untrusted text a bot wrote; the card shows exactly what Approve saves.
"""
import time

from core import bots, jobs

OPS = ("create", "update", "delete")
_CREATE_NEEDS = ("bot", "skill", "name", "message", "schedule", "cap")
_UPDATABLE = ("name", "message", "schedule", "cap", "skill", "mode")
_LABELS = {"name": "Name", "message": "Message", "schedule": "Schedule", "cap": "Cap",
           "skill": "Skill", "mode": "Mode"}
MODE_TEXT = {"approval": "Approval: saves wait for your click",
             "auto": "Auto: saves by itself; deletes, memory and app actions wait for you",
             "report": "Report only: reads and reports, saves nothing"}


def check_target(root, ws, bot, skill, projects):
    """ValueError when this workspace, bot, skill or engine could not run a job."""
    project = None
    if ws != bots.HQ:
        project = next((p for p in projects if p.get("slug") == ws), None)
        if project is None:
            raise ValueError(f"no workspace {ws}")
    team = bots.team(root, ws, project)
    b = team.get(bot)
    if not b or not b.active:
        raise ValueError(f"@{bot} is not an active bot in {ws}")
    if skill not in team.skills_of(bot):
        raise ValueError(f"/{skill} is not one of @{bot}'s skills")
    from dashboard.engines import registry
    from dashboard.engines.base import EngineRefused
    try:
        engine = registry.pick(root, ws, bot)
    except EngineRefused as e:
        raise ValueError(str(e)) from None
    if not registry.metered(engine):
        raise ValueError(f"Jobs need an engine that reports cost; {engine.label} does not. "
                         "Pick another engine for this bot in Settings, Engines.")


def _ws_name(ws):
    return "HQ" if ws == bots.HQ else ws


def _when(ts):
    return time.strftime("%a %d %b %H:%M", time.localtime(ts))


def _show(field, value):
    if field == "cap":
        return f"${value:g} per 24 hours"
    if field == "mode":
        return MODE_TEXT[value]
    if field == "skill":
        return f"/{value}"
    return str(value)


def _clean(field, value):
    try:
        return jobs._clean(field, value)
    except ValueError as e:
        raise ValueError(str(e)) from None


def _in_scope(root, thread_ws, job_id):
    job = jobs.get(root, job_id) if isinstance(job_id, str) and job_id else None
    if job is None or (thread_ws != bots.HQ and job.get("workspace") != thread_ws):
        raise ValueError(f"no job {job_id}")
    return job


def proposal(root, *, thread_ws, op, job_id=None, fields=None, projects=()):
    """The checked proposal with its card text, or ValueError. `thread_ws` is the workspace
    of the thread it was proposed in: a workspace thread reaches only its own jobs, HQ any."""
    if op not in OPS:
        raise ValueError(f"op: one of {', '.join(OPS)}")
    fields = dict(fields or {})
    if op == "create":
        missing = [k for k in _CREATE_NEEDS if fields.get(k) in (None, "")]
        if missing:
            raise ValueError(f"a new job needs {', '.join(missing)}")
        ws = str(fields.get("workspace") or thread_ws).strip()
        if thread_ws != bots.HQ and ws != thread_ws:
            raise ValueError("a workspace thread proposes jobs for its own workspace only")
        clean = {k: _clean(k, fields[k]) for k in ("bot", "skill", "name", "message", "schedule", "cap")}
        clean["mode"] = _clean("mode", fields.get("mode") or "approval")
        clean["workspace"] = ws
        check_target(root, ws, clean["bot"], clean["skill"], projects)
        if len(jobs.load(root)) >= jobs.MAX_JOBS:
            raise ValueError(f"at most {jobs.MAX_JOBS} jobs: delete one first")
        next_run = jobs.next_due({"schedule": clean["schedule"], "created": time.time()})
        name = clean["name"]
        rows = [["Bot", f"@{clean['bot']} /{clean['skill']}"], ["Workspace", _ws_name(ws)],
                ["Schedule", clean["schedule"]], ["Next run", _when(next_run)],
                ["Mode", MODE_TEXT[clean["mode"]]], ["Cap", _show("cap", clean["cap"])],
                ["Message", clean["message"]]]
        return {"op": op, "job_id": None, "fields": clean, "summary": f"New job: {name}",
                "preview": {"title": f"New job: {name}", "fields": rows}}
    job = _in_scope(root, thread_ws, job_id)
    name = job.get("name") or job_id
    if op == "delete":
        rows = [["Bot", f"@{job.get('bot')} /{job.get('skill')}"], ["Workspace", _ws_name(job.get("workspace") or bots.HQ)],
                ["Schedule", job.get("schedule")]]
        return {"op": op, "job_id": job["id"], "fields": {}, "summary": f"Delete job: {name}",
                "preview": {"title": f"Delete job: {name}", "fields": rows}}
    unknown = sorted(k for k in fields if k not in _UPDATABLE)
    if unknown:
        raise ValueError(f"not changeable here: {', '.join(unknown)}")
    clean = {k: _clean(k, v) for k, v in fields.items() if v not in (None, "")}
    if not clean:
        raise ValueError("say what changes: name, message, schedule, cap, skill or mode")
    if "skill" in clean:
        check_target(root, job.get("workspace") or bots.HQ, job.get("bot"), clean["skill"], projects)
    rows = []
    for k, v in clean.items():
        old = job.get(k) if k != "mode" else jobs.mode_of(job)
        rows.append([_LABELS[k], f"{_show(k, old) if old not in (None, '') else '-'} → {_show(k, v)}"])
    return {"op": op, "job_id": job["id"], "fields": clean, "summary": f"Change job: {name}",
            "preview": {"title": f"Change job: {name}", "fields": rows}}


def apply(root, rec, projects):
    """Write what an approved card holds, after checking it again. The result line;
    ValueError when it may no longer be saved."""
    stored = rec.get("job") or {}
    p = proposal(root, thread_ws=rec.get("ws") or bots.HQ, op=stored.get("op"),
                 job_id=stored.get("job_id"), fields=stored.get("fields"), projects=projects)
    f = p["fields"]
    if p["op"] == "create":
        job = jobs.create(root, ws=f["workspace"], bot=f["bot"], skill=f["skill"], name=f["name"],
                          message=f["message"], schedule=f["schedule"], cap=f["cap"], mode=f["mode"])
        return f"Job created: {job['name']}, next run {_when(jobs.next_due(job))}."
    if p["op"] == "update":
        try:
            job = jobs.update(root, p["job_id"], **f)
        except LookupError as e:
            raise ValueError(str(e)) from None
        return f"Job changed: {job['name']}."
    if not jobs.delete(root, p["job_id"]):
        raise ValueError(f"no job {p['job_id']}")
    return p["summary"].replace("Delete job", "Job deleted") + "."
