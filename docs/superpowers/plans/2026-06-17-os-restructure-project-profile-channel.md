# OS Restructure (Project · Profile · Channel · Product) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the OS entity model with one clear hierarchy — Project · Profile · Channel · Product · External — derived from filesystem nesting, and sweep the nomenclature across schema, indexer, dashboard, skills, and docs.

**Architecture:** Authored files stay the source of truth; `database/data/os.db` is a disposable read-only index rebuilt by `index.py`. Local data was cleared before this redesign, so there is NO migration — the single migration file is rewritten in place and the index regenerates clean. The hierarchy is read from folder nesting (`projects/<p>/profiles/<prof>/channels/<ch>/`), replacing the old `portfolio/relationships.md` graph file.

**Tech Stack:** Python 3 stdlib only (`sqlite3`, `json`, `pathlib`, `re`, `http.server`), `unittest`; single vanilla-JS `dashboard/app.html`. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-06-17-os-restructure-project-profile-channel-design.md`

**Commit identity for every task:** `git -c user.name='Leo Bot' -c user.email='you@example.com'`

**Invariant (never break):** files = truth; `os.db` is derived/read-only (dashboard opens `mode=ro`, never writes it); every dashboard write mutates a FILE via `fileops.py` then calls `reindex()`. Stdlib-only, no new deps.

---

## File map

| File | Change |
|---|---|
| `database/migrations/0001_init.sql` | Rewrite: new entity types + `subtype`; `posts.profile_slug` + `post_channels`; `features.product_slug`; updated `timeline` VIEW |
| `index.py` | Replace `relationships.md` parser with a `projects/` tree walk; reshape `collect_*`; update `build()` inserts + `check_slugs` |
| `dashboard/db.py` | Reshape `tree()`, `project()`, add `profile_posts()`/`channel()`; rename `brands()`→`profiles()`; `posts()` via `profile_slug` |
| `dashboard/fileops.py` | New `projects/.../profiles/<slug>/content` paths; `find_post` tree walk; post CRUD with `channels`; channel guidelines |
| `dashboard/server.py` | Route renames: `/api/profile/<slug>/posts`, `/api/channel/<slug>/guidelines`, keep `/api/project/<slug>`, `/api/timeline` |
| `dashboard/app.html` | Rail: profiles + channels + products; content board at profile level; channel guidelines page; wording → profile/channel/product |
| `tests/*.py` | Rewrite the 5 tests to the new tree/schema |
| `skills/*`, `README.md`, `templates/*`, `docs/guide.md` | Nomenclature sweep; `portfolio-map` reduced to folder scaffolding |

---

## Task 1: New schema migration

**Files:**
- Modify (rewrite): `database/migrations/0001_init.sql`
- Test: `tests/test_schema.py` (create)

- [ ] **Step 1: Write the failing test**

Create `tests/test_schema.py`:

```python
import sqlite3, subprocess, sys, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

class TestSchema(unittest.TestCase):
    def _fresh_db(self, tmp):
        import index
        index.build(Path(tmp))
        return Path(tmp) / "database" / "data" / "os.db"

    def test_entities_type_check_and_subtype(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            db = self._fresh_db(tmp)
            con = sqlite3.connect(db)
            cols = {r[1] for r in con.execute("PRAGMA table_info(entities)")}
            self.assertIn("subtype", cols)
            # type CHECK accepts the new vocabulary, rejects an old one
            con.execute("PRAGMA foreign_keys=ON")
            con.execute("INSERT INTO entities(slug,type,name,updated_at) VALUES('p','project','P','t')")
            with self.assertRaises(sqlite3.IntegrityError):
                con.execute("INSERT INTO entities(slug,type,name,updated_at) VALUES('b','brand','B','t')")
            con.close()

    def test_posts_have_profile_slug_and_join(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            db = self._fresh_db(tmp)
            con = sqlite3.connect(db)
            pcols = {r[1] for r in con.execute("PRAGMA table_info(posts)")}
            self.assertIn("profile_slug", pcols)
            self.assertNotIn("brand_slug", pcols)
            tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertIn("post_channels", tables)
            fcols = {r[1] for r in con.execute("PRAGMA table_info(features)")}
            self.assertIn("product_slug", fcols)
            con.close()

if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run it to verify it fails**

Run: `python3 -m unittest tests.test_schema -v`
Expected: FAIL (old schema has `brand_slug`, no `subtype`, no `post_channels`).

- [ ] **Step 3: Rewrite `database/migrations/0001_init.sql`**

Replace the file with this complete schema:

```sql
-- schema.sql — consolidated DDL for os.db (the DERIVED, DISPOSABLE index).
-- Source of truth is always the authored files; index.py wipes and rebuilds this.
-- Hierarchy: project > {profile > channel, product}. Posts are profile-level and
-- target one or more channels via post_channels.
PRAGMA foreign_keys = ON;

CREATE TABLE entities (
  slug           TEXT PRIMARY KEY,
  type           TEXT NOT NULL CHECK (type IN ('project','profile','channel','product','external')),
  subtype        TEXT,                            -- project kind (venture|brand) or product type (app|physical|service|other)
  name           TEXT NOT NULL,
  priority       TEXT CHECK (priority IN ('primary','secondary','experiment')),
  status         TEXT NOT NULL DEFAULT 'active',
  hours_per_week INTEGER,
  file_path      TEXT,
  updated_at     TEXT NOT NULL
);

-- typed edges; only belongs_to is emitted by the indexer (folder nesting)
CREATE TABLE relationships (
  from_slug TEXT NOT NULL REFERENCES entities(slug),
  to_slug   TEXT NOT NULL REFERENCES entities(slug),
  kind      TEXT NOT NULL CHECK (kind IN ('belongs_to','drives_to','depends_on')),
  PRIMARY KEY (from_slug, to_slug, kind)
);

CREATE TABLE memos (
  id          INTEGER PRIMARY KEY,
  entity_slug TEXT NOT NULL REFERENCES entities(slug),   -- the PROJECT
  type        TEXT NOT NULL CHECK (type IN
                ('problem-validation','assessment','channels','icp',
                 'positioning','competitors','pricing','launch')),
  version     INTEGER NOT NULL,
  status      TEXT NOT NULL CHECK (status IN ('proposed','approved','superseded')),
  file_path   TEXT NOT NULL,
  created_at  TEXT NOT NULL,
  UNIQUE (entity_slug, type, version)
);

CREATE TABLE experiments (
  id            INTEGER PRIMARY KEY,
  entity_slug   TEXT NOT NULL REFERENCES entities(slug),  -- the PROJECT
  assumption    TEXT NOT NULL,
  status        TEXT NOT NULL CHECK (status IN ('planned','running','done')),
  duration_days INTEGER,
  started_on    TEXT,
  decision      TEXT CHECK (decision IN ('persist','pivot','kill')),
  result        TEXT,
  file_path     TEXT
);

-- content pipeline: one row per post, authored at the PROFILE level
CREATE TABLE posts (
  id           TEXT PRIMARY KEY,
  profile_slug TEXT NOT NULL REFERENCES entities(slug),
  date         TEXT,
  pillar       TEXT,
  status       TEXT NOT NULL CHECK (status IN
                 ('planned','approved_slot','briefed','approved',
                  'scheduled','published','rejected')),
  version      INTEGER NOT NULL DEFAULT 1,
  brief_path   TEXT
);
CREATE INDEX idx_posts_profile_date ON posts(profile_slug, date);

-- which channels a post targets (0..n); the bare platform lives on the channel entity
CREATE TABLE post_channels (
  post_id      TEXT NOT NULL REFERENCES posts(id),
  channel_slug TEXT NOT NULL REFERENCES entities(slug),
  PRIMARY KEY (post_id, channel_slug)
);

CREATE TABLE activities (
  id          INTEGER PRIMARY KEY,
  entity_slug TEXT REFERENCES entities(slug),
  title       TEXT NOT NULL,
  date        TEXT,
  date_end    TEXT,
  type        TEXT NOT NULL,
  status      TEXT NOT NULL DEFAULT 'planned'
                CHECK (status IN ('planned','running','blocked','done')),
  priority    TEXT CHECK (priority IN ('critical','high','normal','low'))
);

-- product roadmap features (source: projects/<p>/products/<slug>/roadmap.md)
CREATE TABLE features (
  id           INTEGER PRIMARY KEY,
  product_slug TEXT NOT NULL REFERENCES entities(slug),
  title        TEXT NOT NULL,
  status       TEXT NOT NULL CHECK (status IN ('idea','planned','building','shipped')),
  priority     TEXT CHECK (priority IN ('critical','high','normal','low')),
  target_date  TEXT,
  shipped_date TEXT,
  release      TEXT
);
CREATE INDEX idx_features_product_status ON features(product_slug, status);

CREATE TABLE milestones (
  id          TEXT PRIMARY KEY,
  entity_slug TEXT REFERENCES entities(slug),
  entity_type TEXT,                               -- project|profile|channel|product|external
  type        TEXT NOT NULL,
  title       TEXT NOT NULL,
  date        TEXT NOT NULL,
  date_end    TEXT,
  priority    TEXT CHECK (priority IN ('critical','high','normal','low')),
  notes       TEXT
);

CREATE VIEW timeline AS
  SELECT x.started_on AS date, NULL AS date_end, x.entity_slug AS entity_slug,
         'experiment' AS kind, x.assumption AS title, x.status AS status,
         e.priority AS priority, e.hours_per_week AS hours_per_week
  FROM experiments x LEFT JOIN entities e ON e.slug = x.entity_slug
  UNION ALL
  SELECT p.date, NULL, p.profile_slug, 'post',
         COALESCE(p.pillar, ''), p.status, e.priority, e.hours_per_week
  FROM posts p LEFT JOIN entities e ON e.slug = p.profile_slug
  UNION ALL
  SELECT COALESCE(f.shipped_date, f.target_date), NULL, f.product_slug, 'feature',
         f.title, f.status, COALESCE(f.priority, e.priority), e.hours_per_week
  FROM features f LEFT JOIN entities e ON e.slug = f.product_slug
  UNION ALL
  SELECT a.date, a.date_end, a.entity_slug, 'activity', a.title, a.status,
         COALESCE(a.priority, e.priority), e.hours_per_week
  FROM activities a LEFT JOIN entities e ON e.slug = a.entity_slug
  UNION ALL
  SELECT m.date, m.date_end, m.entity_slug, 'milestone', m.title, NULL,
         COALESCE(m.priority, e.priority), e.hours_per_week
  FROM milestones m LEFT JOIN entities e ON e.slug = m.entity_slug;
```

> Note: Task 1's test calls `index.build()`, which won't fully match the new schema's INSERTs until Task 2. To keep Task 1 green in isolation, the test only builds an EMPTY workspace (no `projects/`), which exercises the schema (table/column shape) without needing the new collectors. The empty build must still succeed.

- [ ] **Step 4: Make the empty build succeed against the new schema**

In `index.py` `build()`, the INSERT statements still reference old columns (`brand_slug`, `app_slug`, no `subtype`/`post_channels`). For Task 1, update ONLY the INSERT column lists and the `entities` insert to include `subtype` so an EMPTY build (zero rows) runs without error. Full collector rewrites happen in Task 2. Minimal edits:
  - `entities` insert: add `subtype` → `INSERT INTO entities (slug,type,subtype,name,priority,status,hours_per_week,file_path,updated_at) VALUES (:slug,:type,:subtype,:name,...)`. Add `"subtype": None` default in the (currently old) entity dicts is NOT needed for empty build, but add the column to the SQL.
  - `posts` insert: `INSERT INTO posts (id,profile_slug,date,pillar,status,version,brief_path) VALUES (:id,:profile_slug,:date,:pillar,:status,:version,:brief_path)`.
  - `features` insert: `product_slug` instead of `app_slug`.
  - Add a `post_channels` `executemany` (empty list for now): build a `post_channels` list = `[]` in Task 1; populated in Task 2.

- [ ] **Step 5: Run the test to verify it passes**

Run: `python3 -m unittest tests.test_schema -v`
Expected: PASS (empty build creates the new schema; CHECK rejects `brand`).

- [ ] **Step 6: Commit**

```bash
git add database/migrations/0001_init.sql index.py tests/test_schema.py
git -c user.name='Leo Bot' -c user.email='you@example.com' commit -m "feat(schema): Project/Profile/Channel/Product model + post_channels"
```

---

## Task 2: Indexer tree-walk

**Files:**
- Modify: `index.py` (replace `collect_entities`; reshape `collect_posts`, `collect_features`, `collect_memos`, `collect_experiments`; update `build()` + `check_slugs`)
- Modify (rewrite): `tests/test_index_projects.py`

- [ ] **Step 1: Write the failing test**

Replace `tests/test_index_projects.py` with a temp-tree build:

```python
import json, sqlite3, tempfile, unittest
from pathlib import Path
import index

def write(p: Path, text=""):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")

class TestTreeWalk(unittest.TestCase):
    def _build(self, tmp):
        root = Path(tmp)
        proj = root / "projects" / "acme"
        write(proj / "project.md", "---\nname: Acme\nkind: venture\npriority: primary\nhours_per_week: 12\n---\nvoice")
        prof = proj / "profiles" / "demo"
        write(prof / "profile.md", "---\nname: Demo\ntopic: cooking\n---\nvoice")
        write(prof / "channels" / "demo-tiktok" / "channel.md", "---\nplatform: tiktok\n---\nrules")
        write(prof / "content" / "plan-2026-07.json", json.dumps({"posts": [
            {"id": "post-001", "date": "2026-07-01", "pillar": "curiosity", "status": "planned",
             "channels": ["demo-tiktok"]}]}))
        write(proj / "products" / "acme-app" / "product.md", "---\nname: Acme App\ntype: app\n---")
        write(proj / "products" / "acme-app" / "roadmap.md", "## Now\n- [ ] Editor — priority: high")
        index.build(root)
        return sqlite3.connect(root / "database" / "data" / "os.db")

    def test_entities_and_nesting(self):
        with tempfile.TemporaryDirectory() as tmp:
            con = self._build(tmp)
            types = dict(con.execute("SELECT slug, type FROM entities"))
            self.assertEqual(types["acme"], "project")
            self.assertEqual(types["demo"], "profile")
            self.assertEqual(types["demo-tiktok"], "channel")
            self.assertEqual(types["acme-app"], "product")
            sub = dict(con.execute("SELECT slug, subtype FROM entities"))
            self.assertEqual(sub["acme"], "venture")
            belongs = set(con.execute("SELECT from_slug, to_slug FROM relationships WHERE kind='belongs_to'"))
            self.assertIn(("demo", "acme"), belongs)
            self.assertIn(("demo-tiktok", "demo"), belongs)
            self.assertIn(("acme-app", "acme"), belongs)

    def test_post_profile_and_channels(self):
        with tempfile.TemporaryDirectory() as tmp:
            con = self._build(tmp)
            prof = con.execute("SELECT profile_slug FROM posts WHERE id='post-001'").fetchone()[0]
            self.assertEqual(prof, "demo")
            chans = [r[0] for r in con.execute(
                "SELECT channel_slug FROM post_channels WHERE post_id='post-001'")]
            self.assertEqual(chans, ["demo-tiktok"])

    def test_feature_under_product(self):
        with tempfile.TemporaryDirectory() as tmp:
            con = self._build(tmp)
            row = con.execute("SELECT product_slug, title FROM features").fetchone()
            self.assertEqual(row[0], "acme-app")

if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run it to verify it fails**

Run: `python3 -m unittest tests.test_index_projects -v`
Expected: FAIL (old `collect_entities` reads `relationships.md`, not the tree).

- [ ] **Step 3: Add a frontmatter parser + rewrite `collect_entities` as a tree walk**

In `index.py`, add a small frontmatter reader near the markdown parsers:

```python
def read_frontmatter(path: Path):
    """Return (meta: dict, body: str) from a `---`-fenced YAML-ish header.
    Only flat `key: value` lines are supported (stdlib, no yaml dep)."""
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
                    meta[k.strip()] = clean_val(v)
    return meta, body
```

Replace `collect_entities(root)` with a walk that yields entities + `belongs_to` edges:

```python
def collect_entities(root: Path):
    entities, relationships = [], []
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
            "file_path": rel(proj / "project.md", root) if (proj / "project.md").exists() else None,
            "updated_at": mtime_iso(proj / "project.md"),
        })
        # products
        for prod in sorted((proj / "products").glob("*")) if has_products else []:
            if not prod.is_dir():
                continue
            pm, _ = read_frontmatter(prod / "product.md")
            entities.append({
                "slug": prod.name, "type": "product", "subtype": pm.get("type"),
                "name": pm.get("name") or prod.name, "priority": None,
                "status": pm.get("status") or "active", "hours_per_week": None,
                "file_path": rel(prod / "roadmap.md", root) if (prod / "roadmap.md").exists() else None,
                "updated_at": mtime_iso(prod / "product.md"),
            })
            relationships.append((prod.name, pslug, "belongs_to"))
        # profiles + channels
        for prof in sorted((proj / "profiles").glob("*")) if (proj / "profiles").exists() else []:
            if not prof.is_dir():
                continue
            fm, _ = read_frontmatter(prof / "profile.md")
            entities.append({
                "slug": prof.name, "type": "profile", "subtype": fm.get("topic"),
                "name": fm.get("name") or prof.name, "priority": None,
                "status": "active", "hours_per_week": None,
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
                    "file_path": rel(ch / "channel.md", root) if (ch / "channel.md").exists() else None,
                    "updated_at": mtime_iso(ch / "channel.md"),
                })
                relationships.append((ch.name, prof.name, "belongs_to"))
    return entities, relationships
```

- [ ] **Step 4: Reshape the strategy/content/product collectors**

- `collect_memos(root, slugs)` and `collect_experiments(root)`: change the base directory from `ventures/<slug>/...` to `projects/<slug>/strategy/...`. Memos live in `projects/*/strategy/memos/*.json`; experiments in `projects/*/strategy/experiments/*.json`. The `entity_slug` is the **project** slug (the `<slug>` folder name). Keep the existing version-parsing and field logic; only the glob roots change. (Read the current functions and swap `root / "ventures"` → walk `root / "projects" / <p> / "strategy"`.)
- `collect_posts(root)`: walk `projects/*/profiles/*/content/plan-*.json`. For each post, `profile_slug` = the profile folder name (`plan.parent.parent.name`). Return BOTH a posts list (no `platform` field now; fields `id, profile_slug, date, pillar, status, version, brief_path`) AND a `post_channels` list of `{"post_id": pid, "channel_slug": c}` for each slug in `post.get("channels", [])`. Briefs at `…/content/briefs/<pid>.json`. Change the signature to `return posts, post_channels`.
- `collect_features(root)`: walk `projects/*/products/*/roadmap.md`; `product_slug` = product folder name. Same checklist parsing; rename the dict key `app_slug` → `product_slug`.

- [ ] **Step 5: Update `build()` and `check_slugs()`**

- `build()`: `posts, post_channels = collect_posts(root)`. Add the `post_channels` `executemany` AFTER posts insert:
  `cur.executemany("INSERT INTO post_channels (post_id,channel_slug) VALUES (:post_id,:channel_slug)", post_channels)`.
  Update the `entities` insert to include `subtype` (Task 1 did the SQL; ensure every entity dict has a `subtype` key — the new `collect_entities` sets it).
  Update `posts` insert columns to `profile_slug`/`pillar` (no `platform`), `features` to `product_slug`. Add `post_channels` to the printed `counts`.
- `check_slugs(...)`: add `post_channels` param; validate each `channel_slug` resolves and each post's `profile_slug` resolves; rename `p["brand_slug"]`→`p["profile_slug"]`, `f["app_slug"]`→`f["product_slug"]`. Update the docstring banner near the top of `index.py` (the "Reads, under WORKSPACE_ROOT:" list) to the new paths.

- [ ] **Step 6: Run the tests to verify they pass**

Run: `python3 -m unittest tests.test_index_projects tests.test_schema -v`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add index.py tests/test_index_projects.py
git -c user.name='Leo Bot' -c user.email='you@example.com' commit -m "feat(index): walk projects/ tree → project/profile/channel/product"
```

---

## Task 3: db.py read layer

**Files:**
- Modify: `dashboard/db.py`
- Modify (rewrite): `tests/test_db_tree.py`, `tests/test_db_project.py`

- [ ] **Step 1: Write the failing tests**

Rewrite `tests/test_db_tree.py` to build the same temp tree as Task 2 (factor a helper, or inline it), point `db.DB_PATH` at the temp `os.db`, and assert `tree()` returns the project with a nested profile that itself nests its channels, plus the product:

```python
import importlib, json, tempfile, unittest
from pathlib import Path
import index
from tests.test_index_projects import write   # reuse the writer

class TestTree(unittest.TestCase):
    def _db(self, tmp):
        root = Path(tmp)
        proj = root / "projects" / "acme"
        write(proj / "project.md", "---\nname: Acme\nkind: venture\npriority: primary\n---")
        prof = proj / "profiles" / "demo"
        write(prof / "profile.md", "---\nname: Demo\n---")
        write(prof / "channels" / "demo-tiktok" / "channel.md", "---\nplatform: tiktok\n---")
        write(prof / "content" / "plan-x.json", json.dumps({"posts": [
            {"id": "p1", "status": "planned", "channels": ["demo-tiktok"]}]}))
        write(proj / "products" / "app" / "product.md", "---\nname: App\ntype: app\n---")
        index.build(root)
        import dashboard.db as db
        db.DB_PATH = root / "database" / "data" / "os.db"
        return db

    def test_tree_nests_profiles_channels_products(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = self._db(tmp)
            tree = db.tree()
            self.assertEqual(len(tree), 1)
            p = tree[0]
            self.assertEqual(p["slug"], "acme")
            self.assertEqual(p["kind"], "venture")
            self.assertEqual([x["slug"] for x in p["profiles"]], ["demo"])
            self.assertEqual([c["slug"] for c in p["profiles"][0]["channels"]],
                             ["demo-tiktok"])
            self.assertEqual([a["slug"] for a in p["products"]], ["app"])
            self.assertEqual(p["profiles"][0]["posts"], 1)

if __name__ == "__main__":
    unittest.main()
```

Rewrite `tests/test_db_project.py` similarly to assert `db.project("acme")` returns `profiles`, `products`, `memos`, `experiments`, `activities` keys (build a tree with one strategy memo + one product roadmap), and that `db.profile_posts("demo")` returns the post with a `channels` list `["demo-tiktok"]`.

- [ ] **Step 2: Run them to verify they fail**

Run: `python3 -m unittest tests.test_db_tree tests.test_db_project -v`
Expected: FAIL (old `tree()` keys are `channels`/`apps`; no `profiles`/`products`).

- [ ] **Step 3: Reshape `db.py`**

- `posts()`: `profile_slug`/`e.name AS profile_name`, drop `platform`.
- Rename `brands()` → `profiles()`; `WHERE type = 'profile'`.
- `tree()`: select `slug,type,subtype,name,priority` from entities; build `belongs` set; for each `project` entity, collect children where `(child, project) in belongs`: `type=='profile'` → profile dict `{slug,name, posts: <count from posts WHERE profile_slug>, written: <count brief_path NOT NULL>, channels: [ {slug,name,platform:subtype} for grandchildren where (gc, profile) in belongs and type=='channel' ]}`; `type=='product'` → product dict `{slug,name,features:<count>}`. Return key names: `profiles`, `products`, and `kind` (= project's `subtype`). Keep the primary-first sort.
- Add `profile_posts(slug)` (replaces `channel_posts`): posts WHERE `profile_slug = ?`, and for each attach `channels` = list of `channel_slug` from `post_channels`. Fields: `id, profile_slug, date, pillar, status, version, brief_path, channels`.
- Add `channel(slug)`: returns `{slug, name, platform, guidelines}` — read the entity row; `guidelines` comes from `fileops.read_channel_guidelines(slug)` OR leave guidelines to the server layer (db.py is read-only of os.db; it should NOT read arbitrary files). **Decision:** `channel()` returns only the os.db row `{slug,name,platform}`; the guidelines text is served via `fileops` in Task 4. Keep db.py pure.
- `project(slug)`: replace `channels`/`apps` with `profiles`/`products` (same belongs logic as `tree`), keep `memos`/`experiments`/`activities`; `features` now gathered from the project's product slugs (`product_slug IN (...)`).

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python3 -m unittest tests.test_db_tree tests.test_db_project -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add dashboard/db.py tests/test_db_tree.py tests/test_db_project.py
git -c user.name='Leo Bot' -c user.email='you@example.com' commit -m "feat(db): tree/project read layer over profiles + channels + products"
```

---

## Task 4: fileops.py write layer

**Files:**
- Modify: `dashboard/fileops.py`
- Modify (rewrite): `tests/test_fileops_posts.py`, `tests/test_fileops_plan_args.py`

- [ ] **Step 1: Write the failing tests**

Rewrite `tests/test_fileops_posts.py` to operate on the new tree. Point `fileops.ROOT` at a temp dir containing `projects/acme/profiles/demo/{profile.md,content/}` and `…/channels/demo-tiktok/channel.md`, then:

```python
def test_crud(self):
    fileops.add_post("demo", {"working_title": "Idea A", "channels": "demo-tiktok"})
    posts = db.profile_posts("demo")
    self.assertEqual(len(posts), 1)
    pid = posts[0]["id"]
    self.assertEqual(posts[0]["channels"], ["demo-tiktok"])
    fileops.update_post(pid, {"pillar": "curiosity"})
    fileops.delete_post(pid)
    self.assertEqual(db.profile_posts("demo"), [])

def test_add_unknown_profile(self):
    with self.assertRaises(fileops.ActionError):
        fileops.add_post("nope", {})
```

(Build the temp tree, run `index.build`, point both `fileops.ROOT` and `db.DB_PATH` at it.)

Rewrite `tests/test_fileops_plan_args.py`: `_plan_args` now takes a `profile_slug` and still requires `period`; assert it raises without a period and includes `plan <profile_slug> --period ...` with one.

- [ ] **Step 2: Run them to verify they fail**

Run: `python3 -m unittest tests.test_fileops_posts tests.test_fileops_plan_args -v`
Expected: FAIL (paths point at `brands/`).

- [ ] **Step 3: Update `fileops.py` paths + post shape**

- `find_post(post_id)`: glob `ROOT/"projects"/"*"/"profiles"/"*"/"content"/"plan-*.json"`; `profile_slug = plan.parent.parent.name`; return key `profile_slug` (not `brand_slug`).
- Replace `_brand_dir(slug)` with `_profile_dir(slug)` that globs `ROOT/"projects"/"*"/"profiles"/slug` (profiles are nested, so resolve by walking). Add a `_channel_dir(slug)` that globs `…/profiles/*/channels/slug`.
- `add_post(profile_slug, fields)`: resolve the profile dir via `_profile_dir`; write to its `content/`. Add `"channels"` handling: `_POST_FIELDS = ("date", "pillar", "working_title")` plus a special `channels` field parsed from a comma/space list into a JSON array on the post object.
- `update_post` / `delete_post`: use `profile_slug`; brief path under `…/profiles/<profile>/content/briefs/<pid>.json`.
- Guidelines: rename `read_guidelines`/`write_guidelines`/`refine_guidelines` to operate on a **channel** (`channel.md` body, or a sibling `guidelines.md` in the channel dir — use the channel dir). Add `read_channel_guidelines(slug)` / `write_channel_guidelines(slug, text)`.
- `generate_brief` / `run_plan` / `_plan_args`: rename `brand_slug` params → `profile_slug`; the generate.py subcommand args change from `<brand>` to `<profile>` (generate.py itself is updated in Task 6; keep the argv shape `plan <profile_slug>` / `brief <profile_slug> <post_id>`).
- `read_detail`: brief path under the profile content dir; return `profile_slug`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python3 -m unittest tests.test_fileops_posts tests.test_fileops_plan_args -v`
Expected: PASS.

