"""Connected apps over HTTP: relay route, integrations API, approve/reject."""
import io
import json
import tempfile
import unittest
from email.message import Message
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import dashboard.server as server
from core import approvals, composio, integrations

PORT = 8765
ORIGIN = f"http://127.0.0.1:{PORT}"
CAT = [{"slug": s, "name": s, "logo": None, "auth": "managed"}
       for s in ("gmail", "googlecalendar", "googledrive", "instagram")]
ACCTS = [{"id": "ca_g1", "toolkit": "gmail", "label": "work"}]


def request(method, path, body=None, *, cookie=True, origin=True, host=f"127.0.0.1:{PORT}", headers=None):
    h = server.Handler.__new__(server.Handler)
    out = {"headers": {}}
    h.wfile = io.BytesIO()
    h.send_response = lambda code, *a: out.setdefault("code", code)
    h.send_header = lambda k, v: out["headers"].__setitem__(k, v)
    h.end_headers = lambda: None
    h.log_message = lambda *a: None
    raw = b"" if body is None else json.dumps(body).encode()
    msg = Message()
    msg["Host"] = host
    if raw:
        msg["Content-Type"] = "application/json"
    msg["Content-Length"] = str(len(raw))
    if cookie:
        msg["Cookie"] = f"{server.AUTH_COOKIE}={server.AUTH_TOKEN}"
    if origin:
        msg["Origin"] = ORIGIN
    for k, v in (headers or {}).items():
        msg[k] = v
    h.headers, h.rfile, h.path = msg, io.BytesIO(raw), path
    h.client_address = ("127.0.0.1", 50000)
    h.server = SimpleNamespace(server_address=("127.0.0.1", PORT))
    getattr(h, "do_" + method)()
    payload = h.wfile.getvalue()
    try:
        data = json.loads(payload) if payload else None
    except ValueError:
        data = payload
    return out["code"], data, out["headers"], payload


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "chats").mkdir()
        for p in (mock.patch.object(server, "ROOT", self.root),
                  mock.patch.object(server.chat_store, "CHATS_DIR", self.root / "chats"),
                  mock.patch.object(server.db, "db_exists", return_value=True),
                  mock.patch.object(server.db, "tree", return_value=[]),  # never the real os.db
                  mock.patch.object(server.composio, "catalog", return_value=CAT),
                  mock.patch.object(server.composio, "accounts", return_value=ACCTS),
                  mock.patch.object(server.composio, "check_key", return_value=None)):
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self.tmp.cleanup)
        integrations.note_known(ACCTS, self.root)  # joins HQ, as a first connect does


class Relay(Base):
    PATH = "/api/internal/apps/mcp"
    FRAME = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}

    def setUp(self):
        super().setUp()
        self.token = server.apps_relay.TOKENS.mint(
            {"ws": "hq", "bot": "cos", "chat": "c1", "room": "hq/general", "person": "ceo"})
        self.addCleanup(server.apps_relay.TOKENS.revoke, self.token)
        up = mock.patch.object(server.apps_relay.ComposioUpstream, "forward",
                               return_value=({"jsonrpc": "2.0", "id": 1, "result": {"tools": []}}, "m-1"))
        up.start()
        self.addCleanup(up.stop)

    def test_relay_reachable_without_cookie_with_token(self):
        code, data, hdrs, _ = request("POST", self.PATH, self.FRAME, cookie=False, origin=False,
                                      headers={"Authorization": f"Bearer {self.token}"})
        self.assertEqual(code, 200)
        self.assertEqual(data["result"], {"tools": []})
        self.assertEqual(hdrs.get("mcp-session-id"), "m-1")

    def test_relay_refuses_cookie_only(self):
        self.assertEqual(request("POST", self.PATH, self.FRAME)[0], 403)

    def test_relay_refuses_bad_token(self):
        self.assertEqual(request("POST", self.PATH, self.FRAME, cookie=False,
                                 headers={"Authorization": "Bearer nope"})[0], 403)

    def test_relay_refuses_bad_host(self):
        self.assertEqual(request("POST", self.PATH, self.FRAME, cookie=False, host="evil.example",
                                 headers={"Authorization": f"Bearer {self.token}"})[0], 403)

    def test_relay_get_is_405(self):
        self.assertEqual(request("GET", self.PATH, cookie=False)[0], 405)


