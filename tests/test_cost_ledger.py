"""Harness upgrade phase 1: token parts, per-reply ledger fields, caps wiring, cost report."""
import io
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from dashboard import chat_session
from dashboard.engines import api, claude_code, codex, prices
import dashboard.server as server
from tests.test_teammates import VENTURE

USAGE = {"in": 100, "cache_write": 200, "cache_read": 1000, "out": 0}


def env(**kw):
    return mock.patch.dict(os.environ, kw)


class EngineUsage(unittest.TestCase):
    def test_claude_code_parts(self):
        _, p = claude_code.parse_event({"type": "result", "total_cost_usd": 0.01, "usage": {
            "input_tokens": 10, "cache_creation_input_tokens": 200,
            "cache_read_input_tokens": 1000, "output_tokens": 90}})
        self.assertEqual(p["usage"], {"in": 10, "cache_write": 200, "cache_read": 1000, "out": 90})
        self.assertEqual(p["tokens"], 1300)

    def test_codex_cached_split(self):
        (_, p), = codex.parse_event({"type": "turn.completed", "usage": {
            "input_tokens": 100, "cached_input_tokens": 60, "output_tokens": 5}})
        self.assertEqual(p["usage"], {"in": 40, "cache_write": 0, "cache_read": 60, "out": 5})
        self.assertEqual(p["tokens"], 105)

    def test_openai_compatible_cached_split(self):
        eng = api.ApiEngine.__new__(api.ApiEngine)
        _, p = eng._done(["x"], {"prompt_tokens": 50, "completion_tokens": 7, "cached_tokens": 20}, "some-model")
        self.assertEqual(p["usage"], {"in": 30, "cache_write": 0, "cache_read": 20, "out": 7})
        self.assertEqual(p["tokens"], 57)

    def test_anthropic_api_parts_and_estimate(self):
        eng = api.ApiEngine.__new__(api.ApiEngine)
        _, p = eng._done(["x"], {"input_tokens": 1_000_000, "output_tokens": 0}, "claude-haiku-5-5")
        self.assertEqual(p["usage"], {"in": 1_000_000, "cache_write": 0, "cache_read": 0, "out": 0})
        self.assertAlmostEqual(p["cost"], 0.10)

    def test_estimate(self):
        u = {"in": 1_000_000, "cache_write": 0, "cache_read": 0, "out": 0}
        self.assertAlmostEqual(prices.estimate("claude-haiku-5-5", u), 0.10)
        self.assertIsNone(prices.estimate("unknown-model", u))
        self.assertIsNone(prices.estimate("claude-haiku-5-5", None))
        self.assertIs(api.PRICES, prices.PRICES)



