"""Behavior evals run in the suite: the offline turn cases, the recall set, and the live
runner's grading and bookkeeping (never a real engine)."""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import leobot
from core import memory
from evals import live, recall, turns


class OfflineTurns(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.results = turns.run_all()

    def test_every_case_passes(self):
        bad = [f"{c['id']}: {'; '.join(f)}" for c, f in self.results if f and not c.get("gap")]
        self.assertEqual(bad, [], "\n" + turns.scorecard(self.results))

    def test_known_gaps_still_open(self):
        closed = [c["id"] for c, f in self.results if c.get("gap") and not f]
        self.assertEqual(closed, [], "a gap now passes: drop `gap` from these cases")

    def test_every_category_has_cases(self):
        cats = {c.get("category") for c, _ in self.results}
        self.assertTrue({"routing", "model", "gate", "apps", "shape", "memory"} <= cats)

    def test_a_wrong_expectation_fails(self):
        case = {"id": "x", "msg": "what should I look at today?", "expect": {"bot": "cmo", "prompt_has": "zzz"}}
        fails = turns.check(case, turns.run(case))
        self.assertEqual(len(fails), 2, fails)

    def test_typos_in_cases_are_refused(self):
        with self.assertRaises(ValueError):
            turns.load_check({"id": "x", "msg": "hi", "expect": {"bots": "cos"}})
        with self.assertRaises(ValueError):
            turns.load_check({"id": "x", "msg": "hi", "memroy": {}})



class Recall(unittest.TestCase):
    def setUp(self):
        self.data = recall.load()

    def test_fts_recall_under_the_vector_gate(self):
        found = recall.run(self.data)
        self.assertLessEqual(found["miss"], self.data["max_miss"], recall.scorecard(found, self.data["max_miss"]))
        hits, total = found["by_kind"]["keyword"]
        if memory.HAS_FTS5:
            self.assertEqual(hits, total, "every keyword query should find its entry")

    def test_scan_fallback_under_the_vector_gate(self):
        found = recall.run(self.data, fts=False)
        self.assertLessEqual(found["miss"], self.data["max_miss"], recall.scorecard(found, self.data["max_miss"]))


class RecallWords(unittest.TestCase):
    def test_stopwords_drop_unless_nothing_else(self):
        self.assertEqual(memory._words("what did the webinar teach us?"), ["webinar", "teach"])
        self.assertEqual(memory._words("what is it"), ["what", "is", "it"])

    def test_scan_stems(self):
        self.assertEqual(memory._stem("hiring"), "hir")
        self.assertEqual(memory._stem("deals"), "deal")
        self.assertEqual(memory._stem("news"), "new")
        self.assertEqual(memory._stem("uses"), "use")
        self.assertEqual(memory._stem("bus"), "bus")

    @unittest.skipUnless(memory.HAS_FTS5, "needs FTS5")
    def test_old_tokenizer_index_is_rebuilt(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "projects" / "acme" / "team" / "cmo.md"
            path.parent.mkdir(parents=True)
            path.write_text("- 2026-06-11 [fact] Average deal size is 1200 per year\n")
            db = root / memory.INDEX_RELPATH
            db.parent.mkdir(parents=True)
            conn = sqlite3.connect(db)
            conn.execute("CREATE VIRTUAL TABLE entries USING fts5(path UNINDEXED, workspace UNINDEXED,"
                         " scope UNINDEXED, bot UNINDEXED, date UNINDEXED, type UNINDEXED, line UNINDEXED,"
                         " text, tokenize='unicode61 remove_diacritics 2')")
            conn.execute("CREATE TABLE files(path TEXT PRIMARY KEY, mtime_ns INTEGER, size INTEGER)")
            conn.commit()
            conn.close()
            self.assertEqual(len(memory.recall("cmo", "how big are our deals?", "acme", root)), 1)
            sql = sqlite3.connect(db).execute("SELECT sql FROM sqlite_master WHERE name='entries'").fetchone()[0]
            self.assertIn("porter", sql)


class LiveGrading(unittest.TestCase):
    def test_live_cases_load(self):
        cases = live.load()
        self.assertGreaterEqual(len(cases), 5)
        self.assertEqual(len({c["id"] for c in cases}), len(cases))

    def test_grade_reads_the_real_reply(self):
        case = {"expect": {"bot": "cmo", "mentions": ["29"], "max_chars": 20, "max_bullets": 1,
                           "teammates_include": ["research"]}}
        good = {"bot": "cmo", "reply": "It is 29 a month.", "teammates": ["research"]}
        self.assertEqual(live.grade(case, good), [])
        bad = {"bot": "cos", "reply": "- one\n- two\n- three, and a long tail", "teammates": []}
        self.assertEqual(len(live.grade(case, bad)), 5)

    def test_reaches_counts_a_consult_or_a_handoff(self):
        case = {"expect": {"reaches": "research"}}
        self.assertEqual(live.grade(case, {"teammates": ["research"]}), [])
        self.assertEqual(live.grade(case, {"teammates": [], "handoff": "research"}), [])
        self.assertEqual(len(live.grade(case, {"teammates": ["cmo"], "handoff": None})), 1)

    def test_loaded_is_a_turn_outcome(self):
        from evals import turns
        self.assertIn("loaded", turns._EQUAL)
        self.assertIsNone(turns.run({"id": "x", "msg": "what is on today?"})["loaded"])

    def test_runs_saved_compared_and_pruned(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            self.assertIsNone(live.last_run(folder))
            first = {"when": "a", "engine": "e", "results": [
                {"id": "c1", "pass": True, "fails": [], "cost": 0.01, "seconds": 1}]}
            live.save_run(folder, first)
            last = live.last_run(folder)
            now = {"when": "b", "engine": "e", "results": [
                {"id": "c1", "pass": False, "fails": ["x"], "cost": 0.02, "seconds": 1}]}
            text = live.compare(now, last)
            self.assertIn("(was passing)", text)
            self.assertIn("0/1 passed", text)
            for i in range(live.KEEP_RUNS + 3):
                (folder / f"live-2000{i:04d}.json").write_text(json.dumps(first))
            live.save_run(folder, now)
            self.assertEqual(len(list(folder.glob("live-*.json"))), live.KEEP_RUNS)

    def test_declined_live_run_sends_nothing(self):
        with mock.patch("builtins.input", return_value="n"), \
             mock.patch.object(live.subprocess, "run") as run:
            self.assertEqual(live.main([]), 1)
        run.assert_not_called()


class Cli(unittest.TestCase):
    def test_live_flags_need_live(self):
        args = leobot.build_parser().parse_args(["eval", "--engine", "claude-code"])
        self.assertEqual(leobot.cmd_eval(args), 2)

    def test_live_flags_pass_through(self):
        args = leobot.build_parser().parse_args(["eval", "--live", "--case", "c1", "--yes"])
        with mock.patch.object(leobot.subprocess, "call", return_value=0) as call:
            self.assertEqual(leobot.cmd_eval(args), 0)
        self.assertEqual(call.call_args[0][0][-5:], ["evals", "--live", "--case", "c1", "--yes"])


if __name__ == "__main__":
    unittest.main()
