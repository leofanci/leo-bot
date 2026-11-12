import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from dashboard import chat_store

# os_relay imports its dashboard siblings as top-level modules (as the server runs it).
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "dashboard"))


class SpendCap(unittest.TestCase):
    def test_cap_counts_only_that_person_and_recent(self):
        from dashboard import spend
        with tempfile.TemporaryDirectory() as d, mock.patch.object(chat_store, "CHATS_DIR", Path(d)):
            cid = "0" * 8 + "-0000-0000-0000-" + "0" * 12
            chat_store.save({"id": cid, "spend": [
                {"ts": time.time(), "by": "p1", "cost": 4.0},
                {"ts": time.time(), "by": "p2", "cost": 9.0},
                {"ts": time.time() - 200_000, "by": "p1", "cost": 9.0}]})
            with mock.patch.dict(os.environ, {"LEOBOT_PERSON_DAILY_CAP": "5"}):
                self.assertIsNone(spend.refusal(None, "p1"))
                self.assertIn("allowance", spend.refusal(None, "p2"))
                self.assertIsNone(spend.refusal(None, "owner"))  # the owner has no allowance
                self.assertIn("allowance", spend.refusal(None, "p1", pending=1.0))
            with mock.patch.dict(os.environ, {"LEOBOT_PERSON_DAILY_CAP": "0"}):
                self.assertIsNone(spend.refusal(None, "p2"))

    def test_person_cap_defaults_to_five(self):
        from dashboard import spend
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(spend.cap_value("person"), 5.0)
        with mock.patch.dict(os.environ, {"LEOBOT_PERSON_DAILY_CAP": "abc"}):
            self.assertEqual(spend.cap_value("person"), 5.0)  # a typo never lifts an invited person's cap
        with mock.patch.dict(os.environ, {"LEOBOT_PERSON_DAILY_CAP": "-1"}):
            self.assertEqual(spend.cap_value("person"), 0.0)


class JobCap(unittest.TestCase):
    def test_job_cap_counts_only_that_job(self):
        from dashboard import spend
        now = time.time()
        rows = [{"ts": now, "by": "owner", "cost": 0.6, "ws": "acme", "job": "j1"},
                {"ts": now, "by": "owner", "cost": 0.9, "ws": "acme", "job": "j2"},
                {"ts": now - 200_000, "by": "owner", "cost": 9.0, "ws": "acme", "job": "j3"}]
        records = lambda: iter([{"spend": rows}])  # noqa: E731
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertIn("this job reached its cap", spend.refusal("acme", "owner", records=records, job=("j1", 0.5)))
            self.assertIsNone(spend.refusal("acme", "owner", records=records, job=("j3", 0.5)))
            self.assertIsNone(spend.refusal("acme", "owner", records=records, job=("j1", 1.0)))
            self.assertIn("this job", spend.refusal("acme", "owner", records=records, pending=0.5, job=("j3", 0.5)))
            self.assertIsNone(spend.refusal("acme", "owner", records=records))
            with tempfile.TemporaryDirectory() as d:
                self.assertIn("this job's daily cap",
                              spend.near_notice("acme", "owner", d, records=records, job=("j1", 0.7)))


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


