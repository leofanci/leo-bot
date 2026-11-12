"""Thread compaction: the threshold and the last reply's context size."""
import os
import unittest
from unittest import mock

from dashboard import compaction


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


if __name__ == "__main__":
    unittest.main()
