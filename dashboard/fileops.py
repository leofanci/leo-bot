"""fileops.py — WRITE side of the dashboard API.

The dashboard NEVER writes os.db. It mutates the authored source FILE (the plan
JSON / brief JSON), then re-runs index.py so the derived index catches up. This
module is that isolated mutation layer — the server-shaped seam that keeps a
future server migration mechanical.

Pipeline status state machine (lives in the plan file's post object, mirrored to
posts.status on re-index):
    planned -> approved_slot -> briefed -> approved -> published
    (rejected at any review point; rejected can reopen to planned)

Content lives under:
    projects/<project-slug>/profiles/<profile-slug>/
        profile.md
        content/plan-*.json      # posts: {id, status, date?, pillar?, working_title?, channels:[...]}
        content/briefs/<post-id>.json
        channels/<channel-slug>/channel.md
        channels/<channel-slug>/guidelines.md
"""

import datetime
import json
import os
import re
import subprocess
import sys
from pathlib import Path

from core.brief_spec_util import (
    allowed_brief_keys,
    delete_brief,
    list_brief_ids,
    merge_fields_from_slot,
    next_brief_id,
    normalize_brief_for_spec,
    read_spec_platforms,
    read_spec_text,
    validate_brief_obj,
    write_spec_text,
)
from core.voice_util import (
    delete_voice as _delete_voice_file,
    list_voice_ids,
    next_voice_id,
    read_voice_platforms,
    read_voice_text,
    write_voice_text,
)
from core import app_rules, evidence, folders, paths
from core import gtm as gtm_store
from core.ids import (
    build_id_registry,
    find_duplicate_post_ids,
    lk_evidence,
    lk_experiment,
    lk_feature,
    lk_fld_brief,
    lk_memo,
    lk_phase,
    lk_prod,
    lk_strategy,
    mint_post_ids,
    next_activity_id,
    next_experiment_stem,
    next_memo_version,
    next_milestone_id,
    slug_key,
)
from core.project_schemas import (
    FEATURE_PRIORITIES,
    MEMO_TYPES,
    METRIC_MAX_WINDOW_DAYS,
    ROADMAP_SECTION_ALIASES,
    dumps_json,
    normalize_experiment_body,
    normalize_markdown,
    normalize_memo_body,
    normalize_metric,
    parse_markdown_sections,
)
from core.subsections import (
    DOC_META,
    add_doc_subsection,
    config_path,
    ensure_config,
    load_config,
    normalize_doc_text,
    parse_subsections_arg as _parse_subsections_arg,
    save_config,
    set_doc_subsections,
    set_validation_tab_subsections,
    starter_text,
    subsections_api_payload,
    subsections_for_doc,
    validation_tab_subsections,
)


def parse_subsections_arg(raw: str) -> list[str]:
    return _parse_subsections_arg(raw)

ROOT = paths.DATA  # user data; scripts and prompts live in paths.CODE
_GENERATE_SCRIPT = paths.CODE / "generate.py"


def _db_module():
    """Return the `db` module matching however THIS copy of fileops was
    imported (server.py's bare `import fileops` vs. `import dashboard.fileops`
    elsewhere) — each gets its own separate `db` import too, so callers must
    fetch the one THIS copy of fileops would use, not a fixed dotted path, or
    a ROOT/DB_PATH override on one copy silently misses the other's `db`.
    """
    if __name__ == "dashboard.fileops":
        from dashboard import db  # noqa: WPS433 — avoid import cycle at module load
    else:
        import db  # noqa: WPS433 — bare-imported fileops (server.py's sys.path setup)
    return db


def _composed_id(lookup_key: str) -> str:
    """Map internal lookup key → composed id after reindex."""
    db = _db_module()
    reg = build_id_registry(db.tree(), db.posts(), root=ROOT)
    return reg.get(lookup_key) or lookup_key

# index.py is a fixed script (it lives at the repo root); ROOT is the WORKSPACE
# it indexes. They coincide in production but tests point ROOT at a temp dir.
_INDEX_SCRIPT = paths.CODE / "index.py"

ALLOWED_TRANSITIONS = {
    "planned":       {"approved_slot", "rejected"},
    "approved_slot": {"briefed", "rejected", "planned"},
    "briefed":       {"approved", "rejected", "approved_slot"},
    "approved":      {"published", "rejected"},
    "published":     set(),
    "rejected":      {"planned"},
}


class ActionError(Exception):
    """A rejected dashboard action (bad transition, missing post, job failure)."""


_SAFE_COMPONENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_SAFE_POST_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,254}$")


def _safe_component(value: str, label: str = "slug") -> str:
    """Return a path-safe identifier, rejecting separators, dots, and whitespace."""
    cleaned = str(value or "").strip()
    if not _SAFE_COMPONENT_RE.fullmatch(cleaned):
        raise ActionError(
            f"invalid {label}: use 1-128 letters, numbers, hyphens, or underscores"
        )
    return cleaned


def _safe_post_id(value: str) -> str:
    cleaned = str(value or "").strip()
    if not _SAFE_POST_ID_RE.fullmatch(cleaned) or ".." in cleaned:
        raise ActionError("invalid post id")
    return cleaned


def _inside(path: Path, base: Path, label: str) -> Path:
    """`path` resolved, refused unless it stays inside `base`: a symlink (dangling or not)
    never takes a write out of the data folder."""
    real = os.path.normpath(os.path.realpath(path))
    if not real.startswith(os.path.realpath(base) + os.sep):
        raise ActionError(f"{label} is outside the data folder")
    return Path(real)


def _confined_dir(base: Path, component: str, label: str) -> Path:
    """Resolve one child directory and reject symlinks that leave its base."""
    base_path = base
    base_resolved = base_path.resolve()
    child_path = base_path / _safe_component(component, label)
    child_resolved = child_path.resolve()
    if (child_path.is_symlink() or child_resolved.parent != base_resolved
            or not child_resolved.is_dir()):
        raise ActionError(f"{label} '{component}' not found")
    return child_path


# --------------------------------------------------------------------------- #
# generate.py ends itself at its own JOB_DEADLINE; these outer limits catch a child that cannot.
GENERATE_TIMEOUT = 1600
REINDEX_TIMEOUT = 300


def _timed_out(timeout):
    return ActionError(f"stopped after {timeout // 60} minutes: it did not finish")


