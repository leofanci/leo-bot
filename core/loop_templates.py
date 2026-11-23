"""Loop templates: a skill file with `kind: loop` and a `bot:` field, written as the parts every
loop needs (spec "Standard", Loop and Loop template). A job may use a template as its skill
when the template's bot is the job's bot (dashboard/job_cards.py check_target), so a template
never grows a bot's card. The run loads the body like any explicit skill.

Frontmatter: name, kind: loop, bot (a bot id), schedule (the suggested cadence, a jobs.py
schedule string), description.
"""
import re

from core import bots, jobs, skill_loads

PARTS = ("Cadence", "Acts when", "Purpose", "Skills used", "Body", "Self-check", "State",
         "Stop or expiry", "Output", "Post-run critique", "Cost profile", "Success metrics",
         "Handoff points")
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")


def _split(text):
    from dashboard import fileops  # same frontmatter parser as skills
    return fileops._parse_frontmatter(text or "")


def _read(name):
    from core import workflows  # its path guard: the file stays inside skills/
    if not isinstance(name, str) or not _NAME_RE.match(name) or name not in skill_loads.names():
        return None
    f = workflows._inside(skill_loads.SKILLS_DIR, name, "SKILL.md")
    return f.read_text(encoding="utf-8") if f is not None and f.is_file() else None


def is_loop(text):
    return (_split(text)[0].get("kind") or "").strip().lower() == "loop"


def problems(text):
    """What a template text lacks: a part, its bot or its schedule. [] when it holds up."""
    fm, body = _split(text)
    found = {h.strip().lower() for h in re.findall(r"^##\s+(.+?)\s*$", body, re.M)}
    out = [f"no ## {p} part" for p in PARTS if p.lower() not in found]
    bot = (fm.get("bot") or "").strip().lstrip("@").lower()
    if not bot:
        out.append("no bot")
    elif bot not in bots.load_defs():
        out.append(f"unknown bot @{bot}")
    try:
        jobs.parse_schedule(fm.get("schedule"))
    except ValueError:
        out.append("no valid schedule")
    return out


def load(name):
    """{"name", "bot", "schedule", "description", "body"} of a `kind: loop` skill, else None."""
    text = _read(name)
    if text is None or not is_loop(text):
        return None
    fm, body = _split(text)
    return {"name": fm.get("name") or name, "bot": (fm.get("bot") or "").strip().lstrip("@").lower(),
            "schedule": fm.get("schedule") or "", "description": fm.get("description") or "", "body": body}


def names():
    """Every shipped loop template."""
    return [n for n in skill_loads.names() if load(n)]
