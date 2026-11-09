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


class JobBoundTests(unittest.TestCase):
    def test_shared_job_cap_and_kill(self):
        import os_relay
        ctx = {"ws": "demo", "shared": True}
        held = [os_relay._JOBS.acquire(blocking=False) for _ in range(2)]
        try:
            text, failed = os_relay.run_osctl(["generate-brief", "--id", "x"], "", ctx)
            self.assertTrue(failed)
            self.assertIn("running", text)
            text, _ = os_relay.run_osctl(["tree"], "", ctx)  # not a job: not capped
            self.assertNotIn("generate jobs are running", text)
        finally:
            for ok in held:
                if ok:
                    os_relay._JOBS.release()

    def test_timeout_kills_group(self):
        import os_relay
        with tempfile.TemporaryDirectory() as d:
            fake = Path(d) / "fakepy"
            fake.write_text("#!/bin/sh\nsleep 30\n")
            fake.chmod(0o755)
            with mock.patch.object(os_relay, "TIMEOUT", 1), mock.patch.object(os_relay.sys, "executable", str(fake)):
                text, failed = os_relay.run_osctl(["tree"], "", {"ws": "demo", "shared": False})
        self.assertTrue(failed)
        self.assertIn("too long", text)
