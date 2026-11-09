"""Folders screen routes: owner only, same-origin, and no folder path ever reaches a person."""
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from core import folders, people
from tests import test_server_people as sp

server = sp.server
request = sp.request


class FolderRoutes(sp.Base):
    def setUp(self):
        super().setUp()
        ext = tempfile.TemporaryDirectory()
        self.addCleanup(ext.cleanup)
        self.ext = Path(ext.name).resolve()
        self.app = self.ext / "acme-app"
        self.docs = self.ext / "acme-docs"
        for d in (self.app, self.docs):
            d.mkdir()
        (self.root / "team").mkdir(exist_ok=True)
        for p in sp.PROJECTS:
            (self.root / "projects" / p["slug"] / "project.md").write_text(f"---\nname: {p['name']}\n---\n")
        for p in (mock.patch.object(server.fileops, "ROOT", self.root),
                  mock.patch.object(server.fileops, "reindex", lambda: None)):
            p.start()
            self.addCleanup(p.stop)

    def get(self, ws="acme", **kw):
        return request("GET", f"/api/folders?ws={ws}", **kw)

    def post(self, route, body, **kw):
        kw.setdefault("owner", True)
        return request("POST", f"/api/folders/{route}", body, **kw)

    def test_owner_adds_limits_and_removes(self):
        code, out, _ = self.post("add", {"ws": "acme", "path": str(self.app)})
        self.assertEqual(code, 200, out)
        self.assertEqual([(r["path"], r["access"]) for r in out["folders"]], [(str(self.app), "edit")])
        code, out, _ = self.post("add", {"ws": "acme", "path": str(self.docs), "access": "read"})
        self.assertEqual([r["access"] for r in out["folders"]], ["edit", "read"])
        code, out, _ = self.post("access", {"ws": "acme", "id": "f2", "access": "edit"})
        self.assertEqual(out["folders"][1]["access"], "edit")
        code, out, _ = self.post("limit", {"ws": "acme", "kind": "bots", "key": "cto", "limit": {"f1": "read"}})
        self.assertEqual((code, out["bots"]), (200, {"cto": {"f1": "read"}}))
        code, out, _ = self.post("limit", {"ws": "acme", "kind": "rooms", "key": "general", "limit": {}})
        self.assertEqual(out["rooms"], {"general": {}})
        code, out, _ = self.post("limit", {"ws": "acme", "kind": "rooms", "key": "general", "limit": None})
        self.assertEqual(out["rooms"], {})
        code, out, _ = self.post("remove", {"ws": "acme", "id": "f1"})
        self.assertEqual([r["id"] for r in out["folders"]], ["f2"])
        self.assertEqual(out["bots"], {"cto": {}})
        code, out, _ = request("GET", "/api/folders?ws=acme", owner=True)
        self.assertEqual(code, 200)
        self.assertEqual([r["id"] for r in out["folders"]], ["f2"])
        self.assertIn("general", [c["id"] for c in out["channels"]])
        self.assertFalse([c for c in out["channels"] if c["id"].startswith("dm-")])
        self.assertTrue(any(b["id"] == "cto" for b in out["team"]))

    def test_hq_has_folders_too(self):
        code, out, _ = self.post("add", {"ws": "hq", "path": str(self.docs)})
        self.assertEqual((code, [r["path"] for r in out["folders"]]), (200, [str(self.docs)]))
        self.assertTrue((self.root / "team" / "folders.json").exists())

    def test_open_uses_the_saved_path_only(self):
        self.post("add", {"ws": "acme", "path": str(self.app)})
        with mock.patch.object(server, "open_local_folder") as opener:
            code, out, _ = self.post("open", {"ws": "acme", "id": "f1", "path": "/etc"})
        self.assertEqual(code, 200, out)
        opener.assert_called_once_with(str(self.app))

    def test_old_project_open_route_is_gone(self):
        code, _, _ = request("POST", "/api/project/acme/folder/open", {}, owner=True)
        self.assertEqual(code, 404)

    def test_invited_person_gets_403_on_every_folder_route(self):
        folders.add("acme", str(self.app), root=self.root)
        _, member = self.join("acme")
        for method, path, body in (("GET", "/api/folders?ws=acme", None),
                                   ("POST", "/api/folders/add", {"ws": "acme", "path": str(self.docs)}),
                                   ("POST", "/api/folders/remove", {"ws": "acme", "id": "f1"}),
                                   ("POST", "/api/folders/access", {"ws": "acme", "id": "f1", "access": "read"}),
                                   ("POST", "/api/folders/limit", {"ws": "acme", "kind": "bots", "key": "cto", "limit": {}}),
                                   ("POST", "/api/folders/open", {"ws": "acme", "id": "f1"}),
                                   ("POST", "/api/local-folder/pick", {})):
            with self.subTest(path):
                code, out, _ = request(method, path, body, member=member)
                self.assertEqual(code, 403, out)
                self.assertNotIn(str(self.app), json.dumps(out))
        self.assertEqual([(r["id"], r["access"]) for r in folders.load("acme", self.root)["folders"]], [("f1", "edit")])

    def test_person_payloads_hold_no_paths(self):
        folders.add("acme", str(self.app), root=self.root)
        tree = [{**p, "folders": folders.load(p["slug"], self.root)["folders"], "profiles": [], "products": []}
                for p in sp.PROJECTS]
        cid = "0f8e2c1a-1111-4222-8333-444455556666"
        server.chat_store.save({"id": cid, "title": "t", "created": 1, "updated": 1, "room": "acme/general",
                                "scope": ["acme", str(self.app)], "folder_paths": [str(self.app)], "shared": True,
                                "messages": [{"role": "user", "content": "hi", "ts": 1}], "engines": {}})
        _, member = self.join("acme")
        with mock.patch.object(server.db, "tree", return_value=tree):
            for path in ("/api/me", "/api/runners", "/api/schemas", "/api/skills-index", "/api/workspaces",
                         "/api/bots?ws=acme", "/api/rooms?ws=acme", "/api/people?ws=acme",
                         "/api/chats?room=acme/general", f"/api/chats/{cid}", "/api/tree",
                         "/api/project/acme", "/api/folders?ws=acme"):
                with self.subTest(path):
                    code, out, _ = request("GET", path, member=member)
                    self.assertNotIn(str(self.app), json.dumps(out) if not isinstance(out, bytes) else out.decode())

    def test_row_flags(self):
        folders.add("acme", str(self.app), root=self.root)
        folders.add("acme", str(self.docs), root=self.root)
        (self.app / ".git").mkdir()
        self.docs.chmod(0o500)
        self.addCleanup(self.docs.chmod, 0o700)
        gone = self.ext / "gone"
        gone.mkdir()
        folders.add("acme", str(gone), root=self.root)
        gone.rmdir()
        code, out, _ = self.get(owner=True)
        rows = {r["name"]: r for r in out["folders"]}
        self.assertEqual((rows["acme-app"]["missing"], rows["acme-app"]["not_git"], rows["acme-app"]["cant_write"]),
                         (False, False, False))
        self.assertTrue(rows["acme-docs"]["not_git"])
        if os.geteuid() != 0:
            self.assertTrue(rows["acme-docs"]["cant_write"])
        self.assertTrue(rows["gone"]["missing"])

    def test_a_damaged_file_is_flagged_and_kept(self):
        f = self.root / "projects/acme/team/folders.json"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("{broken")
        code, out, _ = self.get(owner=True)
        self.assertEqual(code, 200)
        self.assertTrue(out["damaged"])
        code, out, _ = self.post("add", {"ws": "acme", "path": str(self.app)})
        self.assertEqual(code, 400)
        self.assertEqual(f.read_text(), "{broken")

    def test_layer_choices_never_offer_more_than_the_folder(self):
        js = (Path(server.HERE) / "app.js").read_text()
        self.assertIn('r.access === "edit" ? [["", "None"], ["read", "Read"], ["edit", "Edit"]] : [["", "None"], ["read", "Read"]]', js)

    def test_bad_access_or_unknown_id_is_400(self):
        folders.add("acme", str(self.app), root=self.root)
        for route, body in (("access", {"ws": "acme", "id": "f1", "access": "admin"}),
                            ("access", {"ws": "acme", "id": "f9", "access": "read"}),
                            ("remove", {"ws": "acme", "id": "f9"}),
                            ("add", {"ws": "acme", "path": str(self.docs), "access": "write"}),
                            ("limit", {"ws": "acme", "kind": "bots", "key": "nobody", "limit": {}}),
                            ("limit", {"ws": "acme", "kind": "rooms", "key": "dm-cto", "limit": {}}),
                            ("limit", {"ws": "acme", "kind": "bots", "key": "cto", "limit": {"f9": "read"}}),
                            ("add", {"ws": "nope", "path": str(self.docs)}),
                            ("add", {"ws": "acme", "path": "relative/path"})):
            with self.subTest((route, body)):
                code, out, _ = self.post(route, body)
                self.assertEqual(code, 400, out)

    def test_add_refuses_a_folder_inside_leobot(self):
        inside = self.root / "projects" / "acme" / "code"
        inside.mkdir()
        code, out, _ = self.post("add", {"ws": "acme", "path": str(inside)})
        self.assertEqual(code, 400)
        self.assertIn("Leo Bot", out["error"])
        self.assertEqual(folders.load("acme", self.root)["folders"], [])

    def test_server_mode_has_no_chooser(self):
        with mock.patch.object(server, "DEPLOYMENT", "server"):
            code, out, _ = self.get(owner=True)
        self.assertFalse(out["chooser"])
        with mock.patch.object(server, "DEPLOYMENT", "mac"), mock.patch.object(server.sys, "platform", "darwin"):
            code, out, _ = self.get(owner=True)
        self.assertTrue(out["chooser"])

    def test_cross_site_post_is_refused(self):
        code, _, _ = self.post("add", {"ws": "acme", "path": str(self.app)}, origin="https://evil.example")
        self.assertEqual(code, 403)
        self.assertEqual(folders.load("acme", self.root)["folders"], [])

    def test_bot_turn_token_cannot_reach_folder_routes(self):
        token = server.os_relay.TOKENS.mint({"ws": "acme", "readonly": False, "apps_used": False,
                                              "engine": "claude-code", "runner_owner": "owner", "shared": False})
        self.addCleanup(server.os_relay.TOKENS.revoke, token)
        for method, path, body in (("GET", "/api/folders?ws=acme", None),
                                   ("POST", "/api/folders/add", {"ws": "acme", "path": str(self.app)})):
            code, _, _ = request(method, path, body, headers={"Authorization": f"Bearer {token}"})
            self.assertEqual(code, 403)
        self.assertEqual(folders.load("acme", self.root)["folders"], [])


class EnginesScreen(unittest.TestCase):
    def test_codex_and_acp_rows_say_read_only(self):
        from dashboard.engines import registry
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(registry, "on_server", return_value=False), \
                mock.patch.object(registry.config, "load", return_value={"acp_agents": [{"id": "demo", "name": "Demo", "command": "x"}],
                                                                       "providers": []}):
            rows = {r["id"]: r for r in registry.statuses(Path(tmp))}
        self.assertTrue(rows["claude-code"]["folder_edit"])
        self.assertFalse(rows["codex"]["folder_edit"])
        self.assertFalse(rows["acp:demo"]["folder_edit"])
        js = (Path(server.HERE) / "app.js").read_text()
        self.assertIn("Read-only in linked folders", js)


if __name__ == "__main__":
    unittest.main()