- [ ] **Step 5: Run the FULL suite**

Run: `python3 -m unittest discover -s tests -v`
Expected: all PASS (schema, index, db ×2, fileops ×2).

- [ ] **Step 6: Commit**

```bash
git add dashboard/fileops.py tests/test_fileops_posts.py tests/test_fileops_plan_args.py
git -c user.name='Leo Bot' -c user.email='you@example.com' commit -m "feat(fileops): profile-level posts with channel targets; channel guidelines"
```

---

## Task 5: server routes + dashboard

**Files:**
- Modify: `dashboard/server.py`, `dashboard/app.html`

- [ ] **Step 1: Update server routes**

In `dashboard/server.py`, rename the post/guidelines routes to profile/channel and wire the new db/fileops names:
- `GET /api/profile/<slug>/posts` → `db.profile_posts(slug)` (was `/api/channel/<slug>/posts`).
- `GET /api/channel/<slug>/guidelines` / `POST …/guidelines` / `POST …/guidelines/refine` → `fileops.read_channel_guidelines` / `write_channel_guidelines` / `refine_guidelines`.
- `POST /api/profile/<slug>/posts` (add), `POST /api/profile/<slug>/plan` (run_plan) → fileops with `profile_slug`.
- Keep `/api/project/<slug>`, `/api/timeline`, `/api/tree`, `/api/post/<id>/{status,brief,update,delete}` (these now return the reshaped data automatically).
- Read the current route table and swap names consistently; no behavior change beyond the rename + the reshaped JSON.

