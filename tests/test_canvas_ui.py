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
        rules = "\n".join(ln for ln in css.splitlines() if ln.startswith(".cv-"))
        self.assertIn(".cv-block{", rules)
        self.assertIn(".cv-grid{", rules)
        self.assertNotRegex(rules, r"#[0-9a-fA-F]{3,8}\b")
        self.assertNotRegex(rules, r"rgba?\(")


class TestCanvasWrites(unittest.TestCase):
    def test_stale_write_reloads_and_says_so(self):
        js = APP_JS.read_text(encoding="utf-8")
        sec = _canvas_section(js)
        self.assertIn("Changed by someone else: reloaded", sec)
        self.assertIn("status === 409", sec)

    def test_every_write_sends_versions(self):
        sec = _canvas_section(APP_JS.read_text(encoding="utf-8"))
        self.assertIn("item_version:", sec)
        self.assertIn("version:", sec)
        for cmd in ("update-block", "move-block", "delete-block", "add-block", "update-row",
                    "add-row", "delete-row", "update-list", "update-canvas", "undo", "trust",
                    "pin-canvas", "unpin-canvas", "delete-canvas"):
            self.assertIn(f'"{cmd}"', sec, cmd)

    def test_trust_is_owner_only_and_outside_is_tagged(self):
        sec = _canvas_section(APP_JS.read_text(encoding="utf-8"))
        cvjs = CANVAS_JS.read_text(encoding="utf-8")
        self.assertIn("Outside content", cvjs)
        self.assertIn("isPerson()", sec)
        self.assertIn("data-cv-trust", cvjs)
        self.assertIn("h.owner &&", cvjs)   # Trust buttons only when the viewer is the owner


class TestCanvasEscaping(unittest.TestCase):
    """Every user, bot or outside string goes through the escape helper."""

    RAW = re.compile(r"\$\{\s*(?!h\.esc\(|esc\(|h\.md\(|formatBlocks\()"
                     r"[^}]*?\.(md|text|title|label|cells|route|name|by)\b(?!\()")

    def test_no_raw_strings_in_canvas_view(self):
        cvjs = CANVAS_JS.read_text(encoding="utf-8")
        self.assertEqual(self.RAW.findall(cvjs), [])
        self.assertIn("function cellHtml(", cvjs)
        self.assertIn("h.esc(", _body(cvjs, "function cellHtml(", "\n  }\n"))

    def test_no_raw_strings_in_app_canvas_section(self):
        sec = _canvas_section(APP_JS.read_text(encoding="utf-8"))
        self.assertEqual(self.RAW.findall(sec), [])

    def test_canvas_helpers_get_the_existing_escape_helper(self):
        sec = _canvas_section(APP_JS.read_text(encoding="utf-8"))
        self.assertIn("esc, md: formatBlocks", sec)
        cvjs = CANVAS_JS.read_text(encoding="utf-8")
        self.assertNotIn("replace(/[&<>", cvjs)   # no second escape helper


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
                    "//evil.example", "java\\tscript:x", "mailto:a@b"):
            self.assertIsNone(self.run_js(f"CV.safeHref({json.dumps(bad)})"), bad)

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
        lst = ('{id:"bk3",type:"list",list:"ls1",v:1}', '{ls1:{id:"ls1",version:2,total:1,'
               'columns:[{key:"name",label:"<i>Name</i>",type:"text"}],'
               'rows:[{id:"rw1",v:2,cells:{name:"<u>acme</u>"},outside:true}]}}')
        got = self.run_js(f"CV.blockHtml({lst[0]},{{lists:{lst[1]}}},H)")
        self.assertIn("<table", got)
        self.assertIn('<th scope="col"', got)
        self.assertNotIn("<i>Name</i>", got)
        self.assertNotIn("<u>acme</u>", got)
        self.assertIn("Outside content", got)

    def test_unknown_types_have_a_hook(self):
        got = self.run_js('CV.blockHtml({id:"bk6",type:"browser",bot:"research",v:1},{},H)')
        self.assertIn("research", got)
        self.assertEqual(self.run_js('typeof CV.registerBlock'), "function")
        got = self.run_js('(CV.registerBlock("browser",(b,s,h)=>"<p>live "+h.esc(b.bot)+"</p>"),'
                          'CV.blockHtml({id:"bk6",type:"browser",bot:"research",v:1},{},H))')
        self.assertIn("<p>live research</p>", got)

    def test_files_block_links_only_its_workspace(self):
        blk = '{id:"bk4",type:"files",v:1,paths:["projects/demo/technical.md","projects/demo/notes/<x>.md"]}'
        got = self.run_js(f"CV.blockHtml({blk},{{}},H)")
        self.assertIn('href="#/project/demo/technical"', got)
        self.assertNotIn("<x>", got)


if __name__ == "__main__":
    unittest.main()
