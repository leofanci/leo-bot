"""deliverables.py — the work a workspace's teammates hand you (Slack-style files).

A deliverable is a saved file under projects/<slug>/: a memo, an experiment,
the company intake, the technical doc, a roadmap, the go-to-market plan, a
content plan, post briefs, a profile's voice. The six project tabs still exist
underneath as storage; the user never has to think in them:

- chat: the server snapshots the project folder before a bot turn and turns
  what changed into cards ("CMO: Positioning memo v2 → Open")
- canvas: the Files list, newest first, filterable by kind

Each item: {key, kind, label, link (canvas route), focus (composed id of the
card to scroll to, or None), updated, profile (for post links), post (the post
id, for a single brief)}. Content plans and briefs open the Content view, focused
on the profile's card or the post's row.
"""
import json
from pathlib import Path

from core.ids import build_id_registry, lk_doc, lk_experiment, lk_memo, lk_post, lk_prof
from core.project_schemas import MEMO_SECTION

KINDS = ("memo", "experiment", "doc", "roadmap", "plan", "content", "brief", "voice")
_SKIP_TOP = {"team"}                         # bot state/memory: not work product
_SKIP_FILES = {"subsections.json", "project.md"}


def _project_dir(root, slug):
    if not isinstance(slug, str) or not slug or slug in (".", "..") or "/" in slug or "\\" in slug:
        raise ValueError("invalid project slug")
    return Path(root) / "projects" / slug


def snapshot(root, slug):
    """{relative path: mtime} for every work file of the project."""
    base = _project_dir(root, slug)
    out = {}
    if not base.is_dir():
        return out
    for f in base.rglob("*"):
        if f.is_symlink() or not f.is_file():  # a link could point outside the project
            continue
        rel = f.relative_to(base).as_posix()
        if rel.split("/", 1)[0] in _SKIP_TOP or rel in _SKIP_FILES or f.name.startswith("."):
            continue
        out[rel] = f.stat().st_mtime
    return out


def _read_json(path):
    if path.is_symlink():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _title(text):
    return text.replace("-", " ").capitalize()


def _names(projects, slug):
    """{profile/product slug: display name} for the project."""
    p = next((x for x in projects or [] if x.get("slug") == slug), {})
    return {x["slug"]: x.get("name") or x["slug"] for x in (p.get("profiles") or []) + (p.get("products") or [])}


