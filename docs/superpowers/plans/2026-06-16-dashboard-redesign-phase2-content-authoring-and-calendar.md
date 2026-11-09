# Dashboard Redesign — Phase 2: Project Workspace, Content Authoring & Calendar — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. Work on a branch off `main` (e.g. `dashboard-phase2`); merge with superpowers:finishing-a-development-branch when done.

**Goal:** Fix the information architecture so a **project is its whole GTM surface** (not just its social accounts), make content authoring possible (add / generate / edit / delete), and replace the Timeline table with a **month calendar**.

**The three problems this fixes:**
1. Social profiles looked like peers of the project — they must sit *inside* it, grouped under "Social profiles".
2. The rail only showed channels, so a project looked social-only. A project must expose its full GTM scope: **Overview · Problem & validation · Experiments · Positioning & pricing · Product · Operations · Social profiles**.
3. "Content / Planned" was jargon. Content becomes one list with plain stages **Idea → Draft → Scheduled → Published**, each row offering the obvious next action ("Write it →", "Review →").

**Architecture:** Read side stays in `dashboard/db.py` (os.db, read-only); a new `project(slug)` aggregate powers the project sections. The write side stays in `dashboard/fileops.py` (mutate authored files → `reindex()`); new functions add/edit/delete post slots and wrap `generate.py plan`. New routes in `dashboard/server.py`. The single `dashboard/app.html` gets a reworked rail, a data-driven project-section renderer, the new content list, and the calendar. The files-are-truth / read-only-os.db invariant is unchanged.

**Tech Stack:** Python 3 stdlib (`unittest`, `subprocess`, `json`, `datetime`), vanilla-JS `app.html`. No new dependencies.

**Reference:** Spec `docs/superpowers/specs/2026-06-16-dashboard-redesign-design.md`. Original visual mockups are intentionally not included in the public repository. Phase 1 (merged) delivered the shell, `/api/tree`, `/api/channel/<slug>/posts`, and the post-action/guideline endpoints.

**Existing helpers to reuse:**
- `db._rows(sql, params)` → list[dict] from the read-only connection.
- `fileops.find_post(post_id)` → `{plan, data, post, brand_slug}`; `fileops._write_plan(ctx)`; `fileops.reindex()`; `fileops._brand_dir(slug)`; `fileops.ActionError`.
- Plan file shape: `{"posts":[{id,date,platform,pillar,status,version,...}]}`. Status machine: `planned → approved_slot → briefed → approved → scheduled → published` (+ `rejected`). Plain-stage mapping for the UI: `planned`/`approved_slot`→**Idea**, `briefed`→**Draft**, `approved`→**Ready**, `scheduled`→**Scheduled**, `published`→**Published**, `rejected`→**Archived**.
- `generate.py plan <brand> --period "<start> to <end>" [--platforms a,b] [--cadence N] [--focus "…"]` writes `brands/<slug>/content/plan-<period>.json`.

---

### Task 1: `db.project(slug)` — the project aggregate

Powers every project section in one read.

**Files:** Modify `dashboard/db.py` (append `project()`); Test `tests/test_db_project.py`.

- [ ] **Step 1: Write the failing test** — create `tests/test_db_project.py`:
```python
import importlib, json, subprocess, sys, tempfile, unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
RELATIONSHIPS = """# Portfolio Relationships

## Ventures
- slug: acme
  name: Acme
  priority: primary
  status: prototype

## Brands
- slug: acme-tok
  name: Acme TikTok
  venture: acme
  channels: [tiktok]

## Web Apps / Products
- slug: acme-app
  name: Acme App
  venture: acme
"""

class TestProject(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        (root / "portfolio").mkdir(parents=True)
        (root / "portfolio" / "relationships.md").write_text(RELATIONSHIPS, encoding="utf-8")
        (root / "portfolio" / "activities.md").write_text(
            "## Ops\n- [ ] Email 5 writers — entity: acme — type: task\n", encoding="utf-8")
        (root / "ventures" / "acme" / "experiments").mkdir(parents=True)
        (root / "ventures" / "acme" / "experiments" / "exp-001.json").write_text(
            json.dumps({"id":"exp-001","assumption_under_test":"will pay","status":"planned"}), encoding="utf-8")
        (root / "apps" / "acme-app").mkdir(parents=True)
        (root / "apps" / "acme-app" / "roadmap.md").write_text("## Now\n- [ ] Login\n", encoding="utf-8")
        res = subprocess.run([sys.executable, str(REPO / "index.py"), str(root)], capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, res.stderr)
        sys.path.insert(0, str(REPO / "dashboard"))
        import db; importlib.reload(db); db.DB_PATH = root / "database" / "data" / "os.db"
        self.db = db

    def tearDown(self): self.tmp.cleanup()

    def test_project_aggregates_all_sections(self):
        p = self.db.project("acme")
        self.assertEqual(p["entity"]["name"], "Acme")
        self.assertEqual([c["slug"] for c in p["channels"]], ["acme-tok"])
        self.assertEqual([a["slug"] for a in p["apps"]], ["acme-app"])
        self.assertEqual(len(p["experiments"]), 1)
        self.assertEqual(len(p["features"]), 1)
        self.assertEqual(len(p["activities"]), 1)
        self.assertIsNone(self.db.project("does-not-exist"))

if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run, expect FAIL** — `python3 -m unittest tests.test_db_project -v` → `module 'db' has no attribute 'project'`.

- [ ] **Step 3: Implement** — append to `dashboard/db.py`:
```python
def project(slug):
    """Everything one project owns, for the project-section views. None if unknown."""
    ent = _rows("SELECT slug, type, name, priority, status, hours_per_week, file_path"
                " FROM entities WHERE slug = ?", (slug,))
    if not ent:
        return None
    rels = _rows("SELECT from_slug, to_slug FROM relationships WHERE kind = 'belongs_to'")
    belongs = {(r["from_slug"], r["to_slug"]) for r in rels}
    everyone = _rows("SELECT slug, type, name FROM entities")
    channels = [{"slug": c["slug"], "name": c["name"]} for c in everyone
                if c["type"] == "brand" and (c["slug"], slug) in belongs]
    apps = [{"slug": c["slug"], "name": c["name"]} for c in everyone
            if c["type"] == "web_app" and (c["slug"], slug) in belongs]
    memos = _rows("SELECT type, version, status, file_path, created_at FROM memos"
                  " WHERE entity_slug = ? ORDER BY type, version", (slug,))
    experiments = _rows("SELECT id, assumption, status, duration_days, started_on, decision,"
                        " result, file_path FROM experiments WHERE entity_slug = ?", (slug,))
    features = []
    app_slugs = [a["slug"] for a in apps]
    if app_slugs:
        ph = ",".join("?" * len(app_slugs))
        features = _rows("SELECT app_slug, title, status, priority, target_date, shipped_date, release"
                         f" FROM features WHERE app_slug IN ({ph}) ORDER BY status, title", tuple(app_slugs))
    activities = _rows("SELECT id, title, date, date_end, type, status, priority"
                       " FROM activities WHERE entity_slug = ? ORDER BY (date IS NULL), date", (slug,))
    return {"entity": ent[0], "channels": channels, "apps": apps,
            "memos": memos, "experiments": experiments,
            "features": features, "activities": activities}
