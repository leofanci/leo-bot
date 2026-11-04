"""Invited people over HTTP: sharing address, invites, join, scoped routes, chat."""
import io
import json
import re
import tempfile
import unittest
from email.message import Message
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import dashboard.server as server
from core import people, rooms

PORT = 8765
LOCAL = f"127.0.0.1:{PORT}"
PUBLIC = "mac.tail1.example"
PROJECTS = [{"slug": "acme", "name": "Acme", "kind": "venture"},
            {"slug": "demo", "name": "Demo", "kind": "venture"}]


def request(method, path, body=None, *, owner=False, member=None, host=LOCAL, origin=None,
            raw=None, ctype="application/json", headers=None):
    h = server.Handler.__new__(server.Handler)
    out = {"headers": {}}
    h.wfile = io.BytesIO()
    h.send_response = lambda code, *a: out.setdefault("code", code)
    h.send_header = lambda k, v: out["headers"].__setitem__(k, v)
    h.end_headers = lambda: None
    h.log_message = lambda *a: None
    data = raw if raw is not None else (b"" if body is None else json.dumps(body).encode())
    msg = Message()
    msg["Host"] = host
    if data:
        msg["Content-Type"] = ctype
    msg["Content-Length"] = str(len(data))
    jar = []
    if owner:
        jar.append(f"{server.AUTH_COOKIE}={server.AUTH_TOKEN}")
    if member:
        jar.append(f"{server.MEMBER_COOKIE}={member}")
    if jar:
        msg["Cookie"] = "; ".join(jar)
    if origin is None:
        origin = f"https://{PUBLIC}" if host == PUBLIC else f"http://{LOCAL}"
    if origin:
        msg["Origin"] = origin
    for k, v in (headers or {}).items():
        msg[k] = v
    h.headers, h.rfile, h.path = msg, io.BytesIO(data), path
    h.client_address = ("127.0.0.1", 50000)
    h.server = SimpleNamespace(server_address=("127.0.0.1", PORT))
    getattr(h, "do_" + method)()
    payload = h.wfile.getvalue()
    try:
        parsed = json.loads(payload) if payload else None
    except ValueError:
        parsed = payload
    return out.get("code"), parsed, out["headers"]


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "chats").mkdir()
        for p in PROJECTS:
            (self.root / "projects" / p["slug"]).mkdir(parents=True)
        for p in (mock.patch.object(server, "ROOT", self.root),
                  mock.patch.object(server.chat_store, "CHATS_DIR", self.root / "chats"),
                  mock.patch.object(server.db, "db_exists", return_value=True),
                  mock.patch.object(server.db, "tree", return_value=PROJECTS)):
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self.tmp.cleanup)
        people.set_public_url(self.root, f"https://{PUBLIC}")

    def join(self, ws="acme", role="member", name="Pat"):
        _, code = people.create_invite(self.root, ws=ws, role=role, by="ceo")
        return people.join(self.root, code, name)


