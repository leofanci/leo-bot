# Dashboard Redesign — Phase 1: Shell & Reads — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the tab-based dashboard with the locked project-centric three-panel glass shell, wired to the existing read + post-action endpoints — a full, non-regressing replacement of `app.html` plus the new `/api/tree` and channel read endpoints. No new mutations yet (those are Phase 2+).

**Architecture:** Files stay the source of truth; `os.db` is the read-only derived index (unchanged invariant). This phase adds (a) a schema widening so a project can be a non-venture, (b) `index.py` parsing of a `## Projects` section + a `project`/`venture` parent alias, (c) read helpers `db.tree()` and `db.channel_posts()`, (d) two GET endpoints, and (e) a rewritten `app.html` that renders the rail from `/api/tree` and reuses the existing `/api/post/...` actions and `/api/brand/.../guidelines` endpoints.

**Tech Stack:** Python 3 stdlib only (`sqlite3`, `http.server`, `subprocess`, `unittest`), vanilla-JS single-file `app.html`. No framework, no third-party deps.

**Reference:** Spec at `docs/superpowers/specs/2026-06-16-dashboard-redesign-design.md`. Original visual mockup is intentionally not included in the public repository.

---

### Task 0: Initialize git (enables the frequent-commit workflow)

This workspace is not yet a git repo; the plan assumes commits.

**Files:**
- Create: `.gitignore`

- [ ] **Step 1: Init the repo**

Run:
```bash
cd /path/to/Leo Bot
git init
```
Expected: `Initialized empty Git repository …`

- [ ] **Step 2: Write `.gitignore`**

Create `.gitignore` with exactly:
```
database/data/os.db
.superpowers/
__pycache__/
*.pyc
```

- [ ] **Step 3: First commit**

Run:
```bash
git add -A && git commit -m "chore: init repo with gitignore"
```
Expected: a commit is created; `database/data/os.db` and `.superpowers/` are untracked.

---

### Task 1: Widen `entities.type` so a project can be a non-venture

The current CHECK is `type IN ('venture','brand','web_app','external')`. Add `'project'`. Because `os.db` is wiped and fully rebuilt from `*.sql` on every index run, editing the consolidated DDL in place is correct — there is no persisted data to migrate.

**Files:**
- Modify: `database/migrations/0001_init.sql:17`

- [ ] **Step 1: Edit the CHECK**

In `database/migrations/0001_init.sql`, change the `entities.type` line from:
```sql
  type           TEXT NOT NULL CHECK (type IN ('venture','brand','web_app','external')),
```
to:
```sql
  type           TEXT NOT NULL CHECK (type IN ('venture','project','brand','web_app','external')),
```

- [ ] **Step 2: Rebuild and verify it still indexes cleanly**

Run:
```bash
python3 index.py
```
Expected: ends with `Rebuilt database/data/os.db` and per-table counts (no `INDEX FAILED`).

- [ ] **Step 3: Commit**

```bash
git add database/migrations/0001_init.sql
git commit -m "feat(schema): allow entities.type='project' for non-venture projects"
```

---

### Task 2: Parse standalone `## Projects` + a `project`/`venture` parent alias in `index.py`

Today `collect_entities` reads Ventures, Brands, Web Apps from `relationships.md`. Add a Projects section (entities of `type='project'`), and let brands/apps name their parent with either `project:` or `venture:`.

