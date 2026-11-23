"""Loop templates (core/loop_templates.py): a skill file with `kind: loop` made for one bot,
usable as a job's skill by that bot only; and the loop-design skill that guides a new loop."""
import unittest

from core import loop_templates, skill_loads, workflows
from dashboard import job_cards, server
from tests.test_job_cards import JobCardsBase

SHIPPED = ("weekly-review-loop", "signal-sweep", "pipeline-watch", "runway-check", "feedback-digest",
           "daily-plan", "hiring-watch")


def _text(bot="cos", drop=None):
    parts = "\n\n".join(f"## {p}\nsome text" for p in loop_templates.PARTS if p != drop)
    return f"---\nname: x-loop\nkind: loop\nbot: {bot}\nschedule: mon 09:00\ndescription: test\n---\n\n# X\n\n{parts}\n"


class Format(unittest.TestCase):
    def test_shipped_templates_are_complete(self):
        self.assertEqual(sorted(loop_templates.names()), sorted(SHIPPED))
        for name in SHIPPED:
            with self.subTest(name):
                text = (skill_loads.SKILLS_DIR / name / "SKILL.md").read_text(encoding="utf-8")
                self.assertEqual(loop_templates.problems(text), [])
                tpl = loop_templates.load(name)
                self.assertTrue(tpl["bot"] and tpl["schedule"] and tpl["description"])
                self.assertIn("3", tpl["body"].split("## State", 1)[1].split("\n## ", 1)[0])  # attempt cap

    def test_missing_part_reported(self):
        self.assertEqual(loop_templates.problems(_text()), [])
        self.assertEqual(loop_templates.problems(_text(drop="Self-check")), ["no ## Self-check part"])
        self.assertIn("unknown bot @nobody", loop_templates.problems(_text(bot="nobody")))

    def test_load_refuses_other_files(self):
        for name in ("weekly-review", "idea-to-experiment", "../skills", "", None):
            with self.subTest(name):
                self.assertIsNone(loop_templates.load(name))

    def test_template_is_explicit_skill(self):
        self.assertEqual(server._explicit_skill("@cos /signal-sweep look again"), "signal-sweep")
        self.assertEqual(server._explicit_skill("/loop-design for my inbox"), "loop-design")

    def test_template_not_a_workflow_step(self):
        with self.assertRaisesRegex(ValueError, "is a loop"):
            workflows._parse_step(1, "skill: weekly-review-loop | bot: cos | output: x")

    def test_job_prompt_has_attempt_cap(self):
        text = server._job_notepad({"notes": "", "stop": "done"})
        self.assertIn("3 tries", text)
        self.assertIn("Recent noise", text)

    def test_job_prompt_names_the_workflows(self):
        # A real run offered `/venture-intake`, a skill, as a workflow: the prompt names the real ones.
        text = server._job_notepad({"notes": ""})
        self.assertIn("/idea-to-experiment", text)
        self.assertNotIn("/venture-intake", text)

    def test_job_prompt_says_osctl_runs_plain(self):
        # A real job run wrapped osctl in `cd ...;` and `| tail`: refused, with nobody to approve.
        self.assertIn("no `cd`", server._job_notepad({"notes": ""}))

    def test_loop_design_routes(self):
        self.assertEqual(server._suggest_skill("set up a loop that checks my experiments every week"),
                         "loop-design")
        self.assertNotEqual(server._suggest_skill("how do we build a feedback loop with customers?"), "loop-design")
        self.assertIn("loop-design", skill_loads.names())
        body = skill_loads.body("loop-design")
        for needed in ("propose-job", "--stop", "report", "3", "LOOP DONE", "--job <id>", "No `APPROVAL:` line"):
            self.assertIn(needed, body)


class JobTarget(JobCardsBase):
    def test_job_may_use_its_bots_template(self):
        job_cards.check_target(self.root, "acme", "cos", "weekly-review-loop", self.projects)
        with self.assertRaisesRegex(ValueError, "made for @cos"):
            job_cards.check_target(self.root, "acme", "cmo", "weekly-review-loop", self.projects)
        with self.assertRaisesRegex(ValueError, "not one of @cmo's skills"):
            job_cards.check_target(self.root, "acme", "cmo", "weekly-review", self.projects)

    def test_routing_keeps_template_and_loop_design_with_the_right_bot(self):
        from core import bots
        team = bots.team(self.root, "acme", next(p for p in self.projects if p["slug"] == "acme"))
        # A template goes to the bot it was made for, not to the lead.
        self.assertEqual(server._route_bot(team, "/signal-sweep rivals", None)[:2], ("research", "signal-sweep"))
        # Any tagged bot designs its own loop: no handoff to the lead.
        self.assertEqual(server._route_bot(team, "@research set up a loop that sweeps rivals every day", None),
                         ("research", "loop-design", True, None))
        self.assertEqual(server._route_bot(team, "/loop-design an inbox watch", None)[:2], ("cos", "loop-design"))
        self.assertEqual(server._route_bot(team, "set up a loop that sweeps rivals every day", None, dm="research"),
                         ("research", "loop-design", True, None))


if __name__ == "__main__":
    unittest.main()
