"""The engine layer: interface rules and the claude-code driver behind ChatSession."""
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import dashboard.chat_session as cs
from dashboard.engines.base import Engine, EngineRefused, SessionLost, TurnPolicy
from dashboard.engines import claude_code, prices
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

    def test_claude_code_done_carries_last_context(self):
        def start(n):
            return json.dumps({"type": "stream_event", "event": {"type": "message_start", "message": {
                "usage": {"input_tokens": n, "cache_read_input_tokens": 0}}}})
        result = json.dumps({"type": "result", "subtype": "success", "result": "ok", "usage": {}})
        bin_ = self.stub(f"cat <<'X'\n{start(1000)}\n{start(3000)}\n{result}\nX\n")
        sess = cs.ChatSession(repo_dir=self.tmp.name, rail="R", claude_bin=bin_)
        ev = list(sess.ask("hi"))
        self.assertNotIn("context", [k for k, _ in ev])
        self.assertEqual(ev[-1][0], "done")
        self.assertEqual(ev[-1][1]["context"], 3000)

    def test_claude_code_reports_spend_after_each_call(self):
        def start(n):
            return json.dumps({"type": "stream_event", "event": {"type": "message_start", "message": {
                "usage": {"input_tokens": n, "cache_read_input_tokens": 100}}}})
        delta = json.dumps({"type": "stream_event", "event": {"type": "message_delta", "usage": {"output_tokens": 20}}})
        result = json.dumps({"type": "result", "subtype": "success", "result": "ok", "usage": {}})
        bin_ = self.stub(f"cat <<'X'\n{start(1000)}\n{delta}\n{start(3000)}\n{delta}\n{result}\nX\n")
        sess = cs.ChatSession(repo_dir=self.tmp.name, rail="R", claude_bin=bin_)
        spent = [p for k, p in sess.ask("hi", model="sonnet") if k == "spent"]
        self.assertEqual([(p["usage"]["in"], p["usage"]["cache_read"], p["usage"]["out"]) for p in spent],
                         [(1000, 100, 20), (4000, 200, 40)])  # this run so far, call by call
        # Priced by the tier asked for (the CLI takes "sonnet"): never 0 for a priced model.
        one = prices.estimate("claude-sonnet-5-5", prices.usage(1000, 0, 100, 20))
        self.assertAlmostEqual(spent[0]["cost"], one)
        self.assertAlmostEqual(spent[1]["cost"], one + prices.estimate("claude-sonnet-5-5", prices.usage(3000, 0, 100, 20)))

    def test_claude_code_prices_each_call_by_the_model_it_names(self):
        run = {}
        start = {"type": "stream_event", "event": {"type": "message_start", "message": {
            "model": "claude-opus-5-5", "usage": {"input_tokens": 1000}}}}
        delta = {"type": "stream_event", "event": {"type": "message_delta", "usage": {"output_tokens": 10}}}
        self.assertFalse(claude_code.call_usage(start, run, "haiku"))
        self.assertTrue(claude_code.call_usage(delta, run, "haiku"))
        self.assertAlmostEqual(run["cost"], prices.estimate("claude-opus-5-5", prices.usage(1000, 0, 0, 10)))

    def test_claude_code_hour_cache_writes_cost_twice_input(self):
        # Usage from a real Claude Code run (2.1.322): the CLI reported $0.00063971 for these two calls.
        def call(fresh, write, read, out):
            return [{"type": "stream_event", "event": {"type": "message_start", "message": {
                        "model": "claude-haiku-5-5", "usage": {
                            "input_tokens": fresh, "cache_creation_input_tokens": write,
                            "cache_read_input_tokens": read,
                            "cache_creation": {"ephemeral_5m_input_tokens": 0, "ephemeral_1h_input_tokens": write}}}}},
                    {"type": "stream_event", "event": {"type": "message_delta", "usage": {"output_tokens": out}}}]
        run = {}
        for obj in call(2, 2017, 6582, 54) + call(2, 183, 8599, 41):
            claude_code.call_usage(obj, run, "haiku")
        self.assertAlmostEqual(run["cost"], 0.00063971, places=8)
        self.assertAlmostEqual(prices.estimate("claude-haiku-5-5", {"cache_write": 10, "cache_write_1h": 99}),
                               10 * 0.10 * 2 / 1_000_000)  # never more hour writes than writes

    def test_claude_code_compact_starts_new_session_id(self):
        sess = cs.ChatSession(repo_dir=self.tmp.name, rail="R")
        sess._started = True
        self.assertIn(sess.session_id, sess._base_cmd())
        self.assertTrue(sess.engine.compact(sess))
        cmd = sess._base_cmd()
        new = sess.engine_state["claude-code"]["session"]
        self.assertNotEqual(new, sess.session_id)
        self.assertEqual(cmd[cmd.index("--session-id") + 1], new)
        self.assertNotIn("--resume", cmd)
        self.assertNotIn(sess.session_id, cmd)
        sess._started = True
        cmd = sess._base_cmd()
        self.assertEqual(cmd[cmd.index("--resume") + 1], new)

    def test_reopened_chat_resumes_the_compacted_session(self):
        from dashboard import chat_store
        sess = cs.ChatSession(repo_dir=self.tmp.name, rail="R")
        sess.engine.compact(sess)
        new = sess.engine_state["claude-code"]["session"]
        sess._started = True
        with mock.patch.object(chat_store, "CHATS_DIR", Path(self.tmp.name) / "chats"):
            chat_store.record_turn(sess, None, "hi", "ok", bot="cos")
            back = cs.ChatSession.restore(self.tmp.name, "R", chat_store.load(sess.session_id))
        cmd = back._base_cmd()
        self.assertEqual(cmd[cmd.index("--resume") + 1], new)

    def test_claude_session_id_must_look_like_one(self):
        sess = cs.ChatSession(repo_dir=self.tmp.name, rail="R")
        sess._started = True
        sess.engine_state["claude-code"] = {"session": "--dangerously-skip-permissions"}
        cmd = sess._base_cmd()
        self.assertEqual(cmd[cmd.index("--resume") + 1], sess.session_id)
        self.assertNotIn("--dangerously-skip-permissions", cmd)

    def test_runner_and_api_never_compact(self):
        from dashboard.engines.api import ApiEngine
        from dashboard.engines.runner import RunnerEngine
        sess = cs.ChatSession(repo_dir=self.tmp.name, rail="R")
        for eng in (RunnerEngine("owner", hub=object()),
                    ApiEngine({"id": "demo", "format": "anthropic", "base_url": "http://127.0.0.1:9"}, self.tmp.name)):
            sess.engine_state = {eng.id: {"cut": 3}}
            self.assertFalse(eng.compact(sess))
            self.assertEqual(sess.engine_state, {eng.id: {"cut": 3}})

    def test_base_compact_drops_the_engines_state(self):
        class Plain(Engine):
            id = "plain"
        sess = cs.ChatSession(repo_dir=self.tmp.name, rail="R")
        sess.engine_state = {"plain": {"session": "s1"}, "other": {"session": "s2"}}
        self.assertTrue(Plain().compact(sess))
        self.assertEqual(sess.engine_state, {"other": {"session": "s2"}})

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

    def test_turn_env_carries_the_thread_and_job(self):
        with mock.patch.dict(os.environ, {"LEOBOT_JOB": "leak", "LEOBOT_READONLY": "1"}):
            env = ClaudeCodeEngine().turn_env(TurnPolicy(chat="c1", job="j1", job_mode="auto"), cs.TurnTokens())
            plain = ClaudeCodeEngine().turn_env(TurnPolicy(chat="c2"), cs.TurnTokens())
        self.assertEqual((env["LEOBOT_CHAT"], env["LEOBOT_JOB"], env["LEOBOT_JOB_MODE"]), ("c1", "j1", "auto"))
        self.assertNotIn("LEOBOT_READONLY", env)
        self.assertEqual(plain["LEOBOT_CHAT"], "c2")
        self.assertNotIn("LEOBOT_JOB", plain)
        self.assertNotIn("LEOBOT_JOB_MODE", plain)

    def test_status_when_not_installed(self):
        row = ClaudeCodeEngine("no-such-binary-xyz").status()
        self.assertFalse(row["installed"])
        self.assertFalse(row["ok"])


