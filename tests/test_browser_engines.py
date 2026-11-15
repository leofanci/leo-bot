"""The browser relay mounted on the engines that mount apps: Claude Code, Codex, the API key engine.

Never ACP, never a runner, never a thread an invited person can see. The token rides in the
child's env, never in argv, and the endpoint answers only a live browser token from this machine.
"""
import io
import json
import os
import signal
import subprocess
import sys
import threading
import time
import tempfile
import unittest
from email.message import Message
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import dashboard.chat_session as cs
import dashboard.server as server
from core import approvals, browser
from dashboard import browser_relay, browser_upstream
from dashboard.engines import acp, api, api_tools, codex, runner
from dashboard.engines.base import EngineRefused, TurnPolicy, TurnTokens

PORT = 8765
TOKEN = "b" * 64
P = "agent_browser_"
CHAT = "11111111-2222-3333-4444-555555555555"


def request(method, path, body=None, *, host=f"127.0.0.1:{PORT}", headers=None, cookie=False, origin=False):
    h = server.Handler.__new__(server.Handler)
    out = {"headers": {}}
    h.wfile = io.BytesIO()
    h.send_response = lambda code, *a: out.setdefault("code", code)
    h.send_header = lambda k, v: out["headers"].__setitem__(k, v)
    h.end_headers = lambda: None
    h.log_message = lambda *a: None
    raw = b"" if body is None else json.dumps(body).encode()
    msg = Message()
    msg["Host"] = host
    if raw:
        msg["Content-Type"] = "application/json"
    msg["Content-Length"] = str(len(raw))
    if cookie:
        msg["Cookie"] = f"{server.AUTH_COOKIE}={server.AUTH_TOKEN}"
    if origin:
        msg["Origin"] = f"http://127.0.0.1:{PORT}"
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
    return out["code"], data, out["headers"]


class FakeUpstream(browser_relay.Upstream):
    """A bot browser that lists three engine tools (one unknown to the relay) and answers ok."""

    def __init__(self, mode="bot"):
        self.mode, self.frames = mode, []
        self.browser_key = self.lock_key = self.typed_key = "bot:fake-%d" % id(self)

    def forward(self, frame, sid):
        self.frames.append(frame)
        if frame.get("method") == "tools/list":
            return {"jsonrpc": "2.0", "id": frame.get("id"), "result": {"tools": [
                {"name": P + "open", "inputSchema": {"type": "object", "properties": {"url": {}}}},
                {"name": P + "read"}, {"name": "agent_browser_install_extension"}]}}, "m-1"
        return {"jsonrpc": "2.0", "id": frame.get("id"),
                "result": {"content": [{"type": "text", "text": "ok"}]}}, "m-1"

    def current_url(self):
        return "about:blank"


def claude_mcp(cmd):
    return json.loads(cmd[cmd.index("--mcp-config") + 1])["mcpServers"] if "--mcp-config" in cmd else {}


def allowed_of(cmd):
    return cmd[cmd.index("--allowedTools") + 1:cmd.index("--disallowedTools")]


class Root(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "chats").mkdir()
        for p in (mock.patch.object(server, "ROOT", self.root),
                  mock.patch.object(server.chat_store, "CHATS_DIR", self.root / "chats"),
                  mock.patch.object(server.db, "db_exists", return_value=True),
                  mock.patch.object(server.db, "tree", return_value=[])):  # never the real os.db
            p.start()
            self.addCleanup(p.stop)


