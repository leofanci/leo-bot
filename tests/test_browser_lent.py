"""Lend my Chrome: a server bot uses the owner's Chrome through the owner's runner."""
import contextlib
import io
import os
import stat
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from core import runners
from dashboard import browser_lent, browser_relay
from dashboard import browser_upstream as bu
from dashboard import runner as runner_client
from tests import test_server_people as sp

P = "agent_browser_"
EPOCH = "cccccc"
LEND = {"turns": False, "lend": True}


def call(rid, name, **args):
    return {"jsonrpc": "2.0", "id": rid, "method": "tools/call", "params": {"name": name, "arguments": args}}


def text(reply):
    if "error" in reply:
        return reply["error"]["message"]
    return reply["result"]["content"][0]["text"]


def ok(rid, line):
    return {"jsonrpc": "2.0", "id": rid, "result": {"content": [{"type": "text", "text": line}], "isError": False}}


class FakeRunner(threading.Thread):
    """One runner: polls the hub as `rid` for `owner` and answers each browser job with answer(job)."""

    def __init__(self, hub, rid, owner, answer, caps=LEND, take_only=False):
        super().__init__(daemon=True)
        self.hub, self.rid, self.owner, self.answer, self.caps = hub, rid, owner, answer, caps
        self.take_only = take_only
        self.jobs, self.halt = [], threading.Event()

    def run(self):
        while not self.halt.is_set():
            job = self.hub.next(self.rid, self.owner, wait=0.1, caps=self.caps)
            if job:
                self.jobs.append(job)
                if self.take_only:  # takes the job, then goes silent
                    return
                self.hub.reply(self.rid, job["turn"], self.answer(job))

    def stop(self):
        self.halt.set()
        self.join(2)


def echo(tag):
    def answer(job):
        if job["op"] == "url":
            return {"url": "https://acme.example/" + tag}
        return ok(job["frame"].get("id"), f"from {tag}")
    return answer


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.hub = runners.Hub()
        self.runners = []

    def runner(self, rid, owner, answer=None, **kw):
        r = FakeRunner(self.hub, rid, owner, answer or echo(rid), **kw)
        r.start()
        self.runners.append(r)
        self.addCleanup(r.stop)
        deadline = time.time() + 2
        while not self.hub.reachable(owner) and time.time() < deadline:
            time.sleep(0.01)
        return r

    def lent(self, owner="owner", shared=False):
        up = browser_lent.RunnerChromeUpstream("demo", "research", self.root, owner, EPOCH,
                                               shared=shared, hub=self.hub)
        up.POLL = 0.05
        return up


