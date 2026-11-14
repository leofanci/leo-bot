"""job_cards.py: scheduled jobs a bot sets up itself, with the owner's Undo.

A bot runs `osctl propose-job`, which checks the change here and queues it in the outbox
(core/jobs.py). When that turn ends the server checks it again, from the thread's own
workspace, and applies it only in the owner's private thread (never a shared thread, an
invited person's or a job's own run). `apply` checks it against the jobs and team as they
are then, writes it, and returns the Undo the thread's receipt offers (core/approvals.py).

A proposal is {"op": "create" | "update" | "delete", "job_id", "fields"}. Every field is
untrusted text a bot wrote; the receipt shows exactly what was saved.
"""
import time

from core import bots, jobs

OPS = ("create", "update", "delete")
_CREATE_NEEDS = ("bot", "skill", "name", "message", "schedule")
_UPDATABLE = ("name", "message", "schedule", "cap", "skill", "mode", "check")
_LABELS = {"name": "Name", "message": "Message", "schedule": "Schedule", "cap": "Cap",
           "skill": "Skill", "mode": "Mode", "check": "Runs when"}
MODE_TEXT = {"approval": "Approval: saves wait for your click",
             "auto": "Auto: saves by itself; deletes, whole-document replaces, memory and app actions wait for you",
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
        return f"${value:g} per 24 hours" if value else "no cap"
    if field == "mode":
        return MODE_TEXT[value]
    if field == "skill":
        return f"/{value}"
    if field == "check":
        return f"{value['app']} {value['action']} shows something new" if value else "every time"
    return str(value)


def _name_free(existing, ws, name, skip=None):
    """One name per workspace: a second card for the same job never makes a twin."""
    if any(j.get("id") != skip and j.get("workspace") == ws
           and str(j.get("name") or "").lower() == name.lower() for j in existing):
        raise ValueError(f"a job named {name} already exists here: propose a change to it")


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
        clean = {k: jobs.clean(k, fields.get(k)) for k in ("bot", "skill", "name", "message", "schedule", "cap")}
        clean["mode"] = jobs.clean("mode", fields.get("mode") or "approval")
        clean["check"] = jobs.clean("check", fields.get("check"))
        clean["workspace"] = ws
        check_target(root, ws, clean["bot"], clean["skill"], projects)
        existing = jobs.load(root)
        if len(existing) >= jobs.MAX_JOBS:
            raise ValueError(f"at most {jobs.MAX_JOBS} jobs: delete one first")
        _name_free(existing, ws, clean["name"])
        next_run = jobs.next_due({"schedule": clean["schedule"], "created": time.time()})
        name = clean["name"]
        rows = [["Bot", f"@{clean['bot']} /{clean['skill']}"], ["Workspace", _ws_name(ws)],
                ["Schedule", clean["schedule"]], ["Next run", _when(next_run)],
                ["Mode", MODE_TEXT[clean["mode"]]], ["Cap", _show("cap", clean["cap"])],
                ["Runs when", _show("check", clean["check"])],
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
    # An empty field is no change, except the cap: "" or 0 there means no cap.
    clean = {k: jobs.clean(k, v) for k, v in fields.items() if v is not None and (v != "" or k in ("cap", "check"))}
    if not clean:
        raise ValueError("say what changes: name, message, schedule, cap, skill, mode or check")
    if "name" in clean:
        _name_free(jobs.load(root), job.get("workspace"), clean["name"], skip=job["id"])
    if "skill" in clean:
        check_target(root, job.get("workspace") or bots.HQ, job.get("bot"), clean["skill"], projects)
    rows = []
    for k, v in clean.items():
        old = job.get(k) if k != "mode" else jobs.mode_of(job)
        rows.append([_LABELS[k], f"{_show(k, old) if old not in (None, '') else '-'} → {_show(k, v)}"])
    return {"op": op, "job_id": job["id"], "fields": clean, "summary": f"Change job: {name}",
            "preview": {"title": f"Change job: {name}", "fields": rows}}


def apply(root, rec, projects):
    """Write what a proposal or approved card holds, after checking it again.
    (result line, undo, job id); ValueError when it may no longer be saved."""
    stored = rec.get("job") or {}
    p = proposal(root, thread_ws=rec.get("ws") or bots.HQ, op=stored.get("op"),
                 job_id=stored.get("job_id"), fields=stored.get("fields"), projects=projects)
    f = p["fields"]
    if p["op"] == "create":
        job = jobs.create(root, ws=f["workspace"], bot=f["bot"], skill=f["skill"], name=f["name"],
                          message=f["message"], schedule=f["schedule"], cap=f["cap"], mode=f["mode"],
                          check=f.get("check"))
        return (f"Job created: {job['name']}, next run {_when(jobs.next_due(job))}.",
                {"op": "delete", "job_id": job["id"]}, job["id"])
    before = jobs.get(root, p["job_id"])
    if before is None:
        raise ValueError(f"no job {p['job_id']}")
    if p["op"] == "update":
        try:
            job = jobs.update(root, p["job_id"], **f)
        except LookupError as e:
            raise ValueError(str(e)) from None
        old = {k: (before.get(k) if k != "mode" else jobs.mode_of(before)) for k in f}
        return f"Job changed: {job['name']}.", {"op": "update", "job_id": job["id"], "fields": old}, job["id"]
    if not jobs.delete(root, p["job_id"]):
        raise ValueError(f"no job {p['job_id']}")
    return (p["summary"].replace("Delete job", "Job deleted") + ".",
            {"op": "restore", "job": before}, before["id"])


def undo(root, u):
    """Run a receipt's Undo (from `apply`): the result line, or ValueError."""
    op = (u or {}).get("op")
    if op == "delete":
        job = jobs.get(root, u.get("job_id"))
        if job is None or not jobs.delete(root, u.get("job_id")):
            raise ValueError("that job is gone already")
        return f"Undone: job {job.get('name')} deleted."
    if op == "update":
        try:
            job = jobs.update(root, u.get("job_id"), **(u.get("fields") or {}))
        except LookupError:
            raise ValueError("that job is gone") from None
        return f"Undone: job {job['name']} is back as it was."
    if op == "restore":
        job = jobs.restore(root, u.get("job"))
        return f"Undone: job {job.get('name')} is back."
    raise ValueError("nothing to undo")
