"""The job tick (dashboard/job_runner.py): due jobs one at a time, state around each run,
restart, timeout, and the daily HQ digest."""
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from core import approvals, jobs
from dashboard import chat_store, job_ledger, job_runner


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        p = mock.patch.object(chat_store, "CHATS_DIR", self.root / "chats")
        p.start()
        self.addCleanup(p.stop)
        # 07:00 local today: before the digest hour, so ticks post no digest unless a test moves on.
        lt = time.localtime()
        self.clock = Clock(time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 7, 0, 0, 0, 0, -1)))

    def make(self, name="weekly", ago=7 * 3600, schedule="every 6h", **kw):
        job = jobs.create(self.root, ws="acme", bot="cmo", skill="positioning", name=name,
                          message="review", schedule=schedule, cap=0.5, now=self.clock() - ago)
        if kw:
            jobs.update(self.root, job["id"], **kw)
        return job

    def runner(self, run_turn, **kw):
        return job_runner.Runner(self.root, run_turn, now=self.clock, **kw)


class Tick(Base):
    def test_due_job_runs_and_state_is_written_before(self):
        job = self.make()
        seen = []

        def run_turn(j):
            seen.append(jobs.get(self.root, j["id"])["last_status"])
            return {"status": "done", "chat": "c1"}
        r = self.runner(run_turn)
        self.assertEqual(r.tick(), [job["id"]])
        self.assertEqual(seen, ["running"])
        got = jobs.get(self.root, job["id"])
        self.assertEqual((got["last_status"], got["last_chat"], got["last_run"]), ("done", "c1", self.clock()))
        self.assertEqual(r.tick(), [])
        self.clock.t += 6 * 3600
        self.assertEqual(r.tick(), [job["id"]])

    def test_not_due_and_disabled_do_not_run(self):
        self.make(ago=60)
        self.make(name="off", enabled=False)
        self.assertEqual(self.runner(lambda j: self.fail("ran")).tick(), [])

    def test_one_at_a_time_in_due_order(self):
        late = self.make(name="late", ago=7 * 3600)
        early = self.make(name="early", ago=9 * 3600)
        order, busy = [], threading.Lock()

        def run_turn(j):
            self.assertTrue(busy.acquire(blocking=False), "two job turns at once")
            try:
                order.append(j["id"])
            finally:
                busy.release()
            return {"status": "done"}
        self.assertEqual(self.runner(run_turn).tick(), [early["id"], late["id"]])
        self.assertEqual(order, [early["id"], late["id"]])

    def test_job_changed_during_an_earlier_run_uses_its_latest_state(self):
        first = self.make(name="first", ago=9 * 3600)
        off = self.make(name="off")
        edited = self.make(name="edited", ago=8 * 3600)
        seen = []

        def run_turn(j):
            seen.append((j["name"], j["message"]))
            if j["id"] == first["id"]:  # the owner changes the others while this one runs
                jobs.update(self.root, off["id"], enabled=False)
                jobs.update(self.root, edited["id"], message="new words")
            return {"status": "done"}
        self.assertEqual(self.runner(run_turn).tick(), [first["id"], edited["id"]])
        self.assertEqual(seen, [("first", "review"), ("edited", "new words")])
        self.assertIsNone(jobs.get(self.root, off["id"])["last_run"])

    def test_restart_marks_interrupted_and_does_not_rerun(self):
        job = self.make()
        jobs.mark_start(self.root, job["id"], self.clock() - 600)
        r = self.runner(lambda j: self.fail("ran"))
        with mock.patch.object(job_runner, "TICK", 3600):
            r.start()
        r.halt()
        self.assertEqual(jobs.get(self.root, job["id"])["last_status"], "interrupted")
        self.assertEqual(r.tick(), [])

    def test_timeout_stops_the_turn(self):
        job = self.make()
        stopped = threading.Event()

        def run_turn(j):
            stopped.wait(5)
            return {"status": "failed"}
        with mock.patch.object(job_runner, "JOB_TURN_SECONDS", 0.05):
            self.runner(run_turn, stop_turn=stopped.set).tick()
        self.assertTrue(stopped.is_set())
        self.assertEqual(jobs.get(self.root, job["id"])["last_status"], "timeout")

    def test_exception_is_failed_and_others_still_run(self):
        bad = self.make(name="bad", ago=9 * 3600)
        good = self.make(name="good")

        def run_turn(j):
            if j["id"] == bad["id"]:
                raise RuntimeError("boom")
            return {"status": "waiting", "chat": "c2"}
        with mock.patch("traceback.print_exc"):
            self.runner(run_turn).tick()
        self.assertEqual(jobs.get(self.root, bad["id"])["last_status"], "failed")
        self.assertEqual(jobs.get(self.root, good["id"])["last_status"], "waiting")

    def test_unknown_status_is_failed(self):
        job = self.make()
        self.runner(lambda j: {"status": "weird"}).tick()
        self.assertEqual(jobs.get(self.root, job["id"])["last_status"], "failed")


