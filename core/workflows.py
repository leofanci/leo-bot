"""Workflows: a skill file with `kind: workflow` whose `## Steps` list names, per step, the skill,
the bot, the output, the gate, an optional checker bot and up to 3 bots consulted in order. The server (dashboard/workflow_turns.py)
runs one step per chat turn in the owner's private thread; this module holds the template parser,
the per-thread run record and the pure state machine that decides what comes after each turn.

Step line (one numbered line per step, fields split by `|`, keys case-insensitive):
    1. skill: problem-validation | bot: product | output: problem verdict | gate: no | checker: research
    2. skill: launch-plan | bot: gtm | output: the plan | gate: approval | checker: product | consult: research, cmo
"""
import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path

from core import bots, general, skill_loads

MAX_STEPS = 12
MAX_CONSULTS = 3
_STEPS_RE = re.compile(r"^##\s+Steps\s*$", re.M)
_STEP_LINE_RE = re.compile(r"^\s*\d+\.\s+(.*)$")
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")


@dataclass(frozen=True)
class Step:
    skill: str
    bot: str
    output: str
    gate: bool          # True: the step ends in one APPROVAL: line (a card)
    checker: str | None
    consult: tuple = ()  # bots that advise on the draft, in order, before the maker revises


@dataclass(frozen=True)
class Workflow:
    name: str
    description: str
    steps: tuple
    body: str           # the file body without the steps list


def _inside(base, *parts):
    """base/parts as a Path, or None when the result would leave `base`."""
    base = os.path.normpath(str(base))
    full = os.path.normpath(os.path.join(base, *parts))
    return Path(full) if full.startswith(base + os.sep) else None


def _is_workflow_file(name):
    from dashboard import fileops  # same frontmatter parser as skills
    if not isinstance(name, str) or not _NAME_RE.match(name):
        return None
    f = _inside(skill_loads.SKILLS_DIR, name, "SKILL.md")
    if f is None or not f.is_file():
        return None
    text = f.read_text(encoding="utf-8")
    fm, _ = fileops._parse_frontmatter(text)
    return text if fm.get("kind", "").strip().lower() == "workflow" else None


def _parse_step(n, line):
    fields = {}
    for part in line.split("|"):
        k, sep, v = part.partition(":")
        if sep:
            fields[k.strip().lower()] = v.strip()
    for key in ("skill", "bot", "output"):
        if not fields.get(key):
            raise ValueError(f"step {n} has no {key}")
    gate = fields.get("gate", "no").lower()
    if gate not in ("no", "approval"):
        raise ValueError(f"step {n}: gate must be no or approval")
    skill, bot = fields["skill"].lower(), fields["bot"].lower().lstrip("@")
    if skill not in skill_loads.names():
        raise ValueError(f"step {n}: unknown skill {skill}")
    if _is_workflow_file(skill) is not None:
        raise ValueError(f"step {n}: {skill} is a workflow, not a skill")
    from core import loop_templates
    if loop_templates.load(skill):
        raise ValueError(f"step {n}: {skill} is a loop, not a skill")
    checker = (fields.get("checker") or "").lower().lstrip("@") or None
    if checker == bot:
        raise ValueError(f"step {n}: the checker must be a different bot from the maker")
    consult = tuple(c.strip().lower().lstrip("@") for c in fields.get("consult", "").split(","))
    consult = () if consult == ("",) else consult
    if any(not c for c in consult):
        raise ValueError(f"step {n}: consult names an empty bot")
    if len(consult) > MAX_CONSULTS:
        raise ValueError(f"step {n}: consult names more than {MAX_CONSULTS} bots")
    if len(set(consult)) != len(consult):
        raise ValueError(f"step {n}: consult names a bot twice")
    if bot in consult or (checker and checker in consult):
        raise ValueError(f"step {n}: consult must name bots other than the maker and the checker")
    return Step(skill, bot, fields["output"], gate == "approval", checker, consult)


