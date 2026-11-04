"""Runners: tokens, the hub, the locked-down job policy, and their HTTP routes."""
import json
import stat
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from core import people, runners
from dashboard import runner as runner_client
from dashboard.engines.base import TurnPolicy, TurnTokens
from dashboard.engines.claude_code import ClaudeCodeEngine
from dashboard.engines.runner import RunnerEngine
from dashboard.chat_session import ChatSession
from tests import test_server_people as sp

server = sp.server


class Tokens(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_stored_hashed_owner_only_and_verified(self):
        rid, token = runners.create(self.root, "p1", "Laptop")
        path = self.root / "team" / "runners.json"
        self.assertNotIn(token, path.read_text())
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(runners.verify(self.root, token)["owner"], "p1")
        for bad in ("myr_wrong", "", None, 7, token + "x"):
            self.assertIsNone(runners.verify(self.root, bad))

    def test_only_the_owner_can_revoke(self):
        rid, token = runners.create(self.root, "p1")
        self.assertFalse(runners.revoke(self.root, rid, "p2"))
        self.assertTrue(runners.revoke(self.root, rid, "p1"))
        self.assertIsNone(runners.verify(self.root, token))

    def test_list_never_shows_a_hash(self):
        runners.create(self.root, "p1")
        runners.create(self.root, "p2")
        rows = runners.list_for(self.root, "p1")
        self.assertEqual(len(rows), 1)
        self.assertNotIn("token_hash", json.dumps(rows))


class HubFlow(unittest.TestCase):
    def setUp(self):
        self.hub = runners.Hub()

    def test_job_goes_only_to_the_senders_runner(self):
        self.assertFalse(self.hub.online("p1"))
        self.hub.next("r2", "p2", wait=0)
        turn = self.hub.submit("p1", {"text": "secret"})
        self.assertIsNone(self.hub.next("r2", "p2", wait=0))
        job = self.hub.next("r1", "p1", wait=0)
        self.assertEqual(job["text"], "secret")
        self.assertTrue(self.hub.online("p1"))
        self.assertEqual(turn.runner, "r1")

    def test_events_only_from_the_runner_holding_the_turn(self):
        turn = self.hub.submit("p1", {})
        self.hub.next("r1", "p1", wait=0)
        self.assertTrue(self.hub.events("r9", turn.id, [["delta", "forged"]]))
        self.assertFalse(self.hub.events("r1", turn.id, [["delta", "ok"], ["nope", "x"]], end=True))
        self.assertEqual(list(self.hub.wait_events(turn)), [("delta", "ok")])

    def test_runner_lost_mid_turn_ends_with_an_error(self):
        turn = self.hub.submit("p1", {})
        self.hub.next("r1", "p1", wait=0)
        self.hub._seen["r1"] = ("p1", time.time() - runners.ONLINE_SECONDS - 1)
        ev = list(self.hub.wait_events(turn, poll=0.01))
        self.assertIn("offline", ev[-1][1])

    def test_job_nobody_takes_times_out(self):
        turn = self.hub.submit("p1", {})
        turn.created -= runners.LEASE_SECONDS + 1
        self.assertIn("didn't pick", list(self.hub.wait_events(turn, poll=0.01))[-1][1])

    def test_stop_reaches_the_runner(self):
        turn = self.hub.submit("p1", {})
        self.hub.next("r1", "p1", wait=0)
        self.hub.cancel(turn)
        self.assertTrue(self.hub.events("r1", turn.id, []))


class Engine(unittest.TestCase):
    def test_turn_sends_prompt_and_streams_back(self):
        hub = runners.Hub()
        sess = ChatSession(repo_dir=".", rail="RAIL")
        sess.engine = RunnerEngine("p1", "Pat", hub)
        sess.recap = "RECAP"

        def mac():
            job = hub.next("r1", "p1", wait=5)
            self.assertEqual((job["rail"], job["text"], job["recap"], job["os_token"]), ("RAIL", "hi", "RECAP", "tok"))
            hub.events("r1", job["turn"], [["delta", "yo"], ["done", {"tokens": 3}]], end=True)
        th = threading.Thread(target=mac)
        th.start()
        self.assertEqual(list(sess.ask("hi", os_token="tok"))[0], ("delta", "yo"))
        th.join()
        self.assertEqual(sess.engine.label, "Pat's Mac")
        self.assertFalse(sess.is_fresh())


class ClientSide(unittest.TestCase):
    def test_a_job_can_never_widen_the_policy(self):
        p = runner_client.job_policy({"policy": {"shared": False, "apps": True, "workspace": "acme"}})
        self.assertTrue(p.shared)
        self.assertFalse(p.apps)
        self.assertIsNone(p.workspace_dir)

    def test_runner_turn_has_no_shell_or_files_and_reaches_only_the_server(self):
        sess = runner_client.JobSession({"chat": "c", "rail": "R", "model": "haiku"}, "/tmp/x", False)
        policy = runner_client.job_policy({"policy": {"workspace": "acme"}})
        cmd = ClaudeCodeEngine().build_cmd(sess, policy, "haiku", os_url="https://myos.example/api/runner/os/mcp")
        tools = cmd[cmd.index("--tools") + 1]
        self.assertEqual(tools, "")
        allowed = cmd[cmd.index("--allowedTools") + 1:cmd.index("--disallowedTools")]
        self.assertEqual(allowed, ["mcp__os"])
        config = json.loads(cmd[cmd.index("--mcp-config") + 1])["mcpServers"]["os"]
        self.assertEqual(config["url"], "https://myos.example/api/runner/os/mcp")
        self.assertEqual(config["headers"]["X-Runner-Token"], "${MYOS_RUNNER_TOKEN}")
        self.assertNotIn("myr_", " ".join(cmd))

    def test_runner_token_rides_in_env_not_argv(self):
        env = ClaudeCodeEngine().turn_env(TurnPolicy(shared=True), TurnTokens(os_token="t", runner_token="myr_x"))
        self.assertEqual(env["MYOS_RUNNER_TOKEN"], "myr_x")


class Routes(sp.Base):
    def call(self, path, body=None, token=None, headers=None):
        h = dict(headers or {})
        if token:
            h["Authorization"] = f"Bearer {token}"
        return sp.request("POST", path, body or {}, origin="", headers=h)

    def setUp(self):
        super().setUp()
        self.hub = runners.Hub()
        patcher = mock.patch.object(runners, "HUB", self.hub)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.pid, self.tok = self.join()
        self.rid, self.runner = runners.create(self.root, self.pid)

    def test_no_token_no_access(self):
        for path in ("/api/runner/next", "/api/runner/events/x", "/api/runner/os/mcp"):
            self.assertEqual(self.call(path)[0], 403)
            self.assertEqual(self.call(path, token="myr_wrong")[0], 403)

    def test_next_returns_a_job_for_its_person(self):
        with mock.patch.object(self.hub, "next", return_value={"turn": "t1"}) as nx:
            code, data, _ = self.call("/api/runner/next", token=self.runner)
        self.assertEqual((code, data["job"]["turn"]), (200, "t1"))
        self.assertEqual(nx.call_args[0], (self.rid, self.pid))

    def test_removed_person_loses_the_runner(self):
        people.remove(self.root, "acme", self.pid)
        self.assertEqual(self.call("/api/runner/next", token=self.runner)[0], 403)

    def test_os_relay_needs_both_tokens_for_the_same_person(self):
        frame = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        good = server.os_relay.TOKENS.mint({"ws": "acme", "readonly": False, "runner_owner": self.pid})
        other = server.os_relay.TOKENS.mint({"ws": "acme", "readonly": False, "runner_owner": "someone-else"})
        plain = server.os_relay.TOKENS.mint({"ws": "acme", "readonly": False})
        for bearer, code in ((good, 200), (other, 403), (plain, 403), ("nope", 403)):
            got = self.call("/api/runner/os/mcp", frame, token=bearer, headers={"X-Runner-Token": self.runner})
            self.assertEqual(got[0], code)
        self.assertEqual(self.call("/api/runner/os/mcp", frame, token=good)[0], 403)  # no runner token

    def test_local_relay_stays_local_only(self):
        good = server.os_relay.TOKENS.mint({"ws": "acme", "readonly": False, "runner_owner": self.pid})
        code = sp.request("POST", "/api/internal/os/mcp", {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                          origin="", host=sp.PUBLIC, headers={"Authorization": f"Bearer {good}"})[0]
        self.assertEqual(code, 403)

    def test_member_manages_only_their_own_runners(self):
        code, data, _ = sp.request("POST", "/api/runners", {"name": "Air"}, member=self.tok)
        self.assertEqual(code, 200)
        self.assertTrue(data["token"].startswith("myr_"))
        self.assertEqual(len(data["runners"]), 2)
        code, _, _ = sp.request("POST", "/api/runners/revoke", {"id": "rn-unknown"}, member=self.tok)
        self.assertEqual(code, 404)
        owner_rid, _ = runners.create(self.root, "owner")
        self.assertEqual(sp.request("POST", "/api/runners/revoke", {"id": owner_rid}, member=self.tok)[0], 404)

    def test_member_turn_goes_to_their_runner_when_online_and_is_locked_down(self):
        seen = {}
        self.hub.next(self.rid, self.pid, wait=0)
        body = {"messages": [{"role": "user", "content": "hello"}], "room_id": "acme/general"}
        with mock.patch.object(server, "person_chat_session", return_value=sp.fake_session(seen)):
            code, raw, _ = sp.request("POST", "/api/ask", body, member=self.tok)
        self.assertEqual(code, 200)
        self.assertTrue(seen["shared"])
        self.assertTrue(seen["live"])

    def test_owner_private_turn_on_a_runner_is_locked_down_too(self):
        seen = {}
        self.hub.next("r0", "owner", wait=0)
        body = {"messages": [{"role": "user", "content": "hello"}], "room_id": "acme/dm-cos"}
        with mock.patch.object(server, "get_chat_session", return_value=sp.fake_session(seen)):
            sp.request("POST", "/api/ask", body, owner=True)
        self.assertTrue(seen["shared"])
        self.assertIsNotNone(seen["kw"].get("os_token"))


if __name__ == "__main__":
    unittest.main()