class Digest(Base):
    def seed(self):
        job = self.make()
        now = self.clock()
        cid = "0" * 8 + "-0000-0000-0000-" + "0" * 12
        chat_store.save({"id": cid, "title": "Job: weekly", "job": job["id"], "created": now - 3600,
                         "spend": [{"ts": now - 3600, "by": "owner", "cost": 0.03, "ws": "acme", "job": job["id"]}]})
        jobs.mark_start(self.root, job["id"], now - 3600)
        jobs.mark_end(self.root, job["id"], "waiting", chat=cid)
        job_ledger.append(self.root, {"ts": now - 600, "ws": "acme", "bot": "cmo", "job": "memory-consolidate",
                                      "cost": 0.001, "before": 310, "after": 240})
        job_ledger.append(self.root, {"ts": now - 500, "ws": "acme", "job": "generate", "cost": 0.002})
        approvals.add(self.root, ws="acme", room="acme/dm-cmo", chat=cid, bot="cmo",
                      summary="save the review memo to sec04")

    def test_digest_text(self):
        self.seed()
        text = job_runner.digest_text(self.root, self.clock() - 86400, self.clock())
        self.assertIn("- weekly (acme, @cmo): 1 run, $0.03, last: waiting", text)
        self.assertIn("@cmo in acme: 310 → 240 entries", text)
        self.assertIn("Generation runs\n- 1 run", text)
        self.assertIn("Waiting for your approval (1)\n- acme: save the review memo to sec04", text)
        self.assertNotIn("—", text)

    def test_auto_job_generate_cost_and_job_cards(self):
        self.seed()
        jid = jobs.load(self.root)[0]["id"]
        job_ledger.append(self.root, {"ts": self.clock() - 400, "ws": "acme", "job": "generate",
                                      "for_job": jid, "cost": 0.05})
        approvals.add_job(self.root, ws="acme", room="acme/dm-cmo", chat="c9", bot="cmo",
                          job={"op": "create", "job_id": None, "fields": {}}, summary="New job: daily",
                          preview={"title": "New job: daily", "fields": []})
        text = job_runner.digest_text(self.root, self.clock() - 86400, self.clock())
        self.assertIn("- weekly (acme, @cmo): 1 run, $0.08, last: waiting", text)
        self.assertIn("Generation runs\n- 1 run, $0.0020", text)  # the job's run is on the job's line
        self.assertIn("Waiting for your approval (2)", text)
        self.assertIn("- acme: New job: daily", text)

    def test_posted_once_a_day_from_the_hour(self):
        self.seed()
        r = self.runner(lambda j: {"status": "done"})
        self.assertIsNone(r.maybe_digest())  # 07:00: too early
        self.clock.t += 2 * 3600
        chat = r.maybe_digest()
        rec = chat_store.load(chat)
        self.assertEqual((rec["room"], rec["messages"][0]["bot"]), ("hq/general", "cos"))
        self.assertTrue(rec["title"].startswith("Digest "))
        self.assertIn("weekly", rec["messages"][0]["content"])
        self.clock.t += 3600
        self.assertIsNone(r.maybe_digest())
        self.assertEqual(len(chat_store.list_meta()), 2)  # the job thread and one digest

    def test_nothing_to_report_posts_nothing(self):
        r = self.runner(lambda j: {"status": "done"})
        self.clock.t += 2 * 3600
        self.assertIsNone(r.maybe_digest())
        self.assertEqual(chat_store.list_meta(), [])
        self.assertEqual(jobs.digest_state(self.root)["last"], self.clock())


if __name__ == "__main__":
    unittest.main()
