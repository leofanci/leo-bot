"""Behavior evals run in the suite: the offline turn cases, the recall set, and the live
runner's grading and bookkeeping (never a real engine)."""
import contextlib
import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import leobot
from core import memory
from evals import compare, live, recall, turns


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
        hits, total = found["by_kind"]["paraphrase"]
        self.assertGreaterEqual(hits, total - 1, "a question in other words should find its entry")

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

    def test_live_relay_is_served_by_the_eval_itself(self):
        # Codex and ACP reach osctl through the relay at h.server's port: in a live eval that must
        # be a server inside the run (its tokens), never the owner's real one on 8765.
        import urllib.error
        import urllib.request
        from evals import turns
        import dashboard.server as server
        with turns._relay_server() as srv:
            port = srv.server_address[1]
            self.assertNotEqual(port, 8765)
            req = urllib.request.Request(f"http://127.0.0.1:{port}{server.OS_RELAY_PATH}", data=b"{}",
                                         headers={"Content-Type": "application/json"})
            with self.assertRaises(urllib.error.HTTPError) as e:
                urllib.request.urlopen(req, timeout=5)
            self.assertIn(e.exception.code, (401, 403))  # served here, refused without a token

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

    def test_last_run_is_the_same_engines(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            (folder / "live-1.json").write_text(json.dumps({"when": "a", "engine": "codex", "results": []}))
            (folder / "live-2.json").write_text(json.dumps({"when": "b", "engine": "claude-code", "results": []}))
            self.assertEqual(live.last_run(folder, "codex")["when"], "a")
            self.assertEqual(live.last_run(folder)["when"], "b")
            self.assertIsNone(live.last_run(folder, "gemini"))

    def test_engine_all_runs_every_ready_engine(self):
        rows = [{"id": "claude-code", "ok": True}, {"id": "codex", "ok": True}, {"id": "gemini", "ok": False}]
        ran = []

        def fake_run(cmd, cwd, env):
            home = Path(env["LEOBOT_HOME"])
            ran.append(json.loads((home / "data" / "team" / "engines.json").read_text())["default"])
            out = Path(cmd[cmd.index("--out") + 1])
            out.write_text(json.dumps([{"id": "c1", "pass": ran[-1] == "claude-code", "fails": [],
                                        "cost": 0.0, "seconds": 1}]))
            return mock.Mock(returncode=0)
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(live, "_runs_dir", return_value=Path(tmp)), \
                mock.patch("dashboard.engines.registry.statuses", return_value=rows), \
                mock.patch.object(live.subprocess, "run", side_effect=fake_run), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            code = live.main(["--engine", "all", "--yes"])
        self.assertEqual(ran, ["claude-code", "codex"])
        self.assertEqual(code, 1)  # codex failed a case
        self.assertIn("claude-code: 1/1", out.getvalue())
        self.assertIn("codex: 0/1", out.getvalue())

    def test_engine_all_with_none_ready_sends_nothing(self):
        with mock.patch("dashboard.engines.registry.statuses", return_value=[{"id": "codex", "ok": False}]), \
                mock.patch.object(live.subprocess, "run") as run, \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(live.main(["--engine", "all", "--yes"]), 2)
        run.assert_not_called()

    def test_declined_live_run_sends_nothing(self):
        with mock.patch("builtins.input", return_value="n"), \
             mock.patch.object(live.subprocess, "run") as run:
            self.assertEqual(live.main([]), 1)
        run.assert_not_called()



SCORES = {"specific": 4, "grounded": 4, "risks": 3, "doable": 4, "consistent": 5}


class Compare(unittest.TestCase):
    def write(self, tmp, cases):
        path = Path(tmp) / "cases.json"
        path.write_text(json.dumps(cases))
        return path

    def test_load_checks_cases(self):
        cases = compare.load()
        self.assertEqual(len(cases), 3)
        for c in cases:
            self.assertEqual(set(c), {"id", "workflow", "brief", "intake"})
            self.assertIn("acme", (c["brief"] + c["intake"]).lower())
        good = cases[0]
        bad = {"unknown key": [dict(good, extra=1)],
               "missing brief": [{k: v for k, v in good.items() if k != "brief"}],
               "empty brief": [dict(good, brief=" ")],
               "duplicate id": [good, dict(good)],
               "unknown workflow": [dict(good, workflow="no-such-flow")],
               "not one step": [dict(good, workflow="idea-to-experiment")],
               "not a list": {"id": "x"}}
        with tempfile.TemporaryDirectory() as tmp:
            for why, data in bad.items():
                with self.subTest(why), self.assertRaises(ValueError):
                    compare.load(self.write(tmp, data))

    def test_drive_ticks_until_the_run_stops(self):
        sent = []
        replies = [[{"chat_id": "c1"}, {"workflow": {"chat": "c1", "next": True, "status": "running",
                                                     "label": "@cmo Advise on step 1 of /x", "why": ""}}],
                   [{"workflow": {"chat": "c1", "next": True, "status": "running", "label": "@gtm Revise",
                                  "why": ""}}],
                   [{"workflow": {"chat": "c1", "next": False, "status": "paused",
                                  "label": "", "why": "Waiting for your approval."}}]]

        def post(payload):
            sent.append(payload)
            return replies[len(sent) - 1]
        got = compare.drive(post, {"messages": [{"role": "user", "content": "/x go"}], "room_id": "acme/general"})
        self.assertEqual(got, {"chat": "c1", "status": "paused", "why": "Waiting for your approval.", "ticks": 2})
        self.assertEqual(len(sent), 3)
        self.assertNotIn("workflow", sent[0])
        for p in sent[1:]:
            self.assertEqual((p["workflow"], p["chat_id"], p["room_id"]), ("next", "c1", "acme/general"))
        self.assertEqual(sent[1]["messages"][0]["content"], "@cmo Advise on step 1 of /x")

        sent.clear()
        forever = [{"workflow": {"chat": "c1", "next": True, "status": "running", "label": "x", "why": ""}}]
        got = compare.drive(lambda p: sent.append(p) or forever, {"messages": []}, limit=4)
        self.assertEqual((got["ticks"], got["status"], len(sent)), (4, "running", 5))
        got = compare.drive(lambda p: [{"error": "internal error"}], {"messages": []})
        self.assertEqual((got["status"], got["ticks"]), ("failed", 0))

    def test_judge_prompt_is_blind_and_order_varies(self):
        case = compare.load()[0]
        plans = {"workflow": "Goal: 40 signups by 2027-01-15. @research said the segment is small.",
                 "single": "Goal: 30 signups. Ask @cmo for copy."}
        prompt = compare.judge_prompt(case, plans, ["single", "workflow"])
        for word in ("Plan A", "Plan B", case["brief"], case["intake"].strip()[:40], "40 signups"):
            self.assertIn(word, prompt)
        self.assertLess(prompt.index("30 signups"), prompt.index("40 signups"))
        for word in ("workflow", "single", "@research", "@cmo", "@gtm", "consult", "checker"):
            self.assertNotIn(word, prompt.lower() if word.islower() else prompt)
        orders = {tuple(compare.order_for(c["id"])) for c in compare.load()}
        self.assertEqual(len(orders), 2)
        self.assertEqual(compare.order_for("x"), compare.order_for("x"))

    def test_verdict_maps_back(self):
        order = ["single", "workflow"]
        text = "Here you go:\n" + json.dumps({"A": SCORES, "B": dict(SCORES, risks=5), "better": "B",
                                              "why": "B names its risks."}) + "\nthanks"
        got = compare.verdict(text, order)
        self.assertEqual(got["better"], "workflow")
        self.assertEqual(got["scores"], {"single": 20, "workflow": 22})
        self.assertEqual(got["why"], "B names its risks.")
        self.assertEqual(compare.verdict(json.dumps({"A": SCORES, "B": SCORES, "better": "tie", "why": ""}),
                                         order)["better"], "tie")
        for bad in ("no json", "{bad", json.dumps({"A": SCORES, "B": {"specific": 3}, "better": "A"}),
                    json.dumps({"A": SCORES, "B": dict(SCORES, risks=9), "better": "A"}),
                    json.dumps({"A": SCORES, "B": SCORES, "better": "C"}), None):
            with self.subTest(bad):
                self.assertEqual(compare.verdict(bad, order)["better"], "none")

    def test_paused_workflow_is_marked_not_judged(self):
        case = compare.load()[0]
        arms = {"workflow": {"status": "paused", "why": "Step 1 is still weak: x", "plan": "", "cost": 0.2},
                "single": {"status": "done", "why": "", "plan": "A plan.", "cost": 0.1}}
        judge = mock.Mock()
        got = compare.judge_case(case, arms, judge)
        judge.assert_not_called()
        self.assertEqual(got["better"], "none")
        self.assertEqual(got["arms"]["workflow"]["status"], "paused")
        self.assertIn("still weak", got["arms"]["workflow"]["why"])
        self.assertIn("no plan", got["why"])

        arms["workflow"]["plan"] = "Another plan."
        judge = mock.Mock(return_value=(json.dumps({"A": SCORES, "B": SCORES, "better": "A", "why": "w"}), 0.05))
        got = compare.judge_case(case, arms, judge)
        judge.assert_called_once()
        self.assertEqual(got["better"], got["order"][0])
        self.assertEqual(got["judge_cost"], 0.05)

    def test_summary_gain_line(self):
        def res(better, wc=0.3, sc=0.1):
            return {"id": "c", "better": better, "scores": {}, "why": "",
                    "arms": {"workflow": {"cost": wc}, "single": {"cost": sc}}, "judge_cost": 0.01}
        text = compare.summary([res("workflow"), res("workflow"), res("single")])
        self.assertIn("Gain: yes", text)
        self.assertIn("$0.9000", text)  # the workflow arm over 3 cases
        self.assertIn("$0.3000", text)
        self.assertIn("3.0x", text)
        self.assertIn("Gain: no", compare.summary([res("workflow"), res("tie"), res("none")]))
        self.assertIn("Gain: no", compare.summary([]))

    def test_results_saved_private_and_trimmed(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "evals"
            for i in range(compare.KEEP_RUNS + 2):
                path = compare.save_run(folder, {"when": str(i), "results": []})
            self.assertEqual(len(list(folder.glob("compare-*.json"))), compare.KEEP_RUNS)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_declined_compare_sends_nothing(self):
        with mock.patch("builtins.input", return_value="n"), \
                mock.patch.object(compare.subprocess, "run") as run, \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(compare.main([]), 1)
        run.assert_not_called()
        with mock.patch.object(compare.subprocess, "run") as run, contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(compare.main(["--case", "nope", "--yes"]), 2)
        run.assert_not_called()

    def test_main_runs_child_in_throwaway_home(self):
        seen = {}

        def fake_run(cmd, cwd, env):
            home = Path(env["LEOBOT_HOME"])
            seen["engine"] = json.loads((home / "data" / "team" / "engines.json").read_text())["default"]
            seen["cmd"] = cmd
            Path(cmd[cmd.index("--out") + 1]).write_text(json.dumps([
                {"id": "c1", "better": "workflow", "scores": {}, "why": "", "judge_cost": 0.0,
                 "arms": {"workflow": {"cost": 0.2}, "single": {"cost": 0.1}}}]))
            return mock.Mock(returncode=0)
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(compare, "_runs_dir", return_value=Path(tmp)), \
                mock.patch.object(compare.subprocess, "run", side_effect=fake_run), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            code = compare.main(["--engine", "codex", "--yes"])
            self.assertEqual(len(list(Path(tmp).glob("compare-*.json"))), 1)
        self.assertEqual(code, 0)
        self.assertEqual(seen["engine"], "codex")
        self.assertIn("--cases-file", seen["cmd"])
        self.assertIn("Gain: yes", out.getvalue())

    def test_evals_entry_routes_compare(self):
        import evals.__main__ as entry
        with mock.patch.object(compare, "main", return_value=0) as m:
            self.assertEqual(entry.main(["--compare", "--yes"]), 0)
        m.assert_called_once_with(["--yes"])


class Cli(unittest.TestCase):
    def test_live_flags_need_live(self):
        args = leobot.build_parser().parse_args(["eval", "--engine", "claude-code"])
        self.assertEqual(leobot.cmd_eval(args), 2)

    def test_live_flags_pass_through(self):
        args = leobot.build_parser().parse_args(["eval", "--live", "--case", "c1", "--yes"])
        with mock.patch.object(leobot.subprocess, "call", return_value=0) as call:
            self.assertEqual(leobot.cmd_eval(args), 0)
        self.assertEqual(call.call_args[0][0][-5:], ["evals", "--live", "--case", "c1", "--yes"])

    def test_compare_flags_pass_through(self):
        args = leobot.build_parser().parse_args(["eval", "--compare", "--cases-file", "x.json", "--yes"])
        with mock.patch.object(leobot.subprocess, "call", return_value=0) as call:
            self.assertEqual(leobot.cmd_eval(args), 0)
        cmd = call.call_args[0][0]
        self.assertEqual(cmd[1:4], ["-m", "evals", "--compare"])
        self.assertEqual(Path(cmd[cmd.index("--cases-file") + 1]), Path("x.json").resolve())
        self.assertEqual(cmd[-1], "--yes")
        with contextlib.redirect_stderr(io.StringIO()):
            for argv in (["eval", "--live", "--compare"], ["eval", "--cases-file", "x.json"],
                         ["eval", "--compare", "--engine", "all"]):
                self.assertEqual(leobot.cmd_eval(leobot.build_parser().parse_args(argv)), 2)

    def test_live_run_starts_outside_the_test_runner(self):
        # The live parent reads the real home's engine settings. Importing the turn harness
        # (which loads unittest.mock) before the paths module must not trip the test guard.
        import os
        import subprocess
        import sys
        with tempfile.TemporaryDirectory() as home:
            env = {k: v for k, v in os.environ.items() if k != "LEOBOT_HOME"}
            env["HOME"] = home
            proc = subprocess.run([sys.executable, "-m", "evals", "--live", "--case", "no-such-case"],
                                  cwd=str(Path(__file__).resolve().parent.parent), env=env,
                                  capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 2, proc.stderr[-500:])
        self.assertIn("Unknown case: no-such-case", proc.stderr)


if __name__ == "__main__":
    unittest.main()