class Sharing(Base):
    def test_owner_sets_sharing_address_from_this_machine(self):
        code, data, _ = request("POST", "/api/sharing", {"url": "https://a.example"}, owner=True)
        self.assertEqual((code, data["public_url"]), (200, "https://a.example"))
        code, data, _ = request("POST", "/api/sharing", {"url": "http://a.example"}, owner=True)
        self.assertEqual(code, 400)

    def test_public_host_is_accepted_and_others_are_not(self):
        self.assertEqual(request("GET", "/api/me", owner=True, host=PUBLIC)[0], 200)
        self.assertEqual(request("GET", "/api/me", owner=True, host="evil.example",
                                 origin="https://evil.example")[0], 403)

    def test_cross_origin_post_refused(self):
        code, _, _ = request("POST", "/api/sharing", {"url": ""}, owner=True, origin="https://evil.example")
        self.assertEqual(code, 403)

    def test_terminal_only_on_this_machine(self):
        code, _, _ = request("GET", "/ws/terminal", owner=True, host=PUBLIC,
                             headers={"Upgrade": "websocket", "Sec-WebSocket-Key": "x"})
        self.assertEqual(code, 403)


    def test_terminal_refused_through_a_tunnel_that_rewrites_host(self):
        ws_h = {"Upgrade": "websocket", "Sec-WebSocket-Key": "x"}
        for extra, origin in (({"X-Forwarded-For": "100.64.0.9"}, None), ({}, f"https://{PUBLIC}")):
            code, _, _ = request("GET", "/ws/terminal", owner=True, origin=origin, headers={**ws_h, **extra})
            self.assertEqual(code, 403, extra or origin)

    def test_relays_answer_this_machine_only(self):
        tok = server.os_relay.TOKENS.mint({"ws": "acme", "readonly": False})
        self.addCleanup(server.os_relay.TOKENS.revoke, tok)
        init = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
        auth = {"Authorization": f"Bearer {tok}"}
        code, data, _ = request("POST", server.OS_RELAY_PATH, init, origin="", headers=auth)
        self.assertEqual((code, data["result"]["serverInfo"]["name"]), (200, "myos"))
        for host, extra in ((PUBLIC, {}), (LOCAL, {"CF-Connecting-IP": "203.0.113.5"})):
            self.assertEqual(request("POST", server.OS_RELAY_PATH, init, host=host, origin="",
                                     headers={**auth, **extra})[0], 403)
        self.assertEqual(request("POST", server.OS_RELAY_PATH, init, origin="",
                                 headers={"Authorization": "Bearer nope"})[0], 403)
        apps_tok = server.apps_relay.TOKENS.mint({"ws": "acme", "bot": "cos", "person": "ceo"})
        self.addCleanup(server.apps_relay.TOKENS.revoke, apps_tok)
        self.assertEqual(request("POST", server.OS_RELAY_PATH, init, origin="",
                                 headers={"Authorization": f"Bearer {apps_tok}"})[0], 403)  # separate stores
        self.assertEqual(request("POST", server.APPS_RELAY_PATH, init, origin="",
                                 headers={"Authorization": f"Bearer {apps_tok}", "X-Forwarded-For": "1.2.3.4"})[0],
                         403)


class InviteAndJoin(Base):
    def test_cookie_is_secure_behind_a_tunnel_that_rewrites_host(self):
        _, code = people.create_invite(self.root, ws="acme", role="member", by="ceo")
        status, _, headers = request("POST", f"/join/{code}", raw=b"name=Pat", ctype="application/x-www-form-urlencoded",
                                     origin=f"https://{PUBLIC}", headers={"X-Forwarded-Proto": "https"})
        self.assertEqual(status, 303)
        self.assertIn("; Secure", headers["Set-Cookie"])

    def test_owner_invites_and_person_joins(self):
        code, data, _ = request("POST", "/api/invites", {"ws": "acme", "role": "admin"}, owner=True)
        self.assertEqual(code, 200)
        link = data["link"]
        self.assertTrue(link.startswith(f"https://{PUBLIC}/join/"))
        self.assertEqual([i["role"] for i in data["invites"]], ["admin"])
        invite_code = link.rsplit("/", 1)[1]

        code, page, _ = request("GET", f"/join/{invite_code}", host=PUBLIC)
        self.assertEqual(code, 200)
        self.assertIn(b"Join Acme", page)

        code, _, hdrs = request("POST", f"/join/{invite_code}", raw=b"name=Pat", host=PUBLIC,
                                ctype="application/x-www-form-urlencoded")
        self.assertEqual(code, 303)
        self.assertEqual(hdrs["Location"], "/?joined=acme")
        cookie = hdrs["Set-Cookie"]
        self.assertIn("HttpOnly", cookie)
        self.assertIn("Secure", cookie)
        token = re.match(rf"{server.MEMBER_COOKIE}=([^;]+)", cookie).group(1)

        code, me, _ = request("GET", "/api/me", member=token, host=PUBLIC)
        self.assertEqual((code, me["name"], me["ws"]), (200, "Pat", {"acme": "admin"}))
        code, page, _ = request("GET", f"/join/{invite_code}", host=PUBLIC)
        self.assertEqual(code, 410)  # used

    def test_join_needs_same_origin(self):
        _, code = people.create_invite(self.root, ws="acme", role="member", by="ceo")
        status, _, _ = request("POST", f"/join/{code}", raw=b"name=Pat", host=PUBLIC, origin="",
                               ctype="application/x-www-form-urlencoded")
        self.assertEqual(status, 403)
        self.assertIsNotNone(people.invite_for(self.root, code))  # still unused

    def test_invite_needs_sharing_address(self):
        people.set_public_url(self.root, "")
        code, data, _ = request("POST", "/api/invites", {"ws": "acme", "role": "member"}, owner=True)
        self.assertEqual(code, 400)
        self.assertIn("sharing address", data["error"])

    def test_no_invites_into_hq(self):
        code, _, _ = request("POST", "/api/invites", {"ws": "hq", "role": "member"}, owner=True)
        self.assertEqual(code, 403)

    def test_admin_invites_members_only(self):
        _, admin = self.join(role="admin")
        self.assertEqual(request("POST", "/api/invites", {"ws": "acme", "role": "member"},
                                 member=admin, host=PUBLIC)[0], 200)
        self.assertEqual(request("POST", "/api/invites", {"ws": "acme", "role": "admin"},
                                 member=admin, host=PUBLIC)[0], 403)
        self.assertEqual(request("POST", "/api/invites", {"ws": "demo", "role": "member"},
                                 member=admin, host=PUBLIC)[0], 403)

    def test_member_cannot_invite_or_see_invites(self):
        _, tok = self.join()
        self.assertEqual(request("POST", "/api/invites", {"ws": "acme", "role": "member"},
                                 member=tok, host=PUBLIC)[0], 403)
        code, data, _ = request("GET", "/api/people?ws=acme", member=tok, host=PUBLIC)
        self.assertEqual(code, 200)
        self.assertNotIn("invites", data)
        self.assertEqual([r["role"] for r in data["people"]], ["owner", "member"])

    def test_removal_rules(self):
        a_id, admin = self.join(role="admin", name="Ada")
        m_id, member = self.join(name="Mo")
        b_id, _ = self.join(role="admin", name="Bo")
        self.assertEqual(request("POST", "/api/people/remove", {"ws": "acme", "id": b_id},
                                 member=admin, host=PUBLIC)[0], 403)
        self.assertEqual(request("POST", "/api/people/remove", {"ws": "acme", "id": a_id},
                                 member=member, host=PUBLIC)[0], 403)
        self.assertEqual(request("POST", "/api/people/remove", {"ws": "acme", "id": m_id},
                                 member=admin, host=PUBLIC)[0], 200)
        self.assertEqual(request("GET", "/api/me", member=member, host=PUBLIC)[0], 403)  # cookie dead
        code, data, _ = request("POST", "/api/people/remove", {"ws": "acme", "id": a_id},
                                member=admin, host=PUBLIC)  # leave
        self.assertEqual((code, data), (200, {"left": "acme"}))
        self.assertEqual(request("POST", "/api/people/remove", {"ws": "acme", "id": b_id}, owner=True)[0], 200)


