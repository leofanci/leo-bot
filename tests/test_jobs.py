"""Scheduled jobs: the store and schedule math (core/jobs.py) and the osctl commands."""
import contextlib
import io
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import index
from core import bots, jobs, memory
from dashboard import db, fileops, osctl
from dashboard.engines import registry


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
        jobs.update(self.root, job["id"], enabled=True)
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

    def test_job_count_is_limited(self):
        with mock.patch.object(jobs, "MAX_JOBS", 2):
            self.make()
            self.make()
            with self.assertRaises(ValueError):
                self.make()
        self.assertEqual(len(jobs.load(self.root)), 2)

    def test_mark_start_returns_the_latest_job_or_none(self):
        job = self.make()
        jobs.update(self.root, job["id"], message="new words")
        self.assertEqual(jobs.mark_start(self.root, job["id"], 100.0)["message"], "new words")
        jobs.update(self.root, job["id"], enabled=False)
        self.assertIsNone(jobs.mark_start(self.root, job["id"], 200.0))
        self.assertEqual(jobs.get(self.root, job["id"])["last_run"], 100.0)  # a disabled job is not marked

    def test_corrupt_file_reads_empty(self):
        path = self.root / jobs.RELPATH
        path.parent.mkdir(parents=True)
        path.write_text("not json")
        self.assertEqual(jobs.load(self.root), [])
        self.make()
        self.assertEqual(len(jobs.load(self.root)), 1)

    def test_mode_defaults_and_validates(self):
        job = self.make()
        self.assertEqual(job["mode"], "approval")
        with self.assertRaises(ValueError):
            self.make(mode="bogus")
        self.assertEqual(self.make(mode="report")["mode"], "report")
        jobs.update(self.root, job["id"], mode="auto")
        self.assertEqual(jobs.get(self.root, job["id"])["mode"], "auto")
        with self.assertRaises(ValueError):
            jobs.update(self.root, job["id"], mode="free")
        self.assertEqual(jobs.mode_of({}), "approval")
        self.assertEqual(jobs.mode_of({"mode": "x"}), "approval")
        self.assertEqual(jobs.mode_of({"mode": "auto"}), "auto")

    def test_proposal_outbox(self):
        now = 1_000_000.0
        for chat in ("c1", "c1", "c2"):
            jobs.add_proposal(self.root, {"chat": chat, "ws": "acme", "op": "create", "fields": {}}, now=now)
        self.make()  # the outbox lives beside the jobs and survives their writes
        got = jobs.take_proposals(self.root, "c1", now=now + 1)
        self.assertEqual([p["chat"] for p in got], ["c1", "c1"])
        self.assertEqual(got[0]["created"], now)
        self.assertEqual(jobs.take_proposals(self.root, "c1", now=now + 2), [])
        for _ in range(jobs.PROPOSALS_PER_CHAT):
            jobs.add_proposal(self.root, {"chat": "c3", "op": "delete"}, now=now)
        with self.assertRaises(ValueError):
            jobs.add_proposal(self.root, {"chat": "c3", "op": "delete"}, now=now)
        with self.assertRaises(ValueError):
            jobs.add_proposal(self.root, {"op": "delete"}, now=now)  # no thread
        # A day later nothing left behind shows up anywhere.
        self.assertEqual(jobs.take_proposals(self.root, "c2", now=now + jobs.PROPOSAL_TTL + 1), [])
        self.assertEqual(jobs.take_proposals(self.root, "c3", now=now + 1), [])
        self.assertEqual(len(jobs.load(self.root)), 1)

    def test_digest_state_kept_beside_jobs(self):
        self.make()
        self.assertEqual(jobs.digest_state(self.root), {})
        jobs.set_digest(self.root, 123.0)
        self.assertEqual(jobs.digest_state(self.root), {"last": 123.0})
        self.assertEqual(len(jobs.load(self.root)), 1)


class _Engine:
    def __init__(self, metered=True):
        self.id, self.label, self.kind = "api:x", "X API", "api"
        self._metered = metered


