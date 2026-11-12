"""osctl.py — the single mutation entry point for the Leo Bot AI agent.

Wraps dashboard/fileops.py mutations as a stdlib argparse CLI. Each subcommand
validates input, calls fileops (which writes authored files and reindexes), and
prints exactly one JSON line. The chat agent is allowed to mutate state ONLY
through this CLI, so the authored-files-are-truth invariant cannot be bypassed.
"""
import argparse
import fnmatch
import functools
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # repo root
from dashboard import chat_session, db, fileops  # noqa: E402
from core import bots, jobs, memory, paths  # noqa: E402
from core.ids import (  # noqa: E402
    bare_slug,
    build_catalog,
    build_id_registry,
    build_project_sections,
    catalog_as_text,
    describe_id,
    parse_id,
    resolve_section,
)


# --------------------------------------------------------------------------- #
# Workspace isolation. The chat server sets LEOBOT_WORKSPACE for every bot turn:
# "hq" (the holding's CEO office: sees everything) or a project slug (one
# company: its bots only touch that project). The calendar is the one shared
# thing, and other workspaces show there only as busy time.
# --------------------------------------------------------------------------- #
def _workspace():
    ws = (os.environ.get("LEOBOT_WORKSPACE") or "").strip()
    return ws if ws and ws != "hq" else None


_BLOCKED_IN_WORKSPACE = {"create-project"}
# A consulted teammate (LEOBOT_READONLY) may only look things up; the lead saves.
_READ_ONLY_CMDS = {"tree", "get-project", "get-posts", "read-file", "get-id-catalog",
                   "resolve-id", "get-memory", "recall", "get-busy", "get-subsections",
                   "get-brief-spec", "get-voice", "research-signal"}
# Commands that send text off the machine: off in a thread that read an app,
# like WebSearch (chat_session.APPS_USED_ENV).
_NETWORK_CMDS = {"research-signal"}
# Repo areas that hold every workspace's data at once (calendar via get-busy),
# or the owner's own machine state (session notes, indexes, logs).
_PRIVATE_PREFIXES = ("chats/", "team/", "portfolio/", "private/", "database/", "os.db", ".remember/",
                     ".superpowers/", ".tokensave/", ".claude/", ".git/", "dashboard/vendor/")
_PRIVATE_SUFFIXES = (".db", ".db-wal", ".db-shm", ".db-journal", ".sqlite", ".sqlite3", ".log")
# A shared thread (os_relay: an invited person can see it). It reads the team memory,
# never the private memory of the owner's DMs, and the owner alone writes either from
# outside it; files read only from this workspace's folder and the public repo docs.
_SHARED_BLOCKED = {"remember", "research-signal",  # research-signal sends text off the machine
                   # Scheduled jobs: the owner's own threads only, never a job turn (read-only).
                   "create-job", "update-job", "delete-job", "list-jobs"}
# A thread invited people can see runs an ALLOWLIST: a new command is refused there until
# someone adds it here (tests/test_case_variants.py fails on any command in neither set).
_SHARED_ALLOWED = frozenset({
    "add-feature", "add-post", "add-slide", "add-subsection", "create-activity",
    "create-brief-spec", "create-channel", "create-experiment", "create-intake", "create-memo",
    "create-milestone", "create-phase", "create-product", "create-profile", "create-project",
    "create-strategy", "create-technical", "create-voice", "delete-brief-spec",
    "delete-experiment", "delete-feature", "delete-memo", "delete-phase", "delete-strategy",
    "delete-voice", "generate-brief", "generate-plan", "get-brief-spec", "get-busy",
    "get-id-catalog", "get-memory", "get-posts", "get-project", "get-subsections", "get-voice",
    "mark-done", "read-file", "recall", "resolve-id", "revise-post", "set-status", "tree", "update-brief",
    "update-brief-spec", "update-channel", "update-experiment", "update-feature",
    "update-intake", "update-memo", "update-milestone", "update-phase", "update-post",
    "update-profile", "update-project", "update-roadmap", "update-strategy",
    "update-subsections", "update-technical", "update-validation-tab", "update-voice",
})
_SHARED_READ_PREFIXES = ("skills/", "docs/", "templates/", "prompts/", "bots/")
# Those folders ship with the code (paths.CODE), not the data folder: read-file finds them there.
_CODE_READ_DIRS = tuple(p.rstrip("/") for p in _SHARED_READ_PREFIXES)


def _code_file(path):
    """The file `path` names inside one of the public code folders (docs/, skills/, ...), when
    it exists there, else None (read-file then looks in the data folder)."""
    code = paths.CODE.resolve()
    try:
        target = (code / path).resolve()
    except (OSError, RuntimeError, ValueError):
        return None
    if any(target.is_relative_to(code / d) for d in _CODE_READ_DIRS) and target.is_file():
        return target
    return None
# Args whose value names an existing entity (create commands mint new slugs,
# which resolve to no owner and pass).
_OWNER_ARGS = ("project", "entity", "profile", "product", "slug", "id", "channel")


@functools.cache
def _owners():
    """slug/id → owning project, for every entity in the index. Built once per
    command (main clears it), however many checks ask."""
    tree, posts = db.tree(), db.posts()
    own = {}  # keys lowercased: _guard looks them up lowercased, and a slug may hold capitals
    for proj in tree:
        own[proj["slug"].lower()] = proj["slug"]
        for prof in proj.get("profiles", []):
            own[prof["slug"].lower()] = proj["slug"]
            for ch in prof.get("channels", []):
                own[ch["slug"].lower()] = proj["slug"]
        for prod in proj.get("products", []):
            own[prod["slug"].lower()] = proj["slug"]
    # The folders themselves, so a project the index has not caught up with is walled too.
    projects = fileops.ROOT / "projects"
    for proj in (projects.iterdir() if projects.is_dir() else ()):
        if proj.is_dir() and not proj.name.startswith("."):
            own.setdefault(proj.name.lower(), proj.name)
            for sub in ("products/*", "profiles/*", "profiles/*/channels/*"):
                for d in proj.glob(sub):
                    if d.is_dir():
                        own.setdefault(d.name.lower(), proj.name)
    for post in posts:
        owner = own.get(str(post.get("profile_slug") or "").lower())
        if owner:
            own.setdefault(str(post["id"]).lower(), owner)  # an id never remaps a slug
    for ms in db._rows("SELECT id, entity_slug FROM milestones"):
        if ms.get("id"):
            # A milestone with no workspace entity (HQ's, or 'external') is HQ's: never a workspace's.
            own[str(ms["id"]).lower()] = own.get(str(ms.get("entity_slug") or "").lower(), bots.HQ)
    try:
        reg = build_id_registry(tree, posts, root=fileops.ROOT)
        for entry in reg.entries:
            if entry.get("kind") == "project" and (entry.get("ref") or {}).get("project"):
                own[str(entry["id"]).lower()] = entry["ref"]["project"]
    except Exception:  # noqa: BLE001 — ids are a convenience, slugs still guard
        pass
    return own


