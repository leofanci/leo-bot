"""bots.py — the team: named bots (Slack-style teammates) that own skills.

Definitions are generic roles, tracked in `bots/<id>.md` (frontmatter + job
description). Each workspace (HQ, or one project = one company of the holding)
switches its own bots on/off and keeps its own memory. User data, gitignored:

  team/state.json                   HQ on/off per bot
  team/memory/<bot>.md              HQ memory
  projects/<slug>/team/state.json   that workspace's on/off (smart defaults until set)
  projects/<slug>/team/<bot>.md     that workspace's private memory: the owner's private threads only
  projects/<slug>/team/shared/<bot>.md  its team memory: threads invited people can see
                                    (bots never mix workspaces, and the two never mix)

Every skill always has exactly one owner: a switched-off bot's skills roll up
to its nearest active manager. `cos` is the root and is always on.
"""
import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from core import paths

ROOT = paths.DATA  # user data; bot definitions ship in paths.CODE
ROOT_BOT = "cos"
CEO = "ceo"  # the user; top of the org chart, not a bot
HQ = "hq"    # the holding's own workspace (CEO office); every other workspace is a project
MEMORY_MAX_LINES = 40
MEMORY_TAIL_LINES = 20
MEMORY_LINE_MAX = 300

_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_MENTION_RE = re.compile(r"(?<![\w@/])@([a-z][a-z0-9-]*)\b")


@dataclass
class Bot:
    id: str
    name: str
    handle: str
    icon: str
    reports_to: str
    skills: tuple
    default_skill: str
    body: str = ""
    active: bool = True
    covering: list = field(default_factory=list)


def _parse(text):
    fm, body = {}, ""
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) >= 3:
            for line in parts[1].strip().splitlines():
                k, sep, v = line.partition(":")
                if sep:
                    fm[k.strip()] = v.strip()
            body = parts[2].strip()
    return fm, body


def load_defs(root=paths.CODE):
    """{id: Bot} from bots/*.md. Definitions ship with the code, so callers
    pass a data root only for state/memory; defs default to the code folder."""
    out = {}
    for f in sorted((Path(root) / "bots").glob("*.md")):
        fm, body = _parse(f.read_text(encoding="utf-8"))
        skills = tuple(s.strip() for s in fm.get("skills", "").split(",") if s.strip())
        out[f.stem] = Bot(
            id=f.stem,
            name=fm.get("name", f.stem),
            handle=fm.get("handle", f.stem),
            icon=fm.get("icon", "●"),
            reports_to=fm.get("reports_to", CEO),
            skills=skills,
            default_skill=fm.get("default_skill") or (skills[0] if skills else ""),
            body=body,
        )
    return out


def ws_dir(ws=HQ, root=ROOT):
    """Folder holding a workspace's team state (HQ, or an existing project)."""
    root = Path(root)
    if not ws or ws == HQ:
        return root / "team"
    if not _SLUG_RE.fullmatch(ws):  # one path segment: no "/", "..", or newline
        raise ValueError(f"unknown workspace: {ws}")
    project = root / "projects" / ws
    if not project.is_dir():
        raise ValueError(f"unknown workspace: {ws}")
    return project / "team"


def default_active(project=None):
    """Smart starting team for a workspace; the user can switch any bot later.
    HQ: Chief of Staff only. Venture: product + go-to-market team (Social if it
    has profiles). Brand: content team. CTO only when an app folder is linked."""
    if project is None:
        return {ROOT_BOT}
    if (project.get("kind") or "venture") == "brand":
        on = {ROOT_BOT, "social", "cmo"}
    else:
        on = {ROOT_BOT, "product", "cmo", "gtm", "research"}
        if project.get("profiles"):
            on.add("social")
    if project.get("folders"):
        on.add("cto")
    return on


def load_state(root=ROOT, ws=HQ, project=None):
    """{bot_id: on} for a workspace: saved choices over the smart defaults."""
    defaults = default_active(project)
    state = {b: b in defaults for b in load_defs()}
    try:
        data = json.loads((ws_dir(ws, root) / "state.json").read_text())
        saved = data.get("active") if isinstance(data, dict) else None
        state.update({k: bool(v) for k, v in (saved or {}).items() if k in state})
    except (OSError, ValueError):
        pass
    return state


def _atomic_write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def set_active(bot_id, active, root=ROOT, ws=HQ, project=None):
    if bot_id not in load_defs():
        raise ValueError(f"unknown bot: {bot_id}")
    if bot_id == ROOT_BOT and not active:
        raise ValueError("Chief of Staff is always on (other bots roll up to it)")
    state = load_state(root, ws, project)
    state[bot_id] = bool(active)
    _atomic_write(ws_dir(ws, root) / "state.json", json.dumps({"active": state}, indent=2) + "\n")
    return team(root, ws, project)