class Lent(Base):
    def test_frames_go_to_owner_runner_only(self):
        a = self.runner("rn-a", "profile-a")
        b = self.runner("rn-b", "profile-b")
        up_b = self.lent("profile-b")
        reply, _ = up_b.forward(call(7, P + "get_title"), None)
        self.assertEqual((reply["id"], text(reply)), (7, "from rn-b"))
        self.assertEqual(up_b.current_url(), "https://acme.example/rn-b")
        self.assertEqual(a.jobs, [])
        self.assertEqual([j["frame"]["params"]["name"] for j in b.jobs if j["op"] == "call"], [P + "get_title"])
        self.assertTrue(all(j["runner"] == "rn-b" for j in b.jobs))
        # Only person A's runner is on: person B's frame is never handed to it.
        b.stop()
        hub = runners.Hub()
        a2 = FakeRunner(hub, "rn-a", "profile-a", echo("rn-a"))
        a2.start()
        self.addCleanup(a2.stop)
        up = browser_lent.RunnerChromeUpstream("demo", "research", self.root, "profile-b", EPOCH, hub=hub)
        reply, _ = up.forward(call(8, P + "get_title"), None)
        self.assertEqual(text(reply), browser_lent.RUNNER_OFF)
        self.assertEqual(a2.jobs, [])

    def test_reply_only_from_the_runner_holding_the_job(self):
        turn = self.hub.submit("profile-b", {"kind": "browser", "op": "url"})
        self.assertIsNone(self.hub.next("rn-a", "profile-a", wait=0, caps=LEND))
        job = self.hub.next("rn-b", "profile-b", wait=0, caps=LEND)
        self.assertEqual(job["turn"], turn.id)
        self.assertFalse(self.hub.reply("rn-a", turn.id, {"url": "x"}))
        self.assertTrue(self.hub.reply("rn-b", turn.id, {"url": "x"}))
        self.assertFalse(self.hub.reply("rn-b", turn.id, {"url": "again"}))  # one reply per job

    def test_runner_offline_says_so_no_fallback(self):
        up = self.lent()
        with mock.patch.object(bu, "BrowserUpstream", side_effect=AssertionError("no fallback")):
            reply, _ = up.forward(call(3, P + "open", url="https://acme.example/"), None)
            self.assertEqual(text(reply), browser_lent.RUNNER_OFF)
            self.assertTrue(reply["result"]["isError"])
            self.assertIsNone(up.current_url())
            listed, _ = up.forward({"jsonrpc": "2.0", "id": 4, "method": "tools/list"}, None)
            self.assertEqual(text(listed), browser_lent.RUNNER_OFF)
        # A runner that takes the call and then goes silent: offline, again no fallback.
        with mock.patch.object(runners, "ONLINE_SECONDS", 0.3):
            self.runner("rn-x", "owner", take_only=True)
            reply, _ = up.forward(call(5, P + "get_title"), None)
        self.assertEqual(text(reply), browser_lent.RUNNER_OFF)

    def test_pool_lends_my_chrome_on_a_server_only(self):
        pool = bu.Pool()
        ctx = {"ws": "demo", "bot": "research", "runner_owner": "owner", "shared": False}
        with mock.patch.object(bu.browser, "epochs", return_value={"demo": EPOCH}), \
                mock.patch.dict(os.environ, {"LEOBOT_DEPLOYMENT": "server"}):
            up = pool.get("demo", "research", "my_chrome", self.root, ctx=ctx)
            self.assertIsInstance(up, browser_lent.RunnerChromeUpstream)
            self.assertIs(pool.get("demo", "research", "my_chrome", self.root, ctx=ctx), up)  # kept: gate state
            self.assertEqual(pool._items, {})
            self.assertIsInstance(pool.get("demo", "research", "bot", self.root, ctx=ctx), bu.BrowserUpstream)
            pool.kill_where("demo", EPOCH)
            self.assertTrue(up.closed)
            reply, _ = up.forward(call(1, P + "get_title"), None)
            self.assertEqual(text(reply), bu.WS_DELETED)
        with mock.patch.object(bu.browser, "epochs", return_value={"demo": EPOCH}), \
                mock.patch.dict(os.environ, {"LEOBOT_DEPLOYMENT": "mac", "MYOS_DEPLOYMENT": ""}):
            self.assertIsInstance(bu.Pool().get("demo", "research", "my_chrome", self.root, ctx=ctx), bu.BrowserUpstream)

    def test_runner_reply_size_capped(self):
        self.runner("rn-o", "owner", answer=lambda j: ok(j["frame"].get("id"), "x" * 5000))
        up = self.lent()
        with mock.patch.object(runners, "MAX_TURN_CHARS", 1000):
            reply, _ = up.forward(call(9, P + "snapshot"), None)
        self.assertEqual(text(reply), browser_lent.TOO_LONG)
        self.assertTrue(reply["result"]["isError"])

    def test_shared_thread_refused(self):
        r = self.runner("rn-o", "owner")
        pool = bu.Pool()
        ctx = {"ws": "demo", "bot": "research", "runner_owner": "owner", "shared": True}
        with mock.patch.object(bu.browser, "epochs", return_value={"demo": EPOCH}), \
                mock.patch.dict(os.environ, {"LEOBOT_DEPLOYMENT": "server"}):
            up = pool.get("demo", "research", "my_chrome", self.root, ctx=ctx)
            reply, _ = up.forward(call(2, P + "get_title"), None)
            self.assertEqual(text(reply), browser_lent.SHARED)
            self.assertIsNone(up.current_url())
            # No owner on the turn at all: refused the same way, never guessed.
            up = pool.get("demo", "research", "my_chrome", self.root, ctx={"ws": "demo", "bot": "research"})
            self.assertEqual(text(up.forward(call(3, P + "get_title"), None)[0]), browser_lent.SHARED)
        self.assertEqual(r.jobs, [])

    def test_lend_off_says_so(self):
        # Runner online but not lending: the server says so and sends no job.
        r = self.runner("rn-o", "owner", caps={"turns": True, "lend": False})
        up = self.lent()
        self.assertEqual(text(up.forward(call(1, P + "get_title"), None)[0]), browser_lent.LEND_OFF)
        self.assertEqual(r.jobs, [])
        r.stop()
        # Lend turned off on the runner after its last poll: the runner refuses, the bot hears it.
        hub = runners.Hub()
        settings = self.root / "runner-settings.json"
        lender = runner_client.Lender(self.root / "lend", settings)
        r2 = FakeRunner(hub, "rn-o", "owner", lender.answer)
        r2.start()
        self.addCleanup(r2.stop)
        up = browser_lent.RunnerChromeUpstream("demo", "research", self.root, "owner", EPOCH, hub=hub)
        reply, _ = up.forward(call(2, P + "get_title"), None)
        self.assertEqual(text(reply), browser_lent.LEND_OFF)

    def test_tab_reset_sets_typed_aside_on_the_server(self):
        self.runner("rn-o", "owner", answer=lambda j: bu._call_error(j["frame"], bu.TAB_RESET))
        up = self.lent()
        browser_relay._save_typed(up.typed_key, {"typed": "hello", "aside": ""}, self.root)
        reply, _ = up.forward(call(1, P + "get_title"), None)
        self.assertEqual(text(reply), bu.TAB_RESET)
        self.assertEqual(browser_relay.typed_state(up.typed_key, self.root), {"typed": "", "aside": "hello", "by": {}})

    def test_tools_list_kept_for_when_the_runner_is_off(self):
        tools = [{"name": P + "get_title", "inputSchema": {"type": "object", "properties": {}}}]
        r = self.runner("rn-o", "owner", answer=lambda j: {"jsonrpc": "2.0", "id": 1, "result": {"tools": tools}})
        up = self.lent()
        listed, _ = up.forward({"jsonrpc": "2.0", "id": 4, "method": "tools/list"}, None)
        self.assertEqual(listed["result"]["tools"], tools)
        r.stop()
        fresh = browser_lent.RunnerChromeUpstream("demo", "research", self.root, "owner", EPOCH, hub=runners.Hub())
        listed, _ = fresh.forward({"jsonrpc": "2.0", "id": 5, "method": "tools/list"}, None)
        self.assertEqual((listed["id"], listed["result"]["tools"]), (5, tools))

    def test_tools_list_does_not_wait_a_full_lease(self):
        tools = [{"name": P + "get_title", "inputSchema": {"type": "object", "properties": {}}}]
        browser_lent._save_tools(self.root, "owner", tools)
        self.hub.next("rn-gone", "owner", wait=0, caps=LEND)  # seen just now, then never polls again
        up = self.lent()
        with mock.patch.object(browser_lent, "LIST_LEASE", 0.2):
            started = time.time()
            listed, _ = up.forward({"jsonrpc": "2.0", "id": 6, "method": "tools/list"}, None)
        self.assertLess(time.time() - started, 2)
        self.assertEqual(listed["result"]["tools"], tools)

    def test_a_new_runner_sets_typed_aside(self):
        a = self.runner("rn-1", "owner")
        up = self.lent()
        up.forward(call(1, P + "get_title"), None)
        browser_relay._save_typed(up.typed_key, {"typed": "hi", "aside": ""}, self.root)
        a.stop()
        with mock.patch.object(self.hub, "lenders", return_value=["rn-2"]):
            self.runner("rn-2", "owner")
            up.forward(call(2, P + "get_title"), None)
        self.assertEqual(browser_relay.typed_state(up.typed_key, self.root)["aside"], "hi")


