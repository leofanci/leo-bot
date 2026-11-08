#!/usr/bin/env python3
"""index.py — rebuild os.db (the DERIVED, DISPOSABLE index) from source files.

The authored files are the source of truth. This script WIPES os.db and fully
repopulates it from the workspace, so a rebuild is always safe and idempotent.
Never hand-edit os.db; never let anything but this script write its structure.

Usage:
    python3 index.py [WORKSPACE_ROOT]     # default: current directory

Reads, under WORKSPACE_ROOT:
    projects/<slug>/project.md                      -> project entities
    projects/<slug>/profiles/<slug>/profile.md      -> profile entities
    projects/<slug>/profiles/<slug>/channels/<slug>/channel.md -> channel entities
    projects/<slug>/products/<slug>/product.md      -> product entities
    projects/<slug>/strategy/memos/*.json           -> memos (metadata only)
    projects/<slug>/strategy/experiments/*.json     -> experiments
    projects/<slug>/profiles/<slug>/content/plan-*.json (+ briefs/) -> posts
    projects/<slug>/products/<slug>/roadmap.md      -> features
    portfolio/activities.md                         -> activities
    portfolio/milestones.json                       -> milestones

Writes WORKSPACE_ROOT/database/data/os.db. Entities are inserted FIRST so every
FK resolves; an unresolved slug aborts the whole rebuild (no orphan rows).
"""

import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
MIGRATIONS_DIR = HERE / "database" / "migrations"   # schema lives with the code
DB_RELPATH = Path("database") / "data" / "os.db"    # generated under the workspace root

from core.project_schemas import MEMO_TYPES, normalize_workspace_artifacts  # noqa: E402
EXPERIMENT_STATUSES = {"planned", "running", "done"}
EXPERIMENT_DECISIONS = {"persist", "pivot", "kill"}
POST_STATUSES = {
    "planned", "approved_slot", "briefed", "approved",
    "published", "rejected",
}
ACTIVITY_STATUSES = {"planned", "running", "blocked", "done"}
PRIORITIES = {"critical", "high", "normal", "low"}


class IndexError_(Exception):
    """Fatal indexing problem — abort the rebuild loudly."""


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #
def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def mtime_iso(path: Path):
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(timespec="seconds")
    except OSError:
        return now_iso()


def rel(path: Path, root: Path):
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def clean_val(raw: str):
    """Strip an inline ' # comment' and surrounding whitespace; '' -> None."""
    if raw is None:
        return None
    v = re.sub(r"\s+#.*$", "", raw).strip()
    if v == "" or v.lower() in ("null", "none", "n/a"):
        return None
    return v


def first_int(raw):
    if raw is None:
        return None
    m = re.search(r"-?\d+", str(raw))
    return int(m.group()) if m else None


def load_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise IndexError_(f"{path}: invalid JSON ({exc})")


# --------------------------------------------------------------------------- #
# markdown parsers
# --------------------------------------------------------------------------- #
CHECKLIST_RE = re.compile(r"^\s*- \[( |x|X)\]\s*(.*)$")


def parse_checklist(text: str):
    """Yield (section, checked, title, fields) for every checklist line."""
    section = None
    for line in text.splitlines():
        if line.startswith("## "):
            section = line[3:].strip()
            continue
        m = CHECKLIST_RE.match(line)
        if not m:
            continue
        checked = m.group(1).lower() == "x"
        parts = [p.strip() for p in m.group(2).split(" — ")]
        title = parts[0].strip()
        fields = {}
        for part in parts[1:]:
            fm = re.match(r"^(\w+):\s*(.*)$", part)
            if fm:
                fields[fm.group(1).lower()] = clean_val(fm.group(2))
            else:
                fields.setdefault("why", part)  # bare chunk = freeform why
        if title:
            yield section, checked, title, fields


def read_frontmatter(path: Path):
    """Return (meta: dict, body: str) from a `---`-fenced flat header.
    Only flat `key: value` lines (stdlib, no yaml dep)."""
    if not path.exists():
        return {}, ""
    text = path.read_text(encoding="utf-8")
    meta, body = {}, text
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            head = text[3:end]
            body = text[end + 4:].lstrip("\n")
            for line in head.splitlines():
                if ":" in line and not line.strip().startswith("#"):
                    k, v = line.split(":", 1)
                    key = k.strip()
                    # A machine-local path may legitimately contain " #";
                    # unlike regular metadata it has no inline-comment syntax.
                    meta[key] = v.strip() if key == "local_folder" else clean_val(v)
    return meta, body