class ClaudeCode(unittest.TestCase):
    def test_claude_cmd_has_browser_server_only_when_on(self):
        sess = cs.ChatSession(repo_dir=".", rail="R")
        off = sess._base_cmd()
        self.assertNotIn("browser", claude_mcp(off))
        self.assertNotIn("mcp__browser", allowed_of(off))
        on = sess._base_cmd(browser_port=PORT)
        cfg = claude_mcp(on)["browser"]
        self.assertEqual(cfg["type"], "http")
        self.assertEqual(cfg["url"], f"http://127.0.0.1:{PORT}/api/internal/browser/mcp")
        self.assertEqual(cfg["headers"], {"Authorization": "Bearer ${LEOBOT_BROWSER_TOKEN}"})
        self.assertIn("mcp__browser", allowed_of(on))
        self.assertIn("--strict-mcp-config", on)
        both = sess._base_cmd(apps_port=PORT, browser_port=PORT)
        self.assertEqual(set(claude_mcp(both)), {"apps", "browser"})
        self.assertTrue({"mcp__apps", "mcp__browser"} <= set(allowed_of(both)))
        # A port alone is not enough: the turn's policy must say browser.
        policy = sess._policy()
        cmd = cs.ClaudeCodeEngine().build_cmd(sess, policy, None, browser_port=PORT)
        self.assertNotIn("browser", claude_mcp(cmd))
        # Pages read and web with nothing watching the turn: web goes off, like apps.
        cmd = sess._base_cmd(with_web=True, browser_port=PORT)
        self.assertNotIn("WebFetch", cmd[cmd.index("--tools") + 1:cmd.index("--allowedTools")])

    def test_shared_thread_never_gets_browser(self):
        sess = cs.ChatSession(repo_dir=".", rail="R")
        sess.shared = True
        self.assertFalse(sess._policy(browser=True).browser)
        cmd = cs.ClaudeCodeEngine().build_cmd(sess, TurnPolicy(shared=True, browser=True), None,
                                              os_port=PORT, browser_port=PORT)
        self.assertEqual(set(claude_mcp(cmd)), {"os"})
        self.assertNotIn("mcp__browser", allowed_of(cmd))
        env = cs.ClaudeCodeEngine().turn_env(TurnPolicy(shared=True, browser=True),
                                             TurnTokens(browser_token=TOKEN, browser_port=PORT))
        self.assertNotIn("LEOBOT_BROWSER_TOKEN", env)
        # The api engine: no browser tools in a shared thread either.
        box = api_tools.ToolBox(TurnPolicy(shared=True, browser=True), Path("."),
                                TurnTokens(browser_token=TOKEN), "api",
                                {"browser_list": lambda t: [{"name": P + "open"}]})
        self.assertNotIn(P + "open", [s["name"] for s in box.specs])

    def test_token_not_in_argv(self):
        sess = cs.ChatSession(repo_dir=".", rail="R")
        seen = {}

        class Proc:
            def __init__(self, cmd, **kw):
                seen["cmd"], seen["env"] = cmd, kw["env"]
                self.stdin, self.stdout, self.stderr = io.StringIO(), io.StringIO(), io.StringIO()
                self.returncode = 0

            def wait(self, *a, **k):
                return 0

            def poll(self):
                return 0
        with mock.patch.dict(cs.os.environ, {"LEOBOT_BROWSER_TOKEN": "leak"}), \
                mock.patch.object(cs.subprocess, "Popen", Proc):
            list(sess.ask("hi"))
            self.assertNotIn("LEOBOT_BROWSER_TOKEN", seen["env"])  # never the server's own copy
            list(sess.ask("hi", browser_token=TOKEN, browser_port=PORT))
        self.assertFalse([a for a in seen["cmd"] if TOKEN in a])
        self.assertEqual(seen["env"]["LEOBOT_BROWSER_TOKEN"], TOKEN)
        self.assertIn("browser", claude_mcp(seen["cmd"]))
        # Codex: the same, the token in env and named by its variable in argv.
        policy = TurnPolicy(browser=True)
        tokens = TurnTokens(browser_token=TOKEN, browser_port=PORT)
        argv = codex.CodexEngine().build_cmd(cs.ChatSession(repo_dir=".", rail="R"), policy, None, tokens)
        self.assertFalse([a for a in argv if TOKEN in a])
        self.assertEqual(codex.CodexEngine().turn_env(policy, tokens)["LEOBOT_BROWSER_TOKEN"], TOKEN)
        # osctl, started by a bot's run, never inherits it.
        with mock.patch.dict(cs.os.environ, {"LEOBOT_BROWSER_TOKEN": "leak"}):
            self.assertNotIn("LEOBOT_BROWSER_TOKEN", server.os_relay.env_for({"ws": "hq"}))


class Codex(unittest.TestCase):
    def test_codex_cmd_has_browser_server(self):
        eng, sess = codex.CodexEngine(), cs.ChatSession(repo_dir=".", rail="R")
        tokens = TurnTokens(browser_token=TOKEN, browser_port=PORT)
        argv = " ".join(eng.build_cmd(sess, TurnPolicy(browser=True), None, tokens))
        self.assertIn(f'mcp_servers.browser.url="http://127.0.0.1:{PORT}/api/internal/browser/mcp"', argv)
        self.assertIn('mcp_servers.browser.bearer_token_env_var="LEOBOT_BROWSER_TOKEN"', argv)
        self.assertIn("mcp_servers.browser.tool_timeout_sec=600", argv)
        off = " ".join(eng.build_cmd(sess, TurnPolicy(), None, tokens))
        self.assertNotIn("mcp_servers.browser", off)
        self.assertNotIn("LEOBOT_BROWSER_TOKEN", eng.turn_env(TurnPolicy(), tokens))
        self.assertNotIn("mcp_servers.browser", " ".join(eng.build_cmd(sess, TurnPolicy(browser=True), None,
                                                                       TurnTokens())))


