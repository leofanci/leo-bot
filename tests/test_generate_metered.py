"""generate.py runs count against the caps: checked before, one job_ledger row after."""
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import generate
from dashboard import chat_store, fileops, job_ledger
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


if __name__ == "__main__":
    unittest.main()
