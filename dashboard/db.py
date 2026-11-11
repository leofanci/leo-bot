"""db.py — READ side of the dashboard API.

Opens os.db READ-ONLY (uri mode=ro) so the dashboard physically cannot write to
the derived index — the invariant enforced at the connection level, not just by
discipline. All view queries live here; nothing in this module mutates anything,
except core.folders' one-time move of an old `local_folder` line when a project's
folders are first read.
"""

import sqlite3
from pathlib import Path

from core import folders, paths

ROOT = paths.DATA
DB_PATH = ROOT / "database" / "data" / "os.db"


def _rows(sql, params=()):
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def db_exists():
    return DB_PATH.exists()


def timeline():
    return _rows(
        "SELECT t.date, t.date_end, t.entity_slug, e.name AS entity_name,"
        " t.kind, t.title, t.status, t.priority, t.hours_per_week, t.ref_id,"
        " t.time, t.time_end"
        " FROM timeline t LEFT JOIN entities e ON e.slug = t.entity_slug"
        " ORDER BY (t.date IS NULL), t.date, (t.time IS NULL), t.time"
    )


def posts():
    return _rows(
        "SELECT p.id, p.profile_slug, e.name AS profile_name, p.date,"
        "       p.pillar, p.working_title, p.concept, p.status, p.version, p.brief_path"
        " FROM posts p LEFT JOIN entities e ON e.slug = p.profile_slug"
        " ORDER BY (p.date IS NULL), p.date"
    )


def features():
    return _rows(
        "SELECT f.id, f.product_slug, e.name AS product_name, f.title, f.status,"
        "       f.priority, f.target_date, f.shipped_date, f.release"
        " FROM features f LEFT JOIN entities e ON e.slug = f.product_slug"
        " ORDER BY f.product_slug, f.status, f.title"
    )


def profiles():
    return _rows(
        "SELECT slug, name, subtype, status FROM entities"
        " WHERE type = 'profile' ORDER BY name"
    )


def entities():
    return _rows(
        "SELECT slug, type, subtype, name, priority, status, hours_per_week, file_path"
        " FROM entities ORDER BY type, slug"
    )


def memos():
    return _rows(
        "SELECT m.entity_slug, e.name AS entity_name, m.type, m.version,"
        "       m.status, m.file_path, m.created_at"
        " FROM memos m LEFT JOIN entities e ON e.slug = m.entity_slug"
        " ORDER BY m.entity_slug, m.type, m.version"
    )


def experiments():
    return _rows(
        "SELECT entity_slug, assumption, status, duration_days, started_on,"
        "       decision, result, file_path FROM experiments ORDER BY entity_slug"
    )


def _folders(slug):
    """A project's linked folders (core.folders). The checkout this index belongs to is
    three levels above DB_PATH, so a test index reads its own folders."""
    try:
        return folders.load(slug, Path(DB_PATH).resolve().parents[2])["folders"]
    except (OSError, ValueError):
        return []


def tree():
    """Projects with nested profiles (+ channels) and products, plus counts —
    the shape the left rail renders."""
    ents = _rows("SELECT slug, type, subtype, name, priority FROM entities")
    rels = _rows("SELECT from_slug, to_slug FROM relationships WHERE kind = 'belongs_to'")
    belongs = {(r["from_slug"], r["to_slug"]) for r in rels}

    posts_n = {r["profile_slug"]: r["n"] for r in
               _rows("SELECT profile_slug, COUNT(*) n FROM posts GROUP BY profile_slug")}
    written_n = {r["profile_slug"]: r["n"] for r in
                 _rows("SELECT profile_slug, COUNT(*) n FROM posts"
                       " WHERE brief_path IS NOT NULL GROUP BY profile_slug")}
    feat_n = {r["product_slug"]: r["n"] for r in
              _rows("SELECT product_slug, COUNT(*) n FROM features GROUP BY product_slug")}
    exp_n = {r["entity_slug"]: r["n"] for r in
             _rows("SELECT entity_slug, COUNT(*) n FROM experiments GROUP BY entity_slug")}

    projects = []
    for e in ents:
        if e["type"] != "project":
            continue
        slug = e["slug"]
        profiles_list, products_list = [], []

        for c in ents:
            if (c["slug"], slug) not in belongs:
                continue
            if c["type"] == "profile":
                # Gather channels belonging to this profile
                channels = [
                    {
                        "slug": gc["slug"],
                        "name": gc["name"],
                        "platform": gc["subtype"],
                    }
                    for gc in ents
                    if gc["type"] == "channel" and (gc["slug"], c["slug"]) in belongs
                ]
                channels.sort(key=lambda x: x["slug"])
                profiles_list.append({
                    "slug": c["slug"],
                    "name": c["name"],
                    "posts": posts_n.get(c["slug"], 0),
                    "written": written_n.get(c["slug"], 0),
                    "channels": channels,
                })
            elif c["type"] == "product":
                products_list.append({
                    "slug": c["slug"],
                    "name": c["name"],
                    "features": feat_n.get(c["slug"], 0),
                })

        projects.append({
            "slug": slug,
            "name": e["name"],
            "kind": e["subtype"],
            "type": e["type"],
            "priority": e["priority"],
            "folders": _folders(slug),
            "profiles": sorted(profiles_list, key=lambda x: x["name"]),
            "products": sorted(products_list, key=lambda x: x["name"]),
            "experiments": exp_n.get(slug, 0),
        })

    projects.sort(key=lambda p: (p["priority"] != "primary", p["name"]))
    return projects