# --------------------------------------------------------------------------- #
# collectors — return plain row dicts/lists; DB insertion happens later
# --------------------------------------------------------------------------- #
def collect_entities(root: Path):
    """Return (entities, relationships) by walking the projects/ tree."""
    entities = []
    relationships = []
    pdir = root / "projects"
    for proj in sorted(pdir.glob("*")) if pdir.exists() else []:
        if not proj.is_dir():
            continue
        pslug = proj.name
        meta, _ = read_frontmatter(proj / "project.md")
        has_strategy = (proj / "strategy").exists()
        has_products = (proj / "products").exists()
        kind = meta.get("kind") or ("venture" if (has_strategy or has_products) else "brand")
        entities.append({
            "slug": pslug, "type": "project", "subtype": kind,
            "name": meta.get("name") or pslug, "priority": meta.get("priority"),
            "status": meta.get("status") or "active",
            "hours_per_week": first_int(meta.get("hours_per_week")),
            "local_folder": None,  # folders live in team/folders.json (core.folders)
            "file_path": rel(proj / "project.md", root) if (proj / "project.md").exists() else None,
            "updated_at": mtime_iso(proj / "project.md"),
        })
        for prod in sorted((proj / "products").glob("*")) if has_products else []:
            if not prod.is_dir():
                continue
            pm, _ = read_frontmatter(prod / "product.md")
            entities.append({
                "slug": prod.name, "type": "product", "subtype": pm.get("type"),
                "name": pm.get("name") or prod.name, "priority": None,
                "status": pm.get("status") or "active", "hours_per_week": None,
                "local_folder": None,
                "file_path": rel(prod / "roadmap.md", root) if (prod / "roadmap.md").exists() else None,
                "updated_at": mtime_iso(prod / "product.md"),
            })
            relationships.append((prod.name, pslug, "belongs_to"))
        for prof in sorted((proj / "profiles").glob("*")) if (proj / "profiles").exists() else []:
            if not prof.is_dir():
                continue
            fm, _ = read_frontmatter(prof / "profile.md")
            entities.append({
                "slug": prof.name, "type": "profile", "subtype": fm.get("topic"),
                "name": fm.get("name") or prof.name, "priority": None,
                "status": "active", "hours_per_week": None,
                "local_folder": None,
                "file_path": rel(prof / "profile.md", root) if (prof / "profile.md").exists() else None,
                "updated_at": mtime_iso(prof / "profile.md"),
            })
            relationships.append((prof.name, pslug, "belongs_to"))
            for ch in sorted((prof / "channels").glob("*")) if (prof / "channels").exists() else []:
                if not ch.is_dir():
                    continue
                cm, _ = read_frontmatter(ch / "channel.md")
                entities.append({
                    "slug": ch.name, "type": "channel", "subtype": cm.get("platform"),
                    "name": cm.get("name") or ch.name, "priority": None,
                    "status": "active", "hours_per_week": None,
                    "local_folder": None,
                    "file_path": rel(ch / "channel.md", root) if (ch / "channel.md").exists() else None,
                    "updated_at": mtime_iso(ch / "channel.md"),
                })
                relationships.append((ch.name, prof.name, "belongs_to"))
    return entities, relationships


def collect_memos(root: Path, slugs):
    rows = []
    pdir = root / "projects"
    for proj in sorted(pdir.glob("*")) if pdir.exists() else []:
        if not proj.is_dir():
            continue
        slug = proj.name
        memo_dir = proj / "strategy" / "memos"
        if not memo_dir.exists():
            continue
        for f in sorted(memo_dir.glob("*.json")):
            m = re.match(r"^(.+)-v(\d+)\.json$", f.name)
            if not m:
                print(f"  warn: memo filename not <type>-vN.json, skipping: {rel(f, root)}")
                continue
            mtype, version = m.group(1), int(m.group(2))
            if mtype not in MEMO_TYPES:
                print(f"  warn: memo type '{mtype}' not in allowed set, skipping: {rel(f, root)}")
                continue
            data = load_json(f)
            status = data.get("status") if isinstance(data, dict) else None
            if status not in ("proposed", "approved", "superseded"):
                status = "proposed"
            created = (data.get("date") if isinstance(data, dict) else None) or mtime_iso(f)
            rows.append({
                "entity_slug": slug, "type": mtype, "version": version,
                "status": status, "file_path": rel(f, root), "created_at": created,
            })
    return rows


