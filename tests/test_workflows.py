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

    def test_shipped_file_still_needs_skill(self):
        with self.assertRaises(ValueError):
            workflows.parse(wf_text(["1. bot: gtm | output: x | gate: no"]), "demo-flow")

    def test_parse_fills_lines(self):
        wf = workflows.parse(wf_text(TWO), "demo-flow")
        self.assertEqual(wf.lines, (TWO[0][3:], TWO[1][3:]))
        self.assertFalse(wf.plan)

    def test_parse_lines_optional_skill(self):
        steps = workflows.parse_lines(["bot: gtm | output: x"], need_skill=False)
        self.assertEqual(steps[0].skill, "")
        with self.assertRaises(ValueError):
            workflows.parse_lines(["bot: gtm | output: x"])
        with self.assertRaises(ValueError) as e:
            workflows.parse_lines([], need_skill=False)
        self.assertIn("the Steps list is empty", str(e.exception))

    def test_problems_skip_empty_skill(self):
        from core import plans
        wf = plans.parse("PLAN: T\nbrief: b\n1. bot: gtm | output: x")
        self.assertEqual(workflows.problems(wf, FakeTeam()), [])

    def test_shipped_template_loads(self):
        wf = workflows.load("idea-to-experiment")
        self.assertIsNotNone(wf)
        self.assertEqual(len(wf.steps), 4)
        self.assertEqual([s.bot for s in wf.steps], ["product", "research", "cmo", "product"])
        self.assertEqual(wf.steps[3].checker, "research")
        self.assertTrue(wf.steps[3].gate)
        self.assertEqual(workflows.problems(wf, venture_team()), [])
        self.assertTrue(workflows.problems(wf, bots.team()))  # HQ starts with cos only
        team = workflows.load("team-launch-plan")
        self.assertIsNotNone(team)
        self.assertEqual(len(team.steps), 1)
        step = team.steps[0]
        self.assertEqual((step.skill, step.bot, step.checker, step.gate, step.consult),
                         ("launch-plan", "gtm", "product", True, ("research", "cmo")))
        self.assertEqual(workflows.problems(team, venture_team()), [])
        debate = workflows.load("team-launch-debate")
        step = debate.steps[0]
        self.assertEqual((step.bot, step.checker, step.gate, step.team, step.debate, step.consult),
                         ("gtm", "product", True, True, 2, ("research", "cmo")))
        self.assertEqual(workflows.problems(debate, venture_team()), [])

    def test_parse_consult(self):
        base = "1. skill: experiment-design | bot: product | output: test | gate: no | checker: gtm"
        wf = workflows.parse(wf_text([base + " | consult: research, @cmo"]), "x")
        self.assertEqual(wf.steps[0].consult, ("research", "cmo"))
        self.assertEqual(workflows.parse(wf_text([base]), "x").steps[0].consult, ())
        for why, extra in {"maker": "product, cmo", "checker": "gtm", "repeat": "cmo, cmo",
                           "four": "research, cmo, cos, social", "empty": " , "}.items():
            with self.subTest(why), self.assertRaises(ValueError):
                workflows.parse(wf_text([base + " | consult: " + extra]), "x")

    def test_consultant_problems(self):
        got = workflows.problems(CONSULTED, FakeTeam(off={"cmo"}))
        self.assertEqual(got, ["Step 1 consult: @cmo is switched off."])
        wf = workflows.parse(wf_text([
            "1. skill: experiment-design | bot: product | output: test | gate: no | consult: nobody"]), "x")
        self.assertEqual(workflows.problems(wf, FakeTeam()), ["Step 1 consult: @nobody is not on the team."])

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



def out(cost=0.0, card=False, answer=None, stopped=False, failed=False, replies=(), chat="c1", asked=()):
    return {"chat": chat, "cost": cost, "card": card, "answer": answer, "stopped": stopped,
            "failed": failed, "replies": list(replies), "asked": list(asked)}


PLAIN = workflows.parse(wf_text([
    "1. skill: problem-validation | bot: product | output: verdict | gate: no",
    "2. skill: icp-research | bot: research | output: segments | gate: no"]), "demo-flow")
CHECKED = workflows.parse(wf_text([
    "1. skill: icp-research | bot: research | output: segments | gate: no | checker: product"]), "demo-flow")
GATED = workflows.parse(wf_text([
    "1. skill: experiment-design | bot: product | output: test | gate: approval | checker: research"]), "demo-flow")
GATED_SOLO = workflows.parse(wf_text([
    "1. skill: experiment-design | bot: product | output: test | gate: approval"]), "demo-flow")