class Engines(unittest.TestCase):
    def test_acp_and_runner_never_get_browser(self):
        for eng in (cs.ClaudeCodeEngine, codex.CodexEngine, api.ApiEngine):
            self.assertIn("browser", eng.supports, eng)
        for eng in (acp.AcpEngine, runner.RunnerEngine):
            self.assertNotIn("browser", eng.supports, eng)
        with self.assertRaises(EngineRefused):
            runner.RunnerEngine("owner").check(TurnPolicy(browser=True))


class ApiEngine(Root):
    def test_api_engine_lists_browser_tools(self):
        calls = []
        backends = {"browser_list": lambda t: [{"name": P + "open", "description": "Open a page",
                                                "inputSchema": {"type": "object"}}, {"name": "osctl"}],
                    "browser_call": lambda t, n, a: calls.append((t, n, a)) or ("opened", False)}
        tokens = TurnTokens(browser_token=TOKEN)
        box = api_tools.ToolBox(TurnPolicy(browser=True), self.root, tokens, "api", backends)
        names = [s["name"] for s in box.specs]
        self.assertIn(P + "open", names)
        self.assertEqual(names.count("osctl"), 1)  # a browser tool never shadows Leo Bot's own
        self.assertEqual(box.run(P + "open", {"url": "about:blank"}), ("opened", False))
        self.assertEqual(calls, [(TOKEN, P + "open", {"url": "about:blank"})])
        self.assertFalse(box.web_open())  # nothing watching the turn: no web next to pages read
        off = api_tools.ToolBox(TurnPolicy(), self.root, tokens, "api", backends)
        self.assertNotIn(P + "open", [s["name"] for s in off.specs])
        self.assertEqual(off.run(P + "open", {})[1], True)
        # Through the server's own backend: the relay's list, from this bot's pooled upstream.
        browser.set_bot("hq", "cos", "write", "bot", self.root)
        up = FakeUpstream()
        tok = server.BROWSER_TOKENS.mint({"ws": "hq", "bot": "cos", "person": "ceo"})
        self.addCleanup(server.BROWSER_TOKENS.revoke, tok)
        with mock.patch.object(server.browser_upstream.POOL, "get", return_value=up) as get:
            listed = [t["name"] for t in server._browser_list(tok)]
            self.assertEqual(server._browser_call(tok, P + "read", {}), ("ok", False))
        self.assertEqual(listed, [P + "open", P + "read", browser_relay.PLAN_TOOL])
        get.assert_called_with("hq", "cos", "bot", self.root)
        server.BROWSER_TOKENS.revoke(tok)
        with self.assertRaises(ValueError):
            server._browser_rpc(tok, "tools/list", {})

    def test_browser_that_will_not_list_leaves_the_turn_without_it(self):
        def broken(token):
            raise RuntimeError("engine gone")
        box = api_tools.ToolBox(TurnPolicy(browser=True), self.root, TurnTokens(browser_token=TOKEN), "api",
                                {"browser_list": broken})
        self.assertEqual([s["name"] for s in box.specs if s["name"].startswith(P)], [])
        self.assertIn("osctl", [s["name"] for s in box.specs])

    def test_api_session_initialized_once_even_without_a_session_id(self):
        browser.set_bot("hq", "cos", "write", "bot", self.root)
        up = FakeUpstream()
        tok = server.BROWSER_TOKENS.mint({"ws": "hq", "bot": "cos", "person": "ceo"})
        self.addCleanup(server.BROWSER_TOKENS.revoke, tok)
        self.addCleanup(server._API_BROWSER_SESSIONS.pop, tok, None)
        seen = []
        real = server.browser_relay.handle

        def handle(frame, ctx, sid, upstream, root):
            seen.append(frame.get("method"))
            reply, _ = real(frame, ctx, sid, upstream, root)
            return reply, None  # an upstream that names no MCP session
        with mock.patch.object(server.browser_upstream.POOL, "get", return_value=up), \
                mock.patch.object(server.browser_relay, "handle", side_effect=handle):
            server._browser_list(tok)
            server._browser_call(tok, P + "read", {})
        self.assertEqual(seen.count("initialize"), 1)


