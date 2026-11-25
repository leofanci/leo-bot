"""workflow_turns.py + the server hook: a workflow runs one step per chat turn in a private thread."""
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from core import approvals, bots, plans, workflows
import dashboard.server as server
from dashboard import workflow_turns

CHAT = "0123abcd-0000-4000-8000-00000000000f"


def events(out):
    return [json.loads(line[6:]) for line in out.splitlines() if line.startswith("data: {")]


def wf_event(out):
    got = [e["workflow"] for e in events(out) if "workflow" in e]
    return got[-1] if got else None


class WorkflowTurns(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "projects" / "acme").mkdir(parents=True)
        self.tree = [{"slug": "acme", "name": "Acme", "kind": "venture", "type": "project",
                      "folders": [], "profiles": [], "products": []}]
        for p in (mock.patch.object(server.chat_store, "CHATS_DIR", self.root / "chats"),
                  mock.patch.object(workflows, "cost_cap", return_value=0.0)):
            p.start()
            self.addCleanup(p.stop)
        self.runs = []
        self.replies = []
        self.sess = self.make_session()

    def make_session(self):
        test = self

        class S:
            _started = True
            _turn_count = 1
            _last_skill = None
            _skill_explicit = False
            _last_bot = None
            session_id = CHAT

            def is_fresh(self):
                return False

            def note_skill(self, *a, **k):
                pass

            def note_bot(self, bot_id):
                self._last_bot = bot_id

            def ask(self, text, **kw):
                test.runs.append({"text": text, "readonly": bool(getattr(self, "readonly", False))})
                reply = test.replies.pop(0) if test.replies else "ok"
                yield ("delta", reply)
                yield ("done", {"tokens": 100, "cost": 0.01})
        return S()

    def ask(self, msg, replies=(), viewer=None, room="acme/general", **extra):
        self.replies = list(replies)
        sink = io.BytesIO()
        h = server.Handler.__new__(server.Handler)
        h.wfile = sink
        h.send_response = h.send_header = h.end_headers = lambda *a, **k: None
        h.server = mock.Mock(server_address=("127.0.0.1", 8765))
        ctx = [mock.patch.object(server, "ROOT", self.root),
               mock.patch.object(server.db, "tree", return_value=self.tree),
               mock.patch.object(server.db, "posts", return_value=[]),
               mock.patch.object(server, "get_chat_session", return_value=self.sess),
               mock.patch.object(server.chat_session.ChatSession, "ask", type(self.sess).ask)]
        for c in ctx:
            c.start()
        try:
            h._handle_ask({"messages": [{"role": "user", "content": msg}], "room_id": room,
                           "chat_id": CHAT, "context": "Current view: Home", **extra},
                          viewer or server.OWNER)
        finally:
            for c in reversed(ctx):
                c.stop()
        return sink.getvalue().decode(), server.chat_store.load(CHAT)

    def tick(self, replies=(), **extra):
        return self.ask("anything the browser sent", replies, workflow="next", **extra)

    def run_rec(self):
        return workflows.get(self.root, CHAT)

    def lead(self, out):
        return next(e["bot"]["id"] for e in events(out) if "bot" in e)

    def test_start_runs_step_one_and_emits_next(self):
        out, rec = self.ask("/idea-to-experiment acme sells widgets @cmo /positioning", ["The problem is real."])
        self.assertEqual(self.lead(out), "product")
        self.assertEqual(len(self.runs), 1)  # @cmo in the brief co-tagged nobody
        user = [m for m in rec["messages"] if m["role"] == "user"][-1]["content"]
        self.assertTrue(user.startswith("@product /problem-validation Step 1 of 4 of /idea-to-experiment: "))
        self.assertIn("acme sells widgets cmo positioning", user)
        self.assertIn("Do not save anything", self.runs[0]["text"])
        self.assertNotIn("Current view: Home", self.runs[0]["text"])
        ev = wf_event(out)
        self.assertEqual((ev["step"], ev["total"], ev["status"], ev["next"]), (2, 4, "running", True))
        self.assertTrue(ev["label"].startswith("@research /icp-research Step 2 of 4"))
        self.assertEqual(self.run_rec()["chat"], CHAT)

    def test_step_turn_holds_sends(self):
        minted = []
        real = server.apps_relay.TOKENS.mint
        with mock.patch.object(server.apps_relay.TOKENS, "mint", side_effect=lambda ctx: minted.append(ctx) or real(ctx)), \
                mock.patch.object(server.integrations, "api_key", return_value="k"), \
                mock.patch.object(server.integrations, "effective_grant", return_value={"gmail": True}):
            self.ask("hello there", ["ok"])
            self.ask("/idea-to-experiment acme sells widgets", ["The problem is real."])
        self.assertEqual([bool(c.get("hold_sends")) for c in minted], [False, True])

    def test_tick_runs_checker_then_retry_then_pause(self):
        self.ask("/idea-to-experiment an idea", ["ok"])
        out, _ = self.tick(["Segments: small shops."])
        self.assertEqual(self.lead(out), "research")
        self.assertEqual(wf_event(out)["phase"], "check")
        out, _ = self.tick(["WEAK: no segment size"])
        self.assertEqual(self.lead(out), "product")
        self.assertIn("PASS or WEAK", self.runs[-1]["text"])
        self.tick(["Segments again."])
        self.assertIn("no segment size", self.runs[-1]["text"])  # the retry carries the notes
        out, _ = self.tick(["WEAK: still none"])
        ev = wf_event(out)
        self.assertEqual((ev["status"], ev["next"]), ("paused", False))
        self.assertIn("still weak", ev["why"])

    def test_gate_card_pauses_and_approve_advances(self):
        self.ask("/idea-to-experiment an idea", ["ok"])
        run = dict(self.run_rec(), step=3, phase="stage")
        workflows.save(self.root, run)
        out, _ = self.tick(["Test: a landing page."])
        self.assertIn("Do not save anything", self.runs[-1]["text"])  # approval gate with a checker
        self.tick(["PASS"])
        self.assertEqual(self.run_rec()["phase"], "save")
        out, _ = self.tick(["Saving it.\nAPPROVAL: save the experiment"])
        self.assertIn("APPROVAL: line", self.runs[-1]["text"])
        ev = wf_event(out)
        self.assertEqual((ev["status"], ev["phase"], ev["next"]), ("paused", "approve", False))
        self.assertTrue(approvals.pending(self.root, "acme"))
        n = len(self.runs)
        out, _ = self.tick()
        self.assertIn(workflow_turns.ANSWER_CARD, out)
        self.assertEqual(len(self.runs), n)  # no model turn
        out, _ = self.ask("Yes, approved.", ["Saved."], approval="approved")
        ev = wf_event(out)
        self.assertEqual((ev["status"], ev["next"]), ("done", False))

    def test_rejected_card_then_continue_redoes_the_turn(self):
        self.ask("/idea-to-experiment an idea", ["ok"])
        workflows.save(self.root, dict(self.run_rec(), step=3, phase="save"))
        self.tick(["APPROVAL: save the experiment"])
        out, _ = self.ask("No.", ["Understood."], approval="rejected")
        self.assertEqual(wf_event(out)["status"], "paused")
        out, _ = self.tick(["APPROVAL: save the experiment, smaller"])
        self.assertEqual(self.lead(out), "product")
        self.assertEqual(wf_event(out)["phase"], "approve")

    def test_ordinary_message_never_advances(self):
        self.ask("/idea-to-experiment an idea", ["ok"])
        before = self.run_rec()
        out, _ = self.ask("what do you think so far?", ["Looks good."])
        self.assertIsNone(wf_event(out))
        self.assertEqual(self.run_rec()["step"], before["step"])
        workflows.save(self.root, dict(before, status="paused", phase="approve", card_phase="stage"))
        out, _ = self.ask("hmm, tell me more", ["Sure."])
        self.assertIsNone(wf_event(out))
        self.assertEqual(self.run_rec()["status"], "paused")

    def test_shared_room_and_person_refused(self):
        with mock.patch.object(server, "_shared_room", return_value=True):
            out, _ = self.ask("/idea-to-experiment an idea", ["ok"])
        self.assertIn(workflow_turns.PRIVATE_ONLY, out)
        self.assertEqual(self.runs, [])
        self.assertIsNone(self.run_rec())
        got = workflow_turns.prepare({"messages": [{"role": "user", "content": "/idea-to-experiment x"}]},
                                     {"kind": "person", "id": "p1"}, "acme", None, None, True, self.root)
        self.assertEqual(got, workflow_turns.PRIVATE_ONLY)
        got = workflow_turns.prepare({"messages": [{"role": "user", "content": "x"}], "workflow": "next"},
                                     {"kind": "job"}, "acme", None, None, True, self.root)
        self.assertEqual(got, workflow_turns.PRIVATE_ONLY)
        body = {"messages": [{"role": "user", "content": "hello"}]}
        self.assertEqual(workflow_turns.prepare(body, {"kind": "job"}, "acme", None, None, True, self.root),
                         (body, None))

    def test_problem_refuses_start(self):
        bots.set_active("cmo", False, self.root, "acme", self.tree[0])
        out, _ = self.ask("/idea-to-experiment an idea", ["ok"])
        self.assertIn("@cmo", out)
        self.assertIn("Switch them on in this workspace's Team", out)
        self.assertEqual(self.runs, [])
        self.assertIsNone(self.run_rec())

    def test_bot_switched_off_mid_run_pauses(self):
        self.ask("/idea-to-experiment an idea", ["ok"])
        bots.set_active("research", False, self.root, "acme", self.tree[0])
        out, _ = self.tick()
        self.assertIn("@research", out)
        self.assertEqual(self.run_rec()["status"], "paused")
        self.assertEqual(len(self.runs), 1)

    def test_second_start_in_same_thread_refused(self):
        self.ask("/idea-to-experiment an idea", ["ok"])
        out, _ = self.ask("/idea-to-experiment another", ["ok"])
        self.assertIn("already runs", out)
        self.assertEqual(len(self.runs), 1)

    def test_tick_without_run_and_at_cost_cap(self):
        out, _ = self.tick()
        self.assertIn("no workflow", out)
        self.ask("/idea-to-experiment an idea", ["ok"])
        with mock.patch.object(workflows, "cost_cap", return_value=0.001):
            out, _ = self.tick()
        self.assertIn("cost cap", out)
        self.assertEqual(len(self.runs), 1)

    def test_stopped_client_pauses_the_run(self):
        self.ask("/idea-to-experiment an idea", ["ok"])

        def ask(self_, text, **kw):
            yield ("delta", "half")
            raise BrokenPipeError
        with mock.patch.object(type(self.sess), "ask", ask):
            self.tick()
        run = self.run_rec()
        self.assertEqual((run["status"], run["why"], run["step"]), ("paused", "Stopped.", 1))

    def test_stop_route_and_delete_drop_the_run(self):
        self.ask("/idea-to-experiment an idea", ["ok"])
        got = workflow_turns.stop(self.root, CHAT)
        self.assertEqual((got["status"], got["next"]), ("stopped", False))
        out, _ = self.tick()
        self.assertIn("no workflow", out)
        self.assertEqual(workflow_turns.current(self.root, CHAT)["status"], "stopped")
        self.assertIsNone(workflow_turns.stop(self.root, "nope"))
        self.assertEqual(workflow_turns.current(self.root, "../x"), {})
        # a stopped run lets a new one start in the same thread
        self.ask("/idea-to-experiment again", ["ok"])
        self.assertEqual(self.run_rec()["status"], "running")
        sink = io.BytesIO()
        h = server.Handler.__new__(server.Handler)
        h.wfile = sink
        h.path = f"/api/chats/{CHAT}/delete"
        sent = []
        h._send = lambda code, obj: sent.append((code, obj))
        with mock.patch.object(server, "ROOT", self.root), \
                mock.patch.object(server.Handler, "_read_json", return_value={}, create=True), \
                mock.patch.object(server.Handler, "_authorized", return_value=True, create=True):
            try:
                h.do_POST()
            except Exception:  # noqa: BLE001 - the route is what matters, not the socket
                pass
        self.assertIsNone(self.run_rec())

    def test_stop_or_delete_during_a_step_wins(self):
        self.ask("/idea-to-experiment an idea", ["ok"])
        test = self

        def ask(self_, text, **kw):
            workflow_turns.stop(test.root, CHAT)  # Stop workflow pressed while the step runs
            yield ("delta", "done")
            yield ("done", {"tokens": 1, "cost": 0.01})
        with mock.patch.object(type(self.sess), "ask", ask):
            out, _ = self.tick()
        self.assertEqual(self.run_rec()["status"], "stopped")
        self.assertEqual(wf_event(out)["next"], False)

        self.ask("/idea-to-experiment again", ["ok"])

        def ask2(self_, text, **kw):
            workflows.drop(test.root, CHAT)  # the thread was deleted while the step runs
            yield ("delta", "done")
            yield ("done", {"tokens": 1, "cost": 0.01})
        with mock.patch.object(type(self.sess), "ask", ask2):
            self.tick()
        self.assertIsNone(self.run_rec())

    def test_stop_during_step_one_wins(self):
        test, seen = self, []

        def ask(self_, text, **kw):
            seen.append(workflows.get(test.root, CHAT))  # saved before the model runs
            workflow_turns.stop(test.root, CHAT)  # Stop workflow pressed during step 1
            yield ("delta", "done")
            yield ("done", {"tokens": 1, "cost": 0.01})
        with mock.patch.object(type(self.sess), "ask", ask):
            out, _ = self.ask("/idea-to-experiment an idea")
        self.assertEqual(seen[0]["status"], "running")
        self.assertEqual(self.run_rec()["status"], "stopped")
        self.assertEqual(wf_event(out)["next"], False)
        live = [e["workflow_live"] for e in events(out) if "workflow_live" in e]
        self.assertEqual((live[0]["step"], live[0]["status"], live[0]["next"]), (1, "running", False))

    def consult_flow(self):
        """A one-step workflow with two consultants and a checker, served as /demo-flow."""
        text = ("---\nname: demo-flow\nkind: workflow\ndescription: Workflow. Demo.\n---\n\nIntro.\n\n"
                "## Steps\n1. skill: experiment-design | bot: product | output: a test | gate: approval"
                " | checker: gtm | consult: research, cmo\n\n## Stop when\nDone.\n")
        wf = workflows.parse(text, "demo-flow")
        real = workflows.load
        p = mock.patch.object(workflows, "load", side_effect=lambda n: wf if n == "demo-flow" else real(n))
        p.start()
        self.addCleanup(p.stop)
        return wf

    def test_consult_and_revise_labels_and_instructions(self):
        wf = self.consult_flow()
        run = workflows.new_run(wf, "an idea", "acme", "acme/general")
        stage = workflow_turns._instructions(run, wf)
        self.assertIn("@research, then @cmo", stage)
        self.assertIn("Do not save anything", stage)
        run = dict(run, phase="consult")
        self.assertEqual(workflow_turns.label(run, wf), "@research Advise on step 1 of /demo-flow")
        body, ctx = workflow_turns._turn({}, run, wf)
        self.assertEqual(ctx["route"], ("research", None, False, None))
        self.assertEqual(ctx["kind"], "consult")
        self.assertIn("Do not rewrite", body["context"])
        self.assertIn("Do not save anything", body["context"])
        self.assertEqual(workflow_turns.label(dict(run, consulted=1), wf), "@cmo Advise on step 1 of /demo-flow")
        run = dict(run, phase="revise", consulted=2)
        self.assertEqual(workflow_turns.label(run, wf), "@product Revise step 1 of /demo-flow")
        body, ctx = workflow_turns._turn({}, run, wf)
        self.assertEqual(ctx["route"], ("product", "experiment-design", True, None))
        self.assertIn("advice", body["context"])
        self.assertIn("Do not save anything", body["context"])  # the checker comes first
        self.assertIn("stands alone", body["context"])

    def test_draft_and_advice_are_kept_short(self):
        # Output tokens are most of a consult round's cost: the draft is a skeleton the revise
        # turn writes out in full, and each piece of advice is capped.
        wf = self.consult_flow()
        run = workflows.new_run(wf, "an idea", "acme", "acme/general")
        self.assertIn(f"under {workflow_turns.DRAFT_WORDS} words", workflow_turns._instructions(run, wf))
        self.assertIn(f"under {workflow_turns.ADVICE_WORDS} words",
                      workflow_turns._instructions(dict(run, phase="consult"), wf))
        revise = workflow_turns._instructions(dict(run, phase="revise", consulted=2), wf)
        self.assertNotIn(f"under {workflow_turns.DRAFT_WORDS} words", revise)
        self.assertIn("in full", revise)

    def test_revise_checks_contradictions_and_fit_itself(self):
        # In the revise turn, not the checker: a checker rule sent every run through a second
        # revise and check, about 50% more cost for the same plans.
        wf = self.consult_flow()
        run = workflows.new_run(wf, "an idea", "acme", "acme/general")
        text = workflow_turns._instructions(dict(run, phase="revise", consulted=2), wf)
        self.assertIn("agrees", text)
        self.assertIn("fits the team", text)
        self.assertNotIn("fits the team", workflow_turns._instructions(dict(run, phase="check"), wf))

    def test_step_without_consults_has_no_draft_cap(self):
        text = ("---\nname: demo-solo\nkind: workflow\ndescription: Workflow. Demo.\n---\n\nIntro.\n\n"
                "## Steps\n1. skill: experiment-design | bot: product | output: a test\n\n## Stop when\nDone.\n")
        wf = workflows.parse(text, "demo-solo")
        run = workflows.new_run(wf, "an idea", "acme", "acme/general")
        self.assertNotIn(f"under {workflow_turns.DRAFT_WORDS} words", workflow_turns._instructions(run, wf))

    def test_full_consult_round_runs_through_the_server(self):
        self.consult_flow()
        out, _ = self.ask("/demo-flow an idea", ["Draft test."])
        self.assertEqual(self.lead(out), "product")
        out, _ = self.tick(["Research advice."])
        self.assertEqual(self.lead(out), "research")
        out, _ = self.tick(["Marketing advice."])
        self.assertEqual(self.lead(out), "cmo")
        self.assertEqual(wf_event(out)["phase"], "revise")
        out, _ = self.tick(["Revised test."])
        self.assertEqual(self.lead(out), "product")
        self.assertIn("advice", self.runs[-1]["text"])
        self.assertEqual(wf_event(out)["phase"], "check")

    def test_check_turn_is_read_only_and_raises_no_card(self):
        self.consult_flow()
        minted = []
        real = server.os_relay.TOKENS.mint
        apps = []
        real_apps = server.apps_relay.TOKENS.mint
        self.ask("/demo-flow an idea", ["Draft test."])
        workflows.save(self.root, dict(self.run_rec(), phase="consult"))
        with mock.patch.object(server.os_relay.TOKENS, "mint", side_effect=lambda c: minted.append(c) or real(c)), \
                mock.patch.object(server.apps_relay.TOKENS, "mint", side_effect=lambda c: apps.append(c) or real_apps(c)), \
                mock.patch.object(server.integrations, "api_key", return_value="k"), \
                mock.patch.object(server.integrations, "effective_grant", return_value={"gmail": "act"}):
            out, _ = self.tick(["Advice.\nAPPROVAL: create-memo the plan"])
            self.assertFalse([e for e in events(out) if "approval" in e])
            self.assertFalse(approvals.pending(self.root, "acme"))
            self.assertEqual(self.run_rec()["consulted"], 1)
            workflows.save(self.root, dict(self.run_rec(), phase="check", consulted=2))
            out, _ = self.tick(["PASS\nAPPROVAL: create-memo the plan"])
        self.assertFalse([e for e in events(out) if "approval" in e])
        self.assertFalse(approvals.pending(self.root, "acme"))
        self.assertEqual(self.run_rec()["phase"], "save")
        self.assertEqual([r["readonly"] for r in self.runs], [True, True, True])
        self.assertTrue(all(c.get("readonly") for c in minted))
        self.assertTrue(apps and all(c.get("report") for c in apps))
        self.assertFalse(self.sess.readonly)  # the thread's session is writable again after the turn
        out, _ = self.tick(["Saving.\nAPPROVAL: create-memo the plan"])  # the save turn still asks
        self.assertTrue([e for e in events(out) if "approval" in e])
        self.assertFalse(self.runs[-1]["readonly"])

    def test_draft_turn_runs_no_own_consults_and_raises_no_card(self):
        self.consult_flow()
        out, rec = self.ask("/demo-flow an idea", ["Draft test.\nCONSULT @research: is it big?\n"
                                                   "HANDOFF @cmo: write the copy\nAPPROVAL: save the test"])
        self.assertEqual(len(self.runs), 1)  # the workflow runs the consults, not the bot
        self.assertTrue(self.runs[0]["readonly"])
        self.assertFalse([e for e in events(out) if "approval" in e])
        self.assertFalse(approvals.pending(self.root, "acme"))
        self.assertEqual((self.run_rec()["phase"], self.run_rec()["status"]), ("consult", "running"))
        self.tick(["Advice."])
        self.tick(["More advice."])
        out, _ = self.tick(["Revised.\nCONSULT @cmo: again?"])
        self.assertEqual(len(self.runs), 4)
        self.assertFalse(self.runs[-1]["readonly"])
        self.assertEqual(wf_event(out)["phase"], "check")

    def test_consultant_switched_off_pauses(self):
        self.consult_flow()
        self.ask("/demo-flow an idea", ["Draft test."])
        bots.set_active("cmo", False, self.root, "acme", self.tree[0])
        out, _ = self.tick()
        self.assertIn("@cmo is switched off", out)
        self.assertEqual(self.run_rec()["status"], "paused")
        self.assertEqual(len(self.runs), 1)

    def test_brief_label_is_neutral_and_cut(self):
        wf = workflows.load("idea-to-experiment")
        run = workflows.new_run(wf, "@cmo do /positioning " + "x" * 400, "acme", "acme/general")
        text = workflow_turns.label(run, wf)
        self.assertNotIn("@cmo", text)
        self.assertNotIn("/positioning", text)
        self.assertTrue(text.endswith("..."))

    def team_flow(self, debate=2):
        """A one-step team workflow: @product leads and picks its consultants, @gtm checks."""
        text = ("---\nname: team-flow\nkind: workflow\ndescription: Workflow. Demo.\n---\n\nIntro.\n\n"
                "## Steps\n1. skill: experiment-design | bot: product | output: a test | gate: approval"
                f" | checker: gtm | team: yes | debate: {debate} | consult: research\n\n## Stop when\nDone.\n")
        wf = workflows.parse(text, "team-flow")
        real = workflows.load
        p = mock.patch.object(workflows, "load", side_effect=lambda n: wf if n == "team-flow" else real(n))
        p.start()
        self.addCleanup(p.stop)
        return wf

    def test_team_stage_instructions_offer_alone_or_consult(self):
        wf = self.team_flow()
        run = workflows.new_run(wf, "an idea", "acme", "acme/general")
        text = workflow_turns._instructions(run, wf)
        self.assertIn("If you can do it well alone, write the full output and consult nobody", text)
        self.assertIn("`CONSULT @handle:", text)
        self.assertIn("Teammates who often help here: @research.", text)
        self.assertIn("Do not save anything", text)
        _, ctx = workflow_turns._turn({}, run, wf)
        self.assertTrue(workflow_turns.team(ctx) and workflow_turns.advisory(ctx))
        self.assertFalse(workflow_turns.solo(ctx))

    def test_debate_and_answer_labels_and_instructions(self):
        wf = self.team_flow()
        run = dict(workflows.new_run(wf, "an idea", "acme", "acme/general"), phase="debate",
                   team=["research", "cmo"], panel=["research", "cmo"], round=1, debated=1)
        self.assertEqual(workflow_turns.label(run, wf), "@cmo Review step 1 of /team-flow")
        text = workflow_turns._instructions(run, wf)
        self.assertIn("AGREE, or OBJECT:", text)
        _, ctx = workflow_turns._turn({}, run, wf)
        self.assertEqual(ctx["route"][0], "cmo")
        self.assertTrue(workflow_turns.advisory(ctx))
        run = dict(run, phase="answer", objections=[["cmo", "the date is @research's call"]])
        self.assertEqual(workflow_turns.label(run, wf), "@product Answer objections on step 1 of /team-flow")
        text = workflow_turns._instructions(run, wf)
        self.assertIn("- @cmo: the date is research's call", text)
        self.assertIn("`Kept:`", text)
        self.assertIn("Write the whole revised output in full", text)
        _, ctx = workflow_turns._turn({}, run, wf)
        self.assertTrue(workflow_turns.advisory(ctx) and workflow_turns.solo(ctx))
        run = dict(run, objections=[], notes="no dates")
        self.assertEqual(workflow_turns.label(run, wf), "@product Rewrite step 1 of /team-flow")
        self.assertIn("The checker found this missing: no dates", workflow_turns._instructions(run, wf))

    def test_open_objections_shown_to_checker(self):
        wf = self.team_flow()
        run = dict(workflows.new_run(wf, "an idea", "acme", "acme/general"), phase="check", open="@cmo: no budget")
        self.assertIn("Objections still open after the team's debate", workflow_turns._instructions(run, wf))
        self.assertIn("cmo: no budget", workflow_turns._instructions(run, wf))

    def test_team_turns_save_only_in_save_phase(self):
        wf = self.team_flow()
        run = workflows.new_run(wf, "an idea", "acme", "acme/general")
        for phase in ("stage", "debate", "answer", "check"):
            _, ctx = workflow_turns._turn({}, dict(run, phase=phase, panel=["cmo"]), wf)
            self.assertTrue(workflow_turns.advisory(ctx), phase)
        _, ctx = workflow_turns._turn({}, dict(run, phase="save"), wf)
        self.assertFalse(workflow_turns.advisory(ctx))

    def test_current_pauses_an_idle_run(self):
        self.ask("/idea-to-experiment acme sells widgets", ["The problem is real."])
        workflows.save(self.root, self.run_rec())
        f = self.root / "team" / "workflow_runs" / f"{CHAT}.json"
        rec = json.loads(f.read_text())
        f.write_text(json.dumps(dict(rec, updated=rec["updated"] - workflows.IDLE_SECONDS - 5)))
        view = workflow_turns.current(self.root, CHAT)
        self.assertEqual((view["status"], view["why"]), ("paused", "No step ran for 30 minutes."))
        self.assertEqual(self.run_rec()["status"], "paused")
        out, _ = self.tick(["Segments."])  # Continue picks it up
        self.assertEqual(self.lead(out), "research")

    def test_team_stage_runs_blind_consults_and_revises(self):
        self.team_flow()
        out, rec = self.ask("/team-flow an idea", [
            "Draft plan.\nCONSULT @research: is the market big?\nCONSULT @cmo: what copy?",
            "Research advice.", "Cmo advice.", "Full plan."])
        texts = [r["text"] for r in self.runs]
        self.assertEqual(len(texts), 4)
        self.assertIn("Draft plan.", texts[1])
        self.assertIn("is the market big?", texts[1])
        self.assertIn("Draft plan.", texts[2])
        self.assertNotIn("Research advice.", texts[2])  # blind: never another consultant's advice
        self.assertIn("Your teammates answered your step 1 draft", texts[3])
        self.assertIn("Research advice.", texts[3])
        self.assertTrue(all(r["readonly"] for r in self.runs))
        self.assertFalse([e for e in events(out) if "approval" in e])
        run = self.run_rec()
        self.assertEqual((run["phase"], run["team"], run["panel"]), ("debate", ["research", "cmo"], ["research", "cmo"]))
        self.assertEqual(wf_event(out)["label"], "@research Review step 1 of /team-flow")

    def test_team_stage_pool_drops_lead_checker_and_outsiders(self):
        self.team_flow()
        self.ask("/team-flow an idea", [
            "Full plan.\nCONSULT @product: me?\nCONSULT @gtm: check early?\nCONSULT @nobody: hi"])
        self.assertEqual(len(self.runs), 1)
        self.assertEqual((self.run_rec()["phase"], self.run_rec()["team"]), ("check", []))
        wf = workflows.load("team-flow")
        ctx = {"wf": wf, "run": workflows.new_run(wf, "x", "acme", "acme/general")}
        asks = [("research", "a"), ("cmo", "b"), ("cmo", "c"), ("cos", "d"), ("social", "e")]
        self.assertEqual(workflow_turns.team_asks(ctx, asks, members=["product", "cmo", "cos"]),
                         [("cmo", "b"), ("cos", "d")])
        self.assertEqual([b for b, _ in workflow_turns.team_asks(ctx, asks)], ["research", "cmo", "cos"])

    def test_also_adds_one_bot_and_stops_at_three(self):
        self.team_flow()
        out, rec = self.ask("/team-flow an idea", [
            "Draft.\nCONSULT @research: big?\nCONSULT @cmo: copy?",
            "Research advice.\nALSO @cos: who owns the budget\nALSO @cmo: again",
            "Cmo advice.\nALSO @gtm: checker",
            "Cos advice.\nALSO @social: more",
            "Full plan."])
        self.assertEqual(len(self.runs), 5)
        self.assertIn("who owns the budget", self.runs[3]["text"])
        self.assertEqual(self.run_rec()["team"], ["research", "cmo", "cos"])
        shown = [m["content"] for m in rec["messages"] if m.get("bot") == "research"]
        self.assertTrue(shown and "ALSO" not in shown[-1])

    def test_debater_switched_off_mid_run_pauses(self):
        self.team_flow()
        self.ask("/team-flow an idea", ["Draft.\nCONSULT @cmo: copy?", "Cmo advice.", "Full plan."])
        bots.set_active("cmo", False, self.root, "acme", self.tree[0])
        n = len(self.runs)
        out, _ = self.tick()
        self.assertIn("@cmo is switched off", out)
        self.assertEqual(self.run_rec()["status"], "paused")
        self.assertEqual(len(self.runs), n)

    def test_team_step_full_run_through_debate(self):
        self.team_flow(debate=2)
        self.ask("/team-flow an idea", ["Draft.\nCONSULT @research: big?", "Research advice.", "Full plan."])
        out, _ = self.tick(["OBJECT: the date is too early"])
        self.assertEqual(self.lead(out), "research")
        self.assertEqual(wf_event(out)["phase"], "answer")
        out, _ = self.tick(["Plan with a later date."])
        self.assertEqual(self.lead(out), "product")
        self.assertIn("the date is too early", self.runs[-1]["text"])
        out, _ = self.tick(["AGREE"])
        self.assertEqual(wf_event(out)["phase"], "check")
        out, _ = self.tick(["PASS"])
        self.assertEqual(wf_event(out)["phase"], "save")
        out, _ = self.tick(["Saving.\nAPPROVAL: create-memo the plan"])
        self.assertTrue([e for e in events(out) if "approval" in e])
        self.assertFalse(self.runs[-1]["readonly"])

    # --- drafted plans -------------------------------------------------------------------------

    def save_plan_run(self, block, chat=CHAT):
        wf = plans.parse(block)
        run = dict(workflows.new_run(wf, wf.description, "acme", "acme/general"), chat=chat)
        workflows.save(self.root, run)
        return run

    def test_plan_run_ticks_with_no_skill(self):
        self.save_plan_run("PLAN: Hire a designer\nbrief: b\n1. bot: product | output: role brief\n"
                           "2. bot: cmo | output: job post | gate: no")
        out, _ = self.tick(["role brief done"])
        self.assertEqual(self.lead(out), "product")
        ev = wf_event(out)
        self.assertEqual((ev["step"], ev["status"], ev["name"]), (2, "running", "Hire a designer"))
        self.assertTrue(ev["label"].startswith("@cmo Step 2 of 2 of Hire a designer"))
        self.assertNotIn("/", ev["label"])
        self.assertIn("## Workflow Hire a designer, step 1 of 2", self.runs[-1]["text"])

    def test_plan_step_routes_without_skill(self):
        run = self.save_plan_run("PLAN: T\nbrief: b\n1. bot: product | output: x\n"
                                 "2. skill: launch-plan | bot: gtm | output: y")
        body, ctx = workflow_turns._turn({"messages": []}, run, workflows.of_run(run))
        self.assertEqual(ctx["route"], ("product", None, False, None))
        self.assertEqual(body["messages"][0]["content"], "@product Step 1 of 2 of T: b")
        run2 = dict(run, step=1)
        body, ctx = workflow_turns._turn({"messages": []}, run2, workflows.of_run(run2))
        self.assertEqual(ctx["route"], ("gtm", "launch-plan", True, None))
        self.assertEqual(body["messages"][0]["content"], "@gtm /launch-plan Step 2 of 2 of T")

    def test_stop_and_current_on_plan_run(self):
        self.save_plan_run("PLAN: T\nbrief: b\n1. bot: product | output: x")
        self.assertEqual(workflow_turns.current(self.root, CHAT)["name"], "T")
        self.assertIs(workflow_turns.current(self.root, CHAT)["plan"], True)
        self.assertEqual(workflow_turns.stop(self.root, CHAT)["status"], "stopped")

    def test_plan_run_paused_refusal_uses_title(self):
        self.save_plan_run("PLAN: T\nbrief: b\n1. bot: cmo | output: x")
        bots.set_active("cmo", False, self.root, "acme", self.tree[0])
        out, _ = self.tick()
        self.assertIn("T is paused: Step 1: @cmo is switched off.", out)
        self.assertNotIn("/T", out)

    SRC = "0123abcd-0000-4000-8000-0000000000aa"

    def offer_plan(self, lines=("bot: product | output: role brief",), room="acme/general", chat=CHAT,
                   status="offered"):
        plan = {"title": "T", "brief": "Hire a designer for acme.", "lines": list(lines), "bot": "gtm",
                "status": status, "run_chat": None}
        server.chat_store.save({"id": chat, "room": room, "messages": [
            {"role": "user", "content": "@gtm hire a designer"},
            {"role": "assistant", "bot": "gtm", "content": "Needs two areas.", "plan": plan}]})
        return chat

    def approve(self, src, replies=("done",), msg=1, **kw):
        rec = server.chat_store.load(src)
        return self.ask("Approved plan: T", list(replies), room=kw.pop("room", rec["room"]), chat_id=src,
                        workflow="plan", plan_ref={"chat": src, "msg": msg}, **kw)

    def plan_of(self, src):
        return server.chat_store.load(src)["messages"][1]["plan"]

    def test_approve_starts_once(self):
        src = self.offer_plan()
        out, _ = self.approve(src)
        self.assertEqual(self.lead(out), "product")
        self.assertEqual(wf_event(out)["name"], "T")
        self.assertEqual(self.run_rec()["steps"], ["bot: product | output: role brief"])
        self.assertEqual(self.run_rec()["brief"], "Hire a designer for acme.")
        self.assertEqual((self.plan_of(src)["status"], self.plan_of(src)["run_chat"]), ("started", CHAT))
        n = len(self.runs)
        again, _ = self.approve(src)
        self.assertIn("This plan already started.", again)
        self.assertEqual(len(self.runs), n)

    def test_approve_refused_for_switched_off_bot_keeps_offer(self):
        src = self.offer_plan(lines=["bot: cmo | output: x"])
        bots.set_active("cmo", False, self.root, "acme", self.tree[0])
        out, _ = self.approve(src)
        self.assertIn("T cannot start here:", out)
        self.assertIn("@cmo is switched off", out)
        self.assertIn("Switch them on", out)
        self.assertEqual(self.plan_of(src)["status"], "offered")
        self.assertEqual(self.runs, [])

    def test_approve_not_owner(self):
        src = self.offer_plan()
        with mock.patch.object(server.rooms, "can_see", return_value=True), \
                mock.patch.object(server.rooms, "chat_visible", return_value=True):
            out, _ = self.approve(src, viewer={"kind": "person", "id": "p1", "ws": ["acme"]})
        self.assertIn(workflow_turns.PRIVATE_ONLY, out)
        self.assertEqual(self.plan_of(src)["status"], "offered")

    def test_approve_from_dm_starts_new_thread_in_general(self):
        src = self.offer_plan(room="acme/dm-gtm", chat=self.SRC)
        out, _ = self.approve(src)
        ev = wf_event(out)
        self.assertEqual(ev["chat"], CHAT)  # the new thread (the test session's id), not the DM
        self.assertEqual(self.plan_of(src)["run_chat"], CHAT)
        self.assertEqual(self.run_rec()["room"], "acme/general")

    def test_approve_in_thread_with_live_run_starts_new_thread(self):
        self.save_plan_run("PLAN: Other\nbrief: b\n1. bot: product | output: x", chat=self.SRC)
        src = self.offer_plan(chat=self.SRC)
        body = {"workflow": "plan", "plan_ref": {"chat": src, "msg": 1}, "chat_id": src, "room_id": "acme/general",
                "messages": [{"role": "user", "content": "Approved plan: T"}]}
        team = bots.team(self.root, "acme", self.tree[0])
        got, ctx = workflow_turns.prepare(body, server.OWNER, "acme", team, {"id": "acme/general", "kind": "channel"},
                                          False, self.root)
        self.assertIsNone(got["chat_id"])
        self.assertEqual(got["room_id"], "acme/general")
        self.assertNotIn("plan_ref", got)
        self.assertEqual(ctx["plan_ref"], (src, 1))

    def test_approve_bad_refs(self):
        src = self.offer_plan()
        team = bots.team(self.root, "acme", self.tree[0])
        room = {"id": "acme/general", "kind": "channel"}
        for ref, why in (({"chat": "../x", "msg": 1}, "No plan to start here."),
                         ({"chat": src, "msg": 0}, "No plan to start here."),
                         ({"chat": src, "msg": -1}, "No plan to start here."),
                         ({"chat": src, "msg": "1"}, "No plan to start here."),
                         ({"chat": src, "msg": 9}, "No plan to start here."),
                         ("x", "No plan to start here.")):
            body = {"workflow": "plan", "plan_ref": ref, "messages": []}
            self.assertEqual(workflow_turns.prepare(body, server.OWNER, "acme", team, room, False, self.root), why)
        other = workflow_turns.prepare({"workflow": "plan", "plan_ref": {"chat": src, "msg": 1}, "messages": []},
                                       server.OWNER, "demo", team, room, False, self.root)
        self.assertEqual(other, "No plan to start here.")  # another workspace's thread
        self.offer_plan(status="rejected")
        self.assertEqual(workflow_turns.prepare({"workflow": "plan", "plan_ref": {"chat": src, "msg": 1},
                                                 "messages": []}, server.OWNER, "acme", team, room, False,
                                                self.root), "This plan was rejected.")

    def test_approve_refused_when_general_is_shared(self):
        src = self.offer_plan(room="acme/dm-gtm", chat=self.SRC)
        body = {"workflow": "plan", "plan_ref": {"chat": src, "msg": 1}, "chat_id": src, "room_id": "acme/dm-gtm",
                "messages": []}
        team = bots.team(self.root, "acme", self.tree[0])
        got = workflow_turns.prepare(body, server.OWNER, "acme", team, {"id": "acme/dm-gtm", "kind": "dm"},
                                     False, self.root, general_shared=True)
        self.assertEqual(got, workflow_turns.PRIVATE_ONLY)
        self.assertEqual(self.plan_of(src)["status"], "offered")

    def test_approve_on_busy_thread_stays_offered(self):
        src = self.offer_plan()
        with mock.patch.object(server, "_mark_busy", return_value=False), \
                mock.patch.object(server.Handler, "_send", lambda self, code, data: None):
            self.approve(src)
        self.assertEqual(self.plan_of(src)["status"], "offered")
        self.assertEqual(self.runs, [])

    def test_approve_from_dm_refused_when_general_shared(self):
        src = self.offer_plan(room="acme/dm-gtm", chat=self.SRC)
        with mock.patch.object(server, "_shared_room", side_effect=lambda ws, room: (room or {}).get("id") == "acme/general"):
            out, _ = self.approve(src)
        self.assertIn(workflow_turns.PRIVATE_ONLY, out)
        self.assertEqual(self.plan_of(src)["status"], "offered")

    def test_message_with_only_a_plan_is_kept(self):
        self.assertTrue(server.chat_store.kept({"plan": {"title": "T"}}))


if __name__ == "__main__":
    unittest.main()
