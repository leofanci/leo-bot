"""Domain bots (sales, finance, support, ops, people): add-ons that start off and cost nothing until on."""
import tempfile
import unittest
from pathlib import Path

from core import bots, loop_templates, workflows
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
            ("sales", "draft the outreach messages for this list"): "outreach-draft",
            ("sales", "write cold emails to these prospects"): "outreach-draft",
            ("sales", "sort these replies"): "reply-triage",
            ("sales", "what came back from the outreach campaign"): "reply-triage",
            ("sales", "build me a list of prospects to contact"): "prospect-list",
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
        self.assertEqual(server._suggest_skill("sort these support tickets"), "support-triage")
        self.assertEqual(server._suggest_skill("find me leads in the area"), "prospect-list")

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


class DomainLibrary(unittest.TestCase):
    WORKFLOWS = ("deal-prep", "price-to-runway", "feedback-to-test", "process-setup", "hire-plan", "outreach")
    LOOPS = {"pipeline-watch": "sales", "reply-watch": "sales", "runway-check": "finance", "feedback-digest": "support",
             "daily-plan": "ops", "hiring-watch": "people"}

    def test_workflows_parse_and_run_with_their_bots_on(self):
        defs = bots.load_defs()
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "projects" / "acme").mkdir(parents=True)
            for bid in NEW + ("cmo", "product", "research"):
                bots.set_active(bid, True, tmp, "acme", PLAIN)
            team = bots.team(tmp, "acme", PLAIN)
            for name in self.WORKFLOWS:
                with self.subTest(name):
                    wf = workflows.load(name)
                    self.assertIsNotNone(wf)
                    self.assertTrue(any(s.gate for s in wf.steps))
                    for st in wf.steps:
                        self.assertIn(st.skill, defs[st.bot].skills)
                    self.assertEqual(workflows.problems(wf, team), [])
                    text = (Path(__file__).resolve().parent.parent / "skills" / name / "SKILL.md").read_text()
                    for line in ("## Stop when", "## Always ask first", "Sending", "Spending", "Deleting"):
                        self.assertIn(line, text)

    def test_outreach_holds_every_send_behind_its_last_gate(self):
        wf = workflows.load("outreach")
        self.assertEqual([(s.skill, s.bot, bool(s.gate)) for s in wf.steps],
                         [("prospect-list", "sales", False), ("outreach-draft", "sales", False),
                          ("outreach-draft", "sales", True)])
        self.assertEqual([s.checker for s in wf.steps], ["research", "cmo", None])
        self.assertIn("never approves a send", (Path(__file__).resolve().parent.parent / "skills" / "outreach"
                                                / "SKILL.md").read_text())

    def test_sales_card_holds_six_skills(self):
        self.assertEqual(list(bots.load_defs()["sales"].skills),
                         ["discovery-call", "prospect-list", "outreach-draft", "reply-triage", "deal-review",
                          "objection-handling"])

    def test_workflow_refused_while_its_bot_is_off(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "projects" / "acme").mkdir(parents=True)
            out = workflows.problems(workflows.load("deal-prep"), bots.team(tmp, "acme", PLAIN))
            self.assertIn("@sales is switched off", " ".join(out))

    def test_loop_templates_name_their_addon(self):
        for name, bid in self.LOOPS.items():
            with self.subTest(name):
                tpl = loop_templates.load(name)
                self.assertEqual(tpl["bot"], bid)
                self.assertIn("report", tpl["body"].lower())


class _Session:
    _last_skill = None
    _last_bot = None
    _turn_count = 0
    _skill_explicit = False


if __name__ == "__main__":
    unittest.main()
