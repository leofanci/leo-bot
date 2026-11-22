"""workflows.py: workflow templates (skills with kind: workflow), the per-thread run record and
the pure step state machine the server drives."""
import tempfile
import unittest
from pathlib import Path

from core import bots, paths, workflows


def wf_text(steps, head="---\nname: demo-flow\nkind: workflow\ndescription: Workflow. Demo.\n---\n"):
    return head + "\nIntro line.\n\n## Steps\n" + "\n".join(steps) + "\n\n## Stop when\nDone.\n"


TWO = ["1. skill: problem-validation | bot: product | output: verdict | gate: no",
       "2. Skill: icp-research | Bot: research | Output: segments | Gate: approval | Checker: product"]


VENTURE = {"slug": "acme", "kind": "venture", "profiles": [{"slug": "profile-a"}]}


def venture_team():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "projects" / "acme").mkdir(parents=True)
        return bots.team(root, "acme", VENTURE)


class FakeBot:
    def __init__(self, active=True):
        self.active = active


class FakeTeam:
    def __init__(self, off=(), skills=None):
        self.off, self.extra = set(off), skills or {}

    def get(self, bot_id):
        if bot_id not in {"cos", "research", "cmo", "gtm", "product", "social", "cto"}:
            return None
        return FakeBot(bot_id not in self.off)

    def skills_of(self, bot_id):
        real = venture_team()
        return [] if bot_id in self.off else real.skills_of(bot_id) + self.extra.get(bot_id, [])


class Templates(unittest.TestCase):
    def test_parse_steps(self):
        wf = workflows.parse(wf_text(TWO), "demo-flow")
        self.assertEqual(len(wf.steps), 2)
        self.assertEqual(wf.steps[1].checker, "product")
        self.assertIsNone(wf.steps[0].checker)
        self.assertIs(wf.steps[0].gate, False)
        self.assertIs(wf.steps[1].gate, True)
        self.assertEqual(wf.steps[1].skill, "icp-research")
        self.assertNotIn("## Steps", wf.body)
        self.assertIn("## Stop when", wf.body)
        self.assertEqual(wf.description, "Workflow. Demo.")

    def test_parse_errors(self):
        base = "1. skill: problem-validation | bot: product | output: verdict | gate: no"
        cases = {
            "no steps heading": "---\nname: x\nkind: workflow\n---\nbody only\n",
            "zero steps": wf_text([]),
            "too many": wf_text([base.replace("1.", f"{i}.") for i in range(1, workflows.MAX_STEPS + 2)]),
            "missing skill": wf_text(["1. bot: product | output: verdict | gate: no"]),
            "missing bot": wf_text(["1. skill: problem-validation | output: verdict | gate: no"]),
            "missing output": wf_text(["1. skill: problem-validation | bot: product | gate: no"]),
            "bad gate": wf_text([base.replace("gate: no", "gate: maybe")]),
            "unknown skill": wf_text([base.replace("problem-validation", "no-such-skill")]),
            "workflow as step": wf_text([base.replace("problem-validation", "idea-to-experiment")]),
            "checker is maker": wf_text([base + " | checker: product"]),
        }
        for why, text in cases.items():
            with self.subTest(why), self.assertRaises(ValueError):
                workflows.parse(text, "x")

    def test_shipped_template_loads(self):
        wf = workflows.load("idea-to-experiment")
        self.assertIsNotNone(wf)
        self.assertEqual(len(wf.steps), 4)
        self.assertEqual([s.bot for s in wf.steps], ["product", "research", "cmo", "product"])
        self.assertEqual(wf.steps[3].checker, "research")
        self.assertTrue(wf.steps[3].gate)
        self.assertEqual(workflows.problems(wf, venture_team()), [])
        self.assertTrue(workflows.problems(wf, bots.team()))  # HQ starts with cos only

    def test_plain_skill_is_not_a_workflow(self):
        self.assertIsNone(workflows.load("positioning"))
        self.assertIsNone(workflows.load("../skills"))
        self.assertIsNone(workflows.load("no-such"))
        self.assertIn("idea-to-experiment", workflows.names())
        self.assertNotIn("positioning", workflows.names())

    def test_problems(self):
        wf = workflows.load("idea-to-experiment")
        got = workflows.problems(wf, FakeTeam(off={"cmo"}))
        self.assertEqual(len(got), 1)
        self.assertIn("@cmo", got[0])
        got = workflows.problems(wf, FakeTeam(off={"research"}))
        self.assertTrue(any("@research" in g for g in got))
        self.assertEqual(len(got), 2)  # step 2's maker and step 4's checker
        self.assertEqual(workflows.problems(wf, FakeTeam()), [])



