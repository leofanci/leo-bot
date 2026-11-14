"""The browser upstream: one engine child per bot, both modes, against a fake engine."""
import json
import os
import socket
import stat
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


def call(rid, name, **args):
    return {"jsonrpc": "2.0", "id": rid, "method": "tools/call", "params": {"name": name, "arguments": args}}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        self.home, self.root = d / "home", d / "root"
        self.home.mkdir()
        self.root.mkdir()
        env = mock.patch.dict(os.environ, {"HOME": str(self.home), "LEOBOT_AGENT_BROWSER": str(FAKE),
                                           "AGENT_BROWSER_HEADED": "1", "LEOBOT_GATE_TOKEN": "secret"})
        env.start()
        self.addCleanup(env.stop)
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
        self.assertEqual(start["env"], {
            "AGENT_BROWSER_SESSION": "acme--b1", "AGENT_BROWSER_NAMESPACE": "leobot",
            "AGENT_BROWSER_RESTORE": "1", "AGENT_BROWSER_ENCRYPTION_KEY": key,
            "AGENT_BROWSER_IDLE_TIMEOUT_MS": "600000", "AGENT_BROWSER_STATE_EXPIRE_DAYS": "365"})
        self.assertNotIn("LEOBOT_GATE_TOKEN", start["keys"])
        self.assertNotIn("LEOBOT_AGENT_BROWSER", start["keys"])

    def test_mode_change_restarts_child(self):
        a = self.pool.get("acme", "b1", "bot", self.root)
        a.forward(call(1, P + "click", selector="@e1"), None)
        first = self.log("start")[0]["pid"]
        with mock.patch.object(bu, "find_chrome_debug_url", return_value=CDP):
            b = self.pool.get("acme", "b1", "my_chrome", self.root)
            self.assertIsNot(b, a)
            self.assertFalse(self.alive(first))
            closes = self.log("close")
            self.assertEqual(len(closes), 1)
            self.assertEqual(closes[0]["argv"], ["--session", "acme--b1", "--namespace", "leobot", "close"])
            b.forward(call(2, P + "click", selector="@e1"), None)
        self.assertEqual(b.browser_key, "my_chrome")
        env = self.log("start")[1]["env"]
        self.assertEqual(env["AGENT_BROWSER_CDP"], CDP)
        self.assertEqual(env["AGENT_BROWSER_PIN_TAB"], "1")
        self.assertNotIn("AGENT_BROWSER_RESTORE", env)
        self.assertEqual(env["AGENT_BROWSER_SESSION"], "acme--b1--chrome")  # never the bot browser's daemon
        self.assertEqual(self.pool.get("acme", "b1", "my_chrome", self.root), b)

    def test_my_chrome_close_never_closes_chrome(self):
        with mock.patch.object(bu, "find_chrome_debug_url", return_value=CDP):
            up = self.pool.get("acme", "b1", "my_chrome", self.root)
            up.forward(call(1, P + "click", selector="@e1"), None)
        pid = self.log("start")[0]["pid"]
        self.pool.close_all()
        self.assertFalse(self.alive(pid))
        self.assertEqual(self.log("close"), [])

    def test_two_bots_share_my_chrome_each_own_tab(self):
        with mock.patch.object(bu, "find_chrome_debug_url", return_value=CDP):
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
        self.assertEqual(reply["result"]["content"][0]["text"], bu.CLOSED)
        self.assertEqual(len(self.log("start")), 1)

    def test_close_all_stops_every_child(self):
        for bot in ("b1", "b2"):
            self.pool.get("acme", bot, "bot", self.root).forward(call(1, P + "click", selector="@e1"), None)
        pids = [s["pid"] for s in self.log("start")]
        self.pool.close_all()
        self.assertFalse(any(self.alive(p) for p in pids))
        self.assertEqual(len(self.log("close")), 2)

    def test_my_chrome_off_says_how_and_starts_nothing(self):
        with mock.patch.object(bu, "find_chrome_debug_url", return_value=None):
            up = self.pool.get("acme", "b1", "my_chrome", self.root)
            reply, _ = up.forward(call(3, P + "open", url="about:blank"), None)
            listed, _ = up.forward({"jsonrpc": "2.0", "id": 4, "method": "tools/list"}, None)
        self.assertEqual(reply["id"], 3)
        self.assertTrue(reply["result"]["isError"])
        self.assertEqual(reply["result"]["content"][0]["text"], bu.CHROME_OFF)
        self.assertIn("error", listed)
        self.assertEqual(self.log(), [])
        self.assertIsNone(up.current_url())
        # once the engine's tools are known, Chrome off still lists them; the call says how
        self.pool.get("acme", "b2", "bot", self.root).forward(call(1, P + "click", selector="@e1"), None)
        with mock.patch.object(bu, "find_chrome_debug_url", return_value=None):
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
        self.assertEqual(reply["result"]["content"][0]["text"], bu.CRASHED)
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
        self.assertEqual(reply["result"]["content"][0]["text"], bu.TIMED_OUT.format(s=1))
        self.assertFalse(self.alive(first))
        self.control()
        reply, _ = up.forward(call(3, P + "click", selector="@e1"), None)
        self.assertFalse(reply["result"]["isError"])
        self.assertEqual(len(self.log("start")), 2)

    def test_bad_names_refused(self):
        for ws, bot in (("acme--x", "b1"), ("acme", "b-"), ("Acme", "b1"), ("acme", "b 1"), ("-a", "b"), ("", "b")):
            up = bu.BrowserUpstream(ws, bot, "bot", self.root)
            reply, _ = up.forward(call(1, P + "click", selector="@e1"), None)
            self.assertEqual(reply["result"]["content"][0]["text"], bu.BAD_NAME, (ws, bot))
            up.close()
        self.assertEqual(self.log(), [])

    def test_no_engine_refused(self):
        with mock.patch.object(bu.browser_engine, "resolve", return_value=None):
            up = bu.BrowserUpstream("acme", "b1", "bot", self.root)
            reply, _ = up.forward(call(1, P + "click", selector="@e1"), None)
        self.assertEqual(reply["result"]["content"][0]["text"], bu.NO_ENGINE)

    def test_bad_tools_list_fails_start(self):
        self.control(bad_list=True)
        up = self.pool.get("acme", "b1", "bot", self.root)
        reply, _ = up.forward(call(1, P + "click", selector="@e1"), None)
        self.assertEqual(reply["result"]["content"][0]["text"], bu.NO_START)
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
            self.assertEqual(reply["result"]["content"][0]["text"], bu.NO_KEY)
            self.assertFalse((Path(d) / "fake-log.jsonl").exists())


