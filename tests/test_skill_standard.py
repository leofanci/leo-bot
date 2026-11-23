"""Skills written to the standard: inputs first, options, one recommendation, save after approval."""
import unittest
from pathlib import Path

from core import paths

# Skills rewritten or written to the standard (spec: "Standard", Skill).
REWRITTEN = (
    "competitor-scan", "icp-research", "pricing-strategy", "launch-plan", "positioning",
    # Domain bots (add-ons).
    "discovery-call", "prospect-list", "deal-review", "objection-handling", "outreach-draft", "reply-triage",
    "unit-economics", "runway-plan", "contract-check",
    "support-triage", "faq-builder", "feedback-synthesis",
    "weekly-plan", "process-doc", "vendor-compare",
    "role-scope", "interview-plan", "onboarding-plan",
)


def _text(name):
    return (Path(paths.CODE) / "skills" / name / "SKILL.md").read_text(encoding="utf-8")


class SkillStandard(unittest.TestCase):
    def test_rewritten_skills_follow_the_standard(self):
        for name in REWRITTEN:
            with self.subTest(skill=name):
                text = _text(name)
                self.assertTrue(text.startswith(f"---\nname: {name}\ndescription: "))
                self.assertIn("## Write gate", text)
                self.assertIn("intake", text)
                self.assertIn("Recommendation", text)
                lines = text.count("\n") + 1
                self.assertGreaterEqual(lines, 35, f"{name}: {lines} lines, too thin")
                self.assertLessEqual(lines, 130, f"{name}: {lines} lines, too long")


if __name__ == "__main__":
    unittest.main()
