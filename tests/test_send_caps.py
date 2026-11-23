"""Send safeguards: a daily send cap per connected app account and a pace between browser sends."""
import tempfile
import time
import unittest
from pathlib import Path

from core import app_rules, approvals, integrations
from dashboard import apps_relay, taint

CAT = ["gmail", "googlecalendar", "slack", "linkedin"]
CTX = {"ws": "hq", "bot": "cos", "chat": "c1", "room": "hq/general", "person": "ceo"}


class FakeUpstream:
    def __init__(self):
        self.frames = []

    def forward(self, frame, mcp_session_id):
        self.frames.append(frame)
        return {"jsonrpc": "2.0", "id": frame.get("id"),
                "result": {"content": [{"type": "text", "text": "upstream ok"}]}}, "m-1"


def multi(*items):
    return {"jsonrpc": "2.0", "id": 7, "method": "tools/call",
            "params": {"name": "COMPOSIO_MULTI_EXECUTE_TOOL", "arguments": {"tools": list(items)}}}


SEND = {"tool_slug": "GMAIL_SEND_EMAIL", "arguments": {"recipient_email": "a@example.com", "body": "hi"}}
EVENT = {"tool_slug": "GOOGLECALENDAR_CREATE_EVENT", "arguments": {"summary": "call"}}


class AppSendCap(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        integrations.note_known([{"id": "acc1", "toolkit": "gmail", "label": "work"},
                                 {"id": "cal1", "toolkit": "googlecalendar", "label": "cal"}], self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def start(self, slug="GMAIL_SEND_EMAIL", toolkit="gmail", account="acc1", ws="acme", daily=2, now=None):
        return approvals.start_app_run(self.root, ws=ws, room=None, chat=None, bot="sales", person="ceo",
                                       toolkit=toolkit, slug=slug, arguments={}, account=account, daily=daily,
                                       now=now)

    def test_is_send(self):
        for slug, tk in (("GMAIL_SEND_EMAIL", "gmail"), ("GMAIL_REPLY_TO_THREAD", "gmail"),
                         ("GMAIL_SEND_DRAFT", "gmail"), ("SLACK_CHAT_POST_MESSAGE", "slack"),
                         ("SLACK_SENDS_A_MESSAGE_TO_A_SLACK_CHANNEL", "slack"),
                         ("LINKEDIN_CREATE_LINKED_IN_POST", "linkedin")):
            self.assertTrue(app_rules.is_send(slug, tk), slug)
        for slug, tk in (("GMAIL_CREATE_EMAIL_DRAFT", "gmail"), ("GMAIL_FETCH_EMAILS", "gmail"),
                         ("GOOGLECALENDAR_CREATE_EVENT", "googlecalendar"), ("SLACK_LIST_POSTS", "slack")):
            self.assertFalse(app_rules.is_send(slug, tk), slug)

    def test_default_and_set(self):
        self.assertEqual(integrations.send_cap("acc1", self.root), 50)
        self.assertEqual(integrations.set_send_cap("acc1", 5, self.root), 5)
        self.assertEqual(integrations.send_cap("acc1", self.root), 5)
        for bad in (0, 501, "x", True):
            with self.assertRaises(ValueError):
                integrations.set_send_cap("acc1", bad, self.root)
        with self.assertRaises(ValueError):
            integrations.set_send_cap("nobody", 5, self.root)
        self.assertEqual(integrations.set_send_cap("acc1", None, self.root), 50)
        self.assertEqual(integrations.send_cap("acc1", self.root), 50)

    def test_act_send_over_cap_refused(self):
        self.assertIsNotNone(self.start())
        self.assertIsNotNone(self.start())
        self.assertIsNone(self.start())
        for _ in range(3):
            self.assertIsNotNone(self.start("GOOGLECALENDAR_CREATE_EVENT", "googlecalendar", account="acc1"))

    def test_cap_counts_across_workspaces(self):
        self.assertIsNotNone(self.start(ws="acme"))
        self.assertIsNotNone(self.start(ws="demo"))
        self.assertIsNone(self.start(ws="acme"))
        self.assertIsNone(self.start(ws="demo"))
        self.assertIsNotNone(self.start(account="acc2"))  # another account has its own count

    def test_sends_older_than_a_day_free_up(self):
        now = time.time()
        rec = self.start(daily=1, now=now - approvals.DAY - 1)
        approvals.end_app_run(rec["id"], "done", "Ran on its own.", self.root)
        self.assertIsNotNone(self.start(daily=1, now=now))

    def test_run_rows_end_and_stay_a_day(self):
        rec = self.start()
        self.assertEqual(rec["status"], "running")
        self.assertTrue(rec["auto"])
        out = approvals.end_app_run(rec["id"], "done", "Ran on its own.", self.root)
        self.assertEqual(out["status"], "done")
        self.assertEqual(approvals.count_app_sends(approvals.load(self.root), "acc1", time.time()), 1)

    def test_approve_over_cap_stays_pending(self):
        rec = self.start(daily=1)
        approvals.end_app_run(rec["id"], "done", "Ran on its own.", self.root)
        held = approvals.add_app_action(self.root, ws="acme", room=None, chat=None, bot="sales", person="ceo",
                                        toolkit="gmail", slug="GMAIL_SEND_EMAIL", arguments={}, account="acc1")
        with self.assertRaisesRegex(ValueError, "capped"):
            approvals.claim(held["id"], self.root, daily=1)
        self.assertEqual(approvals.get(held["id"], self.root)["status"], "pending")
        self.assertEqual(approvals.claim(held["id"], self.root, daily=2)["status"], "running")
        # A held write that is not a send is never capped.
        ev = approvals.add_app_action(self.root, ws="acme", room=None, chat=None, bot="sales", person="ceo",
                                      toolkit="googlecalendar", slug="GOOGLECALENDAR_CREATE_EVENT", arguments={},
                                      account="acc1")
        self.assertEqual(approvals.claim(ev["id"], self.root, daily=1)["status"], "running")

    def test_relay_refuses_over_cap(self):
        integrations.set_grant("hq", "cos", {"gmail": "act", "googlecalendar": "act"}, self.root)
        integrations.set_send_cap("acc1", 1, self.root)
        up = FakeUpstream()

        def handle(*items):
            reply, _ = apps_relay.handle(multi(*items), {**CTX, "taint": taint.Turn()}, None, up, self.root,
                                         catalog=CAT, read_only=lambda slug: True)
            return "\n".join(c.get("text", "") for c in reply["result"]["content"])

        first = handle(SEND)
        self.assertIn("Ran on its own", first)
        self.assertEqual(len(up.frames), 1)
        second = handle(SEND, EVENT)
        self.assertIn("work reached today's limit of 1 sends", second)
        self.assertEqual(len(up.frames), 2)
        forwarded = [t["tool_slug"] for t in up.frames[-1]["params"]["arguments"]["tools"]]
        self.assertEqual(forwarded, ["GOOGLECALENDAR_CREATE_EVENT"])
        rows = [r for r in approvals.load(self.root) if r.get("kind") == approvals.APP]
        self.assertEqual([r["status"] for r in rows], ["done", "done"])
        third = handle(SEND)
        self.assertIn("reached today's limit", third)
        self.assertEqual(len(up.frames), 2)  # nothing left to forward


if __name__ == "__main__":
    unittest.main()