- [ ] **Step 2: Update the dashboard rail + views**

In `dashboard/app.html`:
- `renderRail()`: render each project with `kind` badge (`venture`/`brand`); under it list **profiles**; each profile expands to its **channels** (link each channel to a guidelines page) and is itself the click target for the content board; render the project's **products** in the Product section. Replace the word "Social profiles"/"channel" wiring with profile/channel. Use the new `tree()` keys (`profiles`, each with `channels`; `products`).
- `renderChannel` → `renderProfile(slug)`: the content board now fetches `/api/profile/<slug>/posts`; each post card shows its target channels (chips) and the plain stages Idea→Draft→Scheduled→Published (keep the `plainStatus`/`STAGE_GROUP`/`NEXT` mapping). Keep ＋ Add idea / ✦ Generate ideas / Edit / Delete. The Add-idea modal gains a channels field.
- Add a `renderChannelGuidelines(slug)` view reachable from a channel in the rail (editor + Save + Refine, reusing the old guidelines UI but pointed at `/api/channel/<slug>/guidelines`).
- `renderProjectSection`: Product section lists products (each with feature counts/roadmap); other sections unchanged in shape.
- Keep `renderTimeline` (the month calendar) as-is.

- [ ] **Step 3: Smoke-test (no paid endpoints)**