class LenderClose(unittest.TestCase):
    def test_close_all_closes_each_tab(self):
        closed = []

        class Tab(FakeLocal):
            def close(self):
                closed.append(self)

        with tempfile.TemporaryDirectory() as tmp:
            settings = Path(tmp) / "s.json"
            runner_client.set_lend(settings, True)
            lender = runner_client.Lender(Path(tmp) / "lend", settings, make=lambda *a: Tab())
            job = {"kind": "browser", "turn": "b" * 32, "op": "url", "ws": "demo", "epoch": EPOCH}
            lender.answer({**job, "bot": "profile-a"})
            lender.answer({**job, "bot": "profile-b"})
            lender.close_all()
        self.assertEqual(len(closed), 2)
        self.assertEqual(lender._ups, {})


class RunnerMain(unittest.TestCase):
    """runner_client.main: lent tabs close when the poll loop ends, however it ends."""

    def run_main(self, argv_extra=(), engine_ok=True):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        token = root / "token"
        token.write_text("myr_x")
        token.chmod(0o600)
        settings = root / "settings.json"
        runner_client.set_lend(settings, True)
        made, handlers = [], {}

        class FakeLender:
            def __init__(self, *a, **k):
                self.closed = 0
                made.append(self)

            def close_all(self):
                self.closed += 1

            def sweep(self):
                pass

        class Engine:
            def __init__(self, *a):
                pass

            def status(self):
                return {"ok": engine_ok, "detail": "ok"}

        def post(client, path, body, timeout=30):
            raise SystemExit(0)  # what the SIGTERM handler raises in the poll

        out = io.StringIO()
        with mock.patch.object(runner_client, "Lender", FakeLender), \
                mock.patch.object(runner_client.Client, "post", post), \
                mock.patch.object(runner_client, "ClaudeCodeEngine", Engine), \
                mock.patch.object(runner_client, "sweep_stale_tmpdirs"), \
                mock.patch.object(runner_client, "private_tmpdir", return_value=str(root / "work")), \
                mock.patch.object(runner_client.atexit, "register"), \
                mock.patch.object(runner_client.os, "umask"), \
                mock.patch.object(runner_client.signal, "signal",
                                  side_effect=lambda sig, fn: handlers.__setitem__(sig, fn)), \
                contextlib.redirect_stdout(out), self.assertRaises(SystemExit):
            runner_client.main(["--server", "http://127.0.0.1:9", "--token-file", str(token),
                                "--settings-file", str(settings), "--browser-root", str(root / "b"), *argv_extra])
        return made, handlers, out.getvalue()

    def test_stop_closes_lent_tabs(self):
        made, handlers, _ = self.run_main(["--browser-only"])
        self.assertEqual([m.closed for m in made], [1])
        with self.assertRaises(SystemExit) as stop:  # SIGTERM takes the same way out
            handlers[runner_client.signal.SIGTERM](runner_client.signal.SIGTERM, None)
        self.assertEqual(stop.exception.code, 0)

    def test_lend_with_turns_says_chats_have_no_browser(self):
        _, _, said = self.run_main()
        self.assertIn("--browser-only", said)
        self.assertIn("no browser", said)
        _, _, said = self.run_main(["--browser-only"])
        self.assertNotIn("no browser", said)


