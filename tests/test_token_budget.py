"""Token budget guards — keep per-turn chat prompt lean."""
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# chars / 4 ≈ tokens (conservative for English prose)
def est_tokens(text: str) -> int:
    return len(text) // 4


class TestTokenBudget(unittest.TestCase):
    def test_chat_rail_under_budget(self):
        from dashboard.ai_rules import CHAT_RAIL

        chars = len(CHAT_RAIL)
        tok = est_tokens(CHAT_RAIL)
        self.assertLessEqual(chars, 7200, f"CHAT_RAIL bloated: {chars} chars (~{tok} tok)")
        self.assertLessEqual(tok, 1750, f"CHAT_RAIL over ~1750 tok guard: ~{tok} tok")
        self.assertGreater(chars, 4000, "CHAT_RAIL unexpectedly tiny — rules missing?")

    def test_no_duplicate_tab_tables_in_rail(self):
        from dashboard.ai_rules import CHAT_RAIL

        self.assertEqual(CHAT_RAIL.count("## Deliverables"), 1)
        self.assertNotIn("PROJECT_TABS", CHAT_RAIL)
        self.assertNotIn("Project left panel (six tabs", CHAT_RAIL)

    def test_workspace_skill_file_lean(self):
        skill = (ROOT / "skills" / "workspace" / "SKILL.md").read_text(encoding="utf-8")
        self.assertLessEqual(len(skill), 2200, f"workspace SKILL.md should stay lean: {len(skill)} chars")

    def test_all_skill_files_under_budget(self):
        """workspace has its own stricter cap above (it's RAIL-duplicated and
        stub-injected on every tag). Every other skill only loads on-demand
        (once per tagged thread), but still shouldn't grow unbounded — this
        guard exists to catch that, not to force every skill to the bone."""
        skills_dir = ROOT / "skills"
        cap = 4500
        oversized = []
        for path in sorted(skills_dir.glob("*/SKILL.md")):
            if path.parent.name == "workspace":
                continue
            size = len(path.read_text(encoding="utf-8"))
            if size > cap:
                oversized.append(f"{path.parent.name}: {size} chars")
        self.assertFalse(oversized, f"skill file(s) over {cap}-char budget: {oversized}")

    def test_workspace_injected_stub_not_full_skill(self):
        from dashboard.server import _RAIL_COVERED_SKILL_STUBS, _load_skill_body

        self.assertIn("workspace", _RAIL_COVERED_SKILL_STUBS)
        body = _load_skill_body("workspace")
        self.assertLess(len(body), 400, "workspace injection should be stub-sized")
        self.assertIn("system prompt", body.lower())

    def test_write_gate_forbids_chat_body_dup(self):
        from dashboard.ai_rules import CHAT_RAIL

        self.assertIn("Do not paste full memo", CHAT_RAIL)
        self.assertIn("never echo", CHAT_RAIL.lower())

    def test_state_snapshot_is_index_only(self):
        from dashboard.server import state_snapshot
        import dashboard.db as db
        import index

        import tempfile
        import dashboard.server as server

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            index.build(root)
            dbs = {db, server.db}  # the dashboard.db and bare-imported db copies
            prev = {m: m.DB_PATH for m in dbs}
            try:
                for m in dbs:
                    m.DB_PATH = root / "database" / "data" / "os.db"
                snap = state_snapshot(db.tree())
            finally:
                for m, p in prev.items():
                    m.DB_PATH = p
        self.assertIn("index only", snap.lower())
        self.assertLess(len(snap), 4000, f"state_snapshot too large: {len(snap)} chars")


if __name__ == "__main__":
    unittest.main()
