"""workflow_turns.py + the server hook: a workflow runs one step per chat turn in a private thread."""
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from core import approvals, bots, workflows
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
                test.runs.append({"text": text})
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

    def test_brief_label_is_neutral_and_cut(self):
        wf = workflows.load("idea-to-experiment")
        run = workflows.new_run(wf, "@cmo do /positioning " + "x" * 400, "acme", "acme/general")
        text = workflow_turns.label(run, wf)
        self.assertNotIn("@cmo", text)
        self.assertNotIn("/positioning", text)
        self.assertTrue(text.endswith("..."))


if __name__ == "__main__":
    unittest.main()