class ChromeUrl(unittest.TestCase):
    def _port_file(self, home, port, rel="Library/Application Support/Google/Chrome"):
        folder = Path(home) / rel
        folder.mkdir(parents=True)
        (folder / "DevToolsActivePort").write_text(f"{port}\n/devtools/browser/abc-1\n")

    def test_listening_port_gives_ws_url(self):
        srv = socket.socket()
        srv.bind((LOOPBACK, 0))
        srv.listen(1)
        self.addCleanup(srv.close)
        port = srv.getsockname()[1]
        with tempfile.TemporaryDirectory() as home:
            self._port_file(home, port)
            self.assertEqual(bu.find_chrome_debug_url(home=home, platform="darwin"),
                             "ws" + "://" + LOOPBACK + f":{port}/devtools/browser/abc-1")
            self.assertIsNone(bu.find_chrome_debug_url(home=home, platform="win32"))

    def test_stale_file_is_none(self):
        srv = socket.socket()
        srv.bind((LOOPBACK, 0))
        port = srv.getsockname()[1]
        srv.close()  # Chrome quit: the file stays, nothing listens
        with tempfile.TemporaryDirectory() as home:
            self._port_file(home, port, ".config/google-chrome")
            self.assertIsNone(bu.find_chrome_debug_url(home=home, platform="linux"))

    def test_missing_or_bad_file_is_none(self):
        with tempfile.TemporaryDirectory() as home:
            self.assertIsNone(bu.find_chrome_debug_url(home=home, platform="darwin"))
            folder = Path(home) / ".config/chromium"
            folder.mkdir(parents=True)
            (folder / "DevToolsActivePort").write_text("notaport\n/x\n")
            self.assertIsNone(bu.find_chrome_debug_url(home=home, platform="linux"))


if __name__ == "__main__":
    unittest.main()
