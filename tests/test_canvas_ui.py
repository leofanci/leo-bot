"""Canvas pane UI: route, pinned tabs, block editors, list grid (static checks plus the
pure helpers of dashboard/canvas-view.js run under node)."""
import json
import re
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APP_JS = ROOT / "dashboard" / "app.js"
APP_HTML = ROOT / "dashboard" / "app.html"
APP_CSS = ROOT / "dashboard" / "app.css"
CANVAS_JS = ROOT / "dashboard" / "canvas-view.js"
SERVER = ROOT / "dashboard" / "server.py"


def _body(src, start, end="\n}\n"):
    i = src.index(start)
    return src[i:src.index(end, i)]


def _canvas_section(js):
    """The app.js part that draws and edits canvases."""
    start = js.index("// ── canvases")
    end = js.index("// ── end canvases")
    return js[start:end]


class TestCanvasRoute(unittest.TestCase):
    def test_route_is_handled(self):
        js = APP_JS.read_text(encoding="utf-8")
        self.assertIn(r"[/^\/canvas\/(cv\d{1,6})$/,", js)
        self.assertIn("async function renderCanvas(cv", js)
        self.assertIn(r"[/^\/canvases$/,", js)   # the owner's full list (More menu)

    def test_invited_person_may_open_a_canvas(self):
        js = APP_JS.read_text(encoding="utf-8")
        line = next(ln for ln in js.splitlines() if ln.startswith("const PERSON_ROUTE"))
        self.assertIn(r"canvas\/cv\d{1,6}", line)

    def test_render_guards_against_a_newer_route(self):
        js = APP_JS.read_text(encoding="utf-8")
        body = _body(js, "async function renderCanvas(")
        self.assertIn("routeGuard()", body)


class TestCanvasNav(unittest.TestCase):
    def test_nav_reads_pins_of_the_open_room(self):
        js = APP_JS.read_text(encoding="utf-8")
        body = _body(js, "function renderCanvasNav(")
        self.assertIn("window.ChatRooms?.current()", body)
        self.assertIn("canvasPins(", body)
        pins = _body(js, "function canvasPins(")
        self.assertIn("/api/canvases?ws=", pins)
        self.assertIn("&room=", pins)

    def test_nav_has_new_canvas_and_all_canvases(self):
        js = APP_JS.read_text(encoding="utf-8")
        body = _body(js, "function renderCanvasNav(")
        self.assertIn("data-cn-new", body)
        self.assertIn('aria-label="New canvas"', body)
        self.assertIn("data-cn-canvases", body)   # owner's More menu: All canvases
        self.assertIn('aria-current="page"', body)

    def test_new_canvas_menu_offers_blank_and_templates(self):
        js = APP_JS.read_text(encoding="utf-8")
        sec = _canvas_section(js)
        self.assertIn('"list-templates"', sec)
        self.assertIn("Blank canvas", js)
        self.assertIn('"create-canvas"', sec)


class TestCanvasAssets(unittest.TestCase):
    def test_canvas_view_is_loaded_and_served(self):
        html = APP_HTML.read_text(encoding="utf-8")
        self.assertIn('<script src="/canvas-view.js"></script>', html)
        self.assertLess(html.index('src="/canvas-view.js"'), html.index('src="/app.js"'))
        server = SERVER.read_text(encoding="utf-8")
        self.assertIn('"canvas-view.js"', server)
        self.assertIn('"/canvas-view.js"', server)

    def test_css_uses_tokens_only(self):
        css = APP_CSS.read_text(encoding="utf-8")
        rules = "\n".join(ln for ln in css.splitlines()
                          if ln.lstrip().startswith((".cv-", ".cn-tab")) and not ln.startswith(".cv-project"))
        self.assertIn(".cv-block{", rules)
        self.assertIn(".cv-grid{", rules)
        self.assertIn(".cn-tab{", rules)
        self.assertNotRegex(rules, r"#[0-9a-fA-F]{3,8}\b")
        self.assertNotRegex(rules, r"rgba?\(")
        # sizes come from tokens; 1px (hairlines, the visually-hidden idiom) is the only raw length
        self.assertEqual(re.findall(r"(?<![\w.-])(?!1px)\d+(?:\.\d+)?px", rules), [])


