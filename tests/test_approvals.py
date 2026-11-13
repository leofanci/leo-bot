"""Step 2: approvals inbox, usage/cost capture, editable bot memory."""
import io
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from core import approvals, bots
import dashboard.chat_session as chat_session
import dashboard.server as server


class Marker(unittest.TestCase):
    def test_extract(self):
        clean, s = approvals.extract("Plan:\n- memo to sec04\n\n**APPROVAL:** save positioning memo to sec04")
        self.assertEqual(s, "save positioning memo to sec04")
        self.assertEqual(clean, "Plan:\n- memo to sec04")
        self.assertEqual(approvals.extract("no marker here"), ("no marker here", None))
        self.assertEqual(approvals.extract("APPROVAL: a\nAPPROVAL: b")[1], "b")


class Store(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def add(self, ws, chat, summary="x"):
        return approvals.add(self.root, ws=ws, room=f"{ws}/general", chat=chat, bot="cmo", summary=summary)

    def test_pending_per_workspace_and_hq_sees_all(self):
        self.add("acme", "c1")
        self.add("demo", "c2")
        self.assertEqual([a["chat"] for a in approvals.pending(self.root, "acme")], ["c1"])
        self.assertEqual(len(approvals.pending(self.root, "hq")), 2)

    def test_new_proposal_supersedes_and_answer_resolves(self):
        self.add("acme", "c1", "first")
        self.add("acme", "c1", "second")
        self.assertEqual([a["summary"] for a in approvals.pending(self.root)], ["second"])
        self.assertEqual(approvals.resolve_chat("c1", "approved", self.root), 1)
        self.assertEqual(approvals.pending(self.root), [])
        self.assertEqual(approvals.load(self.root)[-1]["status"], "approved")

    def test_renamed_channel_carries_its_approvals(self):
        approvals.add(self.root, ws="acme", room="acme/t-launch", chat="c1", bot="cmo", summary="x")
        self.add("acme", "c2")
        self.assertEqual(approvals.move_room("acme/t-launch", "acme/t-go-live", self.root), 1)
        self.assertEqual([a["room"] for a in approvals.load(self.root)], ["acme/t-go-live", "acme/general"])


class Usage(unittest.TestCase):
    def test_result_event_carries_tokens_and_cost(self):
        kind, p = chat_session.parse_event({
            "type": "result", "result": "ok", "subtype": "success", "total_cost_usd": 0.0123,
            "usage": {"input_tokens": 10, "cache_read_input_tokens": 1000,
                      "cache_creation_input_tokens": 200, "output_tokens": 90}})
        self.assertEqual((kind, p["tokens"], p["cost"]), ("done", 1300, 0.0123))


class Turn(unittest.TestCase):
    """A proposing reply becomes an approval; the next message in the thread closes it."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "projects" / "acme").mkdir(parents=True)
        self.tree = [{"slug": "acme", "name": "Acme", "kind": "venture", "type": "project",
                      "folders": [], "profiles": [], "products": []}]
        self._chats = server.chat_store.CHATS_DIR   # the server's own module copy
        server.chat_store.CHATS_DIR = self.root / "chats"

    def tearDown(self):
        server.chat_store.CHATS_DIR = self._chats
        self.tmp.cleanup()

    def ask(self, msg, reply, **body):
        sink = io.BytesIO()
        h = server.Handler.__new__(server.Handler)
        h.wfile = sink
        h.send_response = h.send_header = h.end_headers = lambda *a, **k: None
        h.server = mock.Mock(server_address=("127.0.0.1", 8765))

        class S:
            _started = True
            _turn_count = 1
            _last_skill = None
            _skill_explicit = False
            _last_bot = None
            session_id = "0" * 8 + "-0000-0000-0000-" + "0" * 12

            def is_fresh(self):
                return False

            def note_skill(self, *a, **k):
                pass

            def note_bot(self, *a):
                pass

            def ask(self, text, **kw):
                yield ("delta", reply)
                yield ("done", {"tokens": 1500, "cost": 0.02})

        with mock.patch.object(server, "ROOT", self.root), \
                mock.patch.object(server.db, "tree", return_value=self.tree), \
                mock.patch.object(server.db, "posts", return_value=[]), \
                mock.patch.object(server, "get_chat_session", return_value=S()):
            h._handle_ask({"messages": [{"role": "user", "content": msg}],
                           "room_id": "acme/general", "chat_id": S.session_id, **body})
        return sink.getvalue().decode(), server.chat_store.load(S.session_id)

    def test_approval_flow_and_usage(self):
        out, rec = self.ask("@cmo positioning for acme", "Two options.\nAPPROVAL: save positioning memo to sec04")
        self.assertIn('"approval"', out)
        self.assertIn('"usage"', out)
        last = rec["messages"][-1]
        self.assertEqual(last["content"], "Two options.")
        self.assertEqual((last["tokens"], last["cost"], last["bot"]), (1500, 0.02, "cmo"))
        pend = approvals.pending(self.root, "acme")
        self.assertEqual(pend[0]["summary"], "save positioning memo to sec04")
        self.assertEqual(last["approval"], pend[0]["id"])
        self.ask("Yes, approved.", "Saved.", approval="approved")
        self.assertEqual(approvals.pending(self.root), [])
        self.assertEqual(approvals.load(self.root)[-1]["status"], "approved")
        costs = server._costs("acme", self.tree)
        self.assertEqual(costs["bots"]["cmo"]["turns"], 2)  # Approve answers the bot that asked
        self.assertEqual(costs["total"]["turns"], 2)
        self.assertAlmostEqual(costs["total"]["cost"], 0.04)
        self.assertEqual(server._costs("hq", self.tree)["total"]["tokens"], 3000)


class UntaggedSaves(Turn):
    """An untagged private turn may propose a save; the owner's approval stays the gate."""

    def test_untagged_private_turn_gets_a_card(self):
        out, rec = self.ask("save the positioning memo", "One memo.\nAPPROVAL: save positioning memo")
        self.assertNotIn("tag who should do it", out)
        self.assertIn('"approval"', out)
        self.assertEqual(approvals.pending(self.root, "acme")[0]["summary"], "save positioning memo")
        self.assertEqual(rec["messages"][-1]["content"], "One memo.")

    def test_shared_thread_gets_no_card(self):
        with mock.patch.object(server, "_shared_room", return_value=True):
            out, _ = self.ask("save the positioning memo", "One memo.\nAPPROVAL: save positioning memo")
        self.assertNotIn('"approval"', out)
        self.assertEqual(approvals.pending(self.root, "acme"), [])

    def test_prompt_takes_no_skill_hint(self):
        with self.assertRaises(TypeError):
            server.compose_turn_prompt(user_msg="x", context="", skill=None, session=None,
                                       projects=[], suggest="positioning")


class Memory(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "projects" / "acme").mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def test_forget_removes_one_line(self):
        bots.remember("cmo", "B2B only", "acme", self.root)
        bots.remember("cmo", "premium tier", "acme", self.root)
        line = bots.read_memory("cmo", "acme", self.root, tail=0).splitlines()[0]
        self.assertTrue(bots.forget("cmo", line, "acme", self.root))
        self.assertFalse(bots.forget("cmo", line, "acme", self.root))
        self.assertNotIn("B2B only", bots.read_memory("cmo", "acme", self.root))
        self.assertIn("premium tier", bots.read_memory("cmo", "acme", self.root))


class JobCards(unittest.TestCase):
    """Job proposals: held like app actions, answered only through their own card."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def card(self, chat="c1"):
        return approvals.add_job(self.root, ws="acme", room="acme/dm-cmo", chat=chat, bot="cmo",
                                 job={"op": "create", "job_id": None, "fields": {"name": "weekly"}},
                                 summary="New job: weekly", preview={"title": "New job: weekly", "fields": []})

    def test_held_like_an_app_action(self):
        rec = self.card()
        self.assertEqual((rec["kind"], rec["status"]), ("job", "pending"))
        approvals.add(self.root, ws="acme", room="acme/dm-cmo", chat="c1", bot="cmo", summary="save a memo")
        self.assertEqual(approvals.resolve_chat("c1", "answered", self.root), 1)  # the chat row only
        self.assertEqual(approvals.get(rec["id"], self.root)["status"], "pending")
        self.assertIn(rec["id"], [r["id"] for r in approvals.pending(self.root, "acme")])

    def test_claim_once_and_finish(self):
        rec = self.card()
        approvals.claim(rec["id"], self.root)
        with self.assertRaises(ValueError):
            approvals.claim(rec["id"], self.root)
        approvals.finish(rec["id"], "done", "Job created.", self.root)
        self.assertEqual(approvals.get(rec["id"], self.root)["result"], "Job created.")

    def test_edit_and_reject(self):
        rec = self.card()
        got = approvals.edit_job(rec["id"], {"op": "create", "job_id": None, "fields": {"name": "daily"}},
                                 "New job: daily", {"title": "New job: daily", "fields": []}, self.root)
        self.assertEqual((got["job"]["fields"]["name"], got["summary"]), ("daily", "New job: daily"))
        with self.assertRaises(ValueError):  # an app-only edit never reaches a job row
            approvals.edit_arguments(rec["id"], {}, self.root)
        approvals.reject(rec["id"], self.root)
        with self.assertRaises(ValueError):
            approvals.edit_job(rec["id"], got["job"], "x", {}, self.root)

    def test_week_old_card_expires(self):
        rec = self.card()
        with mock.patch.object(approvals.time, "time", return_value=time.time() + approvals.APP_TTL + 5):
            self.assertEqual(approvals.pending(self.root), [])
        self.assertEqual(approvals.get(rec["id"], self.root)["status"], "expired")

    def test_a_second_card_for_the_same_target_supersedes_the_first(self):
        first, other_thread = self.card(), self.card(chat="c2")
        second = self.card()  # the same new job proposed again in c1
        self.assertEqual(approvals.get(first["id"], self.root)["status"], "superseded")
        self.assertEqual(approvals.get(other_thread["id"], self.root)["status"], "pending")
        change = approvals.add_job(self.root, ws="acme", room="acme/dm-cmo", chat="c1", bot="cmo",
                                   job={"op": "update", "job_id": "j1", "fields": {"cap": 2}},
                                   summary="Change job: weekly", preview={})
        self.assertEqual(approvals.get(second["id"], self.root)["status"], "pending")  # another target
        approvals.add_job(self.root, ws="acme", room="acme/dm-cmo", chat="c1", bot="cmo",
                          job={"op": "update", "job_id": "j1", "fields": {"cap": 3}},
                          summary="Change job: weekly", preview={})
        self.assertEqual(approvals.get(change["id"], self.root)["status"], "superseded")

    def test_stale_running_card_says_check_jobs(self):
        rec = self.card()
        approvals.claim(rec["id"], self.root)
        started = approvals.get(rec["id"], self.root)["started"]
        with mock.patch("core.approvals.time.time", return_value=started + approvals.RUNNING_STALE + 1):
            approvals.pending(self.root)
        got = approvals.get(rec["id"], self.root)
        self.assertEqual((got["status"], got["result"]), ("failed", approvals.JOB_STALE_NOTE))


class AppActions(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def hold(self, ws="hq", chat="c1", slug="GMAIL_SEND_EMAIL", args=None):
        return approvals.add_app_action(self.root, ws=ws, room=f"{ws}/general", chat=chat, bot="cos",
                                        person="ceo", toolkit="gmail", slug=slug,
                                        arguments=args or {"recipient_email": "a@example.com"})

    def test_add_and_claim_once(self):
        rec = self.hold()
        self.assertEqual((rec["kind"], rec["status"]), ("app_action", "pending"))
        self.assertEqual(rec["preview"]["title"], "Send email")
        self.assertEqual(approvals.claim(rec["id"], self.root)["status"], "running")
        with self.assertRaises(ValueError):
            approvals.claim(rec["id"], self.root)

    def test_finish_done(self):
        rec = self.hold()
        approvals.claim(rec["id"], self.root)
        approvals.finish(rec["id"], "done", "Sent.", self.root)
        got = approvals.get(rec["id"], self.root)
        self.assertEqual((got["status"], got["result"]), ("done", "Sent."))
        self.assertEqual(approvals.pending(self.root), [])

    def test_reject(self):
        rec = self.hold()
        approvals.reject(rec["id"], self.root)
        self.assertEqual(approvals.get(rec["id"], self.root)["status"], "rejected")
        with self.assertRaises(ValueError):
            approvals.claim(rec["id"], self.root)

    def test_resolve_chat_skips_app_action(self):
        rec = self.hold()
        approvals.add(self.root, ws="hq", room="hq/general", chat="c1", bot="cos", summary="save memo")
        self.assertEqual(approvals.resolve_chat("c1", "answered", self.root), 1)
        self.assertEqual(approvals.get(rec["id"], self.root)["status"], "pending")

    def test_chat_add_does_not_supersede_app_action(self):
        rec = self.hold()
        approvals.add(self.root, ws="hq", room="hq/general", chat="c1", bot="cos", summary="save memo")
        self.assertEqual(approvals.get(rec["id"], self.root)["status"], "pending")

    def test_two_app_actions_same_chat_both_pending(self):
        self.hold()
        self.hold()
        self.assertEqual(len(approvals.pending(self.root)), 2)

    def test_expire_after_7_days(self):
        rec = self.hold()
        with mock.patch("core.approvals.time.time", return_value=rec["created"] + 7 * 86400 + 1):
            self.assertEqual(approvals.pending(self.root), [])
        self.assertEqual(approvals.get(rec["id"], self.root)["status"], "expired")

    def test_stale_running_becomes_failed(self):
        rec = self.hold()
        approvals.claim(rec["id"], self.root)
        started = approvals.get(rec["id"], self.root)["started"]
        with mock.patch("core.approvals.time.time", return_value=started + 601):
            approvals.pending(self.root)
        got = approvals.get(rec["id"], self.root)
        self.assertEqual(got["status"], "failed")
        self.assertEqual(got["result"], "May or may not have gone out. Check the app.")

    def test_trim_keeps_pending_app_actions(self):
        rec = self.hold()
        for i in range(approvals.KEEP + 50):
            approvals.add(self.root, ws="hq", room="hq/general", chat=f"x{i}", bot="cos", summary="s")
        self.assertIsNotNone(approvals.get(rec["id"], self.root))
        self.assertLessEqual(len(approvals.load(self.root)), approvals.KEEP + 1)

    def test_rows_without_kind_still_chat(self):
        approvals._save(self.root, [{"id": "old1", "chat": "c9", "status": "pending", "ws": "hq"}])
        self.assertEqual(approvals.resolve_chat("c9", "answered", self.root), 1)

    def test_edit_rebuilds_preview(self):
        rec = self.hold()
        approvals.edit_arguments(rec["id"], {"recipient_email": "z@example.com", "subject": "New"}, self.root)
        got = approvals.get(rec["id"], self.root)
        self.assertEqual(got["action"]["arguments"]["subject"], "New")
        self.assertEqual(dict(got["preview"]["fields"])["To"], "z@example.com")

    def test_app_actions_scoped_by_ws(self):
        a = self.hold(ws="acme", chat="c1")
        b = self.hold(ws="demo", chat="c2")
        self.assertEqual([r["id"] for r in approvals.pending(self.root, "acme")], [a["id"]])
        self.assertEqual(len(approvals.pending(self.root, "hq")), 2)
        approvals.claim(a["id"], self.root)
        self.assertEqual(approvals.get(b["id"], self.root)["status"], "pending")


if __name__ == "__main__":
    unittest.main()