def out(cost=0.0, card=False, answer=None, stopped=False, failed=False, replies=(), chat="c1"):
    return {"chat": chat, "cost": cost, "card": card, "answer": answer, "stopped": stopped,
            "failed": failed, "replies": list(replies)}


PLAIN = workflows.parse(wf_text([
    "1. skill: problem-validation | bot: product | output: verdict | gate: no",
    "2. skill: icp-research | bot: research | output: segments | gate: no"]), "demo-flow")
CHECKED = workflows.parse(wf_text([
    "1. skill: icp-research | bot: research | output: segments | gate: no | checker: product"]), "demo-flow")
GATED = workflows.parse(wf_text([
    "1. skill: experiment-design | bot: product | output: test | gate: approval | checker: research"]), "demo-flow")
GATED_SOLO = workflows.parse(wf_text([
    "1. skill: experiment-design | bot: product | output: test | gate: approval"]), "demo-flow")


class Runs(unittest.TestCase):
    def run_of(self, wf):
        return workflows.new_run(wf, "an idea", "acme", "dm-self")

    def test_store_round_trip_and_bad_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run = dict(self.run_of(PLAIN), chat="c1")
            self.assertIsNone(run.get("chat") and workflows.get(root, "c1"))
            workflows.save(root, run)
            self.assertEqual(workflows.get(root, "c1")["brief"], "an idea")
            self.assertTrue((root / "team" / "workflow_runs" / "c1.json").is_file())
            self.assertIsNone(workflows.get(root, "../x"))
            workflows.save(root, dict(run, chat="../x"))  # writes nothing
            self.assertFalse((root / "team" / "x.json").exists())
            workflows.save(root, dict(run, chat=None))
            workflows.drop(root, "c1")
            self.assertIsNone(workflows.get(root, "c1"))
            workflows.drop(root, "../x")
            (root / "team" / "workflow_runs" / "c2.json").write_text("{bad")
            self.assertIsNone(workflows.get(root, "c2"))

    def test_new_run_shape(self):
        run = self.run_of(PLAIN)
        self.assertEqual((run["name"], run["step"], run["phase"], run["status"], run["cost"]),
                         ("demo-flow", 0, "stage", "running", 0.0))
        self.assertIsNone(run["chat"])

    def test_plain_steps_advance_to_done(self):
        run = self.run_of(PLAIN)
        run = workflows.advance(PLAIN, run, out(cost=0.1), 0)
        self.assertEqual((run["step"], run["status"], run["phase"]), (1, "running", "stage"))
        run = workflows.advance(PLAIN, run, out(cost=0.2), 0)
        self.assertEqual((run["step"], run["status"]), (2, "done"))
        self.assertAlmostEqual(run["cost"], 0.3)

    def test_checker_pass(self):
        run = workflows.advance(CHECKED, self.run_of(CHECKED), out(replies=[("research", "x")]), 0)
        self.assertEqual(run["phase"], "check")
        run = workflows.advance(CHECKED, run, out(replies=[("product", "Looks fine.\nPASS")]), 0)
        self.assertEqual(run["status"], "done")

    def test_checker_weak_retries_once_then_pauses(self):
        run = workflows.advance(CHECKED, self.run_of(CHECKED), out(), 0)
        run = workflows.advance(CHECKED, run, out(replies=[("product", "WEAK: no segment size")]), 0)
        self.assertEqual((run["phase"], run["retried"], run["notes"], run["status"]),
                         ("stage", True, "no segment size", "running"))
        run = workflows.advance(CHECKED, run, out(), 0)
        run = workflows.advance(CHECKED, run, out(replies=[("product", "WEAK: still none")]), 0)
        self.assertEqual(run["status"], "paused")
        self.assertIn("still weak", run["why"])
        self.assertIn("still none", run["why"])

    def test_checker_without_verdict_is_weak(self):
        self.assertEqual(workflows.verdict("PASS"), (True, ""))
        self.assertEqual(workflows.verdict("ok\nPASS - good"), (True, ""))
        self.assertEqual(workflows.verdict("WEAK: thin"), (False, "thin"))
        self.assertFalse(workflows.verdict("Looks great!")[0])
        self.assertFalse(workflows.verdict("")[0])
        self.assertFalse(workflows.verdict("PASSABLE")[0])
        run = workflows.advance(CHECKED, self.run_of(CHECKED), out(), 0)
        run = workflows.advance(CHECKED, run, out(replies=[("product", "Great work")]), 0)
        self.assertEqual((run["phase"], run["retried"]), ("stage", True))

    def test_card_pauses_then_approve_advances(self):
        run = workflows.advance(GATED_SOLO, self.run_of(GATED_SOLO), out(card=True), 0)
        self.assertEqual((run["status"], run["phase"], run["card_phase"]), ("paused", "approve", "stage"))
        self.assertEqual(run["why"], "Waiting for your approval.")
        self.assertEqual(workflows.advance(GATED_SOLO, run, out(answer="rejected"), 0)["status"], "paused")
        self.assertEqual(workflows.advance(GATED_SOLO, run, out(answer="approved", card=True), 0)["status"],
                         "paused")
        self.assertEqual(workflows.advance(GATED_SOLO, run, out(answer=None), 0)["status"], "paused")
        self.assertEqual(workflows.advance(GATED_SOLO, run, out(answer="approved"), 0)["status"], "done")

    def test_card_in_checked_stage_goes_to_check_after_approval(self):
        run = workflows.advance(CHECKED, self.run_of(CHECKED), out(card=True), 0)
        run = workflows.advance(CHECKED, run, out(answer="approved"), 0)
        self.assertEqual((run["status"], run["phase"]), ("running", "check"))

    def test_approval_gate_with_checker_checks_before_save(self):
        run = workflows.advance(GATED, self.run_of(GATED), out(replies=[("product", "the test")]), 0)
        self.assertEqual(run["phase"], "check")
        run = workflows.advance(GATED, run, out(replies=[("research", "PASS")]), 0)
        self.assertEqual((run["phase"], run["status"]), ("save", "running"))
        run = workflows.advance(GATED, run, out(card=True), 0)
        self.assertEqual((run["phase"], run["card_phase"], run["status"]), ("approve", "save", "paused"))
        run = workflows.advance(GATED, run, out(answer="approved"), 0)
        self.assertEqual(run["status"], "done")

    def test_rejected_card_then_resume_redoes_turn(self):
        run = workflows.advance(GATED_SOLO, self.run_of(GATED_SOLO), out(card=True), 0)
        run = workflows.advance(GATED_SOLO, run, out(answer="rejected"), 0)
        run = workflows.resume(run)
        self.assertEqual((run["status"], run["phase"], run["card_phase"]), ("running", "stage", ""))

    def test_blocked_retries_once_then_pauses_with_reason(self):
        run = workflows.advance(PLAIN, self.run_of(PLAIN), out(replies=[("product", "Hmm.\nBLOCKED: no project yet")]), 0)
        self.assertEqual((run["status"], run["phase"], run["retried"], run["notes"]),
                         ("running", "stage", True, "no project yet"))
        run = workflows.advance(PLAIN, run, out(replies=[("product", "BLOCKED: still no project")]), 0)
        self.assertEqual(run["status"], "paused")
        self.assertIn("still no project", run["why"])
        self.assertEqual(run["step"], 0)

    def test_blocked_from_other_bot_ignored(self):
        run = workflows.advance(PLAIN, self.run_of(PLAIN), out(replies=[("cmo", "BLOCKED: mine")]), 0)
        self.assertEqual(run["step"], 1)

    def test_stopped_and_failed_pause(self):
        run = workflows.advance(CHECKED, self.run_of(CHECKED), out(stopped=True, cost=0.1), 0)
        self.assertEqual((run["status"], run["phase"], run["why"]), ("paused", "stage", "Stopped."))
        self.assertAlmostEqual(run["cost"], 0.1)
        run = workflows.advance(CHECKED, self.run_of(CHECKED), out(failed=True), 0)
        self.assertEqual((run["status"], run["why"]), ("paused", "The last step failed."))

    def test_cost_cap_pauses(self):
        run = workflows.advance(PLAIN, self.run_of(PLAIN), out(cost=1.2), 1.0)
        self.assertEqual((run["status"], run["step"]), ("paused", 1))
        self.assertEqual(run["why"], "Reached this run's cost cap.")
        done = workflows.advance(PLAIN, dict(run, status="running"), out(cost=1.0), 1.0)
        self.assertEqual(done["status"], "done")
        self.assertEqual(workflows.advance(PLAIN, self.run_of(PLAIN), out(cost=5), 0)["status"], "running")

    def test_resume(self):
        run = workflows.advance(CHECKED, self.run_of(CHECKED), out(stopped=True), 0)
        self.assertEqual(workflows.resume(run)["status"], "running")
        for status in ("stopped", "done"):
            self.assertEqual(workflows.resume(dict(run, status=status))["status"], status)
        weak = dict(run, status="paused", phase="stage", retried=True, notes="thin", why="Step 1 is still weak: thin")
        again = workflows.resume(weak)
        self.assertEqual((again["status"], again["phase"], again["retried"]), ("running", "stage", False))
        self.assertEqual(again["notes"], "thin")

    def test_advance_is_pure(self):
        run = self.run_of(PLAIN)
        before = dict(run)
        workflows.advance(PLAIN, run, out(cost=1), 0)
        self.assertEqual(run, before)


if __name__ == "__main__":
    unittest.main()