def _guard(args, ws):
    """Refuse anything that reaches outside workspace `ws`."""
    if args.cmd in _BLOCKED_IN_WORKSPACE:
        raise fileops.ActionError(f"{args.cmd} is only available in HQ")
    if args.cmd in ("remember", "get-memory", "create-job") and not args.project:
        args.project = ws  # a company's bot keeps memory in its company, never in HQ
    if args.cmd in ("create-milestone", "mark-done") and not args.entity:
        args.entity = ws  # same for its milestones and activities
    entity = getattr(args, "entity", None)
    if isinstance(entity, str) and entity.strip().lower() in ("external", bots.HQ.lower()) \
            and entity.strip().lower() != ws.lower():
        raise fileops.ActionError("HQ's calendar entries are not writable from a workspace")
    own = _owners()
    for name, value in vars(args).items():
        if not (name in _OWNER_ARGS or name.endswith("_id")):
            continue
        if not isinstance(value, str) or not value:
            continue
        key = value.strip().lower()
        owner = own.get(key) or own.get(key.split(".", 1)[0])
        if owner and owner.lower() != ws.lower():
            raise fileops.ActionError(f"'{value}' belongs to another workspace")
    path = getattr(args, "path", None)
    if isinstance(path, str) and path and _code_file(path) is None:  # public code docs: no workspace data
        # Resolve the way read-file does, then compare lowercased: absolute
        # paths, "..", symlinks and a case-insensitive disk all land here.
        root = fileops.ROOT.resolve()
        try:
            rel = (root / path).resolve().relative_to(root).as_posix().lower()
        except ValueError:
            raise fileops.ActionError("that file is outside this workspace") from None
        if rel.startswith(_PRIVATE_PREFIXES) or rel.endswith(_PRIVATE_SUFFIXES):
            raise fileops.ActionError("that file holds every workspace's data; not readable here")
        parts = rel.split("/")
        if parts[0] == "projects" and len(parts) > 1 and parts[1] != ws.lower():
            raise fileops.ActionError("that file belongs to another workspace")
        if os.environ.get("LEOBOT_SHARED") and not (
                rel.startswith(f"projects/{ws.lower()}/") or rel.startswith(_SHARED_READ_PREFIXES)):
            raise fileops.ActionError("in a shared thread only this workspace's files are readable")
        if os.environ.get("LEOBOT_SHARED") and rel.startswith(f"projects/{ws.lower()}/team/"):
            raise fileops.ActionError("bot memory is not readable as a file here")


_FOLDER_KEYS = ("local_folder", "folders", "folder_paths")


def _no_folder_paths(value):
    """A shared thread never learns where the owner keeps linked folders on this machine."""
    if isinstance(value, dict):
        return {k: _no_folder_paths(v) for k, v in value.items() if k not in _FOLDER_KEYS}
    if isinstance(value, list):
        return [_no_folder_paths(v) for v in value]
    return value


def _emit(obj, ok=True):
    if os.environ.get("LEOBOT_SHARED"):
        obj = _no_folder_paths(obj)
    print(json.dumps({"ok": ok, **obj}, ensure_ascii=False))
    return 0 if ok else 1


def _fields(args, keys):
    """Collect provided (non-None) attrs into a fileops fields dict."""
    return {k: getattr(args, k) for k in keys if getattr(args, k) is not None}