def parse(text, name):
    """Workflow from a SKILL.md text; ValueError with a one-line reason when it does not hold up."""
    from dashboard import fileops
    fm, body = fileops._parse_frontmatter(text)
    m = _STEPS_RE.search(body)
    if not m:
        raise ValueError("no ## Steps section")
    rest = body[m.end():]
    nxt = re.search(r"^##\s", rest, re.M)
    block, after = (rest[:nxt.start()], rest[nxt.start():]) if nxt else (rest, "")
    lines = [s.group(1) for s in map(_STEP_LINE_RE.match, block.splitlines()) if s]
    if not lines:
        raise ValueError("the Steps list is empty")
    if len(lines) > MAX_STEPS:
        raise ValueError(f"more than {MAX_STEPS} steps")
    steps = tuple(_parse_step(i, line) for i, line in enumerate(lines, 1))
    clean = (body[:m.start()].rstrip() + "\n\n" + after.strip()).strip()
    return Workflow(fm.get("name", name) or name, fm.get("description", ""), steps, clean)


def load(name):
    """The workflow called `name`, or None (missing, not kind: workflow, or broken)."""
    text = _is_workflow_file(name)
    if text is None:
        return None
    try:
        return parse(text, name)
    except ValueError:
        return None


def names():
    """Every workflow that loads, sorted."""
    d = skill_loads.SKILLS_DIR
    if not d.is_dir():
        return []
    return sorted(p.name for p in d.iterdir() if load(p.name) is not None)


def _bot_problem(team, bot_id):
    b = team.get(bot_id)
    if b is None:
        return f"@{bot_id} is not on the team"
    if not b.active:
        return f"@{bot_id} is switched off"
    return None


def problems(wf, team):
    """One line per step that cannot run with this team; [] when every step can."""
    out = []
    for n, s in enumerate(wf.steps, 1):
        why = _bot_problem(team, s.bot)
        if why:
            out.append(f"Step {n}: {why}.")
        elif s.skill not in team.skills_of(s.bot):
            out.append(f"Step {n}: @{s.bot} does not have /{s.skill}.")
        if s.checker:
            why = _bot_problem(team, s.checker)
            if why:
                out.append(f"Step {n} checker: {why}.")
        for c in s.consult:
            why = _bot_problem(team, c)
            if why:
                out.append(f"Step {n} consult: {why}.")
    return out


# --- run record ---------------------------------------------------------------------------
# One run per thread: root/team/workflow_runs/<chat>.json. Phases of the current step:
#   stage   the step's bot does the work (a draft when the step has consults)
#   consult one turn per consultant, in order (`consulted` counts them): advice, read-only
#   revise  the step's bot rewrites its draft with the advice
#   check   the checker bot answers PASS or WEAK: <notes>
#   save    approval gate with a checker: the step's bot proposes the save once the check passed
#   approve waiting on a card raised in `card_phase`

def _path(root, chat):
    chat = (chat or "").strip() if isinstance(chat, str) else ""
    if not skill_loads._CHAT_RE.match(chat):
        return None
    return _inside(Path(root) / "team" / "workflow_runs", f"{chat}.json")


def new_run(wf, brief, ws, room):
    now = time.time()
    return {"name": wf.name, "chat": None, "ws": ws, "room": room, "brief": brief, "step": 0,
            "phase": "stage", "card_phase": "", "consulted": 0, "retried": False, "notes": "", "status": "running",
            "why": "", "cost": 0.0, "started": now, "updated": now}


def get(root, chat):
    f = _path(root, chat)
    if f is None or not f.is_file():
        return None
    try:
        run = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return run if isinstance(run, dict) and run.get("chat") == chat else None


def save(root, run):
    f = _path(root, run.get("chat"))
    if f is None:
        return
    f.parent.mkdir(parents=True, exist_ok=True)
    bots._atomic_write(f, json.dumps(dict(run, updated=time.time()), indent=2) + "\n")


def drop(root, chat):
    f = _path(root, chat)
    if f is not None:
        f.unlink(missing_ok=True)


def drop_ws(root, ws):
    """Every run of a deleted workspace goes with it."""
    d = root / "team" / "workflow_runs"
    for f in d.glob("*.json") if d.is_dir() else ():
        run = get(root, f.stem)
        if run is None or run.get("ws") == ws:
            f.unlink(missing_ok=True)


def cost_cap():
    """Settings, General, workflow_cost: dollars per run, 0.0 for no cap. Never raises."""
    try:
        return float(general.view()["workflow_cost"])
    except Exception:  # noqa: BLE001 - a hand-edited file never breaks a turn
        return 0.0


# --- state machine -------------------------------------------------------------------------

_PASS_RE = re.compile(r"^PASS\b")
_WEAK_RE = re.compile(r"^WEAK:\s*(.+)")
_BLOCKED_RE = re.compile(r"^BLOCKED:\s*(.+)", re.M)


