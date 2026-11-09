"""Deleting a workspace or channel, or removing a person, ends what they granted: nothing
comes back when a name is reused, and a removed teammate's runner stops at once."""
import json
from unittest import mock

from core import people, runners
from tests import test_server_people as sp

server = sp.server
request = sp.request


class Lifecycle(sp.Base):
    def setUp(self):
        super().setUp()
        for p in sp.PROJECTS:
            (self.root / "projects" / p["slug"] / "project.md").write_text(f"---\nname: {p['name']}\n---\n")
        for p in (mock.patch.object(server.fileops, "ROOT", self.root),
                  mock.patch.object(server.fileops, "reindex", lambda: None)):
            p.start()
            self.addCleanup(p.stop)

    def thread(self, cid, room):
        server.chat_store.save({"id": cid, "title": "t", "created": 1, "updated": 1, "room": room,
                                "scope": [room.split("/")[0]], "shared": True, "engines": {},
                                "messages": [{"role": "user", "content": "PRIVATE-DUMMY", "ts": 1}]})

    def test_a_reused_workspace_slug_starts_with_nobody(self):
        _, token = self.join("acme")
        _, pending = people.create_invite(self.root, ws="acme", role="admin", by="ceo")
        cid = "11111111-1111-4111-8111-111111111111"
        self.thread(cid, "acme/general")
        code, out, _ = request("POST", "/api/project/acme/delete", {}, owner=True)
        self.assertEqual(code, 200, out)
        (self.root / "projects" / "acme").mkdir()
        (self.root / "projects" / "acme" / "project.md").write_text("---\nname: New Acme\n---\n")
        self.assertIsNone(people.person_by_token(self.root, token))
        self.assertIsNone(people.invite_for(self.root, pending))
        self.assertEqual(people.people_in(self.root, "acme"), [])
        self.assertFalse(server.chat_store.load(cid).get("shared"))

    def test_a_person_in_two_workspaces_keeps_the_other(self):
        _, token = self.join("acme")
        _, token = people.join(self.root, people.create_invite(self.root, ws="demo", role="member",
                                                               by="ceo")[1], "Pat", token=token)
        self.assertEqual(request("POST", "/api/project/acme/delete", {}, owner=True)[0], 200)
        self.assertEqual(people.person_by_token(self.root, token)["ws"], {"demo": "member"})

    def test_a_deleted_channels_threads_never_follow_its_name(self):
        _, admin = self.join("acme", role="admin", name="Ada")
        cid = "22222222-2222-4222-8222-222222222222"
        self.assertEqual(request("POST", "/api/rooms", {"ws": "acme", "name": "deals", "members": ["cos"],
                                                       "lead": "cos", "private": True}, owner=True)[0], 200)
        self.thread(cid, "acme/t-deals")
        self.assertEqual(request("POST", "/api/rooms/delete", {"ws": "acme", "name": "deals"}, owner=True)[0], 200)
        code, _, _ = request("POST", "/api/rooms", {"ws": "acme", "name": "deals", "members": ["cos"],
                                                   "lead": "cos"}, member=admin)
        self.assertEqual(code, 200)
        code, out, _ = request("GET", f"/api/chats/{cid}", member=admin)
        self.assertNotEqual(code, 200)
        self.assertNotIn("PRIVATE-DUMMY", json.dumps(out))

    def test_a_removed_teammates_runner_loses_that_workspace_at_once(self):
        _, token = self.join("acme")
        pid = people.person_by_token(self.root, token)["id"]
        people.join(self.root, people.create_invite(self.root, ws="demo", role="member", by="ceo")[1], "Pat",
                    token=token)
        rid, rtoken = runners.create(self.root, pid, "Pat's Mac")
        hub = runners.Hub()
        os_token = server.os_relay.TOKENS.mint({"ws": "acme", "readonly": False, "runner_owner": pid, "shared": True})
        self.addCleanup(server.os_relay.TOKENS.revoke, os_token)
        turn = hub.submit(pid, {"chat": "c", "os_token": os_token, "policy": {"workspace": "acme"}})
        self.assertIsNotNone(hub.next(rid, pid, wait=0))
        with mock.patch.object(runners, "HUB", hub):
            self.assertEqual(request("POST", "/api/people/remove", {"ws": "acme", "id": pid}, owner=True)[0], 200)
        self.assertTrue(turn.cancelled)
        frame = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        code, _, _ = request("POST", server.RUNNER_OS_PATH, frame, origin="",
                             headers={"Authorization": f"Bearer {os_token}", "X-Runner-Token": rtoken})
        self.assertEqual(code, 403)


if __name__ == "__main__":
    import unittest
    unittest.main()