class IntegrationsApi(Base):
    def test_integrations_never_return_key(self):
        code, _, _, _ = request("POST", "/api/integrations/key", {"api_key": "ak_topsecret999"})
        self.assertEqual(code, 200)
        self.assertEqual(integrations.api_key(self.root), "ak_topsecret999")
        for method, path, body in (("GET", "/api/integrations", None),
                                   ("GET", "/api/integrations/catalog?q=gm", None),
                                   ("POST", "/api/integrations/grants",
                                    {"ws": "hq", "bot": "cos", "apps": {"gmail": "read"}})):
            code, data, _, raw = request(method, path, body)
            self.assertEqual(code, 200, path)
            self.assertNotIn(b"ak_topsecret999", raw, path)
        code, data, _, _ = request("GET", "/api/integrations")
        self.assertEqual((data["configured"], data["connected"]), (True, ["gmail"]))
        self.assertTrue(any("instagram" in g["apps"] for g in data["pinned"]))
        self.assertEqual(data["grants"]["hq"]["cos"], {"gmail": "read"})
        self.assertEqual(data["accounts"], ACCTS)
        self.assertEqual(data["picked"], {"hq": {"gmail": ["ca_g1"]}})
        self.assertEqual(data["workspaces"], [{"id": "hq", "name": "HQ"}])

    def test_offline_view_keeps_last_accounts(self):
        integrations.set_key("ak_x", self.root)
        with mock.patch.object(server.composio, "accounts", side_effect=composio.ComposioError("down")):
            data = request("GET", "/api/integrations")[1]
        self.assertTrue(data["offline"])
        self.assertEqual(data["accounts"], ACCTS)

    def test_pick_account_for_a_project(self):
        tree = [{"slug": "acme", "name": "Acme"}]
        with mock.patch.object(server.db, "tree", return_value=tree):
            code, data, _, _ = request("POST", "/api/integrations/accounts",
                                       {"ws": "acme", "account": "ca_g1", "on": True})
            self.assertEqual((code, data["picked"]), (200, {"gmail": ["ca_g1"]}))
            self.assertEqual(request("POST", "/api/integrations/accounts",
                                     {"ws": "acme", "account": "ca_g1", "on": True}, origin=False)[0], 403)
            self.assertEqual(request("POST", "/api/integrations/accounts",
                                     {"ws": "gone", "account": "ca_g1", "on": True})[0], 400)
            self.assertEqual(request("POST", "/api/integrations/accounts",
                                     {"ws": "acme", "account": "ca_nope", "on": True})[0], 400)
            view = request("GET", "/api/integrations")[1]
        self.assertEqual(view["picked"]["acme"], {"gmail": ["ca_g1"]})
        self.assertEqual(integrations.accounts_for("hq", self.root), {"gmail": ["ca_g1"]})  # both now

    def test_rename_account(self):
        with mock.patch.object(server.composio, "rename_account") as ren:
            code, data, _, _ = request("POST", "/api/integrations/rename", {"account": "ca_g1", "alias": "Sales"})
            self.assertEqual(code, 200)
            ren.assert_called_once_with("ca_g1", "Sales", self.root)
            for bad in ({"account": "ca_g1", "alias": ""}, {"account": "ca_g1", "alias": "<b>x</b>"},
                        {"account": "ca_nope", "alias": "ok"}):
                self.assertEqual(request("POST", "/api/integrations/rename", bad)[0], 400, bad)
        self.assertEqual(integrations.known(self.root)["ca_g1"]["label"], "Sales")

    def test_disconnect_account(self):
        tree = [{"slug": "acme", "name": "Acme"}]
        with mock.patch.object(server.db, "tree", return_value=tree):
            request("POST", "/api/integrations/accounts", {"ws": "acme", "account": "ca_g1", "on": True})
        with mock.patch.object(server.composio, "disconnect_account") as dc:
            self.assertEqual(request("POST", "/api/integrations/disconnect", {"account": "ca_g1"}, origin=False)[0], 403)
            dc.assert_not_called()
            self.assertEqual(request("POST", "/api/integrations/disconnect", {"account": "ca_nope"})[0], 400)
            code, data, _, _ = request("POST", "/api/integrations/disconnect", {"account": "ca_g1"})
        self.assertEqual((code, data["disconnected"]), (200, True))
        dc.assert_called_once_with("ca_g1", self.root)
        self.assertEqual(integrations.accounts_for("hq", self.root), {})
        self.assertEqual(integrations.accounts_for("acme", self.root), {})

    def test_disconnect_failure_keeps_account(self):
        with mock.patch.object(server.composio, "disconnect_account",
                               side_effect=composio.ComposioError("Composio HTTP 500", status=500)):
            self.assertEqual(request("POST", "/api/integrations/disconnect", {"account": "ca_g1"})[0], 502)
        self.assertEqual(integrations.accounts_for("hq", self.root), {"gmail": ["ca_g1"]})
        with mock.patch.object(server.composio, "disconnect_account",
                               side_effect=composio.ComposioError("Composio HTTP 404", status=404)):
            self.assertEqual(request("POST", "/api/integrations/disconnect", {"account": "ca_g1"})[0], 200)
        self.assertEqual(integrations.accounts_for("hq", self.root), {})

    def test_rename_requires_origin(self):
        with mock.patch.object(server.composio, "rename_account") as ren:
            self.assertEqual(request("POST", "/api/integrations/rename",
                                     {"account": "ca_g1", "alias": "x"}, origin=False)[0], 403)
        ren.assert_not_called()

    def test_deleted_project_leaves_no_apps_behind(self):
        integrations.set_account("acme", "ca_g1", True, self.root)
        integrations.set_grant("acme", "cos", {"gmail": "write"}, self.root)
        with mock.patch.object(server.fileops, "delete_project", return_value={"slug": "acme", "deleted": True}):
            self.assertEqual(request("POST", "/api/project/acme/delete", {})[0], 200)
        self.assertEqual(integrations.accounts_for("acme", self.root), {})
        self.assertEqual(integrations.grant("acme", "cos", self.root), {})

    def test_refused_project_delete_keeps_apps(self):
        integrations.set_account("acme", "ca_g1", True, self.root)
        with mock.patch.object(server.fileops, "delete_project", side_effect=server.fileops.ActionError("in use")):
            self.assertNotEqual(request("POST", "/api/project/acme/delete", {})[0], 200)
        self.assertEqual(integrations.accounts_for("acme", self.root), {"gmail": ["ca_g1"]})

    def test_logo_served_from_this_origin_sandboxed(self):
        integrations.set_key("ak_x", self.root)
        with mock.patch.object(server.composio, "logo", return_value=(b"<svg/>", "image/svg+xml")):
            code, _, hdrs, raw = request("GET", "/api/integrations/logo/gmail")
        self.assertEqual((code, raw, hdrs["Content-Type"]), (200, b"<svg/>", "image/svg+xml"))
        self.assertIn("sandbox", hdrs["Content-Security-Policy"])
        with mock.patch.object(server.composio, "logo", return_value=None):
            self.assertEqual(request("GET", "/api/integrations/logo/gmail")[0], 404)
        with mock.patch.object(server.composio, "logo") as lg:
            self.assertEqual(request("GET", "/api/integrations/logo/..%2Fx")[0], 404)
            lg.assert_not_called()
        self.assertEqual(request("GET", "/api/integrations/logo/gmail", cookie=False)[0], 403)

    def test_refused_key_is_not_saved(self):
        refused = composio.ComposioError("Composio HTTP 401", status=401, reason="APIKey_InvalidAPIKey")
        with mock.patch.object(server.composio, "check_key", side_effect=refused) as ck:
            code, data, _, raw = request("POST", "/api/integrations/key", {"api_key": " ak_otherproject1 "})
        ck.assert_called_once_with("ak_otherproject1", self.root)
        self.assertEqual(code, 400)
        self.assertIn("project API key", data["error"])
        self.assertIn("(Composio: APIKey_InvalidAPIKey)", data["error"])
        self.assertNotIn(b"ak_otherproject1", raw)
        self.assertIsNone(integrations.api_key(self.root))

    def test_consumer_key_named_and_never_sent(self):
        with mock.patch.object(server.composio, "check_key") as ck:
            code, data, _, raw = request("POST", "/api/integrations/key", {"api_key": "ck_consumerkey9"})
        ck.assert_not_called()
        self.assertEqual(code, 400)
        self.assertIn("consumer MCP key", data["error"])
        self.assertNotIn(b"ck_consumerkey9", raw)
        self.assertIsNone(integrations.api_key(self.root))

    def test_key_saved_when_composio_unreachable(self):
        down = composio.ComposioError("Composio unreachable (URLError)")
        with mock.patch.object(server.composio, "check_key", side_effect=down):
            self.assertEqual(request("POST", "/api/integrations/key", {"api_key": "ak_x"})[0], 200)
        self.assertEqual(integrations.api_key(self.root), "ak_x")

    def test_view_says_when_saved_key_is_refused(self):
        integrations.set_key("ak_x", self.root)
        with mock.patch.object(server.composio, "accounts",
                               side_effect=composio.ComposioError("Composio HTTP 401", status=401)):
            data = request("GET", "/api/integrations")[1]
        self.assertIn("project API key", data["key_refused"])
        with mock.patch.object(server.composio, "accounts", side_effect=composio.ComposioError("down")):
            self.assertNotIn("key_refused", request("GET", "/api/integrations")[1])

    def test_key_requires_origin(self):
        self.assertEqual(request("POST", "/api/integrations/key", {"api_key": "ak_x"}, origin=False)[0], 403)

    def test_delete_key(self):
        integrations.set_key("ak_x", self.root)
        self.assertEqual(request("DELETE", "/api/integrations/key")[0], 200)
        self.assertIsNone(integrations.api_key(self.root))

    def test_catalog_search(self):
        code, data, _, _ = request("GET", "/api/integrations/catalog?q=insta")
        self.assertEqual([a["slug"] for a in data["apps"]], ["instagram"])

    def test_connect_blocked_400(self):
        integrations.set_key("ak_x", self.root)
        self.assertEqual(request("POST", "/api/integrations/connect", {"toolkit": "firecrawl"})[0], 400)

    def test_connect_returns_url(self):
        integrations.set_key("ak_x", self.root)
        link = {"url": "https://backend.composio.dev/l/1", "account_id": "ca_new"}
        with mock.patch.object(server.composio, "connect_link", return_value=link):
            code, data, _, _ = request("POST", "/api/integrations/connect", {"toolkit": "gmail"})
        self.assertEqual((code, data), (200, link))

    def test_grants_unknown_workspace_400(self):
        # An unknown workspace must not fall back to HQ and grant an HQ bot apps.
        with mock.patch.object(server.db, "tree", return_value=[]):
            code = request("POST", "/api/integrations/grants",
                           {"ws": "gone-project", "bot": "cos", "apps": {"gmail": "write"}})[0]
        self.assertEqual(code, 400)
        self.assertEqual(integrations.all_grants(self.root), {})

    def test_grants_invalid_400(self):
        self.assertEqual(request("POST", "/api/integrations/grants",
                                 {"ws": "hq", "bot": "cos", "apps": {"firecrawl": "write"}})[0], 400)


