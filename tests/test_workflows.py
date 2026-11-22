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


if __name__ == "__main__":
    unittest.main()
