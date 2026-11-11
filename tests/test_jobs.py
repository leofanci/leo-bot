"""Scheduled jobs: the store and schedule math (core/jobs.py) and the osctl commands."""
import tempfile
import time
import unittest
from pathlib import Path

from core import jobs


def local(y, mo, d, h, mi, s=0):
    return time.mktime((y, mo, d, h, mi, s, 0, 0, -1))


def monday():
    """A Monday at 00:00 local, in the past."""
    t = local(2026, 6, 1, 0, 0)  # 2026-06-01 is a Monday
    assert time.localtime(t).tm_wday == 0
    return t


class Schedule(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(jobs.parse_schedule("every 6h"), {"every": 21600})
        self.assertEqual(jobs.parse_schedule("every 90m"), {"every": 5400})
        self.assertEqual(jobs.parse_schedule("Every 2d"), {"every": 172800})
        self.assertEqual(jobs.parse_schedule("daily 09:00"), {"days": list(range(7)), "at": (9, 0)})
        self.assertEqual(jobs.parse_schedule("mon,thu 18:30"), {"days": [0, 3], "at": (18, 30)})
        for bad in ("every 30m", "tue 25:00", "someday 09:00", "", "every h", "mon 9", None):
            with self.assertRaises(ValueError, msg=bad):
                jobs.parse_schedule(bad)

    def test_missed_runs_run_once(self):
        now = time.time()
        job = {"enabled": True, "schedule": "every 6h", "created": now - 10 * 86400,
               "last_run": now - 3 * 86400}
        self.assertTrue(jobs.is_due(job, now))
        job["last_run"] = now
        self.assertFalse(jobs.is_due(job, now + 60))
        self.assertEqual(jobs.next_due(job), now + 21600)

    def test_weekly_slot_strictly_after_base(self):
        mon = monday()
        job = {"enabled": True, "schedule": "mon 09:00", "created": mon + 9 * 3600 + 30, "last_run": None}
        self.assertEqual(jobs.next_due(job), mon + 7 * 86400 + 9 * 3600)
        job["last_run"] = mon + 9 * 3600 + 5
        self.assertFalse(jobs.is_due(job, mon + 9 * 3600 + 60))
        job = {"enabled": True, "schedule": "mon,wed 09:00", "created": mon, "last_run": None}
        self.assertEqual(jobs.next_due(job), mon + 9 * 3600)
        job["last_run"] = mon + 9 * 3600
        self.assertEqual(jobs.next_due(job), mon + 2 * 86400 + 9 * 3600)

    def test_disabled_or_broken_is_never_due(self):
        now = time.time()
        self.assertFalse(jobs.is_due({"enabled": False, "schedule": "every 1h", "created": 0}, now))
        self.assertFalse(jobs.is_due({"enabled": True, "schedule": "nope", "created": 0}, now))


class Store(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def make(self, **kw):
        args = {"ws": "acme", "bot": "cmo", "skill": "positioning", "name": "weekly",
                "message": "review the week", "schedule": "every 6h", "cap": 0.5, **kw}
        return jobs.create(self.root, **args)

    def test_create_validates(self):
        for bad in ({"cap": 0}, {"cap": -1}, {"cap": float("nan")}, {"cap": "x"},
                    {"schedule": "whenever"}, {"name": ""}, {"name": "x" * 61},
                    {"message": " "}, {"message": "x" * 2001}, {"bot": ""}, {"skill": ""}):
            with self.assertRaises(ValueError, msg=bad):
                self.make(**bad)
        job = self.make()
        got = jobs.load(self.root)
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["id"], job["id"])
        self.assertEqual(len(job["id"]), 8)
        self.assertTrue(got[0]["enabled"])
        self.assertIsNone(got[0]["last_run"])
        self.assertEqual(got[0]["cap"], 0.5)

    def test_update_delete_and_recover(self):
        job = self.make()
        jobs.update(self.root, job["id"], enabled=False, cap=1.5, name="renamed")
        got = jobs.get(self.root, job["id"])
        self.assertEqual((got["enabled"], got["cap"], got["name"]), (False, 1.5, "renamed"))
        self.assertFalse(jobs.is_due(got, time.time() + 86400))
        with self.assertRaises(ValueError):
            jobs.update(self.root, job["id"], cap=0)
        with self.assertRaises(LookupError):
            jobs.update(self.root, "missing1", cap=1)
        jobs.mark_start(self.root, job["id"], 100.0)
        self.assertEqual(jobs.get(self.root, job["id"])["last_status"], "running")
        self.assertEqual(jobs.recover(self.root), 1)
        self.assertEqual(jobs.get(self.root, job["id"])["last_status"], "interrupted")
        jobs.mark_end(self.root, job["id"], "done", chat="c1")
        got = jobs.get(self.root, job["id"])
        self.assertEqual((got["last_status"], got["last_chat"], got["last_run"]), ("done", "c1", 100.0))
        self.assertTrue(jobs.delete(self.root, job["id"]))
        self.assertFalse(jobs.delete(self.root, job["id"]))
        self.assertEqual(jobs.load(self.root), [])

    def test_corrupt_file_reads_empty(self):
        path = self.root / jobs.RELPATH
        path.parent.mkdir(parents=True)
        path.write_text("not json")
        self.assertEqual(jobs.load(self.root), [])
        self.make()
        self.assertEqual(len(jobs.load(self.root)), 1)

    def test_digest_state_kept_beside_jobs(self):
        self.make()
        self.assertEqual(jobs.digest_state(self.root), {})
        jobs.set_digest(self.root, 123.0)
        self.assertEqual(jobs.digest_state(self.root), {"last": 123.0})
        self.assertEqual(len(jobs.load(self.root)), 1)


if __name__ == "__main__":
    unittest.main()