CONSULTED = workflows.parse(wf_text([
    "1. skill: experiment-design | bot: product | output: test | gate: approval | checker: gtm"
    " | consult: research, cmo"]), "demo-flow")
TEAM = workflows.parse(wf_text([
    "1. skill: experiment-design | bot: product | output: test | gate: approval | checker: gtm"
    " | team: yes | debate: 2 | consult: research"]), "demo-flow")
TEAM_SOLO = workflows.parse(wf_text([
    "1. skill: experiment-design | bot: product | output: test | gate: approval | team: yes | debate: 1"]),
    "demo-flow")
DEBATED = workflows.parse(wf_text([
    "1. skill: experiment-design | bot: product | output: test | gate: approval"
    " | consult: research, cmo | debate: 1"]), "demo-flow")
CONSULTED_SOLO = workflows.parse(wf_text([
    "1. skill: experiment-design | bot: product | output: test | gate: approval | consult: research, cmo"]),
    "demo-flow")


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

    def test_consults_run_in_order_then_revise(self):
        run = self.run_of(CONSULTED)
        self.assertEqual(run["consulted"], 0)
        run = workflows.advance(CONSULTED, run, out(replies=[("product", "draft")]), 0)
        self.assertEqual((run["phase"], run["consulted"]), ("consult", 0))
        run = workflows.advance(CONSULTED, run, out(replies=[("research", "advice")]), 0)
        self.assertEqual((run["phase"], run["consulted"]), ("consult", 1))
        run = workflows.advance(CONSULTED, run, out(replies=[("cmo", "advice")]), 0)
        self.assertEqual((run["phase"], run["consulted"]), ("revise", 2))
        run = workflows.advance(CONSULTED, run, out(replies=[("product", "revised")]), 0)
        self.assertEqual(run["phase"], "check")
        run = workflows.advance(CONSULTED, run, out(replies=[("gtm", "PASS")]), 0)
        self.assertEqual(run["phase"], "save")
        run = workflows.advance(CONSULTED, run, out(card=True), 0)
        self.assertEqual((run["phase"], run["card_phase"]), ("approve", "save"))
        run = workflows.advance(CONSULTED, run, out(answer="approved"), 0)
        self.assertEqual((run["status"], run["consulted"]), ("done", 0))

    def test_weak_check_retries_revise_not_consults(self):
        run = dict(self.run_of(CONSULTED), phase="check", consulted=2)
        run = workflows.advance(CONSULTED, run, out(replies=[("gtm", "WEAK: no date")]), 0)
        self.assertEqual((run["phase"], run["retried"], run["notes"], run["consulted"]),
                         ("revise", True, "no date", 2))
        run = workflows.advance(CONSULTED, run, out(replies=[("product", "revised again")]), 0)
        self.assertEqual(run["phase"], "check")
        run = workflows.advance(CONSULTED, run, out(replies=[("gtm", "WEAK: still no date")]), 0)
        self.assertEqual((run["status"], run["phase"]), ("paused", "revise"))
        self.assertIn("still weak", run["why"])
        again = workflows.resume(run)
        self.assertEqual((again["status"], again["phase"], again["retried"]), ("running", "revise", False))

    def test_blocked_in_revise_retries_revise(self):
        run = dict(self.run_of(CONSULTED), phase="revise", consulted=2)
        run = workflows.advance(CONSULTED, run, out(replies=[("product", "BLOCKED: no intake")]), 0)
        self.assertEqual((run["phase"], run["retried"], run["status"]), ("revise", True, "running"))

    def test_blocked_in_stage_retries_stage(self):
        run = workflows.advance(CONSULTED, self.run_of(CONSULTED), out(replies=[("product", "BLOCKED: no intake")]), 0)
        self.assertEqual((run["phase"], run["retried"], run["notes"]), ("stage", True, "no intake"))

    def test_stage_retry_does_not_use_up_the_revise_retry(self):
        run = workflows.advance(CONSULTED, self.run_of(CONSULTED), out(replies=[("product", "BLOCKED: x")]), 0)
        run = workflows.advance(CONSULTED, run, out(replies=[("product", "draft")]), 0)
        self.assertEqual((run["phase"], run["retried"], run["notes"]), ("consult", False, ""))

    def test_consultant_blocked_is_ignored(self):
        run = dict(self.run_of(CONSULTED), phase="consult")
        run = workflows.advance(CONSULTED, run, out(replies=[("research", "BLOCKED: not my area")]), 0)
        self.assertEqual((run["phase"], run["consulted"], run["status"]), ("consult", 1, "running"))

    def test_card_in_consult_counts_after_approval(self):
        run = dict(self.run_of(CONSULTED), phase="consult")
        run = workflows.advance(CONSULTED, run, out(card=True), 0)
        self.assertEqual((run["phase"], run["card_phase"], run["consulted"]), ("approve", "consult", 0))
        run = workflows.advance(CONSULTED, run, out(answer="approved"), 0)
        self.assertEqual((run["phase"], run["consulted"], run["status"]), ("consult", 1, "running"))

    def test_card_in_stage_of_consult_step_goes_to_consults(self):
        run = workflows.advance(CONSULTED, self.run_of(CONSULTED), out(card=True), 0)
        run = workflows.advance(CONSULTED, run, out(answer="approved"), 0)
        self.assertEqual((run["phase"], run["consulted"]), ("consult", 0))

    def test_gate_without_checker_saves_in_revise(self):
        run = dict(self.run_of(CONSULTED_SOLO), phase="revise", consulted=2)
        run = workflows.advance(CONSULTED_SOLO, run, out(card=True), 0)
        self.assertEqual((run["phase"], run["card_phase"]), ("approve", "revise"))
        run = workflows.advance(CONSULTED_SOLO, run, out(answer="approved"), 0)
        self.assertEqual(run["status"], "done")

    def test_advance_is_pure(self):
        run = self.run_of(PLAIN)
        before = dict(run)
        workflows.advance(PLAIN, run, out(cost=1), 0)
        self.assertEqual(run, before)