class Approve(Base):
    def setUp(self):
        super().setUp()
        integrations.set_key("ak_x", self.root)
        server.chat_store.save({"id": "0" * 8 + "-0000-0000-0000-" + "0" * 12, "messages": []})
        self.chat = "0" * 8 + "-0000-0000-0000-" + "0" * 12
        self.rec = approvals.add_app_action(self.root, ws="hq", room="hq/general", chat=self.chat, bot="cos",
                                            person="ceo", toolkit="gmail", slug="GMAIL_SEND_EMAIL",
                                            arguments={"recipient_email": "a@example.com", "body": "hi"})
        self.path = f"/api/approvals/{self.rec['id']}"

    def test_approve_executes_once(self):
        with mock.patch.object(server.composio, "execute", return_value={"successful": True, "data": {}}) as ex:
            code, data, _, _ = request("POST", self.path + "/approve", {})
            again = request("POST", self.path + "/approve", {})[0]
        self.assertEqual((code, data["status"]), (200, "done"))
        self.assertEqual(again, 409)
        ex.assert_called_once_with("ceo", "hq", "GMAIL_SEND_EMAIL",
                                   {"recipient_email": "a@example.com", "body": "hi"}, "ca_g1", self.root)
        notes = server.chat_store.load(self.chat)["messages"]
        self.assertEqual(notes[-1], {**notes[-1], "role": "assistant", "bot": "cos", "content": "Sent."})

    def batch(self):
        return [approvals.add_app_action(self.root, ws="hq", room="hq/general", chat=self.chat, bot="cos",
                                         person="ceo", toolkit="gmail", slug="GMAIL_SEND_EMAIL",
                                         arguments={"recipient_email": f"p{i}@example.com"}, batch="b1")
                for i in range(3)]

    def test_approve_all_runs_the_whole_batch_once(self):
        recs = self.batch()
        with mock.patch.object(server.composio, "execute", return_value={"successful": True, "data": {}}) as ex:
            code, data, _, _ = request("POST", "/api/approvals/batch/b1/approve", {})
            again = request("POST", "/api/approvals/batch/b1/approve", {})
        self.assertEqual(code, 200)
        self.assertEqual(sorted(r["id"] for r in data["results"]), sorted(r["id"] for r in recs))
        self.assertEqual({r["status"] for r in data["results"]}, {"done"})
        self.assertEqual(ex.call_count, 3)
        self.assertEqual(again[0], 404)  # nothing of b1 is pending now
        self.assertEqual(approvals.get(self.rec["id"], self.root)["status"], "pending")  # not in the batch

    def test_reject_all_sends_nothing(self):
        self.batch()
        with mock.patch.object(server.composio, "execute") as ex:
            code = request("POST", "/api/approvals/batch/b1/reject", {})[0]
        self.assertEqual(code, 200)
        ex.assert_not_called()
        self.assertEqual([r for r in approvals.pending(self.root) if r.get("batch") == "b1"], [])

    def test_approve_all_requires_origin(self):
        self.batch()
        with mock.patch.object(server.composio, "execute") as ex:
            self.assertEqual(request("POST", "/api/approvals/batch/b1/approve", {}, origin=False)[0], 403)
        ex.assert_not_called()

    def test_approve_requires_origin(self):
        with mock.patch.object(server.composio, "execute") as ex:
            self.assertEqual(request("POST", self.path + "/approve", {}, origin=False)[0], 403)
        ex.assert_not_called()

    def test_reject_no_call(self):
        with mock.patch.object(server.composio, "execute") as ex:
            code, data, _, _ = request("POST", self.path + "/reject", {})
        self.assertEqual((code, data["status"]), (200, "rejected"))
        ex.assert_not_called()

    def test_approve_still_runs_after_grant_raised_to_act(self):
        # Held under "write"; the owner then lets the bot act on its own: the card still runs.
        integrations.set_grant("hq", "cos", {"gmail": "act"}, self.root)
        with mock.patch.object(server.composio, "execute", return_value={"successful": True, "data": {}}) as ex:
            code, data, _, _ = request("POST", self.path + "/approve", {})
        self.assertEqual((code, data["status"]), (200, "done"))
        ex.assert_called_once()

    def test_approve_refused_after_grant_removed(self):
        integrations.set_grant("hq", "cos", {}, self.root)
        with mock.patch.object(server.composio, "execute") as ex:
            code, data, _, _ = request("POST", self.path + "/approve", {})
        self.assertEqual(code, 409)
        ex.assert_not_called()
        self.assertEqual(approvals.get(self.rec["id"], self.root)["status"], "pending")

    def test_approve_refused_when_not_connected(self):
        with mock.patch.object(server.composio, "accounts", return_value=[]), \
                mock.patch.object(server.composio, "execute") as ex:
            self.assertEqual(request("POST", self.path + "/approve", {})[0], 409)
        ex.assert_not_called()

    def test_approve_refused_when_account_unpicked(self):
        # The card named this account: it never runs on another one instead.
        integrations.note_known(ACCTS + [{"id": "ca_g2", "toolkit": "gmail", "label": "other"}], self.root)
        rec = approvals.add_app_action(self.root, ws="hq", room="hq/general", chat=self.chat, bot="cos",
                                       person="ceo", toolkit="gmail", slug="GMAIL_SEND_EMAIL",
                                       arguments={}, account="ca_g1")
        self.assertEqual(rec["preview"]["fields"][0], ["Account", "work"])
        integrations.set_account("hq", "ca_g1", False, self.root)
        with mock.patch.object(server.composio, "execute") as ex:
            self.assertEqual(request("POST", f"/api/approvals/{rec['id']}/approve", {})[0], 409)
        ex.assert_not_called()

    def test_approve_runs_on_the_held_account(self):
        two = ACCTS + [{"id": "ca_g2", "toolkit": "gmail", "label": "other"}]
        integrations.note_known(two, self.root)
        rec = approvals.add_app_action(self.root, ws="hq", room="hq/general", chat=self.chat, bot="cos",
                                       person="ceo", toolkit="gmail", slug="GMAIL_SEND_EMAIL",
                                       arguments={}, account="ca_g2")
        with mock.patch.object(server.composio, "accounts", return_value=two), \
                mock.patch.object(server.composio, "execute", return_value={"result_type": "failed",
                                                                            "error": "quota"}) as ex:
            data = request("POST", f"/api/approvals/{rec['id']}/approve", {})[1]
        self.assertEqual(ex.call_args[0][4], "ca_g2")
        self.assertEqual((data["status"], data["result"]), ("failed", "Failed: quota"))

    def test_approve_refused_without_key(self):
        integrations.remove_key(self.root)
        with mock.patch.object(server.composio, "execute") as ex:
            self.assertEqual(request("POST", self.path + "/approve", {})[0], 409)
        ex.assert_not_called()

    def test_approve_with_edit_uses_edited_args(self):
        edited = {"recipient_email": "z@example.com", "body": "edited"}
        with mock.patch.object(server.composio, "execute", return_value={"successful": True}) as ex:
            request("POST", self.path + "/approve", {"arguments": edited})
        self.assertEqual(ex.call_args[0][3], edited)

    def test_approve_timeout_marks_failed(self):
        with mock.patch.object(server.composio, "execute",
                               side_effect=composio.ComposioError("Composio unreachable (TimeoutError)")):
            code, data, _, _ = request("POST", self.path + "/approve", {})
        self.assertEqual((code, data["status"]), (200, "failed"))
        self.assertEqual(data["result"], approvals.STALE_NOTE)

    def test_approve_cut_off_reply_marks_failed_may_have_run(self):
        with mock.patch.object(server.composio, "execute",
                               side_effect=composio.ComposioError("Composio unreachable (IncompleteRead)")):
            data = request("POST", self.path + "/approve", {})[1]
        self.assertEqual((data["status"], data["result"]), ("failed", approvals.STALE_NOTE))

    def test_approve_unsuccessful_marks_failed(self):
        with mock.patch.object(server.composio, "execute", return_value={"successful": False, "error": "bad to"}):
            code, data, _, _ = request("POST", self.path + "/approve", {})
        self.assertEqual((data["status"], data["result"]), ("failed", "Failed: bad to"))