class TestCanvasWrites(unittest.TestCase):
    def test_stale_write_reloads_and_says_so(self):
        js = APP_JS.read_text(encoding="utf-8")
        sec = _canvas_section(js)
        self.assertIn("Changed by someone else: reloaded", sec)
        self.assertIn("status === 409", sec)

    # core/canvases.py _VERSIONED: the API refuses these without `version`.
    VERSIONED = {"update-canvas", "add-block", "update-block", "move-block", "delete-block",
                 "delete-canvas", "update-list", "add-row", "add-rows", "update-row",
                 "delete-row", "delete-list"}
    # writes to one block, row or checklist item also name the version of that item
    ITEM = {"update-block", "delete-block", "update-row", "delete-row"}

    @staticmethod
    def _calls(sec):
        """(cmd, params text) of every canvasCmd("<cmd>", {...}) call."""
        out = []
        for m in re.finditer(r'canvasCmd\("([a-z-]+)",\s*\{', sec):
            i, depth = m.end() - 1, 0
            for j in range(i, len(sec)):
                depth += {"{": 1, "}": -1}.get(sec[j], 0)
                if depth == 0:
                    out.append((m.group(1), sec[i:j + 1]))
                    break
        return out

    def test_every_write_sends_versions(self):
        sec = _canvas_section(APP_JS.read_text(encoding="utf-8"))
        calls = self._calls(sec)
        seen = set(re.findall(r'canvasCmd\("([a-z-]+)"', sec))
        for cmd in ("update-block", "move-block", "delete-block", "add-block", "update-row",
                    "add-row", "delete-row", "update-list", "update-canvas", "undo", "trust",
                    "delete-canvas", "create-canvas", "list-templates"):
            self.assertIn(cmd, seen, cmd)
        self.assertIn('on ? "pin-canvas" : "unpin-canvas"', sec)
        for cmd, params in calls:
            if cmd in self.VERSIONED:
                self.assertRegex(params, r"\bversion:", f"{cmd}: {params}")
            if cmd in self.ITEM:
                # a checklist op carries item_version on each op instead
                self.assertTrue("item_version:" in params or "items:" in params, f"{cmd}: {params}")

    def test_conflicts_keep_what_the_viewer_typed(self):
        sec = _canvas_section(APP_JS.read_text(encoding="utf-8"))
        self.assertIn("onStale?.()", sec)
        save = sec[sec.index('if (t.matches("[data-cv-savemd]"))'):]
        self.assertIn("st.conflict[bk] = true", save[:1500])
        cell = _body(sec, "async function cvSaveCell(")
        self.assertIn("edit.conflict = true", cell)
        self.assertIn("edit.draft = typed", cell)
        self.assertIn("edit.announce = true", cell)
        # a save on leaving the cell that loses: the cell reopens with the typing, or a toast says what was lost
        self.assertIn("st.editCell = edit;", cell)
        self.assertIn('What you typed: " + typed, true)', cell)
        self.assertLess(cell.index('canvasCmd("update-row"'), cell.rindex("st.editCell = null; st.focusNext"))

    def test_each_list_block_fetches_its_own_view(self):
        body = _body(_canvas_section(APP_JS.read_text(encoding="utf-8")), "async function loadCanvas(")
        self.assertIn("lists[b.id] = await fetches[url]", body)
        self.assertIn("st.limits[b.id]", body)
        self.assertNotIn("lists[b.list]", body)

    def test_page_rights_come_from_the_shared_rule(self):
        body = _body(_canvas_section(APP_JS.read_text(encoding="utf-8")), "function drawCanvas(")
        self.assertIn("CanvasView.rights({ person: isPerson(), me: myCanvasTag(), doc, pinned,", body)
        self.assertIn("const pinBtn = room && mayPin", body)
        self.assertIn("${mayDelete ? `<button", body)

    def test_reloads_are_sequenced(self):
        body = _body(_canvas_section(APP_JS.read_text(encoding="utf-8")), "async function loadCanvas(")
        self.assertIn("const seq = st.loadSeq = (st.loadSeq || 0) + 1;", body)
        # nothing lands on the page before the newest-load check
        self.assertLess(body.index("if (seq !== st.loadSeq) return false;\n  Object.assign(st, { doc"),
                        body.index("Object.assign(st, { doc"))
        self.assertNotIn("st.doc = await", body)
        reload = _body(_canvas_section(APP_JS.read_text(encoding="utf-8")), "async function reloadCanvas(")
        self.assertIn("if (await loadCanvas(st) && stillOn() && CANVAS === st) drawCanvas();", reload)

    def test_background_reloads_wait_for_edits(self):
        js = APP_JS.read_text(encoding="utf-8")
        sec = _canvas_section(js)
        req = _body(sec, "function requestCanvasReload(")
        self.assertIn("if (CanvasView.busy(st, canvasTyping())) { st.reloadPending = true; return; }", req)
        # the deliverable and the room-change reloads are background ones
        hook = js[js.index('saved.some(d => d.kind === "canvas")'):][:200]
        self.assertIn("requestCanvasReload();", hook)
        self.assertNotIn("reloadCanvas()", hook.replace("requestCanvasReload()", ""))
        nav = _body(js, "function renderCanvasNav(")
        self.assertIn("requestCanvasReload();", nav)
        self.assertNotIn("void reloadCanvas()", nav)
        draw = _body(sec, "function drawCanvas(")
        self.assertLess(draw.index("captureCanvasInputs(st);"), draw.index("main.innerHTML = `<div class=\"cv-shell\">"))
        self.assertIn("runPendingReload(st);", draw)
        start = _body(sec, "function startCellEdit(")
        self.assertIn("void cvSaveCell(otd, open);", start)
        cap = _body(sec, "function captureCanvasInputs(")
        for field in ("e.draft = ed.value", "st.renameDraft = rn.value", "st.colForm.draft =",
                      "st.itemDrafts[", "st.pathDrafts[", "st.drafts["):
            self.assertIn(field, cap)

    def test_escape_returns_focus_to_the_opener(self):
        sec = _canvas_section(APP_JS.read_text(encoding="utf-8"))
        self.assertIn("function closeColumnForm(", sec)
        self.assertIn('[data-cv-renameform] *") && e.key === "Escape"', sec)

    def test_tabs_refresh_while_the_pane_shows(self):
        js = APP_JS.read_text(encoding="utf-8")
        sec = _canvas_section(js)
        self.assertIn('const canvasPaneShown = () => !$("#app")?.classList.contains("canvas-hidden");', sec)
        poll = sec[sec.index("setInterval(() => {"):]
        poll = poll[:poll.index("}, 30000);")]
        self.assertIn('document.visibilityState === "visible"', poll)
        self.assertIn("canvasPaneShown()", poll)
        self.assertNotIn("canvas-nav", poll)   # the nav element is never hidden: not a pane check
        # app.js hides the pane with exactly that class
        self.assertIn('appEl.classList.toggle("canvas-hidden", canvasHidden)', js)
        self.assertIn('saved.some(d => d.kind === "canvas")', js)

    def test_pin_cache_expires_instead_of_blanking(self):
        sec = _canvas_section(APP_JS.read_text(encoding="utf-8"))
        self.assertNotIn("dropCanvasPins", sec)
        refresh = _body(sec, "function refreshCanvasPins(")
        self.assertIn("Object.assign(CANVAS_PINS, { at: 0, pending: null });", refresh)
        self.assertNotIn("list: null", refresh)
        pins = _body(sec, "function canvasPins(")
        self.assertIn("const req = ++CANVAS_PINS.req;", pins)
        self.assertIn("if (req !== CANVAS_PINS.req) return;", pins)

    def test_trust_is_owner_only_and_outside_is_tagged(self):
        sec = _canvas_section(APP_JS.read_text(encoding="utf-8"))
        cvjs = CANVAS_JS.read_text(encoding="utf-8")
        self.assertIn("Outside content", cvjs)
        self.assertIn("isPerson()", sec)
        self.assertIn("data-cv-trust", cvjs)
        self.assertIn("h.owner &&", cvjs)   # Trust buttons only when the viewer is the owner


class TestCanvasEscaping(unittest.TestCase):
    """Every user, bot or outside string goes through the escape helper."""

    RAW = re.compile(r"\$\{\s*(?!h\.esc\(|esc\(|h\.md\(|formatBlocks\(|encodeURIComponent\(|CSS\.escape\()"
                     r"[^}]*?\.(md|text|title|label|cells|route|name|by|id|type|bot|key|message|error|options)\b(?!\()")
    # any bare `${a.b}` / `${a?.b.c}`: a field printed as is (only .length is a number)
    BARE = re.compile(r"\$\{\s*[A-Za-z_$][\w$]*(?:\??\.[\w$]+)+\s*\}")

    def _bare(self, src):
        return [m for m in self.BARE.findall(src) if not re.search(r"\.length\s*\}$", m)]

    def test_no_raw_strings_in_canvas_view(self):
        cvjs = CANVAS_JS.read_text(encoding="utf-8")
        self.assertEqual(self.RAW.findall(cvjs), [])
        self.assertEqual(self._bare(cvjs), [])
        self.assertIn("function cellHtml(", cvjs)
        self.assertIn("h.esc(", _body(cvjs, "function cellHtml(", "\n  }\n"))

    def test_no_raw_strings_in_app_canvas_section(self):
        sec = _canvas_section(APP_JS.read_text(encoding="utf-8"))
        self.assertEqual(self.RAW.findall(sec), [])
        self.assertEqual(self._bare(sec), [])

    def test_canvas_helpers_get_the_existing_escape_helper(self):
        sec = _canvas_section(APP_JS.read_text(encoding="utf-8"))
        self.assertIn("esc, md: cvMd", sec)
        cvjs = CANVAS_JS.read_text(encoding="utf-8")
        self.assertNotIn("replace(/[&<>", cvjs)   # no second escape helper