class TeamSteps(unittest.TestCase):
    def run_of(self, wf):
        return workflows.new_run(wf, "an idea", "acme", "acme/general")

    def test_parse_team_and_debate_fields(self):
        step = TEAM.steps[0]
        self.assertEqual((step.team, step.debate, step.consult), (True, 2, ("research",)))
        plain = PLAIN.steps[0]
        self.assertEqual((plain.team, plain.debate), (False, 0))
        base = "1. skill: experiment-design | bot: product | output: test | gate: no"
        for extra in (" | team: yes | debate: 3", " | debate: 1", " | team: maybe", " | team: yes | debate: x"):
            with self.assertRaises(ValueError, msg=extra):
                workflows.parse(wf_text([base + extra]), "x")

    def test_team_hints_are_not_problems(self):
        class Off:
            def __init__(self, active):
                self.active = active
        team = type("T", (), {"get": lambda s, b: Off(b != "research"),
                              "skills_of": lambda s, b: ["experiment-design"]})()
        self.assertEqual(workflows.problems(TEAM, team), [])

    def test_team_stage_records_asked_and_goes_to_debate(self):
        run = workflows.advance(TEAM, self.run_of(TEAM), out(replies=[("product", "plan")],
                                                             asked=["research", "cmo", "research"]), 0)
        self.assertEqual((run["phase"], run["team"], run["panel"], run["round"], run["debated"]),
                         ("debate", ["research", "cmo"], ["research", "cmo"], 1, 0))

    def test_team_stage_alone_skips_debate(self):
        run = workflows.advance(TEAM, self.run_of(TEAM), out(replies=[("product", "plan")]), 0)
        self.assertEqual((run["phase"], run["team"]), ("check", []))
        run = workflows.advance(TEAM_SOLO, self.run_of(TEAM_SOLO), out(replies=[("product", "plan")]), 0)
        self.assertEqual(run["phase"], "save")

    def test_team_stage_blocked_retries_stage(self):
        run = workflows.advance(TEAM, self.run_of(TEAM), out(replies=[("product", "BLOCKED: no intake")],
                                                             asked=["cmo"]), 0)
        self.assertEqual((run["phase"], run["retried"], run["notes"]), ("stage", True, "no intake"))

    def test_debate_objection_then_answer_then_objectors_only(self):
        run = workflows.advance(TEAM, self.run_of(TEAM), out(replies=[("product", "plan")],
                                                             asked=["research", "cmo"]), 0)
        run = workflows.advance(TEAM, run, out(replies=[("research", "OBJECT: the date is too early")]), 0)
        self.assertEqual((run["phase"], run["debated"]), ("debate", 1))
        run = workflows.advance(TEAM, run, out(replies=[("cmo", "**AGREE**")]), 0)
        self.assertEqual((run["phase"], run["objections"]), ("answer", [["research", "the date is too early"]]))
        run = workflows.advance(TEAM, run, out(replies=[("product", "answered")]), 0)
        self.assertEqual((run["phase"], run["round"], run["panel"], run["debated"], run["objections"]),
                         ("debate", 2, ["research"], 0, []))
        run = workflows.advance(TEAM, run, out(replies=[("research", "AGREE")]), 0)
        self.assertEqual((run["phase"], run["open"]), ("check", ""))

    def test_debate_reply_without_verdict_counts_as_agree(self):
        run = dict(self.run_of(TEAM), phase="debate", team=["cmo"], panel=["cmo"], round=1)
        run = workflows.advance(TEAM, run, out(replies=[("cmo", "Looks fine to me.")]), 0)
        self.assertEqual((run["phase"], run["objections"]), ("check", []))

    def test_last_round_open_objections_reach_check(self):
        run = dict(self.run_of(TEAM), phase="debate", team=["cmo"], panel=["cmo"], round=2)
        run = workflows.advance(TEAM, run, out(replies=[("cmo", "OBJECT: no budget line")]), 0)
        self.assertEqual(run["phase"], "answer")
        run = workflows.advance(TEAM, run, out(replies=[("product", "kept as is")]), 0)
        self.assertEqual((run["phase"], run["open"]), ("check", "@cmo: no budget line"))

    def test_weak_check_on_team_step_retries_answer(self):
        run = dict(self.run_of(TEAM), phase="check", team=["cmo"], round=1)
        run = workflows.advance(TEAM, run, out(replies=[("gtm", "WEAK: no dates")]), 0)
        self.assertEqual((run["phase"], run["retried"], run["notes"]), ("answer", True, "no dates"))
        run = workflows.advance(TEAM, run, out(replies=[("product", "fixed")]), 0)
        self.assertEqual(run["phase"], "check")
        run = workflows.advance(TEAM, run, out(replies=[("gtm", "WEAK: still no dates")]), 0)
        self.assertEqual((run["phase"], run["status"]), ("answer", "paused"))

    def test_fixed_consult_step_with_debate_saves_in_save_phase(self):
        run = dict(self.run_of(DEBATED), phase="revise", consulted=2)
        run = workflows.advance(DEBATED, run, out(replies=[("product", "revised")]), 0)
        self.assertEqual((run["phase"], run["panel"]), ("debate", ["research", "cmo"]))
        run = workflows.advance(DEBATED, run, out(replies=[("research", "AGREE")]), 0)
        run = workflows.advance(DEBATED, run, out(replies=[("cmo", "AGREE")]), 0)
        self.assertEqual(run["phase"], "save")

    def test_team_stage_retry_does_not_use_up_the_answer_retry(self):
        run = workflows.advance(TEAM, self.run_of(TEAM), out(replies=[("product", "BLOCKED: thin")]), 0)
        run = workflows.advance(TEAM, run, out(replies=[("product", "plan")]), 0)
        self.assertEqual((run["phase"], run["retried"], run["notes"]), ("check", False, ""))
        run = workflows.advance(TEAM, run, out(replies=[("gtm", "WEAK: no dates")]), 0)
        self.assertEqual((run["phase"], run["status"]), ("answer", "running"))

    def test_run_problem_names_a_debater_switched_off(self):
        class B:
            def __init__(self, active):
                self.active = active
        team = type("T", (), {"get": lambda s, b: None if b == "nobody" else B(b != "cmo")})()
        run = dict(self.run_of(TEAM), phase="debate", panel=["research", "cmo"], debated=1)
        self.assertEqual(workflows.run_problem(run, team), "@cmo is switched off")
        self.assertIsNone(workflows.run_problem(dict(run, debated=0), team))
        self.assertIsNone(workflows.run_problem(dict(run, phase="check"), team))

    def test_objection_parser(self):
        self.assertEqual(workflows.objection("Fine.\n- **OBJECT:** the price is wrong"), "the price is wrong")
        self.assertIsNone(workflows.objection("AGREE"))
        self.assertIsNone(workflows.objection("AGREE\nOBJECT: but also this"))  # AGREE first wins

    def test_idle_run_reads_paused(self):
        run = dict(self.run_of(TEAM), updated=1000.0)
        self.assertEqual(workflows.idle(run, 1000.0 + workflows.IDLE_SECONDS - 1)["status"], "running")
        got = workflows.idle(run, 1000.0 + workflows.IDLE_SECONDS + 1)
        self.assertEqual((got["status"], got["why"]), ("paused", "No step ran for 30 minutes."))
        self.assertEqual(run["status"], "running")
        self.assertEqual(workflows.idle(dict(run, status="paused", why="x"), 10 ** 10)["why"], "x")


