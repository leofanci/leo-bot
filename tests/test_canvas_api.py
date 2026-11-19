"""Dashboard canvas API: owner sees all, a coworker only canvases pinned in rooms they see."""
import unittest
from unittest import mock

import dashboard.server as server
from core import canvas_store as cs, rooms
from tests.test_server_people import PUBLIC, Base, request


class CanvasApi(Base):
    def setUp(self):
        super().setUp()
        self.pid, self.tok = self.join()

    # -- helpers ---------------------------------------------------------- #
    def owner(self, method, path, body=None):
        return request(method, path, body, owner=True)[:2]

    def person(self, method, path, body=None, tok=None):
        return request(method, path, body, member=tok or self.tok, host=PUBLIC)[:2]

    def run_cmd(self, who, cmd, params, ws="acme"):
        call = self.owner if who == "owner" else self.person
        return call("POST", "/api/canvases", {"ws": ws, "cmd": cmd, "params": params})

    def make(self, title, pin=None, blocks=None):
        params = {"title": title, "blocks": blocks or [{"type": "text", "md": "hello"}]}
        if pin:
            params["pin"] = pin
        code, data = self.run_cmd("owner", "create-canvas", params)
        self.assertEqual(code, 200, data)
        return data["id"]

    def version(self, cid, who="owner"):
        call = self.owner if who == "owner" else self.person
        return call("GET", f"/api/canvases/{cid}?ws=acme")[1]["version"]

    # -- listing and visibility ------------------------------------------- #
    def test_owner_lists_every_canvas(self):
        a = self.make("Alpha", pin="acme/general")
        b = self.make("Beta")
        code, data = self.owner("GET", "/api/canvases?ws=acme")
        self.assertEqual(code, 200)
        rows = {c["id"]: c for c in data["canvases"]}
        self.assertEqual(set(rows), {a, b})
        self.assertEqual(rows[a]["pinned_in"], ["acme/general"])
        self.assertEqual(rows[b]["pinned_in"], [])
        self.assertTrue(rows[a]["updated"])
        self.assertEqual(rows[a]["title"], "Alpha")
        code, data = self.owner("GET", "/api/canvases?ws=acme&room=acme/general")
        self.assertEqual(data["pins"], [a])
        self.assertEqual([c["id"] for c in data["canvases"]], [a])

    def test_owner_list_shows_deleted_canvases_and_restores_them(self):
        a = self.make("Alpha", pin="acme/general")
        self.make("Beta")
        code, data = self.run_cmd("owner", "delete-canvas", {"id": a, "version": self.version(a)})
        self.assertEqual(code, 200, data)
        code, data = self.owner("GET", "/api/canvases?ws=acme")
        self.assertEqual([c["id"] for c in data["canvases"]], ["cv2"])
        [gone] = data["trash"]
        self.assertEqual((gone["id"], gone["title"]), (a, "Alpha"))
        self.assertTrue(gone["deleted"])
        code, out = self.run_cmd("owner", "undo", {"id": a, "change": gone["change"]})
        self.assertEqual(code, 200, out)
        code, data = self.owner("GET", "/api/canvases?ws=acme")
        self.assertEqual(data["trash"], [])
        self.assertEqual({c["id"]: c["pinned_in"] for c in data["canvases"]}[a], ["acme/general"])

    def test_deleted_canvases_are_listed_for_the_owner_only(self):
        a = self.make("Alpha", pin="acme/general")
        self.run_cmd("owner", "delete-canvas", {"id": a, "version": self.version(a)})
        code, data = self.person("GET", "/api/canvases?ws=acme")
        self.assertEqual(code, 200)
        self.assertNotIn("trash", data)
        code, data = self.owner("GET", "/api/canvases?ws=acme&room=acme/general")
        self.assertNotIn("trash", data)  # a room's tabs never carry the trash

    def test_person_sees_only_canvases_pinned_in_rooms_they_see(self):
        rooms.save_topic("acme", "deals", ["cos"], "cos", self.root, private=True, people=[self.pid])
        rooms.save_topic("acme", "secret", ["cos"], "cos", self.root, private=True, people=["p-other"])
        open_cv = self.make("Open", pin="acme/general")
        deals = self.make("Deals", pin="acme/t-deals")
        hidden = self.make("Hidden", pin="acme/t-secret")
        loose = self.make("Loose")
        code, data = self.person("GET", "/api/canvases?ws=acme")
        self.assertEqual(code, 200)
        self.assertEqual({c["id"] for c in data["canvases"]}, {open_cv, deals})
        self.assertEqual(self.person("GET", f"/api/canvases/{deals}?ws=acme")[0], 200)
        # invisible, missing and malformed all answer the same 404
        bodies = []
        for cid in (hidden, loose, "cv999", "cv1x", "..%2Fcv1"):
            code, body = self.person("GET", f"/api/canvases/{cid}?ws=acme")
            self.assertEqual(code, 404, cid)
            bodies.append(body)
        self.assertEqual(len({str(b) for b in bodies}), 1, bodies)
        # a room they cannot see: same 404
        self.assertEqual(self.person("GET", "/api/canvases?ws=acme&room=acme/t-secret")[0], 404)
        # taken off the private room: the next read is 404, not stale content
        rooms.forget_person("acme", self.pid, self.root)
        self.assertEqual(self.person("GET", f"/api/canvases/{deals}?ws=acme")[0], 404)
        ids = {c["id"] for c in self.person("GET", "/api/canvases?ws=acme")[1]["canvases"]}
        self.assertEqual(ids, {open_cv})

    def test_unpinned_from_their_only_visible_room_is_404(self):
        cid = self.make("Open", pin="acme/general")
        self.assertEqual(self.person("GET", f"/api/canvases/{cid}?ws=acme")[0], 200)
        code, _ = self.run_cmd("owner", "unpin-canvas", {"id": cid, "room": "acme/general"})
        self.assertEqual(code, 200)
        self.assertEqual(self.person("GET", f"/api/canvases/{cid}?ws=acme")[0], 404)
        code, _ = self.run_cmd("person", "add-block", {"id": cid, "version": 99,
                                                       "blocks": {"type": "text", "md": "x"}})
        self.assertEqual(code, 404)

    def test_hq_and_other_workspaces_are_404_for_a_person(self):
        self.owner("POST", "/api/canvases", {"ws": "hq", "cmd": "create-canvas",
                                             "params": {"title": "Board"}})
        demo = self.owner("POST", "/api/canvases", {"ws": "demo", "cmd": "create-canvas",
                                                    "params": {"title": "D", "pin": "demo/general"}})[1]
        for ws in ("hq", "demo", "nope"):
            self.assertEqual(self.person("GET", f"/api/canvases?ws={ws}")[0], 404, ws)
            self.assertEqual(self.person("GET", f"/api/canvases/cv1?ws={ws}")[0], 404, ws)
            code, _ = self.run_cmd("person", "create-canvas",
                                   {"title": "x", "pin": f"{ws}/general"}, ws=ws)
            self.assertEqual(code, 404, ws)
        self.assertEqual(self.person("GET", f"/api/canvases/{demo['id']}?ws=demo")[0], 404)
        # the owner does reach HQ
        code, data = self.owner("GET", "/api/canvases?ws=hq")
        self.assertEqual((code, [c["title"] for c in data["canvases"]]), (200, ["Board"]))

    def test_no_cookie_no_canvases(self):
        self.make("Open", pin="acme/general")
        self.assertEqual(request("GET", "/api/canvases?ws=acme", host=PUBLIC)[0], 403)
        self.assertEqual(request("POST", "/api/canvases", {"ws": "acme", "cmd": "list-canvases"},
                                 host=PUBLIC)[0], 403)

    # -- writes ------------------------------------------------------------- #
    def test_person_edit_refused_on_view_canvas(self):
        cid = self.make("Open", pin="acme/general")
        code, _ = self.run_cmd("owner", "update-canvas",
                               {"id": cid, "coworkers": "view", "version": self.version(cid)})
        self.assertEqual(code, 200)
        self.assertEqual(self.person("GET", f"/api/canvases/{cid}?ws=acme")[0], 200)
        code, data = self.run_cmd("person", "add-block", {"id": cid, "version": self.version(cid),
                                                          "blocks": {"type": "text", "md": "x"}})
        self.assertEqual(code, 403, data)
        self.assertIn("error", data)
        code, _ = self.run_cmd("person", "update-canvas", {"id": cid, "coworkers": "edit",
                                                           "version": self.version(cid)})
        self.assertEqual(code, 403)

    def test_stale_write_is_409(self):
        cid = self.make("Open")
        v = self.version(cid)
        code, _ = self.run_cmd("owner", "update-block", {"id": cid, "block": "bk1", "version": v,
                                                         "blocks": {"md": "first"}})
        self.assertEqual(code, 200)
        code, data = self.run_cmd("owner", "update-block", {"id": cid, "block": "bk1", "version": v,
                                                            "blocks": {"md": "second"}})
        self.assertEqual(code, 409, data)
        self.assertIn("error", data)

    def test_person_write_is_labeled_and_recorded_as_them(self):
        cid = self.make("Open", pin="acme/general")
        code, data = self.run_cmd("person", "add-block", {"id": cid, "version": self.version(cid, "person"),
                                                          "blocks": {"type": "text", "md": "from pat"}})
        self.assertEqual(code, 200, data)
        bk = data["block"]
        doc = self.owner("GET", f"/api/canvases/{cid}?ws=acme")[1]
        block = next(b for b in doc["blocks"] if b["id"] == bk)
        self.assertEqual(block["created_by"], f"person:{self.pid}")
        self.assertTrue(block["outside"])
        code, hist = self.owner("GET", f"/api/canvases/{cid}/history?ws=acme")
        self.assertEqual(code, 200)
        self.assertEqual(hist["entries"][-1]["by"], f"person:{self.pid}")
        # a person's history read carries no before/after
        code, hist = self.person("GET", f"/api/canvases/{cid}/history?ws=acme")
        self.assertEqual(code, 200)
        self.assertNotIn("after", hist["entries"][-1])

    def test_person_undo_on_a_canvas_they_cannot_see_is_404(self):
        cid = self.make("Open", pin="acme/general")
        code, data = self.run_cmd("person", "add-block", {"id": cid, "version": self.version(cid),
                                                          "blocks": {"type": "text", "md": "mine"}})
        self.assertEqual(code, 200)
        self.run_cmd("owner", "unpin-canvas", {"id": cid, "room": "acme/general"})
        code, body = self.run_cmd("person", "undo", {"id": cid, "change": data["change"]})
        self.assertEqual((code, body), (404, {"error": "not found"}))
        owners = self.owner("GET", f"/api/canvases/{cid}/history?ws=acme")[1]["entries"][0]["id"]
        code, body = self.run_cmd("person", "undo", {"id": cid, "change": owners})
        self.assertEqual((code, body), (404, {"error": "not found"}))

    def test_project_param_never_moves_the_write(self):
        code, data = self.run_cmd("owner", "create-canvas", {"title": "X", "project": "demo"})
        self.assertEqual(code, 200, data)
        self.assertIn(data["id"], cs.list_ids(self.root, "acme", "cv"))
        self.assertNotIn(data["id"], cs.list_ids(self.root, "demo", "cv"))

    def test_bad_input_is_400(self):
        for body in ({"ws": "acme"}, {"ws": "acme", "cmd": "nope"},
                     {"ws": "acme", "cmd": "get-canvas", "params": "x"}, {"cmd": "list-canvases"}):
            self.assertEqual(self.owner("POST", "/api/canvases", body)[0], 400, body)
        self.assertEqual(self.owner("GET", "/api/canvases")[0], 400)
        code, _, _ = request("POST", "/api/canvases", raw=b"{nope", owner=True)
        self.assertEqual(code, 400)

    def test_core_validation_errors_are_400(self):
        code, data = self.run_cmd("owner", "create-canvas", {})
        self.assertEqual(code, 400, data)
        cid = self.make("Open", pin="acme/general")
        code, data = self.run_cmd("owner", "update-block", {"id": cid, "block": "x", "version": 1,
                                                            "blocks": {"md": "y"}})
        self.assertEqual(code, 400, data)
        code, data = self.run_cmd("person", "add-block", {"id": cid, "blocks": {"type": "text", "md": "x"}})
        self.assertEqual(code, 400, data)  # missing version
        code, data = self.run_cmd("owner", "create-list", {"title": "L", "columns": "{nope"})
        self.assertEqual(code, 400, data)

    def test_a_bug_in_core_is_500_not_404(self):
        cid = self.make("Open")
        with mock.patch.object(server.canvases, "dispatch", side_effect=KeyError("key")):
            code, data = self.owner("GET", f"/api/canvases/{cid}?ws=acme")
        self.assertEqual((code, data), (500, {"error": "internal error"}))

    def test_delete_list_never_counts_canvases_a_person_cannot_see(self):
        rooms.save_topic("acme", "secret", ["cos"], "cos", self.root, private=True, people=["p-other"])
        cols = [{"key": "name", "label": "Name"}]
        cid = self.make("Leads", pin="acme/general", blocks=[{"type": "list", "title": "L", "columns": cols}])
        lid = self.owner("GET", f"/api/canvases/{cid}?ws=acme")[1]["blocks"][0]["list"]
        self.make("Hidden", pin="acme/t-secret", blocks=[{"type": "list", "list": lid}])
        v = self.person("GET", f"/api/lists/{lid}?ws=acme")[1]["version"]
        code, data = self.run_cmd("person", "delete-list", {"id": lid, "version": v})
        self.assertEqual(code, 403, data)
        self.assertNotIn("2 canvas", data["error"])
        self.assertNotIn("canvas(es)", data["error"])

    def test_delete_list_hidden_embed_reads_like_a_visible_one(self):
        rooms.save_topic("acme", "secret", ["cos"], "cos", self.root, private=True, people=["p-other"])
        cols = [{"key": "name", "label": "Name"}]
        seen = self.make("Seen", pin="acme/general", blocks=[{"type": "list", "title": "A", "columns": cols}])
        lid_a = self.owner("GET", f"/api/canvases/{seen}?ws=acme")[1]["blocks"][0]["list"]
        both = self.make("Both", pin="acme/general", blocks=[{"type": "list", "title": "B", "columns": cols}])
        lid_b = self.owner("GET", f"/api/canvases/{both}?ws=acme")[1]["blocks"][0]["list"]
        self.make("Hidden", pin="acme/t-secret", blocks=[{"type": "list", "list": lid_b}])
        out = []
        for lid in (lid_a, lid_b):
            v = self.person("GET", f"/api/lists/{lid}?ws=acme")[1]["version"]
            out.append(self.run_cmd("person", "delete-list", {"id": lid, "version": v}))
        self.assertEqual(out[0], out[1])
        self.assertEqual(out[0][0], 403)
        # the owner keeps the detailed count
        v = self.owner("GET", f"/api/lists/{lid_b}?ws=acme")[1]["version"]
        code, data = self.run_cmd("owner", "delete-list", {"id": lid_b, "version": v})
        self.assertIn("2 canvas(es)", data["error"])

    def test_trash_race_is_404(self):
        cid = self.make("Open")
        with mock.patch.object(server.canvases.cs, "to_trash", side_effect=LookupError(cid)):
            code, data = self.run_cmd("owner", "delete-canvas", {"id": cid, "version": self.version(cid)})
        self.assertEqual(code, 404, data)

    def test_person_write_needs_same_origin(self):
        cid = self.make("Open", pin="acme/general")
        before = cs.read(self.root, "acme", cid)
        code, _, _ = request("POST", "/api/canvases", {"ws": "acme", "cmd": "create-canvas",
                                                       "params": {"title": "X", "pin": "acme/general"}},
                             member=self.tok, host=PUBLIC, origin="https://evil.example")
        self.assertEqual(code, 403)
        code, _, _ = request("POST", "/api/canvases", {"ws": "acme", "cmd": "add-block", "params": {
            "id": cid, "version": before["version"], "blocks": {"type": "text", "md": "x"}}},
                             member=self.tok, host=PUBLIC, origin="https://evil.example")
        self.assertEqual(code, 403)
        self.assertEqual(cs.list_ids(self.root, "acme", "cv"), [cid])
        self.assertEqual(cs.read(self.root, "acme", cid), before)

    def test_owner_write_needs_same_origin(self):
        code, _, _ = request("POST", "/api/canvases", {"ws": "acme", "cmd": "create-canvas",
                                                       "params": {"title": "X"}},
                             owner=True, origin="https://evil.example")
        self.assertEqual(code, 403)
        self.assertEqual(cs.list_ids(self.root, "acme", "cv"), [])

    # -- lists ---------------------------------------------------------------- #
    def test_lists_only_through_a_visible_canvas(self):
        cols = [{"key": "name", "label": "Name"}, {"key": "stage", "label": "Stage", "type": "select",
                                                   "options": ["new", "won"]}]
        cid = self.make("Leads", pin="acme/general", blocks=[{"type": "list", "title": "Leads",
                                                              "columns": cols}])
        lid = self.owner("GET", f"/api/canvases/{cid}?ws=acme")[1]["blocks"][0]["list"]
        code, other = self.run_cmd("owner", "create-list", {"title": "Other", "columns": cols})
        self.assertEqual(code, 200)
        v = self.owner("GET", f"/api/lists/{lid}?ws=acme")[1]["version"]
        code, _ = self.run_cmd("person", "add-row", {"id": lid, "version": v,
                                                     "cells": {"name": "acme", "stage": "new"}})
        self.assertEqual(code, 200)
        self.run_cmd("owner", "add-row", {"id": lid, "version": v + 1,
                                          "cells": {"name": "demo", "stage": "won"}})
        code, data = self.person("GET", f"/api/lists/{lid}?ws=acme&limit=1&sort=-name")
        self.assertEqual(code, 200)
        self.assertEqual((data["total"], [r["cells"]["name"] for r in data["rows"]]), (2, ["demo"]))
        self.assertIn("columns", data)
        code, data = self.person("GET", f"/api/lists/{lid}/rows?ws=acme&offset=1&limit=5"
                                        "&filter=%7B%22stage%22%3A%22new%22%7D")
        self.assertEqual(code, 200, data)
        self.assertNotIn("columns", data)
        self.assertEqual((data["total"], data["rows"]), (1, []))
        self.assertEqual(self.person("GET", f"/api/lists/{other['id']}?ws=acme")[0], 404)
        self.assertEqual(self.person("GET", "/api/lists/ls999/rows?ws=acme")[0], 404)
        self.assertEqual(self.owner("GET", f"/api/lists/{lid}?ws=acme&limit=x")[0], 400)

    # -- rooms ---------------------------------------------------------------- #
    def test_room_delete_drops_its_pins_and_rename_moves_them(self):
        rooms.save_topic("acme", "launch", ["cos"], "cos", self.root)
        rooms.save_topic("acme", "deals", ["cos"], "cos", self.root)
        a = self.make("A", pin="acme/t-launch")
        b = self.make("B", pin="acme/t-deals")
        code, _ = self.owner("POST", "/api/rooms/delete", {"ws": "acme", "name": "launch"})
        self.assertEqual(code, 200)
        self.assertNotIn("acme/t-launch", cs.pins(self.root, "acme"))
        # a new channel under the old name starts with no pins
        rooms.save_topic("acme", "launch", ["cos"], "cos", self.root)
        self.assertEqual(self.person("GET", f"/api/canvases/{a}?ws=acme")[0], 404)
        code, _ = self.owner("POST", "/api/rooms", {"ws": "acme", "name": "won", "replace": "deals",
                                                    "members": ["cos"], "lead": "cos"})
        self.assertEqual(code, 200)
        pins = cs.pins(self.root, "acme")
        self.assertEqual((pins.get("acme/t-won"), pins.get("acme/t-deals")), ([b], None))


    def test_room_delete_drops_pins_even_if_the_delete_fails(self):
        rooms.save_topic("acme", "launch", ["cos"], "cos", self.root)
        self.make("A", pin="acme/t-launch")
        with mock.patch.object(server.rooms, "delete_topic", side_effect=RuntimeError("disk")):
            self.owner("POST", "/api/rooms/delete", {"ws": "acme", "name": "launch"})
        self.assertNotIn("acme/t-launch", cs.pins(self.root, "acme"))


class BotPathUntouched(unittest.TestCase):
    def test_bot_actor_helper_still_builds_bots(self):
        actor = server._canvas_actor({"bot": "cos", "ws": "acme"})
        self.assertEqual(actor.tag, "bot:cos")


if __name__ == "__main__":
    unittest.main()