class Scoped(Base):
    def setUp(self):
        super().setUp()
        self.pid, self.tok = self.join()

    def get(self, path):
        return request("GET", path, member=self.tok, host=PUBLIC)

    def test_owner_only_routes_refused(self):
        for path in ("/api/tree", "/api/posts-index", "/api/integrations", "/api/approvals?ws=acme",
                     "/api/files?ws=acme", "/api/timeline", "/api/id-registry", "/api/chat-session"):
            self.assertEqual(self.get(path)[0], 403, path)
        for path, body in (("/api/integrations/key", {"api_key": "x"}), ("/api/bots/cos/active", {"ws": "acme"}),
                           ("/api/sharing", {"url": ""}), ("/api/project/new", {}), ("/api/memory", {})):
            self.assertEqual(request("POST", path, body, member=self.tok, host=PUBLIC)[0], 403, path)

    def test_no_cookie_no_access(self):
        self.assertEqual(request("GET", "/api/me", host=PUBLIC)[0], 403)
        self.assertEqual(request("GET", "/api/me", member="forged", host=PUBLIC)[0], 403)

    def test_shell_served_without_owner_cookie(self):
        with mock.patch.object(server, "_app_html_bytes", return_value=b"<html>"):
            code, _, hdrs = request("GET", "/", member=self.tok, host=PUBLIC)
        self.assertEqual(code, 200)
        self.assertNotIn("Set-Cookie", hdrs)

    def test_only_their_workspaces(self):
        code, data, _ = self.get("/api/workspaces")
        self.assertEqual([w["id"] for w in data["workspaces"]], ["acme"])
        self.assertEqual(self.get("/api/rooms?ws=demo")[0], 403)
        self.assertEqual(self.get("/api/rooms?ws=hq")[0], 403)
        self.assertEqual(self.get("/api/bots?ws=acme")[0], 200)

    def test_private_channels_and_dm_threads(self):
        rooms.save_topic("acme", "deals", ["cos"], "cos", self.root, private=True, people=["p-other"])
        rooms.save_topic("acme", "launch", ["cos"], "cos", self.root)
        names = [r["name"] for r in self.get("/api/rooms?ws=acme")[1]["rooms"]]
        self.assertIn("launch", names)
        self.assertNotIn("deals", names)
        self.assertEqual(self.get("/api/chats?room=acme/t-deals")[0], 403)

        owner_dm = {"id": "11111111-1111-1111-1111-111111111111", "room": "acme/dm-cos", "messages": []}
        mine = {"id": "22222222-2222-2222-2222-222222222222", "room": "acme/dm-cos", "by": self.pid,
                "shared": True, "messages": []}
        secret = {"id": "33333333-3333-3333-3333-333333333333", "room": "acme/t-deals", "messages": []}
        for rec in (owner_dm, mine, secret):
            server.chat_store.save(rec)
        ids = [c["id"] for c in self.get("/api/chats?room=acme/dm-cos")[1]["chats"]]
        self.assertEqual(ids, [mine["id"]])
        self.assertEqual(self.get(f"/api/chats/{owner_dm['id']}")[0], 404)
        self.assertEqual(self.get(f"/api/chats/{secret['id']}")[0], 404)
        self.assertEqual(self.get(f"/api/chats/{mine['id']}")[0], 200)
        # the owner does not list the person's DM thread either
        owner_ids = [c["id"] for c in request("GET", "/api/chats?room=acme/dm-cos", owner=True)[1]["chats"]]
        self.assertEqual(owner_ids, [owner_dm["id"]])

    def test_members_cannot_manage_channels_admins_can(self):
        body = {"ws": "acme", "name": "deals", "members": ["cos"], "lead": "cos", "private": True}
        self.assertEqual(request("POST", "/api/rooms", body, member=self.tok, host=PUBLIC)[0], 403)
        _, admin = self.join(role="admin", name="Ada")
        code, data, _ = request("POST", "/api/rooms", body, member=admin, host=PUBLIC)
        self.assertEqual(code, 200)
        self.assertIn("deals", [r["name"] for r in data["rooms"]])  # the admin is added to it
        self.assertNotIn("deals", [r["name"] for r in self.get("/api/rooms?ws=acme")[1]["rooms"]])