**Files:**
- Modify: `index.py:175-224` (inside `collect_entities`)
- Test: `tests/test_index_projects.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_index_projects.py`:
```python
import json, sqlite3, subprocess, sys, tempfile, unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

RELATIONSHIPS = """# Portfolio Relationships

## Projects
- slug: demo-project
  name: Weekly Demo Pod
  type: social
  priority: secondary

## Brands
- slug: demo-channel
  name: Demo Channel
  project: demo-project
  channels: [instagram]
"""

class TestStandaloneProjects(unittest.TestCase):
    def _build(self, root: Path):
        (root / "portfolio").mkdir(parents=True)
        (root / "portfolio" / "relationships.md").write_text(RELATIONSHIPS, encoding="utf-8")
        res = subprocess.run([sys.executable, str(REPO / "index.py"), str(root)],
                             capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, res.stderr)
        return sqlite3.connect(root / "database" / "data" / "os.db")

    def test_project_entity_and_channel_belongs_to_it(self):
        with tempfile.TemporaryDirectory() as d:
            conn = self._build(Path(d))
            types = dict(conn.execute("SELECT slug, type FROM entities").fetchall())
            self.assertEqual(types.get("demo-project"), "project")
            self.assertEqual(types.get("demo-channel"), "brand")
            edge = conn.execute(
                "SELECT 1 FROM relationships WHERE from_slug='demo-channel'"
                " AND to_slug='demo-project' AND kind='belongs_to'").fetchone()
            self.assertIsNotNone(edge, "brand should belong_to its standalone project")
            conn.close()

if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run it to confirm it fails**

Run:
```bash
python3 -m unittest tests.test_index_projects -v
```
Expected: FAIL — `demo-project` is `None` (Projects section not parsed) or the index aborts on the unknown `project:` field reference.

- [ ] **Step 3: Add Projects parsing**

In `index.py`, inside `collect_entities`, immediately after the `# Ventures` loop (after the block ending at line ~190, before `# Brands`), insert:
```python
    # Projects (standalone — may be non-venture: social, content, etc.)
    for r in section_records("project"):
        slug = r.get("slug")
        if not slug:
            continue
        entities.append({
            "slug": slug,
            "type": "project",
            "name": r.get("name") or slug,
            "priority": r.get("priority"),
            "status": r.get("status") or "active",
            "hours_per_week": first_int(r.get("hours_per_week")),
            "file_path": None,
            "updated_at": now_iso(),
        })
```

- [ ] **Step 4: Accept `project:` as a parent alias for brands and apps**

In the `# Brands` loop, replace:
```python
        if r.get("venture"):
            relationships.append((slug, r["venture"], "belongs_to"))
```
with:
```python
        parent = r.get("project") or r.get("venture")
        if parent:
            relationships.append((slug, parent, "belongs_to"))
```
In the `# Web Apps / Products` loop, replace:
```python
        if r.get("venture"):
            relationships.append((slug, r["venture"], "belongs_to"))
```
with:
```python
        parent = r.get("project") or r.get("venture")
        if parent:
            relationships.append((slug, parent, "belongs_to"))
```

- [ ] **Step 5: Run the test — expect PASS**

Run:
```bash
python3 -m unittest tests.test_index_projects -v
```
Expected: PASS.

- [ ] **Step 6: Re-index the real workspace (no regression)**

Run:
```bash
python3 index.py
```
Expected: `Rebuilt database/data/os.db`, entities count unchanged from before (still includes `acme`, the two brands, the app).

- [ ] **Step 7: Commit**

```bash
git add index.py tests/test_index_projects.py
git commit -m "feat(index): parse standalone ## Projects and project/venture parent alias"
```

---

### Task 3: Read helpers `db.tree()` and `db.channel_posts()`

`tree()` returns projects (ventures + standalone projects) with nested channels/apps and counts — the data the left rail needs. `channel_posts()` returns one channel's posts (the center view splits them into written vs idea on the client by `brief_path`).