```

- [ ] **Step 4: Run, expect PASS.** `python3 -m unittest tests.test_db_project -v`
- [ ] **Step 5: Commit.** `git add dashboard/db.py tests/test_db_project.py && git -c user.name='Leo Bot' -c user.email='you@example.com' commit -m "feat(db): project(slug) aggregate for the project-section views"`

---

### Task 2: `fileops` post CRUD + memo reader + plan wrapper

**Files:** Modify `dashboard/fileops.py` (add `import datetime`; append functions); Tests `tests/test_fileops_posts.py`, `tests/test_fileops_plan_args.py`.

- [ ] **Step 1: Write the failing tests.**

`tests/test_fileops_posts.py`:
```python
import importlib, json, sys, tempfile, unittest
from pathlib import Path
REPO = Path(__file__).resolve().parent.parent
REL = """# Portfolio Relationships

## Ventures
- slug: acme
  name: Acme

## Brands
- slug: acme-tok
  name: Acme TikTok
  venture: acme
"""
class T(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.root = Path(self.tmp.name)
        (self.root/"portfolio").mkdir(parents=True)
        (self.root/"portfolio"/"relationships.md").write_text(REL, encoding="utf-8")
        (self.root/"brands"/"acme-tok"/"content").mkdir(parents=True)
        sys.path.insert(0, str(REPO/"dashboard"))
        import fileops; importlib.reload(fileops); fileops.ROOT = self.root; self.f = fileops
    def tearDown(self): self.tmp.cleanup()
    def test_crud(self):
        pid = self.f.add_post("acme-tok", {"date":"2026-07-01","platform":"tiktok","pillar":"Teaser"})["id"]
        plan = self.root/"brands"/"acme-tok"/"content"/"plan-manual.json"
        self.assertEqual(json.loads(plan.read_text())["posts"][0]["status"], "planned")
        self.f.update_post(pid, {"pillar":"Big Teaser"})
        self.assertEqual(json.loads(plan.read_text())["posts"][0]["pillar"], "Big Teaser")
        self.f.delete_post(pid)
        self.assertEqual(json.loads(plan.read_text())["posts"], [])
    def test_add_unknown_brand(self):
        with self.assertRaises(self.f.ActionError): self.f.add_post("nope", {"date":"x"})
if __name__ == "__main__": unittest.main()
```

`tests/test_fileops_plan_args.py`:
```python
import importlib, sys, unittest
from pathlib import Path
REPO = Path(__file__).resolve().parent.parent
class T(unittest.TestCase):
    def setUp(self):
        sys.path.insert(0, str(REPO/"dashboard"))
        import fileops; importlib.reload(fileops); self.f = fileops
    def test_full(self):
        a = self.f._plan_args("acme-tok", {"period":"2026-07-01 to 2026-07-14","platforms":"tiktok,instagram","cadence":3,"focus":"launch"})
        for x in ("plan","acme-tok","--period","2026-07-01 to 2026-07-14","--platforms","tiktok,instagram","--cadence","3","--focus","launch"):
            self.assertIn(x, a)
    def test_requires_period(self):
        with self.assertRaises(self.f.ActionError): self.f._plan_args("acme-tok", {"period":""})
if __name__ == "__main__": unittest.main()
```

- [ ] **Step 2: Run both, expect FAIL** (`add_post`/`_plan_args` missing).
- [ ] **Step 3: Implement.** Add `import datetime` to `dashboard/fileops.py` imports, then append:
```python
_POST_FIELDS = ("date", "platform", "pillar", "working_title")


def add_post(brand_slug, fields):
    """Create a manual idea-slot in the brand's newest plan file (or plan-manual.json)."""
    if not (ROOT / "brands" / brand_slug).exists():
        raise ActionError(f"channel '{brand_slug}' not found")
    content = ROOT / "brands" / brand_slug / "content"
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
    existing = {p.get("id") for p in data["posts"]}
    pid = "m-" + datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    while pid in existing:
        pid += "x"
    post = {"id": pid, "status": "planned"}
    for k in _POST_FIELDS:
        v = (fields.get(k) or "").strip()
        if v:
            post[k] = v
    data["posts"].append(post)
    plan.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    reindex()
    return {"id": pid, "brand_slug": brand_slug}


def update_post(post_id, fields):
    """Edit a slot's authored fields in its plan file, then re-index."""
    ctx = find_post(post_id)
    for k in _POST_FIELDS:
        if k in fields:
            v = (fields.get(k) or "").strip()
            if v:
                ctx["post"][k] = v
            else:
                ctx["post"].pop(k, None)
    _write_plan(ctx)
    reindex()
    return {"id": post_id}


def delete_post(post_id):
    """Remove a slot (and its brief file, if any), then re-index."""
    ctx = find_post(post_id)
    ctx["data"]["posts"] = [p for p in ctx["data"]["posts"] if p.get("id") != post_id]
    brief = ROOT / "brands" / ctx["brand_slug"] / "content" / "briefs" / f"{post_id}.json"
    if brief.exists():
        brief.unlink()
    _write_plan(ctx)
    reindex()
    return {"id": post_id, "deleted": True}


def read_authored_json(relpath):
    """Read an authored JSON file (memo/experiment body) by its os.db-relative path."""
    if not relpath:
        return None
    f = ROOT / relpath
    if not f.exists():
        return None
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"_error": "file is not valid JSON"}