class BadJobReply(unittest.TestCase):
    def test_bad_job_with_a_turn_gets_an_error_reply(self):
        posted = []

        class C:
            def post(self, path, body, timeout=30):
                posted.append((path, body))

        lender = runner_client.Lender(Path(tempfile.gettempdir()) / "unused", Path("/nonexistent/s.json"),
                                      make=lambda *a: FakeLocal())
        turn = "c" * 32
        bad = {"kind": "browser", "turn": turn, "op": "call", "ws": "demo", "bot": "research", "epoch": "nope",
               "frame": call(5, P + "get_title")}
        with contextlib.redirect_stderr(io.StringIO()):
            runner_client.safe_browser_job(bad, C(), lender)
            runner_client.safe_browser_job({**bad, "turn": "not-a-turn"}, C(), lender)
            runner_client.safe_browser_job("junk", C(), lender)
        self.assertEqual(len(posted), 1)
        path, body = posted[0]
        self.assertEqual(path, f"/api/runner/reply/{turn}")
        self.assertEqual(body["reply"]["error"]["message"], browser_lent.BAD_JOB)
        self.assertEqual(body["reply"]["id"], 5)

    def test_reply_cap_is_the_hubs(self):
        self.assertFalse(hasattr(runner_client, "MAX_REPLY_CHARS"))
        posted = []

        class C:
            def post(self, path, body, timeout=30):
                posted.append(body)

        class Big:
            def answer(self, job):
                return ok(1, "x" * 2000)

        with mock.patch.object(runners, "MAX_TURN_CHARS", 1000):
            runner_client.safe_browser_job({"turn": "d" * 32}, C(), Big())
        self.assertEqual(posted[0]["reply"]["error"]["message"], browser_lent.TOO_LONG)

    def test_lend_off_line_names_the_command(self):
        self.assertIn("--lend-chrome on", browser_lent.LEND_OFF)
        self.assertNotIn("\u2014", browser_lent.LEND_OFF)