class TestCanvasMarkdown(unittest.TestCase):
    def test_blank_markdown_falls_through_to_canvas_copy(self):
        js = APP_JS.read_text(encoding="utf-8")
        start = js.index("const esc")
        end = js.index("\n}\n", js.index("function formatMdDoc(")) + 3
        source = js[start:end] + _body(_canvas_section(js), "function cvMd(") + "\n}\n"
        script = """
const assert = require('node:assert/strict');
const vm = require('node:vm');
const CV = require(process.argv[1]);
const context = {};
vm.createContext(context);
vm.runInContext(SOURCE, context);
const md = context.cvMd;
for (const blank of ['', '   ', ' \\n\\t ', null, undefined]) assert.equal(md(blank), '');
assert.ok(md('**demo**').includes('<strong>demo</strong>'));
const h = {esc: s => String(s ?? ''), md, icon: () => '', owner: true, canEdit: true, canOpen: true, ws: 'demo'};
const out = [
  CV.blockHtml({id: 'bk1', type: 'text', md: '  '}, {}, h),
  CV.blockHtml({id: 'bk1', type: 'text', md: ''}, {editing: {bk1: true}, preview: {bk1: true}, drafts: {bk1: ' '}}, h),
  CV.blockHtml({id: 'bk1', type: 'text', md: ''}, {editing: {bk1: true}, conflict: {bk1: true}}, h),
].join('');
for (const line of ['Empty.', 'Nothing to preview yet.', 'Their version is empty.']) assert.ok(out.includes(line), line);
assert.ok(!out.includes('memo-empty'));
assert.ok(!out.includes('\u2014'));
""".replace("SOURCE", json.dumps(source))
        result = subprocess.run(["node", "-e", script, str(CANVAS_JS)], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)