def collect_experiments(root: Path):
    rows = []
    pdir = root / "projects"
    for proj in sorted(pdir.glob("*")) if pdir.exists() else []:
        if not proj.is_dir():
            continue
        slug = proj.name
        exp_dir = proj / "strategy" / "experiments"
        if not exp_dir.exists():
            continue
        for f in sorted(exp_dir.glob("*.json")):
            d = load_json(f)
            if not isinstance(d, dict):
                continue
            status = d.get("status")
            if status not in EXPERIMENT_STATUSES:
                status = "planned"
            decision = d.get("decision")
            if decision not in EXPERIMENT_DECISIONS:
                decision = None
            rows.append({
                "entity_slug": slug,
                "assumption": d.get("assumption") or d.get("assumption_under_test") or "(unspecified)",
                "status": status,
                "duration_days": first_int(d.get("duration_days") or d.get("duration")),
                "started_on": d.get("started_on") or d.get("date"),
                "decision": decision,
                "result": d.get("result"),
                "file_path": rel(f, root),
            })
    return rows


def collect_posts(root: Path):
    rows = {}
    chan_by_pid = {}
    pdir = root / "projects"
    for proj in sorted(pdir.glob("*")) if pdir.exists() else []:
        if not proj.is_dir():
            continue
        for prof in sorted((proj / "profiles").glob("*")) if (proj / "profiles").exists() else []:
            if not prof.is_dir():
                continue
            profile_slug = prof.name
            content = prof / "content"
            if not content.exists():
                continue
            briefs = content / "briefs"
            for plan in sorted(content.glob("plan-*.json")):
                data = load_json(plan)
                for post in (data.get("posts", []) if isinstance(data, dict) else []):
                    pid = post.get("id")
                    if not pid:
                        continue
                    status = post.get("status")
                    brief_file = briefs / f"{pid}.json"
                    brief_path = rel(brief_file, root) if brief_file.exists() else None
                    if status not in POST_STATUSES:
                        status = "briefed" if brief_path else "planned"
                    # A brief file exists but the slot was never advanced past the
                    # idea stage (e.g. a brief written by the CLI/agent that didn't
                    # flip status). The artifact is the truth: show it as at least
                    # 'briefed' so written posts don't masquerade as blank ideas.
                    elif brief_path and status in ("planned", "approved_slot"):
                        status = "briefed"
                    if pid in rows:
                        print(f"  warn: duplicate post id '{pid}' — keeping latest from {rel(plan, root)}")
                    rows[pid] = {
                        "id": pid, "profile_slug": profile_slug, "date": post.get("date"),
                        "pillar": post.get("pillar"),
                        "working_title": post.get("working_title"),
                        "concept": post.get("concept"),
                        "status": status, "version": first_int(post.get("version")) or 1,
                        "brief_path": brief_path,
                    }
                    chan_by_pid[pid] = [c for c in (post.get("channels") or []) if c]
    post_channels = [
        {"post_id": pid, "channel_slug": c}
        for pid in rows
        for c in chan_by_pid.get(pid, [])
    ]
    return list(rows.values()), post_channels


def collect_features(root: Path):
    rows = []
    pdir = root / "projects"
    for proj in sorted(pdir.glob("*")) if pdir.exists() else []:
        if not proj.is_dir():
            continue
        products_dir = proj / "products"
        if not products_dir.exists():
            continue
        for prod in sorted(products_dir.glob("*")):
            if not prod.is_dir():
                continue
            product_slug = prod.name
            roadmap = prod / "roadmap.md"
            if not roadmap.exists():
                continue
            for section, checked, title, f in parse_checklist(roadmap.read_text(encoding="utf-8")):
                sec = (section or "").lower()
                # Shipped badge only when checkbox is ticked — unchecked lines under
                # ## Shipped stay planned (common agent mistake).
                if checked:
                    status = "shipped"
                elif "now" in sec or "building" in sec:
                    status = "building"
                elif ("next" in sec or "planned" in sec or "later" in sec or "ideas" in sec
                      or "shipped" in sec):
                    # Unchecked under ## Shipped = misplaced spec, not done (common agent error).
                    status = "planned"
                else:
                    status = "idea"
                priority = f.get("priority")
                rows.append({
                    "product_slug": product_slug, "title": title, "status": status,
                    "priority": priority if priority in PRIORITIES else None,
                    "target_date": f.get("target"),
                    "shipped_date": f.get("shipped"),
                    "release": f.get("release"),
                })
    return rows