class CloseOnDelete(Base):
    def test_workspace_delete_closes_the_runners_lent_tabs(self):
        settings = self.root / "s.json"
        runner_client.set_lend(settings, True)
        tabs = {}

        class Tab(FakeLocal):
            closed = False

            def close(self):
                self.closed = True

        lender = runner_client.Lender(self.root / "lend", settings,
                                      make=lambda ws, bot, epoch: tabs.setdefault((ws, bot, epoch), Tab()))
        r = self.runner("rn-o", "owner", answer=lambda job: lender.answer(job) if job["op"] != "close" else None)
        other = self.runner("rn-p", "profile-b")
        pool = bu.Pool()
        ctx = {"ws": "demo", "bot": "research", "runner_owner": "owner", "shared": False}
        with mock.patch.object(bu.browser, "epochs", return_value={"demo": EPOCH}), \
                mock.patch.dict(os.environ, {"LEOBOT_DEPLOYMENT": "server"}), \
                mock.patch.object(browser_lent.runners, "HUB", self.hub):
            up = pool.get("demo", "research", "my_chrome", self.root, ctx=ctx)
            up.hub = self.hub
            up.POLL = 0.05
            self.assertEqual(up.current_url(), "https://acme.example/page")
            lender.answer({"kind": "browser", "turn": "e" * 32, "op": "url", "ws": "acme", "bot": "research",
                           "epoch": EPOCH})  # another workspace: stays open
            pool.kill_where("demo", EPOCH)
        deadline = time.time() + 2
        while not any(j["op"] == "close" for j in r.jobs) and time.time() < deadline:
            time.sleep(0.02)
        close = [j for j in r.jobs if j["op"] == "close"]
        self.assertEqual([(j["ws"], j["epoch"]) for j in close], [("demo", EPOCH)])
        lender.answer(close[0])
        self.assertTrue(tabs[("demo", "research", EPOCH)].closed)
        self.assertFalse(tabs[("acme", "research", EPOCH)].closed)
        self.assertNotIn(("demo", "research", EPOCH), lender._ups)
        self.assertEqual([j for j in other.jobs if j["op"] == "close"], [])  # only the person who lent
        self.assertEqual(self.hub._turns, {})  # a close note is not kept waiting for an answer

    def test_delete_notifies_the_owners_lenders_with_no_lent_row_here(self):
        # A server restarted since the tab was lent keeps no _lent row: the owner's runners still
        # hear about the delete, and close that workspace's tabs at once.
        r = self.runner("rn-o", "owner", answer=lambda job: None)
        pool = bu.Pool()
        with mock.patch.dict(os.environ, {"LEOBOT_DEPLOYMENT": "server"}), \
                mock.patch.object(bu.runners, "HUB", self.hub):
            pool.kill_where("demo", EPOCH)
        deadline = time.time() + 2
        while not any(j["op"] == "close" for j in r.jobs) and time.time() < deadline:
            time.sleep(0.02)
        self.assertEqual([(j["ws"], j["epoch"], j["runner"]) for j in r.jobs if j["op"] == "close"],
                         [("demo", EPOCH, "rn-o")])

    def test_delete_on_a_local_install_notifies_no_runner(self):
        r = self.runner("rn-o", "owner", answer=lambda job: None)
        with mock.patch.dict(os.environ, {"LEOBOT_DEPLOYMENT": "local"}), \
                mock.patch.object(bu.runners, "HUB", self.hub):
            bu.Pool().kill_where("demo", EPOCH)
        time.sleep(0.3)
        self.assertEqual([j for j in r.jobs if j["op"] == "close"], [])

    def test_known_tools_are_the_saved_list(self):
        tools = [{"name": P + "get_title", "inputSchema": {"type": "object", "properties": {"x": {}}}}]
        self.assertIsNone(self.lent().known_tools())
        browser_lent._save_tools(self.root, "owner", tools)
        self.assertEqual(self.lent().known_tools(), tools)
        self.assertIsNone(self.lent(shared=True).known_tools())

    def test_close_note_untaken_expires(self):
        self.hub.notify("owner", {"op": "close", "ws": "demo", "epoch": EPOCH})
        with mock.patch.object(runners, "NOTE_SECONDS", 0):
            self.assertIsNone(self.hub.next("rn-o", "owner", wait=0, caps=LEND))
        self.assertEqual((self.hub._turns, self.hub._waiting.get("owner")), ({}, []))

    def test_close_job_shape(self):
        runner_client.check_browser_job({"kind": "browser", "turn": "f" * 32, "op": "close", "ws": "demo",
                                         "epoch": EPOCH})
        with self.assertRaises(ValueError):
            runner_client.check_browser_job({"kind": "browser", "turn": "f" * 32, "op": "close", "ws": "demo"})


