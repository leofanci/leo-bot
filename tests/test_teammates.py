"""Step 3: teammates pull each other in (CONSULT) and co-tagged bots chime in."""
import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from core import bots
import dashboard.osctl as osctl
import dashboard.server as server

VENTURE = {"slug": "acme", "name": "Acme", "kind": "venture", "type": "project",
           "profiles": [{"slug": "profile-a"}], "folders": [{"id": "f1", "path": "/tmp/acme-app", "name": "app", "access": "read"}], "products": []}


class Planning(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        (root / "projects" / "acme").mkdir(parents=True)
        self.team = bots.team(root, "acme", VENTURE)

    def tearDown(self):
        self.tmp.cleanup()

    def test_extract_consults(self):
        text = "Thinking.\nCONSULT @research: 3 cheapest rivals' prices?\n**consult @nobody: x**"
        clean, asks = server._extract_consults(text, self.team)
        self.assertEqual(clean, "Thinking.")  # every marker line goes; unknown handles are simply ignored
        self.assertEqual(asks, [("research", "3 cheapest rivals' prices?")])

    def test_plan_caps_dedupes_and_skips_lead_and_handoff(self):
        asks = [("research", "q1"), ("research", "q2"), ("cmo", "q3")]
        plan = server._plan_teammates(self.team, "gtm", None, "@gtm @social ideas?", asks)
        self.assertEqual([p[0] for p in plan], ["research", "cmo"])  # max 2, research once
        plan = server._plan_teammates(self.team, "research", "cmo", "@cmo @social competitors?", [])
        self.assertEqual(plan, [("social", "@cmo @social competitors?", None)])

    def test_mentioned_all(self):
        self.assertEqual(self.team.mentioned_all("@cmo and @gtm, @cmo again"), ["cmo", "gtm"])


class Flow(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "projects" / "acme").mkdir(parents=True)
        self._chats = server.chat_store.CHATS_DIR
        server.chat_store.CHATS_DIR = self.root / "chats"

    def tearDown(self):
        server.chat_store.CHATS_DIR = self._chats
        self.tmp.cleanup()

    SID = "44444444-0000-0000-0000-000000000000"

    def turn(self, lead_runs, user_msg="@cmo set our pricing"):
        """Run one chat turn. lead_runs: the events each lead run yields, in order.
        Returns (SSE events, teammate sessions, prompts the lead got)."""
        lead_prompts, mates = [], []

        class Lead:
            _started = True
            _turn_count = 1
            _last_skill = None
            _skill_explicit = False
            _last_bot = None
            session_id = self.SID

            def is_fresh(self):
                return False

            def note_skill(self, *a, **k):
                pass

            def note_bot(self, *a):
                pass

            def ask(self, text, **kw):
                lead_prompts.append(text)
                yield from lead_runs[len(lead_prompts) - 1]

        class Mate:
            def __init__(self, repo_dir, rail):
                self.readonly = False
                mates.append(self)

            def ask(self, text, **kw):
                self.prompt, self.kw = text, kw
                yield ("delta", "A at 19, B at 25, C at 39.")
                yield ("done", {"tokens": 400, "cost": 0.002})

        sink = io.BytesIO()
        h = server.Handler.__new__(server.Handler)
        h.wfile = sink
        h.send_response = h.send_header = h.end_headers = lambda *a, **k: None
        with mock.patch.object(server, "ROOT", self.root), \
                mock.patch.object(server.db, "tree", return_value=[VENTURE]), \
                mock.patch.object(server.db, "posts", return_value=[]), \
                mock.patch.object(server, "state_snapshot", side_effect=lambda p, only=None: "SNAP"), \
                mock.patch.object(server, "get_chat_session", return_value=Lead()), \
                mock.patch.object(server.chat_session, "ChatSession", Mate):
            h._handle_ask({"messages": [{"role": "user", "content": user_msg}],
                           "room_id": "acme/general", "chat_id": self.SID})
        events = [json.loads(l[6:]) for l in sink.getvalue().decode().splitlines()
                  if l.startswith("data: {")]
        return events, mates, lead_prompts

    def test_lead_consults_teammate_then_follows_up(self):
        events, mates, lead_prompts = self.turn([
            [("delta", "Let me check prices.\nCONSULT @research: cheapest 3 rivals?"),
             ("done", {"tokens": 1000, "cost": 0.01})],
            [("delta", "Go with 29/month.\nAPPROVAL: Pricing memo v1 for Acme"),
             ("done", {"tokens": 1000, "cost": 0.01})],
        ])
        starts = [e["msg_start"] for e in events if "msg_start" in e]
        self.assertEqual([(x["id"], x.get("asked_by")) for x in starts], [("research", "cmo"), ("cmo", None)])
        self.assertTrue(mates[0].readonly)
        self.assertIn("@cmo (CMO) asks you: cheapest 3 rivals?", mates[0].prompt)
        self.assertIn("read-only", mates[0].prompt)
        self.assertNotIn("remember --bot", mates[0].prompt)
        self.assertIn("A at 19", lead_prompts[1])
        msgs = server.chat_store.load(self.SID)["messages"]
        self.assertEqual([(m["role"], m.get("bot"), m.get("asked_by")) for m in msgs],
                         [("user", None, None), ("assistant", "cmo", None),
                          ("assistant", "research", "cmo"), ("assistant", "cmo", None)])
        self.assertEqual(msgs[1]["content"], "Let me check prices.")
        self.assertEqual(msgs[3]["content"], "Go with 29/month.")
        self.assertIn("approval", msgs[3])
        self.assertEqual(server.chat_store.load(self.SID)["tokens"], 2400)

    def test_approval_in_the_lead_first_message_is_recorded_even_with_consults(self):
        events, _, _ = self.turn([
            [("delta", "Draft ready.\nCONSULT @research: rivals?\nAPPROVAL: Positioning memo"),
             ("done", {})],
            [("delta", "Research agrees."), ("done", {})],
        ])
        msgs = server.chat_store.load(self.SID)["messages"]
        self.assertEqual(msgs[1]["content"], "Draft ready.")
        self.assertIn("approval", msgs[1])
        ap = [e for e in events if "approval" in e]
        self.assertEqual((ap[0]["approval"]["summary"], ap[0]["msg"]), ("Positioning memo", 0))
        self.assertNotIn("APPROVAL", [e for e in events if e.get("msg") == 0 and "clean" in e][-1]["clean"])

    def test_failed_lead_run_stops_the_turn(self):
        events, mates, lead_prompts = self.turn([
            [("delta", "CONSULT @research: rivals?"), ("error", "claude exited 1")],
        ], user_msg="@cmo @gtm set our pricing")
        self.assertEqual(mates, [])            # no teammate spends tokens after the error
        self.assertEqual(len(lead_prompts), 1)  # and no follow-up
        self.assertEqual([e["error"] for e in events if "error" in e], ["claude exited 1"])


class ReadOnly(unittest.TestCase):
    def run_osctl(self, *argv):
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"MYOS_READONLY": "1"}), contextlib.redirect_stdout(out):
            osctl.main(list(argv))
        return json.loads(out.getvalue())

    def test_consulted_teammate_cannot_write(self):
        r = self.run_osctl("create-memo", "--project", "acme", "--type", "pricing")
        self.assertFalse(r["ok"])
        self.assertIn("read-only", r["error"])
        r = self.run_osctl("remember", "--bot", "cmo", "--text", "x")
        self.assertFalse(r["ok"])
        self.assertTrue(self.run_osctl("get-busy", "--from", "2026-11-01")["ok"])


if __name__ == "__main__":
    unittest.main()