def collect_activities(root: Path):
    rows = []
    path = root / "portfolio" / "activities.md"
    if not path.exists():
        return rows
    for _section, checked, title, f in parse_checklist(path.read_text(encoding="utf-8")):
        status = "done" if checked else (f.get("status") or "planned")
        if status not in ACTIVITY_STATUSES:
            status = "planned"
        priority = f.get("priority")
        rows.append({
            "entity_slug": f.get("entity"),
            "title": title,
            "date": f.get("date"),
            "date_end": f.get("date_end"),
            "time": f.get("time"),
            "time_end": f.get("time_end"),
            "type": f.get("type") or "task",
            "status": status,
            "priority": priority if priority in PRIORITIES else None,
        })
    return rows


def collect_milestones(root: Path, slugs):
    rows = []
    path = root / "portfolio" / "milestones.json"
    if not path.exists():
        return rows
    data = load_json(path)
    for m in (data.get("milestones", []) if isinstance(data, dict) else []):
        entity = m.get("entity")
        # 'external' (or unknown-but-external_type) events map to no entity row
        if entity and (entity == "external" or m.get("entity_type") == "external"):
            entity = None
        date = m.get("date")
        if not date or "Y" in str(date):  # skip the template placeholder row
            continue
        priority = m.get("priority")
        rows.append({
            "id": m.get("id"),
            "entity_slug": entity,
            "entity_type": m.get("entity_type"),
            "type": m.get("type") or "event",
            "title": m.get("title") or "(untitled)",
            "date": date,
            "date_end": m.get("date_end"),
            "priority": priority if priority in PRIORITIES else None,
            "notes": m.get("notes"),
        })
    return rows


# --------------------------------------------------------------------------- #
# slug integrity
# --------------------------------------------------------------------------- #
def check_slugs(slugs, relationships, memos, experiments, posts, post_channels, features, activities, milestones):
    errors = []

    def need(slug, where):
        if slug is not None and slug not in slugs:
            errors.append(f"unresolved slug '{slug}' in {where}")

    for frm, to, kind in relationships:
        need(frm, f"relationship from_slug ({kind})")
        need(to, f"relationship to_slug ({kind})")
    for m in memos:
        need(m["entity_slug"], f"memo {m['type']}-v{m['version']}")
    for x in experiments:
        need(x["entity_slug"], f"experiment {x['file_path']}")
    for p in posts:
        need(p["profile_slug"], f"post {p['id']}")
    for pc in post_channels:
        need(pc["channel_slug"], f"post_channel post_id={pc['post_id']}")
    for f in features:
        need(f["product_slug"], f"feature '{f['title']}'")
    for a in activities:
        need(a["entity_slug"], f"activity '{a['title']}'")
    for ms in milestones:
        need(ms["entity_slug"], f"milestone {ms.get('id')}")

    if errors:
        raise IndexError_(
            "slug integrity check failed — fix the offending file:\n  - "
            + "\n  - ".join(errors)
        )