class HubCaps(unittest.TestCase):
    def setUp(self):
        self.hub = runners.Hub()

    def test_browser_jobs_only_to_lending_runners_and_turns_not_to_lend_only(self):
        self.hub.next("rn-t", "owner", wait=0)  # an older runner: turns only
        self.hub.next("rn-l", "owner", wait=0, caps=LEND)
        self.assertTrue(self.hub.online("owner"))
        self.assertEqual(self.hub.lenders("owner"), ["rn-l"])
        bjob = self.hub.submit("owner", {"kind": "browser", "op": "url", "runner": "rn-l"})
        tjob = self.hub.submit("owner", {"text": "hi"})
        self.assertEqual(self.hub.next("rn-t", "owner", wait=0)["turn"], tjob.id)
        self.assertIsNone(self.hub.next("rn-t", "owner", wait=0))
        self.assertEqual(self.hub.next("rn-l", "owner", wait=0, caps=LEND)["turn"], bjob.id)

    def test_lend_only_runner_is_not_a_turn_engine(self):
        self.hub.next("rn-l", "owner", wait=0, caps=LEND)
        self.assertFalse(self.hub.online("owner"))
        self.assertTrue(self.hub.reachable("owner"))

    def test_pinned_job_waits_for_its_runner(self):
        self.hub.next("rn-1", "owner", wait=0, caps=LEND)
        self.hub.next("rn-2", "owner", wait=0, caps=LEND)
        job = self.hub.submit("owner", {"kind": "browser", "op": "url", "runner": "rn-2"})
        self.assertIsNone(self.hub.next("rn-1", "owner", wait=0, caps=LEND))
        self.assertEqual(self.hub.next("rn-2", "owner", wait=0, caps=LEND)["turn"], job.id)

    def test_events_never_land_on_a_browser_job(self):
        turn = self.hub.submit("owner", {"kind": "browser", "op": "url"})
        self.hub.next("rn-l", "owner", wait=0, caps=LEND)
        self.assertTrue(self.hub.events("rn-l", turn.id, [["delta", "x"]]))
        self.assertTrue(turn.events.empty())


class FakeLocal(browser_relay.Upstream):
    """The runner's own my_chrome upstream, recorded."""
    mode = "my_chrome"

    def __init__(self, schemas=None):
        self.frames = []
        self.url = "https://acme.example/page"
        if schemas is not None:
            browser_relay._state(self)["schemas"].update(schemas)

    def forward(self, frame, sid):
        self.frames.append(frame)
        if frame.get("method") == "tools/list":
            return {"jsonrpc": "2.0", "id": frame.get("id"), "result": {"tools": [
                {"name": P + "click", "inputSchema": {"type": "object", "properties": {
                    "selector": {}, "newTab": {}, "session": {}}}},
                {"name": P + "eval", "inputSchema": {"type": "object", "properties": {"script": {}}}},
                {"name": P + "tab_new", "inputSchema": {"type": "object", "properties": {}}},
                {"name": P + "cookies_get", "inputSchema": {"type": "object", "properties": {}}}]}}, sid
        return ok(frame.get("id"), "done"), sid

    def current_url(self):
        return self.url