**Files:**
- Modify: `dashboard/db.py` (append two functions after `experiments()`, line ~81)
- Test: `tests/test_db_tree.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_db_tree.py`:
```python
import subprocess, sys, tempfile, unittest
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
  name: Acme on TikTok
  venture: acme
  channels: [tiktok]
"""

PLAN = {
    "posts": [
        {"id": "p1", "date": "2026-07-01", "platform": "tiktok", "pillar": "Teaser", "status": "planned"},
        {"id": "p2", "date": "2026-07-02", "platform": "tiktok", "pillar": "Demo", "status": "briefed"},
    ]
}

class TestTree(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        (root / "portfolio").mkdir(parents=True)
        (root / "portfolio" / "relationships.md").write_text(RELATIONSHIPS, encoding="utf-8")
        cdir = root / "brands" / "acme-tok" / "content"
        cdir.mkdir(parents=True)
        bdir = cdir / "briefs"; bdir.mkdir()
        import json
        (cdir / "plan-2026-07-01-to-2026-07-14.json").write_text(json.dumps(PLAN), encoding="utf-8")
        (bdir / "p2.json").write_text(json.dumps({"caption": "hi"}), encoding="utf-8")
        res = subprocess.run([sys.executable, str(REPO / "index.py"), str(root)],
                             capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, res.stderr)
        # point db at this temp os.db
        import importlib
        sys.path.insert(0, str(REPO / "dashboard"))
        import db
        importlib.reload(db)
        db.DB_PATH = root / "database" / "data" / "os.db"
        self.db = db

    def tearDown(self):
        self.tmp.cleanup()

    def test_tree_nests_channel_under_venture_with_counts(self):
        tree = self.db.tree()
        self.assertEqual(len(tree), 1)
        proj = tree[0]
        self.assertEqual(proj["slug"], "acme")
        self.assertEqual(proj["type"], "venture")
        self.assertEqual(len(proj["channels"]), 1)
        ch = proj["channels"][0]
        self.assertEqual(ch["slug"], "acme-tok")
        self.assertEqual(ch["posts"], 2)
        self.assertEqual(ch["written"], 1)  # only p2 has a brief

    def test_channel_posts_returns_both(self):
        posts = self.db.channel_posts("acme-tok")
        self.assertEqual({p["id"] for p in posts}, {"p1", "p2"})
        by_id = {p["id"]: p for p in posts}
        self.assertIsNone(by_id["p1"]["brief_path"])
        self.assertIsNotNone(by_id["p2"]["brief_path"])

if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run it to confirm it fails**

Run:
```bash
python3 -m unittest tests.test_db_tree -v
```
Expected: FAIL — `AttributeError: module 'db' has no attribute 'tree'`.

- [ ] **Step 3: Implement the helpers**

Append to `dashboard/db.py`:
```python
def tree():
    """Projects (ventures + standalone) with nested channels/apps and counts —
    the shape the left rail renders."""
    ents = _rows("SELECT slug, type, name, priority FROM entities")
    rels = _rows("SELECT from_slug, to_slug FROM relationships WHERE kind = 'belongs_to'")
    belongs = {(r["from_slug"], r["to_slug"]) for r in rels}
    posts_n = {r["brand_slug"]: r["n"] for r in
               _rows("SELECT brand_slug, COUNT(*) n FROM posts GROUP BY brand_slug")}
    written_n = {r["brand_slug"]: r["n"] for r in
                 _rows("SELECT brand_slug, COUNT(*) n FROM posts"
                       " WHERE brief_path IS NOT NULL GROUP BY brand_slug")}
    feat_n = {r["app_slug"]: r["n"] for r in
              _rows("SELECT app_slug, COUNT(*) n FROM features GROUP BY app_slug")}
    exp_n = {r["entity_slug"]: r["n"] for r in
             _rows("SELECT entity_slug, COUNT(*) n FROM experiments GROUP BY entity_slug")}

    projects = []
    for e in ents:
        if e["type"] not in ("venture", "project"):
            continue
        slug = e["slug"]
        channels, apps = [], []
        for c in ents:
            if (c["slug"], slug) not in belongs:
                continue
            if c["type"] == "brand":
                channels.append({"slug": c["slug"], "name": c["name"],
                                 "posts": posts_n.get(c["slug"], 0),
                                 "written": written_n.get(c["slug"], 0)})
            elif c["type"] == "web_app":
                apps.append({"slug": c["slug"], "name": c["name"],
                             "features": feat_n.get(c["slug"], 0)})
        projects.append({
            "slug": slug, "name": e["name"], "type": e["type"],
            "priority": e["priority"],
            "channels": sorted(channels, key=lambda x: x["name"]),
            "apps": sorted(apps, key=lambda x: x["name"]),
            "experiments": exp_n.get(slug, 0),
        })
    projects.sort(key=lambda p: (p["priority"] != "primary", p["name"]))
    return projects