class Team:
    def __init__(self, defs, state):
        self.bots = defs
        for b in defs.values():
            b.active = b.id == ROOT_BOT or state.get(b.id, True)
        self._owner = {}
        for b in defs.values():
            for s in b.skills:
                self._owner[s] = self._roll_up(b.id)
        for b in defs.values():
            b.covering = [o.id for o in defs.values()
                          if not o.active and o.skills
                          and self._roll_up(o.id) == b.id]

    def manager(self, bot_id):
        b = self.bots.get(bot_id)
        if not b or bot_id == ROOT_BOT:
            return None
        up = b.reports_to
        return up if up in self.bots else ROOT_BOT

    def _roll_up(self, bot_id):
        seen = set()
        while bot_id and bot_id not in seen:
            seen.add(bot_id)
            b = self.bots.get(bot_id)
            if b and b.active:
                return bot_id
            bot_id = self.manager(bot_id)
        return ROOT_BOT

    def get(self, bot_id):
        return self.bots.get(bot_id)

    def resolve(self, bot_id):
        """Active bot standing in for `bot_id` (itself, or its nearest active manager)."""
        return self._roll_up(bot_id)

    def owner(self, skill):
        """Active bot that answers for this skill (unknown skills → cos)."""
        return self._owner.get(skill, ROOT_BOT)

    def skills_of(self, bot_id):
        """Effective skills: own first, then those covered for switched-off reports."""
        b = self.bots.get(bot_id)
        if not b or not b.active:
            return []
        own = [s for s in b.skills]
        extra = [s for s, o in self._owner.items() if o == bot_id and s not in own]
        return own + extra

    def mentioned(self, text):
        """First active bot @handle in the message, else None."""
        handles = {b.handle: b.id for b in self.bots.values() if b.active}
        for h in _MENTION_RE.findall((text or "").lower()):
            if h in handles:
                return handles[h]
        return None

    def mentioned_all(self, text):
        """Every active bot @handle in the message, in order, once each."""
        handles = {b.handle: b.id for b in self.bots.values() if b.active}
        return list(dict.fromkeys(handles[h] for h in _MENTION_RE.findall((text or "").lower())
                                  if h in handles))

    def roster_line(self, bot_id):
        b = self.bots[bot_id]
        return f"@{b.handle} {b.name}: {', '.join(self.skills_of(bot_id))}"

    def to_api(self):
        return [{
            "id": b.id, "name": b.name, "handle": b.handle, "icon": b.icon,
            "reports_to": b.reports_to, "manager": self.manager(b.id),
            "active": b.active, "locked": b.id == ROOT_BOT,
            "skills": list(b.skills), "effective_skills": self.skills_of(b.id),
            "covering": b.covering, "default_skill": b.default_skill,
            "about": b.body.split("\n", 1)[0],
            "recommended": b.id in getattr(self, "recommended", ()),
        } for b in self.ordered()]

    def ordered(self):
        """Org-chart order: depth first from the CEO, siblings by name."""
        out, seen = [], set()

        def walk(parent):
            kids = sorted((b for b in self.bots.values()
                           if b.id not in seen
                           and (b.reports_to == parent
                                or (parent == CEO and b.reports_to not in self.bots))),
                          key=lambda b: (b.id != ROOT_BOT, b.name))
            for b in kids:
                seen.add(b.id)
                out.append(b)
                walk(b.id)

        walk(CEO)
        out += [b for b in self.bots.values() if b.id not in seen]
        return out


def team(root=ROOT, ws=HQ, project=None):
    """One workspace's team: on/off read from data `root` (smart defaults
    from the `project` record until the user changes them)."""
    t = Team(load_defs(), load_state(root, ws, project))
    t.ws = ws or HQ
    t.recommended = default_active(project)
    return t


# --------------------------------------------------------------------------- #
# Memory — one short markdown list per bot per project. Never mixes projects.
# --------------------------------------------------------------------------- #
def memory_path(bot_id, project=None, root=ROOT, shared=False):
    """`shared`: the team memory of a project workspace. HQ has none: nobody joins HQ."""
    if not _SLUG_RE.fullmatch(bot_id or ""):
        raise ValueError("invalid bot id")
    root = Path(root)
    if project:
        if not _SLUG_RE.fullmatch(project) or not (root / "projects" / project).is_dir():
            raise ValueError(f"unknown project: {project}")
        team_dir = root / "projects" / project / "team"
        return (team_dir / "shared" if shared else team_dir) / f"{bot_id}.md"
    if shared:
        raise ValueError("HQ has no team memory")
    return root / "team" / "memory" / f"{bot_id}.md"


def read_memory(bot_id, project=None, root=ROOT, tail=MEMORY_TAIL_LINES, shared=False):
    try:
        lines = memory_path(bot_id, project, root, shared).read_text(encoding="utf-8").splitlines()
    except (OSError, ValueError):
        return ""
    lines = [ln for ln in lines if ln.strip()]
    return "\n".join(lines[-tail:]) if tail else "\n".join(lines)


def remember(bot_id, text, project=None, root=ROOT, today=None, shared=False):
    """Append one dated line to the bot's memory for this project (or HQ)."""
    if bot_id not in load_defs():
        raise ValueError(f"unknown bot: {bot_id}")
    line = " ".join((text or "").split())
    if not line:
        raise ValueError("empty memory")
    if len(line) > MEMORY_LINE_MAX:
        raise ValueError(f"memory line over {MEMORY_LINE_MAX} chars; keep it to one fact")
    path = memory_path(bot_id, project, root, shared)
    existing = read_memory(bot_id, project, root, tail=0, shared=shared)
    lines = existing.splitlines() if existing else []
    lines.append(f"- {(today or date.today()).isoformat()} {line}")
    _atomic_write(path, "\n".join(lines[-MEMORY_MAX_LINES:]) + "\n")
    return str(path.relative_to(Path(root)))


def forget(bot_id, line, project=None, root=ROOT, shared=False):
    """Delete one memory line (exact text, first match). True if removed."""
    existing = read_memory(bot_id, project, root, tail=0, shared=shared)
    lines = existing.splitlines() if existing else []
    target = (line or "").strip()
    if target not in lines:
        return False
    lines.remove(target)
    path = memory_path(bot_id, project, root, shared)
    _atomic_write(path, ("\n".join(lines) + "\n") if lines else "")
    return True