class RunnerCheck(unittest.TestCase):
    def check(self, name, schemas=None, **args):
        local = FakeLocal(schemas)
        fwd, refusal = browser_lent.runner_check(call(1, name, **args), local)
        return fwd, refusal

    def test_refuses_tools_outside_the_four_sets(self):
        for name in (P + "eval", P + "cookies_get", P + "storage_get", P + "upload", P + "close", "other"):
            fwd, refusal = self.check(name, script="document.cookie")
            self.assertIsNone(fwd, name)
            self.assertTrue(refusal["result"]["isError"])

    def test_refuses_tab_tools_and_new_tab_clicks(self):
        for name in browser_relay.CHROME_TAB_TOOLS:
            self.assertEqual(text(self.check(name)[1]), browser_relay.CHROME_TABS)
        self.assertEqual(text(self.check(P + "click", selector="#a", newTab=True)[1]), browser_relay.CHROME_NEW_TAB)

    def test_drops_engine_wide_args_and_undeclared_keys(self):
        fwd, refusal = self.check(P + "screenshot", path="/tmp/x.png", screenshotDir="/tmp", session="s",
                                  namespace="n", restoreKey="k", headed=True, extraArgs=["--x"], webmcp=True,
                                  allowedDomains=["a"], caCert="c", clearCaCert=True, useSystemCa=True,
                                  idleTimeout=1, full=True)
        self.assertIsNone(refusal)
        self.assertEqual(fwd["params"]["arguments"], {"full": True})
        fwd, _ = self.check(P + "click", schemas={P + "click": frozenset({"selector", "newTab"})},
                            selector="#a", sneaky=1)
        self.assertEqual(fwd["params"]["arguments"], {"selector": "#a"})

    def test_only_web_addresses_open(self):
        for url in ("file:///etc/passwd", "chrome://settings", "javascript:alert(1)", 7):
            self.assertEqual(text(self.check(P + "open", url=url)[1]), browser_relay.BAD_URL)
        self.assertIsNone(self.check(P + "open", url="https://acme.example/")[1])
        self.assertIsNone(self.check(P + "open", url="about:blank")[1])
        self.assertEqual(text(self.check(P + "read", url="https://acme.example/")[1]), browser_relay.READ_URL)

    def test_only_the_relays_own_field_read_may_eval(self):
        fwd, refusal = self.check(browser_relay.EVAL, script=browser_relay.FIELDS_SCRIPT, timeoutMs=999999)
        self.assertIsNone(refusal)
        self.assertEqual(fwd["params"]["arguments"],
                         {"script": browser_relay.FIELDS_SCRIPT, "timeoutMs": browser_relay.EVAL_TIMEOUT_MS})
        self.assertIsNotNone(self.check(browser_relay.EVAL, script=browser_relay.FIELDS_SCRIPT + ";1")[1])

    def test_only_list_and_call(self):
        local = FakeLocal()
        for method in ("initialize", "resources/list", "notifications/x", None):
            fwd, refusal = browser_lent.runner_check({"jsonrpc": "2.0", "id": 1, "method": method}, local)
            self.assertIsNone(fwd)
            self.assertIn("error", refusal)