def reindex():
    try:
        res = subprocess.run([sys.executable, str(_INDEX_SCRIPT), str(ROOT)],
                             capture_output=True, text=True, timeout=REINDEX_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise _timed_out(REINDEX_TIMEOUT) from None
    if res.returncode != 0:
        raise ActionError(f"re-index failed: {res.stderr.strip()[:600]}")
    # Guard: the SQLite index upserts by id, so two posts sharing one id
    # silently collapse to whichever plan file was read last — invisible
    # from the index itself. Check the raw plan files on every reindex so a
    # collision (batch job, hand-edit, drifted counter) fails loudly right
    # where it was introduced instead of surfacing later as a post that
    # resolves to the wrong content.
    dupes = find_duplicate_post_ids(ROOT)
    if dupes:
        detail = "; ".join(f"'{pid}' in {', '.join(files)}" for pid, files in sorted(dupes.items()))
        raise ActionError(f"duplicate post id(s) found after reindex: {detail}")
    return res.stdout


def _profile_dir(slug):
    """Find the profile directory for the given slug under any project."""
    slug = _safe_component(slug, "profile slug")
    projects = ROOT / "projects"
    projects_resolved = projects.resolve()
    if projects.is_dir():
        for project in sorted(projects.iterdir()):
            candidate = project / "profiles" / slug
            resolved = candidate.resolve()
            if (not project.is_symlink() and not candidate.is_symlink()
                    and resolved.is_dir() and resolved.is_relative_to(projects_resolved)):
                return candidate
    raise ActionError(f"profile '{slug}' not found")


def _channel_dir(slug):
    """Find the channel directory for the given slug under any profile."""
    slug = _safe_component(slug, "channel slug")
    projects = ROOT / "projects"
    projects_resolved = projects.resolve()
    if projects.is_dir():
        for project in sorted(projects.iterdir()):
            if project.is_symlink():
                continue
            profiles = project / "profiles"
            if not profiles.is_dir():
                continue
            for profile in sorted(profiles.iterdir()):
                candidate = profile / "channels" / slug
                resolved = candidate.resolve()
                if (not profile.is_symlink() and not candidate.is_symlink()
                        and resolved.is_dir() and resolved.is_relative_to(projects_resolved)):
                    return candidate
    raise ActionError(f"channel '{slug}' not found")


def profile_platforms(slug: str) -> list[str]:
    """Every distinct platform among this profile's channels (for validating
    a brief-spec/voice's `platforms` tag against what actually exists)."""
    profile_dir = _profile_dir(slug)
    channels_dir = profile_dir / "channels"
    if not channels_dir.is_dir():
        return []
    platforms = []
    for channel_dir in sorted(channels_dir.iterdir()):
        channel_md = channel_dir / "channel.md"
        if not channel_md.is_file():
            continue
        fm, _ = _parse_frontmatter(channel_md.read_text(encoding="utf-8"))
        p = fm.get("platform", "").strip()
        if p and p not in platforms:
            platforms.append(p)
    return platforms


def _validate_platforms_tag(slug: str, platforms: str) -> None:
    platforms = (platforms or "all").strip()
    if platforms == "all":
        return
    valid = set(profile_platforms(slug))
    requested = {p.strip() for p in platforms.split(",") if p.strip()}
    unknown = requested - valid
    if unknown:
        raise ActionError(
            f"unknown platform(s) {sorted(unknown)} for profile '{slug}' — "
            f"this profile's channels are: {sorted(valid) or '(none)'}"
        )


def _parse_channels(raw):
    """Parse a comma/space-separated channel slug string into a list of slugs."""
    if not raw:
        return []
    slugs = [s.strip() for s in re.split(r"[,\s]+", raw.strip()) if s.strip()]
    return [_safe_component(slug, "channel slug") for slug in slugs]


def _post_profile_hint(post_id: str, profile_slug: str | None = None) -> str | None:
    """Resolve which profile owns a post id — explicit hint or os.db index."""
    if profile_slug and str(profile_slug).strip():
        return str(profile_slug).strip()
    try:
        db = _db_module()
        rows = db._rows("SELECT profile_slug FROM posts WHERE id = ?", (post_id,))
        if rows:
            return rows[0]["profile_slug"]
    except Exception:
        pass
    return None


def find_post(post_id, profile_slug=None):
    """Locate the plan file + post object for a post id. Returns a dict context.

    Post ids must be unique workspace-wide, but when duplicates exist (e.g. post-001
    in both profile-a and profile-b) the os.db profile_slug disambiguates.
    Callers that know the profile (dashboard post views) should pass profile_slug.
    """
    post_id = _safe_post_id(post_id)
    hint = _post_profile_hint(post_id, profile_slug)
    plan_glob = (
        (_profile_dir(hint) / "content").glob("plan-*.json")
        if hint
        else ROOT.glob("projects/*/profiles/*/content/plan-*.json")
    )
    matches = []
    for plan in sorted(plan_glob):
        try:
            data = json.loads(plan.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for post in data.get("posts", []) if isinstance(data, dict) else []:
            if post.get("id") == post_id:
                matches.append({
                    "plan": plan,
                    "data": data,
                    "post": post,
                    "profile_slug": plan.parent.parent.name,
                })
    if not matches:
        raise ActionError(f"post '{post_id}' not found in any plan file")
    if len(matches) == 1:
        return matches[0]
    resolved = _post_profile_hint(post_id)
    if resolved:
        for ctx in matches:
            if ctx["profile_slug"] == resolved:
                return ctx
    names = ", ".join(sorted({m["profile_slug"] for m in matches}))
    raise ActionError(
        f"post '{post_id}' exists in multiple profiles ({names}) — pass profile to disambiguate"
    )


def _write_plan(ctx):
    ctx["plan"].write_text(
        json.dumps(ctx["data"], indent=2, ensure_ascii=False), encoding="utf-8"
    )


def _effective_status(ctx):
    """The post's real status, reconciled with reality on disk.

    A brief written directly via `generate.py brief` (batch jobs, the terminal)
    creates the brief file but does NOT advance the plan-file status — only
    generate_brief() does. The indexer already *displays* such posts as 'briefed';
    mirror that here so a transition off the UI's "Review →" button doesn't fail
    with an illegal-transition error. Persists the correction when it applies.
    """
    current = ctx["post"].get("status") or "planned"
    brief_file = ctx["plan"].parent / "briefs" / f"{ctx['post'].get('id')}.json"
    if brief_file.exists() and current in ("planned", "approved_slot"):
        ctx["post"]["status"] = current = "briefed"
        _write_plan(ctx)
    return current


def set_status(post_id, new_status, profile_slug=None):
    """Transition a post's status in its plan file, then re-index."""
    if new_status not in ALLOWED_TRANSITIONS:
        raise ActionError(f"unknown status '{new_status}'")
    ctx = find_post(post_id, profile_slug)
    current = _effective_status(ctx)
    if new_status == current:
        raise ActionError(f"post is already '{current}'")
    if new_status not in ALLOWED_TRANSITIONS.get(current, set()):
        raise ActionError(
            f"illegal transition {current} -> {new_status}"
            f" (allowed: {sorted(ALLOWED_TRANSITIONS.get(current, set())) or 'none'})"
        )
    if new_status == "published" and not ctx["post"].get("date"):
        raise ActionError(f"cannot publish '{post_id}' — add a date first")
    ctx["post"]["status"] = new_status
    _write_plan(ctx)
    reindex()
    return {"id": post_id, "status": new_status, "from": current}


def generate_brief(post_id, instruction=None, profile_slug=None, brief_id=None, voice_id=None):
    """Run the claude -p brief job (Write button). Persists via write_brief inside generate.py.

    brief_id/voice_id override which brief-spec/voice to use — omit to use
    whatever the post is stored with, else br1/vc1 (see generate.py do_brief)."""
    ctx = find_post(post_id, profile_slug)
    current = ctx["post"].get("status") or "planned"
    if current not in ("planned", "approved_slot"):
        raise ActionError(f"can only brief a planned/approved_slot post (is '{current}')")

    cmd = [sys.executable, str(_GENERATE_SCRIPT),
           "--workspace", str(ROOT), "brief", ctx["profile_slug"], post_id]
    if instruction and instruction.strip():
        cmd += [f"--instruction={instruction.strip()}"]  # "=": option-looking text stays text
    if brief_id:
        cmd += [f"--spec={brief_id}"]
    if voice_id:
        cmd += [f"--voice={voice_id}"]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        raise ActionError(f"brief job failed: {(res.stderr or res.stdout).strip()[:800]}")
    return {"id": post_id, "status": "briefed", "stdout": res.stdout.strip()}


def update_brief(post_id, instruction=None, profile_slug=None, brief_id=None, voice_id=None):
    """Natural-language brief create or update — primary chat path.

    Existing brief → revise job with the user's words. No brief yet → generate
    job (optional direction). Same validated persist path either way."""
    ctx = find_post(post_id, profile_slug)
    brief_file = ctx["plan"].parent / "briefs" / f"{post_id}.json"
    if brief_file.exists():
        if not (instruction or "").strip():
            raise ActionError("instruction is required to change an existing brief")
        return revise_post(post_id, instruction, profile_slug, brief_id, voice_id)
    return generate_brief(post_id, instruction, profile_slug, brief_id, voice_id)


def revise_post(post_id, instruction, profile_slug=None, brief_id=None, voice_id=None):
    """Revise a slot (idea) or brief (draft) in place via the AI revise job.

    For drafts the brief file is overwritten and the plan-file version is bumped.
    For ideas the slot fields are updated directly in the plan file.
    brief_id/voice_id override which brief-spec/voice to use — omit to use
    whatever the post is stored with, else br1/vc1 (see generate.py do_revise)."""
    if not instruction or not instruction.strip():
        raise ActionError("instruction is required")
    ctx = find_post(post_id, profile_slug)
    brief_file = ctx["plan"].parent / "briefs" / f"{post_id}.json"
    is_draft = brief_file.exists()

    cmd = [sys.executable, str(_GENERATE_SCRIPT),
           "--workspace", str(ROOT), "revise", ctx["profile_slug"], post_id,
           f"--instruction={instruction}"]  # "=": option-looking text stays text
    if brief_id:
        cmd += [f"--spec={brief_id}"]
    if voice_id:
        cmd += [f"--voice={voice_id}"]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        raise ActionError(f"revise job failed: {(res.stderr or res.stdout).strip()[:800]}")

    if is_draft:
        ctx = find_post(post_id, profile_slug)
        ctx["post"]["version"] = int(ctx["post"].get("version") or 1) + 1
        _write_plan(ctx)

    reindex()
    return {"id": post_id, "is_draft": is_draft, "stdout": res.stdout.strip()}


def _parse_frontmatter(text):
    """Return (dict of frontmatter fields, body string) for --- ... --- text."""
    fm, body = {}, ""
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) >= 3:
            for line in parts[1].strip().splitlines():
                if ":" in line:
                    k, _, v = line.partition(":")
                    fm[k.strip()] = v.strip()
            body = parts[2].strip()
        else:
            print("warning: frontmatter opened but never closed", file=sys.stderr)
    elif (first := text.split("\n", 1)[0].rstrip()).endswith("---"):
        # e.g. stray typing before the opener: metadata would vanish silently
        print(f"warning: malformed frontmatter opener {first!r}", file=sys.stderr)
    return fm, body


def brief_spec_relpath(slug, brief_id="br1"):
    """Repo-relative path to one of the profile's brief-spec files."""
    profile_dir = _profile_dir(slug)
    from core.brief_spec_util import spec_file
    return str(spec_file(profile_dir, brief_id).relative_to(ROOT))


def read_brief_spec(slug, brief_id="br1"):
    """Live brief spec text for this profile + id (default br1)."""
    return read_spec_text(_profile_dir(slug), brief_id)


def get_brief_spec(slug: str, brief_id: str = "br1") -> dict:
    profile_dir = _profile_dir(slug)
    return {
        "slug": slug, "id": brief_id,
        "path": brief_spec_relpath(slug, brief_id),
        "text": read_spec_text(profile_dir, brief_id),
        "platforms": read_spec_platforms(profile_dir, brief_id),
    }


def list_brief_specs(slug: str) -> list[dict]:
    profile_dir = _profile_dir(slug)
    return [get_brief_spec(slug, bid) for bid in list_brief_ids(profile_dir)]


def write_brief_spec(slug, text, brief_id="br1", platforms=None):
    """Write one brief-spec file. Profile Setup and chat use this same path."""
    profile_dir = _profile_dir(slug)
    if platforms is not None:
        _validate_platforms_tag(slug, platforms)
    write_spec_text(profile_dir, text, brief_id, platforms)
    return {"slug": slug, "id": brief_id, "path": brief_spec_relpath(slug, brief_id)}


# Back-compat name used by existing callers that pass no id (Profile Setup,
# older osctl behavior) — same as write_brief_spec with brief_id="br1".
def update_brief_spec(slug, text, brief_id="br1", platforms=None):
    return write_brief_spec(slug, text, brief_id, platforms)


def create_brief_spec(slug: str, text: str, platforms: str = "all") -> dict:
    profile_dir = _profile_dir(slug)
    _validate_platforms_tag(slug, platforms)
    brief_id = next_brief_id(profile_dir)
    write_spec_text(profile_dir, text, brief_id, platforms)
    return {"slug": slug, "brief_id": brief_id, "path": brief_spec_relpath(slug, brief_id)}


def delete_brief_spec(slug: str, brief_id: str) -> dict:
    profile_dir = _profile_dir(slug)
    try:
        delete_brief(profile_dir, brief_id)
    except ValueError as e:
        raise ActionError(str(e)) from e
    return {"slug": slug, "brief_id": brief_id, "deleted": True}


def get_voice(slug: str, voice_id: str = "vc1") -> dict:
    profile_dir = _profile_dir(slug)
    return {
        "slug": slug, "id": voice_id,
        "text": read_voice_text(profile_dir, voice_id),
        "platforms": read_voice_platforms(profile_dir, voice_id),
    }


def list_voices(slug: str) -> list[dict]:
    profile_dir = _profile_dir(slug)
    return [get_voice(slug, vid) for vid in list_voice_ids(profile_dir)]


def create_voice(slug: str, text: str, platforms: str = "all") -> dict:
    profile_dir = _profile_dir(slug)
    _validate_platforms_tag(slug, platforms)
    voice_id = next_voice_id(profile_dir)
    write_voice_text(profile_dir, text, voice_id, platforms)
    return {"slug": slug, "voice_id": voice_id}


def update_voice(slug: str, text: str, voice_id: str = "vc1", platforms: str = None) -> dict:
    profile_dir = _profile_dir(slug)
    if platforms is not None:
        _validate_platforms_tag(slug, platforms)
    write_voice_text(profile_dir, text, voice_id, platforms)
    return {"slug": slug, "voice_id": voice_id}


def delete_voice(slug: str, voice_id: str) -> dict:
    profile_dir = _profile_dir(slug)
    try:
        _delete_voice_file(profile_dir, voice_id)
    except ValueError as e:
        raise ActionError(str(e)) from e
    return {"slug": slug, "voice_id": voice_id, "deleted": True}


def read_profile(slug):
    """Read profile.md and return name, topic, project. Voice and brief-spec
    live in voices/ and brief-specs/ now — use list_voices/list_brief_specs."""
    d = _profile_dir(slug)
    f = d / "profile.md"
    if not f.exists():
        return {"slug": slug, "name": slug, "topic": "", "project": ""}
    fm, _ = _parse_frontmatter(f.read_text(encoding="utf-8"))
    return {"slug": slug, "name": fm.get("name", slug),
            "topic": fm.get("topic", ""), "project": fm.get("project", "")}


def read_channel_guidelines(slug):
    """Read guidelines for a channel from its guidelines.md file."""
    f = _channel_dir(slug) / "guidelines.md"
    return f.read_text(encoding="utf-8") if f.exists() else ""


def write_channel_guidelines(slug, text):
    """Save a channel's guidelines (authored file; not indexed, so no re-index)."""
    (_channel_dir(slug) / "guidelines.md").write_text(text or "", encoding="utf-8")
    return {"slug": slug, "chars": len(text or "")}


def refine_guidelines(slug, raw_text):
    """AI-polish rough guideline notes via generate.py; returns text, does NOT save."""
    _channel_dir(slug)
    try:
        res = subprocess.run(
            [sys.executable, str(_GENERATE_SCRIPT),
             "--workspace", str(ROOT), "refine-guidelines", slug],
            input=raw_text or "", capture_output=True, text=True, timeout=GENERATE_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        raise _timed_out(GENERATE_TIMEOUT) from None
    if res.returncode != 0:
        raise ActionError(f"refine failed: {(res.stderr or res.stdout).strip()[:800]}")
    return {"refined": res.stdout}


def read_detail(post_id, profile_slug=None):
    """Authored detail for a post: the plan slot + the brief JSON if it exists.

    Prose/authored content is read from FILES (their source of truth), while the
    coordination fields come from os.db via db.py.
    """
    ctx = find_post(post_id, profile_slug)
    brief = None
    brief_file = ctx["plan"].parent / "briefs" / f"{post_id}.json"
    if brief_file.exists():
        try:
            brief = json.loads(brief_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            brief = {"_error": "brief file is not valid JSON"}
    return {"slot": ctx["post"], "brief": brief, "profile_slug": ctx["profile_slug"]}


_POST_FIELDS = ("date", "pillar", "working_title", "concept", "format", "objective", "platform",
                "notes", "brief_id", "voice_id")


def add_post(profile_slug, fields):
    """Create a manual idea-slot in the profile's newest plan file (or plan-manual.json)."""
    profile_dir = _profile_dir(profile_slug)
    content = profile_dir / "content"
    content.mkdir(parents=True, exist_ok=True)
    plans = sorted(content.glob("plan-*.json"))
    if plans:
        plan = plans[-1]
        data = json.loads(plan.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get("posts"), list):
            raise ActionError(f"plan file {plan.name} has an unexpected shape")
    else:
        plan = content / "plan-manual.json"
        data = {"posts": []}
    project_slug = profile_dir.parent.parent.name
    pid = mint_post_ids(ROOT, project_slug, profile_slug, 1)[0]
    status = (fields.get("status") or "planned").strip()
    if status not in ("planned", "published"):
        raise ActionError(f"add_post status must be planned or published, got '{status}'")
    if status == "published" and not (fields.get("date") or "").strip():
        raise ActionError("cannot log a published post without a date")
    post = {"id": pid, "status": status}
    for k in _POST_FIELDS:
        v = (fields.get(k) or "").strip()
        if v:
            post[k] = v
    channels = _parse_channels(fields.get("channels"))
    if channels:
        post["channels"] = channels
    post.setdefault("brief_id", "br1")
    post.setdefault("voice_id", "vc1")
    data["posts"].append(post)
    plan.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    reindex()
    return {"id": pid, "profile_slug": profile_slug}


def _merge_brief_patch(existing: dict, patch: dict) -> dict:
    merged = dict(existing)
    for k, v in patch.items():
        if k == "id":
            continue
        if v is None or v == "" or (isinstance(v, list) and not v):
            merged.pop(k, None)
        else:
            merged[k] = v
    return merged


def _sync_slot_identity_to_brief(brief: dict, slot: dict) -> None:
    """Keep brief identity fields aligned with the plan slot after manual edits."""
    for k in ("format", "objective", "pillar", "platform"):
        if slot.get(k):
            brief[k] = slot[k]
    if slot.get("channels"):
        brief["channels"] = slot["channels"]


def update_post(post_id, fields, profile_slug=None):
    """Edit plan-slot fields and, when a brief exists, patch brief JSON in one save."""
    ctx = find_post(post_id, profile_slug)
    if "date" in fields:
        new_date = (fields.get("date") or "").strip()
        # a published post may have its date corrected (e.g. it actually went
        # out on a different day) but must always keep some date.
        if not new_date and ctx["post"].get("status") == "published":
            raise ActionError("a published post must keep a date")
    for k in _POST_FIELDS:
        if k in fields:
            v = (fields.get(k) or "").strip()
            if v:
                ctx["post"][k] = v
            else:
                ctx["post"].pop(k, None)
    if "channels" in fields:
        channels = _parse_channels(fields.get("channels"))
        if channels:
            ctx["post"]["channels"] = channels
        else:
            ctx["post"].pop("channels", None)
    brief_patch = fields.get("brief")
    if brief_patch is not None:
        if not isinstance(brief_patch, dict):
            raise ActionError("brief must be a JSON object")
        brief_file = ctx["plan"].parent / "briefs" / f"{post_id}.json"
        if not brief_file.exists():
            raise ActionError(f"no brief for post '{post_id}' — generate draft first")
        try:
            existing = json.loads(brief_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ActionError(f"brief file unreadable: {exc}") from exc
        if not isinstance(existing, dict):
            raise ActionError("brief must be a JSON object")
        merged = _merge_brief_patch(existing, brief_patch)
        _sync_slot_identity_to_brief(merged, ctx["post"])
        _write_plan(ctx)
        write_brief(
            post_id, merged, strict_spec=False, set_status=False,
            bump_version_if_exists=True, profile_slug=profile_slug,
        )
    else:
        _write_plan(ctx)
        reindex()
    return {"id": post_id}



def write_brief(post_id, brief, *, bump_version_if_exists=True, set_status=True,
                strict_spec=None, profile_slug=None):
    """Canonical brief persist — write JSON, update plan.

    New briefs validate against current brief-spec.md. Existing briefs are
    grandfathered when the spec changes — they keep their old shape unless
    the user explicitly revises them."""
    if not isinstance(brief, dict):
        raise ActionError("brief must be a JSON object")
    ctx = find_post(post_id, profile_slug)
    slot = ctx["post"]
    briefs_dir = ctx["plan"].parent / "briefs"
    briefs_dir.mkdir(parents=True, exist_ok=True)
    brief_file = briefs_dir / f"{post_id}.json"
    is_rebrief = brief_file.exists()
    if strict_spec is None:
        strict_spec = not is_rebrief
    spec_text = read_brief_spec(ctx["profile_slug"], slot.get("brief_id") or "br1")
    normalize_brief_for_spec(brief, spec_text)
    for k in merge_fields_from_slot(spec_text):
        if not brief.get(k) and slot.get(k):
            brief[k] = slot[k]
    allowed = allowed_brief_keys(spec_text)
    if allowed is not None:
        for k in list(brief.keys()):
            if k not in allowed:
                del brief[k]
    errs = validate_brief_obj(brief, post_id, spec_text, slot, strict_spec=strict_spec)
    if errs:
        label = "brief does not match profile brief spec" if strict_spec else "brief invalid"
        raise ActionError(f"{label}: " + "; ".join(errs))
    brief["id"] = post_id
    brief_file.write_text(json.dumps(brief, indent=2, ensure_ascii=False), encoding="utf-8")
    if set_status:
        ctx["post"]["status"] = "briefed"
    if is_rebrief and bump_version_if_exists:
        ctx["post"]["version"] = int(ctx["post"].get("version") or 1) + 1
    _write_plan(ctx)
    reindex()
    return {"id": post_id, "status": ctx["post"].get("status", "briefed"), "rebrief": is_rebrief}


def set_brief(post_id, brief):
    """Internal persist — generate.py and tests only. Agents use update_brief()."""
    return write_brief(post_id, brief)

def add_slide_overlay(post_id: str, overlay: str, profile_slug=None) -> dict:
    """Append one slide_overlays row to an existing brief (sync via reindex)."""
    text = (overlay or "").strip()
    if not text:
        raise ActionError("overlay text is required")
    ctx = find_post(post_id, profile_slug)
    brief_file = ctx["plan"].parent / "briefs" / f"{post_id}.json"
    if not brief_file.exists():
        raise ActionError(f"no brief for post '{post_id}' — generate draft first")
    try:
        brief = json.loads(brief_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ActionError(f"brief file unreadable: {exc}") from exc
    if not isinstance(brief, dict):
        raise ActionError("brief must be a JSON object")
    slides = brief.get("slide_overlays")
    if not isinstance(slides, list):
        slides = []
    next_n = 1
    for item in slides:
        if isinstance(item, dict) and item.get("slide"):
            try:
                next_n = max(next_n, int(item["slide"]) + 1)
            except (TypeError, ValueError):
                pass
    slides.append({"slide": next_n, "overlay": text})
    brief["slide_overlays"] = slides
    lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
    if lines:
        title = lines[0]
        tail = lines[2] if len(lines) > 2 else (lines[1] if len(lines) > 1 else "")
        bullet = f"• {title}. {tail}" if tail else f"• {title}"
        cap = brief.get("caption") or ""
        if bullet not in cap:
            for marker in ("\n\nWhich", "\n\n#", "\n\nAnd "):
                idx = cap.find(marker)
                if idx != -1:
                    cap = cap[:idx].rstrip() + "\n" + bullet + cap[idx:]
                    break
            else:
                cap = (cap.rstrip() + "\n" + bullet).strip() + "\n"
            brief["caption"] = cap
    result = write_brief(post_id, brief, bump_version_if_exists=True, strict_spec=False,
                          profile_slug=profile_slug)
    return {
        **result,
        "slide": next_n,
        "field_id": _composed_id(lk_fld_brief(post_id, f"slide-{next_n}")),
    }


def delete_post(post_id, profile_slug=None):
    """Remove a slot (and its brief file, if any), then re-index."""
    ctx = find_post(post_id, profile_slug)
    ctx["data"]["posts"] = [p for p in ctx["data"]["posts"] if p.get("id") != post_id]
    brief = ctx["plan"].parent / "briefs" / f"{post_id}.json"
    if brief.exists():
        brief.unlink()
    _write_plan(ctx)
    reindex()
    return {"id": post_id, "deleted": True}


def delete_posts(post_ids, profile_slug=None):
    """Delete several slots in one pass (one re-index). Unknown ids are skipped.

    Touches each plan file at most once so a multi-select delete is a single
    write + rebuild rather than N of them."""
    wanted = {_safe_post_id(i) for i in (post_ids or [])}
    if not wanted:
        return {"deleted": [], "count": 0}
    deleted = []
    plan_glob = (
        (_profile_dir(profile_slug) / "content").glob("plan-*.json")
        if profile_slug
        else ROOT.glob("projects/*/profiles/*/content/plan-*.json")
    )
    for plan in sorted(plan_glob):
        try:
            data = json.loads(plan.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        posts = data.get("posts", []) if isinstance(data, dict) else []
        hit = [p for p in posts if p.get("id") in wanted]
        if not hit:
            continue
        data["posts"] = [p for p in posts if p.get("id") not in wanted]
        for p in hit:
            brief = plan.parent / "briefs" / f"{p.get('id')}.json"
            if brief.exists():
                brief.unlink()
            deleted.append(p.get("id"))
        plan.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    reindex()
    return {"deleted": deleted, "count": len(deleted)}


def _slugify(name: str) -> str:
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", name.lower())).strip("-")


def normalize_local_folder(value) -> str:
    """Return one safe folder (core.folders.normalize_path), or an empty string."""
    try:
        return folders.normalize_path(value, ROOT)
    except folders.FolderError as exc:
        raise ActionError(str(exc)) from exc


_NOT_ONE_LINE = re.compile(r"[\x00-\x1f\x7f\x85\u2028\u2029]")
_PRIORITIES = {"primary", "secondary", "experiment"}


def _one_line(value, label: str) -> str:
    """A value for a one-line file format. A line break in it would write a field or a row of
    its own: `name: x\\nlocal_folder: /etc` would link a folder, which only the owner may do."""
    text = str(value if value is not None else "").strip()
    if _NOT_ONE_LINE.search(text) or "---" in text:
        raise ActionError(f"{label}: one line of text, without '---'")
    return text


def _row_value(value, label: str) -> str:
    """A value for one field of a ' — '-separated row: one line, and no em dash at all. A value
    that begins with one ('— entity: other') would join the row's key prefix into a separator and
    become a field of its own, so the dash is replaced rather than only the exact separator."""
    return _one_line(value, label).replace("\u2014", "-")


def _flat(value, label: str) -> str:
    """Free text typed into a box (a bio, a topic): pasted line breaks become spaces."""
    return _one_line(" ".join(str(value if value is not None else "").split()), label)


def _priority(value) -> str:
    """The index's schema allows these three: anything else would stop it building."""
    text = _one_line(value, "priority")
    if text not in _PRIORITIES:
        raise ActionError("priority: primary, secondary or experiment")
    return text


def _entity_slugs() -> set:
    """Every slug the index counts as one entity. It refuses to build on a duplicate or an
    unknown one, and a broken index stops every write, so these are checked before writing."""
    projects = ROOT / "projects"
    if not projects.is_dir():
        return set()
    found = set()
    for proj in projects.iterdir():
        if proj.is_dir():
            found.add(proj.name)
            found.update(d.name for d in proj.glob("products/*") if d.is_dir())
            found.update(d.name for d in proj.glob("profiles/*") if d.is_dir())
            found.update(d.name for d in proj.glob("profiles/*/channels/*") if d.is_dir())
    return found


def _need_free_slug(slug: str) -> None:
    # Case-blind: on a Mac's disk "Acme" and "acme" are the same folder.
    if slug.lower() in {s.lower() for s in _entity_slugs()}:
        raise ActionError(f"'{slug}' is already used by a project, profile, channel or product: pick another")


def _need_known_entity(slug: str) -> None:
    if slug not in _entity_slugs():
        raise ActionError(f"no project, profile, channel or product called '{slug}'")


def _forget_leftovers(slug: str) -> None:
    """A new workspace starts empty even when a delete of the same slug stopped half way
    (people, invites, approvals, app grants and engine picks are kept outside its folder)."""
    from core import approvals, integrations, people
    from dashboard.engines import config as engine_config
    approvals.forget_workspace(slug, ROOT)
    integrations.forget_workspace(slug, ROOT)
    people.forget_workspace(ROOT, slug)
    engine_config.forget_workspace(slug, ROOT)


def create_project(slug: str, fields: dict) -> dict:
    slug = _safe_component(slug, "project slug")
    projects_dir = ROOT / "projects"
    project_dir = _inside(projects_dir / slug, projects_dir, "project")
    # Resolving to anything but projects/<slug> means a symlink sits there: taken, never followed.
    if project_dir != Path(os.path.realpath(projects_dir)) / slug or project_dir.exists():
        raise ActionError(f"project '{slug}' already exists")
    _need_free_slug(slug)
    _forget_leftovers(slug)
    name = _one_line(fields.get("name") or slug, "name")
    kind = _one_line(fields.get("kind") or "venture", "kind")
    priority = _priority(fields.get("priority") or "primary")
    status = _one_line(fields.get("status") or "idea", "status")
    hours = _one_line(fields.get("hours_per_week") or "0", "hours_per_week")
    voice = (fields.get("voice") or "").strip()
    local_folder = normalize_local_folder(fields.get("local_folder"))  # checked before any write
    for sub in ["profiles", "products", "strategy/memos", "strategy/experiments"]:
        (project_dir / sub).mkdir(parents=True, exist_ok=True)
    md = (f"---\nname: {name}\nkind: {kind}\npriority: {priority}\n"
          f"hours_per_week: {hours}\nstatus: {status}\n---\n{voice}\n")
    (project_dir / "project.md").write_text(md, encoding="utf-8")
    ensure_config(ROOT, slug)
    if local_folder:
        _link_folder(slug, local_folder)
    reindex()
    return {"slug": slug, "folders": folders.load(slug, ROOT)["folders"]}


def _link_folder(slug: str, path: str) -> None:
    """Add one folder at the default access (core.folders.add)."""
    try:
        folders.add(slug, path, root=ROOT)
    except (folders.FolderError, ValueError) as exc:
        raise ActionError(str(exc)) from exc


def update_project(slug: str, fields: dict) -> dict:
    """Rewrite project.md frontmatter (name/kind/priority/status/hours), keep body.
    The slug is identity (it's the directory name + every reference key), so it is
    NOT changed here — only the display name and metadata are editable."""
    slug = _safe_component(slug, "project slug")
    f = _inside(_project_dir(slug) / "project.md", ROOT / "projects", "project")
    folders.load(slug, ROOT)  # moves an old local_folder line out first, so it is never lost
    fm, body = _parse_frontmatter(f.read_text(encoding="utf-8"))
    name = _one_line(fields.get("name") or fm.get("name") or slug, "name")
    kind = _one_line(fields.get("kind") or fm.get("kind") or "venture", "kind")
    priority = _priority(fields.get("priority") or fm.get("priority") or "primary")
    status = _one_line(fields.get("status") or fm.get("status") or "idea", "status")
    hours = _one_line(fields.get("hours_per_week") or fm.get("hours_per_week") or "0", "hours_per_week")
    # A non-empty local_folder adds a folder; unlinking is done on the Folders screen.
    local_folder = normalize_local_folder(fields.get("local_folder"))
    md = (f"---\nname: {name}\nkind: {kind}\npriority: {priority}\n"
          f"hours_per_week: {hours}\nstatus: {status}\n---\n{body}\n")
    f.write_text(md, encoding="utf-8")
    if local_folder:
        _link_folder(slug, local_folder)
    reindex()
    return {"slug": slug, "folders": folders.load(slug, ROOT)["folders"]}


def _portfolio_refs(slug: str) -> list:
    """Portfolio activities/milestones that reference this entity slug. These live
    OUTSIDE the project tree, so deleting the tree would leave them dangling and
    break the next re-index's slug integrity check — callers refuse on non-empty."""
    refs = []
    acts = ROOT / "portfolio" / "activities.md"
    if acts.exists():
        pat = re.compile(rf"entity:\s*{re.escape(slug)}(?![\w-])")
        for line in acts.read_text(encoding="utf-8").splitlines():
            if pat.search(line):
                m = re.match(r"^- \[[ x]\]\s*(.*?)(?:\s+—|$)", line)
                refs.append(f"activity '{(m.group(1) if m else line).strip()}'")
    ms = ROOT / "portfolio" / "milestones.json"
    if ms.exists():
        try:
            data = json.loads(ms.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = {}
        for m in data.get("milestones", []) if isinstance(data, dict) else []:
            if m.get("entity") == slug:
                refs.append(f"milestone '{m.get('title') or m.get('id')}'")
    return refs


def delete_project(slug: str) -> dict:
    """Remove a project tree, refusing if portfolio items still reference it."""
    import shutil
    slug = _safe_component(slug, "project slug")
    project_dir = _project_dir(slug)
    refs = _portfolio_refs(slug)
    if refs:
        raise ActionError(
            f"cannot delete '{slug}' — still referenced by {', '.join(refs)}."
            " Remove or reassign these first.")
    shutil.rmtree(project_dir)
    reindex()
    return {"slug": slug, "deleted": True}


def create_profile(project_slug: str, slug: str, fields: dict) -> dict:
    project_slug = _safe_component(project_slug, "project slug")
    slug = _safe_component(slug, "profile slug")
    project_dir = _project_dir(project_slug)
    profile_dir = project_dir / "profiles" / slug
    if profile_dir.exists():
        raise ActionError(f"profile '{slug}' already exists")
    _need_free_slug(slug)
    name = _one_line(fields.get("name") or slug, "name")
    topic = _flat(fields.get("topic") or "", "topic")
    for sub in ["content/briefs", "channels"]:
        (profile_dir / sub).mkdir(parents=True, exist_ok=True)
    md = f"---\nname: {name}\ntopic: {topic}\nproject: {project_slug}\n---\n"
    (profile_dir / "profile.md").write_text(md, encoding="utf-8")
    reindex()
    return {"slug": slug, "project": project_slug}


def update_profile(slug: str, fields: dict) -> dict:
    """Rewrite profile.md frontmatter (name/topic), keep structure."""
    profile_dir = _profile_dir(slug)  # raises if not found
    f = profile_dir / "profile.md"
    fm, _ = _parse_frontmatter(f.read_text(encoding="utf-8")) if f.exists() else ({}, "")
    name = _one_line(fields.get("name") or fm.get("name") or slug, "name")
    topic = _flat(fields.get("topic") if fields.get("topic") is not None else fm.get("topic", ""), "topic")
    project = fm.get("project", "")
    md = f"---\nname: {name}\ntopic: {topic}\nproject: {project}\n---\n"
    f.write_text(md, encoding="utf-8")
    reindex()
    return {"slug": slug}


def create_channel(profile_slug: str, slug: str, platform: str, handle: str = "") -> dict:
    slug = _safe_component(slug, "channel slug")
    profile_dir = _profile_dir(profile_slug)
    channel_dir = profile_dir / "channels" / slug
    if channel_dir.exists():
        raise ActionError(f"channel '{slug}' already exists")
    _need_free_slug(slug)
    platform, handle = _one_line(platform, "platform"), _one_line(handle, "handle")
    channel_dir.mkdir(parents=True, exist_ok=True)
    handle_line = f"handle: {handle}\n" if handle else ""
    md = f"---\nplatform: {platform}\n{handle_line}---\n"
    (channel_dir / "channel.md").write_text(md, encoding="utf-8")
    (channel_dir / "guidelines.md").write_text("", encoding="utf-8")
    reindex()
    return {"slug": slug, "profile": profile_slug, "platform": platform}


def update_channel(slug: str, fields: dict) -> dict:
    """Rewrite channel.md frontmatter (platform/handle/name), keep body.
    guidelines.md is a separate authored file and is left untouched."""
    f = _channel_dir(slug) / "channel.md"  # _channel_dir raises if slug unknown
    fm, body = _parse_frontmatter(f.read_text(encoding="utf-8")) if f.exists() else ({}, "")
    platform = _one_line(fields.get("platform") or fm.get("platform") or "", "platform")
    raw_handle = fields.get("handle") if fields.get("handle") is not None else fm.get("handle", "")
    handle = _one_line(raw_handle or "", "handle")
    name = _one_line(fields.get("name") or fm.get("name") or "", "name")
    bio = _flat(fields.get("bio") or fm.get("bio") or "", "bio")
    lines = [f"platform: {platform}"]
    if handle:
        lines.append(f"handle: {handle}")
    if name:
        lines.append(f"name: {name}")
    if bio:
        lines.append(f"bio: {bio}")
    md = "---\n" + "\n".join(lines) + "\n---\n" + (f"{body}\n" if body else "")
    f.write_text(md, encoding="utf-8")
    reindex()
    return {"slug": slug}


def delete_channel(slug: str) -> dict:
    import shutil
    shutil.rmtree(_channel_dir(slug))
    reindex()
    return {"slug": slug, "deleted": True}


def delete_profile(slug: str) -> dict:
    import shutil
    shutil.rmtree(_profile_dir(slug))
    reindex()
    return {"slug": slug, "deleted": True}


def delete_activity(title: str, entity_slug: str = "") -> dict:
    """Delete one activity row: this exact title (and workspace, when given). Several rows
    with the same title in different workspaces need the workspace named, never a blanket delete."""
    path = ROOT / "portfolio" / "activities.md"
    if not path.exists():
        raise ActionError("activities.md not found")
    title, entity_slug = str(title or "").strip(), str(entity_slug or "").strip()
    lines = path.read_text(encoding="utf-8").split("\n")
    hits = []
    for i, line in enumerate(lines):
        m = re.match(r"^- \[[ x]\] (.*)$", line)
        if not m or not title:
            continue
        body = m.group(1)
        if body != title and not body.startswith(title + " — "):
            continue  # the whole title, so "Call" never matches "Call back" (a title may hold " — ")
        extra = [p.strip() for p in body[len(title):].split(" — ") if p.strip()]
        has_entity = any(p.startswith("entity:") for p in extra)
        if not entity_slug or f"entity: {entity_slug}" in extra or not has_entity:
            hits.append(i)
    if not hits:
        raise ActionError(f"activity '{title}' not found")
    if len(hits) > 1:
        raise ActionError(f"several activities are called '{title}': say which workspace")
    del lines[hits[0]]
    path.write_text("\n".join(lines), encoding="utf-8")
    reindex()
    return {"title": title, "deleted": True}


def mark_activity_done(title: str, entity_slug: str) -> dict:
    """Mark an activity as done in portfolio/activities.md by checking its checkbox."""
    path = ROOT / "portfolio" / "activities.md"
    if not path.exists():
        raise ActionError("activities.md not found")
    # This exact title in this entity: the file holds every workspace's activities.
    title, entity_slug = str(title or "").strip(), str(entity_slug or "").strip()
    lines, n = path.read_text(encoding="utf-8").split("\n"), 0
    for i, line in enumerate(lines):
        if not line.startswith("- [ ] "):
            continue
        parts = [p.strip() for p in line[6:].split(" — ")]
        no_entity = not entity_slug and not any(p.startswith("entity:") for p in parts[1:])  # a hand-written row
        if title and parts[0] == title and (no_entity or f"entity: {entity_slug}" in parts[1:]):
            lines[i] = "- [x] " + line[6:]
            n += 1
    if n == 0:
        raise ActionError(f"activity '{title}' not found or already done")
    path.write_text("\n".join(lines), encoding="utf-8")
    reindex()
    return {"title": title, "done": True}


_TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
DEFAULT_ACTIVITY_MINUTES = 60


def _minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def _hhmm(minutes: int) -> str:
    minutes = min(minutes, 23 * 60 + 59)
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def timed_activities() -> list[dict]:
    """Open timed activities across ALL workspaces: the CEO has one calendar."""
    path = ROOT / "portfolio" / "activities.md"
    out = []
    if not path.exists():
        return out
    for _sec, checked, title, f in _parse_activities(path.read_text(encoding="utf-8")):
        if checked or not f.get("date") or not f.get("time"):
            continue
        out.append({"id": f.get("id"), "title": title, "entity": f.get("entity"),
                    "date": f["date"], "time": f["time"],
                    "time_end": f.get("time_end") or _hhmm(_minutes(f["time"]) + DEFAULT_ACTIVITY_MINUTES)})
    return out


def find_overlaps(date: str, time: str, time_end: str) -> list[dict]:
    """Open timed activities (any workspace) that intersect date time-time_end."""
    start, end = _minutes(time), _minutes(time_end)
    return [a for a in timed_activities()
            if a["date"] == date and _minutes(a["time"]) < end and start < _minutes(a["time_end"])]


def _validate_times(date: str, date_end: str, time: str, time_end: str) -> tuple[str, str]:
    if not time and not time_end:
        return "", ""
    if not time:
        raise ActionError("time_end needs a start time")
    if not date:
        raise ActionError("a timed activity needs a date")
    if date_end and date_end != date:
        raise ActionError("a timed activity is single-day (drop date_end or the times)")
    for t in (time, time_end):
        if t and not _TIME_RE.match(t):
            raise ActionError(f"time must be HH:MM (24h), got {t!r}")
    if not time_end:
        time_end = _hhmm(_minutes(time) + DEFAULT_ACTIVITY_MINUTES)
    if _minutes(time_end) <= _minutes(time):
        raise ActionError("time_end must be after time")
    return time, time_end


def create_activity(fields: dict, allow_overlap: bool = False) -> dict:
    """Append an activity. Timed ones may not overlap any open timed activity
    in any workspace unless `allow_overlap` (the user's own manual booking);
    bots (osctl) never pass it."""
    # One row per line, " — " between fields, in a file every workspace shares: a line break
    # would write a row into another workspace, so each value stays on its one line.
    entity = _row_value(fields.get("entity") or "", "entity")
    title = _row_value(fields.get("title") or "", "title")
    if not title:
        raise ActionError("title is required")
    if not entity:
        raise ActionError("entity (project slug) is required")
    _need_known_entity(entity)  # an unknown one breaks the index, and with it every write
    portfolio = ROOT / "portfolio"
    portfolio.mkdir(exist_ok=True)
    path = portfolio / "activities.md"
    date = _row_value(fields.get("date") or "", "date")
    date_end = _row_value(fields.get("date_end") or "", "date_end")
    time, time_end = _validate_times(date, date_end, (fields.get("time") or "").strip(),
                                     (fields.get("time_end") or "").strip())
    overlaps = find_overlaps(date, time, time_end) if time else []
    if overlaps and not allow_overlap:
        clash = ", ".join(f"{o['time']}-{o['time_end']}" for o in overlaps)  # no names: workspaces stay private
        raise ActionError(f"{date} {time}-{time_end} overlaps booked time: {clash}. "
                          "Pick a free slot (osctl get-busy) or ask the user.")
    type_ = _row_value(fields.get("type") or "task", "type")
    priority = (fields.get("priority") or "").strip()
    existing_ids: set[str] = set()
    if path.exists():
        for _sec, _chk, _title, row in _parse_activities(path.read_text(encoding="utf-8")):
            if row.get("id"):
                existing_ids.add(str(row["id"]))
    act_id = next_activity_id(existing_ids)
    parts = [title, f"id: {act_id}", f"entity: {entity}"]
    if date:
        parts.append(f"date: {date}")
    if date_end:
        parts.append(f"date_end: {date_end}")
    if time:
        parts += [f"time: {time}", f"time_end: {time_end}"]
    if type_:
        parts.append(f"type: {type_}")
    if priority in {"primary", "secondary", "experiment"}:
        parts.append(f"priority: {priority}")
    line = "- [ ] " + " — ".join(parts) + "\n"
    if path.exists():
        existing = path.read_text(encoding="utf-8")
        path.write_text(existing.rstrip("\n") + "\n" + line, encoding="utf-8")
    else:
        path.write_text(f"## Activities\n{line}", encoding="utf-8")
    reindex()
    out = {"id": act_id, "title": title, "entity": entity}
    if overlaps:
        out["overlaps"] = [{"title": o["title"], "entity": o["entity"], "time": o["time"],
                            "time_end": o["time_end"]} for o in overlaps]
    return out


def _parse_activities(text: str):
    """Reuse index.parse_checklist without importing index at module load."""
    import index
    return index.parse_checklist(text)


def create_milestone(fields: dict) -> dict:
    title = (fields.get("title") or "").strip()
    date = (fields.get("date") or "").strip()
    if not title:
        raise ActionError("title is required")
    if not date:
        raise ActionError("date is required")
    entity = (fields.get("entity") or "").strip()
    if entity:
        _need_known_entity(entity)
    portfolio = ROOT / "portfolio"
    portfolio.mkdir(exist_ok=True)
    path = portfolio / "milestones.json"
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = {"milestones": []}
        if not isinstance(data, dict):
            data = {"milestones": []}
    else:
        data = {"milestones": []}
    existing_ids = {m.get("id") for m in data.get("milestones", [])}
    ms_id = next_milestone_id(existing_ids)
    ms: dict = {"id": ms_id, "title": title, "date": date,
                "type": (fields.get("type") or "event").strip(),
                "entity_type": (fields.get("entity_type") or "project").strip()}
    if entity:
        ms["entity"] = entity
    if fields.get("date_end"):
        ms["date_end"] = fields["date_end"].strip()
    if fields.get("notes"):
        ms["notes"] = fields["notes"].strip()
    if fields.get("priority") in {"primary", "secondary", "experiment"}:
        ms["priority"] = fields["priority"]
    data.setdefault("milestones", []).append(ms)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    reindex()
    return {"id": ms_id, "title": title}


_MILESTONE_FIELDS = ("title", "date", "date_end", "type", "entity", "entity_type",
                     "notes", "priority")


def _load_milestones(path):
    if not path.exists():
        raise ActionError("milestones.json not found")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raise ActionError("milestones.json is not valid JSON")
    if not isinstance(data, dict) or not isinstance(data.get("milestones"), list):
        raise ActionError("milestones.json has an unexpected shape")
    return data


def update_milestone(ms_id: str, fields: dict) -> dict:
    """Edit one milestone in milestones.json. Empty values clear the field."""
    path = ROOT / "portfolio" / "milestones.json"
    data = _load_milestones(path)
    if str(fields.get("entity") or "").strip():
        _need_known_entity(str(fields["entity"]).strip())
    for m in data["milestones"]:
        if m.get("id") == ms_id:
            for k in _MILESTONE_FIELDS:
                if k not in fields or fields[k] is None:
                    continue
                v = str(fields[k]).strip()
                if v:
                    m[k] = v
                else:
                    m.pop(k, None)
            path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
            reindex()
            return {"id": ms_id}
    raise ActionError(f"milestone '{ms_id}' not found")


def delete_milestone(ms_id: str) -> dict:
    path = ROOT / "portfolio" / "milestones.json"
    data = _load_milestones(path)
    kept = [m for m in data["milestones"] if m.get("id") != ms_id]
    if len(kept) == len(data["milestones"]):
        raise ActionError(f"milestone '{ms_id}' not found")
    data["milestones"] = kept
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    reindex()
    return {"id": ms_id, "deleted": True}


def _roadmap_section_name(raw: str, project_slug: str) -> str:
    section = (raw or "Next").strip() or "Next"
    low = section.lower()
    if low in ROADMAP_SECTION_ALIASES:
        return ROADMAP_SECTION_ALIASES[low]
    sections = subsections_for_doc(load_config(ROOT, project_slug), "roadmap")
    for s in sections:
        if s.lower() == low:
            return s
    raise ActionError(
        f"unknown roadmap section '{section}' — use one of: {', '.join(sections)}"
    )


def read_subsections(project_slug: str) -> dict:
    """Load subsection config; writes default subsections.json if missing."""
    _project_dir(project_slug)
    return subsections_api_payload(ensure_config(ROOT, project_slug))


def _project_doc_path(project_slug: str, doc_key: str) -> Path | None:
    rel = DOC_META.get(doc_key, {}).get("path")
    if not rel or "<" in rel:
        return None
    return _project_dir(project_slug) / rel


def _write_project_doc(project_slug: str, doc_key: str, text: str, *, path: Path) -> str:
    project_dir = _project_dir(project_slug).resolve()
    resolved_path = path.resolve()
    if not resolved_path.is_relative_to(project_dir):
        raise ActionError("document path outside project")
    cfg = load_config(ROOT, project_slug)
    norm, cfg = normalize_doc_text(text, doc_key=doc_key, config=cfg)
    resolved_path.parent.mkdir(parents=True, exist_ok=True)
    resolved_path.write_text(norm, encoding="utf-8")
    save_config(ROOT, project_slug, cfg)
    return norm


def _normalize_doc_strict(project_slug: str, doc_key: str, text: str, cfg: dict) -> str:
    """Normalize markdown to config subsections only — no heading merge."""
    meta = DOC_META[doc_key]
    sections = subsections_for_doc(cfg, doc_key)
    return normalize_markdown(
        text,
        title=meta["title"],
        sections=sections,
        roadmap=doc_key == "roadmap",
    )


def _need_inside_project(project_slug: str, *paths) -> None:
    """Refuse a file that is, or sits behind, a symlink leading out of the project folder."""
    base = os.path.realpath(str(_project_dir(project_slug)))
    for p in paths:
        if not p:
            continue
        real = os.path.realpath(str(p))
        if not real.startswith(base + os.sep) or os.path.normpath(str(p)) != os.path.normpath(real).replace(
                os.path.realpath(str(ROOT)), str(ROOT), 1):
            raise ActionError("path outside project")


def update_subsections(project_slug: str, doc_key: str, titles: list[str]) -> dict:
    _need_inside_project(project_slug, _project_doc_path(project_slug, doc_key),
                         config_path(ROOT, project_slug))
    cfg = set_doc_subsections(load_config(ROOT, project_slug), doc_key, titles)
    save_config(ROOT, project_slug, cfg)
    doc_path = _project_doc_path(project_slug, doc_key)
    if doc_path and doc_path.is_file():
        norm = _normalize_doc_strict(
            project_slug, doc_key, doc_path.read_text(encoding="utf-8"), cfg,
        )
        doc_path.write_text(norm, encoding="utf-8")
    reindex()
    return {"project": project_slug, "doc": doc_key, "subsections": subsections_api_payload(cfg)}


def update_validation_tab(project_slug: str, titles: list[str]) -> dict:
    _project_dir(project_slug)
    try:
        cfg = set_validation_tab_subsections(load_config(ROOT, project_slug), titles)
    except ValueError as exc:
        raise ActionError(str(exc)) from exc
    save_config(ROOT, project_slug, cfg)
    reindex()
    return {
        "project": project_slug,
        "validation_tab": list(validation_tab_subsections(cfg)),
        "subsections": subsections_api_payload(cfg),
    }


def add_subsection(project_slug: str, doc_key: str, title: str) -> dict:
    _need_inside_project(project_slug, _project_doc_path(project_slug, doc_key),
                         config_path(ROOT, project_slug))
    cfg = add_doc_subsection(load_config(ROOT, project_slug), doc_key, title)
    save_config(ROOT, project_slug, cfg)
    doc_path = _project_doc_path(project_slug, doc_key)
    if doc_path and doc_path.is_file():
        norm = _normalize_doc_strict(
            project_slug, doc_key, doc_path.read_text(encoding="utf-8"), cfg,
        )
        doc_path.write_text(norm, encoding="utf-8")
    reindex()
    return {"project": project_slug, "doc": doc_key, "title": title.strip(),
            "subsections": subsections_api_payload(cfg)}


def update_doc_section(project_slug: str, doc_key: str, title: str, body: str) -> dict:
    """Patch one ``##`` subsection body inside intake.md or technical.md."""
    title = (title or "").strip()
    if not title:
        raise ActionError("title required")
    if doc_key not in ("intake", "technical"):
        raise ActionError("doc must be intake or technical")
    _project_dir(project_slug)
    doc_path = _project_doc_path(project_slug, doc_key)
    if not doc_path or not doc_path.is_file():
        raise ActionError(f"{doc_key} file missing — create it first")
    cfg = load_config(ROOT, project_slug)
    sections = tuple(subsections_for_doc(cfg, doc_key))
    if title not in sections:
        raise ActionError(f"subsection '{title}' not in config — add it first")
    meta = DOC_META[doc_key]
    roadmap = doc_key == "roadmap"
    parsed = parse_markdown_sections(doc_path.read_text(encoding="utf-8"), roadmap=roadmap)
    parsed[title] = (body or "").strip()
    chunks = [f"# {meta['title']}", ""]
    for sec in sections:
        chunks.extend([f"## {sec}", "", (parsed.get(sec) or "").strip(), ""])
    draft = "\n".join(chunks).rstrip() + "\n"
    norm = _write_project_doc(project_slug, doc_key, draft, path=doc_path)
    reindex()
    return {
        "project": project_slug,
        "doc": doc_key,
        "title": title,
        "path": str(doc_path.relative_to(ROOT)),
        "chars": len(norm),
    }


def _project_dir(project_slug: str) -> Path:
    return _confined_dir(ROOT / "projects", project_slug, "project")


def _product_dir(product_slug: str) -> tuple[Path, str]:
    """Return (product folder, owning project slug)."""
    product_slug = _safe_component(product_slug, "product slug")
    projects = ROOT / "projects"
    projects_resolved = projects.resolve()
    for proj in sorted(projects.iterdir()) if projects.is_dir() else []:
        if not proj.is_dir():
            continue
        d = proj / "products" / product_slug
        resolved = d.resolve()
        if (not proj.is_symlink() and not d.is_symlink()
                and resolved.is_dir() and resolved.is_relative_to(projects_resolved)):
            return d, proj.name
    raise ActionError(f"product '{product_slug}' not found")


def create_intake(project_slug: str) -> dict:
    project_dir = _project_dir(project_slug)
    intake = project_dir / "strategy" / "intake.md"
    if intake.exists():
        raise ActionError("intake.md already exists")
    cfg = ensure_config(ROOT, project_slug)
    intake.parent.mkdir(parents=True, exist_ok=True)
    intake.write_text(starter_text(cfg, "intake"), encoding="utf-8")
    reindex()
    rel = str(intake.relative_to(ROOT))
    return {"path": rel, "project": project_slug}


def write_intake(project_slug: str, text: str) -> dict:
    """Replace strategy/intake.md (creates parent dirs if needed)."""
    if not (text or "").strip():
        raise ActionError("intake text required")
    intake = _project_dir(project_slug) / "strategy" / "intake.md"
    _write_project_doc(project_slug, "intake", text, path=intake)
    reindex()
    return {"path": str(intake.relative_to(ROOT)), "project": project_slug}


def _evidence_ref(project_slug: str, ref: str) -> str:
    """Composed id an evidence entry may point at: an experiment of this same project."""
    db = _db_module()
    ent = build_id_registry(db.tree(), db.posts(), root=ROOT).resolve(ref)
    if not ent or ent["kind"] != "exp" or (ent.get("ref") or {}).get("project") != project_slug:
        raise ActionError(f"ref '{ref}' is not an experiment of {project_slug}")
    return ent["id"]


def add_evidence(project_slug: str, fields: dict) -> dict:
    """Append one dated entry to the intake's Evidence log. Entries are never edited or removed."""
    text = evidence.clean(fields.get("text") or "")
    if not text:
        raise ActionError("evidence text required")
    ref = (fields.get("ref") or "").strip() or None
    if ref:
        ref = _evidence_ref(project_slug, ref)
    date = (fields.get("date") or "").strip() or datetime.date.today().isoformat()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
        raise ActionError("date must be YYYY-MM-DD")
    intake = _project_doc_path(project_slug, "intake")
    if "Evidence log" not in subsections_for_doc(load_config(ROOT, project_slug), "intake"):
        raise ActionError("this project's intake has no Evidence log section")
    old = intake.read_text(encoding="utf-8") if intake.exists() else \
        starter_text(ensure_config(ROOT, project_slug), "intake")
    new, n = evidence.append(old, text, ref, date)
    _write_project_doc(project_slug, "intake", new, path=intake)
    reindex()
    return {"id": _composed_id(lk_evidence(project_slug, n)), "n": n, "project": project_slug}


def create_technical(project_slug: str) -> dict:
    project_dir = _project_dir(project_slug)
    technical = project_dir / "technical.md"
    if technical.exists():
        raise ActionError("technical.md already exists")
    cfg = ensure_config(ROOT, project_slug)
    technical.write_text(starter_text(cfg, "technical"), encoding="utf-8")
    reindex()
    rel = str(technical.relative_to(ROOT))
    return {"path": rel, "project": project_slug}


def write_technical(project_slug: str, text: str) -> dict:
    """Replace technical.md (creates file if needed)."""
    if not (text or "").strip():
        raise ActionError("technical text required")
    technical = _project_dir(project_slug) / "technical.md"
    _write_project_doc(project_slug, "technical", text, path=technical)
    reindex()
    return {"path": str(technical.relative_to(ROOT)), "project": project_slug}


def create_memo(project_slug: str, memo_type: str, fields: dict | None = None,
                body_extra: dict | None = None) -> dict:
    mtype = (memo_type or "").strip()
    if mtype not in MEMO_TYPES:
        raise ActionError(f"unknown memo type '{mtype}'")
    project_dir = _project_dir(project_slug)
    memo_dir = project_dir / "strategy" / "memos"
    memo_dir.mkdir(parents=True, exist_ok=True)
    version = next_memo_version(memo_dir, mtype)
    merged: dict = {}
    if body_extra:
        merged.update(body_extra)
    if fields:
        merged.update({k: v for k, v in fields.items() if v is not None})
    body = normalize_memo_body(mtype, merged, version=version)
    fname = f"{mtype}-v{version}.json"
    path = memo_dir / fname
    path.write_text(dumps_json(body), encoding="utf-8")
    reindex()
    key = lk_memo(project_slug, mtype, version)
    rel = str(path.relative_to(ROOT))
    return {"id": _composed_id(key), "type": mtype, "version": version, "path": rel, "project": project_slug}


def update_memo(project_slug: str, memo_type: str, version: int, fields: dict) -> dict:
    """Patch an existing memo version JSON in place (same version, not a new one)."""
    mtype = (memo_type or "").strip()
    if mtype not in MEMO_TYPES:
        raise ActionError(f"unknown memo type '{mtype}'")
    memo_dir = _project_dir(project_slug) / "strategy" / "memos"
    path = memo_dir / f"{mtype}-v{int(version)}.json"
    if not path.exists():
        raise ActionError(f"memo '{mtype}' v{version} not found")
    existing = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(existing, dict):
        raise ActionError(f"memo '{mtype}' v{version} is not a JSON object")
    merged = dict(existing)
    if fields:
        merged.update({k: v for k, v in fields.items() if v is not None})
    body = normalize_memo_body(mtype, merged, version=int(version))
    path.write_text(dumps_json(body), encoding="utf-8")
    reindex()
    rel = str(path.relative_to(ROOT))
    return {
        "id": _composed_id(lk_memo(project_slug, mtype, int(version))),
        "type": mtype,
        "version": int(version),
        "path": rel,
        "project": project_slug,
    }


def delete_memo(project_slug: str, memo_type: str, version: int) -> dict:
    mtype = (memo_type or "").strip()
    if mtype not in MEMO_TYPES:
        raise ActionError(f"unknown memo type '{mtype}'")
    path = _project_dir(project_slug) / "strategy" / "memos" / f"{mtype}-v{int(version)}.json"
    if not path.exists():
        raise ActionError(f"memo '{mtype}' v{version} not found")
    path.unlink()
    reindex()
    return {"type": mtype, "version": int(version), "project": project_slug, "deleted": True}


def create_experiment(project_slug: str, fields: dict) -> dict:
    assumption = (fields.get("assumption") or fields.get("assumption_under_test") or "").strip()
    if not assumption:
        raise ActionError("assumption is required")
    project_dir = _project_dir(project_slug)
    exp_dir = project_dir / "strategy" / "experiments"
    exp_dir.mkdir(parents=True, exist_ok=True)
    stem = (fields.get("stem") or "").strip() or next_experiment_stem(exp_dir)
    stem = _slugify(stem) or next_experiment_stem(exp_dir)
    path = exp_dir / f"{stem}.json"
    if path.exists():
        raise ActionError(f"experiment '{stem}' already exists")
    # A metric source is saved only by set_metric, which checks the action is read-only.
    body = normalize_experiment_body({**{k: v for k, v in fields.items() if k != "metric"},
                                      "assumption": assumption})
    if fields.get("strategy"):
        _require_strategy(project_slug, fields["strategy"])
    path.write_text(dumps_json(body), encoding="utf-8")
    reindex()
    rel = str(path.relative_to(ROOT))
    return {
        "id": _composed_id(lk_experiment(project_slug, stem)),
        "stem": stem,
        "path": rel,
        "project": project_slug,
        "assumption": assumption,
    }


def update_experiment(project_slug: str, stem: str, fields: dict) -> dict:
    """Patch an existing experiment JSON by stem."""
    stem = _safe_component(stem, "experiment stem")
    path = _project_dir(project_slug) / "strategy" / "experiments" / f"{stem}.json"
    if not path.exists():
        raise ActionError(f"experiment '{stem}' not found")
    body = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(body, dict):
        raise ActionError(f"experiment '{stem}' is not a JSON object")
    patch = dict(body)
    for key in ("assumption", "assumption_under_test", "success_criteria", "kill_criteria", "status", "strategy"):
        if key in fields and fields[key] is not None:
            patch[key] = fields[key]
    if fields.get("strategy"):
        _require_strategy(project_slug, fields["strategy"])
    body = normalize_experiment_body(patch)
    path.write_text(dumps_json(body), encoding="utf-8")
    reindex()
    rel = str(path.relative_to(ROOT))
    return {
        "id": _composed_id(lk_experiment(project_slug, stem)),
        "stem": stem,
        "path": rel,
        "project": project_slug,
    }


def set_metric(project_slug: str, stem: str, fields: dict | None, clear: bool = False,
               catalog=None, read_only=None, paid=frozenset()) -> dict:
    """Set (or clear) an experiment's metric source. Only a read-only app action is saved: the
    scoring job reads it with no click, so a write action is rejected here with the reason.
    `catalog` (toolkit slugs), `read_only` (slug -> Composio's tag) and `paid` stand in for
    Composio's own answers (tests); left out, they are asked of Composio."""
    stem = _safe_component(stem, "experiment stem")
    path = _project_dir(project_slug) / "strategy" / "experiments" / f"{stem}.json"
    if not path.exists():
        raise ActionError(f"experiment '{stem}' not found")
    body = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(body, dict):
        raise ActionError(f"experiment '{stem}' is not a JSON object")
    body.pop("metric", None)
    if not clear:
        metric = normalize_metric(fields)
        if metric is None:
            raise ActionError("metric needs app, action, path (dotted keys like data.followers) "
                              f"and window_days 1 to {METRIC_MAX_WINDOW_DAYS}")
        if catalog is None:
            from core import composio
            try:
                items = composio.catalog()
            except composio.ComposioError as e:
                raise ActionError(f"cannot check the action is read-only ({e})")
            catalog, paid = [a["slug"] for a in items], composio.paid_read_toolkits(items)
            read_only = read_only or composio.tool_read_only
        elif read_only is None:
            raise ActionError("cannot check the action is read-only")
        why = app_rules.metric_problem(metric["app"], metric["action"], catalog, metric["args"], read_only, paid)
        if why:
            raise ActionError(f"metric rejected: {why}")
        body["metric"] = metric
    path.write_text(dumps_json(normalize_experiment_body(body)), encoding="utf-8")
    reindex()
    return {
        "id": _composed_id(lk_experiment(project_slug, stem)),
        "stem": stem,
        "path": str(path.relative_to(ROOT)),
        "project": project_slug,
        "metric": body.get("metric"),
    }


def delete_experiment(project_slug: str, stem: str) -> dict:
    stem = _safe_component(stem, "experiment stem")
    path = _project_dir(project_slug) / "strategy" / "experiments" / f"{stem}.json"
    if not path.exists():
        raise ActionError(f"experiment '{stem}' not found")
    path.unlink()
    reindex()
    return {"stem": stem, "project": project_slug, "deleted": True}


# --------------------------------------------------------------------------- #
# GTM (sec07) — phases (time-boxed bets) + strategies (channel-motions).
# Lens model: experiments tag up to a strategy stem; nothing duplicated here.
# --------------------------------------------------------------------------- #

def _require_strategy(project_slug: str, stem: str) -> str:
    _project_dir(project_slug)  # a symlinked project folder is refused here too
    stem = _safe_component(stem, "strategy stem")
    known = {s["stem"] for s in gtm_store.load_strategies(ROOT, project_slug)}
    if stem not in known:
        raise ActionError(f"strategy '{stem}' not found in project '{project_slug}'")
    return stem


def create_phase(project_slug: str, name: str, goal: str = "", timebox: str = "",
                 status: str = "planned") -> dict:
    _project_dir(project_slug)
    name = (name or "").strip()
    if not name:
        raise ActionError("phase name is required")
    phases = gtm_store.load_phases(ROOT, project_slug)
    pid = gtm_store.slugify(name)
    if any(p["id"] == pid for p in phases):
        raise ActionError(f"phase '{pid}' already exists")
    phases.append({"id": pid, "name": name, "goal": goal or "",
                   "timebox": timebox or "", "status": status or "planned"})
    gtm_store.save_phases(ROOT, project_slug, phases)
    reindex()
    return {"id": _composed_id(lk_phase(project_slug, pid)), "phase": pid,
            "project": project_slug, "name": name}


def _bare_phase_id(project_slug: str, phase_id: str, phases: list) -> str:
    """The phase's own id, also from the composed id create-phase hands out (pr1.sec07.ph1)."""
    phase_id = (phase_id or "").strip()
    if "." in phase_id:
        for p in phases:
            if _composed_id(lk_phase(project_slug, p["id"])) == phase_id:
                return p["id"]
    return phase_id


def update_phase(project_slug: str, phase_id: str, fields: dict) -> dict:
    _project_dir(project_slug)
    phases = gtm_store.load_phases(ROOT, project_slug)
    phase_id = _bare_phase_id(project_slug, phase_id, phases)
    match = next((p for p in phases if p["id"] == phase_id), None)
    if match is None:
        raise ActionError(f"phase '{phase_id}' not found")
    for key in ("name", "goal", "timebox", "status"):
        if key in fields and fields[key] is not None:
            match[key] = fields[key]
    gtm_store.save_phases(ROOT, project_slug, phases)
    reindex()
    return {"id": _composed_id(lk_phase(project_slug, phase_id)), "phase": phase_id,
            "project": project_slug}


def delete_phase(project_slug: str, phase_id: str) -> dict:
    _project_dir(project_slug)
    phases = gtm_store.load_phases(ROOT, project_slug)
    phase_id = _bare_phase_id(project_slug, phase_id, phases)
    if not any(p["id"] == phase_id for p in phases):
        raise ActionError(f"phase '{phase_id}' not found")
    phases = [p for p in phases if p["id"] != phase_id]
    gtm_store.save_phases(ROOT, project_slug, phases)
    # Orphaned strategies keep their file; they re-home under the tab until re-phased.
    reindex()
    return {"phase": phase_id, "project": project_slug, "deleted": True}


def create_strategy(project_slug: str, phase: str, channel: str, hypothesis: str,
                    name: str = "", primary: bool = False) -> dict:
    _project_dir(project_slug)
    phase = (phase or "").strip()
    if phase and not any(p["id"] == phase for p in gtm_store.load_phases(ROOT, project_slug)):
        raise ActionError(f"phase '{phase}' not found")
    name = (name or "").strip() or (channel or "").strip()
    if not name:
        raise ActionError("strategy needs a name or channel")
    stem = gtm_store.slugify(name)
    if gtm_store.strategy_path(ROOT, project_slug, stem).exists():
        raise ActionError(f"strategy '{stem}' already exists")
    if primary:
        _clear_primary(project_slug)
    gtm_store.save_strategy(ROOT, project_slug, {
        "stem": stem, "phase": phase, "name": name, "channel_slug": channel or "",
        "hypothesis": hypothesis or "", "status": "proposed", "primary": bool(primary),
    })
    reindex()
    return {"id": _composed_id(lk_strategy(project_slug, stem)), "stem": stem,
            "project": project_slug, "name": name}


def update_strategy(project_slug: str, stem: str, fields: dict) -> dict:
    stem = _require_strategy(project_slug, stem)
    path = gtm_store.strategy_path(ROOT, project_slug, stem)
    body = json.loads(path.read_text(encoding="utf-8"))
    if fields.get("phase") and not any(
            p["id"] == fields["phase"] for p in gtm_store.load_phases(ROOT, project_slug)):
        raise ActionError(f"phase '{fields['phase']}' not found")
    if fields.get("primary"):
        _clear_primary(project_slug)
    for key in ("name", "phase", "channel_slug", "channel", "hypothesis", "status", "notes", "primary"):
        if key in fields and fields[key] is not None:
            body["channel_slug" if key == "channel" else key] = fields[key]
    body["stem"] = stem
    gtm_store.save_strategy(ROOT, project_slug, body)
    reindex()
    return {"id": _composed_id(lk_strategy(project_slug, stem)), "stem": stem,
            "project": project_slug}


def delete_strategy(project_slug: str, stem: str) -> dict:
    stem = _require_strategy(project_slug, stem)
    gtm_store.strategy_path(ROOT, project_slug, stem).unlink()
    reindex()
    return {"stem": stem, "project": project_slug, "deleted": True}


def _clear_primary(project_slug: str) -> None:
    """At most one primary channel-motion per project."""
    for s in gtm_store.load_strategies(ROOT, project_slug):
        if s.get("primary"):
            path = gtm_store.strategy_path(ROOT, project_slug, s["stem"])
            body = json.loads(path.read_text(encoding="utf-8"))
            body["primary"] = False
            gtm_store.save_strategy(ROOT, project_slug, body)


def create_product(project_slug: str, slug: str, fields: dict) -> dict:
    slug = _safe_component(slug, "product slug")
    project_dir = _project_dir(project_slug)
    prod_dir = project_dir / "products" / slug
    if prod_dir.exists():
        raise ActionError(f"product '{slug}' already exists")
    _need_free_slug(slug)
    name = _one_line(fields.get("name") or slug, "name")
    ptype = _one_line(fields.get("type") or "app", "type")
    status = _one_line(fields.get("status") or "idea", "status")
    prod_dir.mkdir(parents=True)
    (prod_dir / "product.md").write_text(
        f"---\nname: {name}\ntype: {ptype}\nstatus: {status}\n---\n",
        encoding="utf-8",
    )
    cfg = ensure_config(ROOT, project_slug)
    (prod_dir / "roadmap.md").write_text(starter_text(cfg, "roadmap"), encoding="utf-8")
    reindex()
    return {"id": _composed_id(lk_prod(slug)), "slug": slug, "name": name, "project": project_slug}


def feature_roadmap_details(product_slug: str) -> dict[str, dict]:
    """Parse roadmap.md checklist — map title → why, priority, etc."""
    try:
        prod_dir, _ = _product_dir(product_slug)
    except ActionError:
        return {}
    roadmap = prod_dir / "roadmap.md"
    if not roadmap.is_file():
        return {}
    from index import parse_checklist  # noqa: WPS433
    out: dict[str, dict] = {}
    for sec, _checked, title, flds in parse_checklist(roadmap.read_text(encoding="utf-8")):
        if title:
            row = dict(flds)
            if sec:
                row["roadmap_section"] = sec
            out[title] = row
    return out


def enrich_project_memos(memos: list[dict], project_slug: str, registry) -> list[dict]:
    from core.ids import lk_memo  # noqa: WPS433
    rows = []
    for m in memos or []:
        row = dict(m)
        if registry:
            row["id"] = registry.lookup.get(
                lk_memo(project_slug, m.get("type") or "", int(m.get("version") or 0)))
        rows.append(row)
    return rows


def enrich_project_experiments(experiments: list[dict], project_slug: str, registry) -> list[dict]:
    from core.ids import experiment_stem_from_path, lk_experiment  # noqa: WPS433
    rows = []
    for x in experiments or []:
        row = dict(x)
        stem = row.get("stem") or experiment_stem_from_path(row.get("file_path") or "") or ""
        if registry and stem:
            row["id"] = registry.lookup.get(lk_experiment(project_slug, stem))
        rows.append(row)
    return rows


def enrich_project_features(features: list[dict], registry) -> list[dict]:
    """Attach canonical id + roadmap why/priority from live files."""
    from core.ids import lk_feature, slug_key  # noqa: WPS433
    by_prod: dict[str, dict[str, dict]] = {}
    rows = []
    for f in features or []:
        row = dict(f)
        pslug = row.get("product_slug") or ""
        title = row.get("title") or ""
        if pslug not in by_prod:
            by_prod[pslug] = feature_roadmap_details(pslug)
        det = by_prod[pslug].get(title) or {}
        if det.get("why"):
            row["why"] = det["why"]
        if det.get("priority") and not row.get("priority"):
            row["priority"] = det["priority"]
        if det.get("roadmap_section"):
            row["roadmap_section"] = det["roadmap_section"]
        tk = slug_key(title)
        row["id"] = (registry.lookup.get(lk_feature(pslug, tk)) if registry else None)
        rows.append(row)
    return rows


def _append_roadmap_line(text: str, section: str, line: str) -> str:
    """Append one checklist line under `## {section}`, creating the heading if needed."""
    marker = f"## {section}"
    if marker not in text:
        text = text.rstrip() + f"\n\n{marker}\n\n"
    if not text.endswith("\n"):
        text += "\n"
    return text + line + "\n"


_FEATURE_LINE_RE = re.compile(r"^(\s*- \[[ xX]\])\s*(.*)$")


def _parse_feature_line(line: str):
    """Return (checkbox_prefix, title, why, priority) or None if not a checklist line."""
    m = _FEATURE_LINE_RE.match(line)
    if not m:
        return None
    checkbox, rest = m.group(1), m.group(2)
    parts = [p.strip() for p in rest.split(" — ")]
    title = parts[0]
    why, priority = "", ""
    for part in parts[1:]:
        pm = re.match(r"^priority:\s*(.*)$", part, re.I)
        if pm:
            priority = pm.group(1).strip().lower()
        elif not why:
            why = part
    return checkbox, title, why, priority


def _find_feature_line_index(lines: list, feature_id: str, product_slug: str = None):
    """Return (line_index, current_section) for the checklist line whose
    slug_key(title) matches feature_id, or the composed id add-feature hands out
    (pr1.sec05.pd1.ft2), or None."""
    registry = None
    if product_slug and "." in feature_id:
        db = _db_module()
        registry = build_id_registry(db.tree(), db.posts(), root=ROOT)
    section = None
    for i, line in enumerate(lines):
        if line.startswith("## "):
            section = line[3:].strip()
            continue
        parsed = _parse_feature_line(line)
        if not parsed:
            continue
        key = slug_key(parsed[1])
        if key == feature_id or (registry and registry.get(lk_feature(product_slug, key)) == feature_id):
            return i, section
    return None


def _check_feature_text_safe(title: str, why: str) -> None:
    """Roadmap checklist lines serialize as `- [ ] Title — why — priority: X`,
    splitting on the literal ' — ' separator (fileops._parse_feature_line,
    index.parse_checklist) — a title or why containing that exact sequence
    would be split apart and partially lost on the next edit."""
    if " — " in title or " — " in why:
        raise ActionError("title/why can't contain ' — ' — it's the roadmap line's field separator")


def add_feature(product_slug: str, fields: dict) -> dict:
    title = (fields.get("title") or "").strip()
    if not title:
        raise ActionError("title is required")
    why = (fields.get("why") or "").strip()
    _check_feature_text_safe(title, why)
    prod_dir, project_slug = _product_dir(product_slug)
    roadmap = prod_dir / "roadmap.md"
    if not roadmap.exists():
        cfg = ensure_config(ROOT, project_slug)
        roadmap.write_text(starter_text(cfg, "roadmap"), encoding="utf-8")
    section = _roadmap_section_name(fields.get("section") or "Next", project_slug)
    text = _write_project_doc(project_slug, "roadmap", roadmap.read_text(encoding="utf-8"), path=roadmap)
    line = f"- [ ] {title}"
    if why:
        line += f" — {why}"
    prio = (fields.get("priority") or "").strip().lower()
    if prio in FEATURE_PRIORITIES:
        line += f" — priority: {prio}"
    text = _append_roadmap_line(text, section, line)
    _write_project_doc(project_slug, "roadmap", text, path=roadmap)
    reindex()
    return {
        "id": _composed_id(lk_feature(product_slug, slug_key(title))),
        "title": title,
        "product": product_slug,
        "section": section,
    }


def update_feature(product_slug: str, feature_id: str, fields: dict) -> dict:
    feature_id = (feature_id or "").strip()
    if not feature_id:
        raise ActionError("feature id is required")
    prod_dir, project_slug = _product_dir(product_slug)
    roadmap = prod_dir / "roadmap.md"
    if not roadmap.exists():
        raise ActionError(f"feature '{feature_id}' not found")
    lines = roadmap.read_text(encoding="utf-8").splitlines()
    found = _find_feature_line_index(lines, feature_id, product_slug)
    if not found:
        raise ActionError(f"feature '{feature_id}' not found")
    idx, cur_section = found
    checkbox, cur_title, cur_why, cur_priority = _parse_feature_line(lines[idx])
    title = (fields.get("title") or cur_title).strip()
    why = fields.get("why") if fields.get("why") is not None else cur_why
    _check_feature_text_safe(title, why)
    priority = (fields.get("priority") if fields.get("priority") is not None else cur_priority) or ""
    priority = priority.strip().lower()
    new_line = f"{checkbox} {title}"
    if why:
        new_line += f" — {why}"
    if priority in FEATURE_PRIORITIES:
        new_line += f" — priority: {priority}"
    target_section = cur_section
    if fields.get("section"):
        target_section = _roadmap_section_name(fields["section"], project_slug)
    if target_section != cur_section:
        del lines[idx]
        marker = f"## {target_section}"
        section_start = next((i for i, ln in enumerate(lines) if ln.strip() == marker), None)
        if section_start is None:
            if lines and lines[-1].strip():
                lines.append("")
            lines += [marker, "", new_line]
        else:
            insert_at = next(
                (i for i in range(section_start + 1, len(lines)) if lines[i].startswith("## ")),
                len(lines),
            )
            while insert_at > section_start + 1 and not lines[insert_at - 1].strip():
                insert_at -= 1
            lines.insert(insert_at, new_line)
        text = "\n".join(lines)
        if not text.endswith("\n"):
            text += "\n"
    else:
        lines[idx] = new_line
        text = "\n".join(lines) + "\n"
    _write_project_doc(project_slug, "roadmap", text, path=roadmap)
    reindex()
    return {
        "id": _composed_id(lk_feature(product_slug, slug_key(title))),
        "title": title,
        "product": product_slug,
        "section": target_section,
    }


def delete_feature(product_slug: str, feature_id: str) -> dict:
    feature_id = (feature_id or "").strip()
    if not feature_id:
        raise ActionError("feature id is required")
    prod_dir, _project_slug = _product_dir(product_slug)
    roadmap = prod_dir / "roadmap.md"
    if not roadmap.exists():
        raise ActionError(f"feature '{feature_id}' not found")
    lines = roadmap.read_text(encoding="utf-8").splitlines()
    found = _find_feature_line_index(lines, feature_id, product_slug)
    if not found:
        raise ActionError(f"feature '{feature_id}' not found")
    idx, _section = found
    del lines[idx]
    roadmap.write_text("\n".join(lines) + "\n", encoding="utf-8")
    reindex()
    return {"feature_id": feature_id, "product": product_slug, "deleted": True}


def write_roadmap(product_slug: str, text: str) -> dict:
    """Replace products/<slug>/roadmap.md."""
    if not (text or "").strip():
        raise ActionError("roadmap text required")
    prod_dir, project_slug = _product_dir(product_slug)
    roadmap = prod_dir / "roadmap.md"
    _write_project_doc(project_slug, "roadmap", text, path=roadmap)
    reindex()
    return {
        "path": str(roadmap.relative_to(ROOT)),
        "product": product_slug,
        "project": project_slug,
    }


def read_authored_json(relpath):
    """Read an authored JSON file (memo/experiment body) by its os.db-relative path."""
    if not relpath:
        return None
    root = ROOT.resolve()
    try:
        f = (root / str(relpath)).resolve()
    except (OSError, RuntimeError):
        return None
    if not f.is_relative_to(root) or not f.is_file():
        return None
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"_error": "file is not valid JSON"}


def _plan_args(profile_slug, params):
    """Build the generate.py plan argv from UI params. Raises if period is missing."""
    period = (params.get("period") or "").strip()
    if not period:
        raise ActionError("a period is required (e.g. '2026-07-01 to 2026-07-14')")
    args = [sys.executable, str(_GENERATE_SCRIPT),
            "--workspace", str(ROOT), "plan", profile_slug, f"--period={period}"]
    # "--flag=value" throughout: option-looking text stays text, never a flag.
    platforms = (params.get("platforms") or "").strip()
    if platforms:
        args += [f"--platforms={platforms}"]
    cadence = params.get("cadence")
    if cadence not in (None, ""):
        args += [f"--cadence={int(cadence)}"]
    focus = (params.get("focus") or "").strip()
    if focus:
        args += [f"--focus={focus}"]
    brief_counts = (params.get("brief_counts") or "").strip()
    if brief_counts:
        args += [f"--brief-counts={brief_counts}"]
    voice_counts = (params.get("voice_counts") or "").strip()
    if voice_counts:
        args += [f"--voice-counts={voice_counts}"]
    if params.get("dates"):
        args += ["--dates"]
    return args


def run_plan(profile_slug, params):
    """Generate a content calendar for a profile via claude -p, then re-index."""
    _profile_dir(profile_slug)
    res = subprocess.run(_plan_args(profile_slug, params), capture_output=True, text=True)
    if res.returncode != 0:
        raise ActionError(f"plan job failed: {(res.stderr or res.stdout).strip()[:800]}")
    reindex()
    return {"profile_slug": profile_slug, "stdout": res.stdout.strip()[:400]}


# last30days research signal — real online-discourse evidence for problem-validation.
# Zero-config sources only (no API keys): reddit, hackernews, polymarket, github.
LAST30DAYS_SCRIPT = Path.home() / ".agents" / "skills" / "last30days" / "scripts" / "last30days.py"
LAST30DAYS_ZERO_CONFIG_SOURCES = "reddit,hackernews,polymarket,github"
LAST30DAYS_TIMEOUT_SECONDS = 90


def research_signal(query, max_clusters=5):
    """Pull real online-discourse signal for a topic via the last30days engine.

    Runs the engine headlessly (bypasses its agent-driven SKILL.md synthesis)
    and condenses to the top `max_clusters` by engagement, so this stays cheap
    to fold into a memo's evidence list (e.g. problem-validation evidence).
    """
    if not query or not query.strip():
        raise ActionError("query is required")
    if not LAST30DAYS_SCRIPT.exists():
        raise ActionError(
            "last30days not installed — run `npx skills add mvanhorn/last30days-skill -g`"
        )
    # The query is bot-written: after "--" it can only be the topic, never a flag
    # (--output=<path>, --publish, a cookie switch).
    cmd = [sys.executable, str(LAST30DAYS_SCRIPT), "--emit=json",
           "--auto-resolve", "--no-browser-cookies",
           "--search", LAST30DAYS_ZERO_CONFIG_SOURCES, "--days", "30", "--", query.strip()]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=LAST30DAYS_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired as exc:
        raise ActionError(
            f"last30days research timed out after {LAST30DAYS_TIMEOUT_SECONDS}s"
        ) from exc
    if res.returncode != 0:
        raise ActionError(f"last30days research failed: {(res.stderr or res.stdout).strip()[:800]}")
    try:
        data = json.loads(res.stdout)
    except json.JSONDecodeError as exc:
        raise ActionError(f"last30days returned non-JSON output: {res.stdout.strip()[:400]}") from exc

    clusters = sorted(
        data.get("clusters") or [],
        key=lambda c: c.get("engagement_total") or 0,
        reverse=True,
    )[:max_clusters]
    return {
        "query": query.strip(),
        "generated_at": data.get("generated_at"),
        "clusters": [
            {
                "title": c.get("title"),
                "summary": c.get("summary"),
                "sources": c.get("sources") or [],
                "engagement_total": c.get("engagement_total") or 0,
            }
            for c in clusters
        ],
    }