class Endpoint(Root):
    PATH = "/api/internal/browser/mcp"
    FRAME = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}

    def setUp(self):
        super().setUp()
        browser.set_bot("hq", "cos", "read", "bot", self.root)
        self.token = server.BROWSER_TOKENS.mint({"ws": "hq", "bot": "cos", "chat": "c1", "room": "hq/general",
                                                 "person": "ceo"})
        self.addCleanup(server.BROWSER_TOKENS.revoke, self.token)
        self.up = FakeUpstream()
        get = mock.patch.object(server.browser_upstream.POOL, "get", return_value=self.up)
        self.get = get.start()
        self.addCleanup(get.stop)

    def post(self, token=None, **kw):
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        return request("POST", self.PATH, self.FRAME, headers=headers, **kw)

    def test_endpoint_refuses_without_token(self):
        self.assertEqual(self.post()[0], 403)
        self.assertEqual(self.post(cookie=True)[0], 403)  # the dashboard cookie is not a turn token
        self.assertEqual(self.post("nope")[0], 403)
        apps_tok = server.apps_relay.TOKENS.mint({"ws": "hq", "bot": "cos", "person": "ceo"})
        self.addCleanup(server.apps_relay.TOKENS.revoke, apps_tok)
        self.assertEqual(self.post(apps_tok)[0], 403)  # separate stores
        self.assertEqual(self.post(self.token, host="evil.example")[0], 403)
        self.assertEqual(request("GET", self.PATH)[0], 405)
        self.assertEqual(self.up.frames, [])  # nothing reached the browser
        code, data, hdrs = self.post(self.token)
        self.assertEqual(code, 200)
        self.assertEqual([t["name"] for t in data["result"]["tools"]], [P + "open", P + "read"])  # read: no plan tool
        self.assertEqual(hdrs.get("mcp-session-id"), "m-1")
        self.get.assert_called_with("hq", "cos", "bot", self.root)
        server.BROWSER_TOKENS.revoke(self.token)
        self.assertEqual(self.post(self.token)[0], 403)

    def test_upstream_asked_on_every_frame_with_the_current_mode(self):
        self.post(self.token)
        browser.set_bot("hq", "cos", "read", "my_chrome", self.root)
        self.post(self.token)
        self.assertEqual([c.args for c in self.get.call_args_list],
                         [("hq", "cos", "bot", self.root), ("hq", "cos", "my_chrome", self.root)])

    def test_a_broken_upstream_answers_an_error(self):
        self.up.forward = mock.Mock(side_effect=RuntimeError("boom"))
        with mock.patch.object(server.traceback, "print_exc"):
            code, data, _ = self.post(self.token)
        self.assertEqual(code, 200)
        self.assertEqual(data["error"]["code"], -32000)