def profile_posts(slug):
    """All posts for one profile, with channels list attached to each post."""
    rows = _rows(
        "SELECT id, profile_slug, date, pillar, working_title, concept,"
        "       status, version, brief_path"
        " FROM posts WHERE profile_slug = ?"
        " ORDER BY (date IS NULL), date",
        (slug,),
    )
    # Attach channels per post
    for post in rows:
        ch_rows = _rows(
            "SELECT channel_slug FROM post_channels WHERE post_id = ? ORDER BY channel_slug",
            (post["id"],),
        )
        post["channels"] = [r["channel_slug"] for r in ch_rows]
    return rows


def channel(slug):
    """Single channel entity by slug, or None if not found."""
    rows = _rows(
        "SELECT slug, name, subtype AS platform FROM entities"
        " WHERE type = 'channel' AND slug = ?",
        (slug,),
    )
    return rows[0] if rows else None


def project(slug):
    """Everything one project owns, for the project-section views. None if unknown."""
    ent = _rows(
        "SELECT slug, type, subtype, name, priority, status, hours_per_week, file_path"
        " FROM entities WHERE slug = ? AND type = 'project'",
        (slug,),
    )
    if not ent:
        return None

    rels = _rows("SELECT from_slug, to_slug FROM relationships WHERE kind = 'belongs_to'")
    belongs = {(r["from_slug"], r["to_slug"]) for r in rels}
    everyone = _rows("SELECT slug, type, name FROM entities")

    profiles_list = [
        {"slug": c["slug"], "name": c["name"]}
        for c in everyone
        if c["type"] == "profile" and (c["slug"], slug) in belongs
    ]
    products_list = [
        {"slug": c["slug"], "name": c["name"]}
        for c in everyone
        if c["type"] == "product" and (c["slug"], slug) in belongs
    ]

    memos_rows = _rows(
        "SELECT type, version, status, file_path, created_at FROM memos"
        " WHERE entity_slug = ? ORDER BY type, version",
        (slug,),
    )
    experiments_rows = _rows(
        "SELECT id, assumption, status, duration_days, started_on, decision,"
        " result, file_path FROM experiments WHERE entity_slug = ?",
        (slug,),
    )
    for row in experiments_rows:
        fp = row.get("file_path") or ""
        row["stem"] = fp.rsplit("/", 1)[-1].replace(".json", "") if fp else ""

    product_slugs = [p["slug"] for p in products_list]
    features_rows = []
    if product_slugs:
        ph = ",".join("?" * len(product_slugs))
        features_rows = _rows(
            "SELECT product_slug, title, status, priority, target_date, shipped_date, release"
            f" FROM features WHERE product_slug IN ({ph}) ORDER BY status, title",
            tuple(product_slugs),
        )

    activities_rows = _rows(
        "SELECT id, title, date, date_end, type, status, priority"
        " FROM activities WHERE entity_slug = ? ORDER BY (date IS NULL), date",
        (slug,),
    )

    return {
        "entity": ent[0],
        "profiles": profiles_list,
        "products": products_list,
        "memos": memos_rows,
        "experiments": experiments_rows,
        "features": features_rows,
        "activities": activities_rows,
    }
