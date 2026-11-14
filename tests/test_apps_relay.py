"""The connected-apps relay: turn tokens, method allowlist, tool filter, read/hold/refuse gate."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from core import approvals, composio, integrations
from dashboard import apps_relay, taint

CAT = ["gmail", "googlecalendar", "googledrive", "instagram"]
CTX = {"ws": "hq", "bot": "cos", "chat": "c1", "room": "hq/general", "person": "ceo"}


class FakeUpstream:
    def __init__(self, result=None):
        self.frames = []
        self.result = result if result is not None else {"content": [{"type": "text", "text": "upstream ok"}]}

    def forward(self, frame, mcp_session_id):
        self.frames.append(frame)
        if frame.get("method", "").startswith("notifications/"):
            return None, "m-1"
        return {"jsonrpc": "2.0", "id": frame.get("id"), "result": self.result}, "m-1"


def call(name, arguments, rid=7):
    return {"jsonrpc": "2.0", "id": rid, "method": "tools/call", "params": {"name": name, "arguments": arguments}}


def multi(*items):
    return call("COMPOSIO_MULTI_EXECUTE_TOOL", {"tools": list(items)})


def text_of(reply):
    return "\n".join(c.get("text", "") for c in reply["result"]["content"])


class Tokens(unittest.TestCase):
    def test_token_lifecycle(self):
        toks = apps_relay.Tokens()
        t = toks.mint(dict(CTX))
        self.assertEqual(len(t), 64)
        self.assertEqual(toks.lookup(t)["bot"], "cos")
        self.assertIsNone(toks.lookup("wrong"))
        self.assertIsNone(toks.lookup(""))
        toks.revoke(t)
        self.assertIsNone(toks.lookup(t))
        t2 = toks.mint(dict(CTX))
        with mock.patch("dashboard.apps_relay.time.time", return_value=apps_relay.time.time() + apps_relay.MAX_AGE + 1):
            self.assertIsNone(toks.lookup(t2))


class Gate(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        integrations.set_grant("hq", "cos", {"gmail": "write", "googledrive": "read"}, self.root)
        integrations.note_known([{"id": "ca_g1", "toolkit": "gmail", "label": "work"},
                                 {"id": "ca_d1", "toolkit": "googledrive", "label": "drive"}], self.root)
        self.up = FakeUpstream()
        self.turn = taint.Turn()  # a clean conversation: only the owner's words so far

    def tearDown(self):
        self.tmp.cleanup()

    def handle(self, frame, **ctx):
        reply, _ = apps_relay.handle(frame, {**CTX, "taint": self.turn, **ctx}, None, self.up, self.root,
                                     catalog=CAT, read_only=self.tagged)
        return reply

    def tagged(self, slug):  # Composio's readOnlyHint tag: every tool here carries it
        return True

    def test_methods_allowlist(self):
        self.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        self.assertIsNone(self.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}))
        self.assertEqual(len(self.up.frames), 2)
        bad = self.handle({"jsonrpc": "2.0", "id": 3, "method": "resources/list"})
        self.assertIn("error", bad)
        self.assertEqual(len(self.up.frames), 2)

    def test_tools_list_filtered(self):
        names = ["COMPOSIO_SEARCH_TOOLS", "COMPOSIO_GET_TOOL_SCHEMAS", "COMPOSIO_MULTI_EXECUTE_TOOL",
                 "COMPOSIO_MANAGE_CONNECTIONS", "COMPOSIO_WAIT_FOR_CONNECTIONS", "COMPOSIO_REMOTE_WORKBENCH",
                 "COMPOSIO_REMOTE_BASH_TOOL", "COMPOSIO_PROXY_EXECUTE"]
        self.up.result = {"tools": [{"name": n} for n in names]}
        reply = self.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        self.assertEqual([t["name"] for t in reply["result"]["tools"]],
                         ["COMPOSIO_SEARCH_TOOLS", "COMPOSIO_GET_TOOL_SCHEMAS", "COMPOSIO_MULTI_EXECUTE_TOOL"])

    def test_search_and_schema_pass_through(self):
        f = call("COMPOSIO_SEARCH_TOOLS", {"queries": [{"use_case": "read inbox"}]})
        self.handle(f)
        self.assertEqual(self.up.frames[-1], f)

    def test_pass_through_rebuilt_from_name_and_arguments_only(self):
        # Like a read: no extra fields (_meta, task hints, ...) reach Composio.
        args = {"queries": [{"use_case": "read inbox"}]}
        f = call("COMPOSIO_SEARCH_TOOLS", args)
        f["params"]["_meta"] = {"progressToken": 1}
        f["extra"] = True
        self.handle(f)
        self.assertEqual(self.up.frames[-1], call("COMPOSIO_SEARCH_TOOLS", args))

    def test_read_forwarded_rebuilt(self):
        read = {"tool_slug": "GMAIL_FETCH_EMAILS", "arguments": {"max_results": 5}}
        f = multi(read)
        f["params"]["arguments"]["sync_response_to_workbench"] = True
        reply = self.handle(f)
        self.assertEqual(self.up.frames[-1]["params"]["arguments"], {"tools": [read]})
        self.assertIn("upstream ok", text_of(reply))

    def test_read_without_composios_tag_is_held(self):
        read = {"tool_slug": "GMAIL_FETCH_EMAILS", "arguments": {}}
        with mock.patch.object(composio, "tool_read_only", return_value=None) as asked:
            reply, _ = apps_relay.handle(multi(read), CTX, None, self.up, self.root, catalog=CAT)
        asked.assert_called_once_with("GMAIL_FETCH_EMAILS", self.root)
        self.assertEqual(self.up.frames, [])
        self.assertEqual(len(approvals.pending(self.root)), 1)
        self.assertIn("Held for approval", text_of(reply))

    def test_write_held_not_forwarded(self):
        reply = self.handle(multi({"tool_slug": "GMAIL_SEND_EMAIL",
                                   "arguments": {"recipient_email": "a@example.com", "body": "hi"}}))
        self.assertEqual(self.up.frames, [])
        rows = approvals.pending(self.root)
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["kind"], rows[0]["bot"], rows[0]["ws"], rows[0]["chat"]),
                         ("app_action", "cos", "hq", "c1"))
        self.assertEqual(rows[0]["action"]["arguments"]["body"], "hi")
        self.assertEqual(text_of(reply), f"Held for approval ({rows[0]['id']}): Send email. It has not run. "
                                         "Tell the user it is waiting in Approvals.")

    def test_report_only_job_refuses_writes_without_holding(self):
        send = {"tool_slug": "GMAIL_SEND_EMAIL", "arguments": {"recipient_email": "a@example.com"}}
        read = {"tool_slug": "GMAIL_FETCH_EMAILS", "arguments": {}}
        reply, _ = apps_relay.handle(multi(send, read), {**CTX, "report": True}, None, self.up, self.root, catalog=CAT,
                                     read_only=self.tagged)
        self.assertEqual(approvals.pending(self.root), [])
        self.assertEqual(self.up.frames[-1]["params"]["arguments"], {"tools": [read]})  # reads still run
        self.assertIn("only reports", text_of(reply))
        reply, _ = apps_relay.handle(multi(send), {**CTX, "report": True}, None, self.up, self.root, catalog=CAT,
                                     read_only=self.tagged)
        self.assertTrue(reply["result"]["isError"])
        self.assertEqual(approvals.pending(self.root), [])

    def test_mixed_call(self):
        reply = self.handle(multi({"tool_slug": "GMAIL_FETCH_EMAILS", "arguments": {}},
                                  {"tool_slug": "GMAIL_SEND_EMAIL", "arguments": {"recipient_email": "a@example.com"}}))
        self.assertEqual([t["tool_slug"] for t in self.up.frames[-1]["params"]["arguments"]["tools"]],
                         ["GMAIL_FETCH_EMAILS"])
        out = text_of(reply)
        self.assertIn("upstream ok", out)
        self.assertIn("Held for approval", out)

    def test_ungranted_and_blocked_refused(self):
        for slug in ("FIRECRAWL_SCRAPE", "NOPE_DO_IT"):
            reply = self.handle(multi({"tool_slug": slug, "arguments": {}}))
            self.assertEqual(text_of(reply), f'"{slug}" is not available to this bot. This call was not performed.')
        self.assertEqual(self.up.frames, [])
        self.assertEqual(approvals.pending(self.root), [])

    def test_ungranted_app_asks_the_owner_to_connect_it(self):
        reply = self.handle(multi({"tool_slug": "INSTAGRAM_GET_USER_INFO", "arguments": {}}))
        out = text_of(reply)
        self.assertIn("Ask the owner to connect instagram in Apps", out)
        self.assertIn("This call was not performed.", out)
        self.assertEqual(self.up.frames, [])

    def test_missing_account_asks_the_owner_to_connect_it(self):
        integrations.set_account("hq", "ca_g1", False, self.root)
        reply = self.handle(multi({"tool_slug": "GMAIL_SEND_EMAIL", "arguments": {}}))
        self.assertIn("Ask the owner to connect gmail in Apps", text_of(reply))

    def acts(self):
        integrations.set_grant("hq", "cos", {"gmail": "act"}, self.root)

    def test_act_grant_sends_without_a_card_and_keeps_a_record(self):
        self.acts()
        send = {"tool_slug": "GMAIL_SEND_EMAIL", "arguments": {"recipient_email": "a@example.com", "body": "hi"}}
        reply = self.handle(multi(send))
        self.assertEqual(self.up.frames[-1]["params"]["arguments"], {"tools": [send]})
        self.assertIn("upstream ok", text_of(reply))
        self.assertEqual(approvals.pending(self.root), [])
        done = [r for r in approvals.load(self.root) if r.get("kind") == approvals.APP]
        self.assertEqual([(r["status"], r["action"]["slug"], r.get("auto")) for r in done],
                         [("done", "GMAIL_SEND_EMAIL", True)])

    def test_act_grant_still_holds_a_delete(self):
        self.acts()
        reply = self.handle(multi({"tool_slug": "GMAIL_DELETE_MESSAGE", "arguments": {"message_id": "m1"}}))
        self.assertEqual(self.up.frames, [])
        self.assertIn("Held for approval", text_of(reply))

    def test_act_grant_in_a_report_only_job_sends_nothing(self):
        self.acts()
        reply, _ = apps_relay.handle(multi({"tool_slug": "GMAIL_SEND_EMAIL", "arguments": {}}), {**CTX, "report": True},
                                     None, self.up, self.root, catalog=CAT, read_only=self.tagged)
        self.assertEqual(self.up.frames, [])
        self.assertIn("only reports", text_of(reply))

    def test_act_grant_logs_a_failed_send_as_failed(self):
        self.acts()
        send = {"tool_slug": "GMAIL_SEND_EMAIL", "arguments": {"recipient_email": "a@example.com"}}
        read = {"tool_slug": "GMAIL_FETCH_EMAILS", "arguments": {}}
        self.up.result = {"content": [{"type": "text", "text": json.dumps({"successful": True, "data": {"results": [
            {"tool_slug": "GMAIL_FETCH_EMAILS", "index": 0, "response": {"successful": True, "data": {}}},
            {"tool_slug": "GMAIL_SEND_EMAIL", "index": 1,
             "response": {"successful": False, "error": "invalid recipient"}}]}})}]}
        reply = self.handle(multi(read, send))
        rows = [r for r in approvals.load(self.root) if r.get("kind") == approvals.APP]
        self.assertEqual([(r["status"], r["result"]) for r in rows], [("failed", "Failed: invalid recipient")])
        self.assertNotIn("Ran on its own", text_of(reply))
        self.assertIn("Did not go out: Send email", text_of(reply))

    def test_act_grant_error_reply_is_not_logged_as_sent(self):
        self.acts()
        self.up.result = {"content": [{"type": "text", "text": "boom"}], "isError": True}
        reply = self.handle(multi({"tool_slug": "GMAIL_SEND_EMAIL", "arguments": {}}))
        rows = [r for r in approvals.load(self.root) if r.get("kind") == approvals.APP]
        self.assertEqual([r["status"] for r in rows], ["failed"])
        self.assertNotIn("Ran on its own", text_of(reply))

    def test_send_without_asking_also_sends_after_outside_content(self):
        # The owner's per-app choice: "Sends without asking" sends whatever the conversation read.
        self.acts()
        self.turn.saw_outside()
        send = {"tool_slug": "GMAIL_SEND_EMAIL", "arguments": {"recipient_email": "a@example.com"}}
        reply = self.handle(multi(send, send))
        self.assertEqual(len(self.up.frames), 1)
        self.assertEqual(approvals.pending(self.root), [])
        self.assertIn("Ran on its own", text_of(reply))

    def test_a_read_marks_the_conversation(self):
        self.acts()
        self.handle(multi({"tool_slug": "GMAIL_FETCH_EMAILS", "arguments": {}}))
        self.assertTrue(self.turn.app_data and self.turn.outside)
        self.handle(multi({"tool_slug": "GMAIL_SEND_EMAIL", "arguments": {"recipient_email": "a@example.com"}}))
        self.assertEqual(len(self.up.frames), 2)  # the owner chose "Sends without asking"
        self.assertEqual(approvals.pending(self.root), [])

    def test_a_send_beside_a_read_in_one_call_runs(self):
        # Written before the read's result existed: the owner's words alone decided it.
        self.acts()
        send = {"tool_slug": "GMAIL_SEND_EMAIL", "arguments": {"recipient_email": "a@example.com"}}
        self.handle(multi({"tool_slug": "GMAIL_FETCH_EMAILS", "arguments": {}}, send))
        self.assertEqual(len(self.up.frames[-1]["params"]["arguments"]["tools"]), 2)
        self.assertEqual(approvals.pending(self.root), [])

    def test_send_without_asking_needs_no_taint_record(self):
        self.acts()
        send = {"tool_slug": "GMAIL_SEND_EMAIL", "arguments": {"recipient_email": "a@example.com"}}
        reply, _ = apps_relay.handle(multi(send), CTX, None, self.up, self.root, catalog=CAT, read_only=self.tagged)
        self.assertEqual(len(self.up.frames), 1)
        self.assertEqual(approvals.pending(self.root), [])

    def test_writes_held_together_share_one_batch(self):
        sends = [{"tool_slug": "GMAIL_SEND_EMAIL", "arguments": {"recipient_email": f"p{i}@example.com"}}
                 for i in range(3)]
        reply = self.handle(multi(*sends))
        rows = approvals.pending(self.root)
        self.assertEqual(len(rows), 3)
        self.assertEqual(len({r["batch"] for r in rows}), 1)
        self.assertIn("3 actions wait for one approval", text_of(reply))
        alone = self.handle(multi(sends[0]))
        self.assertEqual(len([r for r in approvals.pending(self.root) if "batch" not in r]), 1)
        self.assertIn("Held for approval", text_of(alone))

    def test_read_grant_refuses_write(self):
        reply = self.handle(multi({"tool_slug": "GOOGLEDRIVE_UPLOAD_FILE", "arguments": {}}))
        self.assertIn("read-only access to googledrive", text_of(reply))
        self.assertIn("ask the owner", text_of(reply))
        self.assertEqual(approvals.pending(self.root), [])

    def test_hidden_meta_tool_refused(self):
        for name in ("COMPOSIO_REMOTE_BASH_TOOL", "COMPOSIO_MANAGE_CONNECTIONS", "GMAIL_SEND_EMAIL"):
            reply = self.handle(call(name, {}))
            self.assertIn("not available", text_of(reply))
        self.assertEqual(self.up.frames, [])

    def test_malformed_multi_execute_refused(self):
        reply = self.handle(call("COMPOSIO_MULTI_EXECUTE_TOOL", {"tools": [{"arguments": {}}]}))
        self.assertIn("not performed", text_of(reply))
        self.assertEqual(self.up.frames, [])

    def test_grant_reread_each_call(self):
        integrations.set_grant("hq", "cos", {}, self.root)
        reply = self.handle(multi({"tool_slug": "GMAIL_FETCH_EMAILS", "arguments": {}}))
        self.assertIn("connect gmail in Apps", text_of(reply))
        self.assertEqual(self.up.frames, [])

    def second_gmail(self):
        known = [{"id": a, **r} for a, r in integrations.known(self.root).items()]
        integrations.note_known(known + [{"id": "ca_g2", "toolkit": "gmail", "label": "personal"}], self.root)

    def test_no_account_picked_refused(self):
        integrations.set_account("hq", "ca_g1", False, self.root)
        reply = self.handle(multi({"tool_slug": "GMAIL_FETCH_EMAILS", "arguments": {}},
                                  {"tool_slug": "GMAIL_SEND_EMAIL", "arguments": {}}))
        self.assertIn("gmail has no account picked in this workspace", text_of(reply))
        self.assertEqual(self.up.frames, [])
        self.assertEqual(approvals.pending(self.root), [])

    def test_one_account_is_the_pin(self):
        # One account: nothing extra travels, the held write still names it.
        read = {"tool_slug": "GMAIL_FETCH_EMAILS", "arguments": {}}
        self.handle(multi(read, {"tool_slug": "GMAIL_SEND_EMAIL", "arguments": {}}))
        self.assertEqual(self.up.frames[-1]["params"]["arguments"], {"tools": [read]})
        rec = approvals.pending(self.root)[0]
        self.assertEqual(rec["action"]["account"], "ca_g1")
        self.assertEqual(rec["preview"]["fields"][0], ["Account", "work"])

    def test_several_accounts_need_a_choice(self):
        self.second_gmail()
        reply = self.handle(multi({"tool_slug": "GMAIL_FETCH_EMAILS", "arguments": {}}))
        out = text_of(reply)
        self.assertIn('"ca_g1" (work)', out)
        self.assertIn('"ca_g2" (personal)', out)
        self.assertEqual(self.up.frames, [])
        self.handle(multi({"tool_slug": "GMAIL_FETCH_EMAILS", "arguments": {}, "account": "ca_g2"}))
        self.assertEqual(self.up.frames[-1]["params"]["arguments"]["tools"],
                         [{"tool_slug": "GMAIL_FETCH_EMAILS", "arguments": {}, "account": "ca_g2"}])
        self.handle(multi({"tool_slug": "GMAIL_SEND_EMAIL", "arguments": {}, "account": "ca_g2"}))
        self.assertEqual(approvals.pending(self.root)[0]["action"]["account"], "ca_g2")

    def test_account_of_another_workspace_refused(self):
        self.second_gmail()
        integrations.set_account("hq", "ca_g2", False, self.root)
        integrations.set_account("acme", "ca_g2", True, self.root)
        reply = self.handle(multi({"tool_slug": "GMAIL_SEND_EMAIL", "arguments": {}, "account": "ca_g2"}))
        self.assertIn('gmail accounts: "ca_g1" (work). This call was not performed.', text_of(reply))
        self.assertEqual(approvals.pending(self.root), [])

    def test_non_string_account_unreadable(self):
        reply = self.handle(multi({"tool_slug": "GMAIL_FETCH_EMAILS", "arguments": {}, "account": ["ca_g1"]}))
        self.assertIn("could not be read", text_of(reply))


class ComposioUpstreamRetry(unittest.TestCase):
    def test_resets_session_once_on_404(self):
        tmp = tempfile.TemporaryDirectory()
        root = Path(tmp.name)
        self.addCleanup(tmp.cleanup)
        sessions = iter([{"session_id": "s1", "mcp_url": "u1"}, {"session_id": "s2", "mcp_url": "u2"}])
        seen = []

        def fwd(url, frame, sid, r):
            seen.append(url)
            if url == "u1":
                raise composio.ComposioError("gone", status=404)
            return {"result": {}}, None
        with mock.patch.object(composio, "ensure_session", side_effect=lambda p, ws, r: next(sessions)), \
                mock.patch.object(composio, "reset_session") as reset, \
                mock.patch.object(composio, "forward", side_effect=fwd):
            reply, _ = apps_relay.ComposioUpstream("ceo", "acme", root).forward({"id": 1}, None)
        self.assertEqual(seen, ["u1", "u2"])
        reset.assert_called_once_with("ceo", "acme", root)
        self.assertEqual(reply, {"result": {}})


if __name__ == "__main__":
    unittest.main()
