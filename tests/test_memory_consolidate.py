"""Memory consolidation: one background fast-tier tidy-up past 300 entries, guarded and ledgered."""
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from core import memory
from dashboard import job_ledger
from dashboard import memory_consolidate as mc
from dashboard.engines.base import EngineRefused
import dashboard.server as server
from tests.test_cost_ledger import AskHarness
from tests import test_server_people as http


def fixture_text():
    """300 entries: 2 decisions, 1 preference, 197 distinct facts and 100 exact repeats."""
    lines = ["- 2026-09-01 [decision] B2B only", "- 2026-09-02 [preference] short replies",
             "- 2026-09-03 [decision] no paid ads"]
    facts = [f"- 2026-10-{i % 28 + 1:02d} [fact] fact {i}" for i in range(197)]
    return "\n".join(lines + facts + facts[:100]) + "\n", "\n".join(lines + facts) + "\n"


class Fake:
    kind = "api"
    supports = frozenset({"complete"})
    label = "Fake"
    id = "api:fake"

    def __init__(self, reply, calls):
        self.reply, self.calls = reply, calls

    def complete_metered(self, prompt, stdin_text, model):
        self.calls.append((prompt, stdin_text, model))
        if callable(self.reply):
            return self.reply(prompt, stdin_text, model)
        return {"text": self.reply, "model": "claude-haiku-5-5",
                "usage": {"in": 10, "cache_write": 0, "cache_read": 0, "out": 5}, "cost": 0.001}


class Consolidate(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "projects" / "acme").mkdir(parents=True)
        (self.root / "projects" / "demo").mkdir(parents=True)
        self.before_text, self.merged = fixture_text()
        self.path = memory.memory_path("cmo", "acme", self.root)
        self.path.parent.mkdir(parents=True)
        self.path.write_text(self.before_text)
        self.fake_calls = []
        refusal = mock.patch.object(mc.spend, "refusal", return_value=None)
        refusal.start()
        self.addCleanup(refusal.stop)

    def fake(self, reply):
        return Fake(reply, self.fake_calls)

    def test_done_writes_backup_ledger_and_index(self):
        status = mc.consolidate(self.root, "acme", "cmo", engine=self.fake(self.merged))
        self.assertEqual(status, "done")
        self.assertEqual(self.path.with_name("cmo.md.bak").read_text(), self.before_text)
        self.assertEqual(len(memory.entries("cmo", "acme", self.root)), 200)
        row = job_ledger.records(self.root)[-1]["messages"][0]
        self.assertEqual((row["job"], row["bot"], row["cost"]), ("memory-consolidate", "cmo", 0.001))
        self.assertEqual(self.fake_calls[0][2], "haiku")
        self.assertEqual(self.fake_calls[0][0], mc.PROMPT)
        self.assertEqual(self.fake_calls[0][1], self.before_text)
        self.assertEqual(memory.recall("cmo", "fact 150", "acme", self.root)[0].split("] ")[1], "fact 150")
        raw = (self.root / job_ledger.RELPATH).read_text()
        self.assertIn('"before": 300', raw)
        self.assertIn('"after": 200', raw)

    def test_dropping_a_pinned_line_is_rejected(self):
        bad = "\n".join(ln for ln in self.merged.splitlines() if "[decision] B2B" not in ln)
        self.assertEqual(mc.consolidate(self.root, "acme", "cmo", engine=self.fake(bad)), "rejected")
        self.assertEqual(self.path.read_text(), self.before_text)
        self.assertFalse(self.path.with_name("cmo.md.bak").exists())
        self.assertEqual(len(job_ledger.records(self.root)), 1)  # it still spent

    def test_invented_or_longer_output_is_rejected(self):
        before = memory.entries("cmo", "acme", self.root)
        promoted = self.merged.replace("[fact] fact 5\n", "[decision] fact 5\n")
        long_line = self.merged.replace("fact 7\n", "x" * 400 + "\n")
        for bad in (self.before_text + "- 2020-01-01 [fact] invented\n", "not a list", "",
                    self.merged.replace("2026-10-01", "2020-01-01"), promoted, long_line,
                    self.merged.replace("[fact] fact 9", "fact 9")):
            self.assertIsNone(mc.check(before, bad), bad[:80])

    def test_output_without_the_newest_entries_is_rejected(self):
        # A reply cut short loses the tail of the file, its newest entries. Merging keeps
        # the newest date and only a newer entry replaces a fact, so that date always survives.
        before = memory.entries("cmo", "acme", self.root)
        newest = max(e.date for e in before)
        cut = "\n".join(ln for ln in self.merged.splitlines() if newest not in ln) + "\n"
        self.assertIsNone(mc.check(before, cut))

    def test_repeated_pinned_line_is_rejected(self):
        before = memory.entries("cmo", "acme", self.root)
        self.assertIsNone(mc.check(before, self.merged + "- 2026-09-01 [decision] B2B only\n"))

    def test_reply_cut_at_the_token_limit_is_rejected(self):
        def cut(*_):
            return {"text": self.merged, "model": "claude-haiku-5-5", "usage": None, "cost": 0.001,
                    "truncated": True}
        self.assertEqual(mc.consolidate(self.root, "acme", "cmo", engine=self.fake(cut)), "rejected")
        self.assertEqual(self.path.read_text(), self.before_text)
        self.assertEqual(len(job_ledger.records(self.root)), 1)  # it still spent

    def test_code_fences_are_stripped(self):
        got = mc.check(memory.entries("cmo", "acme", self.root), "```\n" + self.merged + "```\n")
        self.assertEqual(len(got), 200)

    def test_file_changed_during_run_is_kept(self):
        def during(*_):
            memory.remember("cmo", "landed mid-run", "acme", self.root)
            return {"text": self.merged, "model": "claude-haiku-5-5", "usage": None, "cost": 0.001}
        status = mc.consolidate(self.root, "acme", "cmo", engine=self.fake(during))
        self.assertEqual(status, "file-changed")
        self.assertIn("landed mid-run", memory.read_memory("cmo", "acme", self.root, tail=0))

    def test_cap_reached_means_no_run(self):
        with mock.patch.object(mc.spend, "refusal", return_value="Paused"):
            self.assertEqual(mc.consolidate(self.root, "acme", "cmo", engine=self.fake(self.merged)), "capped")
        self.assertEqual(self.fake_calls, [])

    def test_engine_without_one_shot_is_skipped(self):
        eng = self.fake(self.merged)
        eng.supports = frozenset()
        self.assertEqual(mc.consolidate(self.root, "acme", "cmo", engine=eng), "no-engine")
        self.assertEqual(self.fake_calls, [])

    def test_engine_refusing_is_skipped(self):
        def refuse(*_):
            raise EngineRefused("no")
        self.assertEqual(mc.consolidate(self.root, "acme", "cmo", engine=self.fake(refuse)), "no-engine")
        self.assertEqual(job_ledger.records(self.root), [])

    def test_engine_error_writes_nothing(self):
        def boom(*_):
            raise RuntimeError("down")
        with mock.patch.object(mc.traceback, "print_exc"):
            self.assertEqual(mc.consolidate(self.root, "acme", "cmo", engine=self.fake(boom)), "failed")
        self.assertEqual(self.path.read_text(), self.before_text)

    def test_unpriced_cost_is_estimated(self):
        def free(*_):
            return {"text": self.merged, "model": "claude-haiku-5-5",
                    "usage": {"in": 1000, "cache_write": 0, "cache_read": 0, "out": 1000}, "cost": None}
        mc.consolidate(self.root, "acme", "cmo", engine=self.fake(free))
        row = job_ledger.records(self.root)[-1]["messages"][0]
        self.assertGreater(row["cost"], 0)
        self.assertTrue(row["metered"])

    def test_engine_is_picked_for_the_bot(self):
        with mock.patch.object(mc.registry, "pick", return_value=self.fake(self.merged)) as pick:
            self.assertEqual(mc.consolidate(self.root, "acme", "cmo"), "done")
        pick.assert_called_once_with(self.root, "acme", "cmo")

    def test_hq_memory(self):
        hq = memory.memory_path("cos", None, self.root)
        hq.parent.mkdir(parents=True)
        hq.write_text(self.before_text)
        self.assertEqual(mc.consolidate(self.root, "hq", "cos", engine=self.fake(self.merged)), "done")
        self.assertEqual(len(memory.entries("cos", None, self.root)), 200)

    def test_below_threshold_starts_nothing(self):
        self.assertIsNone(mc.maybe_start(self.root, "demo", "cmo"))
        self.assertEqual(mc.consolidate(self.root, "demo", "cmo", engine=self.fake(self.merged)), "below")

    def test_one_run_per_file_at_a_time(self):
        gate = threading.Event()

        def slow(root, ws, bot, **kw):
            gate.wait(5)
            return "done"
        with mock.patch.object(mc, "consolidate", side_effect=slow):
            first = mc.maybe_start(self.root, "acme", "cmo")
            self.assertIsNotNone(first)
            self.assertIsNone(mc.maybe_start(self.root, "acme", "cmo"))
            gate.set()
            first.join(5)

    def test_no_retry_until_the_file_grows(self):
        with mock.patch.object(mc, "consolidate", return_value="rejected") as run:
            mc.maybe_start(self.root, "acme", "cmo").join(5)
            self.assertIsNone(mc.maybe_start(self.root, "acme", "cmo"))
            for i in range(mc.RETRY_STEP):
                memory.remember("cmo", f"more {i}", "acme", self.root)
            mc.maybe_start(self.root, "acme", "cmo").join(5)
        self.assertEqual(run.call_count, 2)

    def tearDown(self):
        mc._LAST.clear()