def describe(root, slug, rel, reg=None, mtime=None, names=None):
    """One file → its deliverable, or None when it isn't one."""
    base = _project_dir(root, slug)
    names = names or {}
    name = lambda s: names.get(s) or _title(s)  # noqa: E731
    parts = rel.split("/")
    get = (lambda key: reg.get(key)) if reg is not None else (lambda key: None)
    item = None
    if rel == "strategy/intake.md":
        item = {"key": "doc:intake", "kind": "doc", "label": "Company intake",
                "link": f"#/project/{slug}/validation", "focus": get(lk_doc(slug, "intake"))}
    elif rel == "technical.md":
        item = {"key": "doc:technical", "kind": "doc", "label": "Technical doc",
                "link": f"#/project/{slug}/technical", "focus": get(lk_doc(slug, "technical"))}
    elif parts[:2] == ["strategy", "memos"] and rel.endswith(".json") and "-v" in parts[-1]:
        mtype, _, ver = parts[-1][:-5].rpartition("-v")
        if mtype and ver.isdigit():
            status = _read_json(base / rel).get("status") or "proposed"
            item = {"key": f"memo:{mtype}:{ver}", "kind": "memo",
                    "label": f"{_title(mtype)} memo v{ver}", "status": status,
                    "link": f"#/project/{slug}/{MEMO_SECTION.get(mtype, 'overview')}",
                    "focus": get(lk_memo(slug, mtype, int(ver)))}
    elif parts[:2] == ["strategy", "experiments"] and rel.endswith(".json"):
        stem = parts[-1][:-5]
        body = _read_json(base / rel)
        what = (body.get("assumption") or stem).strip()
        item = {"key": f"experiment:{stem}", "kind": "experiment",
                "label": f"Experiment: {what[:70]}{'…' if len(what) > 70 else ''}",
                "status": body.get("status"),
                "link": f"#/project/{slug}/experiments", "focus": get(lk_experiment(slug, stem))}
    elif parts[0] == "gtm":
        item = {"key": "gtm", "kind": "plan", "label": "Go-to-market plan",
                "link": f"#/project/{slug}/gtm", "focus": None}
    elif parts[0] == "products" and len(parts) == 3 and parts[2] in ("roadmap.md", "product.md"):
        item = {"key": f"roadmap:{parts[1]}", "kind": "roadmap", "label": f"Roadmap: {name(parts[1])}",
                "link": f"#/project/{slug}/product", "focus": None}
    elif parts[0] == "profiles" and len(parts) >= 3:
        prof = parts[1]
        if parts[2] == "content" and len(parts) == 4 and parts[3].startswith("plan-"):
            n = len(_read_json(base / rel).get("posts") or [])
            item = {"key": f"content:{prof}", "kind": "content",
                    "label": f"Content plan: {name(prof)} ({n} posts)",
                    "link": "#/content", "focus": get(lk_prof(prof)), "profile": prof}
        elif parts[2] == "content" and len(parts) == 5 and parts[3] == "briefs" and rel.endswith(".json"):
            pid = parts[4][:-5]
            item = {"key": f"brief:{pid}", "kind": "brief", "label": f"Post brief {pid}",
                    "link": "#/content", "focus": get(lk_post(pid)), "profile": prof, "post": pid}
        elif parts[2] in ("brief-specs", "voices", "profile.md"):
            item = {"key": f"voice:{prof}", "kind": "voice", "label": f"Voice & brief spec: {name(prof)}",
                    "link": f"#/profile/{prof}/setup", "focus": None, "profile": prof}
    if item:
        item["updated"] = mtime
    return item


def _registry(projects, posts, root):
    try:
        return build_id_registry(projects, posts, root=root)
    except Exception:  # noqa: BLE001 — ids only sharpen the link; cards still work without
        return None


def _collapse(items, names=None, reg=None):
    """One card per deliverable; many post briefs from one turn become one card."""
    seen, out, briefs = set(), [], {}
    for it in items:
        if it["key"] in seen:
            continue
        seen.add(it["key"])
        if it["kind"] == "brief":
            briefs.setdefault(it.get("profile"), []).append(it)
            continue
        out.append(it)
    for prof, group in briefs.items():
        if len(group) == 1:
            out.append(group[0])
        else:
            out.append({"key": f"briefs:{prof}", "kind": "brief", "label": f"{len(group)} post briefs: {(names or {}).get(prof) or _title(prof or '')}",
                        "link": "#/content", "focus": reg.get(lk_prof(prof)) if reg is not None and prof else None,
                        "profile": prof,
                        "updated": max(g.get("updated") or 0 for g in group)})
    return out


def changed(root, slug, before, projects, posts):
    """Deliverables created or modified since `before` (a snapshot)."""
    after = snapshot(root, slug)
    rels = [r for r, t in after.items() if before.get(r) != t]
    if not rels:
        return []
    reg, names = _registry(projects, posts, root), _names(projects, slug)
    items = [describe(root, slug, r, reg, after[r], names) for r in sorted(rels)]
    return _collapse([i for i in items if i], names, reg)


def list_files(root, slug, projects, posts):
    """Every deliverable of the project, newest first (the Files list)."""
    snap = snapshot(root, slug)
    reg, names = _registry(projects, posts, root), _names(projects, slug)
    best = {}
    for rel, t in snap.items():
        it = describe(root, slug, rel, reg, t, names)
        if it and it["kind"] != "brief" and (it["key"] not in best or t > best[it["key"]]["updated"]):
            best[it["key"]] = it
    return sorted(best.values(), key=lambda i: i.get("updated") or 0, reverse=True)