# --------------------------------------------------------------------------- #
# build
# --------------------------------------------------------------------------- #
def build(root: Path):
    db_path = root / DB_RELPATH
    db_path.parent.mkdir(parents=True, exist_ok=True)

    # Canonicalize authored files before collectors read them (UI/osctl/chat → same shape).
    changed = normalize_workspace_artifacts(root)
    if changed:
        print(f"  normalized {len(changed)} artifact(s)")

    entities, relationships = collect_entities(root)
    slugs = {e["slug"] for e in entities}
    if len(slugs) != len(entities):
        raise IndexError_("duplicate entity slug in projects/")

    memos = collect_memos(root, slugs)
    experiments = collect_experiments(root)
    posts, post_channels = collect_posts(root)
    features = collect_features(root)
    activities = collect_activities(root)
    milestones = collect_milestones(root, slugs)

    # Resilience: generated content (a plan-*.json) can reference a profile or
    # channel slug that doesn't exist — typically the model naming a platform
    # ('tiktok') instead of the real channel slug. Rather than refuse to boot
    # the entire OS, drop those dangling references with a warning so everything
    # else still indexes.
    profile_slugs = {e["slug"] for e in entities if e["type"] == "profile"}
    channel_slugs = {e["slug"] for e in entities if e["type"] == "channel"}
    kept_posts = []
    for p in posts:
        if p["profile_slug"] in profile_slugs:
            kept_posts.append(p)
        else:
            print(f"  warn: post '{p['id']}' references unknown profile "
                  f"'{p['profile_slug']}' — skipping it")
    posts = kept_posts
    live_pids = {p["id"] for p in posts}
    kept_pc = []
    for pc in post_channels:
        if pc["post_id"] not in live_pids:
            continue
        if pc["channel_slug"] in channel_slugs:
            kept_pc.append(pc)
        else:
            print(f"  warn: post '{pc['post_id']}' references unknown channel "
                  f"'{pc['channel_slug']}' — dropping that channel ref")
    post_channels = kept_pc

    check_slugs(slugs, relationships, memos, experiments, posts, post_channels, features, activities, milestones)

    # Recreate from schema beside the old one, and swap only when it all worked: a failure
    # here must leave the last good index in place, not an empty one.
    tmp_path = db_path.with_name(f".{db_path.name}.{os.getpid()}.tmp")
    tmp_path.unlink(missing_ok=True)
    conn = sqlite3.connect(tmp_path)
    conn.execute("PRAGMA foreign_keys = ON;")
    for migration in sorted(MIGRATIONS_DIR.glob("*.sql")):
        conn.executescript(migration.read_text(encoding="utf-8"))

    cur = conn.cursor()
    # entities FIRST (every other table FKs to them)
    cur.executemany(
        "INSERT INTO entities (slug,type,subtype,name,priority,status,hours_per_week,local_folder,file_path,updated_at)"
        " VALUES (:slug,:type,:subtype,:name,:priority,:status,:hours_per_week,:local_folder,:file_path,:updated_at)",
        entities,
    )
    cur.executemany(
        "INSERT INTO relationships (from_slug,to_slug,kind) VALUES (?,?,?)",
        relationships,
    )
    cur.executemany(
        "INSERT INTO memos (entity_slug,type,version,status,file_path,created_at)"
        " VALUES (:entity_slug,:type,:version,:status,:file_path,:created_at)",
        memos,
    )
    cur.executemany(
        "INSERT INTO experiments (entity_slug,assumption,status,duration_days,started_on,decision,result,file_path)"
        " VALUES (:entity_slug,:assumption,:status,:duration_days,:started_on,:decision,:result,:file_path)",
        experiments,
    )
    cur.executemany(
        "INSERT INTO posts (id,profile_slug,date,pillar,working_title,concept,status,version,brief_path)"
        " VALUES (:id,:profile_slug,:date,:pillar,:working_title,:concept,:status,:version,:brief_path)",
        posts,
    )
    cur.executemany(
        "INSERT INTO post_channels (post_id,channel_slug) VALUES (:post_id,:channel_slug)",
        post_channels,
    )
    cur.executemany(
        "INSERT INTO features (product_slug,title,status,priority,target_date,shipped_date,release)"
        " VALUES (:product_slug,:title,:status,:priority,:target_date,:shipped_date,:release)",
        features,
    )
    cur.executemany(
        "INSERT INTO activities (entity_slug,title,date,date_end,time,time_end,type,status,priority)"
        " VALUES (:entity_slug,:title,:date,:date_end,:time,:time_end,:type,:status,:priority)",
        activities,
    )
    cur.executemany(
        "INSERT INTO milestones (id,entity_slug,entity_type,type,title,date,date_end,priority,notes)"
        " VALUES (:id,:entity_slug,:entity_type,:type,:title,:date,:date_end,:priority,:notes)",
        milestones,
    )
    conn.commit()

    counts = {
        "entities": entities, "relationships": relationships, "memos": memos,
        "experiments": experiments, "posts": posts, "post_channels": post_channels,
        "features": features, "activities": activities, "milestones": milestones,
    }
    fk = conn.execute("PRAGMA foreign_key_check;").fetchall()
    timeline_n = conn.execute("SELECT COUNT(*) FROM timeline;").fetchone()[0]
    conn.close()

    if fk:
        raise IndexError_(f"foreign_key_check found violations: {fk}")
    os.replace(tmp_path, db_path)

    print(f"Rebuilt {rel(db_path, root)}")
    for name, rows in counts.items():
        print(f"  {name:<14} {len(rows)}")
    print(f"  {'timeline (view)':<14} {timeline_n}")


def main():
    root = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path.cwd()
    if not any(MIGRATIONS_DIR.glob("*.sql")):
        print(f"error: no migrations found in {MIGRATIONS_DIR}", file=sys.stderr)
        sys.exit(1)
    try:
        build(root)
    except (IndexError_, sqlite3.Error) as exc:
        print(f"\nINDEX FAILED: {exc}", file=sys.stderr)
        sys.exit(1)
    finally:
        for leftover in (root / DB_RELPATH).parent.glob(f".{DB_RELPATH.name}.{os.getpid()}.tmp*"):
            leftover.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