class MemoryEndpoint(http.Base):
    def add(self, scope):
        with mock.patch.object(server.memory_consolidate, "maybe_start") as start:
            code, body, _ = http.request("POST", "/api/memory", {"ws": "acme", "bot": "cmo", "scope": scope,
                                                                 "text": "B2B only", "type": "decision"},
                                         owner=True)
        return code, body, start

    def test_owner_add_starts_a_run_for_that_scope(self):
        code, body, start = self.add("team")
        self.assertEqual(code, 200, body)
        self.assertRegex(body["lines"][0], r"\[decision\] B2B only$")
        start.assert_called_once()
        self.assertEqual(start.call_args[0], (self.root, "acme", "cmo"))
        self.assertIs(start.call_args[1]["shared"], True)
        self.assertEqual(self.add("")[2].call_args[1]["shared"], False)


class ServerHooks(AskHarness):
    def test_private_owner_turn_starts_a_run(self):
        with mock.patch.object(server.memory_consolidate, "maybe_start") as start:
            self.ask("@cmo hello", "hi")
        start.assert_called_once()
        self.assertEqual(start.call_args[0], (self.root, "acme", "cmo"))
        self.assertIs(start.call_args[1]["shared"], False)

    def test_shared_turn_never_starts_a_run(self):
        with mock.patch.object(server.memory_consolidate, "maybe_start") as start, \
                mock.patch.object(server, "_shared_room", return_value=True):
            self.ask("@cmo hello", "hi")
        start.assert_not_called()


if __name__ == "__main__":
    unittest.main()