class Caps(unittest.TestCase):
    """Owner and workspace caps over the per-thread spend ledger (dashboard/spend.py)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        patcher = mock.patch.object(chat_store, "CHATS_DIR", self.root / "chats")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)
        self.n = 0

    def save_rows(self, rows):
        self.n += 1
        chat_store.save({"id": f"{self.n:08d}-0000-0000-0000-" + "0" * 12, "spend": rows})

    @staticmethod
    def env(**kw):
        return mock.patch.dict(os.environ, kw)

    def test_workspace_cap_counts_only_that_workspace(self):
        from dashboard import spend
        now = time.time()
        self.save_rows([{"ts": now, "by": "owner", "cost": 4.0, "ws": "acme"},
                        {"ts": now, "by": "owner", "cost": 9.0, "ws": "demo"}])
        with self.env(LEOBOT_WORKSPACE_DAILY_CAP="5"):
            self.assertIsNone(spend.refusal("acme", "owner"))
            self.assertIn("workspace", spend.refusal("demo", "owner"))
            self.assertIn("workspace", spend.refusal("demo", "p1"))  # the workspace cap holds for everyone

    def test_owner_cap_skips_invited_people(self):
        from dashboard import spend
        self.save_rows([{"ts": time.time(), "by": "owner", "cost": 6.0, "ws": "acme"}])
        with self.env(LEOBOT_OWNER_DAILY_CAP="5"):
            text = spend.refusal("acme", "owner")
            self.assertIn("LEOBOT_OWNER_DAILY_CAP", text)
            self.assertIn("$5.00", text)
            self.assertNotIn("—", text)
            self.assertIsNone(spend.refusal("acme", "p1"))

    def test_refusal_copy_small_cap_and_tomorrow(self):
        from dashboard import spend
        now = time.time()
        self.save_rows([{"ts": now - 60, "by": "owner", "cost": 1.0, "ws": "acme"}])
        with self.env(LEOBOT_OWNER_DAILY_CAP="0.0001"):
            text = spend.refusal("acme", "owner", now=now)
        self.assertIn("$0.0001", text)
        frees = now - 60 + spend.WINDOW
        same_day = time.localtime(frees)[:3] == time.localtime(now)[:3]
        self.assertIn("from " if same_day else "tomorrow at ", text)

    def test_old_rows_leave_the_window(self):
        from dashboard import spend
        self.save_rows([{"ts": time.time() - 90_000, "by": "owner", "cost": 99.0, "ws": "acme"}])
        with self.env(LEOBOT_OWNER_DAILY_CAP="5"):
            self.assertIsNone(spend.refusal("acme", "owner"))

    def test_pending_spend_counts(self):
        from dashboard import spend
        with self.env(LEOBOT_OWNER_DAILY_CAP="5"):
            self.assertIsNone(spend.refusal("acme", "owner"))
            self.assertIsNotNone(spend.refusal("acme", "owner", pending=5.0))

    def test_rows_without_ws_skip_workspace_cap(self):
        from dashboard import spend
        self.save_rows([{"ts": time.time(), "by": "owner", "cost": 9.0}])  # recorded before workspace caps
        with self.env(LEOBOT_WORKSPACE_DAILY_CAP="5"):
            self.assertIsNone(spend.refusal("acme", "owner"))

    def test_bad_cap_values_mean_no_cap(self):
        from dashboard import spend
        for v in ("abc", "-3", "", "0", "nan", "inf"):
            with self.env(LEOBOT_OWNER_DAILY_CAP=v):
                self.assertEqual(spend.cap_value("owner"), 0.0)
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(spend.cap_value("workspace"), 0.0)

    def test_notice_once_per_day_survives_reload(self):
        import importlib
        from dashboard import spend
        self.save_rows([{"ts": time.time(), "by": "owner", "cost": 4.2, "ws": "acme"}])
        with self.env(LEOBOT_OWNER_DAILY_CAP="5"):
            note = spend.near_notice("acme", "owner", self.root)
            self.assertIn("84%", note)
            importlib.reload(spend)
            self.assertIsNone(spend.near_notice("acme", "owner", self.root))
            later = time.time() + 90_000  # a day on, and the owner is near the cap again
            self.save_rows([{"ts": later - 60, "by": "owner", "cost": 4.5, "ws": "acme"}])
            self.assertIsNotNone(spend.near_notice("acme", "owner", self.root, now=later))

    def test_no_notice_under_80_percent(self):
        from dashboard import spend
        self.save_rows([{"ts": time.time(), "by": "owner", "cost": 3.0, "ws": "acme"}])
        with self.env(LEOBOT_OWNER_DAILY_CAP="5"):
            self.assertIsNone(spend.near_notice("acme", "owner", self.root))
            self.assertFalse((self.root / "team" / "cap_notices.json").exists())

    def test_workspace_notice_reaches_owner_after_a_person(self):
        from dashboard import spend
        self.save_rows([{"ts": time.time(), "by": "p1", "cost": 4.5, "ws": "acme"}])
        with self.env(LEOBOT_WORKSPACE_DAILY_CAP="5"):
            self.assertIsNotNone(spend.near_notice("acme", "p1", self.root))
            self.assertIsNotNone(spend.near_notice("acme", "owner", self.root))
            self.assertIsNone(spend.near_notice("acme", "owner", self.root))

    def test_one_scan_per_check(self):
        from dashboard import spend
        calls = []

        def records():
            calls.append(1)
            return iter([{"spend": [{"ts": time.time(), "by": "owner", "cost": 1.0, "ws": "acme"}]}])
        with self.env(LEOBOT_WORKSPACE_DAILY_CAP="5", LEOBOT_OWNER_DAILY_CAP="5"):
            spend.refusal("acme", "owner", records=records)
            spend.near_notice("acme", "owner", self.root, records=records)
        self.assertEqual(len(calls), 2)
