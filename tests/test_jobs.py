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
from dashboard import chat_session, db, fileops, osctl
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
        for bad in ({"cap": -1}, {"cap": float("nan")}, {"cap": "x"},
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

    def test_cap_is_optional(self):
        for none in (None, "", 0, "0", " "):
            self.assertIsNone(jobs.clean("cap", none), none)
        self.assertEqual(jobs.clean("cap", "2"), 2.0)
        with self.assertRaises(ValueError) as e:
            jobs.clean("cap", "-1")
        self.assertIn("0 or empty for no cap", str(e.exception))
        job = self.make(cap=None)
        self.assertIsNone(jobs.get(self.root, job["id"])["cap"])
        jobs.update(self.root, job["id"], cap=2)
        self.assertEqual(jobs.get(self.root, job["id"])["cap"], 2.0)
        jobs.update(self.root, job["id"], cap="")  # cleared: no cap again
        self.assertIsNone(jobs.get(self.root, job["id"])["cap"])
        jobs.update(self.root, job["id"], name="kept")  # a change without cap keeps it
        self.assertIsNone(jobs.get(self.root, job["id"])["cap"])

    def test_update_delete_and_recover(self):
        job = self.make()
        jobs.update(self.root, job["id"], enabled=False, cap=1.5, name="renamed")
        got = jobs.get(self.root, job["id"])
        self.assertEqual((got["enabled"], got["cap"], got["name"]), (False, 1.5, "renamed"))
        self.assertFalse(jobs.is_due(got, time.time() + 86400))
        with self.assertRaises(ValueError):
            jobs.update(self.root, job["id"], cap=-1)
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

    def test_corrupt_file_reads_empty_and_is_never_overwritten(self):
        path = self.root / jobs.RELPATH
        path.parent.mkdir(parents=True)
        path.write_text("not json")
        self.assertEqual(jobs.load(self.root), [])
        with self.assertRaises(ValueError):  # a write would replace every job with nothing
            self.make()
        self.assertEqual(jobs.take_proposals(self.root, "c1"), [])  # every chat turn ends here
        self.assertEqual(path.read_text(), "not json")

    def test_take_proposals_rewrites_only_when_the_thread_has_some(self):
        job = self.make()
        with mock.patch.object(jobs, "_write") as write:
            self.assertEqual(jobs.take_proposals(self.root, "c1"), [])
        write.assert_not_called()
        jobs.add_proposal(self.root, {"chat": "c1", "op": "delete", "job_id": job["id"]})
        self.assertEqual(len(jobs.take_proposals(self.root, "c1")), 1)
        self.assertEqual(jobs.take_proposals(self.root, "c1"), [])

    def test_live_spend_is_the_running_jobs_only(self):
        self.assertEqual(jobs.live_spent(self.root, "j1"), 0.0)
        jobs.set_live(self.root, "j1", 0.25, now=1000.0)
        self.assertEqual(jobs.live_spent(self.root, "j1", now=1010.0), 0.25)
        self.assertEqual(jobs.live_spent(self.root, "j2", now=1010.0), 0.0)
        self.assertEqual(jobs.live_spent(self.root, "j1", now=1000.0 + jobs.LIVE_MAX_AGE + 1), 0.0)  # a crash's leftover
        jobs.set_live(self.root, None, 0)
        self.assertEqual(jobs.live_spent(self.root, "j1"), 0.0)
        jobs.set_live(self.root, None, 0)  # clearing twice is fine
        (self.root / jobs.LIVE_RELPATH).write_text("not json")
        self.assertEqual(jobs.live_spent(self.root, "j1"), 0.0)

    def test_view_resolves_mode_and_next_run(self):
        job = self.make()
        self.assertEqual(jobs.view({**job, "mode": "bogus"})["mode"], "approval")
        self.assertIsNotNone(jobs.view(job)["next_due"])
        self.assertIsNone(jobs.view({**job, "enabled": False})["next_due"])
        self.assertIsNone(jobs.view({**job, "schedule": "whenever"})["next_due"])

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
        # Adding sweeps stale rows too, so threads nobody collects never pile up.
        jobs.add_proposal(self.root, {"chat": "c8", "op": "delete"}, now=now)
        jobs.add_proposal(self.root, {"chat": "c9", "op": "delete"}, now=now + jobs.PROPOSAL_TTL + 5)
        self.assertEqual([p["chat"] for p in jobs._read(self.root)["proposals"]], ["c9"])

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
    """propose-job and list-jobs: a bot proposes, never writes; job turns run their mode."""

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
        for k in ("LEOBOT_WORKSPACE", "LEOBOT_SHARED", "LEOBOT_READONLY", "LEOBOT_CHAT", "LEOBOT_JOB",
                  "LEOBOT_JOB_MODE"):
            os.environ.pop(k, None)
        with contextlib.redirect_stdout(io.StringIO()):
            index.build(self.root)

    def run_osctl(self, *argv, env=None):
        out = io.StringIO()
        with mock.patch.dict(os.environ, env or {}), contextlib.redirect_stdout(out):
            osctl.main(list(argv))
        return json.loads(out.getvalue().strip().splitlines()[-1])

    def propose(self, *extra, env=None, skill="positioning", bot="cmo"):
        env = {"LEOBOT_WORKSPACE": "acme", "LEOBOT_CHAT": "c1", **(env or {})}
        return self.run_osctl("propose-job", "--bot", bot, "--skill", skill, "--name", "weekly",
                              "--message", "review the week", "--schedule", "mon 09:00", "--cap", "0.5",
                              *extra, env=env)

    def test_job_writes_are_gone(self):
        for cmd in ("create-job", "update-job", "delete-job"):
            with self.assertRaises(SystemExit, msg=cmd), contextlib.redirect_stderr(io.StringIO()):
                osctl.main([cmd, "--job", "x"])

    def test_propose_queues_for_the_thread_only(self):
        r = self.propose("--mode", "auto")
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["proposed"], "New job: weekly")
        self.assertEqual(jobs.load(self.root), [])  # nothing saved
        got = jobs.take_proposals(self.root, "c1")
        self.assertEqual(len(got), 1)
        self.assertEqual((got[0]["op"], got[0]["ws"], got[0]["fields"]["mode"], got[0]["fields"]["workspace"]),
                         ("create", "acme", "auto", "acme"))

    def test_propose_change_and_delete(self):
        jid = jobs.create(self.root, ws="acme", bot="cmo", skill="positioning", name="weekly",
                          message="m", schedule="every 6h", cap=1)["id"]
        env = {"LEOBOT_WORKSPACE": "acme", "LEOBOT_CHAT": "c1"}
        self.assertTrue(self.run_osctl("propose-job", "--job", jid, "--cap", "2", env=env)["ok"])
        self.assertTrue(self.run_osctl("propose-job", "--job", jid, "--delete", env=env)["ok"])
        self.assertFalse(self.run_osctl("propose-job", "--delete", env=env)["ok"])
        self.assertFalse(self.run_osctl("propose-job", "--job", jid, "--bot", "cos", env=env)["ok"])
        self.assertEqual([p["op"] for p in jobs.take_proposals(self.root, "c1")], ["update", "delete"])
        self.assertEqual(jobs.get(self.root, jid)["cap"], 1.0)
        other = jobs.create(self.root, ws="demo", bot="cmo", skill="positioning", name="other",
                            message="m", schedule="every 6h", cap=1)["id"]
        self.assertFalse(self.run_osctl("propose-job", "--job", other, "--cap", "9", env=env)["ok"])
        self.assertFalse(self.propose("--project", "demo")["ok"])  # another workspace from acme
        self.assertTrue(self.propose("--project", "demo", env={"LEOBOT_WORKSPACE": "hq"})["ok"])

    def test_list_jobs_shows_mode(self):
        jobs.create(self.root, ws="acme", bot="cmo", skill="positioning", name="weekly",
                    message="m", schedule="every 6h", cap=1, mode="report")
        listed = self.run_osctl("list-jobs", env={"LEOBOT_WORKSPACE": "acme"})["jobs"]
        self.assertEqual((listed[0]["mode"], "next_due" in listed[0]), ("report", True))
        self.assertEqual(self.run_osctl("list-jobs", env={"LEOBOT_WORKSPACE": "demo"})["jobs"], [])

    def test_refusals(self):
        self.assertFalse(self.propose(bot="nobody")["ok"])
        self.assertFalse(self.propose(bot="social")["ok"])  # inactive
        self.assertFalse(self.propose(skill="market-sizing")["ok"])  # research's skill
        self.assertFalse(self.propose(env={"LEOBOT_CHAT": ""})["ok"])  # outside a chat turn
        self.engine = _Engine(metered=False)
        r = self.propose()
        self.assertFalse(r["ok"])
        self.assertIn("reports cost", r["error"])
        self.assertEqual(jobs.take_proposals(self.root, "c1"), [])

    def test_shared_readonly_and_job_turns_never_propose(self):
        for env in ({"LEOBOT_SHARED": "1"}, {"LEOBOT_READONLY": "1"},
                    {"LEOBOT_JOB": "j1", "LEOBOT_JOB_MODE": "auto"},
                    {"LEOBOT_JOB": "j1", "LEOBOT_JOB_MODE": "approval", "LEOBOT_READONLY": "1"}):
            self.assertFalse(self.propose(env=env)["ok"], env)
        self.assertFalse(self.run_osctl("list-jobs", env={"LEOBOT_WORKSPACE": "acme", "LEOBOT_SHARED": "1"})["ok"])
        self.assertEqual(jobs.take_proposals(self.root, "c1"), [])

    def test_auto_job_that_loads_another_skill_waits_for_approval(self):
        from core import skill_loads
        jid = jobs.create(self.root, ws="acme", bot="cmo", skill="positioning", name="weekly",
                          message="m", schedule="every 6h", cap=None, mode="auto")["id"]
        auto = {"LEOBOT_WORKSPACE": "acme", "LEOBOT_JOB": jid, "LEOBOT_JOB_MODE": "auto", "LEOBOT_CHAT": "run1"}
        memo = ["create-memo", "--project", "acme", "--type", "positioning", "--summary", "s"]
        self.assertTrue(self.run_osctl("get-skill", "--name", "positioning", env=auto)["ok"])
        self.assertTrue(self.run_osctl(*memo, env=auto)["ok"])  # its own skill: still saves
        self.assertTrue(self.run_osctl("get-skill", "--name", "pricing-strategy", env=auto)["ok"])
        r = self.run_osctl(*memo, env=auto)
        self.assertFalse(r["ok"])
        self.assertEqual(r["error"], osctl.READONLY_TEXT["approval"])
        self.assertTrue(self.run_osctl("get-project", "--slug", "acme", env=auto)["ok"])  # reads still work
        # The next run is a fresh chat: it starts clean.
        self.assertTrue(self.run_osctl(*memo, env={**auto, "LEOBOT_CHAT": "run2"})["ok"])
        self.assertEqual(skill_loads.loaded(self.root, "run1"), ["positioning", "pricing-strategy"])

    def test_auto_job_refuses_deletes_and_memory(self):
        auto = {"LEOBOT_WORKSPACE": "acme", "LEOBOT_JOB": "j1", "LEOBOT_JOB_MODE": "auto"}
        for argv in (["delete-memo", "--project", "acme", "--type", "positioning", "--version", "1"],
                     ["remember", "--bot", "cmo", "--text", "a fact"],
                     # whole-document and section-list replaces remove work too
                     ["update-intake", "--project", "acme", "--text", "x"],
                     ["update-technical", "--project", "acme", "--text", "x"],
                     ["update-roadmap", "--product", "p", "--text", "x"],
                     ["update-subsections", "--project", "acme", "--doc", "intake", "--subsections", "A"],
                     ["update-validation-tab", "--project", "acme", "--subsections", "A"],
                     ["update-brief-spec", "--profile", "profile-a", "--text", "x"],
                     ["update-voice", "--profile", "profile-a", "--text", "x"]):
            r = self.run_osctl(*argv, env=auto)
            self.assertFalse(r["ok"], argv)
            self.assertIn("APPROVAL", r["error"])
        self.assertIn("whole document", self.run_osctl("update-intake", "--project", "acme", "--text", "x",
                                                       env=auto)["error"])
        self.assertTrue(osctl._AUTO_REPLACES <= osctl._AUTO_WAITS)
        r = self.run_osctl("create-memo", "--project", "acme", "--type", "positioning", "--summary", "s", env=auto)
        self.assertNotIn("Auto job", r.get("error", ""))  # past the mode gate

    def test_read_only_text_follows_the_mode(self):
        argv = ["create-memo", "--project", "acme", "--type", "positioning"]
        base = {"LEOBOT_WORKSPACE": "acme", "LEOBOT_READONLY": "1"}
        report = self.run_osctl(*argv, env={**base, "LEOBOT_JOB": "j1", "LEOBOT_JOB_MODE": "report"})["error"]
        self.assertIn("only reports", report)
        self.assertNotIn("APPROVAL", report)
        self.assertIn("APPROVAL", self.run_osctl(*argv, env={**base, "LEOBOT_JOB": "j1",
                                                             "LEOBOT_JOB_MODE": "approval"})["error"])
        self.assertIn("the lead saves", self.run_osctl(*argv, env=base)["error"])

    def test_every_command_is_classified_for_auto_jobs_and_app_threads(self):
        import argparse
        names = {n for a in osctl._build_parser()._actions
                 if isinstance(a, argparse._SubParsersAction) for n in a.choices}
        for allowed, other in ((osctl._AUTO_ALLOWED, osctl._AUTO_WAITS),
                               (osctl._LOCAL_CMDS, osctl._NETWORK_CMDS)):
            self.assertEqual(names - allowed - other, set())  # a new command: pick a side
            self.assertEqual(allowed & other, set())
            self.assertEqual(allowed - names, set())  # no stale names
        self.assertFalse({n for n in osctl._AUTO_ALLOWED if n.startswith("delete-")})

    def test_unlisted_command_is_refused_in_auto_jobs_and_app_threads(self):
        auto = {"LEOBOT_WORKSPACE": "acme", "LEOBOT_JOB": "j1", "LEOBOT_JOB_MODE": "auto"}
        with mock.patch.object(osctl, "_AUTO_ALLOWED", osctl._AUTO_ALLOWED - {"create-memo"}):
            r = self.run_osctl("create-memo", "--project", "acme", "--type", "positioning", env=auto)
        self.assertIn("APPROVAL", r["error"])
        apps = {"LEOBOT_WORKSPACE": "acme", chat_session.APPS_USED_ENV: "1"}
        with mock.patch.object(osctl, "_LOCAL_CMDS", osctl._LOCAL_CMDS - {"tree"}):
            self.assertIn("read a connected app", self.run_osctl("tree", env=apps)["error"])
        self.assertNotIn("connected app", self.run_osctl("tree", env=apps).get("error", ""))

    def test_job_commands_are_outside_shared_and_read_only_sets(self):
        for cmd in ("propose-job", "list-jobs"):
            self.assertIn(cmd, osctl._SHARED_BLOCKED)
            self.assertNotIn(cmd, osctl._SHARED_ALLOWED)
        self.assertNotIn("propose-job", osctl._READ_ONLY_CMDS)


if __name__ == "__main__":
    unittest.main()
