"""The engine layer: interface rules and the claude-code driver behind ChatSession."""
import os
import stat
import tempfile
import unittest
from pathlib import Path

import dashboard.chat_session as cs
from dashboard.engines.base import Engine, EngineRefused, SessionLost, TurnPolicy
from dashboard.engines.claude_code import ClaudeCodeEngine


class FakeEngine(Engine):
    id = "fake"
    label = "Fake"

    def __init__(self, events):
        self.events = events
        self.prompts = []

    def turn(self, session, text, policy, model, tokens):
        self.prompts.append(text)
        session.engine_state[self.id] = {}
        event = self.events.pop(0)
        if isinstance(event, Exception):
            raise event
        yield from event


class Interface(unittest.TestCase):
    def test_shared_turn_refused_on_unsafe_engine(self):
        with self.assertRaises(EngineRefused):
            FakeEngine([]).check(TurnPolicy(shared=True))

    def test_apps_turn_refused_without_apps_support(self):
        with self.assertRaises(EngineRefused):
            FakeEngine([]).check(TurnPolicy(apps=True))

    def test_fresh_is_per_engine(self):
        sess = cs.ChatSession(repo_dir=".", rail="R")
        sess.engine = FakeEngine([[("done", {})]])
        self.assertTrue(sess.is_fresh())
        list(sess.ask("hi"))
        self.assertFalse(sess.is_fresh())
        sess.engine = FakeEngine([])
        sess.engine.id = "other"
        self.assertTrue(sess.is_fresh())

    def test_engine_runs_the_turn_and_bookkeeping_follows(self):
        sess = cs.ChatSession(repo_dir=".", rail="R")
        sess.engine = FakeEngine([[("delta", "x"), ("done", {})]])
        sess.note_bot("cmo")
        self.assertEqual(list(sess.ask("hi"))[0], ("delta", "x"))
        self.assertEqual((sess._turn_count, sess._last_bot), (1, "cmo"))

    def test_failed_turn_keeps_no_bookkeeping(self):
        sess = cs.ChatSession(repo_dir=".", rail="R")
        sess.engine = FakeEngine([[("error", "boom")]])
        sess.note_bot("cmo")
        list(sess.ask("hi"))
        self.assertEqual((sess._turn_count, sess._last_bot), (0, None))

    def test_lost_session_retries_once_with_the_recap(self):
        sess = cs.ChatSession(repo_dir=".", rail="R")
        sess.recap = "RECAP"
        sess.engine = FakeEngine([SessionLost("gone"), [("delta", "ok"), ("done", {})]])
        self.assertEqual(list(sess.ask("hi"))[0], ("delta", "ok"))
        self.assertEqual(sess.engine.prompts, ["hi", "RECAP\n\nhi"])


class TypedHosts(unittest.TestCase):
    def test_hosts_found_and_lookalikes_skipped(self):
        got = cs.user_web_domains("see www.acme.example and https://docs.example/x, notes.md, me@example.com, a/b.example")
        self.assertEqual(got, ["acme.example", "docs.example"])

    def test_long_dash_run_stays_fast(self):
        import time
        start = time.time()
        cs.user_web_domains("-" * 20000 + " a" + "-" * 19000)
        self.assertLess(time.time() - start, 0.5)


class ClaudeDriver(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(cs.remove_bot_tmpdir)

    def stub(self, script):
        path = Path(self.tmp.name) / "claude"
        path.write_text("#!/bin/sh\n" + script)
        path.chmod(path.stat().st_mode | stat.S_IEXEC)
        return str(path)

    def test_missing_conversation_becomes_session_lost(self):
        bin_ = self.stub('echo "No conversation found with session ID: x" >&2\nexit 1\n')
        sess = cs.ChatSession(repo_dir=self.tmp.name, rail="R", claude_bin=bin_)
        sess._started = True
        with self.assertRaises(SessionLost):
            list(sess.engine.turn(sess, "hi", sess._policy(), "haiku", cs.TurnTokens()))
        self.assertFalse(sess._started)

    def test_other_failures_stay_errors(self):
        bin_ = self.stub('echo "boom" >&2\nexit 1\n')
        sess = cs.ChatSession(repo_dir=self.tmp.name, rail="R", claude_bin=bin_)
        sess._started = True
        self.assertEqual(list(sess.ask("hi"))[-1][0], "error")

    def test_logged_out_says_what_to_do(self):
        out = '{"type":"result","subtype":"success","is_error":true,"result":"Not logged in \\u00b7 Please run /login"}'
        bin_ = self.stub(f"cat <<'X'\n{out}\nX\nexit 1\n")
        sess = cs.ChatSession(repo_dir=self.tmp.name, rail="R", claude_bin=bin_)
        last = list(sess.ask("hi"))[-1]
        self.assertEqual(last[0], "error")
        self.assertIn("claude auth login", last[1])

    def test_turn_env_names_the_engine_and_is_never_inherited(self):
        os.environ["LEOBOT_ENGINE"] = "api:leak"
        self.addCleanup(os.environ.pop, "LEOBOT_ENGINE", None)
        env = ClaudeCodeEngine().turn_env(TurnPolicy(), cs.TurnTokens())
        self.assertEqual(env["LEOBOT_ENGINE"], "claude-code")

    def test_status_when_not_installed(self):
        row = ClaudeCodeEngine("no-such-binary-xyz").status()
        self.assertFalse(row["installed"])
        self.assertFalse(row["ok"])


if __name__ == "__main__":
    unittest.main()