class Turn(Root):
    """A chat turn: the token minted per bot run, only when the level is on and the thread is private."""

    def run_turn(self, shared=False, plan=False, fail=False):
        seen, root = [], self.root

        class S:
            _started = False
            _turn_count = 0
            _last_skill = None
            _skill_explicit = False
            session_id = CHAT
            apps_used = False

            def __init__(self, *a, **k):
                pass

            def is_fresh(self):
                return not self._started

            def note_skill(self, *a, **k):
                pass

            def note_bot(self, *a):
                pass

            def close(self):
                pass

            def ask(self, prompt, **kw):
                tok = kw.get("browser_token")
                seen.append({"prompt": prompt, "ctx": server.BROWSER_TOKENS.lookup(tok) if tok else None, **kw})
                if fail:
                    raise RuntimeError("the engine broke")
                if tok and plan:  # what the relay does when the bot plans a send
                    approvals.add_send_plan(root, ws="hq", room=None, chat=CHAT, bot="cos", person="ceo",
                                            items=[{"url": "https" + "://acme.example/inbox", "text": "Hello",
                                                    "site": "acme.example"}])
                yield ("delta", "ok")
                yield ("done", {})
        h = server.Handler.__new__(server.Handler)
        h.wfile = io.BytesIO()
        h.send_response = h.send_header = h.end_headers = lambda *a, **k: None
        h.log_message = lambda *a: None
        h.server = SimpleNamespace(server_address=("127.0.0.1", PORT))
        with mock.patch.object(server, "get_chat_session", return_value=S()), \
                mock.patch.object(server, "_shared_room", return_value=shared), \
                mock.patch.object(server.chat_session, "ChatSession", S):
            h._handle_ask({"messages": [{"role": "user", "content": "check the page"}]})
        self.out = h.wfile.getvalue().decode()
        return seen

    def test_token_minted_when_on_and_revoked_after(self):
        self.assertNotIn("browser_token", self.run_turn()[0])  # level off: no token, no server
        self.assertNotIn("## Browser", self.run_turn()[0]["prompt"])
        browser.set_bot("hq", "cos", "write", "bot", self.root)
        kw = self.run_turn()[0]
        self.assertEqual(len(kw["browser_token"]), 64)
        self.assertEqual(kw["browser_port"], PORT)
        self.assertEqual((kw["ctx"]["ws"], kw["ctx"]["bot"], kw["ctx"]["report"]), ("hq", "cos", False))
        self.assertIsNotNone(kw["ctx"]["taint"])  # pages read mark the turn as outside content
        self.assertIsNone(server.BROWSER_TOKENS.lookup(kw["browser_token"]))
        self.assertIn("## Browser", kw["prompt"])
        self.assertIn("leobot_plan_sends", kw["prompt"])
        self.assertIn("any click or key press there is a send", kw["prompt"])
        note = kw["prompt"].split("## Browser")[1].split("\n## ")[0]
        self.assertNotIn(chr(0x2014), note)  # no em dash in what the bot reads

    def test_token_revoked_when_the_run_fails(self):
        browser.set_bot("hq", "cos", "act", "bot", self.root)
        with mock.patch.object(server.traceback, "print_exc"):
            kw = self.run_turn(fail=True)[0]
        self.assertEqual(len(kw["browser_token"]), 64)
        self.assertIsNone(server.BROWSER_TOKENS.lookup(kw["browser_token"]))

    def test_held_send_plan_streamed_to_thread(self):
        browser.set_bot("hq", "cos", "write", "bot", self.root)
        self.run_turn(plan=True)
        events = [json.loads(line[6:]) for line in self.out.splitlines() if line.startswith("data: {")]
        held = [e["app_actions"] for e in events if "app_actions" in e]
        self.assertEqual(len(held), 1)
        self.assertEqual(held[0][0]["action"]["slug"], approvals.BROWSER_SEND)

    def test_shared_thread_never_gets_a_token(self):
        browser.set_bot("hq", "cos", "act", "bot", self.root)
        kw = self.run_turn(shared=True)[0]
        self.assertNotIn("browser_token", kw)
        self.assertNotIn("## Browser", kw["prompt"])

    def test_note_names_level_mode_and_report_jobs(self):
        note = server._browser_note({"level": "act", "mode": "my_chrome"}, "report")
        self.assertIn("Acts on its own", note.split("Your level:")[1])
        self.assertIn("owner's Chrome", note)
        self.assertIn("only reports", note)
        self.assertIn("daily limit", note)
        self.assertNotIn("only reports", server._browser_note({"level": "read", "mode": "bot"}))


if __name__ == "__main__":
    unittest.main()


class Up:
    """A pooled child that records its kill and its engine close (slow ones overlap in parallel)."""

    def __init__(self, ws, bot, slow=0.0, log=None):
        self.ws, self.bot, self.closed, self.mode, self.root = ws, bot, False, "bot", None
        self.slow, self.log, self.killed = slow, log if log is not None else [], False
        self._done = threading.Event()
        self._closed_line = None

    def kill(self, line=None):
        self.killed, self._closed_line = True, line

    def _engine_close(self):
        time.sleep(self.slow)
        self.closed = True
        self.log.append(self.bot)


