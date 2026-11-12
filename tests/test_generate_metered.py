"""generate.py runs count against the caps: checked before, one job_ledger row after."""
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import generate
from core import jobs
from dashboard import chat_store, fileops, job_ledger, spend
from dashboard.engines import registry


class _Engine:
    id, label, kind = "api:test", "Test API", "api"

    def __init__(self, cost=0.002):
        self.calls, self.cost = [], cost

    def complete_metered(self, prompt, stdin_text, model):
        self.calls.append(model)
        return {"text": "{}", "model": "claude-haiku-5-5",
                "usage": {"in": 100, "cache_write": 0, "cache_read": 0, "out": 50},
                "cost": self.cost, "truncated": False}


class Metered(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.engine = _Engine()
        for p in (mock.patch.object(fileops, "ROOT", self.root),
                  mock.patch.object(chat_store, "CHATS_DIR", self.root / "chats"),
                  mock.patch.object(registry, "for_job", side_effect=lambda *a: self.engine),
                  mock.patch.dict(os.environ, {"LEOBOT_WORKSPACE": "acme", "LEOBOT_OWNER_DAILY_CAP": "",
                                               "LEOBOT_WORKSPACE_DAILY_CAP": ""})):
            p.start()
            self.addCleanup(p.stop)

    def test_run_is_recorded(self):
        out = json.loads(generate.run_claude("prompt", "text"))
        self.assertEqual(generate.extract_result(json.dumps(out)), "{}")
        rows = job_ledger.rows(self.root)
        self.assertEqual([(r["job"], r["ws"], r["cost"], r["tokens"]) for r in rows], [("generate", "acme", 0.002, 150)])

    def test_cost_estimated_when_engine_reports_none(self):
        self.engine.cost = None
        generate.run_claude("prompt", "text")
        self.assertGreater(job_ledger.rows(self.root)[0]["cost"], 0)

    def test_capped_run_never_calls_the_engine(self):
        job_ledger.append(self.root, {"ts": time.time(), "ws": "acme", "by": "owner", "job": "generate", "cost": 2.0})
        with mock.patch.dict(os.environ, {"LEOBOT_WORKSPACE_DAILY_CAP": "1"}):
            with self.assertRaises(generate.JobError) as err:
                generate.run_claude("prompt", "text")
        self.assertIn("Paused:", str(err.exception))
        self.assertEqual(self.engine.calls, [])

    def auto_job(self, cap=1.0):
        return jobs.create(self.root, ws="acme", bot="cmo", skill="positioning", name="weekly",
                           message="m", schedule="every 6h", cap=cap, mode="auto")

    def test_auto_job_run_counts_against_the_job(self):
        job = self.auto_job()
        with mock.patch.dict(os.environ, {"LEOBOT_JOB": job["id"]}):
            generate.run_claude("prompt", "text")
        row = job_ledger.rows(self.root)[0]
        self.assertEqual((row["job"], row["for_job"]), ("generate", job["id"]))  # still grouped as generate
        records = lambda: job_ledger.records(self.root)  # noqa: E731
        self.assertEqual(spend.spent("job", job["id"], records=records)[0], 0.002)

    def test_job_cap_stops_a_run_before_the_engine(self):
        job = self.auto_job(cap=1.0)
        job_ledger.append(self.root, {"ts": time.time(), "ws": "acme", "by": "owner", "job": "generate",
                                      "for_job": job["id"], "cost": 1.5})
        with mock.patch.dict(os.environ, {"LEOBOT_JOB": job["id"]}):
            with self.assertRaises(generate.JobError) as err:
                generate.run_claude("prompt", "text")
        self.assertIn("this job reached its cap", str(err.exception))
        self.assertEqual(self.engine.calls, [])
        generate.run_claude("prompt", "text")  # outside the job: no job cap
        self.assertEqual(len(self.engine.calls), 1)

    def test_job_cap_counts_the_turn_spent_so_far(self):
        job = self.auto_job(cap=1.0)
        env = {"LEOBOT_JOB": job["id"], "LEOBOT_JOB_SPENT": "0.95"}
        job_ledger.append(self.root, {"ts": time.time(), "ws": "acme", "by": "owner", "job": "generate",
                                      "for_job": job["id"], "cost": 0.06})
        with mock.patch.dict(os.environ, env), self.assertRaises(generate.JobError):
            generate.run_claude("prompt", "text")  # 0.06 saved + 0.95 not saved yet
        self.assertEqual(self.engine.calls, [])
        for junk in ("abc", "-5"):  # a bad value never lifts the cap check or breaks the run
            with mock.patch.dict(os.environ, {"LEOBOT_JOB": job["id"], "LEOBOT_JOB_SPENT": junk}):
                generate.run_claude("prompt", "text")
        self.assertEqual(len(self.engine.calls), 2)

    def test_unknown_job_id_is_not_a_cap(self):
        with mock.patch.dict(os.environ, {"LEOBOT_JOB": "missing1"}):
            generate.run_claude("prompt", "text")
        self.assertNotIn("for_job", job_ledger.rows(self.root)[0])


if __name__ == "__main__":
    unittest.main()