def fake_session(seen):
    class Sess:
        session_id = "66666666-6666-6666-6666-666666666666"
        _turn_count, _last_skill, _skill_explicit, apps_used = 0, None, False, False
        workspace = deny_dirs = None

        def is_fresh(self):
            return True

        def note_skill(self, *a, **k):
            pass

        def ask(self, prompt, **kw):
            seen["prompt"], seen["kw"], seen["shared"] = prompt, kw, getattr(self, "shared", None)
            seen["live"] = server.os_relay.TOKENS.lookup(kw.get("os_token")) is not None
            yield ("delta", "hi")
            yield ("done", {})
    return Sess()


class FakeApiEngine:
    id, label, kind = "api:demo", "Demo API", "api"

    def resolve_model(self, model):
        return model


class Ask(Base):
    def setUp(self):
        super().setUp()
        self.pid, self.tok = self.join()
        # Invited people run on the workspace's API key engine.
        picked = mock.patch.object(server.engine_registry, "pick", return_value=FakeApiEngine())
        picked.start()
        self.addCleanup(picked.stop)

    def test_member_turn_without_an_api_engine_is_told_and_spends_nothing(self):
        seen = {}
        with mock.patch.object(server.engine_registry, "pick",
                               side_effect=server.EngineRefused(server.engine_registry.NO_PEOPLE_ENGINE)), \
                mock.patch.object(server, "person_chat_session", return_value=fake_session(seen)):
            code, data, _ = self.ask()
        self.assertEqual(code, 200)
        self.assertNotIn("prompt", seen)  # no model turn
        self.assertIn(b"no API key", data)

    def owner_ask(self, room, seen):
        body = {"messages": [{"role": "user", "content": "hello team"}], "room_id": room}
        with mock.patch.object(server, "get_chat_session", return_value=fake_session(seen)):
            return request("POST", "/api/ask", body, owner=True)

    def test_member_turn_gets_only_the_osctl_relay(self):
        seen = {}
        with mock.patch.object(server, "person_chat_session", return_value=fake_session(seen)):
            self.assertEqual(self.ask()[0], 200)
        self.assertTrue(seen["shared"])
        self.assertTrue(seen["live"])  # a live token during the run...
        self.assertIsNone(server.os_relay.TOKENS.lookup(seen["kw"]["os_token"]))  # ...revoked after
        self.assertEqual(seen["kw"]["os_port"], PORT)
        self.assertIn("## Shared thread", seen["prompt"])

    def test_owner_turn_is_shared_where_a_person_can_read_it(self):
        rooms.save_topic("acme", "deals", ["cos"], "cos", self.root, private=True, people=[])
        for room, shared in (("acme/general", True), ("acme/dm-cos", False), ("acme/t-deals", False)):
            seen = {}
            with mock.patch.object(server.integrations, "api_key", return_value="ak_x"), \
                    mock.patch.object(server.integrations, "effective_grant", return_value={"gmail": "write"}), \
                    mock.patch.object(server, "_needs_apps", side_effect=lambda m, g, e: bool(g)):
                self.assertEqual(self.owner_ask(room, seen)[0], 200)
            self.assertEqual(bool(seen["shared"]), shared, room)
            self.assertEqual("apps_token" in seen["kw"], not shared, room)
            self.assertEqual("os_token" in seen["kw"], shared, room)

    def test_no_people_no_shared_threads(self):
        people.remove(self.root, "acme", self.pid)
        seen = {}
        self.assertEqual(self.owner_ask("acme/general", seen)[0], 200)
        self.assertFalse(seen["shared"])

    def test_threads_remember_whether_every_turn_was_shared(self):
        seen = {}
        with mock.patch.object(server, "person_chat_session", return_value=fake_session(seen)):
            self.ask()
        cid = fake_session({}).session_id
        self.assertTrue(server.chat_store.load(cid)["shared"])
        rec = server.chat_store.load(cid)
        rec["room"] = "acme/dm-cos"  # the owner continues it with full tools (a DM)
        rec["by"] = "ceo"
        server.chat_store.save(rec)
        body = {"messages": [{"role": "user", "content": "hi"}], "room_id": "acme/dm-cos", "chat_id": cid}
        with mock.patch.object(server, "get_chat_session", return_value=fake_session(seen)):
            self.assertEqual(request("POST", "/api/ask", body, owner=True)[0], 200)
        self.assertFalse(server.chat_store.load(cid)["shared"])  # never shared again

    def test_listing_hides_threads_that_read_an_app(self):
        rec = {"id": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb", "room": "acme/general", "shared": True,
               "apps_used": True, "messages": []}
        server.chat_store.save(rec)
        listed = request("GET", "/api/chats?room=acme/general", member=self.tok, host=PUBLIC)[1]["chats"]
        self.assertEqual(listed, [])

    def test_old_owner_thread_in_general_is_not_reachable(self):
        rec = {"id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", "room": "acme/general", "messages": []}
        server.chat_store.save(rec)
        self.assertEqual(self.ask(chat_id=rec["id"])[0], 403)
        self.assertEqual(request("GET", f"/api/chats/{rec['id']}", member=self.tok, host=PUBLIC)[0], 404)

    def test_thread_must_be_asked_in_its_own_room(self):
        rooms.save_topic("acme", "launch", ["cos"], "cos", self.root)
        rec = {"id": "88888888-8888-8888-8888-888888888888", "room": "acme/t-launch", "messages": []}
        server.chat_store.save(rec)
        self.assertEqual(self.ask("acme/general", chat_id=rec["id"])[0], 403)

    def test_member_never_gets_the_owner_folder_path(self):
        rec = {"id": "99999999-9999-9999-9999-999999999999", "room": "acme/general",
               "scope": ["acme", "/somewhere/app"], "shared": True, "messages": []}
        server.chat_store.save(rec)
        listed = request("GET", "/api/chats?room=acme/general", member=self.tok, host=PUBLIC)[1]["chats"]
        self.assertNotIn("scope", listed[0])
        self.assertNotIn("scope", request("GET", f"/api/chats/{rec['id']}", member=self.tok, host=PUBLIC)[1])

    def ask(self, room="acme/general", chat_id=None, tok=None):
        body = {"messages": [{"role": "user", "content": "hello team"}], "room_id": room}
        if chat_id:
            body["chat_id"] = chat_id
        return request("POST", "/api/ask", body, member=tok or self.tok, host=PUBLIC)

    def test_other_workspace_refused(self):
        self.assertEqual(self.ask("demo/general")[0], 403)
        self.assertEqual(self.ask("hq/general")[0], 403)

    def test_private_room_refused(self):
        rooms.save_topic("acme", "deals", ["cos"], "cos", self.root, private=True, people=[])
        self.assertEqual(self.ask("acme/t-deals")[0], 403)

    def test_someone_elses_dm_thread_refused(self):
        rec = {"id": "44444444-4444-4444-4444-444444444444", "room": "acme/dm-cos", "messages": []}
        server.chat_store.save(rec)
        self.assertEqual(self.ask("acme/dm-cos", chat_id=rec["id"])[0], 403)

    def test_busy_thread_refused(self):
        rec = {"id": "55555555-5555-5555-5555-555555555555", "room": "acme/general", "shared": True, "messages": []}
        server.chat_store.save(rec)
        server._BUSY.add(rec["id"])
        self.addCleanup(server._BUSY.discard, rec["id"])
        code, data, _ = self.ask(chat_id=rec["id"])
        self.assertEqual(code, 409)
        self.assertIn("replying", data["error"])

    def test_turn_runs_in_their_slot_without_apps_and_is_signed(self):
        seen = {}

        class Sess:
            session_id = "66666666-6666-6666-6666-666666666666"
            _turn_count, _last_skill, _skill_explicit, apps_used = 0, None, False, False
            workspace = deny_dirs = None

            def is_fresh(self):
                return True

            def note_skill(self, *a, **k):
                pass

            def ask(self, prompt, **kw):
                seen["prompt"], seen["kw"] = prompt, kw
                yield ("delta", "hi Pat")
                yield ("done", {})

        with mock.patch.object(server, "person_chat_session", return_value=Sess()) as slot, \
                mock.patch.object(server, "get_chat_session") as owner_slot, \
                mock.patch.object(server.integrations, "api_key", return_value="ak_x"), \
                mock.patch.object(server.integrations, "effective_grant", return_value={"gmail": "write"}):
            code, _, _ = self.ask()
        self.assertEqual(code, 200)
        self.assertEqual(slot.call_args.args[0], self.pid)
        owner_slot.assert_not_called()
        self.assertNotIn("apps_token", seen["kw"])
        self.assertIn("[From Pat, invited member of this workspace", seen["prompt"])
        rec = server.chat_store.load(Sess.session_id)
        self.assertEqual(rec["by"], self.pid)
        self.assertEqual(rec["messages"][0], {"role": "user", "content": "hello team",
                                              "by": self.pid, "by_name": "Pat"})
        self.assertFalse(server._BUSY)