def _build_parser():
    parser = argparse.ArgumentParser(prog="osctl", description="Leo Bot mutation CLI")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("tree", help="Print current project/profile/channel structure")
    def _tree(a):
        lines = []
        ws = _workspace()
        for proj in db.tree():
            if ws and proj["slug"] != ws:
                continue
            lines.append(f"{proj['slug']} ({proj.get('kind') or proj.get('type')})")
            for prof in proj.get("profiles", []):
                lines.append(f"  profile {prof['slug']} \"{prof['name']}\"")
                for ch in prof.get("channels", []):
                    lines.append(f"    channel {ch['slug']} ({ch.get('platform')})")
        return {"tree": "\n".join(lines) if lines else "(no projects yet)"}
    p.set_defaults(_run=_tree)

    p = sub.add_parser("create-project")
    p.add_argument("--slug", required=True)
    p.add_argument("--name")
    p.add_argument("--kind")
    p.add_argument("--priority")
    p.add_argument("--status")
    p.add_argument("--hours-per-week", dest="hours_per_week")
    p.add_argument("--local-folder", dest="local_folder")
    p.add_argument("--voice")
    p.set_defaults(_run=lambda a: fileops.create_project(
        a.slug, _fields(a, ["name", "kind", "priority", "status", "hours_per_week", "local_folder", "voice"])))

    p = sub.add_parser("create-profile")
    p.add_argument("--project", required=True)
    p.add_argument("--slug", required=True)
    p.add_argument("--name")
    p.add_argument("--topic")
    p.set_defaults(_run=lambda a: fileops.create_profile(
        a.project, a.slug, _fields(a, ["name", "topic"])))

    p = sub.add_parser("create-channel")
    p.add_argument("--profile", required=True)
    p.add_argument("--slug", required=True)
    p.add_argument("--platform", required=True)
    p.add_argument("--handle", default="")
    p.set_defaults(_run=lambda a: fileops.create_channel(
        a.profile, a.slug, a.platform, a.handle))

    p = sub.add_parser("create-intake", help="Scaffold strategy/intake.md for a project")
    p.add_argument("--project", required=True)
    p.set_defaults(_run=lambda a: fileops.create_intake(a.project))

    p = sub.add_parser("update-intake", help="Replace strategy/intake.md (--text or stdin)")
    p.add_argument("--project", required=True)
    p.add_argument("--text", default="")
    def _update_intake(a):
        text = a.text
        if not text.strip():
            text = sys.stdin.read()
        return fileops.write_intake(a.project, text)
    p.set_defaults(_run=_update_intake)

    p = sub.add_parser("create-technical", help="Scaffold technical.md for a project")
    p.add_argument("--project", required=True)
    p.set_defaults(_run=lambda a: fileops.create_technical(a.project))

    p = sub.add_parser("update-technical", help="Replace technical.md (--text or stdin)")
    p.add_argument("--project", required=True)
    p.add_argument("--text", default="")
    def _update_technical(a):
        text = a.text
        if not text.strip():
            text = sys.stdin.read()
        return fileops.write_technical(a.project, text)
    p.set_defaults(_run=_update_technical)

    p = sub.add_parser("get-subsections", help="Per-project tab subsection config")
    p.add_argument("--project", required=True)
    def _get_subsections(a):
        return {"project": a.project, "subsections": fileops.read_subsections(a.project)}
    p.set_defaults(_run=_get_subsections)

    p = sub.add_parser("update-subsections",
                       help="Replace subsection list for intake, technical, or roadmap doc")
    p.add_argument("--project", required=True)
    p.add_argument("--doc", required=True, choices=["intake", "technical", "roadmap"])
    p.add_argument("--subsections", required=True,
                   help="Comma- or newline-separated subsection titles")
    def _update_subsections(a):
        titles = fileops.parse_subsections_arg(a.subsections)
        if not titles:
            raise fileops.ActionError("at least one subsection title required")
        return fileops.update_subsections(a.project, a.doc, titles)
    p.set_defaults(_run=_update_subsections)

    p = sub.add_parser("add-subsection", help="Append one subsection to a project doc config")
    p.add_argument("--project", required=True)
    p.add_argument("--doc", required=True, choices=["intake", "technical", "roadmap"])
    p.add_argument("--title", required=True)
    p.set_defaults(_run=lambda a: fileops.add_subsection(a.project, a.doc, a.title))

    p = sub.add_parser("update-validation-tab",
                       help="Set which intake subsections show on Problem & validation tab")
    p.add_argument("--project", required=True)
    p.add_argument("--subsections", required=True,
                   help="Comma- or newline-separated titles (must exist in intake list)")
    def _update_validation_tab(a):
        titles = fileops.parse_subsections_arg(a.subsections)
        if not titles:
            raise fileops.ActionError("at least one subsection title required")
        return fileops.update_validation_tab(a.project, titles)
    p.set_defaults(_run=_update_validation_tab)

    p = sub.add_parser("create-memo", help="Create next versioned strategy memo JSON")
    p.add_argument("--project", required=True)
    p.add_argument("--type", required=True, dest="memo_type")
    p.add_argument("--summary")
    p.add_argument("--recommendation")
    p.add_argument("--problem-statement", dest="problem_statement")
    p.add_argument("--body-json", dest="body_json", default="",
                   help="Extra memo fields as JSON (--body-json or stdin when --text empty)")
    def _create_memo(a):
        fields = _fields(a, ["summary", "recommendation", "problem_statement"])
        body_extra = None
        raw = (a.body_json or "").strip()
        if raw:
            body_extra = json.loads(raw)
        return fileops.create_memo(a.project, a.memo_type, fields, body_extra=body_extra)
    p.set_defaults(_run=_create_memo)

    p = sub.add_parser("remember", help="Append one line to a bot's memory (per project, or HQ)")
    p.add_argument("--bot", required=True)
    p.add_argument("--project", help="project slug; omit for HQ memory")
    p.add_argument("--text", required=True)
    p.add_argument("--type", choices=memory.TYPES, default="fact",
                   help="decision and preference are pinned: always on the bot's card, never dropped")
    p.set_defaults(_run=lambda a: {"path": memory.remember(a.bot, a.text, a.project, root=bots.ROOT,
                                                           type=a.type)})

    p = sub.add_parser("get-memory", help="Read a bot's memory (per project, or HQ)")
    p.add_argument("--bot", required=True)
    p.add_argument("--project")
    p.set_defaults(_run=lambda a: {"memory": bots.read_memory(a.bot, a.project, tail=0,
                                                              shared=bool(os.environ.get("LEOBOT_SHARED")))})

    p = sub.add_parser("recall", help="Look up older entries in a bot's memory (this workspace and "
                                      "thread's scope only)")
    p.add_argument("--bot", required=True)
    p.add_argument("--query", required=True)
    p.add_argument("--limit", type=int, default=8)

    def _recall(a):
        # No --project: the server sets the workspace and scope, so a shared thread
        # reaches only the team memory and one company never reaches another's.
        if a.bot not in bots.load_defs():
            raise fileops.ActionError(f"unknown bot: {a.bot}")
        return {"entries": memory.recall(a.bot, a.query, _workspace(), root=bots.ROOT,
                                         shared=bool(os.environ.get("LEOBOT_SHARED")), limit=a.limit)}
    p.set_defaults(_run=_recall)

    # -- scheduled jobs (core/jobs.py): the owner's threads only ------------ #
    def _job_target(ws, bot, skill):
        """Refuse a job its workspace, bot, skill or engine could not run."""
        project = None
        if ws != bots.HQ:
            project = next((p for p in db.tree() if p.get("slug") == ws), None)
            if project is None:
                raise fileops.ActionError(f"no workspace {ws}")
        team = bots.team(bots.ROOT, ws, project)
        b = team.get(bot)
        if not b or not b.active:
            raise fileops.ActionError(f"@{bot} is not an active bot in this workspace")
        if skill not in team.skills_of(bot):
            raise fileops.ActionError(f"/{skill} is not one of @{bot}'s skills")
        from dashboard.engines import registry
        from dashboard.engines.base import EngineRefused
        try:
            engine = registry.pick(bots.ROOT, ws, bot)
        except EngineRefused as e:
            raise fileops.ActionError(str(e)) from None
        if not registry.metered(engine):
            raise fileops.ActionError(f"Jobs need an engine that reports cost; {engine.label} does not. "
                                      "Pick another engine for this bot in Settings, Engines.")

    def _own_job(job_id):
        job = jobs.get(bots.ROOT, job_id)
        ws = _workspace()
        if job is None or (ws and job.get("workspace") != ws):
            raise fileops.ActionError(f"no job {job_id}")
        return job

    def _job_out(job):
        try:
            due = jobs.next_due(job) if job.get("enabled") else None
        except ValueError:
            due = None
        return {**job, "next_due": due}

    p = sub.add_parser("create-job", help="Schedule a bot's skill (read-only runs, writes wait for approval)")
    p.add_argument("--bot", required=True)
    p.add_argument("--skill", required=True)
    p.add_argument("--name", required=True)
    p.add_argument("--message", required=True)
    p.add_argument("--schedule", required=True, help='"every 6h", "daily 09:00" or "mon,thu 09:00"')
    p.add_argument("--cap", required=True, help="dollars per 24 hours for this job, above 0")
    p.add_argument("--project", help="workspace slug; omit for HQ")

    def _create_job(a):
        ws = a.project or bots.HQ
        _job_target(ws, a.bot, a.skill)
        try:
            return {"job": _job_out(jobs.create(bots.ROOT, ws=ws, bot=a.bot, skill=a.skill, name=a.name,
                                                message=a.message, schedule=a.schedule, cap=a.cap))}
        except ValueError as e:
            raise fileops.ActionError(str(e)) from None
    p.set_defaults(_run=_create_job)

    p = sub.add_parser("update-job", help="Change a scheduled job")
    p.add_argument("--job", required=True)
    p.add_argument("--name")
    p.add_argument("--message")
    p.add_argument("--schedule")
    p.add_argument("--cap")
    p.add_argument("--skill")
    p.add_argument("--enabled", choices=("true", "false"))

    def _update_job(a):
        job = _own_job(a.job)
        if a.skill or a.enabled == "true":
            _job_target(job["workspace"], job["bot"], a.skill or job["skill"])
        fields = {"name": a.name, "message": a.message, "schedule": a.schedule, "cap": a.cap,
                  "skill": a.skill, "enabled": None if a.enabled is None else a.enabled == "true"}
        try:
            return {"job": _job_out(jobs.update(bots.ROOT, job["id"], **fields))}
        except (ValueError, LookupError) as e:
            raise fileops.ActionError(str(e)) from None
    p.set_defaults(_run=_update_job)

    p = sub.add_parser("delete-job", help="Remove a scheduled job (its threads stay)")
    p.add_argument("--job", required=True)
    p.set_defaults(_run=lambda a: {"deleted": jobs.delete(bots.ROOT, _own_job(a.job)["id"])})

    p = sub.add_parser("list-jobs", help="Scheduled jobs (this workspace's, or every one from HQ)")
    p.set_defaults(_run=lambda a: {"jobs": [_job_out(j) for j in jobs.load(bots.ROOT)
                                            if not _workspace() or j.get("workspace") == _workspace()]})

    p = sub.add_parser("create-experiment", help="Create strategy/experiments/<stem>.json")
    p.add_argument("--project", required=True)
    p.add_argument("--assumption", required=True)
    p.add_argument("--stem")
    p.add_argument("--success-criteria", dest="success_criteria")
    p.add_argument("--kill-criteria", dest="kill_criteria")
    p.add_argument("--strategy", help="GTM strategy stem this experiment validates (sec07 lens)")
    p.set_defaults(_run=lambda a: fileops.create_experiment(
        a.project, _fields(a, ["assumption", "stem", "success_criteria", "kill_criteria", "strategy"])))

    p = sub.add_parser("update-experiment", help="Patch an existing experiment JSON")
    p.add_argument("--project", required=True)
    p.add_argument("--stem", required=True)
    p.add_argument("--assumption")
    p.add_argument("--success-criteria", dest="success_criteria")
    p.add_argument("--kill-criteria", dest="kill_criteria")
    p.add_argument("--strategy", help="GTM strategy stem this experiment validates (sec07 lens)")
    p.set_defaults(_run=lambda a: fileops.update_experiment(
        a.project, a.stem, _fields(a, ["assumption", "success_criteria", "kill_criteria", "strategy"])))

    p = sub.add_parser("update-memo", help="Patch an existing memo JSON (same version, in place)")
    p.add_argument("--project", required=True)
    p.add_argument("--type", required=True, dest="memo_type")
    p.add_argument("--version", required=True, type=int)
    p.add_argument("--summary")
    p.add_argument("--recommendation")
    p.add_argument("--problem-statement", dest="problem_statement")
    p.add_argument("--body-json", dest="body_json", default="",
                   help="Extra memo fields as JSON (--body-json or stdin when --text empty)")
    def _update_memo(a):
        fields = _fields(a, ["summary", "recommendation", "problem_statement"])
        raw = (a.body_json or "").strip()
        if raw:
            fields.update(json.loads(raw))
        return fileops.update_memo(a.project, a.memo_type, a.version, fields)
    p.set_defaults(_run=_update_memo)

    p = sub.add_parser("delete-memo", help="Delete one memo version JSON")
    p.add_argument("--project", required=True)
    p.add_argument("--type", required=True, dest="memo_type")
    p.add_argument("--version", required=True, type=int)
    p.set_defaults(_run=lambda a: fileops.delete_memo(a.project, a.memo_type, a.version))

    p = sub.add_parser("delete-experiment", help="Delete strategy/experiments/<stem>.json")
    p.add_argument("--project", required=True)
    p.add_argument("--stem", required=True)
    p.set_defaults(_run=lambda a: fileops.delete_experiment(a.project, a.stem))

    # GTM (sec07): phases (time-boxed bets) + strategies (channel-motions)
    p = sub.add_parser("create-phase", help="Create a GTM phase (time-boxed bet)")
    p.add_argument("--project", required=True)
    p.add_argument("--name", required=True)
    p.add_argument("--goal", default="")
    p.add_argument("--timebox", default="")
    p.add_argument("--status", default="planned")
    p.set_defaults(_run=lambda a: fileops.create_phase(
        a.project, a.name, a.goal, a.timebox, a.status))

    p = sub.add_parser("update-phase", help="Patch a GTM phase by id")
    p.add_argument("--project", required=True)
    p.add_argument("--id", required=True, dest="phase_id")
    p.add_argument("--name")
    p.add_argument("--goal")
    p.add_argument("--timebox")
    p.add_argument("--status")
    p.set_defaults(_run=lambda a: fileops.update_phase(
        a.project, a.phase_id, _fields(a, ["name", "goal", "timebox", "status"])))

    p = sub.add_parser("delete-phase", help="Delete a GTM phase by id")
    p.add_argument("--project", required=True)
    p.add_argument("--id", required=True, dest="phase_id")
    p.set_defaults(_run=lambda a: fileops.delete_phase(a.project, a.phase_id))

    p = sub.add_parser("create-strategy", help="Create a GTM strategy (channel-motion) under a phase")
    p.add_argument("--project", required=True)
    p.add_argument("--phase", default="", help="phase id this strategy sits under")
    p.add_argument("--channel", default="", help="channel slug this motion targets")
    p.add_argument("--hypothesis", default="")
    p.add_argument("--name")
    p.add_argument("--primary", action="store_true")
    p.set_defaults(_run=lambda a: fileops.create_strategy(
        a.project, a.phase, a.channel, a.hypothesis, a.name or "", a.primary))

    p = sub.add_parser("update-strategy", help="Patch a GTM strategy by stem")
    p.add_argument("--project", required=True)
    p.add_argument("--stem", required=True)
    p.add_argument("--name")
    p.add_argument("--phase")
    p.add_argument("--channel")
    p.add_argument("--hypothesis")
    p.add_argument("--status")
    p.add_argument("--notes")
    p.add_argument("--primary", action="store_true", default=None)
    p.set_defaults(_run=lambda a: fileops.update_strategy(
        a.project, a.stem,
        _fields(a, ["name", "phase", "channel", "hypothesis", "status", "notes", "primary"])))

    p = sub.add_parser("delete-strategy", help="Delete a GTM strategy by stem")
    p.add_argument("--project", required=True)
    p.add_argument("--stem", required=True)
    p.set_defaults(_run=lambda a: fileops.delete_strategy(a.project, a.stem))

    p = sub.add_parser("create-product", help="Scaffold products/<slug>/ under a project")
    p.add_argument("--project", required=True)
    p.add_argument("--slug", required=True)
    p.add_argument("--name")
    p.add_argument("--type")
    p.add_argument("--status")
    p.set_defaults(_run=lambda a: fileops.create_product(
        a.project, a.slug, _fields(a, ["name", "type", "status"])))

    p = sub.add_parser("add-feature", help="Append a roadmap checklist item")
    p.add_argument("--product", required=True)
    p.add_argument("--title", required=True)
    p.add_argument("--section", default="Next")
    p.add_argument("--why", default=None, help="One-line description after title")
    p.add_argument("--priority", default=None, choices=["critical", "high", "normal", "low"])
    p.set_defaults(_run=lambda a: fileops.add_feature(
        a.product, _fields(a, ["title", "section", "why", "priority"])))

    p = sub.add_parser("update-feature", help="Patch one roadmap checklist line")
    p.add_argument("--product", required=True)
    p.add_argument("--id", required=True, dest="feature_id")
    p.add_argument("--title")
    p.add_argument("--why")
    p.add_argument("--section")
    p.add_argument("--priority")
    p.set_defaults(_run=lambda a: fileops.update_feature(
        a.product, a.feature_id, _fields(a, ["title", "why", "section", "priority"])))

    p = sub.add_parser("delete-feature", help="Remove one roadmap checklist line")
    p.add_argument("--product", required=True)
    p.add_argument("--id", required=True, dest="feature_id")
    p.set_defaults(_run=lambda a: fileops.delete_feature(a.product, a.feature_id))

    p = sub.add_parser("update-roadmap", help="Replace products/<slug>/roadmap.md (--text or stdin)")
    p.add_argument("--product", required=True)
    p.add_argument("--text", default="")
    def _update_roadmap(a):
        text = a.text
        if not text.strip():
            text = sys.stdin.read()
        return fileops.write_roadmap(a.product, text)
    p.set_defaults(_run=_update_roadmap)

    # add-post fields map to fileops._POST_FIELDS (date, pillar, working_title)
    # + channels; hook/angle are not real post fields, so they are not exposed.
    p = sub.add_parser("add-post")
    p.add_argument("--profile", required=True)
    p.add_argument("--working-title", dest="working_title")
    p.add_argument("--pillar")
    p.add_argument("--date")
    p.add_argument("--channels")
    p.add_argument("--status", choices=["planned", "published"],
                   help="published logs an already-posted item (requires --date)")
    p.set_defaults(_run=lambda a: fileops.add_post(
        a.profile, _fields(a, ["working_title", "pillar", "date", "channels", "status"])))

    # --title is intentionally NOT argparse-required: fileops.create_activity
    # validates it and returns a JSON error, keeping validation in one place.
    p = sub.add_parser("create-activity")
    p.add_argument("--entity", required=True)
    p.add_argument("--title")
    p.add_argument("--date")
    p.add_argument("--date-end", dest="date_end")
    p.add_argument("--time", help="start HH:MM (24h); timed activities may not overlap")
    p.add_argument("--time-end", dest="time_end", help="end HH:MM (default: start + 1h)")
    p.add_argument("--type")
    p.add_argument("--priority")
    p.set_defaults(_run=lambda a: fileops.create_activity(
        _fields(a, ["entity", "title", "date", "date_end", "time", "time_end", "type", "priority"])))

    p = sub.add_parser("get-busy", help="Booked times across ALL workspaces (the CEO has one calendar)")
    p.add_argument("--from", dest="date_from", required=True, help="YYYY-MM-DD")
    p.add_argument("--to", dest="date_to", help="YYYY-MM-DD (default: same day)")
    def _get_busy(a):
        ws, end = _workspace(), a.date_to or a.date_from
        own = _owners() if ws else {}
        slots = []
        for act in fileops.timed_activities():
            if not (a.date_from <= act["date"] <= end):
                continue
            mine = not ws or act["entity"] == ws or own.get(str(act["entity"]).lower()) == ws
            slots.append({"date": act["date"], "time": act["time"], "time_end": act["time_end"],
                          "title": act["title"] if mine else "busy (another workspace)",
                          "entity": act["entity"] if mine else None})
        return {"busy": sorted(slots, key=lambda x: (x["date"], x["time"]))}
    p.set_defaults(_run=_get_busy)

    p = sub.add_parser("create-milestone")
    p.add_argument("--title", required=True)
    p.add_argument("--date", required=True)
    p.add_argument("--entity")
    p.add_argument("--type")
    p.add_argument("--entity-type", dest="entity_type")
    p.add_argument("--date-end", dest="date_end")
    p.add_argument("--notes")
    p.add_argument("--priority")
    p.set_defaults(_run=lambda a: fileops.create_milestone(
        _fields(a, ["title", "date", "entity", "type", "entity_type",
                    "date_end", "notes", "priority"])))

    p = sub.add_parser("mark-done")
    p.add_argument("--title", required=True)
    p.add_argument("--entity", required=True)
    p.set_defaults(_run=lambda a: fileops.mark_activity_done(a.title, a.entity))

    p = sub.add_parser("update-post")
    p.add_argument("--id", required=True, dest="id")
    p.add_argument("--working-title", dest="working_title")
    p.add_argument("--pillar")
    p.add_argument("--date")
    p.add_argument("--channels")
    p.add_argument("--format")
    p.add_argument("--objective")
    p.add_argument("--platform")
    p.set_defaults(_run=lambda a: fileops.update_post(
        a.id, _fields(a, ["working_title", "pillar", "date", "channels", "format", "objective", "platform"])))

    p = sub.add_parser("set-status")
    p.add_argument("--id", required=True, dest="id")
    p.add_argument("--status", required=True)
    p.set_defaults(_run=lambda a: fileops.set_status(a.id, a.status))

    # edit (rename) commands — the slug stays fixed; metadata/name are editable.

    p = sub.add_parser("update-profile")
    p.add_argument("--slug", required=True)
    p.add_argument("--name")
    p.add_argument("--topic")
    p.set_defaults(_run=lambda a: fileops.update_profile(
        a.slug, _fields(a, ["name", "topic"])))

    p = sub.add_parser("get-brief-spec",
                       help="Read one profile brief-spec (default br1), or list all with --list")
    p.add_argument("--profile", required=True)
    p.add_argument("--id", default="br1", dest="brief_id")
    p.add_argument("--list", action="store_true")
    p.set_defaults(_run=lambda a: (
        {"profile": a.profile, "specs": fileops.list_brief_specs(a.profile)}
        if a.list else fileops.get_brief_spec(a.profile, a.brief_id)
    ))

    p = sub.add_parser("update-brief-spec",
                       help="Replace one profile brief-spec (default br1)")
    p.add_argument("--profile", required=True)
    p.add_argument("--id", default="br1", dest="brief_id")
    p.add_argument("--platforms")
    p.add_argument("--text", default="")
    def _update_brief_spec(a):
        text = a.text
        if not text.strip():
            text = sys.stdin.read()
        if not text.strip():
            raise fileops.ActionError("brief spec text required (--text or stdin)")
        return fileops.write_brief_spec(a.profile, text, a.brief_id, a.platforms)
    p.set_defaults(_run=_update_brief_spec)

    p = sub.add_parser("create-brief-spec",
                       help="Add a new brief-spec to a profile (mints the next br id)")
    p.add_argument("--profile", required=True)
    p.add_argument("--platforms", default="all")
    p.add_argument("--text", default="")
    def _create_brief_spec(a):
        text = a.text
        if not text.strip():
            text = sys.stdin.read()
        if not text.strip():
            raise fileops.ActionError("brief spec text required (--text or stdin)")
        return fileops.create_brief_spec(a.profile, text, a.platforms)
    p.set_defaults(_run=_create_brief_spec)

    p = sub.add_parser("delete-brief-spec", help="Delete a profile brief-spec by id")
    p.add_argument("--profile", required=True)
    p.add_argument("--id", required=True, dest="brief_id")
    p.set_defaults(_run=lambda a: fileops.delete_brief_spec(a.profile, a.brief_id))

    p = sub.add_parser("get-voice",
                       help="Read one profile voice (default vc1), or list all with --list")
    p.add_argument("--profile", required=True)
    p.add_argument("--id", default="vc1", dest="voice_id")
    p.add_argument("--list", action="store_true")
    p.set_defaults(_run=lambda a: (
        {"profile": a.profile, "voices": fileops.list_voices(a.profile)}
        if a.list else fileops.get_voice(a.profile, a.voice_id)
    ))

    p = sub.add_parser("update-voice", help="Replace one profile voice (default vc1)")
    p.add_argument("--profile", required=True)
    p.add_argument("--id", default="vc1", dest="voice_id")
    p.add_argument("--platforms")
    p.add_argument("--text", default="")
    def _update_voice(a):
        text = a.text
        if not text.strip():
            text = sys.stdin.read()
        if not text.strip():
            raise fileops.ActionError("voice text required (--text or stdin)")
        return fileops.update_voice(a.profile, text, a.voice_id, a.platforms)
    p.set_defaults(_run=_update_voice)

    p = sub.add_parser("create-voice", help="Add a new voice to a profile (mints the next vc id)")
    p.add_argument("--profile", required=True)
    p.add_argument("--platforms", default="all")
    p.add_argument("--text", default="")
    def _create_voice(a):
        text = a.text
        if not text.strip():
            text = sys.stdin.read()
        if not text.strip():
            raise fileops.ActionError("voice text required (--text or stdin)")
        return fileops.create_voice(a.profile, text, a.platforms)
    p.set_defaults(_run=_create_voice)

    p = sub.add_parser("delete-voice", help="Delete a profile voice by id")
    p.add_argument("--profile", required=True)
    p.add_argument("--id", required=True, dest="voice_id")
    p.set_defaults(_run=lambda a: fileops.delete_voice(a.profile, a.voice_id))

    p = sub.add_parser("update-project")
    p.add_argument("--slug", required=True)
    p.add_argument("--name")
    p.add_argument("--kind")
    p.add_argument("--priority")
    p.add_argument("--status")
    p.add_argument("--hours-per-week", dest="hours_per_week")
    p.add_argument("--local-folder", dest="local_folder",
                   help="absolute directory path to link (edit access); unlink on the Folders screen")
    p.set_defaults(_run=lambda a: fileops.update_project(
        a.slug, _fields(a, ["name", "kind", "priority", "status", "hours_per_week", "local_folder"])))

    p = sub.add_parser("update-channel")
    p.add_argument("--slug", required=True)
    p.add_argument("--platform")
    p.add_argument("--handle")
    p.add_argument("--name")
    p.add_argument("--bio")
    p.set_defaults(_run=lambda a: fileops.update_channel(
        a.slug, _fields(a, ["platform", "handle", "name", "bio"])))

    p = sub.add_parser("update-milestone")
    p.add_argument("--id", required=True, dest="id")
    p.add_argument("--title")
    p.add_argument("--date")
    p.add_argument("--date-end", dest="date_end")
    p.add_argument("--type")
    p.add_argument("--entity")
    p.add_argument("--entity-type", dest="entity_type")
    p.add_argument("--notes")
    p.add_argument("--priority")
    p.set_defaults(_run=lambda a: fileops.update_milestone(a.id, _fields(
        a, ["title", "date", "date_end", "type", "entity", "entity_type", "notes", "priority"])))

    # Content generation — same generate.py jobs the dashboard buttons run.
    p = sub.add_parser("generate-brief",
                       help="Run the brief job for a planned/approved slot (same as Write button)")
    p.add_argument("--id", required=True, dest="id")
    p.add_argument("--instruction", default="")
    p.add_argument("--spec", dest="brief_id", default=None,
                   help="brief-spec id to use, e.g. br2 (default: post's stored id, else br1)")
    p.add_argument("--voice", dest="voice_id", default=None,
                   help="voice id to use, e.g. vc2 (default: post's stored id, else vc1)")
    p.set_defaults(_run=lambda a: fileops.generate_brief(
        a.id, a.instruction or None, brief_id=a.brief_id, voice_id=a.voice_id))

    p = sub.add_parser("update-brief",
                       help="Create or change a brief from natural language (primary chat path)")
    p.add_argument("--id", required=True, dest="id")
    p.add_argument("--instruction", default="")
    p.add_argument("--spec", dest="brief_id", default=None,
                   help="brief-spec id to use, e.g. br2 (default: post's stored id, else br1)")
    p.add_argument("--voice", dest="voice_id", default=None,
                   help="voice id to use, e.g. vc2 (default: post's stored id, else vc1)")
    p.set_defaults(_run=lambda a: fileops.update_brief(
        a.id, a.instruction or None, brief_id=a.brief_id, voice_id=a.voice_id))

    p = sub.add_parser("generate-plan",
                       help="Run the plan job for a profile (same as Generate ideas button)")
    p.add_argument("--profile", required=True)
    p.add_argument("--period", required=True)
    p.add_argument("--platforms", default="")
    p.add_argument("--cadence", default="")
    p.add_argument("--focus", default="")
    p.add_argument("--brief-counts", default="", dest="brief_counts",
                   help='e.g. "br1:5,br2:2" — only meaningful when the profile has >1 brief-spec')
    p.add_argument("--voice-counts", default="", dest="voice_counts",
                   help='e.g. "vc1:5,vc2:2" — only meaningful when the profile has >1 voice')
    p.add_argument("--dates", action="store_true",
                   help="assign a date to each post (default: leave unscheduled)")
    def _generate_plan(a):
        params = {"period": a.period}
        if a.platforms:
            params["platforms"] = a.platforms
        if a.cadence not in (None, ""):
            params["cadence"] = a.cadence
        if a.focus:
            params["focus"] = a.focus
        if a.brief_counts:
            params["brief_counts"] = a.brief_counts
        if a.voice_counts:
            params["voice_counts"] = a.voice_counts
        if a.dates:
            params["dates"] = True
        return fileops.run_plan(a.profile, params)
    p.set_defaults(_run=_generate_plan)

    p = sub.add_parser("add-slide", help="Append one slide_overlays row to a post brief")
    p.add_argument("--id", required=True, dest="post_id")
    p.add_argument("--overlay", required=True)
    p.set_defaults(_run=lambda a: fileops.add_slide_overlay(a.post_id, a.overlay))

    p = sub.add_parser("revise-post",
                       help="Revise a slot or draft via generate.py (same as Revise button)")
    p.add_argument("--id", required=True, dest="id")
    p.add_argument("--instruction", required=True)
    p.add_argument("--spec", dest="brief_id", default=None,
                   help="brief-spec id to use, e.g. br2 (default: post's stored id, else br1)")
    p.add_argument("--voice", dest="voice_id", default=None,
                   help="voice id to use, e.g. vc2 (default: post's stored id, else vc1)")
    p.set_defaults(_run=lambda a: fileops.revise_post(
        a.id, a.instruction, brief_id=a.brief_id, voice_id=a.voice_id))

    # --- read commands (no mutations) ---

    p = sub.add_parser("research-signal",
                       help="Real online-discourse signal for a topic via last30days "
                            "(zero-config sources, no API keys required)")
    p.add_argument("--query", required=True)
    p.add_argument("--max-clusters", type=int, default=5, dest="max_clusters")
    p.set_defaults(_run=lambda a: fileops.research_signal(a.query, max_clusters=a.max_clusters))

    p = sub.add_parser("get-posts", help="List posts, optionally filtered by profile")
    p.add_argument("--profile", default=None)
    def _get_posts(a):
        rows = db.profile_posts(a.profile) if a.profile else db.posts()
        ws = _workspace()
        if ws:
            own = _owners()
            rows = [r for r in rows if own.get(r.get("profile_slug")) == ws]
        return {"posts": rows}
    p.set_defaults(_run=_get_posts)

    p = sub.add_parser("get-project", help="Full project data: activities, memos, experiments, features")
    p.add_argument("--slug", required=True)
    def _get_project(a):
        data = db.project(a.slug)
        if data is None:
            raise fileops.ActionError(f"project '{a.slug}' not found")
        reg = build_id_registry(
            db.tree(), db.posts(), root=fileops.ROOT, features=data["features"])
        data["memos"] = fileops.enrich_project_memos(data["memos"], a.slug, reg)
        data["experiments"] = fileops.enrich_project_experiments(data["experiments"], a.slug, reg)
        data["features"] = fileops.enrich_project_features(data["features"], reg)
        data["sections"] = build_project_sections(a.slug, fileops.ROOT, data, registry=reg)
        from core.project_schemas import feature_form_fields

        data["subsections"] = fileops.read_subsections(a.slug)
        from core.ids import subsection_id_map
        data["subsection_ids"] = subsection_id_map(reg, a.slug)
        data["feature"] = feature_form_fields(data["subsections"]["docs"]["roadmap"])
        return {"project": data}
    p.set_defaults(_run=_get_project)

    p = sub.add_parser("read-file", help="Read an authored file by its path in the data folder "
                                         "(docs/, skills/, templates/, prompts/, bots/: the app's own)")
    p.add_argument("--path", required=True)
    def _read_file(a):
        data_root = fileops.ROOT
        target = _code_file(a.path) or (data_root / a.path).resolve()
        if not (target.is_relative_to(data_root.resolve()) or _code_file(a.path)):
            raise fileops.ActionError("path outside the data folder")
        # Same secret-file policy as the chat agent's Read deny rules.
        # Case-blind, as a Mac disk is: .AUTH-TOKEN opens .auth-token.
        name = target.name.lower()
        if any(fnmatch.fnmatchcase(name, g.lower()) for g in chat_session.SECRET_FILE_GLOBS):
            raise fileops.ActionError("secret files are not readable")
        if not target.exists():
            raise fileops.ActionError(f"file not found: {a.path}")
        return {"path": a.path, "content": target.read_text(encoding="utf-8")}
    p.set_defaults(_run=_read_file)

    p = sub.add_parser("get-id-catalog", help="List all canonical IDs (UI skeleton + live entities)")
    p.add_argument("--text", action="store_true", help="Human-readable summary instead of JSON entries")
    def _id_catalog(a):
        ws = _workspace()
        tree = [p for p in db.tree() if not ws or p["slug"] == ws]
        own = _owners() if ws else {}
        posts = [r for r in db.posts() if not ws or own.get(r.get("profile_slug")) == ws]
        entries = build_catalog(tree, root=fileops.ROOT, posts=posts)
        if a.text:
            return {"catalog": catalog_as_text(entries), "count": len(entries)}
        return {"entries": entries, "count": len(entries)}
    p.set_defaults(_run=_id_catalog)

    p = sub.add_parser("resolve-id", help="Describe a canonical ID")
    p.add_argument("--id", required=True, dest="id")
    def _resolve_id(a):
        reg = build_id_registry(db.tree(), db.posts(), root=fileops.ROOT)
        parsed = parse_id(a.id)
        if not parsed:
            raise fileops.ActionError(f"not a canonical id: {a.id}")
        ent = reg.resolve(a.id)
        out = {
            "id": a.id,
            "describe": describe_id(a.id, reg),
            "bare": bare_slug(a.id, reg),
            "parsed": parsed,
        }
        if ent:
            out["entry"] = ent
            ref = ent.get("ref") or {}
            if ent.get("kind") == "tab" and ref.get("project") and ref.get("section"):
                pdata = db.project(ref["project"])
                sec = resolve_section(
                    ref["project"], ref["section"], fileops.ROOT,
                    project_data=pdata, registry=reg,
                )
                if sec is not None:
                    out["section"] = sec
            if ent.get("kind") == "slot_field" and ref.get("post") and ref.get("field"):
                post_id, field = ref["post"], ref["field"]
                try:
                    detail = fileops.read_detail(post_id)
                    slot = detail.get("slot") or {}
                    out["field"] = {
                        "post": post_id,
                        "field": field,
                        "scope": "slot",
                        "value": slot.get(field) if field != "channels" else slot.get("channels"),
                    }
                except fileops.ActionError:
                    pass
            elif ent.get("kind") in ("field", "brief_field") and ref.get("post") and ref.get("field"):
                post_id, field = ref["post"], ref["field"]
                try:
                    detail = fileops.read_detail(post_id)
                    brief = detail.get("brief") or {}
                    path = None
                    if detail.get("profile_slug"):
                        prof_dir = fileops._profile_dir(detail["profile_slug"])
                        bf = prof_dir / "content" / "briefs" / f"{post_id}.json"
                        if bf.exists():
                            path = str(bf.relative_to(fileops.ROOT))
                    out["field"] = {
                        "post": post_id,
                        "field": field,
                        "scope": "brief",
                        "path": path,
                        "value": _brief_field_value(brief, field),
                    }
                except fileops.ActionError:
                    pass
            elif ent.get("kind") == "brief" and ref.get("post"):
                post_id = ref["post"]
                try:
                    detail = fileops.read_detail(post_id)
                    out["brief"] = {"post": post_id, "fields": list((detail.get("brief") or {}).keys())}
                except fileops.ActionError:
                    pass
        return out
    p.set_defaults(_run=_resolve_id)

    return parser