class Metered(unittest.TestCase):
    def test_metered(self):
        from dashboard.engines import registry
        from dashboard.engines.api import ApiEngine
        from dashboard.engines.claude_code import ClaudeCodeEngine
        from dashboard.engines.codex import CodexEngine
        self.assertTrue(registry.metered(ClaudeCodeEngine()))
        priced = {"id": "x", "models": {"fast": "claude-haiku-5-5", "strong": "claude-sonnet-5-5"}}
        self.assertTrue(registry.metered(ApiEngine(priced, "/nonexistent")))
        # Tokens on every run: an unpriced model is counted at the costliest price, never skipped.
        half = {"id": "y", "models": {"fast": "claude-haiku-5-5", "strong": "some-other-model"}}
        self.assertTrue(registry.metered(ApiEngine(half, "/nonexistent")))
        self.assertTrue(registry.metered(CodexEngine()))
        from dashboard.engines.acp import AcpEngine  # no usage reported: still no jobs
        self.assertFalse(registry.metered(AcpEngine({"id": "a", "name": "A", "command": "a", "args": []})))

    def test_unknown_model_priced_as_costliest(self):
        from dashboard.engines import prices
        parts = prices.usage(1_000_000, 0, 0, 1_000_000)
        self.assertIsNone(prices.estimate("some-other-model", parts))
        top = max(prices.estimate(m, parts) for m in prices.PRICES)
        self.assertEqual(prices.estimate("some-other-model", parts, unknown_ok=True), top)
        self.assertEqual(prices.estimate(None, parts, unknown_ok=True), top)
        self.assertEqual(prices.estimate("claude-haiku-5-5", parts, unknown_ok=True),
                         prices.estimate("claude-haiku-5-5", parts))


if __name__ == "__main__":
    unittest.main()