Seed a tiny tree by hand under a temp run OR create `projects/demo/...` minimal files, then:
```bash
python3 index.py
python3 dashboard/server.py &   # http://127.0.0.1:8765
sleep 1
curl -s http://127.0.0.1:8765/api/tree | python3 -m json.tool | head
curl -s http://127.0.0.1:8765/api/timeline | head
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8765/   # 200
kill %1
```
Verify `/api/tree` shows `profiles` (with `channels`) and `products`; the page serves; DO NOT call `/plan` or `/brief` (they spend money). Delete any temp demo tree afterward so the repo stays empty.

- [ ] **Step 4: Run the full suite**

Run: `python3 -m unittest discover -s tests -v` → all PASS.

- [ ] **Step 5: Commit**

```bash
git add dashboard/server.py dashboard/app.html
git -c user.name='Leo Bot' -c user.email='you@example.com' commit -m "feat(dashboard): profile boards, channel guidelines, product section"
```

---

## Task 6: skills + docs nomenclature sweep

**Files:**
- Modify: `skills/*/SKILL.md` (18 dirs), `README.md`, `templates/*`, `docs/guide.md`, `generate.py`, `prompts/*`

- [ ] **Step 1: Update `generate.py` + prompts**

Swap `brand`→`profile` and the file-path conventions (`brands/<slug>/...` → `projects/*/profiles/<slug>/...`) in `generate.py` (the `plan`/`brief`/`refine-guidelines` subcommands and the identity/guidelines injection paths). Inject the voice cascade: project voice (`project.md` body) + profile voice (`profile.md` body) + channel guidelines (`channel.md` body) for the targeted platform(s). Verify offline: `python3 generate.py --help` runs and arg parsing accepts `plan <profile> --period ...` (no live `claude -p` call).

