"""HANDOFF @handle: a bot passes the work to a teammate by name, in the same message."""
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from core import approvals, bots
import dashboard.server as server

CHAT = "0123abcd-0000-4000-8000-00000000000a"


def events(out):
    return [json.loads(line[6:]) for line in out.splitlines() if line.startswith("data: {")]


class ExtractHandoff(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        (root / "projects" / "acme").mkdir(parents=True)
        for b in ("research", "cmo"):
            bots.set_active(b, True, root, "acme")
        self.team = bots.set_active("social", False, root, "acme")

    def test_valid_line_is_stripped_and_returned(self):
        text, h = server._extract_handoff("On it.\nHANDOFF @research: size the market", self.team, speaker="cos")
        self.assertEqual((text, h), ("On it.", ("research", "size the market")))

    def test_markdown_dressing_is_accepted(self):
        _, h = server._extract_handoff("ok\n**HANDOFF @research:** size it", self.team, speaker="cos")
        self.assertEqual(h, ("research", "size it"))

    def test_inactive_self_and_non_member_are_dropped(self):
        for text, kw in (("HANDOFF @social: three posts", {}),
                         ("HANDOFF @cos: do it yourself", {}),
                         ("HANDOFF @cmo: price it", {"members": ["cos", "research"]}),
                         ("HANDOFF @nobody: x", {})):
            with self.subTest(text):
                clean, h = server._extract_handoff("Sure.\n" + text, self.team, speaker="cos", **kw)
                self.assertIsNone(h)
                self.assertEqual(clean, "Sure.")

    def test_only_the_first_valid_line_counts(self):
        clean, h = server._extract_handoff(
            "x\nHANDOFF @social: a\nHANDOFF @research: b\nHANDOFF @cmo: c", self.team, speaker="cos")
        self.assertEqual(h, ("research", "b"))
        self.assertEqual(clean, "x")

    def test_consult_lines_stay(self):
        clean, h = server._extract_handoff("x\nCONSULT @cmo: tone?\nHANDOFF @research: b", self.team, speaker="cos")
        self.assertEqual(h, ("research", "b"))
        self.assertIn("CONSULT @cmo: tone?", clean)

    def test_task_is_capped(self):
        _, h = server._extract_handoff("HANDOFF @research: " + "y" * 900, self.team, speaker="cos")
        self.assertEqual(len(h[1]), server.MAX_CONSULT_CHARS)


class HandoffTurn(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "projects" / "acme").mkdir(parents=True)
        for b in ("research", "cmo"):
            bots.set_active(b, True, self.root, "acme")
        self.tree = [{"slug": "acme", "name": "Acme", "kind": "venture", "type": "project",
                      "folders": [], "profiles": [], "products": []}]
        p = mock.patch.object(server.chat_store, "CHATS_DIR", self.root / "chats")
        p.start()
        self.addCleanup(p.stop)
        self.runs = []
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
                test.runs.append({"text": text, "model": kw.get("model"), "edit_dirs": kw.get("edit_dirs")})
                reply = test.replies.pop(0) if test.replies else "ok"
                yield ("delta", reply)
                yield ("done", {"tokens": 100, "cost": 0.01})
        return S()

    def ask(self, msg, replies, approval=None, **patches):
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
               # a consulted teammate runs in its own ChatSession: same scripted replies
               mock.patch.object(server.chat_session.ChatSession, "ask", type(self.sess).ask)]
        ctx += [mock.patch.object(server, k, v) for k, v in patches.items()]
        for c in ctx:
            c.start()
        try:
            h._handle_ask({"messages": [{"role": "user", "content": msg}],
                           "room_id": "acme/general", "chat_id": CHAT,
                           **({"approval": approval} if approval else {})})
        finally:
            for c in reversed(ctx):
                c.stop()
        return sink.getvalue().decode(), server.chat_store.load(CHAT)

    def test_handoff_runs_the_teammate_next(self):
        out, rec = self.ask("what is our market size?",
                            ["That's research's area.\nHANDOFF @research: size the market", "About 2M buyers."])
        self.assertEqual(len(self.runs), 2)
        self.assertIn("size the market", self.runs[1]["text"])
        self.assertIn("Original request: what is our market size?", self.runs[1]["text"])
        self.assertIn("taking it from @cos", self.runs[1]["text"])
        self.assertIn("@cos passed this to you", self.runs[1]["text"])  # a bot's handoff, not a user tag
        self.assertNotIn("user tagged", self.runs[1]["text"])
        msgs = rec["messages"][-2:]
        self.assertEqual((msgs[0]["bot"], msgs[0]["content"]), ("cos", "That's research's area."))
        self.assertEqual((msgs[1]["bot"], msgs[1]["handoff_from"], msgs[1]["content"]),
                         ("research", "cos", "About 2M buyers."))
        starts = [e["msg_start"] for e in events(out) if "msg_start" in e]
        self.assertEqual((starts[-1]["id"], starts[-1]["handoff_from"]), ("research", "cos"))
        self.assertIn('"clean": "That\'s research\'s area."', out)
        # The next untagged message goes to the teammate that took it over.
        self.assertEqual(self.sess._last_bot, "research")
        out, _ = self.ask("and in Europe?", ["Half of that."])
        bot = next(e["bot"] for e in events(out) if "bot" in e)
        self.assertEqual(bot["id"], "research")

    def test_wording_no_longer_moves_the_turn(self):
        self.sess._last_bot = "cos"
        out, _ = self.ask("how much should we charge for the pro tier?", ["Let me check."])
        bot = next(e["bot"] for e in events(out) if "bot" in e)
        self.assertEqual(bot["id"], "cos")
        self.assertNotIn("picked", bot)

    def test_target_approval_makes_a_card(self):
        out, rec = self.ask("size the market and save it",
                            ["HANDOFF @research: size it and save", "One memo.\nAPPROVAL: save market sizing memo"])
        pend = approvals.pending(self.root, "acme")
        self.assertEqual([(p["bot"], p["summary"]) for p in pend], [("research", "save market sizing memo")])
        self.assertEqual(rec["messages"][-1]["approval"], pend[0]["id"])
        self.assertEqual(rec["messages"][-1]["content"], "One memo.")

    def test_target_handoff_lines_are_ignored(self):
        out, rec = self.ask("x?", ["HANDOFF @research: a", "Done.\nHANDOFF @cmo: b"])
        self.assertEqual(len(self.runs), 2)
        self.assertEqual(rec["messages"][-1]["content"], "Done.")

    def test_invalid_target_runs_nothing_more(self):
        out, rec = self.ask("x?", ["Sure.\nHANDOFF @social: posts"])
        self.assertEqual(len(self.runs), 1)
        self.assertEqual(rec["messages"][-1]["content"], "Sure.")

    def test_consults_run_before_the_handoff(self):
        out, rec = self.ask("x?", ["CONSULT @cmo: tone?\nHANDOFF @research: size it", "warm", "follow-up", "sized"])
        self.assertEqual([m["bot"] for m in rec["messages"][-4:]], ["cos", "cmo", "cos", "research"])
        self.assertEqual(len(self.runs), 4)

    def test_cap_stops_the_handoff(self):
        calls = []

        def refusal(*a, **kw):
            calls.append(kw)
            return "You reached today's daily cap." if len(calls) > 1 else None
        with mock.patch.object(server.spend, "refusal", side_effect=refusal):
            out, rec = self.ask("x?", ["HANDOFF @research: size it", "never"])
        self.assertEqual(len(self.runs), 1)
        self.assertIn("daily cap", out)

    def test_approve_goes_to_the_bot_that_asked(self):
        self.sess._last_skill, self.sess._skill_explicit = "positioning", True  # a tagged skill thread (cmo's)
        self.ask("size it and save", ["HANDOFF @research: size it", "One memo.\nAPPROVAL: save sizing memo"])
        out, _ = self.ask("Yes, approved.", ["Saved."], approval="approved")
        bot = next(e["bot"] for e in events(out) if "bot" in e)
        self.assertEqual(bot["id"], "research")

    def test_a_tag_still_wins_over_the_card(self):
        self.ask("size it", ["HANDOFF @research: size it", "One memo.\nAPPROVAL: save sizing memo"])
        out, _ = self.ask("@cmo yes, approved", ["ok"], approval="approved")
        self.assertEqual(next(e["bot"] for e in events(out) if "bot" in e)["id"], "cmo")

    def test_both_proposals_one_card_and_a_note_on_the_dropped_one(self):
        out, rec = self.ask("x?", ["Plan A.\nAPPROVAL: save plan A\nHANDOFF @research: check it",
                                   "Plan B.\nAPPROVAL: save plan B"])
        pend = approvals.pending(self.root, "acme")
        self.assertEqual([p["summary"] for p in pend], ["save plan B"])
        lead = rec["messages"][-2]
        self.assertTrue(any("replaced" in n for n in lead.get("notices", [])), lead)

    def test_target_on_another_engine_gets_the_thread_and_both_engines_are_saved(self):
        class E:
            kind, full_disk, supports, label, shared_safe = "cli", False, frozenset(), "E", True

            def __init__(self, eid):
                self.id = eid

            def resolve_model(self, m):
                return m

            def is_fresh(self, s):
                return False
        engines = {"cos": E("claude-code"), "research": E("codex")}
        self.ask("hello", ["hi"])  # an earlier exchange the target engine never saw
        with mock.patch.object(server.engine_registry, "pick", side_effect=lambda r, w, b, **k: engines[b]), \
                mock.patch.object(server.engine_registry, "metered", return_value=True):
            _, rec = self.ask("market size?", ["Over to research.\nHANDOFF @research: size it", "2M."])
        self.assertIn("hello", self.runs[-1]["text"])  # the recap of what it missed
        self.assertIn("Over to research.", self.runs[-1]["text"])  # what the lead just said
        self.assertEqual(set(rec["engines"]), {"claude-code", "codex"})
        n = len(rec["messages"])
        self.assertEqual((rec["engines"]["claude-code"]["seen"], rec["engines"]["codex"]["seen"]), (n - 1, n))

    def test_consult_and_failed_runs_leave_no_load_record(self):
        from core import skill_loads
        test = self

        def ask(self_, text, **kw):
            test.runs.append({"text": text})
            skill_loads.record(test.root, getattr(self_, "session_id", None) or "x", "positioning")
            reply = test.replies.pop(0)
            if reply == "FAIL":
                yield ("error", "boom")
                return
            yield ("delta", reply)
            yield ("done", {"tokens": 1, "cost": 0.0})
        with mock.patch.object(type(self.sess), "ask", ask), \
                mock.patch.object(server.chat_session.ChatSession, "ask", ask):
            self.ask("x?", ["CONSULT @cmo: tone?", "warm", "done"])
            self.assertEqual(list((self.root / "team" / "skill_loads").glob("*.json")), [])
            self.ask("y?", ["FAIL"])
        self.assertEqual(list((self.root / "team" / "skill_loads").glob("*.json")), [])

    def test_consulted_teammates_handoff_never_reaches_the_follow_up(self):
        self.ask("x?", ["CONSULT @cmo: tone?", "warm\nHANDOFF @research: take it", "done"])
        self.assertEqual(len(self.runs), 3)
        self.assertNotIn("HANDOFF", self.runs[2]["text"])

    def test_loaded_skill_by_target_becomes_active(self):
        from core import skill_loads
        test = self

        def ask(self_, text, **kw):
            test.runs.append({"text": text})
            if len(test.runs) == 2:
                skill_loads.record(test.root, CHAT, "market-sizing")
            yield ("delta", test.replies.pop(0))
            yield ("done", {"tokens": 1, "cost": 0.0})
        with mock.patch.object(type(self.sess), "ask", ask):
            self.ask("x?", ["HANDOFF @research: size it", "sized"])
        self.assertEqual(self.sess._last_skill, "market-sizing")


if __name__ == "__main__":
    unittest.main()
