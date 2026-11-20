"""Stop everything (core/halt.py), the action log (core/action_log.py) and the new-destination
rule's reader (core/destinations.py)."""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import dashboard.server as server
from core import action_log, destinations, halt, jobs
from dashboard import job_runner, taint
from tests import test_browser_api as api


class Halt(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "chats").mkdir()
        for p in (mock.patch.object(server, "ROOT", self.root),
                  mock.patch.object(server.chat_store, "CHATS_DIR", self.root / "chats"),
                  mock.patch.object(server.db, "db_exists", return_value=True),
                  mock.patch.object(server.db, "tree", return_value=api.PROJECTS)):
            p.start()
            self.addCleanup(p.stop)

    def test_flag_survives_and_resume_clears(self):
        self.assertIsNone(halt.state(self.root))
        halt.halt(self.root, by="cli")
        self.assertEqual(halt.state(self.root)["by"], "cli")
        halt.halt(self.root, by="owner")  # a second press keeps the first record
        self.assertEqual(halt.state(self.root)["by"], "cli")
        halt.resume(self.root)
        self.assertFalse(halt.halted(self.root))

    def test_unreadable_flag_counts_as_stopped(self):
        (self.root / "team").mkdir()
        (self.root / "team" / "halt.json").write_text("{not json")
        self.assertTrue(halt.halted(self.root))

    def test_owner_only_with_origin(self):
        self.assertEqual(api.request("POST", "/api/halt", {}, origin="")[0], 403)
        self.assertEqual(api.request("POST", "/api/halt", {}, owner=False)[0], 403)
        self.assertFalse(halt.halted(self.root))

    def test_stop_everything_ends_runs_and_refuses_relays(self):
        sess = mock.Mock()
        live_token = server.apps_relay.TOKENS.mint({"ws": "acme", "bot": "cos"})
        gate = taint.GATES.mint(taint.Turn())
        with mock.patch.object(server, "_CHAT", sess), mock.patch.object(server.runners.HUB, "drop_all") as drop:
            code, data = api.request("POST", "/api/halt", {})
        self.assertEqual(code, 200)
        self.assertTrue(data["halted"])
        sess.close.assert_called_once()
        drop.assert_called_once()
        self.assertIsNone(server.apps_relay.TOKENS.lookup(live_token))
        self.assertIsNone(taint.GATES.lookup(gate))
        # A tool call on its way, even on a token minted after the stop, is refused.
        code, data = api.request("POST", server.APPS_RELAY_PATH, {"jsonrpc": "2.0", "id": 1, "method": "ping"},
                                 owner=False, origin="")
        self.assertEqual(code, 423)
        code, data = api.request("POST", taint.GATE_PATH, {"tool": "Bash", "command": "ls"}, owner=False, origin="")
        self.assertEqual((code, data["allow"]), (200, False))
        self.assertEqual([r["kind"] for r in action_log.entries(self.root)], ["stop_everything"])
        self.assertEqual(api.request("GET", "/api/halt")[1]["halted"]["by"], "owner")
        code, _ = api.request("POST", "/api/resume", {})
        self.assertEqual(code, 200)
        self.assertFalse(halt.halted(self.root))
        self.assertEqual([r["kind"] for r in action_log.entries(self.root)], ["stop_everything", "resume"])

    def test_no_turn_starts_while_stopped(self):
        halt.halt(self.root)
        h = server.Handler.__new__(server.Handler)
        sent = []
        h._sse_message = sent.append
        h._ask_turn({"messages": [{"role": "user", "content": "hi"}], "room_id": "hq/general"},
                    {"kind": "owner"})
        self.assertEqual(sent, [halt.MESSAGE])

    def test_jobs_do_not_run_while_stopped(self):
        ran = []
        runner = job_runner.Runner(self.root, run_turn=lambda job: ran.append(job) or {"status": "done"})
        with mock.patch.object(jobs, "load", return_value=[{"id": "jb1"}]), \
                mock.patch.object(jobs, "is_due", return_value=True), \
                mock.patch.object(jobs, "next_due", return_value=0):
            halt.halt(self.root)
            self.assertEqual(runner.tick(), [])
        self.assertEqual(ran, [])

    def test_cli_flag_is_picked_up_by_the_watcher(self):
        calls = []
        steps = iter([lambda: None, lambda: halt.halt(self.root, by="cli"), lambda: None])

        def sleep(_):
            try:
                next(steps)()
            except StopIteration:
                raise KeyboardInterrupt from None
        with mock.patch.object(server, "stop_everything", side_effect=calls.append), \
                mock.patch.object(server.time, "sleep", side_effect=sleep):
            with self.assertRaises(KeyboardInterrupt):
                server._halt_watch(every=0)
        self.assertEqual(calls, ["cli"])


