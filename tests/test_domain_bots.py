"""Domain bots (sales, finance, support, ops, people): add-ons that start off and cost nothing until on."""
import tempfile
import unittest
from pathlib import Path

from core import bots
import dashboard.server as server

NEW = ("sales", "finance", "support", "ops", "people")
PLAIN = {"slug": "acme", "kind": "venture", "profiles": [], "folders": []}


class DomainBots(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "projects" / "acme").mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def team(self, project=PLAIN):
        return bots.team(self.root, "acme", project) if project else bots.team(self.root)

    def test_new_bots_are_addons(self):
        defs = bots.load_defs()
        for bid in NEW:
            self.assertTrue(defs[bid].addon, bid)
            self.assertEqual(defs[bid].reports_to, "cos", bid)
        self.assertFalse(any(b.addon for b in defs.values() if b.id not in NEW))

    def test_new_bots_off_by_default(self):
        brand = dict(PLAIN, kind="brand")
        for t in (self.team(None), self.team(), self.team(brand)):
            self.assertEqual([b for b in NEW if t.get(b).active], [])

    def test_off_addon_skills_stay_off_every_menu(self):
        t = self.team()
        finance = bots.load_defs()["finance"].skills
        for b in t.bots.values():
            if b.active:
                self.assertFalse(set(t.skills_of(b.id)) & set(finance), b.id)
            self.assertNotIn("finance", b.covering)
        self.assertNotIn("finance", t.roster_line("cos"))

    def test_explicit_skill_of_an_off_addon_runs_as_cos(self):
        t = self.team()
        self.assertEqual(server._route_bot(t, "/unit-economics for acme", _Session())[:2],
                         ("cos", "unit-economics"))

    def test_off_addon_wording_never_moves_a_tagged_turn(self):
        t = self.team()
        self.assertEqual(server._route_bot(t, "@cmo what is our burn rate?", _Session())[:2],
                         ("cmo", None))
        self.assertEqual(server._route_dm(t, "how long is my runway?", _Session(), "cmo")[0], "cmo")
        bots.set_active("ops", True, self.root, "acme", PLAIN)
        self.assertEqual(server._route_bot(self.team(), "@cmo plan my week", _Session())[:2],
                         ("ops", "weekly-plan"))

    def test_switched_on_addon_owns_its_skills(self):
        bots.set_active("finance", True, self.root, "acme", PLAIN)
        t = self.team()
        self.assertEqual(t.owner("runway-plan"), "finance")
        self.assertIn("runway-plan", t.skills_of("finance"))

    def test_routes(self):
        cases = {
            ("sales", "what should I ask on the discovery call tomorrow"): "discovery-call",
            ("sales", "build me a list of prospects to contact"): "prospect-list",
            ("sales", "which deals in the pipeline should I chase"): "deal-review",
            ("sales", "they said it is too expensive, how do I answer that objection"): "objection-handling",
            ("finance", "how long is my runway"): "runway-plan",
            ("finance", "what are our unit economics, CAC and LTV"): "unit-economics",
            ("finance", "review this contract before I sign"): "contract-check",
            ("support", "sort these support tickets"): "support-triage",
            ("support", "write an FAQ from these questions"): "faq-builder",
            ("support", "what is the customer feedback telling us"): "feedback-synthesis",
            ("ops", "plan my week"): "weekly-plan",
            ("ops", "write an SOP for onboarding a client"): "process-doc",
            ("ops", "which tool should we use for invoicing, compare vendors"): "vendor-compare",
            ("people", "write a job description for a part-time designer"): "role-scope",
            ("people", "what should I ask in the interview"): "interview-plan",
            ("people", "plan the first 30 days for the new hire"): "onboarding-plan",
        }
        defs = bots.load_defs()
        for (bid, msg), want in cases.items():
            with self.subTest(msg=msg):
                self.assertEqual(server._suggest_skill(msg, among=list(defs[bid].skills)), want)
        # Existing asks keep their owners.
        self.assertEqual(server._suggest_skill("how much should we charge for the pro tier"), "pricing-strategy")
        self.assertEqual(server._suggest_skill("who are our competitors"), "competitor-scan")
        self.assertEqual(server._suggest_skill("plan the launch sequence"), "launch-plan")

    def test_every_new_skill_is_explicit(self):
        for bid in NEW:
            for s in bots.load_defs()[bid].skills:
                self.assertEqual(server._explicit_skill(f"/{s} go"), s)

    def test_default_cos_card_unchanged(self):
        t = self.team()
        menu = server._skill_menu(t, "cos")
        for bid in NEW:
            for s in bots.load_defs()[bid].skills:
                self.assertNotIn(s, menu)


class _Session:
    _last_skill = None
    _last_bot = None
    _turn_count = 0
    _skill_explicit = False


if __name__ == "__main__":
    unittest.main()