def _plan_args(brand_slug, params):
    """Build the generate.py plan argv from UI params. Raises if period is missing."""
    period = (params.get("period") or "").strip()
    if not period:
        raise ActionError("a period is required (e.g. '2026-07-01 to 2026-07-14')")
    args = [sys.executable, str(ROOT / "generate.py"),
            "--workspace", str(ROOT), "plan", brand_slug, "--period", period]
    platforms = (params.get("platforms") or "").strip()
    if platforms:
        args += ["--platforms", platforms]
    cadence = params.get("cadence")
    if cadence not in (None, ""):
        args += ["--cadence", str(int(cadence))]
    focus = (params.get("focus") or "").strip()
    if focus:
        args += ["--focus", focus]
    return args


def run_plan(brand_slug, params):
    """Generate a content calendar for a channel via claude -p, then re-index."""
    _brand_dir(brand_slug)
    res = subprocess.run(_plan_args(brand_slug, params), capture_output=True, text=True)
    if res.returncode != 0:
        raise ActionError(f"plan job failed: {(res.stderr or res.stdout).strip()[:800]}")
    reindex()
    return {"brand_slug": brand_slug, "stdout": res.stdout.strip()[:400]}
```

- [ ] **Step 4: Run both, expect PASS.**
- [ ] **Step 5: Commit.** `git add dashboard/fileops.py tests/test_fileops_posts.py tests/test_fileops_plan_args.py && git -c user.name='Leo Bot' -c user.email='you@example.com' commit -m "feat(fileops): post CRUD, authored-json reader, plan wrapper"`

---

### Task 3: server routes — project read + post/plan writes

**Files:** Modify `dashboard/server.py`.

- [ ] **Step 1: Add the project READ route.** In `do_GET`, after the `/api/tree` block, add:
```python
            if path.startswith("/api/project/"):
                slug = path[len("/api/project/"):]
                data = db.project(slug)
                if data is None:
                    return self._send(404, {"error": f"project '{slug}' not found"})
                for m in data["memos"]:
                    m["body"] = fileops.read_authored_json(m.get("file_path"))
                for x in data["experiments"]:
                    x["body"] = fileops.read_authored_json(x.get("file_path"))
                return self._send(200, data)
```

- [ ] **Step 2: Add the WRITE routes.** In `do_POST`, BEFORE the existing `/api/post/.../status` block, add:
```python
            if path.startswith("/api/channel/") and path.endswith("/posts"):
                slug = path[len("/api/channel/"):-len("/posts")]
                return self._send(200, {"ok": True, **fileops.add_post(slug, body)})
            if path.startswith("/api/channel/") and path.endswith("/plan"):
                slug = path[len("/api/channel/"):-len("/plan")]
                return self._send(200, {"ok": True, **fileops.run_plan(slug, body)})
            if path.startswith("/api/post/") and path.endswith("/update"):
                post_id = path[len("/api/post/"):-len("/update")]
                return self._send(200, {"ok": True, **fileops.update_post(post_id, body)})
            if path.startswith("/api/post/") and path.endswith("/delete"):
                post_id = path[len("/api/post/"):-len("/delete")]
                return self._send(200, {"ok": True, **fileops.delete_post(post_id)})
