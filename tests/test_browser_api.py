"""Bot browser over HTTP: approving held sends, Settings, Browser, Team levels, first logins."""
import io
import json
import os
import re
import subprocess
import tempfile
import time
import unittest
from email.message import Message
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import dashboard.server as server
from core import approvals, browser, people
from dashboard import browser_upstream as bu

PORT = 8765
LOCAL = f"127.0.0.1:{PORT}"
ORIGIN = f"http://{LOCAL}"
FAKE = Path(__file__).resolve().parent / "fixtures" / "fake_agent_browser.py"
PROJECTS = [{"slug": "acme", "name": "Acme", "kind": "venture"}]
HOST = "acme" + ".example"
PAGE = "https" + "://" + HOST + "/compose"
OTHER = "https" + "://" + "demo" + ".example" + "/login"
CHAT = "0" * 8 + "-0000-0000-0000-" + "0" * 12
APP_JS = (Path(__file__).resolve().parents[1] / "dashboard" / "app.js").read_text(encoding="utf-8")


def request(method, path, body=None, *, owner=True, member=None, origin=ORIGIN, headers=None):
    h = server.Handler.__new__(server.Handler)
    out = {"headers": {}}
    h.wfile = io.BytesIO()
    h.send_response = lambda code, *a: out.setdefault("code", code)
    h.send_header = lambda k, v: out["headers"].__setitem__(k, v)
    h.end_headers = lambda: None
    h.log_message = lambda *a: None
    raw = b"" if body is None else json.dumps(body).encode()
    msg = Message()
    msg["Host"] = LOCAL
    if raw:
        msg["Content-Type"] = "application/json"
    msg["Content-Length"] = str(len(raw))
    jar = []
    if owner:
        jar.append(f"{server.AUTH_COOKIE}={server.AUTH_TOKEN}")
    if member:
        jar.append(f"{server.MEMBER_COOKIE}={member}")
    if jar:
        msg["Cookie"] = "; ".join(jar)
    if origin:
        msg["Origin"] = origin
    for k, v in (headers or {}).items():
        msg[k] = v
    h.headers, h.rfile, h.path = msg, io.BytesIO(raw), path
    h.client_address = ("127.0.0.1", 50000)
    h.server = SimpleNamespace(server_address=("127.0.0.1", PORT))
    getattr(h, "do_" + method)()
    payload = h.wfile.getvalue()
    try:
        data = json.loads(payload) if payload else None
    except ValueError:
        data = payload
    return out.get("code"), data


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        self.root, self.home = d / "root", d / "home"
        self.home.mkdir()
        (self.root / "chats").mkdir(parents=True)
        (self.root / "projects" / "acme").mkdir(parents=True)
        self.pool = bu.Pool()
        for p in (mock.patch.object(server, "ROOT", self.root),
                  mock.patch.object(server.chat_store, "CHATS_DIR", self.root / "chats"),
                  mock.patch.object(server.db, "db_exists", return_value=True),
                  mock.patch.object(server.db, "tree", return_value=PROJECTS),
                  mock.patch.object(server.browser_upstream, "POOL", self.pool),
                  mock.patch.object(server.browser_upstream, "find_chrome_debug_url", return_value=None),
                  mock.patch.object(server, "DEPLOYMENT", "local"),
                  mock.patch.object(bu, "SOCKET_MAX", 1000),  # temp homes are long
                  mock.patch.dict(os.environ, {"HOME": str(self.home), "LEOBOT_AGENT_BROWSER": str(FAKE)}),
                  mock.patch.dict(bu._TOOLS, clear=True)):
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.pool.close_all)
        # Composio is never asked about a browser send: every call it could take fails the test.
        self.composio = {}
        for name in ("execute", "catalog", "accounts"):
            p = mock.patch.object(server.composio, name, side_effect=AssertionError(name))
            self.composio[name] = p.start()
            self.addCleanup(p.stop)

    def log(self, event=None):
        path = self.home / "fake-log.jsonl"
        rows = [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []
        return [r for r in rows if event is None or r["event"] == event]


class ApproveSends(Base):
    def setUp(self):
        super().setUp()
        server.chat_store.save({"id": CHAT, "messages": []})
        browser.set_bot("acme", "cmo", "write", "bot", self.root)
        items = [{"url": PAGE, "text": "hello one", "site": HOST}, {"url": PAGE + "/2", "text": "hello two", "site": HOST}]
        self.recs = approvals.add_send_plan(self.root, ws="acme", room="acme/general", chat=CHAT, bot="cmo",
                                            person="ceo", items=items)
        self.batch = self.recs[0]["batch"]

    def status(self, rec):
        return approvals.get(rec["id"], self.root)["status"]

    def test_approve_browser_send_never_calls_composio(self):
        # No Composio key at all: a browser send does not need one.
        code, data = request("POST", f"/api/approvals/{self.recs[0]['id']}/approve", {})
        self.assertEqual(code, 200, data)
        self.assertEqual((data["status"], data["result"]), ("approved", server.BROWSER_APPROVED))
        self.assertEqual(self.status(self.recs[0]), "approved")
        self.assertEqual(self.status(self.recs[1]), "pending")
        notes = [m for m in server.chat_store.load(CHAT)["messages"] if server.BROWSER_APPROVED in json.dumps(m)]
        self.assertEqual(len(notes), 1)
        self.assertEqual(request("POST", f"/api/approvals/{self.recs[0]['id']}/approve", {})[0], 409)  # once
        self.assertFalse(any(m.called for m in self.composio.values()))

    def test_batch_approve_marks_all_approved(self):
        code, data = request("POST", f"/api/approvals/batch/{self.batch}/approve", {})
        self.assertEqual(code, 200, data)
        self.assertEqual([r["status"] for r in data["results"]], ["approved", "approved"])
        self.assertEqual([self.status(r) for r in self.recs], ["approved", "approved"])
        self.assertFalse(any(m.called for m in self.composio.values()))

    def test_reject_marks_rejected_single_and_batch(self):
        code, data = request("POST", f"/api/approvals/{self.recs[0]['id']}/reject", {})
        self.assertEqual((code, data["status"]), (200, "rejected"))
        code, data = request("POST", f"/api/approvals/batch/{self.batch}/reject", {})
        self.assertEqual((code, [r["status"] for r in data["results"]]), (200, ["rejected"]))
        self.assertEqual([self.status(r) for r in self.recs], ["rejected", "rejected"])
        self.assertFalse(any(m.called for m in self.composio.values()))

    def test_approve_refused_when_level_dropped_or_edited(self):
        rid = self.recs[0]["id"]
        code, data = request("POST", f"/api/approvals/{rid}/approve", {"arguments": {"url": PAGE, "text": "other"}})
        self.assertEqual(code, 409, data)
        browser.set_bot("acme", "cmo", "read", "bot", self.root)
        self.assertEqual(request("POST", f"/api/approvals/{rid}/approve", {})[0], 409)
        self.assertEqual(self.status(self.recs[0]), "pending")
        self.assertEqual(approvals.get(rid, self.root)["action"]["arguments"]["text"], "hello one")

    def test_approve_needs_origin(self):
        self.assertEqual(request("POST", f"/api/approvals/{self.recs[0]['id']}/approve", {}, origin="")[0], 403)
        self.assertEqual(self.status(self.recs[0]), "pending")


class Settings(Base):
    def test_settings_owner_only(self):
        _, code = people.create_invite(self.root, ws="acme", role="admin", by="ceo")
        _, member = people.join(self.root, code, "Pat")
        writes = (("/api/browser/settings", {"ws": "acme", "bot": "cmo", "level": "act", "mode": "bot"}),
                  ("/api/browser/settings", {"site": HOST, "mode": "my_chrome", "daily": 3}),
                  ("/api/browser/install", {}),
                  ("/api/browser/login", {"ws": "acme", "bot": "cmo", "url": PAGE}))
        with mock.patch.object(server.browser_engine, "install", side_effect=AssertionError("install")):
            self.assertNotEqual(request("GET", "/api/browser/settings?ws=acme", owner=False, member=member)[0], 200)
            self.assertEqual(request("GET", "/api/browser/settings", owner=False)[0], 403)
            for path, body in writes:
                for kw in ({"owner": False, "member": member}, {"owner": False},
                           {"origin": ""}, {"origin": "https://evil.example"}):
                    code, _ = request("POST", path, body, **kw)
                    self.assertIn(code, (403, 404), (path, kw))
        self.assertEqual(browser.bot_setting("acme", "cmo", self.root), {"level": "off", "mode": "bot"})
        self.assertNotIn(HOST, browser.sites(self.root))
        self.assertEqual(self.log("open"), [])

    def test_settings_rejects_bad_level(self):
        for body in ({"ws": "acme", "bot": "cmo", "level": "god", "mode": "bot"},
                     {"ws": "acme", "bot": "cmo", "level": "read", "mode": "tab"},
                     {"ws": "acme", "bot": "nobody", "level": "read", "mode": "bot"},
                     {"ws": "gone", "bot": "cmo", "level": "read", "mode": "bot"}):
            self.assertEqual(request("POST", "/api/browser/settings", body)[0], 400, body)
        self.assertEqual(browser.bot_setting("acme", "cmo", self.root), {"level": "off", "mode": "bot"})

    def test_bot_setting_saves_and_reloads(self):
        code, data = request("POST", "/api/browser/settings", {"ws": "acme", "bot": "cmo", "level": "write", "mode": "bot"})
        self.assertEqual((code, data["bots"]["cmo"]), (200, {"level": "write", "mode": "bot"}))
        code, data = request("GET", "/api/browser/settings?ws=acme")
        self.assertEqual((code, data["bots"]["cmo"], data["bots"]["cos"]),
                         (200, {"level": "write", "mode": "bot"}, {"level": "off", "mode": "bot"}))
        self.assertEqual((data["levels"], data["modes"]), (list(browser.LEVELS), list(browser.MODES)))
        self.assertFalse(data["chrome"])
        self.assertEqual(request("GET", "/api/browser/settings?ws=gone")[0], 400)

    def test_sites_add_change_remove(self):
        code, data = request("POST", "/api/browser/settings", {"site": HOST, "mode": "my_chrome", "daily": "7"})
        self.assertEqual(code, 200, data)
        self.assertIn({"site": HOST, "mode": "my_chrome", "daily": 7}, data["sites"])
        for bad in ({"daily": 0}, {"daily": 501}, {"daily": "lots"}, {"daily": True}, {"mode": "tab"}):
            self.assertEqual(request("POST", "/api/browser/settings", {"site": HOST, "mode": None, **bad})[0], 400, bad)
        data = request("POST", "/api/browser/settings", {"site": HOST, "mode": "", "daily": ""})[1]
        self.assertIn({"site": HOST, "mode": None, "daily": None}, data["sites"])
        data = request("POST", "/api/browser/settings", {"site": HOST, "remove": True})[1]
        self.assertNotIn(HOST, [s["site"] for s in data["sites"]])
        self.assertNotIn(HOST, browser.sites(self.root))

    def test_install_runs_in_the_background(self):
        ready = {"state": "ready", "detail": "/x/agent-browser"}
        with mock.patch.object(server.browser_engine, "install", return_value=ready) as inst:
            code, data = request("POST", "/api/browser/install", {})
            self.assertEqual(code, 202)
            deadline = time.monotonic() + 5
            while server._BROWSER_INSTALL["running"] and time.monotonic() < deadline:
                time.sleep(0.02)
        self.assertEqual(inst.call_count, 1)
        data = request("GET", "/api/browser/settings")[1]
        self.assertEqual((data["engine"]["installing"], data["engine"]["last_install"]), (False, ready))


class Login(Base):
    def setUp(self):
        super().setUp()
        browser.set_bot("acme", "cmo", "write", "bot", self.root)
        # A session daemon lives from its window's `open` until a `close` for it (the fake has no daemon).
        alive = mock.patch.object(bu, "daemon_alive", side_effect=self.alive)
        alive.start()
        self.addCleanup(alive.stop)
        poll = mock.patch.object(bu, "LOGIN_POLL", 0.05)
        poll.start()
        self.addCleanup(poll.stop)

    def alive(self, sess, home=None):
        state = False
        for r in self.log():
            if r["event"] in ("open", "close") and r["env"].get("AGENT_BROWSER_SESSION") == sess:
                state = r["event"] == "open"
        return state

    def session(self):
        return bu.session_name("acme", "cmo", "bot", browser.epochs(self.root)["acme"])

    def login(self, url=PAGE, bot="cmo", **kw):
        return request("POST", "/api/browser/login", {"ws": "acme", "bot": bot, "url": url}, **kw)

    def test_login_refused_for_my_chrome_mode(self):
        browser.set_bot("acme", "cmo", "write", "my_chrome", self.root)
        code, data = self.login()
        self.assertEqual(code, 409, data)
        self.assertIn("your Chrome", data["error"])
        self.assertEqual(self.log("open"), [])
        self.assertEqual(self.pool.logins(), [])

    def test_login_refused_for_a_site_bound_to_my_chrome(self):
        browser.set_site(HOST, "my_chrome", None, self.root)
        code, data = self.login()
        self.assertEqual(code, 409, data)
        self.assertEqual(self.log("open"), [])
        self.assertEqual(self.login(OTHER)[0], 200)  # another site still opens

    def test_login_needs_a_web_address_and_this_machine(self):
        for url in ("file:///etc/passwd", "javascript:alert(1)", "", None, "chrome://settings"):
            self.assertEqual(self.login(url)[0], 400, url)
        self.assertEqual(self.login(headers={"X-Forwarded-For": "203.0.113.5"})[0], 403)
        with mock.patch.object(server, "DEPLOYMENT", "server"):
            self.assertEqual(self.login()[0], 403)
        self.assertEqual(self.log("open"), [])
        self.assertNotIn("acme", browser.epochs(self.root))  # nothing made for a refused login

    def test_login_opens_headed_with_the_bots_own_env(self):
        code, data = self.login()
        self.assertEqual(code, 200, data)
        self.assertEqual(data["logins"], [{"ws": "acme", "bot": "cmo"}])
        opened = self.log("open")
        self.assertEqual(len(opened), 1)
        o, sess = opened[0], self.session()
        self.assertEqual(o["argv"], ["--session", sess, "--namespace", bu.NAMESPACE, "open", PAGE])
        self.assertEqual(o["env"]["AGENT_BROWSER_HEADED"], "true")
        self.assertTrue(o["settings"]["headed"])
        self.assertFalse(o["settings"]["autoConnect"])
        self.assertEqual(o["env"]["AGENT_BROWSER_RESTORE"], sess)
        self.assertEqual(o["env"]["AGENT_BROWSER_CONFIG"], str(self.root / "run" / bu.CONFIG_NAME))
        self.assertEqual(len(o["env"]["AGENT_BROWSER_ENCRYPTION_KEY"]), 64)
        self.assertNotIn("AGENT_BROWSER_CDP", o["env"])
        self.assertEqual(self.login()[0], 409)  # one window per bot

    def test_bot_refused_while_logging_in_and_back_after_done(self):
        browser.epoch("acme", self.root)
        up = self.pool.get("acme", "cmo", "bot", self.root)
        reply, _ = up.forward({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                               "params": {"name": "agent_browser_open", "arguments": {"url": "about:blank"}}}, None)
        self.assertFalse(reply["result"]["isError"])
        self.assertEqual(self.login()[0], 200)
        self.assertTrue(up.closed)  # the bot's own child was closed first
        closes = [r for r in self.log("close") if r["env"]["AGENT_BROWSER_SESSION"] == self.session()]
        self.assertEqual(len(closes), 1)
        busy = self.pool.get("acme", "cmo", "bot", self.root)
        reply, _ = busy.forward({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                                 "params": {"name": "agent_browser_get_url", "arguments": {}}}, None)
        self.assertEqual(reply["result"]["content"][0]["text"], bu.LOGGING_IN)
        self.assertEqual(len(self.log("start")), 1)  # no child started meanwhile
        mine = self.pool.get("acme", "cmo", "my_chrome", self.root)  # another session: not held
        self.assertFalse(mine.closed)
        code, data = request("POST", "/api/browser/login/done", {"ws": "acme", "bot": "cmo"})
        self.assertEqual((code, data["logins"]), (200, []))
        again = self.pool.get("acme", "cmo", "bot", self.root)
        reply, _ = again.forward({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                                  "params": {"name": "agent_browser_get_url", "arguments": {}}}, None)
        self.assertFalse(reply["result"]["isError"])

    def test_login_ends_when_its_daemon_is_gone(self):
        self.assertEqual(self.login()[0], 200)
        bu.engine_close_session(self.session(), "bot", self.root)  # the engine's idle timeout, say
        deadline = time.monotonic() + 5
        while self.pool.logins() and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertEqual(self.pool.logins(), [])

    def test_failed_open_ends_the_login(self):
        (self.home / "fake.json").write_text(json.dumps({"open_fails": True}))
        code, data = self.login()
        self.assertEqual(code, 409, data)
        self.assertIn("did not open", data["error"])
        self.assertEqual(self.pool.logins(), [])

    def test_no_login_for_a_deleted_workspace(self):
        with mock.patch.object(server, "_workspace_exists", return_value=False):
            self.assertEqual(self.login()[0], 404)
        self.assertNotIn("acme", browser.epochs(self.root))
        self.assertEqual(self.log("open"), [])


def _fn(name):
    m = re.search(rf"^(?:async )?function {name}\(.*?\n}}\n", APP_JS, re.S | re.M)
    assert m, f"{name} missing from app.js"
    return m.group(0)


def _run(names, expr):
    script = APP_JS.splitlines()[2] + "\n" + "\n".join(_fn(n) for n in names) + \
        f"\nprocess.stdout.write(JSON.stringify({expr}));"
    return json.loads(subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True).stdout)