def _brief_field_value(brief: dict, field: str):
    if not brief or field.startswith("_"):
        return None
    if field.startswith("slide-"):
        try:
            n = int(field.split("-", 1)[1])
        except (IndexError, ValueError):
            return None
        for item in brief.get("slide_overlays") or []:
            if isinstance(item, dict) and int(item.get("slide") or 0) == n:
                return item.get("overlay")
        return None
    if field.startswith("gen-prompt-"):
        try:
            n = int(field.rsplit("-", 1)[1])
        except (IndexError, ValueError):
            return None
        prompts = brief.get("gen_prompts") or []
        return prompts[n - 1] if 0 < n <= len(prompts) else None
    return brief.get(field)


def main(argv=None):
    parser = _build_parser()
    args = parser.parse_args(argv)
    _owners.cache_clear()
    try:
        if os.environ.get("LEOBOT_READONLY") and args.cmd not in _READ_ONLY_CMDS:
            raise fileops.ActionError("read-only here: you can look things up but not save. Say what "
                                      "should be saved: a teammate saves it, or, in a scheduled job, end "
                                      "with one APPROVAL: line.")
        if os.environ.get("LEOBOT_WORKSPACE") and getattr(args, "local_folder", None) is not None:
            # A linked folder becomes --add-dir on that company's turns: only the
            # user links one, in the dashboard. Any bot (HQ too) is refused.
            raise fileops.ActionError("only you can link a local folder, from the dashboard")
        if os.environ.get(chat_session.APPS_USED_ENV) and args.cmd in _NETWORK_CMDS:
            raise fileops.ActionError(f"{args.cmd} is off here: this thread read a connected app")
        ws = _workspace()
        if os.environ.get("LEOBOT_SHARED"):
            if not ws:
                raise fileops.ActionError("a shared thread runs inside one project workspace")
            if args.cmd in _SHARED_BLOCKED or args.cmd not in _SHARED_ALLOWED:
                raise fileops.ActionError(f"{args.cmd} is not available in a thread invited people "
                                          "can see; the owner adds what the team should remember in Team")
        if ws:
            _guard(args, ws)
        result = args._run(args)
    except fileops.ActionError as exc:
        return _emit({"error": str(exc)}, ok=False)
    except Exception as exc:  # noqa: BLE001
        return _emit({"error": repr(exc)}, ok=False)
    return _emit(result)


if __name__ == "__main__":
    raise SystemExit(main())
