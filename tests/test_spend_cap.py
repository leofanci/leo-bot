import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from dashboard import chat_store


class SpendCap(unittest.TestCase):
    def test_cap_counts_only_that_person_and_recent(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(chat_store, "CHATS_DIR", Path(d)):
            import time
            cid = "0" * 8 + "-0000-0000-0000-" + "0" * 12
            chat_store.save({"id": cid, "spend": [
                {"ts": time.time(), "by": "p1", "cost": 4.0},
                {"ts": time.time(), "by": "p2", "cost": 9.0},
                {"ts": time.time() - 200_000, "by": "p1", "cost": 9.0}]})
            with mock.patch.dict(os.environ, {"MYOS_PERSON_DAILY_CAP": "5"}):
                self.assertFalse(chat_store.over_cap("p1"))
                self.assertTrue(chat_store.over_cap("p2"))
            with mock.patch.dict(os.environ, {"MYOS_PERSON_DAILY_CAP": "0"}):
                self.assertFalse(chat_store.over_cap("p2"))


if __name__ == "__main__":
    unittest.main()


class ReadSize(unittest.TestCase):
    def test_big_argument_holds(self):
        from core import app_rules
        cat = {"gmail": {}}
        self.assertEqual(app_rules.decide("GMAIL_FETCH_EMAILS", {"gmail": "write"}, cat, {"q": "a"})[0], "read")
        self.assertEqual(app_rules.decide("GMAIL_FETCH_EMAILS", {"gmail": "write"}, cat, {"q": "a" * 3000})[0], "hold")
        self.assertEqual(app_rules.decide("GMAIL_GENERATE_THING", {"gmail": "write"}, cat, {})[0], "hold")


class OpenFolder(unittest.TestCase):
    def test_app_bundle_refused(self):
        from dashboard import server
        fileops = server.fileops
        with tempfile.TemporaryDirectory() as d:
            app = Path(d) / "Thing.app"
            app.mkdir()
            with self.assertRaises(fileops.ActionError):
                server.open_local_folder(str(app))