class TestCanvasReloads(unittest.TestCase):
    """Exercise actual app loaders with deliberately out-of-order responses."""

    def run_loads(self, scenario):
        source = _canvas_section(APP_JS.read_text(encoding="utf-8"))
        script = """
const vm = require('node:vm');
const assert = require('node:assert/strict');
const pending = [];
const context = {
  URLSearchParams, setInterval: () => {}, _WS: 'demo',
  window: {ChatRooms: {current: () => 'demo/general'}},
  isPerson: () => false,
  fetch: url => new Promise(resolve => pending.push({url, resolve})),
};
vm.createContext(context);
vm.runInContext(SOURCE, context);
const settle = async () => { for (let i = 0; i < 20; i++) await Promise.resolve(); };
const answer = (part, body, status = 200) => {
  const i = pending.findIndex(p => p.url.includes(part));
  assert.notEqual(i, -1, 'missing request: ' + part);
  pending.splice(i, 1)[0].resolve({ok: status === 200, status, json: async () => body});
};
const st = {id: 'cv1', limits: {}, history: false};
const timeout = setTimeout(() => { console.error('loader did not finish'); process.exit(1); }, 3000);
(async () => { SCENARIO })().catch(e => { console.error(e); process.exitCode = 1; })
  .finally(() => clearTimeout(timeout));
""".replace("SOURCE", json.dumps(source)).replace("SCENARIO", scenario)
        result = subprocess.run(["node", "-e", script, str(CANVAS_JS)], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_old_canvas_response_cannot_replace_newer_load(self):
        for old_status in (200, 404):
            with self.subTest(old_status=old_status):
                self.run_loads("""
const older = context.loadCanvas(st);
const old = pending.splice(0, 2);
const newer = context.loadCanvas(st);
answer('/api/canvases?', {pins: ['cv1']});
answer('/api/canvases/cv1?', {id: 'cv1', title: 'New', version: 2, blocks: []});
const newestLanded = await newer;
for (const p of old) p.resolve({ok: OLD_STATUS === 200, status: OLD_STATUS,
  json: async () => p.url.includes('/cv1?')
    ? {id: 'cv1', title: 'Old', version: 1, blocks: []} : {pins: []}});
const olderLanded = await older;
assert.equal(st.doc.title, 'New');
assert.equal(st.pinnedHere, true);
assert.equal(st.error, null);
assert.equal(newestLanded, true);
assert.equal(olderLanded, false);
assert.equal(pending.length, 0);
""".replace("OLD_STATUS", str(old_status)))

    def test_reload_does_not_draw_over_new_route(self):
        self.run_loads("""
context.state = st;
vm.runInContext('CANVAS = state', context);
context.location = {hash: '#/canvas/cv1'};
let currentRoute = 1, draws = 0, finish;
context.routeGuard = () => { const route = currentRoute; return () => route === currentRoute; };
context.drawCanvas = () => { draws++; };
context.loadCanvas = () => new Promise(resolve => { finish = resolve; });
const loading = context.reloadCanvas();
currentRoute++;
context.location.hash = '#/files';
finish(true);
await loading;
assert.equal(draws, 0);
""")

    def test_add_text_does_not_draw_over_new_route(self):
        self.run_loads("""
Object.assign(st, {ws: 'demo', doc: {version: 2, blocks: [{id: 'bk1', v: 2}]},
  editing: {}, drafts: {}});
context.state = st;
vm.runInContext('CANVAS = state', context);
context.location = {hash: '#/canvas/cv1'};
context.CSS = {escape: x => x};
let currentRoute = 1, paints = 0, finish;
context.routeGuard = () => { const route = currentRoute; return () => route === currentRoute; };
context.$ = () => { paints++; throw Error('stale canvas accessed the new route DOM'); };
context.loadCanvas = () => new Promise(resolve => { finish = resolve; });
context.canvasCmd = async () => ({id: 'cv1', block: 'bk1', version: 2});
const root = {contains: () => true};
context.wireCanvas(root, {viewRoutes: []});
const button = {dataset: {cvAdd: 'text'}, matches: s => s === '[data-cv-add]'};
const adding = root.onclick({target: {closest: () => button}});
await settle();
currentRoute++;
context.location.hash = '#/files';
finish(true);
await adding;
assert.equal(paints, 0);
""")

    def test_old_list_response_cannot_replace_newer_load(self):
        self.run_loads("""
const doc = {id: 'cv1', blocks: [{id: 'bk1', type: 'list', list: 'ls1'}]};
const older = context.loadCanvas(st);
answer('/api/canvases?', {pins: []});
answer('/api/canvases/cv1?', {...doc, version: 1});
await settle();
const oldList = pending.shift();
assert.ok(oldList.url.includes('/api/lists/ls1?'));
const newer = context.loadCanvas(st);
answer('/api/canvases?', {pins: ['cv1']});
answer('/api/canvases/cv1?', {...doc, version: 2});
await settle();
answer('/api/lists/ls1?', {id: 'ls1', version: 2, rows: [{id: 'rw1', cells: {name: 'New'}}]});
const newestLanded = await newer;
oldList.resolve({ok: true, status: 200,
  json: async () => ({id: 'ls1', version: 1, rows: []})});
const olderLanded = await older;
assert.equal(st.doc.version, 2);
assert.equal(st.lists.bk1.rows[0].cells.name, 'New');
assert.equal(st.pinnedHere, true);
assert.equal(newestLanded, true);
assert.equal(olderLanded, false);
assert.equal(pending.length, 0);
""")

    def test_old_history_response_cannot_replace_newer_load(self):
        self.run_loads("""
st.history = true;
const older = context.loadCanvas(st);
answer('/api/canvases?', {pins: []});
answer('/api/canvases/cv1?', {id: 'cv1', version: 1, blocks: []});
await settle();
const oldHistory = pending.shift();
assert.ok(oldHistory.url.includes('/history?'));
const newer = context.loadCanvas(st);
answer('/api/canvases?', {pins: []});
answer('/api/canvases/cv1?', {id: 'cv1', version: 2, blocks: []});
await settle();
answer('/history?', {entries: [{id: 'ch000000000002', at: '2026-01-02'}]});
const newestLanded = await newer;
oldHistory.resolve({ok: true, status: 200,
  json: async () => ({entries: [{id: 'ch000000000001', at: '2026-01-01'}]})});
const olderLanded = await older;
assert.equal(st.entries[0].id, 'ch000000000002');
assert.equal(newestLanded, true);
assert.equal(olderLanded, false);
assert.equal(pending.length, 0);
""")


class TestCanvasEditing(unittest.TestCase):
    run_loads = TestCanvasReloads.run_loads

    def test_metadata_forms_keep_their_opening_version(self):
        for kind in ("rename", "column"):
            with self.subTest(kind=kind):
                self.run_loads("""
context.state = st;
vm.runInContext('CANVAS = state', context);
context.drawCanvas = () => {};
context.CSS = {escape: x => x};
st.doc = {version: 1};
st.lists = {bk1: {id: 'ls1', version: 1, columns: [{key: 'name', label: 'Original', type: 'text'}]}};
const kind = KIND;
const root = {contains: () => true};
context.wireCanvas(root, {});
const button = {dataset: {cvCol: 'name'}, closest: () => ({dataset: {bk: 'bk1'}}),
  matches: s => s === (kind === 'rename' ? '[data-cv-rename]' : '[data-cv-col]')};
await root.onclick({target: {closest: () => button}});
st.doc = {version: 2};
st.lists.bk1 = {...st.lists.bk1, version: 2};
const writes = [];
context.canvasCmd = async (cmd, params) => {writes.push(params); return null;};
const form = {dataset: {cvColform: 'bk1', key: 'name'},
  elements: {title: {value: 'Typed name'}, label: {value: 'Typed column'}, type: {value: 'text'}},
  matches: s => s === (kind === 'rename' ? '[data-cv-renameform]' : '[data-cv-colform]')};
await root.onsubmit({target: form, preventDefault: () => {}});
assert.equal(writes[0].version, 1);
""".replace("KIND", json.dumps(kind)))

    def test_cell_keeps_version_from_when_editing_started(self):
        self.run_loads("""
context.state = st;
vm.runInContext('CANVAS = state', context);
context.drawCanvas = () => {};
context.reloadCanvas = async () => {};
context.CSS = {escape: x => x};
context.CanvasView = require(process.argv[1]);
context.$ = () => ({querySelector: () => null});
const list = {id: 'ls1', version: 1, columns: [{key: 'name', type: 'text'}],
  rows: [{id: 'rw1', v: 1, cells: {name: 'Original'}}]};
st.lists = {bk1: list};
const td = {dataset: {cvCell: 'name'}, closest: selector => ({dataset:
  selector === '.cv-block' ? {bk: 'bk1'} : {rw: 'rw1'}})};
context.startCellEdit(td);
st.lists.bk1 = {...list, version: 2, rows: [{id: 'rw1', v: 2, cells: {name: 'Remote'}}]};
const writes = [];
context.canvasCmd = async (cmd, params) => { writes.push(params); return {}; };
await context.cvSaveCell(td, {value: 'My draft'});
assert.equal(writes[0].item_version, 1);
assert.equal(writes[0].version, 1);
""")

    def test_text_keeps_version_from_when_editing_started(self):
        self.run_loads("""
context.state = st;
vm.runInContext('CANVAS = state', context);
context.drawCanvas = () => {};
context.reloadCanvas = async () => {};
context.CSS = {escape: x => x};
Object.assign(st, {doc: {version: 1, blocks: [{id: 'bk1', v: 1, md: 'Original'}]},
  editing: {}, drafts: {}, preview: {}, conflict: {}});
const root = {contains: () => true, querySelector: () => ({value: 'My draft'})};
context.wireCanvas(root, {});
const click = async (attr, key) => root.onclick({target: {closest: () => ({
  dataset: {[key]: 'bk1'}, matches: selector => selector === '[' + attr + ']'
})}});
await click('data-cv-editmd', 'cvEditmd');
st.doc = {version: 2, blocks: [{id: 'bk1', v: 2, md: 'Remote'}]};
const writes = [];
context.canvasCmd = async (cmd, params) => { writes.push(params); return {}; };
await click('data-cv-savemd', 'cvSavemd');
assert.equal(writes[0].item_version, 1);
assert.equal(writes[0].version, 1);
""")

    def test_successful_add_clears_only_submitted_live_input(self):
        for kind, field, draft in (("item", "text", "itemDrafts"), ("path", "path", "pathDrafts")):
            for new_typing in (False, True):
                with self.subTest(kind=kind, new_typing=new_typing):
                    self.run_loads("""
context.state = st;
vm.runInContext('CANVAS = state', context);
context.CSS = {escape: x => x};
context.reloadCanvas = async () => {};
Object.assign(st, {doc: {version: 1, blocks: [{id: 'bk1', v: 1, paths: []}]},
  itemDrafts: {}, pathDrafts: {}});
const field = FIELD, kind = KIND, draft = DRAFT;
const original = {value: 'docs/demo.md'};
const replacement = {value: NEW_TYPING ? 'docs/next.md' : original.value};
const f = {dataset: {['cvAdd' + (kind === 'item' ? 'item' : 'path')]: 'bk1'},
  elements: {[field]: original}, isConnected: true,
  matches: selector => selector === '[data-cv-add' + kind + ']'};
const live = {elements: {[field]: replacement}};
context.$ = () => ({querySelector: () => live});
let finish;
context.canvasCmd = () => new Promise(resolve => { finish = resolve; });
const root = {};
context.wireCanvas(root, {});
const saving = root.onsubmit({target: f, preventDefault: () => {}});
f.isConnected = false;
st[draft].bk1 = replacement.value;
finish({});
await saving;
assert.equal(replacement.value, NEW_TYPING ? 'docs/next.md' : '');
assert.equal(st[draft].bk1, replacement.value);
""".replace("FIELD", json.dumps(field)).replace("KIND", json.dumps(kind))
                                   .replace("DRAFT", json.dumps(draft)).replace("NEW_TYPING", str(new_typing).lower()))


class TestCanvasBrowserPolling(unittest.TestCase):
    run_loads = TestCanvasReloads.run_loads

    def test_only_visible_blocks_poll_and_duplicate_bots_share_request(self):
        self.run_loads("""
Object.assign(st, {ws: 'demo', browsers: {}, browserPollAt: {}});
context.state = st;
vm.runInContext('CANVAS = state', context);
context.location = {hash: '#/canvas/cv1'};
context.document = {visibilityState: 'visible', addEventListener: () => {}, removeEventListener: () => {}};
let paneShown = true;
context.$ = () => ({classList: {contains: () => !paneShown}});
context.CanvasView = require(process.argv[1]);
context.AbortController = AbortController;
let observer, interval, cleared = false;
context.IntersectionObserver = class {
  constructor(cb) { observer = cb; }
  observe() {}
  disconnect() { cleared = true; }
};
context.setInterval = (fn, ms) => { assert.equal(ms, 3000); interval = fn; return 1; };
context.clearInterval = () => {};
const box = () => ({dataset: {cvBrowser: 'profile-a'}, isConnected: true,
  querySelector: () => ({hidden: true, textContent: '', getAttribute: () => null, removeAttribute: () => {}})});
const boxes = [box(), box()];
const calls = [];
context.cvFetch = async url => { calls.push(url); return {live: false, at: 1, url: '', image: null}; };
const cleanup = context.watchCanvasBrowsers({querySelectorAll: () => boxes}, st);
assert.equal(calls.length, 0);
observer(boxes.map(target => ({target, isIntersecting: true})));
await settle();
assert.equal(calls.length, 1);
assert.ok(calls[0].includes('ws=demo'));
assert.ok(calls[0].includes('bot=profile-a'));
st.browserPollAt['profile-a'] = 0;
observer(boxes.map(target => ({target, isIntersecting: false})));
interval(); await settle();
assert.equal(calls.length, 1);
context.document.visibilityState = 'hidden';
observer(boxes.map(target => ({target, isIntersecting: true})));
interval(); await settle();
assert.equal(calls.length, 1);
context.document.visibilityState = 'visible';
paneShown = false;
interval(); await settle();
assert.equal(calls.length, 1);
cleanup();
assert.equal(cleared, true);
""")


class TestCanvasCellSwitch(unittest.TestCase):
    run_loads = TestCanvasReloads.run_loads

    def test_invalid_cell_keeps_its_editor_and_valid_one_saves(self):
        for value, valid in (("abc", False), ("3", True)):
            with self.subTest(value=value):
                self.run_loads("""
context.state = st;
vm.runInContext('CANVAS = state', context);
context.CanvasView = require(process.argv[1]);
context.CSS = {escape: x => x};
const list = {id: 'ls1', version: 4, columns: [{key: 'n', label: 'Count', type: 'number'}],
  rows: [{id: 'rw1', v: 1, cells: {}}, {id: 'rw2', v: 2, cells: {}}]};
st.lists = {bk1: list};   // the real cvBk, cvList and cvRow read these
const td = rw => ({dataset: {cvCell: 'n'}, closest: sel => sel === '[data-rw]' ? {dataset: {rw}}
  : sel === '.cv-block' ? {dataset: {bk: 'bk1'}} : null});
const tdA = td('rw1'), tdB = td('rw2');
let focused = 0;
const input = {value: VALUE, _done: false, focus: () => { focused += 1; }, closest: () => tdA};
context.$ = () => ({querySelector: () => input});
const saves = [], toasts = [];
let draws = 0;
context.cvSaveCell = (cell, el) => { saves.push([cell, el]); };
context.toast = m => toasts.push(m);
context.drawCanvas = () => { draws += 1; };
const editA = {bk: 'bk1', rw: 'rw1', key: 'n', version: 4, item_version: 1};
st.editCell = editA;
context.startCellEdit(tdB);
if (VALID) {
  assert.deepEqual(saves, [[tdA, input]]);
  assert.equal(input._done, true);
  assert.equal(st.editCell.rw, 'rw2');
  assert.equal(st.editCell.key, 'n');
  assert.equal(draws, 1);
  assert.deepEqual(toasts, []);
} else {
  assert.equal(st.editCell, editA);
  assert.deepEqual(saves, []);
  assert.equal(draws, 0);
  assert.deepEqual(toasts, ['Enter a number']);
  assert.equal(focused, 1);
  assert.equal(input._done, false);
}
""".replace("VALUE", json.dumps(value)).replace("VALID", "true" if valid else "false"))


class TestCanvasBrowserStop(unittest.TestCase):
    run_loads = TestCanvasReloads.run_loads

    def test_stop_repaints_the_live_shell_after_a_redraw(self):
        self.run_loads("""
Object.assign(st, {ws: 'demo', browsers: {'profile-a': {live: true}}});
context.state = st;
vm.runInContext('CANVAS = state', context);
const old = {name: 'old'}, fresh = {name: 'fresh'};
let shell = old;
context.$ = selector => selector === '#main .cv-shell' ? shell : null;
const painted = [];
context.paintCanvasBrowsers = (root, s) => painted.push([root.name, !!s.browserStopping['profile-a']]);
let done;
context.cvFetch = () => new Promise(resolve => { done = resolve; });
context.toast = () => {};
const stopping = context.stopCanvasBrowser(old, st, 'profile-a');
assert.deepEqual(painted, [['old', true]]);
shell = fresh;   // a reload redrew the pane while Stop was in flight
done({ok: true});
await stopping;
assert.deepEqual(painted[painted.length - 1], ['fresh', false]);
assert.equal(st.browsers['profile-a'].live, false);
shell = null;    // the pane left: nothing to paint
painted.length = 0;
const again = context.stopCanvasBrowser(old, st, 'profile-a');
done({ok: true});
await again;
assert.deepEqual(painted, [['old', true]]);
""")


class TestCanvasViewHelpers(unittest.TestCase):
    def run_js(self, expr):
        script = ("const CV=require(process.argv[1]);"
                  "const esc=s=>(s==null?'':String(s)).replace(/[&<>\"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',\"'\":'&#39;'}[c]));"
                  "const H={esc,md:s=>'<p>'+esc(s)+'</p>',icon:n=>'',owner:true,canEdit:true,canOpen:true,ws:'demo'};"
                  "console.log(JSON.stringify(" + expr + "))")
        out = subprocess.run(["node", "-e", script, str(CANVAS_JS)], capture_output=True, text=True, check=True)
        return json.loads(out.stdout)

    def test_safe_href_allows_only_web_and_relative(self):
        self.assertEqual(self.run_js('CV.safeHref("https://acme.example/x")'), "https://acme.example/x")
        self.assertEqual(self.run_js('CV.safeHref("http://acme.example")'), "http://acme.example")
        self.assertEqual(self.run_js('CV.safeHref("/files/x")'), "/files/x")
        for bad in ("javascript:alert(1)", " JavaScript:alert(1)", "data:text/html,x", "vbscript:x",
                    "//evil.example", "java\\tscript:x", "mailto:a@b", "\u0001javascript:x",
                    "javascript:\u0000x"):
            self.assertIsNone(self.run_js(f"CV.safeHref({json.dumps(bad)})"), bad)

    def test_entity_tricks_stay_text(self):
        # no colon, so it is a relative link; the escape keeps the entity literal
        got = self.run_js('CV.cellHtml({type:"url"},"javascript&#58;alert(1)",H)')
        self.assertIn('href="javascript&amp;#58;alert(1)"', got)

    def test_file_route(self):
        self.assertEqual(self.run_js('CV.fileRoute("demo","projects/demo/canvases/cv3.json")'), "#/canvas/cv3")
        self.assertEqual(self.run_js('CV.fileRoute("demo","projects/demo/technical.md")'), "#/project/demo/technical")
        self.assertEqual(self.run_js('CV.fileRoute("demo","projects/demo/strategy/intake.md")'), "#/project/demo/validation")
        self.assertEqual(self.run_js('CV.fileRoute("demo","projects/demo/strategy/memos/positioning-v2.json",{positioning:"gtm"})'),
                         "#/project/demo/gtm")
        self.assertEqual(self.run_js('CV.fileRoute("demo","projects/demo/profiles/profile-a/voices/vc1.md")'),
                         "#/profile/profile-a/setup")
        self.assertEqual(self.run_js('CV.fileRoute("demo","projects/demo/notes/x.md")'), "#/files")
        self.assertIsNone(self.run_js('CV.fileRoute("demo","projects/acme/technical.md")'))

    def test_move_params(self):
        blocks = '[{id:"bk1"},{id:"bk2"},{id:"bk3"}]'
        self.assertEqual(self.run_js(f'CV.moveParams({blocks},"bk3","up")'), {"after": "bk1"})
        self.assertEqual(self.run_js(f'CV.moveParams({blocks},"bk2","up")'), {})
        self.assertIsNone(self.run_js(f'CV.moveParams({blocks},"bk1","up")'))
        self.assertEqual(self.run_js(f'CV.moveParams({blocks},"bk1","down")'), {"after": "bk2"})
        self.assertIsNone(self.run_js(f'CV.moveParams({blocks},"bk3","down")'))

    def test_parse_cell(self):
        self.assertEqual(self.run_js('CV.parseCell({type:"number"},"12.5")'), {"value": 12.5})
        self.assertIn("error", self.run_js('CV.parseCell({type:"number"},"twelve")'))
        self.assertEqual(self.run_js('CV.parseCell({type:"text"},"")'), {"value": None})
        self.assertEqual(self.run_js('CV.parseCell({type:"checkbox"},true)'), {"value": True})
        self.assertIn("error", self.run_js('CV.parseCell({type:"date"},"tomorrow")'))
        self.assertEqual(self.run_js('CV.parseCell({type:"select",options:["new","sent"]},"sent")'), {"value": "sent"})

    def test_cells_are_escaped_and_urls_checked(self):
        got = self.run_js('CV.cellHtml({type:"text"},"<img src=x onerror=alert(1)>",H)')
        self.assertNotIn("<img", got)
        got = self.run_js('CV.cellHtml({type:"url"},"javascript:alert(1)",H)')
        self.assertNotIn("href", got)
        got = self.run_js('CV.cellHtml({type:"url"},"https://acme.example/?a=\\"b",H)')
        self.assertIn('href="https://acme.example/?a=&quot;b"', got)
        self.assertIn('rel="noopener noreferrer"', got)

    def test_blocks_escape_and_tag_outside(self):
        blk = '{id:"bk1",type:"text",md:"<script>x</script>",outside:true,v:1,created_by:"person:p1",by:"person:p1"}'
        got = self.run_js(f"CV.blockHtml({blk},{{}},H)")
        self.assertNotIn("<script>", got)
        self.assertIn("Outside content", got)
        self.assertIn("data-cv-trust", got)
        not_owner = self.run_js(f"CV.blockHtml({blk},{{}},Object.assign({{}},H,{{owner:false}}))")
        self.assertIn("Outside content", not_owner)
        self.assertNotIn("data-cv-trust", not_owner)

    def test_checklist_and_list_render_escaped(self):
        cl = '{id:"bk2",type:"checklist",v:1,items:[{id:"i1",text:"<b>x</b>",done:true,v:1,outside:true}]}'
        got = self.run_js(f"CV.blockHtml({cl},{{}},H)")
        self.assertNotIn("<b>x</b>", got)
        self.assertIn('type="checkbox"', got)
        self.assertIn("checked", got)
        lst = ('{id:"bk3",type:"list",list:"ls1",v:1}', '{bk3:{id:"ls1",version:2,total:1,'
               'columns:[{key:"name",label:"<i>Name</i>",type:"text"}],'
               'rows:[{id:"rw1",v:2,cells:{name:"<u>acme</u>"},outside:true}]}}')
        got = self.run_js(f"CV.blockHtml({lst[0]},{{lists:{lst[1]}}},H)")
        self.assertIn("<table", got)
        self.assertIn('<th scope="col"', got)
        self.assertNotIn("<i>Name</i>", got)
        self.assertNotIn("<u>acme</u>", got)
        self.assertIn("Outside content", got)
        # a real cell: no button role, no label hiding its value, the hint is a description
        self.assertNotIn('role="button"', got)
        self.assertIn('tabindex="0" aria-describedby="cv-hint-bk3"', got)
        self.assertIn('id="cv-hint-bk3">Press Enter to edit this cell<', got)
        self.assertNotRegex(got, r'<td[^>]*aria-label')

    def test_list_rows_come_from_the_block_not_the_list(self):
        blk = '{id:"bk4",type:"list",list:"ls1",v:1}'
        other = '{bk3:{id:"ls1",version:2,total:1,columns:[{key:"name",label:"Name",type:"text"}],rows:[{id:"rw1",v:2,cells:{name:"other"}}]}}'
        got = self.run_js(f"CV.blockHtml({blk},{{lists:{other}}},H)")
        self.assertNotIn("other", got)
        self.assertIn("Loading list", got)

    def test_person_view_has_no_edit_controls_or_links(self):
        P = "Object.assign({},H,{owner:false,canEdit:false,canOpen:false})"
        state = ('{lists:{bk3:{id:"ls1",version:2,total:1,columns:[{key:"name",label:"Name",type:"text"},'
                 '{key:"site",label:"Site",type:"url"}],rows:[{id:"rw1",v:2,cells:{name:"acme",site:"https://acme.example"},outside:true}]}}}')
        blocks = ['{id:"bk1",type:"text",md:"x",v:1,outside:true}',
                  '{id:"bk2",type:"checklist",v:1,items:[{id:"i1",text:"x",done:false,v:1}]}',
                  '{id:"bk3",type:"list",list:"ls1",v:1}',
                  '{id:"bk4",type:"files",v:1,paths:["projects/demo/technical.md"]}',
                  '{id:"bk5",type:"view",route:"#/project/demo/gtm",v:1}']
        for b in blocks:
            got = self.run_js(f"CV.blockHtml({b},{state},{P})")
            for attr in ("data-cv-bmenu", "data-cv-editmd", "data-cv-additem", "data-cv-delitem",
                         "data-cv-addrow", "data-cv-addcol", "data-cv-delrow", "data-cv-col=",
                         "data-cv-cellbox", "data-cv-addpath", "data-cv-delpath", "data-cv-route",
                         "data-cv-trust", "tabindex", 'href="#'):
                self.assertNotIn(attr, got, f"{b}: {attr}")
            if '"checklist"' in b:
                self.assertIn("disabled", got)
            if '"list"' in b:
                self.assertIn("Outside content", got)   # the tag still shows

    def test_text_conflict_shows_their_version_and_keeps_the_draft(self):
        blk = '{id:"bk1",type:"text",md:"their <b>words</b>",v:3}'
        st = '{editing:{bk1:true},drafts:{bk1:"my draft"},conflict:{bk1:true},announce:{bk1:true}}'
        got = self.run_js(f"CV.blockHtml({blk},{st},H)")
        self.assertIn('role="alert"', got)
        # later redraws of the same conflict: no second announcement
        again = self.run_js(f'CV.blockHtml({blk},{{editing:{{bk1:true}},drafts:{{bk1:"my draft"}},conflict:{{bk1:true}}}},H)')
        self.assertIn("Replace their version", again)
        self.assertNotIn('role="alert"', again)
        self.assertIn("their &lt;b&gt;words&lt;/b&gt;", got)
        self.assertIn(">my draft</textarea>", got)
        self.assertIn("Replace their version", got)
        self.assertIn("Keep theirs", got)
        plain = self.run_js(f'CV.blockHtml({blk},{{editing:{{bk1:true}},drafts:{{bk1:"d"}}}},H)')
        self.assertNotIn("Replace their version", plain)

    def test_cell_conflict_keeps_the_typed_value_and_shows_theirs(self):
        blk = '{id:"bk3",type:"list",list:"ls1",v:1}'
        st = ('{lists:{bk3:{id:"ls1",version:3,total:1,columns:[{key:"name",label:"Name",type:"text"}],'
              'rows:[{id:"rw1",v:3,cells:{name:"<i>theirs</i>"}}]}},'
              'editCell:{bk:"bk3",rw:"rw1",key:"name",draft:"mine \\"x\\"",conflict:true,announce:true}}')
        got = self.run_js(f"CV.blockHtml({blk},{st},H)")
        self.assertIn('value="mine &quot;x&quot;"', got)
        self.assertIn("Changed meanwhile to: &lt;i&gt;theirs&lt;/i&gt;", got)
        self.assertIn('role="alert"', got)
        quiet = self.run_js(f"CV.blockHtml({blk},{st.replace('announce:true', 'announce:false')},H)")
        self.assertIn("Changed meanwhile to:", quiet)
        self.assertNotIn('role="alert"', quiet)

    def test_typed_values_survive_a_redraw(self):
        lst = '{id:"bk3",type:"list",list:"ls1",v:1}'
        data = '{bk3:{id:"ls1",version:2,total:0,columns:[{key:"name",label:"Name",type:"text"}],rows:[]}}'
        form = self.run_js(f'CV.blockHtml({lst},{{lists:{data},colForm:{{bk:"bk3",key:"name",'
                           f'draft:{{label:"Half <typed>",type:"select",options:"a, b"}}}}}},H)')
        self.assertIn('name="label" value="Half &lt;typed&gt;"', form)
        self.assertIn('<option value="select" selected>', form)
        self.assertIn('name="options" value="a, b"', form)
        cl = self.run_js('CV.blockHtml({id:"bk2",type:"checklist",v:1,items:[]},{itemDrafts:{bk2:"buy <milk>"}},H)')
        self.assertIn('value="buy &lt;milk&gt;"', cl)
        fl = self.run_js('CV.blockHtml({id:"bk4",type:"files",v:1,paths:[]},{pathDrafts:{bk4:"docs/a.md"}},H)')
        self.assertIn('value="docs/a.md"', fl)

    def test_busy_while_mid_edit(self):
        self.assertFalse(self.run_js("CV.busy({},false)"))
        self.assertTrue(self.run_js("CV.busy({},true)"))
        for st in ('{editCell:{bk:"bk3"}}', '{colForm:{bk:"bk3"}}', "{renaming:true}", "{editing:{bk1:true}}"):
            self.assertTrue(self.run_js(f"CV.busy({st},false)"), st)
        self.assertFalse(self.run_js("CV.busy({editing:{bk1:false},editCell:null,colForm:null,renaming:false},false)"))

    def test_rights_match_the_server(self):
        r = lambda args: self.run_js(f"CV.rights({args})")
        owner = r('{person:false,me:"owner",doc:{created_by:"bot:research"},pinned:false,roomKind:"dm",canEdit:true}')
        self.assertEqual(owner, {"mayDelete": True, "mayPin": True})
        # a person: deletes any canvas the owner did not create (core _delete_canvas)
        self.assertFalse(r('{person:true,me:"person:p1",doc:{created_by:"owner"},pinned:true,roomKind:"topic",canEdit:true}')["mayDelete"])
        self.assertTrue(r('{person:true,me:"person:p1",doc:{created_by:"bot:research"},pinned:true,roomKind:"topic",canEdit:true}')["mayDelete"])
        # pins into another room only a canvas they created; unpin where pinned; never a DM room (_pin_check)
        self.assertFalse(r('{person:true,me:"person:p1",doc:{created_by:"owner"},pinned:false,roomKind:"topic",canEdit:true}')["mayPin"])
        self.assertTrue(r('{person:true,me:"person:p1",doc:{created_by:"person:p1"},pinned:false,roomKind:"topic",canEdit:true}')["mayPin"])
        self.assertTrue(r('{person:true,me:"person:p1",doc:{created_by:"owner"},pinned:true,roomKind:"general",canEdit:true}')["mayPin"])
        self.assertFalse(r('{person:true,me:"person:p1",doc:{created_by:"person:p1"},pinned:false,roomKind:"dm",canEdit:true}')["mayPin"])
        # view-only: nothing
        self.assertEqual(r('{person:true,me:"person:p1",doc:{created_by:"person:p1"},pinned:true,roomKind:"topic",canEdit:false}'),
                         {"mayDelete": False, "mayPin": False})

    def test_pin_signature_tracks_ids_and_titles(self):
        self.assertEqual(self.run_js('CV.pinSig([{id:"cv1",title:"a",x:1}])'), self.run_js('CV.pinSig([{id:"cv1",title:"a"}])'))
        self.assertNotEqual(self.run_js('CV.pinSig([{id:"cv1",title:"a"}])'), self.run_js('CV.pinSig([{id:"cv1",title:"b"}])'))
        self.assertEqual(self.run_js("CV.pinSig(null)"), "[]")

    def test_unknown_types_have_a_hook(self):
        got = self.run_js('CV.blockHtml({id:"bk6",type:"browser",bot:"research",v:1},{},H)')
        self.assertIn("research", got)
        self.assertEqual(self.run_js('typeof CV.registerBlock'), "function")
        got = self.run_js('(CV.registerBlock("browser",(b,s,h)=>"<p>live "+h.esc(b.bot)+"</p>"),'
                          'CV.blockHtml({id:"bk6",type:"browser",bot:"research",v:1},{},H))')
        self.assertIn("<p>live research</p>", got)

    def test_browser_block_has_owner_watch_and_stop_controls(self):
        block = '{id:"bk6",type:"browser",bot:"profile-a",v:1}'
        got = self.run_js(f'CV.blockHtml({block},{{}},H)')
        for marker in ('data-cv-browser="profile-a"', 'data-cv-browser-image',
                       'data-cv-browser-status', 'data-cv-browser-title', 'data-cv-stopbot="profile-a"'):
            self.assertIn(marker, got)
        self.assertIn('alt="', got)
        self.assertIn('disabled', got)
        person = self.run_js(f'CV.blockHtml({block},{{}},Object.assign({{}},H,{{owner:false}}))')
        self.assertIn("owner only", person.lower())
        self.assertNotIn("data-cv-browser=", person)
        self.assertNotIn("data-cv-stopbot", person)

    def test_browser_image_source_rejects_non_png_data(self):
        self.assertTrue(self.run_js('CV.browserImage("data:image/png;base64,YWJj")'))
        for image in ('https://acme.example/demo.png', 'data:text/html,x', 'javascript:alert(1)', None):
            self.assertFalse(self.run_js(f'CV.browserImage({json.dumps(image)})'))

    def test_files_block_links_only_its_workspace(self):
        blk = '{id:"bk4",type:"files",v:1,paths:["projects/demo/technical.md","projects/demo/notes/<x>.md"]}'
        got = self.run_js(f"CV.blockHtml({blk},{{}},H)")
        self.assertIn('href="#/project/demo/technical"', got)
        self.assertNotIn("<x>", got)



class TestCanvasLiveCheck(unittest.TestCase):
    """Defects found driving the real app (keyboard, restore, coworker console, browser block)."""

    def run_js(self, expr):
        return TestCanvasViewHelpers.run_js(self, expr)

    def test_all_canvases_lists_deleted_ones_with_restore(self):
        sec = _canvas_section(APP_JS.read_text(encoding="utf-8"))
        body = _body(sec, "async function renderCanvasList(")
        self.assertIn("d.trash", body)
        self.assertIn("data-cv-restore", body)
        self.assertIn('canvasCmd("undo", { id: b.dataset.cvRestore, change: b.dataset.change })', body)
        self.assertIn("esc(c.title)", body)

    def test_rename_save_returns_focus_to_rename(self):
        sec = _canvas_section(APP_JS.read_text(encoding="utf-8"))
        i = sec.index('f.matches("[data-cv-renameform]")')
        branch = sec[i:sec.index("root.onkeydown", i)]
        self.assertIn('st.focusNext = "[data-cv-rename]"', branch)

    def test_new_block_takes_focus(self):
        self.assertEqual(self.run_js('CV.addFocus("checklist","bk2")'), '.cv-block[data-bk="bk2"] [data-cv-additem] input')
        self.assertEqual(self.run_js('CV.addFocus("files","bk5")'), '.cv-block[data-bk="bk5"] [data-cv-addpath] input')
        self.assertEqual(self.run_js('CV.addFocus("list","bk4")'), '[data-cv-addrow="bk4"]')
        self.assertEqual(self.run_js('CV.addFocus("view","bk6")'), '[data-cv-route="bk6"]')
        self.assertEqual(self.run_js('CV.addFocus("browser","bk7")'), '[data-cv-browserbot="bk7"]')
        self.assertIsNone(self.run_js('CV.addFocus("list","bk4\\"]")'))
        sec = _canvas_section(APP_JS.read_text(encoding="utf-8"))
        i = sec.index('if (t.matches("[data-cv-add]")) {')
        branch = sec[i:sec.index('if (t.matches("[data-cv-rename]"))', i)]
        self.assertIn("CanvasView.addFocus(d.cvAdd, out.block)", branch)

    def test_block_menu_names_its_block(self):
        got = self.run_js('CV.blockHtml({id:"bk1",type:"text",md:"x",v:1},{},H)')
        self.assertIn('aria-label="Actions for Text"', got)
        got = self.run_js('CV.blockHtml({id:"bk3",type:"list",list:"ls1",v:1},{lists:{bk3:{id:"ls1",title:"<b>Leads</b>",'
                          'version:1,total:0,columns:[],rows:[]}}},H)')
        self.assertIn('aria-label="Actions for &lt;b&gt;Leads&lt;/b&gt;"', got)

    def test_browser_block_names_the_bot_once(self):
        h = 'Object.assign({},H,{bots:[{id:"research",name:"Research Analyst"}]})'
        got = self.run_js(f'CV.blockHtml({{id:"bk6",type:"browser",bot:"research",v:1}},{{}},{h})')
        self.assertIn('aria-label="Stop Research Analyst browsing"', got)
        self.assertIn('alt="Latest screenshot from Research Analyst"', got)
        self.assertIn("data-cv-browser-time hidden", got)  # the empty note already says it
        paint = _body(APP_JS.read_text(encoding="utf-8"), "function paintCanvasBrowsers(")
        self.assertIn("? \"Last screenshot: \" + captured.toLocaleString() : \"\").hidden = !captured", paint)

    def test_coworker_never_asks_for_hq_or_dm_tabs(self):
        pins = _body(APP_JS.read_text(encoding="utf-8"), "function canvasPins(")
        self.assertIn('if (isPerson() && (room.startsWith("hq/") || roomById(room)?.kind === "dm")) return [];', pins)
        self.assertLess(pins.index("isPerson()"), pins.index("api("))

    def test_escape_in_a_menu_returns_focus_and_arrows_move(self):
        js = APP_JS.read_text(encoding="utf-8")
        keys = _body(js, "function menuKeys(")
        self.assertIn("m._btn?.focus()", keys)
        for k in ('"ArrowDown"', '"ArrowUp"', '"Home"', '"End"'):
            self.assertIn(k, keys)
        self.assertIn('document.addEventListener("keydown", menuKeys);', js)

    def test_page_scales_on_phones(self):
        html = APP_HTML.read_text(encoding="utf-8")
        self.assertIn('<meta name="viewport" content="width=device-width, initial-scale=1">', html)

if __name__ == "__main__":
    unittest.main()