class Reset(Base):
    def test_reset_drops_only_their_slot(self):
        pid, tok = self.join()
        sess = mock.Mock()
        server._PERSON_CHATS[pid] = [sess, None]
        server._PERSON_CHATS["p-other"] = [mock.Mock(), None]
        self.addCleanup(server._PERSON_CHATS.clear)
        self.assertEqual(request("POST", "/api/chat-reset", {}, member=tok, host=PUBLIC)[0], 200)
        sess.close.assert_called_once_with()
        self.assertEqual(list(server._PERSON_CHATS), ["p-other"])


class Slots(Base):
    def test_a_thread_moves_between_slots(self):
        server.chat_store.save({"id": "77777777-7777-7777-7777-777777777777", "room": "acme/general",
                                "scope": ["acme", ""], "messages": []})
        cid = "77777777-7777-7777-7777-777777777777"
        old_chat, old_scope = server._CHAT, server._CHAT_SCOPE
        self.addCleanup(setattr, server, "_CHAT", old_chat)
        self.addCleanup(setattr, server, "_CHAT_SCOPE", old_scope)
        self.addCleanup(server._PERSON_CHATS.clear)
        a = server.person_chat_session("p-a", ("acme", ""), cid)
        self.assertEqual(a.session_id, cid)
        self.assertIs(server.person_chat_session("p-a", ("acme", ""), cid), a)
        b = server.person_chat_session("p-b", ("acme", ""), cid)
        self.assertEqual(b.session_id, cid)
        self.assertNotIn("p-a", server._PERSON_CHATS)  # handed over
        o = server.get_chat_session(("acme", ""), cid)
        self.assertEqual(o.session_id, cid)
        self.assertNotIn("p-b", server._PERSON_CHATS)


if __name__ == "__main__":
    unittest.main()
