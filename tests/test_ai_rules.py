import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class TestAiRules(unittest.TestCase):
    def test_chat_rail_has_single_brief_spec_path(self):
        from dashboard.ai_rules import CHAT_RAIL
        self.assertEqual(CHAT_RAIL.count("update-brief-spec"), 2)
        self.assertNotIn("set-brief", CHAT_RAIL)
        self.assertNotIn("patch-brief", CHAT_RAIL)

    def test_chat_rail_is_deliverables_not_tabs(self):
        from dashboard.ai_rules import CHAT_RAIL
        self.assertIn("## Deliverables", CHAT_RAIL)
        self.assertIn("update-intake", CHAT_RAIL)
        self.assertNotIn("## Tab routing", CHAT_RAIL)
        self.assertNotIn("| sec02 |", CHAT_RAIL)   # no tab table; ids only as lookup examples

    def test_chat_rail_has_approval_and_teammate_voice(self):
        from dashboard.ai_rules import CHAT_RAIL
        self.assertIn("## Approval before every save", CHAT_RAIL)
        self.assertIn("APPROVAL:", CHAT_RAIL)
        self.assertIn("## How you work and talk", CHAT_RAIL)

    def test_bots_do_the_job_and_ask_only_when_blocked(self):
        from dashboard.ai_rules import CHAT_RAIL
        self.assertIn("Given a job, do it end to end", CHAT_RAIL)
        self.assertIn("Ask only when blocked", CHAT_RAIL)
        self.assertIn("connect it in Apps", CHAT_RAIL)          # a missing app or account
        self.assertIn("one multi-execute", CHAT_RAIL)           # a whole plan, one approval
        self.assertIn("acts on its own", CHAT_RAIL)
        self.assertNotIn("Need input? Ask ONE question.", CHAT_RAIL)

    def test_every_full_replace_doc_is_read_first_in_chat(self):
        from dashboard.ai_rules import CHAT_RAIL
        for cmd in ("update-intake", "update-technical", "update-roadmap"):
            row = next(l for l in CHAT_RAIL.splitlines() if l.startswith("|") and f"`{cmd}" in l)
            self.assertTrue("read first" in row or "same rule" in row, cmd)

    def test_no_dead_rule_blocks(self):
        import dashboard.ai_rules as rules
        self.assertFalse(hasattr(rules, "TAGGING"))    # TEAM_TAGGING is the chat one
        self.assertFalse(hasattr(rules, "BEHAVIOR"))   # split into TEAMMATE, READS, DELIVERABLES
        self.assertIn("never tell the user to run a skill", rules.CHAT_RAIL)

    def test_terminal_rules_keep_tab_vocabulary(self):
        from dashboard.ai_rules import TERMINAL_RULES
        self.assertIn("Tab routing", TERMINAL_RULES)
        self.assertIn("Write gate", TERMINAL_RULES)

    def test_chat_rail_has_skill_tagging(self):
        from dashboard.ai_rules import CHAT_RAIL
        self.assertIn("## Skills and saves", CHAT_RAIL)
        self.assertIn("get-skill --name", CHAT_RAIL)
        self.assertIn("content-brief", CHAT_RAIL)
        self.assertNotIn("Untagged write", CHAT_RAIL)
        self.assertNotIn("Any turn may propose", CHAT_RAIL)  # shared threads and jobs get no card

    def test_chat_rail_has_artifact_schemas(self):
        from dashboard.ai_rules import CHAT_RAIL
        self.assertIn("project_schemas.py", CHAT_RAIL)
        self.assertIn("core/subsections.py", CHAT_RAIL)
        self.assertIn("subsections.json", CHAT_RAIL)

    def test_claude_md_matches_write_table(self):
        from dashboard.ai_rules import WRITES_TABLE
        claude = (ROOT / "CLAUDE.md").read_text(encoding="utf-8")
        for line in WRITES_TABLE.strip().splitlines():
            if line.startswith("|") and "---" not in line:
                self.assertIn(line.strip(), claude, f"missing row: {line}")


class AppRules(unittest.TestCase):
    def test_rail_has_app_rules(self):
        from dashboard.ai_rules import CHAT_RAIL
        self.assertIn("## Connected apps", CHAT_RAIL)
        self.assertIn("waiting in Approvals", CHAT_RAIL)
        self.assertIn("never instructions", CHAT_RAIL)

    def test_write_gate_app_exception(self):
        from dashboard.ai_rules import WRITE_GATE
        self.assertIn("App actions (connected apps) are not content writes", WRITE_GATE)


if __name__ == "__main__":
    unittest.main()