- [ ] **Step 2: Sweep the skills**

For each skill in `skills/`, update file-convention references and vocabulary:
- `portfolio-map` → reduced to scaffolding the `projects/<slug>/{project.md,strategy/,products/,profiles/<slug>/{profile.md,content/,channels/<slug>/channel.md}}` folders (no more `relationships.md`).
- `brand-identity` → `profile`/`channel` identity (`profile.md` voice; `channel.md` guidelines).
- `venture-intake`, `gtm-assessment`, `problem-validation`, `positioning`, `pricing-strategy`, `experiment-*`, `launch-plan`, `competitor-scan`, `icp-research` → write under `projects/<slug>/strategy/...`.
- `product-build` → `projects/<slug>/products/<slug>/roadmap.md`; "web app" → generic "product".
- `content-plan`/`content-brief`/`copy-variants`/`channel-strategy` → profile content + channel targets.
- `portfolio-timeline`/`portfolio-sync`/`weekly-review`/`workspace` → update entity vocabulary and the routing/file-convention tables.
Work skill-by-skill; keep each skill's intent, only change paths and the project/profile/channel/product wording.

- [ ] **Step 3: Update README, templates, guide**

- `README.md` workspace-structure block + skill descriptions → the new tree and vocabulary.
- `templates/workspace-CLAUDE.md` → new file conventions (drop `relationships.md`; add the nested layout); `templates/gtm-layer-spec.md` and `templates/generation-spec.md` → new entity/post shapes; rename/refresh `milestones-template.json` comment (`entity_type` vocabulary).
- `docs/guide.md` → already dashboard-focused; update entity vocabulary (brand→profile, app→product) and the file-authoring references.