if __name__ == "__main__":
    unittest.main()


class PlanRunsAndSavedTemplates(unittest.TestCase):
    def setUp(self):
        from unittest import mock
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.saved = Path(self.tmp.name) / "team" / "workflows"
        p = mock.patch.object(workflows, "SAVED_DIR", self.saved)
        p.start()
        self.addCleanup(p.stop)

    def test_plan_run_rebuilds_from_record(self):
        from core import plans
        wf = plans.parse("PLAN: Hire a designer\nbrief: b\n1. bot: product | output: role brief")
        run = workflows.new_run(wf, "b", "acme", "acme/general")
        self.assertEqual((run["plan"], run["name"], run["title"], run["steps"]),
                         (True, "Hire a designer", "Hire a designer", list(wf.lines)))
        again = workflows.of_run(run)
        self.assertTrue(again.plan)
        self.assertEqual(again.steps, wf.steps)
        self.assertEqual(again.name, "Hire a designer")

    def test_file_run_has_no_plan_fields_and_loads_file(self):
        wf = workflows.load("idea-to-experiment")
        run = workflows.new_run(wf, "b", "acme", "acme/general")
        self.assertNotIn("plan", run)
        self.assertEqual(workflows.of_run(run).steps, wf.steps)

    def test_plan_title_never_loads_a_file(self):
        from unittest import mock
        with mock.patch.object(workflows, "load") as load:
            workflows.of_run({"plan": True, "name": "launch-plan", "steps": ["bot: gtm | output: x"]})
        load.assert_not_called()

    def test_broken_plan_record_is_none(self):
        self.assertIsNone(workflows.of_run({"plan": True, "name": "T", "steps": []}))
        self.assertIsNone(workflows.of_run({"plan": True, "name": "T", "steps": "bot: gtm"}))

    def test_save_and_load_template(self):
        from core import plans
        wf = plans.parse("PLAN: T\nbrief: Vendor pick\n1. bot: cto | output: shortlist")
        self.assertIsNone(workflows.save_template(wf, "vendor-pick"))
        got = workflows.load("vendor-pick")
        self.assertEqual((got.description, got.steps[0].skill, got.steps[0].bot), ("Vendor pick", "", "cto"))
        self.assertFalse(got.plan)
        self.assertIn("vendor-pick", workflows.names())
        self.assertIn("idea-to-experiment", workflows.names())

    def test_save_refusals(self):
        from core import plans
        wf = plans.parse("PLAN: T\nbrief: b\n1. bot: cto | output: x")
        self.assertEqual(workflows.save_template(wf, "plan"), "/plan is reserved.")
        self.assertEqual(workflows.save_template(wf, "launch-plan"), "/launch-plan already exists.")
        self.assertEqual(workflows.save_template(wf, "idea-to-experiment"), "/idea-to-experiment already exists.")
        self.assertIn("lowercase", workflows.save_template(wf, "Bad Name"))
        self.assertIn("lowercase", workflows.save_template(wf, "../x"))
        self.assertIsNone(workflows.save_template(wf, "vendor-pick"))
        self.assertEqual(workflows.save_template(wf, "vendor-pick"), "/vendor-pick already exists.")

    def test_shipped_wins_and_broken_saved_skipped(self):
        self.saved.mkdir(parents=True)
        for name in ("launch-plan", "team-launch-plan"):  # a shipped skill, a shipped workflow
            (self.saved / f"{name}.md").write_text("---\nkind: workflow\n---\n## Steps\n1. bot: cto | output: x\n")
        (self.saved / "broken.md").write_text("---\nkind: workflow\n---\nno steps\n")
        (self.saved / "notes.md").write_text("---\nname: notes\n---\n## Steps\n1. bot: cto | output: x\n")
        self.assertIsNone(workflows.load("launch-plan"))  # a shipped name is never shadowed
        self.assertNotEqual(workflows.load("team-launch-plan").steps[0].bot, "cto")
        self.assertIsNone(workflows.load("broken"))
        self.assertIsNone(workflows.load("notes"))  # not kind: workflow
        names = workflows.names()
        self.assertNotIn("broken", names)
        self.assertNotIn("launch-plan", names)
        self.assertEqual(names.count("team-launch-plan"), 1)

    def test_plan_name_reserved(self):
        from unittest import mock
        from core import skill_loads
        skills = Path(self.tmp.name) / "skills"
        for n in ("plan", "demo-skill"):
            (skills / n).mkdir(parents=True)
            (skills / n / "SKILL.md").write_text("---\nname: x\n---\nbody\n")
        with mock.patch.object(skill_loads, "SKILLS_DIR", skills):
            self.assertEqual(skill_loads.names(), ["demo-skill", "workspace"])
        self.assertIn("plan", workflows.RESERVED)