class Osctl(unittest.TestCase):
    """The four job commands: workspace-bound, refused in shared and read-only turns."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for slug in ("acme", "demo"):
            (self.root / "projects" / slug).mkdir(parents=True)
            (self.root / "projects" / slug / "project.md").write_text(f"---\nname: {slug}\nkind: venture\n---\n")
        self.engine = _Engine()
        for p in [mock.patch.object(t, "ROOT", self.root) for t in (bots, memory, fileops)] + [
                mock.patch.object(db, "DB_PATH", self.root / "database" / "data" / "os.db"),
                mock.patch.object(registry, "pick", side_effect=lambda *a, **k: self.engine),
                mock.patch.object(registry, "metered", side_effect=lambda e: e._metered),
                mock.patch.dict(os.environ, {})]:
            p.start()
            self.addCleanup(p.stop)
        for k in ("LEOBOT_WORKSPACE", "LEOBOT_SHARED", "LEOBOT_READONLY"):
            os.environ.pop(k, None)
        with contextlib.redirect_stdout(io.StringIO()):
            index.build(self.root)

    def run_osctl(self, *argv, env=None):
        out = io.StringIO()
        with mock.patch.dict(os.environ, env or {}), contextlib.redirect_stdout(out):
            osctl.main(list(argv))
        return json.loads(out.getvalue().strip().splitlines()[-1])

    def create(self, *extra, env=None, skill="positioning", bot="cmo"):
        return self.run_osctl("create-job", "--bot", bot, "--skill", skill, "--name", "weekly",
                              "--message", "review the week", "--schedule", "mon 09:00", "--cap", "0.5",
                              *extra, env=env)

    def test_create_list_update_delete(self):
        r = self.create("--project", "acme")
        self.assertTrue(r["ok"], r)
        jid = r["job"]["id"]
        self.assertEqual(r["job"]["workspace"], "acme")
        listed = self.run_osctl("list-jobs")
        self.assertEqual([j["id"] for j in listed["jobs"]], [jid])
        self.assertIn("next_due", listed["jobs"][0])
        r = self.run_osctl("update-job", "--job", jid, "--enabled", "false", "--cap", "2")
        self.assertTrue(r["ok"], r)
        self.assertEqual((jobs.get(self.root, jid)["enabled"], jobs.get(self.root, jid)["cap"]), (False, 2.0))
        self.assertTrue(self.run_osctl("delete-job", "--job", jid)["ok"])
        self.assertEqual(self.run_osctl("list-jobs")["jobs"], [])

    def test_hq_job_without_project(self):
        r = self.create(bot="cos", skill="weekly-review")
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["job"]["workspace"], "hq")

    def test_workspace_bound(self):
        acme = {"LEOBOT_WORKSPACE": "acme"}
        r = self.create(env=acme)
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["job"]["workspace"], "acme")
        self.assertFalse(self.create("--project", "demo", env=acme)["ok"])
        other = self.create("--project", "demo")["job"]["id"]
        self.assertEqual([j["workspace"] for j in self.run_osctl("list-jobs", env=acme)["jobs"]], ["acme"])
        self.assertFalse(self.run_osctl("delete-job", "--job", other, env=acme)["ok"])
        self.assertFalse(self.run_osctl("update-job", "--job", other, "--cap", "9", env=acme)["ok"])
        self.assertIsNotNone(jobs.get(self.root, other))

    def test_refusals(self):
        self.assertFalse(self.create("--project", "acme", bot="nobody")["ok"])
        self.assertFalse(self.create("--project", "acme", bot="social", skill="positioning")["ok"])  # inactive
        self.assertFalse(self.create("--project", "acme", skill="market-sizing")["ok"])  # research's skill
        self.assertFalse(self.create("--project", "gone")["ok"])
        self.engine = _Engine(metered=False)
        r = self.create("--project", "acme")
        self.assertFalse(r["ok"])
        self.assertIn("reports cost", r["error"])
        self.assertEqual(jobs.load(self.root), [])

    def test_shared_and_readonly_refuse_job_commands(self):
        jid = self.create("--project", "acme")["job"]["id"]
        for env in ({"LEOBOT_WORKSPACE": "acme", "LEOBOT_SHARED": "1"}, {"LEOBOT_READONLY": "1"}):
            for argv in (["list-jobs"], ["delete-job", "--job", jid], ["update-job", "--job", jid, "--cap", "9"]):
                self.assertFalse(self.run_osctl(*argv, env=env)["ok"], (env, argv))
            self.assertFalse(self.create("--project", "acme", env=env)["ok"])
        self.assertEqual(len(jobs.load(self.root)), 1)
        self.assertEqual(jobs.get(self.root, jid)["cap"], 0.5)

    def test_job_commands_are_outside_shared_and_read_only_sets(self):
        for cmd in ("create-job", "update-job", "delete-job", "list-jobs"):
            self.assertIn(cmd, osctl._SHARED_BLOCKED)
            self.assertNotIn(cmd, osctl._SHARED_ALLOWED)
            self.assertNotIn(cmd, osctl._READ_ONLY_CMDS)


if __name__ == "__main__":
    unittest.main()