class WorkspaceDelete(Root):
    def test_close_where_closes_only_that_workspace(self):
        pool = browser_upstream.Pool()
        a, b, c = Up("acme", "b1"), Up("acme", "b2"), Up("acme-2", "b1")
        pool._items.update({("acme", "b1"): a, ("acme", "b2"): b, ("acme-2", "b1"): c})
        self.assertEqual(sorted(pool.close_where("acme")), ["b1", "b2"])
        self.assertTrue(a.closed and b.closed and a.killed and b.killed and a._done.is_set())
        self.assertFalse(c.closed or c.killed)
        self.assertEqual(list(pool._items), [("acme-2", "b1")])

    def test_close_where_closes_in_parallel_within_one_bound(self):
        pool = browser_upstream.Pool()
        ups = [Up("acme", f"b{i}", slow=0.3) for i in range(4)]
        pool._items.update({("acme", u.bot): u for u in ups})
        started = time.monotonic()
        pool.close_where("acme", bound=5)
        self.assertLess(time.monotonic() - started, 1.0)  # four 0.3 s closes overlap
        self.assertTrue(all(u.closed for u in ups))
        hung = Up("demo", "b1", slow=3)
        pool._items[("demo", "b1")] = hung
        started = time.monotonic()
        pool.close_where("demo", bound=0.2)
        self.assertLess(time.monotonic() - started, 1.5)  # the bound holds
        self.assertTrue(hung._done.is_set())

    def test_no_browser_starts_between_close_and_forget(self):
        pool = browser_upstream.Pool()
        with mock.patch.object(browser_upstream.BrowserUpstream, "_engine_close"):
            pool.get("acme", "b1", "bot", self.root)
            pool.close_where("acme")
            mid = pool.get("acme", "b1", "bot", self.root)
            other = pool.get("acme-2", "b1", "bot", self.root)
        self.assertTrue(mid.closed)
        with mock.patch.object(browser_upstream.BrowserUpstream, "_start") as start:
            reply, _ = mid.forward({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                                    "params": {"name": P + "open", "arguments": {}}}, None)
        start.assert_not_called()
        self.assertIn(browser_upstream.WS_CLOSING, json.dumps(reply))
        self.assertNotIn(("acme", "b1"), pool._items)
        self.assertFalse(other.closed)  # another workspace is not held
        pool.reopen("acme")
        after = pool.get("acme", "b1", "bot", self.root)
        self.assertFalse(after.closed)
        self.assertIs(pool.get("acme", "b1", "bot", self.root), after)

    def test_recreated_slug_gets_a_new_child(self):
        pool = browser_upstream.Pool()
        with mock.patch.object(browser_upstream.BrowserUpstream, "_engine_close"):
            old = pool.get("acme", "b1", "bot", self.root)
            self.assertIs(pool.get("acme", "b1", "bot", self.root), old)
            pool.close_where("acme")
            pool.reopen("acme")
            new = pool.get("acme", "b1", "bot", self.root)
        self.assertIsNot(new, old)
        self.assertTrue(old.closed)
        self.assertFalse(new.closed)

    def test_state_files_of_that_workspace_removed_others_kept(self):
        home = self.root / "home"
        folder = browser_upstream.state_dir(home)
        folder.mkdir(parents=True)
        long_ws = "acme-" + "x" * 30  # a hashed session name: computed, never guessed
        mine = [n for ws in ("acme",) for n in browser_upstream.state_files(ws, "b1")]
        hashed = browser_upstream.state_files(long_ws, "b1")
        theirs = browser_upstream.state_files("acme-2", "b1") + browser_upstream.state_files("demo", "b1")
        custom = ["acme--side-bot-acme--side-bot.json.enc", "leobot-acme--side-bot--chrome.json"]
        theirs += ["acme-2--side-bot-acme-2--side-bot.json.enc", "x-acme--b1.json.enc"]
        for name in mine + hashed + theirs + custom + ["acme--b1-extra.json.enc"]:
            (folder / name).write_text("x")
        removed = browser_upstream.forget_state("acme", {"b1", "b2"}, home=home)
        self.assertEqual(sorted(p.name for p in removed), sorted(mine + custom))  # a bot no list names too
        browser_upstream.forget_state(long_ws, {"b1"}, home=home)
        left = sorted(p.name for p in folder.iterdir())
        self.assertEqual(left, sorted(theirs + ["acme--b1-extra.json.enc"]))
        self.assertTrue(any(n.startswith("x") for n in hashed))

    def test_home_unset_uses_the_account_home(self):
        account = str(self.root / "acct")
        os.makedirs(account)
        env = {k: v for k, v in os.environ.items() if k != "HOME"}
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(browser_upstream.pwd, "getpwuid",
                                  return_value=SimpleNamespace(pw_dir=account)):
            self.assertEqual(browser_upstream.state_dir(),
                             Path(os.path.realpath(account)) / ".agent-browser" / "namespaces" / "leobot"
                             / "state" / "sessions")
            up = browser_upstream.BrowserUpstream("acme", "b1", "bot", self.root)
            self.assertEqual(up._env("0" * 64, "cfg")["HOME"], os.path.realpath(account))
        with mock.patch.dict(os.environ, {"HOME": account}):
            self.assertEqual(up._env("0" * 64, "cfg")["HOME"], account)  # a set HOME passes as is

    def delete(self, settled):
        order = []
        with mock.patch.object(server.fileops, "delete_project",
                               side_effect=lambda slug: order.append("files") or {"slug": "acme"}), \
                mock.patch.object(server.browser_upstream.POOL, "forget_workspace",
                                  side_effect=lambda ws, ids, root: order.append("browsers") or settled) as fw:
            code, data, _ = request("POST", "/api/project/acme/delete", {}, cookie=True, origin=True)
        self.assertEqual(code, 200)
        self.assertEqual(order, ["files", "browsers"])
        return fw, data

    def test_delete_closes_browsers_and_forgets_their_state(self):
        browser.set_bot("acme", "growth", "write", "bot", self.root)
        server.integrations.set_grant("acme", "outreach", {"gmail": "read"}, self.root)
        team = self.root / "projects" / "acme" / "team"
        team.mkdir(parents=True)
        (team / "state.json").write_text(json.dumps({"active": {"side-bot": True}}))
        fw, data = self.delete(True)
        ws, ids, root = fw.call_args.args
        self.assertEqual((ws, root), ("acme", self.root))
        # Shipped bots, its browser setting, its app grant and its team state (a non-shipped id).
        self.assertTrue({"growth", "outreach", "side-bot", "cos"} <= ids)
        self.assertNotIn("browser", data)
        self.assertEqual(browser.bot_setting("acme", "growth", self.root)["level"], "off")

    def test_delete_says_when_the_browser_is_still_closing(self):
        _, data = self.delete(False)
        self.assertEqual(data["browser"], browser_upstream.BROWSER_CLOSING)