def channel_posts(slug):
    """All posts for one channel (brand), newest-undated last."""
    return _rows(
        "SELECT id, brand_slug, date, platform, pillar, status, version, brief_path"
        " FROM posts WHERE brand_slug = ?"
        " ORDER BY (date IS NULL), date, platform",
        (slug,),
    )
```

- [ ] **Step 4: Run the test — expect PASS**

Run:
```bash
python3 -m unittest tests.test_db_tree -v
```
Expected: PASS (both tests).

- [ ] **Step 5: Commit**

```bash
git add dashboard/db.py tests/test_db_tree.py
git commit -m "feat(db): tree() and channel_posts() read helpers for the new shell"
```

---

### Task 4: Endpoints `/api/tree` and `/api/channel/<slug>/posts`

**Files:**
- Modify: `dashboard/server.py:66-84` (inside `do_GET`, the `/api/...` block)

- [ ] **Step 1: Add the routes**

In `dashboard/server.py`, inside `do_GET`, directly after the `if path == "/api/timeline":` line/return (line ~67), add:
```python
            if path == "/api/tree":
                return self._send(200, db.tree())
            if path.startswith("/api/channel/") and path.endswith("/posts"):
                slug = path[len("/api/channel/"):-len("/posts")]
                return self._send(200, db.channel_posts(slug))
```

- [ ] **Step 2: Manually verify both endpoints**

Run (starts the server, probes, stops it):
```bash
python3 dashboard/server.py --no-reindex &
SRV=$!; sleep 1
curl -s http://127.0.0.1:8765/api/tree | python3 -m json.tool | head -30
curl -s http://127.0.0.1:8765/api/channel/demo/posts | python3 -m json.tool | head -20
kill $SRV
```
Expected: `/api/tree` shows `acme` with a `channels` array containing `demo` (posts: 12, written: 1) and `sample`; `/api/channel/demo/posts` lists 12 post objects.

- [ ] **Step 3: Commit**

```bash
git add dashboard/server.py
git commit -m "feat(server): /api/tree and /api/channel/<slug>/posts read endpoints"
```

---

### Task 5: New `app.html` shell wired to the live data

Build the real UI from the **locked mockup** `final-graphite-sky.html` (its `<style>` is the design system — copy it verbatim), replacing the hard-coded rail and cards with JS that renders from `/api/tree` and `/api/channel/<slug>/posts`, reusing the existing post-action and guideline endpoints. The consultant panel renders as a static "coming next" placeholder this phase (wired in Phase 3).

**Files:**
- Modify (full rewrite): `dashboard/app.html`

- [ ] **Step 1: Seed `app.html` from the locked mockup**

Start from the design spec and reproduce its shell in `dashboard/app.html`.

- [ ] **Step 2: Replace everything from `<body>` onward with the live shell**

In `dashboard/app.html`, keep the `<head>`…`</head>` (the `<style>` design system) exactly as copied. Replace the entire `<body>…</body>` with:
```html
<body>
<div class="app">
  <aside class="panel side" id="rail"></aside>
  <section class="panel main" id="main"><div class="scroll"><div style="padding:30px;color:var(--dim)">loading…</div></div></section>
  <aside class="panel chat">
    <div class="ch"><span class="av"></span><div><b>Consultant</b><span class="sm">arriving in a later build</span></div></div>
    <div class="stream"><div class="msg ai"><div class="b">I’ll live here soon — context-aware advice and one-click proposals. For now, use the workspace on the left.</div></div></div>
    <div class="compose"><div class="ibox"><textarea placeholder="Consultant coming soon…" disabled></textarea><button class="send" disabled>↑</button></div></div>
  </aside>
</div>
<div id="toast" style="position:fixed;bottom:18px;left:50%;transform:translateX(-50%);background:rgba(27,31,39,.92);color:#fff;padding:10px 16px;border-radius:14px;opacity:0;transition:opacity .2s;pointer-events:none;z-index:30"></div>

