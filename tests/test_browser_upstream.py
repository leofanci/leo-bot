"""The browser upstream: one engine child per bot, both modes, against a fake engine."""
import http.server
import json
import os
import signal
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from dashboard import browser_relay
from dashboard import browser_upstream as bu

FAKE = Path(__file__).resolve().parent / "fixtures" / "fake_agent_browser.py"
P = "agent_browser_"
LOOPBACK = "127.0" + ".0.1"
CDP = "ws" + "://" + LOOPBACK + ":9222/devtools/browser/abc-1"
CDP2 = "ws" + "://" + LOOPBACK + ":9333/devtools/browser/def-2"


def call(rid, name, **args):
    return {"jsonrpc": "2.0", "id": rid, "method": "tools/call", "params": {"name": name, "arguments": args}}


def text(reply):
    return reply["result"]["content"][0]["text"]


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        self.home, self.root = d / "home", d / "root"
        self.home.mkdir()
        self.root.mkdir()
        env = mock.patch.dict(os.environ, {"HOME": str(self.home), "LEOBOT_AGENT_BROWSER": str(FAKE),
                                           "AGENT_BROWSER_HEADED": "1", "AGENT_BROWSER_AUTO_CONNECT": "1",
                                           "AGENT_BROWSER_PROXY": "x", "LEOBOT_GATE_TOKEN": "secret"})
        env.start()
        self.addCleanup(env.stop)
        for target, attr, value in ((bu, "SOCKET_MAX", 1000),):  # temp homes are long: plain names here
            p = mock.patch.object(target, attr, value)
            p.start()
            self.addCleanup(p.stop)
        tools = mock.patch.dict(bu._TOOLS, clear=True)  # no tools/list known from another test
        tools.start()
        self.addCleanup(tools.stop)
        self.pool = bu.Pool()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.pool.close_all)

    def log(self, event=None):
        path = self.home / "fake-log.jsonl"
        rows = [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []
        return [r for r in rows if event is None or r["event"] == event]

    def control(self, **c):
        (self.home / "fake.json").write_text(json.dumps(c))

    def alive(self, pid):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        try:  # a zombie is gone too
            return os.waitpid(pid, os.WNOHANG) == (0, 0)
        except ChildProcessError:
            return True

    def chrome(self, value):
        p = mock.patch.object(bu, "find_chrome_debug_url", return_value=value)
        p.start()
        self.addCleanup(p.stop)
        return p


class Children(Base):
    def test_one_child_per_bot(self):
        a = self.pool.get("acme", "b1", "bot", self.root)
        self.assertIs(self.pool.get("acme", "b1", "bot", self.root), a)
        self.assertEqual(a.browser_key, "bot:acme--b1")
        reply, sid = a.forward(call(7, P + "open", url="about:blank"), "m-1")
        self.assertEqual((reply["id"], sid), (7, "m-1"))
        self.assertFalse(reply["result"]["isError"])
        a.forward(call(8, P + "click", selector="@e1"), None)
        self.assertEqual(len(self.log("start")), 1)
        b = self.pool.get("acme", "b2", "bot", self.root)
        self.assertIsNot(b, a)
        b.forward(call(1, P + "click", selector="@e1"), None)
        starts = self.log("start")
        self.assertEqual([s["env"]["AGENT_BROWSER_SESSION"] for s in starts], ["acme--b1", "acme--b2"])

    def test_concurrent_turns_start_one_child(self):
        ups, errors = [], []

        def turn():
            try:
                up = self.pool.get("acme", "b1", "bot", self.root)
                ups.append(up)
                up.forward(call(1, P + "click", selector="@e1"), None)
            except Exception as e:  # noqa: BLE001
                errors.append(e)
        threads = [threading.Thread(target=turn) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
        self.assertEqual(errors, [])
        self.assertEqual(len({id(u) for u in ups}), 1)
        self.assertEqual(len(self.log("start")), 1)

    def test_child_env_bot_mode_headless_and_allowlisted(self):
        up = self.pool.get("acme", "b1", "bot", self.root)
        up.forward(call(1, P + "click", selector="@e1"), None)
        start = self.log("start")[0]
        self.assertEqual(start["argv"], ["mcp", "--tools", "core"])
        key = bu.key_file(self.root).read_text()
        config = self.root / "run" / "agent-browser.json"
        self.assertEqual(start["env"], {
            "AGENT_BROWSER_CONFIG": str(config), "AGENT_BROWSER_HEADED": "false",
            "AGENT_BROWSER_SESSION": "acme--b1", "AGENT_BROWSER_NAMESPACE": "leobot",
            "AGENT_BROWSER_RESTORE": "acme--b1", "AGENT_BROWSER_ENCRYPTION_KEY": key,
            "AGENT_BROWSER_IDLE_TIMEOUT_MS": "600000", "AGENT_BROWSER_STATE_EXPIRE_DAYS": "365"})
        self.assertNotIn("AGENT_BROWSER_AUTO_CONNECT", start["keys"])  # set at all, it turns auto-connect on
        self.assertNotIn("LEOBOT_GATE_TOKEN", start["keys"])
        self.assertNotIn("LEOBOT_AGENT_BROWSER", start["keys"])
        self.assertEqual(json.loads(config.read_text()), {"headed": False, "autoConnect": False})
        self.assertEqual(stat.S_IMODE(config.stat().st_mode), 0o600)
        self.assertEqual(Path(start["cwd"]).resolve(), (self.root / "run" / "engine-cwd").resolve())

    def test_hostile_engine_config_ignored(self):
        user = self.home / ".agent-browser"
        user.mkdir()
        hostile = {"autoConnect": True, "headed": True, "cdp": "9222", "proxy": "x", "plugins": [{"name": "p"}]}
        (user / "config.json").write_text(json.dumps(hostile))
        cwd = self.root / "run" / "engine-cwd"
        cwd.mkdir(parents=True)
        (cwd / "agent-browser.json").write_text(json.dumps(hostile))
        up = self.pool.get("acme", "b1", "bot", self.root)
        up.forward(call(1, P + "click", selector="@e1"), None)
        self.assertEqual(self.log("start")[0]["settings"],
                         {"autoConnect": False, "headed": False, "cdp": None, "proxy": None, "plugins": None})
        self.assertFalse((cwd / "agent-browser.json").exists())

    def test_restore_key_is_the_session(self):
        for bot in ("b1", "b2"):
            self.pool.get("acme", bot, "bot", self.root).forward(call(1, P + "click", selector="@e1"), None)
        self.assertEqual([s["env"]["AGENT_BROWSER_RESTORE"] for s in self.log("start")], ["acme--b1", "acme--b2"])

    def test_mode_change_restarts_child(self):
        a = self.pool.get("acme", "b1", "bot", self.root)
        a.forward(call(1, P + "click", selector="@e1"), None)
        first = self.log("start")[0]["pid"]
        self.chrome(CDP)
        b = self.pool.get("acme", "b1", "my_chrome", self.root)
        self.assertIsNot(b, a)
        self.assertFalse(self.alive(first))
        closes = self.log("close")
        self.assertEqual(len(closes), 1)
        self.assertEqual(closes[0]["argv"], ["--session", "acme--b1", "--namespace", "leobot", "close"])
        self.assertTrue(closes[0]["env"]["AGENT_BROWSER_ENCRYPTION_KEY"])
        b.forward(call(2, P + "click", selector="@e1"), None)
        self.assertEqual(b.browser_key, "my_chrome")
        events = [r["event"] for r in self.log()]
        self.assertLess(events.index("close"), len(events) - 1 - events[::-1].index("start"))
        env = self.log("start")[1]["env"]
        self.assertEqual(env["AGENT_BROWSER_CDP"], CDP)
        self.assertEqual(env["AGENT_BROWSER_PIN_TAB"], "1")
        self.assertNotIn("AGENT_BROWSER_RESTORE", env)
        self.assertEqual(env["AGENT_BROWSER_SESSION"], "acme--b1--chrome")  # never the bot browser's daemon
        self.assertIs(self.pool.get("acme", "b1", "my_chrome", self.root), b)

    def test_my_chrome_close_ends_its_session_only(self):
        self.chrome(CDP)
        up = self.pool.get("acme", "b1", "my_chrome", self.root)
        up.forward(call(1, P + "click", selector="@e1"), None)
        pid = self.log("start")[0]["pid"]
        up.close()
        self.assertFalse(self.alive(pid))
        closes = self.log("close")
        self.assertEqual([c["argv"] for c in closes], [["--session", "acme--b1--chrome", "--namespace", "leobot", "close"]])
        self.assertNotIn("AGENT_BROWSER_CDP", closes[0]["env"])  # closing never attaches anew

    def test_my_chrome_quit_mid_session(self):
        p = self.chrome(CDP)
        up = self.pool.get("acme", "b1", "my_chrome", self.root)
        self.assertFalse(up.forward(call(1, P + "click", selector="@e1"), None)[0]["result"]["isError"])
        pid = self.log("start")[0]["pid"]
        p.stop()
        self.chrome(None)
        reply, _ = up.forward(call(2, P + "click", selector="@e1"), None)
        self.assertEqual(text(reply), bu.CHROME_OFF)
        self.assertFalse(self.alive(pid))
        self.assertEqual(len(self.log("close")), 1)
        up.forward(call(3, P + "click", selector="@e1"), None)  # still off: nothing more starts or closes
        self.assertEqual((len(self.log("start")), len(self.log("close"))), (1, 1))

    def test_my_chrome_restart_on_new_port(self):
        p = self.chrome(CDP)
        up = self.pool.get("acme", "b1", "my_chrome", self.root)
        up.forward(call(1, P + "click", selector="@e1"), None)
        pid = self.log("start")[0]["pid"]
        up.forward(call(2, P + "click", selector="@e1"), None)
        self.assertEqual(len(self.log("start")), 1)  # same Chrome: same child
        p.stop()
        self.chrome(CDP2)
        reply, _ = up.forward(call(3, P + "click", selector="@e1"), None)
        self.assertFalse(reply["result"]["isError"])
        self.assertFalse(self.alive(pid))
        self.assertEqual(len(self.log("close")), 1)
        self.assertEqual(self.log("start")[1]["env"]["AGENT_BROWSER_CDP"], CDP2)

    def test_my_chrome_tab_gone_opens_a_new_tab_and_acts_on_nothing(self):
        self.chrome(CDP)
        self.control(tab_gone_on=P + "click")
        up = self.pool.get("acme", "b1", "my_chrome", self.root)
        reply, _ = up.forward(call(1, P + "click", selector="@e1"), None)
        self.assertEqual(text(reply), bu.TAB_RESET)
        self.assertEqual([c["name"] for c in self.log("call")], [P + "click", P + "tab_new"])
        reply, _ = up.forward(call(2, P + "click", selector="@e1"), None)
        self.assertFalse(reply["result"]["isError"])
        bot = self.pool.get("acme", "b2", "bot", self.root)  # the bot browser passes it through
        (self.home / "tab-new-done").unlink()
        self.assertTrue(bot.forward(call(3, P + "click", selector="@e1"), None)[0]["result"]["isError"])

    def test_two_bots_share_my_chrome_each_own_tab(self):
        self.chrome(CDP)
        a = self.pool.get("acme", "b1", "my_chrome", self.root)
        b = self.pool.get("demo", "b2", "my_chrome", self.root)
        for up in (a, b):
            up.forward(call(1, P + "click", selector="@e1"), None)
        self.assertEqual(a.browser_key, b.browser_key)
        envs = [s["env"] for s in self.log("start")]
        self.assertEqual({e["AGENT_BROWSER_SESSION"] for e in envs}, {"acme--b1--chrome", "demo--b2--chrome"})
        self.assertTrue(all(e["AGENT_BROWSER_PIN_TAB"] == "1" for e in envs))

    def test_idle_sweep_closes(self):
        up = self.pool.get("acme", "b1", "bot", self.root)
        up.forward(call(1, P + "click", selector="@e1"), None)
        pid = self.log("start")[0]["pid"]
        self.assertEqual(self.pool.sweep(now=time.time() + 10), 0)
        self.assertTrue(self.alive(pid))
        self.assertEqual(self.pool.sweep(now=time.time() + bu.IDLE_SECONDS + 1), 1)
        self.assertFalse(self.alive(pid))
        self.assertEqual(len(self.log("close")), 1)
        self.assertIsNot(self.pool.get("acme", "b1", "bot", self.root), up)
        # a turn still holding the swept one is told, and nothing starts behind the pool
        reply, _ = up.forward(call(2, P + "click", selector="@e1"), None)
        self.assertEqual(text(reply), bu.CLOSED)
        self.assertEqual(len(self.log("start")), 1)

    def test_sweep_and_get_never_wait_on_a_busy_bot(self):
        busy = self.pool.get("acme", "b1", "bot", self.root)
        busy.forward(call(1, P + "click", selector="@e1"), None)
        self.control(hang_on=P + "click")
        with mock.patch.object(bu, "CALL_SECONDS", 3):
            t = threading.Thread(target=busy.forward, args=(call(2, P + "click", selector="@e1"), None))
            t.start()
            time.sleep(0.5)
            started = time.monotonic()
            self.assertEqual(self.pool.sweep(now=time.time() + bu.IDLE_SECONDS + 1), 0)  # in flight: left alone
            switch = threading.Thread(target=self.pool.get, args=("acme", "b1", "my_chrome", self.root))
            switch.start()  # waits for b1's call, and only b1's
            time.sleep(0.2)
            other = self.pool.get("acme", "b2", "bot", self.root)
            self.assertLess(time.monotonic() - started, 1.5)
            self.assertIsNot(other, busy)
            t.join(10)
            switch.join(10)
        self.assertTrue(busy.closed)

    def test_close_all_stops_every_child(self):
        for bot in ("b1", "b2"):
            self.pool.get("acme", bot, "bot", self.root).forward(call(1, P + "click", selector="@e1"), None)
        pids = [s["pid"] for s in self.log("start")]
        self.pool.close_all()
        self.assertFalse(any(self.alive(p) for p in pids))
        self.assertEqual(len(self.log("close")), 2)
        late = self.pool.get("acme", "b3", "bot", self.root)  # stopping: nothing new starts
        self.assertEqual(text(late.forward(call(1, P + "click", selector="@e1"), None)[0]), bu.STOPPING)
        self.assertEqual(len(self.log("start")), 2)

    def test_close_all_does_not_wait_for_calls_and_closes_in_parallel(self):
        ups = [self.pool.get("acme", f"b{i}", "bot", self.root) for i in range(3)]
        for up in ups:
            up.forward(call(1, P + "click", selector="@e1"), None)
        self.control(hang_on=P + "click")
        replies = []
        t = threading.Thread(target=lambda: replies.append(ups[0].forward(call(2, P + "click", selector="@e1"), None)))
        t.start()
        time.sleep(0.5)
        real = bu.BrowserUpstream._engine_close

        def slow(up):
            time.sleep(1)
            real(up)
        with mock.patch.object(bu.BrowserUpstream, "_engine_close", slow):
            started = time.monotonic()
            self.pool.close_all(bound=5)
            self.assertLess(time.monotonic() - started, 2.5)
        t.join(5)
        self.assertEqual(text(replies[0][0]), bu.CRASHED)
        self.assertEqual(len(self.log("close")), 3)

    def test_close_without_start_or_key_skips_engine_close(self):
        up = self.pool.get("acme", "b1", "bot", self.root)
        up.close()
        self.assertEqual(self.log(), [])
        up = self.pool.get("acme", "b2", "bot", self.root)
        up.forward(call(1, P + "click", selector="@e1"), None)
        bu.key_file(self.root).write_text("")  # never an empty key: skip instead
        up.close()
        self.assertEqual(self.log("close"), [])

    def test_my_chrome_off_says_how_and_starts_nothing(self):
        self.chrome(None)
        up = self.pool.get("acme", "b1", "my_chrome", self.root)
        reply, _ = up.forward(call(3, P + "open", url="about:blank"), None)
        listed, _ = up.forward({"jsonrpc": "2.0", "id": 4, "method": "tools/list"}, None)
        self.assertEqual(reply["id"], 3)
        self.assertTrue(reply["result"]["isError"])
        self.assertEqual(text(reply), bu.CHROME_OFF)
        self.assertIn("error", listed)
        self.assertEqual(self.log(), [])
        self.assertIsNone(up.current_url())
        # once the engine's tools are known, Chrome off still lists them; the call says how
        self.pool.get("acme", "b2", "bot", self.root).forward(call(1, P + "click", selector="@e1"), None)
        listed, _ = up.forward({"jsonrpc": "2.0", "id": 5, "method": "tools/list"}, None)
        self.assertEqual(listed["id"], 5)
        self.assertIn(P + "open", [t["name"] for t in listed["result"]["tools"]])
        self.assertEqual(len(self.log("start")), 1)

    def test_child_crash_reported_once(self):
        up = self.pool.get("acme", "b1", "bot", self.root)
        up.forward(call(1, P + "open", url="about:blank"), None)
        first = self.log("start")[0]["pid"]
        self.control(crash_on=P + "click")
        reply, _ = up.forward(call(2, P + "click", selector="@e1"), None)
        self.assertEqual(text(reply), bu.CRASHED)
        self.assertTrue(reply["result"]["isError"])
        self.assertFalse(self.alive(first))
        self.control()
        reply, _ = up.forward(call(3, P + "click", selector="@e1"), None)
        self.assertFalse(reply["result"]["isError"])
        starts = self.log("start")
        self.assertEqual(len(starts), 2)
        self.assertNotEqual(starts[1]["pid"], first)

    def test_call_timeout_stops_child_and_next_starts_fresh(self):
        up = self.pool.get("acme", "b1", "bot", self.root)
        up.forward(call(1, P + "open", url="about:blank"), None)
        first = self.log("start")[0]["pid"]
        self.control(hang_on=P + "click")
        with mock.patch.object(bu, "CALL_SECONDS", 1):
            reply, _ = up.forward(call(2, P + "click", selector="@e1"), None)
        self.assertEqual(text(reply), bu.TIMED_OUT.format(s=1))
        self.assertFalse(self.alive(first))
        self.control()
        reply, _ = up.forward(call(3, P + "click", selector="@e1"), None)
        self.assertFalse(reply["result"]["isError"])
        self.assertEqual(len(self.log("start")), 2)

    def test_odd_ids_start_with_hashed_names(self):
        for ws, bot in (("a--b", "b1"), ("acme", "bot-")):
            up = self.pool.get(ws, bot, "bot", self.root)
            reply, _ = up.forward(call(1, P + "click", selector="@e1"), None)
            self.assertFalse(reply["result"]["isError"], (ws, bot))
        self.assertEqual([s["env"]["AGENT_BROWSER_SESSION"] for s in self.log("start")],
                         [bu.session_part("a--b") + "--b1", "acme--" + bu.session_part("bot-")])

    def test_no_engine_refused(self):
        with mock.patch.object(bu.browser_engine, "resolve", return_value=None):
            up = bu.BrowserUpstream("acme", "b1", "bot", self.root)
            reply, _ = up.forward(call(1, P + "click", selector="@e1"), None)
        self.assertEqual(text(reply), bu.NO_ENGINE)

    def test_bad_tools_list_fails_start(self):
        self.control(bad_list=True)
        up = self.pool.get("acme", "b1", "bot", self.root)
        reply, _ = up.forward(call(1, P + "click", selector="@e1"), None)
        self.assertEqual(text(reply), bu.NO_START)
        self.assertFalse(self.alive(self.log("start")[0]["pid"]))

    def test_start_fills_relay_schemas_and_url(self):
        up = self.pool.get("acme", "b1", "bot", self.root)
        init, _ = up.forward({"jsonrpc": "2.0", "id": 0, "method": "initialize",
                              "params": {"protocolVersion": "2024-11-05"}}, None)
        self.assertEqual(init["result"]["protocolVersion"], "2024-11-05")
        self.assertEqual(self.log(), [])  # initialize starts nothing
        self.assertIsNone(up.forward({"jsonrpc": "2.0", "method": "notifications/initialized"}, None)[0])
        up.forward(call(1, P + "open", url="about:blank"), None)
        schemas = browser_relay._state(up)["schemas"]
        self.assertEqual(schemas[P + "open"], frozenset({"url", "session", "timeoutMs"}))
        self.assertEqual(up.current_url(), "about:blank")
        listed, _ = up.forward({"jsonrpc": "2.0", "id": 9, "method": "tools/list"}, None)
        self.assertEqual(listed["id"], 9)
        self.assertIn(P + "open", [t["name"] for t in listed["result"]["tools"]])


class Sweeper(Base):
    def test_sweeper_thread_sweeps_until_close_all(self):
        swept = threading.Event()
        with mock.patch.object(self.pool, "sweep", side_effect=lambda: swept.set()):
            thread = self.pool.start_sweeper(interval=0.01)
            self.assertTrue(swept.wait(5))
            self.pool.close_all()
            thread.join(5)
        self.assertFalse(thread.is_alive())

    def test_server_shutdown_closes_browsers(self):
        from dashboard import server
        with mock.patch.object(server.browser_upstream.POOL, "close_all") as close_all, \
                mock.patch.object(server, "remove_uploads"), mock.patch.object(server, "remove_token_file"), \
                mock.patch.object(server, "release_pid_lock"), \
                mock.patch.object(server.chat_session, "remove_bot_tmpdir"), \
                mock.patch.object(server, "_CHAT", None), mock.patch.object(server, "_PERSON_CHATS", {}):
            server.shutdown_children()
        close_all.assert_called_once_with()


class Names(unittest.TestCase):
    def test_session_part(self):
        self.assertEqual(bu.session_part("acme"), "acme")
        for odd in ("a--b", "bot-", "-a", "Acme", "b 1", "", "h0123abcd"):
            part = bu.session_part(odd)
            self.assertRegex(part, r"^h[0-9a-f]{8}$", odd)
        self.assertNotEqual(bu.session_part("a--b"), bu.session_part("a-b"))
        self.assertNotEqual(bu.session_part("h0123abcd"), "h0123abcd")  # a real id never looks hashed

    def test_long_names_hash_by_fixed_length(self):
        self.assertEqual(bu.session_name("acme", "b1", "bot"), "acme--b1")
        self.assertEqual(bu.session_name("acme", "b1", "my_chrome"), "acme--b1--chrome")
        long_bot, long_chrome = (bu.session_name("w" * 20, "b" * 20, m) for m in ("bot", "my_chrome"))
        self.assertRegex(long_bot, r"^x[0-9a-f]{16}$")
        self.assertEqual(long_chrome, long_bot + "c")
        ws = "a" * (bu.MAX_SESSION - len("--b1"))  # just at the limit stays plain
        self.assertEqual(bu.session_name(ws, "b1", "bot"), ws + "--b1")
        self.assertNotEqual(bu.session_name(ws, "b1", "my_chrome"), ws + "--b1--chrome")

    def test_socket_check_normalizes_home(self):
        real = os.path.realpath("/nonexistent-leobot-home")  # realpath keeps a path that does not exist
        budget = bu.SOCKET_MAX - bu.SOCKET_FIXED - len(bu.NAMESPACE) - len(real)
        for home in (real, real + "/", real + "//"):
            self.assertTrue(bu.session_fits("s" * budget, home), home)
            self.assertFalse(bu.session_fits("s" * (budget + 1), home), home)
        with tempfile.TemporaryDirectory() as d:  # a symlinked home counts by its real path
            link = Path(d) / "h"
            link.symlink_to(real)
            self.assertTrue(bu.session_fits("s" * budget, str(link) + "/"))


class LongHome(Base):
    def test_long_home_refused_plainly(self):
        deep = self.home / ("d" * 90)
        deep.mkdir()
        with mock.patch.object(bu, "SOCKET_MAX", 103), mock.patch.dict(os.environ, {"HOME": str(deep)}):
            up = self.pool.get("acme", "b1", "bot", self.root)
            reply, _ = up.forward(call(1, P + "click", selector="@e1"), None)
        self.assertEqual(text(reply), bu.LONG_HOME)
        self.assertNotIn("\u2014", bu.LONG_HOME)
        self.assertEqual(self.log(), [])


class Shutdown(Base):
    def test_kill_before_start_starts_nothing(self):
        up = self.pool.get("acme", "b1", "bot", self.root)
        real = bu.engine_dirs

        def racing(root):  # shutdown lands after the call passed its own closed check
            up.kill()
            return real(root)
        with mock.patch.object(bu, "engine_dirs", racing):
            reply, _ = up.forward(call(1, P + "click", selector="@e1"), None)
        self.assertEqual(text(reply), bu.STOPPING)
        self.assertEqual(self.log("start"), [])

    def test_kill_during_handshake_kills_the_new_child(self):
        up = self.pool.get("acme", "b1", "bot", self.root)
        real = bu._Child.request
        fired = []

        def racing(child, method, params, timeout):
            if not fired:
                fired.append(1)
                deadline = time.monotonic() + 5
                while not (self.home / "fake-log.jsonl").exists() and time.monotonic() < deadline:
                    time.sleep(0.05)
                up.kill()
            return real(child, method, params, timeout)
        with mock.patch.object(bu._Child, "request", racing):
            reply, _ = up.forward(call(1, P + "click", selector="@e1"), None)
        self.assertEqual(text(reply), bu.STOPPING)
        pid = self.log("start")[0]["pid"]
        self.assertFalse(self.alive(pid))
        self.assertIsNone(up._child)
        self.assertIsNone(up._pending)


class Stop(unittest.TestCase):
    def test_stop_returns_while_a_grandchild_holds_stdout(self):
        script = ("import json, subprocess, sys\n"
                  "g = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
                  "print(json.dumps({'grandchild': g.pid}), flush=True)\n"
                  "sys.stdin.read()\n")
        proc = subprocess.Popen([sys.executable, "-c", script], stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        child = bu._Child(proc)
        grand = child.lines.get(timeout=10)["grandchild"]
        self.addCleanup(lambda: os.kill(grand, signal.SIGKILL))
        started = time.monotonic()
        child.stop()
        self.assertLess(time.monotonic() - started, 3)
        self.assertIsNotNone(proc.poll())


class Key(unittest.TestCase):
    def test_key_file_0600_and_stable(self):
        with tempfile.TemporaryDirectory() as d:
            path = bu.key_file(d)
            self.assertEqual(path, Path(d) / "run" / "browser-engine-key")
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            key = path.read_text()
            self.assertRegex(key, r"^[0-9a-f]{64}$")
            self.assertEqual(bu.key_file(d).read_text(), key)
            self.assertNotEqual(bu.key_file(Path(d) / "other").read_text(), key)

    def test_unreadable_key_refuses_start(self):
        with tempfile.TemporaryDirectory() as d:
            bu.key_file(d).write_text("short")
            up = bu.BrowserUpstream("acme", "b1", "bot", d)
            with mock.patch.dict(os.environ, {"LEOBOT_AGENT_BROWSER": str(FAKE), "HOME": d}):
                reply, _ = up.forward(call(1, P + "click", selector="@e1"), None)
            self.assertEqual(text(reply), bu.NO_KEY)
            self.assertFalse((Path(d) / "fake-log.jsonl").exists())


class ChromeUrl(unittest.TestCase):
    def _port_file(self, home, port, rel="Library/Application Support/Google/Chrome", path="/devtools/browser/abc-1"):
        folder = Path(home) / rel
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "DevToolsActivePort").write_text(f"{port}\n{path}\n")

    def _chrome_like(self, ws_path):
        """A loopback HTTP server answering /json/version like Chrome."""
        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                body = json.dumps({"webSocketDebuggerUrl": "ws" + "://" + LOOPBACK + ":1" + ws_path}).encode()
                self.send_response(200 if self.path == "/json/version" else 404)
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass
        srv = http.server.ThreadingHTTPServer((LOOPBACK, 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        return srv.server_address[1]

    def test_listening_port_gives_ws_url(self):
        port = self._chrome_like("/devtools/browser/abc-1")
        with tempfile.TemporaryDirectory() as home:
            self._port_file(home, port)
            self.assertEqual(bu.find_chrome_debug_url(home=home, platform="darwin"),
                             "ws" + "://" + LOOPBACK + f":{port}/devtools/browser/abc-1")
            self.assertIsNone(bu.find_chrome_debug_url(home=home, platform="win32"))

    def test_other_browser_id_on_port_is_none(self):
        port = self._chrome_like("/devtools/browser/zzz-9")  # another Chrome run reuses the port
        with tempfile.TemporaryDirectory() as home:
            self._port_file(home, port)
            self.assertIsNone(bu.find_chrome_debug_url(home=home, platform="darwin"))

    def _raw_listener(self, answer):
        """A loopback listener that answers each connection with `answer` (None: reset it)."""
        srv = socket.socket()
        srv.bind((LOOPBACK, 0))
        srv.listen(8)
        self.addCleanup(srv.close)

        def serve():
            while True:
                try:
                    conn, _ = srv.accept()
                except OSError:
                    return
                if answer is None:
                    conn.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
                else:
                    try:
                        conn.recv(1024)
                        conn.sendall(answer)
                    except OSError:
                        pass
                conn.close()
        threading.Thread(target=serve, daemon=True).start()
        return srv.getsockname()[1]

    def _falls_back(self, port):
        with mock.patch.object(bu, "CONNECT_SECONDS", 0.3), tempfile.TemporaryDirectory() as home:
            self._port_file(home, port)
            self.assertEqual(bu.find_chrome_debug_url(home=home, platform="darwin"),
                             "ws" + "://" + LOOPBACK + f":{port}/devtools/browser/abc-1")

    def test_no_json_version_falls_back_to_tcp(self):
        self._falls_back(self._raw_listener(b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\n\r\n"))

    def test_reset_falls_back_to_tcp(self):
        self._falls_back(self._raw_listener(None))

    def test_not_http_falls_back_to_tcp(self):
        self._falls_back(self._raw_listener(b"garbage that is not http\n"))

    def test_silent_listener_falls_back_to_tcp(self):
        srv = socket.socket()
        srv.bind((LOOPBACK, 0))
        srv.listen(1)
        self.addCleanup(srv.close)
        self._falls_back(srv.getsockname()[1])

    def test_stale_file_is_none(self):
        srv = socket.socket()
        srv.bind((LOOPBACK, 0))
        port = srv.getsockname()[1]
        srv.close()  # Chrome quit: the file stays, nothing listens
        with tempfile.TemporaryDirectory() as home:
            self._port_file(home, port, ".config/google-chrome")
            self.assertIsNone(bu.find_chrome_debug_url(home=home, platform="linux"))

    def test_missing_or_bad_file_is_none_and_next_candidate_used(self):
        port = self._chrome_like("/devtools/browser/abc-1")
        with tempfile.TemporaryDirectory() as home:
            self.assertIsNone(bu.find_chrome_debug_url(home=home, platform="darwin"))
            for garbage in ("notaport", "9²", "٣٣", "-1", "99999", ""):
                self._port_file(home, garbage)
                self.assertIsNone(bu.find_chrome_debug_url(home=home, platform="darwin"), garbage)
            (Path(home) / "Library/Application Support/Google/Chrome/DevToolsActivePort").write_bytes(b"\xff\xfe\n")
            self._port_file(home, port, "Library/Application Support/Chromium")
            self.assertEqual(bu.find_chrome_debug_url(home=home, platform="darwin"),
                             "ws" + "://" + LOOPBACK + f":{port}/devtools/browser/abc-1")


if __name__ == "__main__":
    unittest.main()