class Daemons(Root):
    """Pool.forget_workspace with the daemon checks faked: what may and may not happen in order."""

    def setUp(self):
        super().setUp()
        self.home = self.root / "home"
        self.folder = browser_upstream.state_dir(self.home)
        self.folder.mkdir(parents=True)
        self.pool = browser_upstream.Pool()
        self.addCleanup(self.pool._halt.set)
        self.log = []
        real = browser_upstream.forget_state

        def forget(ws, ids, home=None):
            self.log.append("forget")
            return real(ws, ids, home)
        p = mock.patch.object(browser_upstream, "forget_state", side_effect=forget)
        p.start()
        self.addCleanup(p.stop)
        reopen = self.pool.reopen
        self.pool.reopen = lambda ws: (self.log.append("reopen"), reopen(ws))[1]

    def saved(self):
        return self.folder / browser_upstream.state_files("acme", "b1")[0]

    def test_daemon_gone_now_forgets_twice_then_reopens(self):
        def settle(ws, ids, root, home=None):
            self.log.append("settle")
            self.saved().write_text("old logins")  # the daemon saved state as it went
            return True
        with mock.patch.object(browser_upstream, "settle_sessions", side_effect=settle):
            self.assertTrue(self.pool.forget_workspace("acme", {"b1"}, self.root, home=self.home))
        self.assertEqual(self.log, ["forget", "settle", "forget", "reopen"])
        self.assertFalse(self.saved().exists())
        self.assertNotIn("acme", self.pool._deleting)

    def test_hanging_close_keeps_the_slug_refused_until_the_daemon_is_gone(self):
        gone = threading.Event()

        def settle(ws, ids, root, home=None):
            self.log.append("settle")
            if gone.is_set():
                self.saved().write_text("old logins")  # written back as the daemon finally exits
                return True
            return False
        with mock.patch.object(browser_upstream, "settle_sessions", side_effect=settle), \
                mock.patch.object(browser_upstream.BrowserUpstream, "_engine_close"):
            self.assertFalse(self.pool.forget_workspace("acme", {"b1"}, self.root, home=self.home, poll=0.05))
            time.sleep(0.2)
            self.assertTrue(self.pool.get("acme", "b1", "bot", self.root).closed)  # still refused
            self.assertNotIn("reopen", self.log)
            gone.set()
            deadline = time.monotonic() + 5
            while "reopen" not in self.log and time.monotonic() < deadline:
                time.sleep(0.02)
            fresh = self.pool.get("acme", "b1", "bot", self.root)
        self.assertEqual(self.log[-3:], ["settle", "forget", "reopen"])  # forget again, before reopen
        self.assertEqual(self.log.count("reopen"), 1)
        self.assertFalse(fresh.closed)
        self.assertFalse(self.saved().exists())  # the recreated slug never sees the old logins

    def test_kill_line_is_set_before_the_child_lock_is_released(self):
        up = browser_upstream.BrowserUpstream("acme", "b1", "bot", self.root)
        seen = []

        class Lock:
            def __enter__(self_):
                return self_

            def __exit__(self_, *a):
                seen.append(up._closed_line)
        up._child_lock = Lock()
        up.kill(browser_upstream.WS_CLOSING)
        self.assertEqual(seen, [browser_upstream.WS_CLOSING])


FAKE_DAEMON = r"""
import os, signal, subprocess, sys, time
signal.signal(signal.SIGTERM, signal.SIG_IGN)  # a daemon that will not close: SIGKILL path
chrome = subprocess.Popen([sys.executable, "-c",
    "import subprocess, sys, time; subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); time.sleep(60)"],
    start_new_session=True)
print(chrome.pid, flush=True)
time.sleep(60)
"""


