"""Workflows: a skill file with `kind: workflow` whose `## Steps` list names, per step, the skill,
the bot, the output, the gate and an optional checker bot. The server (dashboard/workflow_turns.py)
runs one step per chat turn in the owner's private thread; this module holds the template parser,
the per-thread run record and the pure state machine that decides what comes after each turn.

Step line (one numbered line per step, fields split by `|`, keys case-insensitive):
    1. skill: problem-validation | bot: product | output: problem verdict | gate: no | checker: research
"""
import json
import re
import time
from dataclasses import dataclass

from core import bots, general, skill_loads

MAX_STEPS = 12
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


@dataclass(frozen=True)
class Workflow:
    name: str
    description: str
    steps: tuple
    body: str           # the file body without the steps list


def _is_workflow_file(name):
    from dashboard import fileops  # same frontmatter parser as skills
    f = skill_loads.SKILLS_DIR / name / "SKILL.md"
    if not _NAME_RE.match(name or "") or not f.is_file():
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
    checker = (fields.get("checker") or "").lower().lstrip("@") or None
    if checker == bot:
        raise ValueError(f"step {n}: the checker must be a different bot from the maker")
    return Step(skill, bot, fields["output"], gate == "approval", checker)


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
    return out