- [ ] **Step 4: Verify the sweep**

Run a grep to confirm no stale entity words remain in CODE (skills/docs may legitimately mention history, but code must be clean):
```bash
grep -rInw -e brand_slug -e app_slug -e web_app index.py generate.py dashboard | grep -v '\.pyc'
```
Expected: no matches. Then the full suite:
`python3 -m unittest discover -s tests -v` → all PASS.

- [ ] **Step 5: Commit**

```bash
git add skills README.md templates docs/guide.md generate.py prompts
git -c user.name='Leo Bot' -c user.email='you@example.com' commit -m "docs: nomenclature sweep to project/profile/channel/product"
```

---

## Self-review notes (for the executor)

- **Spec coverage:** entity model (T1–T2), filesystem-as-hierarchy/no relationships.md (T2 walk + T6 portfolio-map), Product generic & multiple (T1 type, T2 products walk), posts profile-level + channel targets + override file fields (T1 `post_channels`, T2 `collect_posts`, T4 add `channels`), voice cascade (T6 generate.py), dashboard mapping (T5), nomenclature sweep (T3–T6).
- **Deferred (per spec §10):** per-channel scheduling/stages; Consultant; real Needs-you/Operations; content-status-enum overhaul. Don't build these.
- **Invariant:** every dashboard write goes file → `reindex()`; `db.py` only reads `os.db` `mode=ro` and never touches arbitrary files (guidelines text is served via `fileops`, not `db`).
- **No migration:** the tree is empty (seed wiped). Tests build their own temp trees; the live repo stays empty until the user authors real `projects/`.
- **Channel slugs are global** (`<profile>-<platform>`); posts reference those slugs in `channels`.