class LenderFlow(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.settings = self.root / "settings.json"
        self.local = FakeLocal()
        self.lender = runner_client.Lender(self.root / "lend", self.settings, make=lambda *a: self.local)

    def job(self, op, frame=None, **kw):
        out = {"kind": "browser", "turn": "a" * 32, "op": op, "ws": "demo", "bot": "research", "epoch": EPOCH}
        if frame is not None:
            out["frame"] = frame
        out.update(kw)
        return out

    def test_lend_setting_default_off_local_file_only(self):
        self.assertFalse(runner_client.lend_on(self.settings))
        self.assertEqual(self.lender.answer(self.job("url")), {"refused": "lend_off"})
        self.assertEqual(self.local.frames, [])
        runner_client.set_lend(self.settings, True)
        self.assertTrue(runner_client.lend_on(self.settings))
        self.assertEqual(stat.S_IMODE(self.settings.stat().st_mode), 0o600)
        self.settings.write_text("not json")
        self.assertFalse(runner_client.lend_on(self.settings))
        with self.assertRaises(SystemExit) as done, contextlib.redirect_stdout(io.StringIO()):
            runner_client.main(["--lend-chrome", "on", "--settings-file", str(self.settings)])
        self.assertEqual(done.exception.code, 0)
        self.assertTrue(runner_client.lend_on(self.settings))
        with self.assertRaises(SystemExit), contextlib.redirect_stdout(io.StringIO()):
            runner_client.main(["--lend-chrome", "off", "--settings-file", str(self.settings)])
        self.assertFalse(runner_client.lend_on(self.settings))

    def test_answers_through_the_local_upstream_after_its_check(self):
        runner_client.set_lend(self.settings, True)
        self.assertEqual(self.lender.answer(self.job("url")), {"url": "https://acme.example/page"})
        listed = self.lender.answer(self.job("list", {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}))
        names = [t["name"] for t in listed["result"]["tools"]]
        self.assertEqual(names, [P + "click"])
        self.assertEqual(set(listed["result"]["tools"][0]["inputSchema"]["properties"]), {"selector"})
        reply = self.lender.answer(self.job("call", call(3, P + "get_title", session="x")))
        self.assertEqual(text(reply), "done")
        self.assertEqual(self.local.frames[-1]["params"]["arguments"], {})
        refused = self.lender.answer(self.job("call", call(4, P + "cookies_get")))
        self.assertTrue(refused["result"]["isError"])
        self.assertNotEqual(self.local.frames[-1]["params"]["name"], P + "cookies_get")

    def test_bad_jobs_refused(self):
        runner_client.set_lend(self.settings, True)
        for bad in (self.job("url", turn="nope"), self.job("url", epoch="xyz"), self.job("shell"),
                    self.job("call", frame="text"), self.job("url", ws=7), self.job("url", bot="b" * 300)):
            with self.assertRaises(ValueError):
                runner_client.check_browser_job(bad)

    def test_upstreams_are_per_bot_and_capped(self):
        made = []
        lender = runner_client.Lender(self.root / "lend", self.settings,
                                      make=lambda ws, bot, epoch: made.append((ws, bot, epoch)) or FakeLocal())
        runner_client.set_lend(self.settings, True)
        for i in range(runner_client.MAX_LENT + 2):
            lender.answer(self.job("url", bot=f"bot-{i}"))
        lender.answer(self.job("url", bot="bot-0", epoch="dddddd"))
        self.assertEqual(len(made), runner_client.MAX_LENT + 3)
        self.assertLessEqual(len(lender._ups), runner_client.MAX_LENT)


class Routes(sp.Base):
    """The runner's reply route: only the runner holding the job may answer it."""

    def test_reply_route_and_caps(self):
        hub = runners.Hub()
        quick = lambda rid, owner, wait=25, caps=None: runners.Hub.next(hub, rid, owner, wait=0, caps=caps)  # noqa: E731
        with mock.patch.object(runners, "HUB", hub), mock.patch.object(hub, "next", side_effect=quick):
            rid, tok = runners.create(self.root, "owner")
            _, tok2 = runners.create(self.root, "owner")
            h = {"Authorization": f"Bearer {tok}"}
            code, data, _ = sp.request("POST", "/api/runner/next", {"turns": False, "lend": True}, origin="", headers=h)
            self.assertEqual((code, data["job"]), (200, None))
            self.assertEqual(hub.lenders("owner"), [rid])
            self.assertFalse(hub.online("owner"))
            turn = hub.submit("owner", {"kind": "browser", "op": "url", "runner": rid})
            code, data, _ = sp.request("POST", "/api/runner/next", {"lend": True}, origin="", headers=h)
            self.assertEqual(data["job"]["turn"], turn.id)
            bad = sp.request("POST", f"/api/runner/reply/{turn.id}", {"reply": {"url": "x"}}, origin="",
                             headers={"Authorization": f"Bearer {tok2}"})
            self.assertEqual((bad[0], bad[1]["ok"]), (200, False))
            self.assertEqual(sp.request("POST", f"/api/runner/reply/{turn.id}", {"reply": {"url": "x"}},
                                        origin="")[0], 403)
            good = sp.request("POST", f"/api/runner/reply/{turn.id}", {"reply": {"url": "x"}}, origin="", headers=h)
            self.assertEqual((good[0], good[1]["ok"]), (200, True))
            self.assertEqual(turn.events.get_nowait(), ("reply", {"url": "x"}))

    def test_browser_ctx_names_the_turn_owner(self):
        src = (Path(sp.server.__file__)).read_text()
        mint = src[src.index("BROWSER_TOKENS.mint("):]
        mint = mint[:mint.index("ROOT)")]
        self.assertIn('"runner_owner": sender', mint)
        self.assertIn('"shared": shared', mint)


if __name__ == "__main__":
    unittest.main()
