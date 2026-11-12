"""Thread compaction: the threshold and the last reply's context size."""
import io
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import dashboard.server as server
from dashboard import compaction
from dashboard.engines.base import Engine
from tests.test_teammates import VENTURE


def rec(*replies):
    return {"messages": [{"role": "user", "content": "q"}] + [
        {"role": "assistant", "content": "a", **r} for r in replies]}


class Threshold(unittest.TestCase):
    def test_default_and_bad_values(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("LEOBOT_COMPACT_TOKENS", None)
            self.assertEqual(compaction.threshold(), 60000)
        for raw, want in (("0", 0), ("abc", 0), ("-5", 0), ("1000", 1000)):
            with mock.patch.dict(os.environ, {"LEOBOT_COMPACT_TOKENS": raw}):
                self.assertEqual(compaction.threshold(), want, raw)


class LastContext(unittest.TestCase):
    def test_newest_lead_reply_on_that_engine(self):
        r = rec({"engine": "claude-code", "context": 10}, {"engine": "claude-code", "context": 20},
                {"engine": "api:x", "context": 99}, {"engine": "claude-code", "context": 500, "asked_by": "cmo"})
        self.assertEqual(compaction.last_context(r, "claude-code"), 20)
        self.assertEqual(compaction.last_context(r, "api:x"), 99)

    def test_none_without_context(self):
        self.assertIsNone(compaction.last_context(rec({"engine": "claude-code"}), "claude-code"))
        self.assertIsNone(compaction.last_context(rec({"engine": "claude-code", "context": "9"}), "claude-code"))
        self.assertIsNone(compaction.last_context(rec({"engine": "claude-code", "context": True}), "claude-code"))
        self.assertIsNone(compaction.last_context(None, "claude-code"))
        self.assertIsNone(compaction.last_context(rec(), "claude-code"))

    def test_due(self):
        os.environ.pop("LEOBOT_COMPACT_TOKENS", None)
        self.assertFalse(compaction.due(rec({"engine": "e", "context": 59999}), "e"))
        self.assertTrue(compaction.due(rec({"engine": "e", "context": 60000}), "e"))
        with mock.patch.dict(os.environ, {"LEOBOT_COMPACT_TOKENS": "0"}):
            self.assertFalse(compaction.due(rec({"engine": "e", "context": 10**7}), "e"))


class FakeEngine(Engine):
    id = "fake-eng"
    label = "Fake"
    kind = "cli"
    shared_safe = True
    supports = frozenset({"web", "apps", "folder_edit"})

    def __init__(self, can=True):
        self.can, self.compacted = can, 0

    def compact(self, session):
        self.compacted += 1
        return super().compact(session) if self.can else False


class ServerCompaction(unittest.TestCase):
    SID = "77777777-0000-0000-0000-000000000000"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "projects" / "acme").mkdir(parents=True)
        os.environ.pop("LEOBOT_COMPACT_TOKENS", None)

    def turn(self, replies, engine=None, msg="what did we decide earlier?"):
        """One untagged turn in a saved thread whose replies are `replies` (extra fields)."""
        engine = engine or FakeEngine()
        seen = {}
        with mock.patch.object(server.chat_store, "CHATS_DIR", self.root / "chats"):
            server.chat_store.save({"id": self.SID, "title": "t", "created": time.time(), "messages": [
                {"role": "user", "content": "we pick the blue plan"},
                *[{"role": "assistant", "bot": "cos", "content": f"noted {i}", **r} for i, r in enumerate(replies)]],
                "engines": {engine.id: {"session": "s1", "seen": 1 + len(replies)}}})

            class Lead:
                session_id = self.SID
                _started = True
                _turn_count = 3
                _last_skill = None
                _skill_explicit = False
                _last_bot = "cos"
                engine_state = {engine.id: {"session": "s1"}}

                def is_fresh(self):
                    return self.engine.is_fresh(self)

                def note_skill(self, *a, **k):
                    pass

                def note_bot(self, *a):
                    pass

                def ask(self, text, **kw):
                    seen["prompt"] = text
                    yield ("delta", "ok")
                    yield ("done", {"result": "ok"})

            h = server.Handler.__new__(server.Handler)
            h.wfile = io.BytesIO()
            h.send_response = h.send_header = h.end_headers = lambda *a, **k: None
            h.log_message = lambda *a, **k: None
            h.server = mock.Mock(server_address=("127.0.0.1", 8765))
            with mock.patch.object(server, "ROOT", self.root), \
                    mock.patch.object(server.db, "tree", return_value=[VENTURE]), \
                    mock.patch.object(server.db, "posts", return_value=[]), \
                    mock.patch.object(server, "state_snapshot", side_effect=lambda p, only=None: "SNAPSHOT-BLOCK"), \
                    mock.patch.object(server, "get_chat_session", return_value=Lead()), \
                    mock.patch.object(server.engine_registry, "pick", return_value=engine), \
                    mock.patch.object(server.runners.HUB, "online", return_value=False):
                h._handle_ask({"messages": [{"role": "user", "content": msg}],
                               "room_id": "acme/general", "chat_id": self.SID})
        return engine, seen.get("prompt", "")

    def test_compaction_sends_recap_and_snapshot(self):
        engine, prompt = self.turn([{"engine": "fake-eng", "context": 70000}])
        self.assertEqual(engine.compacted, 1)
        self.assertIn("## Earlier in this thread", prompt)
        self.assertIn("we pick the blue plan", prompt)
        self.assertIn("SNAPSHOT-BLOCK", prompt)

    def test_no_compaction_below_threshold_or_without_context(self):
        for replies in ([{"engine": "fake-eng", "context": 59000}], [{"engine": "fake-eng"}], []):
            engine, prompt = self.turn(replies)
            self.assertEqual(engine.compacted, 0, replies)
            self.assertNotIn("## Earlier in this thread", prompt)
            self.assertNotIn("SNAPSHOT-BLOCK", prompt)

    def test_compaction_skips_other_engine_and_teammate_replies(self):
        for replies in ([{"engine": "other", "context": 90000}],
                        [{"engine": "fake-eng", "context": 100}, {"engine": "fake-eng", "context": 90000, "asked_by": "cos"}]):
            engine, prompt = self.turn(replies)
            self.assertEqual(engine.compacted, 0, replies)

    def test_engine_that_trims_itself_gets_no_recap(self):
        engine, prompt = self.turn([{"engine": "fake-eng", "context": 70000}], engine=FakeEngine(can=False))
        self.assertEqual(engine.compacted, 1)
        self.assertNotIn("## Earlier in this thread", prompt)

    def test_threshold_zero_never_compacts(self):
        with mock.patch.dict(os.environ, {"LEOBOT_COMPACT_TOKENS": "0"}):
            engine, _ = self.turn([{"engine": "fake-eng", "context": 10**6}])
        self.assertEqual(engine.compacted, 0)


if __name__ == "__main__":
    unittest.main()