CHAT = "11111111-2222-3333-4444-555555555555"


class NeedsApps(unittest.TestCase):
    def test_needs_apps_table(self):
        gm, gc, dr = {"gmail": "write"}, {"googlecalendar": "write"}, {"googledrive": "read"}
        cases = [
            ("summarize my inbox", gm, False, True),
            ("any unread from the bank?", gm, False, True),
            ("write an email newsletter", gm, False, False),
            ("add to my calendar tomorrow", gc, False, False),
            ("set a meeting with Ana on google calendar", gc, False, True),
            ("write a LinkedIn post", {"linkedin": "write"}, False, False),
            ("check my instagram stats", {"instagram": "read"}, False, True),
            ("how is my google search console doing", {"google_search_console": "read"}, False, True),
            ("send a message (test message) using the insta profile to people", {"instagram": "write"}, False, True),
            ("dm them on ig", {"instagram": "write"}, False, True),
            ("reach out to Ana on Instagram", {"instagram": "write"}, False, True),
            ("what does the instagram account look like", {"instagram": "read"}, False, True),
            ("write an instagram caption about launch day", {"instagram": "write"}, False, False),
            ("plan a LinkedIn post and a facebook carousel", {"linkedin": "write", "facebook": "write"}, False, False),
            ("it is available through composio", {"instagram": "write"}, False, True),
            ("@apps list files", dr, False, True),
            ("summarize my inbox", {}, False, False),
            ("summarize my inbox", gm, True, False),
        ]
        for text, grant, explicit, want in cases:
            self.assertIs(server._needs_apps(text, grant, explicit), want, text)