class AskHarness(unittest.TestCase):
    """One chat turn through the real ask path with fake engine sessions.
    `replies`: one string, or a list with one reply per engine run in order
    (lead, teammate, follow-up). `engine_calls` counts runs that started."""

    SID = "55555555-0000-0000-0000-000000000000"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "projects" / "acme").mkdir(parents=True)
        self._chats = server.chat_store.CHATS_DIR
        server.chat_store.CHATS_DIR = self.root / "chats"
        self.engine_calls = 0

    def tearDown(self):
        server.chat_store.CHATS_DIR = self._chats
        self.tmp.cleanup()

    def seed_spend(self, owner, ws):
        server.chat_store.save({"id": "66666666-0000-0000-0000-000000000000", "title": "earlier",
                                "created": time.time(), "messages": [],
                                "spend": [{"ts": time.time(), "by": "owner", "cost": owner, "ws": ws}]})

    def ask(self, msg, replies, *, cost=0.02, usage=USAGE, done=None):
        queue = [replies] if isinstance(replies, str) else list(replies)
        payload = done if done is not None else {"tokens": 1300, "cost": cost, "usage": usage}
        harness = self

        def stream():
            harness.engine_calls += 1
            text = queue.pop(0) if queue else ""
            yield ("delta", text)
            yield ("done", dict(payload))

        class Lead:
            _started = True
            _turn_count = 1
            _last_skill = None
            _skill_explicit = False
            _last_bot = None
            session_id = self.SID

            def is_fresh(self):
                return False

            def note_skill(self, *a, **k):
                pass

            def note_bot(self, *a):
                pass

            def ask(self, text, **kw):
                yield from stream()

        class Mate:
            def __init__(self, repo_dir, rail):
                self.readonly = False

            def ask(self, text, **kw):
                yield from stream()

        sink = io.BytesIO()
        h = server.Handler.__new__(server.Handler)
        h.wfile = sink
        h.send_response = h.send_header = h.end_headers = lambda *a, **k: None
        with mock.patch.object(server, "ROOT", self.root), \
                mock.patch.object(server.db, "tree", return_value=[VENTURE]), \
                mock.patch.object(server.db, "posts", return_value=[]), \
                mock.patch.object(server, "state_snapshot", side_effect=lambda p, only=None: "SNAP"), \
                mock.patch.object(server, "get_chat_session", return_value=Lead()), \
                mock.patch.object(server.chat_session, "ChatSession", Mate):
            h._handle_ask({"messages": [{"role": "user", "content": msg}],
                           "room_id": "acme/general", "chat_id": self.SID})
        return sink.getvalue().decode(), server.chat_store.load(self.SID)


class LedgerFields(AskHarness):
    def test_reason_codes(self):
        self.assertEqual(server._pick_model_reason(None, False, "what is our plan?"),
                         (chat_session.CHAT_MODEL, "default"))
        self.assertEqual(server._pick_model_reason(None, True, "x")[1], "web")
        self.assertEqual(server._pick_model_reason("strategy", False, "draft it", explicit=True)[1],
                         "explicit-skill")
        self.assertEqual(server._pick_model_reason("tokensave", False, "x")[1], "tokensave")
        self.assertEqual(server._pick_model("strategy", False, "draft it", explicit=True),
                         chat_session.ESCALATED_MODEL)

    def test_reply_records_usage_skill_reason(self):
        _, rec = self.ask("@cmo positioning for acme", "ok")
        last = rec["messages"][-1]
        self.assertEqual(last["usage"]["cache_read"], 1000)
        self.assertEqual(last["tokens"], 1300)
        self.assertTrue(last["metered"])
        self.assertIn(last["reason"], {"explicit-skill", "explicit-read", "default"})
        self.assertIn("skill", last)
        self.assertEqual(rec["spend"][-1]["ws"], "acme")

    def test_cost_estimated_when_engine_reports_none(self):
        with mock.patch.object(claude_code.ClaudeCodeEngine, "resolve_model", return_value="claude-haiku-5-5"):
            _, rec = self.ask("@cmo positioning for acme", "ok", done={
                "tokens": 1_000_000, "cost": None,
                "usage": {"in": 1_000_000, "cache_write": 0, "cache_read": 0, "out": 0}})
        last = rec["messages"][-1]
        self.assertAlmostEqual(last["cost"], 0.10)
        self.assertTrue(last["metered"])

    def test_unpriced_reply_is_unmetered(self):
        _, rec = self.ask("@cmo positioning for acme", "ok", done={"tokens": 50, "cost": None, "usage": None})
        self.assertFalse(rec["messages"][-1]["metered"])
        self.assertNotIn("spend", rec)

    def test_teammate_reply_carries_its_own_reason(self):
        _, rec = self.ask("@cmo plan for acme", ["Lead.\nCONSULT @research: rivals?", "Mate.", "Done."])
        mate = [m for m in rec["messages"] if m.get("bot") == "research"][0]
        self.assertIn(mate["reason"], {"teammate", "teammate-skill"})
        self.assertEqual(rec["messages"][-1]["reason"], rec["messages"][1]["reason"])


if __name__ == "__main__":
    unittest.main()
