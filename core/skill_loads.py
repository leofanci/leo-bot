"""Skills a bot loads on demand (`osctl get-skill`), and the per-chat record of those loads.

A bot sees a short menu of its skills in its card and pulls a body only when the task needs
it. `osctl get-skill` records each load under the chat's id, so the server can keep that
skill active for the thread (stronger model on the next turn) and an Auto job run can tell
that it reached past its own skill. Engines that do not pass the chat id record nothing:
the turn still works, it simply does not escalate.
"""
import json
import re

from core import bots, paths

SKILLS_DIR = paths.CODE / "skills"
_CHAT_RE = re.compile(r"^[A-Za-z0-9_-]+$")

# Skills fully covered by CHAT_RAIL: a short stub stands in for the body (avoids ~1k+
# duplicate tokens per turn).
RAIL_STUBS: dict[str, str] = {
    "workspace": (
        "You route for the team (rules in system prompt). Work out what the user needs. "
        "A teammate owns it → @mention them and say why in one line. Yours → do it. "
        "\"Set everything up\" → list the deliverables to make (one line each), get approval, "
        "then save one per turn."
    ),
}


def names() -> list[str]:
    """Every skill a bot can load: one folder per skill under skills/, plus the rail stubs."""
    found = {d.name for d in SKILLS_DIR.iterdir() if (d / "SKILL.md").is_file()} if SKILLS_DIR.is_dir() else set()
    return sorted(found | set(RAIL_STUBS))


def body(name: str) -> str:
    """A skill's instruction body (frontmatter stripped); '' when there is no such skill.
    Rail-covered skills return their short stub."""
    stub = RAIL_STUBS.get(name)
    if stub:
        return stub
    if name not in names():  # also keeps a name like "../x" inside skills/
        return ""
    txt = (SKILLS_DIR / name / "SKILL.md").read_text(encoding="utf-8")
    from dashboard import fileops  # same frontmatter parser as the skill picker
    return fileops._parse_frontmatter(txt)[1] if txt.startswith("---") else txt.strip()


def _path(root, chat):
    chat = (chat or "").strip()
    if not _CHAT_RE.match(chat):
        return None
    return root / "team" / "skill_loads" / f"{chat}.json"


def loaded(root, chat: str) -> list[str]:
    """Skills loaded in this chat since the record was last cleared, in order of first load."""
    f = _path(root, chat)
    if f is None or not f.is_file():
        return []
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [s for s in data if isinstance(s, str)] if isinstance(data, list) else []


def record(root, chat: str, skill: str) -> None:
    """Note that this chat loaded `skill` (no-op for a malformed chat id)."""
    f = _path(root, chat)
    if f is None:
        return
    got = loaded(root, chat)
    if skill not in got:
        bots._atomic_write(f, json.dumps(got + [skill]) + "\n")


def clear(root, chat: str) -> None:
    f = _path(root, chat)
    if f is not None:
        f.unlink(missing_ok=True)