<script>
const $ = s => document.querySelector(s);
const esc = s => (s==null?"":String(s)).replace(/[&<>]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;"}[c]));
function toast(m){const t=$("#toast");t.textContent=m;t.style.opacity=1;setTimeout(()=>t.style.opacity=0,2400);}
async function api(p,o){const r=await fetch(p,o);const j=await r.json().catch(()=>({}));if(!r.ok)throw new Error(j.error||r.status);return j;}

const LABELS={planned:"Planned",approved_slot:"Slot approved",briefed:"Written",approved:"Approved",scheduled:"Scheduled",published:"Published",rejected:"Rejected"};
const ACTIONS={
  planned:[{l:"Approve slot",k:"go",to:"approved_slot"},{l:"Reject",k:"",to:"rejected"}],
  approved_slot:[{l:"Expand to full content",k:"acc",brief:1},{l:"↩ planned",k:"",to:"planned"},{l:"Reject",k:"",to:"rejected"}],
  briefed:[{l:"Approve",k:"go",to:"approved"},{l:"Reject",k:"",to:"rejected"}],
  approved:[{l:"Schedule",k:"go",to:"scheduled"},{l:"Reject",k:"",to:"rejected"}],
  scheduled:[{l:"Publish",k:"go",to:"published"},{l:"Reject",k:"",to:"rejected"}],
  published:[], rejected:[{l:"Reopen",k:"",to:"planned"}],
};

let STATE={view:"timeline", channel:null};

async function boot(){ await renderRail(); selectView("timeline"); }

async function renderRail(){
  const tree=await api("/api/tree");
  const projects=tree.map(p=>`
    <div class="vent">
      <div class="h">${esc(p.name)} <span class="tag">${esc(p.type)}</span></div>
      <div class="sub">
        ${p.channels.map(c=>`<a class="kid" data-channel="${c.slug}">${esc(c.name)} <span class="cnt">${c.posts}</span></a>`).join("")}
        ${p.apps.map(a=>`<a data-app="${a.slug}">${esc(a.name)} <span class="cnt" style="margin-left:auto;color:var(--dim)">${a.features}</span></a>`).join("")}
      </div>
    </div>`).join("");
  $("#rail").innerHTML=`
    <div class="brand"><span class="mark"></span><b>Leo Bot</b></div>
    <div class="navlabel label">Across everything</div>
    <nav class="nav">
      <a data-view="timeline"><span class="ico">◷</span> Timeline</a>
    </nav>
    <div class="navlabel label">Projects</div>
    ${projects}`;
  $("#rail").querySelectorAll("[data-view]").forEach(a=>a.onclick=()=>selectView(a.dataset.view));
  $("#rail").querySelectorAll("[data-channel]").forEach(a=>a.onclick=()=>selectChannel(a.dataset.channel));
  highlight();
}
function highlight(){
  document.querySelectorAll("#rail [data-view]").forEach(a=>a.classList.toggle("active",STATE.view==="timeline"&&a.dataset.view==="timeline"));
  document.querySelectorAll("#rail [data-channel]").forEach(a=>a.classList.toggle("active",STATE.view==="channel"&&a.dataset.channel===STATE.channel));
}

function selectView(v){ STATE.view=v; STATE.channel=null; highlight(); if(v==="timeline") renderTimeline(); }
function selectChannel(slug){ STATE.view="channel"; STATE.channel=slug; highlight(); renderChannel(slug); }

async function renderTimeline(){
  const rows=await api("/api/timeline");
  const body=rows.length? `<table style="width:100%;border-collapse:collapse">
    <thead><tr>${["Date","Kind","Entity","Title","Status"].map(h=>`<th style="text-align:left;padding:8px 10px;border-bottom:1px solid var(--hair);color:var(--dim);font-size:12px">${h}</th>`).join("")}</tr></thead>
    <tbody>${rows.map(r=>`<tr>
      <td style="padding:8px 10px;border-bottom:1px solid var(--hair)">${esc(r.date||"—")}${r.date_end?" → "+esc(r.date_end):""}</td>
      <td style="padding:8px 10px;border-bottom:1px solid var(--hair)"><span class="tag">${esc(r.kind)}</span></td>
      <td style="padding:8px 10px;border-bottom:1px solid var(--hair)">${esc(r.entity_slug||"—")}</td>
      <td style="padding:8px 10px;border-bottom:1px solid var(--hair)">${esc(r.title||"")}</td>
      <td style="padding:8px 10px;border-bottom:1px solid var(--hair);color:var(--dim)">${esc(r.status||"—")}</td>
    </tr>`).join("")}</tbody></table>` : `<div style="padding:30px;color:var(--dim)">Timeline is empty.</div>`;
  $("#main").innerHTML=`<div class="topbar"><div><div class="crumbs">Across everything</div><h1 class="title">Timeline</h1></div></div><div class="scroll">${body}</div>`;
}

async function renderChannel(slug){
  const posts=await api(`/api/channel/${slug}/posts`);
  const written=posts.filter(p=>p.brief_path), ideas=posts.filter(p=>!p.brief_path);
  const seg=`<div class="seg" id="seg"><span data-seg="content" class="on">Content</span><span data-seg="planned">Planned</span><span data-seg="guidelines">Guidelines</span></div>`;
  $("#main").innerHTML=`<div class="topbar"><div><div class="crumbs">Projects · <b>${esc(slug)}</b></div><h1 class="title">${esc(slug)}</h1>
      <div class="titlemeta">${posts.length} posts · ${written.length} written</div></div></div>
    <div class="scroll"><div class="between">${seg}<div class="mix"></div></div><div id="segbody"></div></div>`;
  const bodies={
    content: ()=> cards(written, "Nothing written yet — expand an idea from Planned."),
    planned: ()=> cards(ideas, "No idea slots. Generate a plan from the terminal (Phase 2 adds a button)."),
    guidelines: ()=> guidelinesEditor(slug),
  };
  function show(seg){ $("#seg").querySelectorAll("span").forEach(s=>s.classList.toggle("on",s.dataset.seg===seg)); $("#segbody").innerHTML=""; $("#segbody").appendChild(bodies[seg]()); }
  $("#seg").querySelectorAll("span").forEach(s=>s.onclick=()=>show(s.dataset.seg));
  show("content");

  function cards(list, empty){
    const wrap=document.createElement("div");
    if(!list.length){ wrap.innerHTML=`<div style="padding:24px 4px;color:var(--dim)">${empty}</div>`; return wrap; }
    wrap.className="cards";
    wrap.innerHTML=list.map(p=>{
      const acts=(ACTIONS[p.status]||[]).map(a=>`<button class="mini ${a.k}" data-id="${p.id}" data-brief="${a.brief?1:''}" data-to="${a.to||''}">${a.l}</button>`).join("");
      return `<div class="c ${p.brief_path?'full':''}"><span class="ribbon"></span>
        <div class="top"><span class="tag ${p.platform==='tiktok'?'tt':(p.platform==='instagram'?'ig':'')}">${esc(p.platform||'?')}</span>
          ${p.pillar?`<span class="tag">${esc(p.pillar)}</span>`:''}${p.brief_path?'<span class="tag go">written ✓</span>':''}</div>
        <h4>${esc(p.pillar||p.id)}</h4>
        <div class="ex">${esc(p.date||'—')} · ${esc(LABELS[p.status]||p.status)}${p.version>1?' · v'+p.version:''}</div>
        <div class="foot">${acts}</div></div>`;
    }).join("");
    wrap.querySelectorAll(".foot button").forEach(b=>b.onclick=()=>act(b.dataset));
    return wrap;
  }
  async function act(d){
    try{
      if(d.brief){ toast("Expanding via claude -p… (a few seconds)"); await api(`/api/post/${d.id}/brief`,{method:"POST"}); toast("Written ✓"); }
      else { await api(`/api/post/${d.id}/status`,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({status:d.to})}); toast(`→ ${LABELS[d.to]||d.to}`); }
      renderChannel(slug); renderRail();
    }catch(e){ toast("✗ "+e.message); }
  }
  function guidelinesEditor(slug){
    const wrap=document.createElement("div"); wrap.style.maxWidth="760px";
    wrap.innerHTML=`<p style="color:var(--dim);font-size:12.5px">Injected into every generation for this channel. Use <code>## General</code> + per-platform sections.</p>
      <textarea id="gtext" rows="14" style="width:100%;border:1px solid var(--gline);border-radius:14px;padding:12px;font:12.5px/1.5 ui-monospace,Menlo,monospace;background:rgba(255,255,255,.8)"></textarea>
      <div class="foot" style="margin-top:10px"><button class="mini acc" id="grefine">✨ Refine with AI</button><button class="btn primary" id="gsave">Save</button><span id="gstatus" style="color:var(--dim);font-size:12px;margin-left:6px"></span></div>`;
    api(`/api/brand/${slug}/guidelines`).then(d=>{wrap.querySelector("#gtext").value=d.text||"";}).catch(()=>{});
    wrap.querySelector("#gsave").onclick=async()=>{ try{ await api(`/api/brand/${slug}/guidelines`,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({text:wrap.querySelector("#gtext").value})}); toast("Guidelines saved ✓"); }catch(e){ toast("✗ "+e.message);} };
    wrap.querySelector("#grefine").onclick=async()=>{ const st=wrap.querySelector("#gstatus"); st.textContent="refining…"; try{ const d=await api(`/api/brand/${slug}/guidelines/refine`,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({text:wrap.querySelector("#gtext").value})}); wrap.querySelector("#gtext").value=d.refined; st.textContent="refined — review, then Save"; }catch(e){ st.textContent=""; toast("✗ "+e.message);} };
    return wrap;
  }
}