class Screens(unittest.TestCase):
    def test_labels(self):
        self.assertEqual(_run(["browserLevelLabel"], '["off","read","write","act",undefined].map(browserLevelLabel)'),
                         ["Off", "Reads only", "Read + write", "Acts on its own", "Off"])
        self.assertEqual(_run(["browserModeLabel"], '["bot","my_chrome"].map(browserModeLabel)'),
                         ["Bot browser", "My Chrome"])
        html = _run(["browserLevelLabel", "browserOptionsHtml"],
                    'browserOptionsHtml(["off","read","write","act"], browserLevelLabel, "write")')
        self.assertIn('<option value="write" selected>Read + write</option>', html)
        self.assertEqual(html.count("selected"), 1)

    def test_site_row_and_chrome_check(self):
        row = _run(["browserSiteRowHtml"], 'browserSiteRowHtml({site:"<b>x.example", mode:"my_chrome", daily:9}, 500)')
        self.assertNotIn("<b>x", row)
        self.assertIn('<option value="my_chrome" selected>My Chrome</option>', row)
        self.assertIn('value="9"', row)
        empty = _run(["browserSiteRowHtml"], 'browserSiteRowHtml({site:"x.example", mode:null, daily:null}, 500)')
        self.assertIn('<option value="" selected>Bot\'s choice</option>', empty)
        off, on = _run(["chromeCheckHtml"], "[chromeCheckHtml(false), chromeCheckHtml(true)]")
        for text in ("chrome://inspect", "Remote debugging", "136", "--remote-debugging-port", "--user-data-dir"):
            self.assertIn(text, off)
        self.assertIn("not open with remote debugging on", off)
        self.assertIn('class="eng-state on"', on)

    def test_browser_send_card_has_no_edit(self):
        web, app = _run(["appCardHtml"], '[appCardHtml({kind:"app_action", action:{toolkit:"browser"}, preview:{title:"Send", fields:[]}}, 1),'
                                         ' appCardHtml({kind:"app_action", action:{toolkit:"gmail"}, preview:{title:"Send", fields:[]}}, 1)]')
        self.assertNotIn('data-app="edit"', web)
        self.assertIn("Approve lets the bot send it on its next run", web)
        self.assertIn('data-app="edit"', app)

    def test_settings_has_a_browser_tab_and_route(self):
        self.assertIn('["browser", "Browser"]', APP_JS)
        self.assertIn("integrations|browser|access", APP_JS)

    def test_no_em_dash_in_browser_copy(self):
        for name in ("browserSiteRowHtml", "chromeCheckHtml", "renderBrowserSettings"):
            self.assertNotIn("—", _fn(name), name)
        for line in (server.BROWSER_APPROVED, bu.LOGGING_IN):
            self.assertNotIn("—", line)


if __name__ == "__main__":
    unittest.main()