class KillPath(unittest.TestCase):
    """kill_daemon on real processes: a fake daemon (its own group, the engine's env words) that
    ignores SIGTERM and a fake Chrome it started in a group of its own, with a helper inside."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.run = browser_upstream.run_dir(self.home)
        self.run.mkdir(parents=True)

    def spawn(self, session, words=True):
        env = {**os.environ, **({"AGENT_BROWSER_DAEMON": "1", "AGENT_BROWSER_SESSION": session,
                                 "AGENT_BROWSER_NAMESPACE": browser_upstream.NAMESPACE} if words else {})}
        proc = subprocess.Popen([sys.executable, "-c", FAKE_DAEMON], env=env, stdout=subprocess.PIPE,
                                text=True, start_new_session=True)
        chrome = int(proc.stdout.readline())
        self.addCleanup(self.reap, proc, chrome)
        (self.run / f"{session}.pid").write_text(f"{proc.pid}\n")
        return proc, chrome

    @staticmethod
    def reap(proc, chrome):
        for g in (proc.pid, chrome):
            try:
                os.killpg(g, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        proc.wait(5)

    def group_alive(self, pgid):
        try:
            os.killpg(pgid, 0)
            return True
        except ProcessLookupError:
            return False

    def test_kill_path_stops_daemon_and_its_chrome(self):
        proc, chrome = self.spawn("acme--b1")
        time.sleep(0.3)
        self.assertEqual(browser_upstream.daemon_pid("acme--b1", self.home), proc.pid)
        self.assertTrue(browser_upstream.daemon_alive("acme--b1", self.home))
        self.assertTrue(browser_upstream.kill_daemon("acme--b1", self.home, wait=1))
        proc.wait(5)
        self.assertFalse(self.group_alive(chrome))  # Chrome and its helper: no orphan left
        self.assertFalse(browser_upstream.daemon_alive("acme--b1", self.home))

    def test_a_pid_that_is_not_that_sessions_daemon_is_never_killed(self):
        other, _ = self.spawn("acme--b1", words=False)   # no engine words in its env
        self.assertIsNone(browser_upstream.daemon_pid("acme--b1", self.home))
        theirs, _ = self.spawn("acme-2--b1")
        (self.run / "acme--b1.pid").write_text(f"{theirs.pid}\n")  # another session's daemon
        self.assertIsNone(browser_upstream.daemon_pid("acme--b1", self.home))
        browser_upstream.kill_daemon("acme--b1", self.home, wait=0.2)
        self.assertIsNone(other.poll())
        self.assertIsNone(theirs.poll())

    def test_socket_that_accepts_is_alive(self):
        short = Path(tempfile.mkdtemp(prefix="lb5", dir="/tmp"))  # a Unix socket path must be short
        self.addCleanup(lambda: __import__("shutil").rmtree(short, ignore_errors=True))
        with mock.patch.object(browser_upstream, "run_dir", return_value=short):
            sock = __import__("socket").socket(__import__("socket").AF_UNIX)
            sock.bind(str(short / "acme--b1.sock"))
            sock.listen(1)
            self.assertTrue(browser_upstream.daemon_alive("acme--b1"))
            sock.close()  # a stale socket left by a killed daemon refuses
            self.assertFalse(browser_upstream.daemon_alive("acme--b1"))

    def test_settle_closes_then_kills_and_lists_plain_sessions(self):
        (self.run / "acme--side-bot.sock").write_text("")
        (self.run / "acme-2--b1.pid").write_text("1")
        names = browser_upstream.sessions_of("acme", {"b1"}, self.home)
        self.assertIn("acme--side-bot", names)
        self.assertNotIn("acme-2--b1", names)
        self.assertEqual(names["acme--b1--chrome"], "my_chrome")
        live = {"acme--b1"}
        calls = []
        with mock.patch.object(browser_upstream, "daemon_alive", side_effect=lambda s, h=None: s in live), \
                mock.patch.object(browser_upstream, "engine_close_session",
                                  side_effect=lambda s, m, r: calls.append(("close", s))), \
                mock.patch.object(browser_upstream, "kill_daemon",
                                  side_effect=lambda s, h=None: calls.append(("kill", s)) or live.discard(s) or True):
            self.assertTrue(browser_upstream.settle_sessions("acme", {"b1"}, self.home, self.home))
        self.assertEqual(calls, [("close", "acme--b1"), ("kill", "acme--b1")])