boot();
</script>
</body>
```

- [ ] **Step 3: Smoke-test the whole shell in the browser**

Run:
```bash
python3 dashboard/server.py
```
Then open http://127.0.0.1:8765 and verify:
- Left rail shows **Projects → Acme (venture)** with **Demo (12)** and **Sample (0)** nested, plus **Timeline** under "Across everything".
- Clicking **Demo** shows the channel view; **Content** tab lists the 1 written post, **Planned** lists the idea slots, **Guidelines** loads the editor.
- An action button (e.g. **Approve slot** on a planned card) updates the card and the rail count without error.
- **Timeline** renders the table.
Stop with Ctrl-C.

- [ ] **Step 4: Commit**

```bash
git add dashboard/app.html
git commit -m "feat(dashboard): project-centric three-panel glass shell (Phase 1)"
```

---

### Task 6: Phase close-out

- [ ] **Step 1: Run the full test suite**

Run:
```bash
python3 -m unittest discover -s tests -v
```
Expected: all tests PASS.

- [ ] **Step 2: Update the running guide**

In `docs/guide.md`, replace the "### The tabs" table (lines ~34-43) with a short description of the new layout: left **Projects** rail (ventures + standalone projects, channels nested), center **workspace** (channel Content/Planned/Guidelines, Timeline), right **Consultant** (arriving in a later phase). Leave the rest of the guide intact.

- [ ] **Step 3: Commit**

```bash
git add docs/guide.md
git commit -m "docs: describe the new project-centric dashboard layout"
```

---

## Self-Review notes (for the executor)

- **Spec coverage (Phase 1 slice):** three-panel glass shell ✓ (Task 5), projects-not-only-ventures ✓ (Tasks 1–2, rail in Task 5), channels nested under project ✓ (`tree()` + rail), channel Content/Planned/Guidelines ✓, Timeline ✓. Deferred to later phases by design: Needs-you/Activities/Roadmap/Experiments views, Add-post/Plan/manual CRUD, the live consultant + propose→apply.
- **No regressions:** existing endpoints (`/api/post/<id>/status`, `/brief`, guidelines) are reused, so the content pipeline keeps working through the new UI.
- **Invariant:** no new write path added this phase; `os.db` still opened read-only; all reads go through `db.py`.