def verdict(text):
    """(passed, notes) from a checker's reply. No PASS or WEAK line counts as weak: the checker
    defaults to rejecting."""
    for line in (text or "").splitlines():
        line = line.strip().strip("*`").strip()
        if _PASS_RE.match(line):
            return True, ""
        m = _WEAK_RE.match(line)
        if m:
            return False, m.group(1).strip()
    return False, "The checker gave no PASS or WEAK line."


def _last_reply(out, bot_id):
    texts = [t for b, t in out.get("replies") or [] if b == bot_id]
    return texts[-1] if texts else ""


def _step_done(wf, run):
    run.update(step=run["step"] + 1, phase="stage", card_phase="", consulted=0, retried=False, notes="",
               status="running", why="")
    if run["step"] >= len(wf.steps):
        run["status"] = "done"


def _weak(run, n, notes, phase="stage"):
    """One retry of `phase` with the notes, then a pause."""
    if not run["retried"]:
        run.update(phase=phase, retried=True, notes=notes, status="running", why="")
    else:
        run.update(phase=phase, notes=notes, status="paused", why=f"Step {n} is still weak: {notes}")


def _next_phase(step, phase, run):
    """The phase after `phase` went through (consults already counted), or None: the step is done."""
    if phase == "stage" and step.consult:
        return "consult"
    if phase == "consult":
        return "consult" if int(run.get("consulted") or 0) < len(step.consult) else "revise"
    if phase in ("stage", "revise"):
        return "check" if step.checker else None
    if phase == "check":
        return "save" if step.gate else None
    return None


def _go(wf, run, step, phase):
    nxt = _next_phase(step, phase, run)
    if nxt == "consult" and phase == "stage":  # the draft is in: the revise phase gets its own retry
        run.update(retried=False, notes="")
    if nxt:
        run.update(phase=nxt, card_phase="", status="running", why="")
    else:
        _step_done(wf, run)


def advance(wf, run, out, cap):
    """The run after one turn's outcome (see the plan's rules). Pure: `run` is not changed."""
    run = dict(run)
    run["cost"] = round(float(run.get("cost") or 0) + float(out.get("cost") or 0), 6)
    if run["status"] in ("done", "stopped") or run["step"] >= len(wf.steps):
        return run
    step, n, phase = wf.steps[run["step"]], run["step"] + 1, run["phase"]
    if out.get("stopped") or out.get("failed"):
        run.update(status="paused", why="Stopped." if out.get("stopped") else "The last step failed.")
    elif phase == "approve":
        if out.get("answer") == "approved" and not out.get("card"):
            done = run.get("card_phase") or "stage"
            if done == "consult":
                run["consulted"] = int(run.get("consulted") or 0) + 1
            _go(wf, run, step, done)
    elif out.get("card"):
        run.update(phase="approve", card_phase=phase, status="paused", why="Waiting for your approval.")
    elif phase in ("stage", "revise"):
        m = _BLOCKED_RE.search(_last_reply(out, step.bot))
        if m:
            _weak(run, n, m.group(1).strip(), phase)
        else:
            _go(wf, run, step, phase)
    elif phase == "consult":  # advice only: a consultant's BLOCKED line never stops the step
        run["consulted"] = int(run.get("consulted") or 0) + 1
        _go(wf, run, step, phase)
    elif phase == "check":
        ok, notes = verdict(_last_reply(out, step.checker))
        if not ok:
            _weak(run, n, notes, "revise" if step.consult else "stage")
        else:
            _go(wf, run, step, phase)
    else:  # save
        _step_done(wf, run)
    if run["status"] != "done" and cap > 0 and run["cost"] >= cap:
        run.update(status="paused", why="Reached this run's cost cap.")
    return run


def resume(run):
    """A paused run made ready for its next turn; a stopped or done run is returned unchanged."""
    if run.get("status") != "paused":
        return dict(run)
    run = dict(run, status="running", why="")
    if run["phase"] == "approve":  # its card was answered no: Continue redoes that turn
        run.update(phase=run.get("card_phase") or "stage", card_phase="")
    elif run["phase"] in ("stage", "revise") and run.get("retried") and run.get("notes"):
        run["retried"] = False  # paused still weak: Continue gives the step a fresh retry
    return run