class Turn(Base):
    def setUp(self):
        super().setUp()
        integrations.set_key("ak_x", self.root)

    def run_turn(self, text, replies=(), apps_used=False, hold=False):
        seen = []
        self_root = self.root
        replies = list(replies)

        def make(who):
            class S:
                _started = False
                _turn_count = 0
                _last_skill = None
                _skill_explicit = False
                session_id = CHAT

                def __init__(self, *a, **k):
                    self.apps_used = apps_used

                def is_fresh(self):
                    return not self._started

                def note_skill(self, *a, **k):
                    pass

                def note_bot(self, *a):
                    pass

                def close(self):
                    pass

                def ask(self, prompt, **kw):
                    seen.append({"who": who, "apps_used": self.apps_used, **kw})
                    if kw.get("apps_token") and hold:  # what the relay does mid-turn
                        approvals.add_app_action(self_root, ws="hq", room="hq/general", chat=CHAT, bot="cos",
                                                 person="ceo", toolkit="gmail", slug="GMAIL_SEND_EMAIL",
                                                 arguments={"recipient_email": "a@example.com"})
                    yield ("delta", replies.pop(0) if replies else "ok")
                    yield ("done", {})
            return S
        lead = make("lead")()
        sink = io.BytesIO()
        h = server.Handler.__new__(server.Handler)
        h.wfile = sink
        h.send_response = h.send_header = h.end_headers = lambda *a, **k: None
        h.log_message = lambda *a: None
        h.server = SimpleNamespace(server_address=("127.0.0.1", PORT))
        with mock.patch.object(server, "get_chat_session", return_value=lead), \
                mock.patch.object(server.db, "tree", return_value=[]), \
                mock.patch.object(server.chat_session, "ChatSession", make("mate")):
            h._handle_ask({"messages": [{"role": "user", "content": text}]})
        return seen, lead, sink.getvalue().decode()

    def test_project_without_picked_account_has_no_app_turn(self):
        tree = [{"slug": "acme", "name": "Acme"}]
        integrations.set_grant("acme", "cos", {"gmail": "write"}, self.root)
        with mock.patch.object(server.db, "tree", return_value=tree):
            self.assertEqual(server.integrations.effective_grant("acme", "cos", self.root), {})
            integrations.set_account("acme", "ca_g1", True, self.root)
            self.assertEqual(server.integrations.effective_grant("acme", "cos", self.root), {"gmail": "write"})

    def test_accounts_note_lists_only_this_workspace(self):
        integrations.note_known(ACCTS + [{"id": "ca_g2", "toolkit": "gmail", "label": "side"}], self.root)
        integrations.set_account("hq", "ca_g2", False, self.root)
        integrations.set_account("acme", "ca_g2", True, self.root)
        note = server._apps_accounts_note("hq", {"gmail": "write"})
        self.assertIn('"ca_g1" (work)', note)
        self.assertNotIn("ca_g2", note)
        self.assertEqual(server._apps_accounts_note("demo", {"gmail": "write"}), "")

    def test_accounts_note_says_how_each_app_runs_and_what_to_connect(self):
        integrations.set_grant("hq", "cos", {"gmail": "act", "instagram": "act"}, self.root)
        note = server._apps_accounts_note("hq", {"gmail": "act"}, bot="cos")
        self.assertIn("- gmail (acts on its own)", note)
        self.assertIn("instagram: no account connected", note)
        self.assertIn("connect", note)
        integrations.set_grant("hq", "cos", {"gmail": "write"}, self.root)
        self.assertIn("- gmail (waits for approval)", server._apps_accounts_note("hq", {"gmail": "write"}, bot="cos"))

    def test_app_turn_flags(self):
        seen, lead, out = self.run_turn("draft a reply to the thread in my inbox")
        self.assertNotIn("tag who should do it", out)
        kw = seen[0]
        self.assertEqual(kw["model"], server.chat_session.ESCALATED_MODEL)
        # Web search beside apps: a query only reaches the search engine, and a fetch only
        # reaches hosts the owner typed, so app data has no road to another host.
        self.assertIs(kw["with_web"], True)
        self.assertEqual(kw["apps_port"], PORT)
        self.assertEqual(len(kw["apps_token"]), 64)
        self.assertTrue(lead.apps_used)

    def test_no_key_no_apps(self):
        integrations.remove_key(self.root)
        seen, lead, _ = self.run_turn("summarize my inbox")
        self.assertNotIn("apps_token", seen[0])
        self.assertFalse(lead.apps_used)

    def test_ordinary_turn_has_apps_on_but_nothing_else_changes(self):
        # A granted bot always has its apps on a private turn: no wording needed. The turn
        # itself stays an ordinary one (fast model, no escalation) and does not taint the thread.
        seen, lead, _ = self.run_turn("what is on the roadmap?")
        self.assertIn("apps_token", seen[0])
        self.assertEqual(seen[0]["model"], "haiku")
        self.assertFalse(lead.apps_used)
        # Apps on and web on: a research job reads the owner's apps and searches the web in one run.
        self.assertIs(seen[0]["with_web"], True)

    def test_asked_web_on_an_apps_turn_is_the_owners_call(self):
        seen, _, _ = self.run_turn("look up competitor pricing /web")
        self.assertIs(seen[0]["with_web"], True)

    def test_first_app_call_marks_the_thread(self):
        calls = []
        ctx = {"on_call": lambda: calls.append(1), "ws": "acme", "bot": "cmo"}
        server.apps_relay.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                  "params": {"name": server.app_rules.MULTI_EXECUTE, "arguments": {}}}, ctx, None, mock.Mock(),
                                 root=self.root, catalog=[])
        self.assertEqual(calls, [1])

    def test_token_revoked_after_request(self):
        seen, _, _ = self.run_turn("summarize my inbox")
        self.assertIsNone(server.apps_relay.TOKENS.lookup(seen[0]["apps_token"]))

    def test_followup_gets_same_token_teammate_none(self):
        server.bots.set_active("cmo", True, root=self.root)
        seen, _, _ = self.run_turn("summarize my inbox", replies=["CONSULT @cmo: tone?", "advice", "final"])
        self.assertEqual([s["who"] for s in seen], ["lead", "mate", "lead"])
        self.assertEqual(seen[0]["apps_token"], seen[2]["apps_token"])
        self.assertNotIn("apps_token", seen[1])
        self.assertIs(seen[1]["with_web"], True)
        # Its prompt quotes the lead's reply (app data): osctl's online research stays off too.
        self.assertIs(seen[1]["apps_used"], True)

    def test_held_actions_streamed_to_thread(self):
        _, _, out = self.run_turn("reply to the thread in my inbox", hold=True)
        events = [json.loads(line[6:]) for line in out.splitlines() if line.startswith("data: {")]
        held = [e["app_actions"] for e in events if "app_actions" in e]
        self.assertEqual(len(held), 1)
        self.assertEqual(held[0][0]["action"]["slug"], "GMAIL_SEND_EMAIL")

    def test_apps_used_keeps_web_on(self):
        seen, _, _ = self.run_turn("find competitor pricing", apps_used=True)
        self.assertIs(seen[0]["with_web"], True)


if __name__ == "__main__":
    unittest.main()