class ActionLog(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_chain_finds_a_removed_or_changed_line(self):
        for i in range(3):
            action_log.record(self.root, "app_run", n=i, arguments={"to": "a@example.com"})
        self.assertEqual(action_log.verify(self.root), (True, 3, None))
        f = next((self.root / "team" / "actions").glob("*.jsonl"))
        lines = f.read_text().splitlines()
        f.write_text("\n".join([lines[0], lines[2]]) + "\n")
        ok, n, where = action_log.verify(self.root)
        self.assertFalse(ok)
        self.assertIn("removed or changed", where)
        f.write_text("\n".join([lines[0], lines[1].replace('"n": 1', '"n": 7'), lines[2]]) + "\n")
        self.assertIn("this line was changed", action_log.verify(self.root)[2])

    def test_files_are_private_and_long_arguments_cut(self):
        action_log.record(self.root, "app_run", arguments={"body": "x" * 10000})
        d = self.root / "team" / "actions"
        self.assertEqual(d.stat().st_mode & 0o777, 0o700)
        f = next(d.glob("*.jsonl"))
        self.assertEqual(f.stat().st_mode & 0o777, 0o600)
        self.assertLess(len(f.read_text()), action_log.MAX_ARGS + 500)

    def test_shell_commands_a_gate_allows_are_logged(self):
        turn = taint.Turn(who={"ws": "acme", "chat": "c1"})
        with mock.patch.object(action_log, "record") as rec:
            allow, _, _ = turn.shell("ls", writable=[str(self.root)], cwd=str(self.root))
        self.assertTrue(allow)
        rec.assert_called_once()
        self.assertEqual(rec.call_args.kwargs["command"], "ls")
        self.assertEqual(rec.call_args.kwargs["ws"], "acme")


class Destinations(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_reads_addresses_hosts_phones_and_recipient_fields(self):
        got = destinations.of_arguments({
            "recipient_email": "Someone <A@Example.com>", "cc": ["b@example.com"], "channel": "C0123",
            "webhook_url": "https://hooks.example/x", "phone": "+39 333 123 4567",
            "body": "mail c@example.org at https://site.example", "start": "2026-11-20T10:00"})
        self.assertEqual(got, {"email:a@example.com", "email:b@example.com", "to:c0123",
                               "host:hooks.example", "phone:393331234567"})

    def test_unreadable_arguments_count_as_new(self):
        deep = cur = {}
        for _ in range(20):
            cur["x"] = {}
            cur = cur["x"]
        self.assertIsNone(destinations.of_arguments(deep))
        self.assertEqual(destinations.unknown("acme", None, self.root), {"?"})

    def test_known_host_covers_subdomains_only(self):
        destinations.learn("acme", {"host:example.com"}, self.root)
        self.assertEqual(destinations.unknown("acme", {"host:mail.example.com"}, self.root), set())
        self.assertEqual(destinations.unknown("acme", {"host:evil-example.com"}, self.root), {"host:evil-example.com"})
        self.assertEqual(destinations.unknown("other", {"host:example.com"}, self.root), {"host:example.com"})
        destinations.forget_workspace("acme", self.root)
        self.assertEqual(destinations.unknown("acme", {"host:example.com"}, self.root), {"host:example.com"})


if __name__ == "__main__":
    unittest.main()
