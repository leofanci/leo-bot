"""plans.py: a bot's drafted PLAN block, cut out of its reply and parsed with the step-line parser."""
import unittest

from core import plans

BLOCK = ("PLAN: Pricing change\nbrief: Move acme to 29 per month for new customers from March.\n"
         "1. bot: research | output: three rival prices with sources | gate: no\n"
         "2. skill: launch-plan | bot: gtm | output: the plan | gate: approval | checker: product")


class Extract(unittest.TestCase):
    def test_extract_cuts_block_and_keeps_text(self):
        clean, block = plans.extract("Here is why.\n\n" + BLOCK + "\n")
        self.assertEqual(clean, "Here is why.")
        self.assertTrue(block.startswith("PLAN: Pricing change"))

    def test_extract_keeps_text_after_block(self):
        clean, block = plans.extract("Why.\n" + BLOCK + "\n\nAfter the plan.")
        self.assertEqual(clean, "Why.\n\nAfter the plan.")
        self.assertEqual(len(block.splitlines()), 4)

    def test_extract_fenced_and_bold(self):
        clean, block = plans.extract("Why.\n```\n**PLAN:** Pricing change\n" + BLOCK.split("\n", 1)[1] + "\n```")
        self.assertEqual(clean, "Why.")
        self.assertIsNotNone(block)
        self.assertEqual(plans.parse(block).name, "Pricing change")

    def test_extract_none(self):
        self.assertEqual(plans.extract("plain answer"), ("plain answer", None))
        self.assertEqual(plans.extract(""), ("", None))

    def test_only_first_block_counts_every_block_cut(self):
        second = BLOCK.replace("Pricing change", "Other")
        clean, block = plans.extract("A.\n" + BLOCK + "\n\n" + second)
        self.assertEqual(clean, "A.")
        self.assertIn("Pricing change", block)
        self.assertNotIn("Other", block)


class Parse(unittest.TestCase):
    def test_parse_plan(self):
        wf = plans.parse(plans.extract(BLOCK)[1])
        self.assertTrue(wf.plan)
        self.assertEqual(wf.name, "Pricing change")
        self.assertEqual(wf.description, "Move acme to 29 per month for new customers from March.")
        self.assertEqual(wf.body, "")
        self.assertEqual(wf.steps[0].skill, "")
        self.assertEqual(wf.steps[1].checker, "product")
        self.assertEqual(len(wf.lines), 2)
        self.assertTrue(wf.lines[0].startswith("bot: research"))

    def test_brief_prefix_case_insensitive_and_capped(self):
        wf = plans.parse("PLAN: T\nBrief: " + "x" * (plans.BRIEF_MAX + 50) + "\n1. bot: gtm | output: y")
        self.assertEqual(len(wf.description), plans.BRIEF_MAX)

    def test_title_capped(self):
        wf = plans.parse("PLAN: " + "t" * 200 + "\nbrief: b\n1. bot: gtm | output: y")
        self.assertEqual(len(wf.name), 80)

    def test_parse_errors(self):
        for block, why in (("PLAN:\nbrief: x\n1. bot: gtm | output: y", "no title"),
                           ("PLAN: T\n1. bot: gtm | output: y", "no brief"),
                           ("PLAN: T\nbrief: x", "the Steps list is empty"),
                           ("PLAN: T\nbrief: x\n1. bot: gtm | output: y | checker: gtm", "checker must be a different bot"),
                           ("PLAN: T\nbrief: x\n1. skill: nope-skill | bot: gtm | output: y", "unknown skill"),
                           ("PLAN: T\nbrief: x\n1. skill: idea-to-experiment | bot: gtm | output: y", "is a workflow")):
            with self.subTest(why), self.assertRaises(ValueError) as e:
                plans.parse(block)
            self.assertIn(why, str(e.exception))


if __name__ == "__main__":
    unittest.main()