```

- [ ] **Step 3: Verify.**
```bash
cd /path/to/Leo Bot && python3 dashboard/server.py & SRV=$!; sleep 2
echo "project:"; curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8765/api/project/acme
ADD=$(curl -s -X POST http://127.0.0.1:8765/api/channel/demo/posts -H 'Content-Type: application/json' -d '{"date":"2026-06-30","platform":"tiktok","pillar":"Test"}'); echo "$ADD"
PID=$(printf '%s' "$ADD" | python3 -c "import sys,json;print(json.load(sys.stdin)['id'])")
curl -s -X POST http://127.0.0.1:8765/api/post/$PID/delete; echo
kill $SRV 2>/dev/null
```
Expected: project → `200`; add → `{"ok":true,"id":"m-…"}`; delete → `{"ok":true,…,"deleted":true}`.

- [ ] **Step 4: Commit.** `git add dashboard/server.py && git -c user.name='Leo Bot' -c user.email='you@example.com' commit -m "feat(server): /api/project read + post CRUD/plan write routes"`

---

### Task 4: Rail rework — full project sections + Social-profiles group

**Files:** Modify `dashboard/app.html` (the `<script>`: `renderRail`, `STATE`, nav handlers; add section/profile CSS to `<style>`).

- [ ] **Step 1: Add rail CSS.** In `<style>` before `</style>`:
```css
  .grp{margin:3px 0 1px;padding:8px 9px 2px;font:600 9.5px/1 var(--body);text-transform:uppercase;letter-spacing:.1em;color:var(--dim)}
  .kid{margin-left:12px;border-left:1px solid var(--hair);padding-left:9px}
  .kid a{color:var(--dim);font-size:12px}
  .kid a.active{color:var(--sky)}
  .sec a{display:flex;align-items:center;gap:9px;padding:6px 9px;border-radius:10px;color:var(--ink2);text-decoration:none;cursor:pointer;font-size:12.5px}
  .sec a .ico{width:15px;text-align:center;color:var(--dim)}
  .sec a:hover{background:rgba(255,255,255,.6)}
  .sec a.active{background:#fff;color:var(--sky);font-weight:600}
  .sec a .c{margin-left:auto;font-size:11px;color:var(--dim)}
```

- [ ] **Step 2: Replace `STATE`, `renderRail`, `highlight`, `selectView`, `selectChannel`** with a section-aware version. The sections per project are fixed; channels come from `/api/tree`. Replace those functions with:
```javascript
const SECTIONS = [
  {key:"overview", ico:"◇", label:"Overview"},
  {key:"validation", ico:"◎", label:"Problem & validation"},
  {key:"experiments", ico:"⚗", label:"Experiments"},
  {key:"pricing", ico:"◧", label:"Positioning & pricing"},
  {key:"product", ico:"▣", label:"Product"},
  {key:"operations", ico:"✓", label:"Operations"},
];
let STATE = {view:"calendar", project:null, section:null, channel:null};

async function renderRail(){
  const tree = await api("/api/tree");
  const projects = tree.map(p=>`
    <div class="proj" style="margin-top:8px;padding:10px 9px 8px;border-radius:14px;background:rgba(255,255,255,.5);border:1px solid var(--gline)">
      <div class="ph" style="display:flex;align-items:center;gap:8px;font:700 13.5px/1 var(--disp);padding:2px 4px 8px">${esc(p.name)}
        <span class="tag" style="margin-left:auto;font:600 9px/1 var(--body);letter-spacing:.06em;text-transform:uppercase;color:var(--sky);background:var(--sky-soft);padding:3px 7px;border-radius:20px">${esc(p.type)}</span></div>
      <nav class="sec">
        ${SECTIONS.map(s=>`<a data-project="${p.slug}" data-section="${s.key}"><span class="ico">${s.ico}</span> ${esc(s.label)}${
          s.key==="experiments"&&p.experiments?`<span class="c">${p.experiments}</span>`:
          s.key==="product"&&p.apps.reduce((n,a)=>n+a.features,0)?`<span class="c">${p.apps.reduce((n,a)=>n+a.features,0)}</span>`:""}</a>`).join("")}
        <div class="grp">Social profiles</div>
        <div class="kid">
          ${p.channels.map(c=>`<a data-channel="${c.slug}">${esc(c.name)} <span class="c">${c.posts}</span></a>`).join("")||'<a style="color:var(--dim)">none yet</a>'}
        </div>
      </nav>
    </div>`).join("");
  $("#rail").innerHTML = `
    <div class="brand"><span class="mark"></span><b>Leo Bot</b></div>
    <div class="navlabel label">Across everything</div>
    <nav class="nav">
      <a data-view="needs"><span class="ico">◉</span> Needs you</a>
      <a data-view="operations"><span class="ico">▤</span> Operations</a>
      <a data-view="calendar"><span class="ico">▦</span> Calendar</a>
    </nav>
    <div class="navlabel label">Projects</div>
    ${projects}`;
  $("#rail").querySelectorAll("[data-view]").forEach(a=>a.onclick=()=>selectGlobal(a.dataset.view));
  $("#rail").querySelectorAll("[data-section]").forEach(a=>a.onclick=()=>selectSection(a.dataset.project,a.dataset.section));
  $("#rail").querySelectorAll("[data-channel]").forEach(a=>a.onclick=()=>selectChannel(a.dataset.channel));
  highlight();
}
function highlight(){
  document.querySelectorAll("#rail [data-view]").forEach(a=>a.classList.toggle("active",STATE.view===a.dataset.view&&!STATE.project&&!STATE.channel));
  document.querySelectorAll("#rail [data-section]").forEach(a=>a.classList.toggle("active",STATE.view==="section"&&STATE.project===a.dataset.project&&STATE.section===a.dataset.section));
  document.querySelectorAll("#rail [data-channel]").forEach(a=>a.classList.toggle("active",STATE.view==="channel"&&STATE.channel===a.dataset.channel));
}
function selectGlobal(v){ STATE={view:v,project:null,section:null,channel:null}; highlight();
  if(v==="calendar") renderTimeline(); else if(v==="operations") renderOperations(); else renderNeeds(); }
function selectSection(project,section){ STATE={view:"section",project,section,channel:null}; highlight(); renderProjectSection(project,section); }
function selectChannel(slug){ STATE={view:"channel",project:null,section:null,channel:slug}; highlight(); renderChannel(slug); }
```
> NOTE: `boot()` already calls `renderRail()` then a default view. Update `boot()` so the default is the calendar: `async function boot(){ await renderRail(); selectGlobal("calendar"); }`.
> `renderNeeds` and `renderOperations` are simple stubs for this phase — add them near `renderTimeline`:
```javascript
async function renderNeeds(){ $("#main").innerHTML=`<div class="topbar"><div><div class="crumbs">Across everything</div><h1 class="title">Needs you</h1></div></div><div class="scroll"><div style="padding:24px 4px;color:var(--dim)">Your prioritized to-act list arrives in a later phase. For now, open a project section or a profile.</div></div>`; }
async function renderOperations(){ $("#main").innerHTML=`<div class="topbar"><div><div class="crumbs">Across everything</div><h1 class="title">Operations</h1></div></div><div class="scroll"><div style="padding:24px 4px;color:var(--dim)">Cross-project tasks &amp; activities arrive in a later phase.</div></div>`; }
```

- [ ] **Step 3: Smoke-test the rail.** Run the server; confirm Acme shows the six sections + a "Social profiles" group with Demo/Sample indented; clicking a section / a profile / Calendar switches the center. Stop.
- [ ] **Step 4: Commit.** `git add dashboard/app.html && git -c user.name='Leo Bot' -c user.email='you@example.com' commit -m "feat(dashboard): project-scoped rail with sections + Social-profiles group"`

---

### Task 5: Project-section view (data-driven, one renderer)

One `renderProjectSection(slug, section)` fetches `/api/project/<slug>` and renders the chosen section. DRY: a shared list/empty pattern.

**Files:** Modify `dashboard/app.html` (`<script>`).

- [ ] **Step 1: Add the renderer** near `renderChannel`:
```javascript
function plainStatus(s){ return ({planned:"Idea",approved_slot:"Idea",briefed:"Draft",approved:"Ready",scheduled:"Scheduled",published:"Published",rejected:"Archived"})[s]||s; }

async function renderProjectSection(slug, section){
  const p = await api(`/api/project/${slug}`);
  const title = (SECTIONS.find(s=>s.key===section)||{}).label || section;
  const memo = t => (p.memos.filter(m=>m.type===t).sort((a,b)=>b.version-a.version)[0]||null);
  const body = {
    overview: ()=>{
      const e=p.entity, pv=memo("problem-validation"), as=memo("assessment");
      const vb = pv&&pv.body||{}, ab = as&&as.body||{};
      return `<div class="grid2">
        ${kv("Stage", e.status)}${kv("Priority", e.priority)}${kv("Hours/week", e.hours_per_week??"—")}
        ${kv("Validation", vb.validation_status||"—")}${kv("Pace", ab.pace_recommendation||"—")}
        ${kv("Profiles", p.channels.map(c=>c.name).join(", ")||"—")}</div>
        ${pv?card("Problem & validation", `Status: <b>${esc(vb.validation_status||"?")}</b> · ${esc(vb.recommendation||"")}`):""}
        ${ab.riskiest_assumption?card("Riskiest assumption", esc(ab.riskiest_assumption)):""}`;
    },
    validation: ()=>{ const m=memo("problem-validation"); if(!m) return empty("No problem-validation memo yet.");
      const b=m.body||{}; return card(`Problem-validation v${m.version}`,
        `Status: <b>${esc(b.validation_status||"?")}</b> · severity: ${esc(b.severity||"?")}<br>${esc(b.recommendation||"")}`); },
    pricing: ()=>{ const items=p.memos.filter(m=>["positioning","pricing","competitors","icp"].includes(m.type));
      return items.length? items.map(m=>card(`${m.type} v${m.version}`, esc((m.body&&(m.body.recommendation||m.body.summary))||m.status))).join("") : empty("No positioning/pricing memos yet."); },
    experiments: ()=> p.experiments.length? p.experiments.map(x=>card(esc(x.assumption),
        `Status: <b>${esc(x.status)}</b>${x.decision?` · decision: ${esc(x.decision)}`:""}`)).join("") : empty("No experiments yet."),
    product: ()=> p.features.length? listRows(p.features.map(f=>({pill:f.status, pillk:"draft", t:f.title, sub:[f.priority,f.target_date].filter(Boolean).join(" · ")}))) : empty("No roadmap features yet."),
    operations: ()=> p.activities.length? listRows(p.activities.map(a=>({pill:a.status, pillk:a.status==="done"?"sched":"idea", t:a.title, sub:[a.type,a.date].filter(Boolean).join(" · ")}))) : empty("No operations/tasks for this project yet."),
  };
  $("#main").innerHTML = `<div class="topbar"><div><div class="crumbs">${esc(p.entity.name)} · <b>${esc(title)}</b></div><h1 class="title">${esc(title)}</h1></div></div>
    <div class="scroll">${(body[section]||(()=>empty("Nothing here yet.")))()}</div>`;

  function kv(k,v){ return `<div class="kv"><span>${esc(k)}</span><b>${esc(v)}</b></div>`; }
  function card(h,html){ return `<div class="pcard"><h4>${esc(h)}</h4><div>${html}</div></div>`; }
  function empty(m){ return `<div style="padding:24px 4px;color:var(--dim)">${esc(m)}</div>`; }
  function listRows(items){ return `<div class="rowc">${items.map(i=>`<div class="post">
      <span class="stp ${i.pillk}">${esc(plainStatus(i.pill))}</span>
      <div class="t">${esc(i.t)}${i.sub?`<small>${esc(i.sub)}</small>`:""}</div></div>`).join("")}</div>`; }
}
```

- [ ] **Step 2: Add section CSS** in `<style>`:
```css
  .grid2{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin:6px 0 16px}
  .kv{background:rgba(255,255,255,.6);border:1px solid var(--gline);border-radius:12px;padding:11px 13px}
  .kv span{display:block;font-size:10.5px;color:var(--dim);text-transform:uppercase;letter-spacing:.06em;margin-bottom:4px}
  .kv b{font:600 14px/1.2 var(--disp)}
  .pcard{background:rgba(255,255,255,.66);border:1px solid var(--gline);border-radius:14px;padding:14px 16px;margin-bottom:12px}
  .pcard h4{margin:0 0 6px;font:600 13.5px/1.2 var(--disp)}
  .rowc{display:flex;flex-direction:column;gap:9px}
  .post{display:flex;align-items:center;gap:13px;background:rgba(255,255,255,.66);border:1px solid var(--gline);border-radius:14px;padding:12px 14px}
  .post .t{font:600 13px/1.3 var(--disp);flex:1;min-width:0}
  .post .t small{display:block;color:var(--dim);font-weight:500;font-size:11px;margin-top:2px}
  .stp{font:600 10px/1 var(--body);padding:4px 9px;border-radius:20px;white-space:nowrap}
  .stp.idea{background:var(--amber-soft);color:var(--amber)} .stp.draft{background:rgba(124,92,214,.16);color:#7c5cd6}
  .stp.ready{background:var(--sky-soft);color:var(--sky)} .stp.sched{background:var(--teal-soft);color:var(--teal)}
  .amber-soft{}
```
> If `--amber-soft`/`--amber` aren't defined in `:root`, add: `--amber:#c98a1a; --amber-soft:rgba(201,138,26,.16);`.

- [ ] **Step 3: Smoke-test** each section for Acme (Overview shows stage/validation/pace + memo cards; Experiments shows the WTP probe; Product shows the 9 features; others show clean empty states). Stop.
- [ ] **Step 4: Commit.** `git add dashboard/app.html && git -c user.name='Leo Bot' -c user.email='you@example.com' commit -m "feat(dashboard): data-driven project-section views"`

---

### Task 6: Reworked content view (plain stages + clear actions + authoring)

Replace the channel's "Content / Planned / Guidelines" segments with one list + filter chips (**All · Ideas · Drafts · Scheduled · Published**), plain-stage pills, a single obvious action per row, plus **＋ Add idea**, **✦ Generate ideas**, and **Edit/Delete**. Guidelines moves to a side button.

**Files:** Modify `dashboard/app.html` (replace `renderChannel`; add the `modal` helper + content CSS).

- [ ] **Step 1: Add modal + content CSS** in `<style>`:
```css
  .modal-bg{position:fixed;inset:0;background:rgba(20,24,33,.34);backdrop-filter:blur(2px);z-index:40;display:flex;align-items:center;justify-content:center}
  .modal{background:#fff;border:1px solid var(--gline);border-radius:18px;box-shadow:var(--shadow);width:min(440px,92vw);padding:20px 22px}
  .modal h3{font:700 15px/1.2 var(--disp);margin:0 0 12px}
  .modal label{display:block;font-size:11px;color:var(--dim);margin:10px 0 4px;text-transform:uppercase;letter-spacing:.06em}
  .modal input,.modal textarea{width:100%;border:1px solid var(--hair);border-radius:10px;padding:9px 11px;font:inherit;background:rgba(255,255,255,.9)}
  .modal .actions{display:flex;gap:8px;justify-content:flex-end;margin-top:18px}
  .filters{display:flex;gap:7px;align-items:center;flex-wrap:wrap;margin:6px 0 14px}
  .chip{font:600 11.5px/1 var(--body);padding:7px 12px;border-radius:20px;background:rgba(255,255,255,.6);border:1px solid var(--gline);color:var(--dim);cursor:pointer}
  .chip.on{background:var(--graphite);color:#fff;border-color:var(--graphite)}
  .chip .n{opacity:.7;margin-left:5px}
  .gl{margin-left:auto;font-size:12px;color:var(--sky);cursor:pointer}
  .pl{font:600 10px/1 var(--body);padding:4px 8px;border-radius:6px;background:#1b1620;color:#fff;white-space:nowrap}
  .pl.ig{background:rgba(190,30,116,.12);color:#b81e74}
  .post .go{border:0;background:var(--sky-soft);color:var(--sky);font:600 11.5px/1 var(--body);padding:8px 12px;border-radius:9px;cursor:pointer;white-space:nowrap}
  .post .more{color:var(--dim);cursor:pointer;padding:4px 8px;font-weight:700}
```

- [ ] **Step 2: Add the `modal` helper** after `toast(...)`:
```javascript
function modal(title, innerHTML, onsubmit){
  const bg=document.createElement("div"); bg.className="modal-bg";
  bg.innerHTML=`<div class="modal"><h3>${esc(title)}</h3><form>${innerHTML}
    <div class="actions"><button type="button" class="btn" data-x>Cancel</button><button type="submit" class="btn primary">Save</button></div></form></div>`;
  const close=()=>bg.remove();
  bg.querySelector("[data-x]").onclick=close;
  bg.onclick=e=>{ if(e.target===bg) close(); };
  bg.querySelector("form").onsubmit=async e=>{ e.preventDefault();
    const data={}; bg.querySelectorAll("[name]").forEach(i=>data[i.name]=i.value);
    try{ await onsubmit(data); close(); }catch(err){ toast("✗ "+err.message); } };
  document.body.appendChild(bg);
  const f=bg.querySelector("input,textarea"); if(f) f.focus();
}
```

- [ ] **Step 3: Replace `renderChannel`** entirely with the reworked version:
```javascript
const STAGE_GROUP = {planned:"ideas",approved_slot:"ideas",briefed:"drafts",approved:"drafts",scheduled:"scheduled",published:"published",rejected:"archived"};
const NEXT = {  // the single obvious action per stage
  planned:{label:"Write it →",brief:1}, approved_slot:{label:"Write it →",brief:1},
  briefed:{label:"Review →",to:"approved"}, approved:{label:"Schedule →",to:"scheduled"},
  scheduled:{label:"Mark published →",to:"published"}, published:null, rejected:{label:"Restore",to:"planned"},
};

async function renderChannel(slug){
  const posts = await api(`/api/channel/${slug}/posts`);
  const count = g => posts.filter(p=>STAGE_GROUP[p.status]===g).length;
  let FILTER = "all";
  $("#main").innerHTML = `<div class="topbar"><div><div class="crumbs">Acme · Social profiles · <b>${esc(slug)}</b></div>
      <h1 class="title">${esc(slug)}</h1><div class="titlemeta">${posts.length} posts</div></div>
      <div style="margin-left:auto;display:flex;gap:8px"><button class="btn" id="addIdea">＋ Add idea</button><button class="btn primary" id="genIdeas">✦ Generate ideas</button></div></div>
    <div class="scroll">
      <div class="filters">
        <span class="chip on" data-f="all">All <span class="n">${posts.length}</span></span>
        <span class="chip" data-f="ideas">💡 Ideas <span class="n">${count("ideas")}</span></span>
        <span class="chip" data-f="drafts">✍ Drafts <span class="n">${count("drafts")}</span></span>
        <span class="chip" data-f="scheduled">📅 Scheduled <span class="n">${count("scheduled")}</span></span>
        <span class="chip" data-f="published">✓ Published <span class="n">${count("published")}</span></span>
        <span class="gl" id="glBtn">⚙ Guidelines</span>
      </div>
      <div class="rowc" id="list"></div></div>`;

  function drawList(){
    const list = posts.filter(p=>FILTER==="all"||STAGE_GROUP[p.status]===FILTER);
    const el = $("#list");
    if(!list.length){ el.innerHTML=`<div style="padding:24px 4px;color:var(--dim)">Nothing here. Add an idea or generate a batch.</div>`; return; }
    el.innerHTML = list.map(p=>{
      const grp=STAGE_GROUP[p.status], pk=({ideas:"idea",drafts:"draft",scheduled:"sched",published:"ready",archived:"idea"})[grp]||"idea";
      const n=NEXT[p.status];
      const sub = p.status==="planned"||p.status==="approved_slot" ? "Just an idea — not written yet" : (p.brief_path?"Written":"");
      return `<div class="post">
        <span class="stp ${pk}">${esc(plainStatus(p.status))}</span>
        <span class="pl ${p.platform==='instagram'?'ig':''}">${esc(p.platform||'?')}</span>
        <div class="t">${esc(p.pillar||p.id)}<small>${[sub,p.date].filter(Boolean).map(esc).join(" · ")}</small></div>
        ${n?`<button class="go" data-act="${p.id}">${n.label}</button>`:""}
        <span class="more" data-menu="${p.id}">⋯</span></div>`;
    }).join("");
    el.querySelectorAll("[data-act]").forEach(b=>b.onclick=()=>doNext(b.dataset.act));
    el.querySelectorAll("[data-menu]").forEach(b=>b.onclick=()=>rowMenu(b.dataset.menu));
  }
  function byId(id){ return posts.find(p=>p.id===id)||{}; }
  async function doNext(id){ const p=byId(id), n=NEXT[p.status]; if(!n) return;
    try{ if(n.brief){ toast("Writing via claude -p… (a few seconds)"); await api(`/api/post/${id}/brief`,{method:"POST"}); toast("Draft ready ✓"); }
      else { await api(`/api/post/${id}/status`,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({status:n.to})}); toast("✓ "+plainStatus(n.to)); }
      renderChannel(slug); renderRail(); }catch(e){ toast("✗ "+e.message); } }
  function rowMenu(id){ const p=byId(id);
    modal("Edit idea", `<label>Date</label><input name="date" value="${esc(p.date||"")}">
      <label>Platform</label><input name="platform" value="${esc(p.platform||"")}">
      <label>Pillar / idea</label><input name="pillar" value="${esc(p.pillar||"")}">
      <p style="margin:14px 0 0"><button type="button" class="btn" id="delBtn" style="color:var(--ink)">Delete this post</button></p>`,
      async data=>{ await api(`/api/post/${id}/update`,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(data)}); toast("Saved ✓"); renderChannel(slug); });
    setTimeout(()=>{ const d=document.getElementById("delBtn"); if(d) d.onclick=async()=>{ if(!confirm("Delete this post and its written content?"))return;
      try{ await api(`/api/post/${id}/delete`,{method:"POST"}); document.querySelector(".modal-bg")?.remove(); toast("Deleted ✓"); renderChannel(slug); renderRail(); }catch(e){ toast("✗ "+e.message);} }; },0);
  }

  $("#main").querySelectorAll(".chip").forEach(c=>c.onclick=()=>{ FILTER=c.dataset.f;
    $("#main").querySelectorAll(".chip").forEach(x=>x.classList.toggle("on",x===c)); drawList(); });
  $("#addIdea").onclick=()=>modal("Add idea", `<label>Date</label><input name="date" placeholder="2026-07-01">
    <label>Platform</label><input name="platform" placeholder="tiktok / instagram">
    <label>Pillar / idea</label><input name="pillar" placeholder="e.g. Example Topic">`,
    async data=>{ await api(`/api/channel/${slug}/posts`,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(data)}); toast("Idea added ✓"); renderChannel(slug); renderRail(); });
  $("#genIdeas").onclick=()=>modal("Generate ideas (claude -p)", `<label>Period</label><input name="period" placeholder="2026-07-01 to 2026-07-14">
    <label>Platforms</label><input name="platforms" placeholder="tiktok,instagram">
    <label>Cadence (per platform / week)</label><input name="cadence" placeholder="3">
    <label>Focus (optional)</label><input name="focus" placeholder="push the launch">`,
    async data=>{ toast("Generating via claude -p… (10–30s)"); await api(`/api/channel/${slug}/plan`,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(data)}); toast("Ideas generated ✓"); renderChannel(slug); renderRail(); });
  $("#glBtn").onclick=()=>openGuidelines(slug);
  drawList();
}

function openGuidelines(slug){
  modal("Channel guidelines", `<p style="color:var(--dim);font-size:12px;margin:0 0 8px">Injected into every generation. Use <code>## General</code> + per-platform sections.</p>
    <textarea name="text" rows="12" style="font:12px/1.5 ui-monospace,Menlo,monospace"></textarea>
    <p style="margin:12px 0 0"><button type="button" class="btn" id="refineBtn">✨ Refine with AI</button></p>`,
    async data=>{ await api(`/api/brand/${slug}/guidelines`,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({text:data.text})}); toast("Guidelines saved ✓"); });
  api(`/api/brand/${slug}/guidelines`).then(d=>{ const t=document.querySelector(".modal [name=text]"); if(t) t.value=d.text||""; });
  setTimeout(()=>{ const r=document.getElementById("refineBtn"); if(r) r.onclick=async()=>{ const t=document.querySelector(".modal [name=text]");
    r.textContent="refining…"; try{ const d=await api(`/api/brand/${slug}/guidelines/refine`,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({text:t.value})}); t.value=d.refined; r.textContent="✨ Refine with AI"; }catch(e){ r.textContent="✨ Refine with AI"; toast("✗ "+e.message);} }; },0);
}
```
> The old `renderChannel` had a `guidelinesEditor`/`cards`/`act` set — they are fully replaced by the above. Remove any now-unused leftovers so there are no dead functions.

- [ ] **Step 4: Smoke-test** Demo: filter chips switch the list; the draft shows **Review →**, ideas show **Write it →**; **＋ Add idea** adds; **⋯** edits/deletes; **⚙ Guidelines** opens, refines, saves; **✦ Generate ideas** opens (cancel to avoid spend). Stop.
- [ ] **Step 5: Commit.** `git add dashboard/app.html && git -c user.name='Leo Bot' -c user.email='you@example.com' commit -m "feat(dashboard): plain-language content list with stages, actions, authoring"`

---

### Task 7: Month-calendar "Calendar" (all seven days)

**Files:** Modify `dashboard/app.html` (replace `renderTimeline`; add calendar CSS + `CAL` state).

- [ ] **Step 1: Add calendar CSS** in `<style>`:
```css
  .cal-head{display:flex;align-items:center;gap:10px;margin:10px 0 12px}
  .cal-head .mlabel{font:700 16px/1 var(--disp)}
  .cal-nav{display:flex;gap:6px;margin-left:auto}
  .cal-nav button{border:1px solid var(--hair);background:rgba(255,255,255,.7);border-radius:10px;padding:6px 11px;cursor:pointer;font:600 12px/1 var(--body);color:var(--ink)}
  .cal{display:grid;grid-template-columns:repeat(7,1fr);gap:7px}
  .dow{font:600 10px/1 var(--body);text-transform:uppercase;letter-spacing:.08em;color:var(--dim);padding:2px 4px 4px}
  .day{min-height:84px;border:1px solid var(--hair);border-radius:12px;padding:6px 7px;background:rgba(255,255,255,.45)}
  .day.out{background:transparent;opacity:.5}
  .day .n{font:600 11px/1 var(--body);color:var(--dim);margin-bottom:5px}
  .day.today{border-color:var(--sky);box-shadow:0 0 0 2px var(--sky-soft)}
  .day.today .n{color:var(--sky)}
  .ev{font-size:10.5px;border-radius:6px;padding:3px 6px;margin-bottom:3px;line-height:1.25;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .ev.post{background:var(--sky-soft);color:var(--sky)}
  .ev.experiment{background:rgba(124,92,214,.16);color:#7c5cd6}
  .ev.feature{background:var(--teal-soft);color:var(--teal)}
  .ev.activity{background:var(--amber-soft);color:var(--amber)}
  .ev.milestone{background:rgba(27,31,39,.08);color:var(--ink2)}
```

- [ ] **Step 2: Replace `renderTimeline`** with the calendar (and keep the name `renderTimeline` since `selectGlobal("calendar")` calls it):
```javascript
let CAL = (function(){ const d=new Date(); return {y:d.getFullYear(), m:d.getMonth()}; })();
const MONTHS = ["January","February","March","April","May","June","July","August","September","October","November","December"];
function calShift(delta){ let m=CAL.m+delta,y=CAL.y; if(m<0){m=11;y--;} if(m>11){m=0;y++;} CAL={y,m}; renderTimeline(); }
function calToday(){ const d=new Date(); CAL={y:d.getFullYear(),m:d.getMonth()}; renderTimeline(); }
async function renderTimeline(){
  const rows = await api("/api/timeline");
  const byDay={}; rows.forEach(r=>{ if(r.date){ (byDay[r.date]=byDay[r.date]||[]).push(r); } });
  const first=new Date(CAL.y,CAL.m,1), lead=(first.getDay()+6)%7, start=new Date(CAL.y,CAL.m,1-lead);
  const iso=d=>`${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,"0")}-${String(d.getDate()).padStart(2,"0")}`;
  const today=iso(new Date());
  let cells="";
  for(let i=0;i<42;i++){
    const d=new Date(start.getFullYear(),start.getMonth(),start.getDate()+i), k=iso(d), out=d.getMonth()!==CAL.m;
    const evs=(byDay[k]||[]).map(r=>`<div class="ev ${esc(r.kind)}" title="${esc(r.title||"")}">${esc(r.title||r.kind)}</div>`).join("");
    cells+=`<div class="day${out?' out':''}${k===today?' today':''}"><div class="n">${d.getDate()}</div>${evs}</div>`;
    if(i>=34 && d.getMonth()!==CAL.m && (i+1)%7===0) break;
  }
  const dow=["Mon","Tue","Wed","Thu","Fri","Sat","Sun"].map(x=>`<div class="dow">${x}</div>`).join("");
  $("#main").innerHTML=`<div class="topbar"><div><div class="crumbs">Across everything</div><h1 class="title">Calendar</h1></div></div>
    <div class="scroll"><div class="cal-head"><span class="mlabel">${MONTHS[CAL.m]} ${CAL.y}</span>
      <div class="cal-nav"><button id="cprev">‹</button><button id="ctoday">Today</button><button id="cnext">›</button></div></div>
      <div class="cal">${dow}${cells}</div></div>`;
  $("#cprev").onclick=()=>calShift(-1); $("#cnext").onclick=()=>calShift(1); $("#ctoday").onclick=calToday;
}
```

- [ ] **Step 3: Smoke-test** the **Calendar** nav item: month grid, all seven days, today highlighted, items on days, ‹ Today › nav. Stop.
- [ ] **Step 4: Commit.** `git add dashboard/app.html && git -c user.name='Leo Bot' -c user.email='you@example.com' commit -m "feat(dashboard): month-calendar view (all seven days, month nav)"`

---

### Task 8: Phase close-out

- [ ] **Step 1: Full suite** — `python3 -m unittest discover -s tests -v` → all PASS (Phase 1's 3 + `test_db_project` (1) + `test_fileops_posts` (2) + `test_fileops_plan_args` (2) = 8).
- [ ] **Step 2: Update `docs/guide.md`** layout section: note that each **project** exposes Overview / Problem & validation / Experiments / Positioning & pricing / Product / Operations, with **Social profiles** nested inside; content uses plain stages (Idea → Draft → Scheduled → Published) with **＋ Add idea** / **✦ Generate ideas** / Edit / Delete; and **Calendar** is a month grid. Keep the rest intact.
- [ ] **Step 3: Commit.** `git add docs/guide.md && git -c user.name='Leo Bot' -c user.email='you@example.com' commit -m "docs: project workspace, content stages, calendar"`

---

## Self-Review notes (for the executor)

- **Fixes the three user complaints:** social profiles nested under the project (Task 4), project exposes its full GTM scope via sections (Tasks 1, 4, 5), content is plain-language stages with one clear action per row (Task 6).
- **Invariant:** every write is `fileops` → file mutation → `reindex()`; `db.py` stays read-only; `/api/project` enriches with `fileops.read_authored_json` (reads authored files, never os.db).
- **Deferred (own later phases):** the live Consultant + propose→apply; a real **Needs you** and cross-project **Operations** list (stubbed here); CRUD on experiments/activities/projects/channels (this phase is posts only); editing memos in-app (Overview/Validation are read-only this phase).
- **Type/route consistency:** UI fetch paths match server routes exactly — reads `/api/tree`, `/api/project/<slug>`, `/api/channel/<slug>/posts`, `/api/timeline`, `/api/brand/<slug>/guidelines`; writes `/api/channel/<slug>/posts`, `/api/channel/<slug>/plan`, `/api/post/<id>/{status,brief,update,delete}`, `/api/brand/<slug>/guidelines{,/refine}`. Plain-stage mapping centralised in `plainStatus()` + `STAGE_GROUP` + `NEXT`.
