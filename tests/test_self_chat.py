"""Notes to self (a room no bot answers in) and people's profile pictures."""
import base64
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from core import avatars, people, rooms
from tests.test_server_people import Base, PUBLIC, request, server

self_chat = server.self_chat

APP_JS = (Path(__file__).resolve().parent.parent / "dashboard" / "app.js").read_text(encoding="utf-8")

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 32


class _Team:
    ws = "acme"

    def ordered(self):
        return []


class Core(unittest.TestCase):
    def test_self_room_listed_and_private_to_starter(self):
        listed = rooms.list_rooms(_Team(), "acme", Path(tempfile.mkdtemp()))
        room = next(r for r in listed if r["kind"] == "self")
        self.assertEqual(room["id"], "acme/dm-self")
        mine, theirs = {"room": room["id"]}, {"room": room["id"], "by": "p-1"}
        owner = {"kind": "owner"}
        self.assertTrue(rooms.chat_visible(mine, room, owner))
        self.assertFalse(rooms.chat_visible(theirs, room, owner))

    def test_avatar_image_then_preset_replaces_it(self):
        root = Path(tempfile.mkdtemp())
        self.assertIn("image", avatars.save_image("ceo", PNG, root))
        self.assertEqual(avatars.save_preset("ceo", "zap", "plum", root), {"icon": "zap", "hue": "plum"})
        self.assertIsNone(avatars.get_image("ceo", root))
        self.assertIn("image", avatars.save_image("ceo", PNG, root))
        self.assertEqual(list(avatars.all_views(root)), ["ceo"])
        avatars.remove("ceo", root)
        self.assertEqual(avatars.all_views(root), {})

    def test_avatar_refuses_bad_input(self):
        root = Path(tempfile.mkdtemp())
        for bad in (b"<svg onload=x>", b"", b"x" * (avatars.MAX_BYTES + 1)):
            with self.assertRaises(ValueError):
                avatars.save_image("ceo", bad, root)
        for who in ("../x", "a/b", ""):
            with self.assertRaises(ValueError):
                avatars.save_image(who, PNG, root)
        with self.assertRaises(ValueError):
            avatars.save_preset("ceo", "nope", "plum", root)


class Http(Base):
    def test_owner_note_round_trip_and_ask_refused(self):
        code, got, _ = request("GET", "/api/self-chat?ws=acme", owner=True)
        self.assertEqual(code, 200)
        code, out, _ = request("POST", "/api/self-note", {"ws": "acme", "text": "buy milk"}, owner=True)
        self.assertEqual(code, 200)
        self.assertEqual(out["id"], got["id"])
        code, rec, _ = request("GET", f"/api/chats/{got['id']}", owner=True)
        self.assertEqual([m["content"] for m in rec["messages"]], ["buy milk"])
        code, lst, _ = request("GET", "/api/chats?room=acme/dm-self", owner=True)
        self.assertEqual([c["id"] for c in lst["chats"]], [got["id"]])
        self.assertEqual(request("POST", "/api/ask", {"room_id": "acme/dm-self", "messages": []}, owner=True)[0], 400)
        self.assertEqual(request("POST", "/api/self-note", {"ws": "acme", "text": " "}, owner=True)[0], 400)

    def test_person_notes_stay_theirs(self):
        pid, token = self.join()
        code, out, _ = request("POST", "/api/self-note", {"ws": "acme", "text": "mine"}, member=token, host=PUBLIC)
        self.assertEqual(code, 200)
        code, rec, _ = request("GET", f"/api/chats/{out['id']}", member=token, host=PUBLIC)
        self.assertEqual(rec["messages"][0]["by"], pid)
        _, other = self.join(name="Sam")
        self.assertEqual(request("GET", f"/api/chats/{out['id']}", member=other, host=PUBLIC)[0], 404)
        self.assertEqual(request("POST", "/api/self-note", {"ws": "demo", "text": "x"}, member=token, host=PUBLIC)[0], 403)
        _, mine, _ = request("GET", "/api/chats?room=acme/dm-self", member=other, host=PUBLIC)
        self.assertEqual(mine["chats"], [])

    @staticmethod
    def global_headers():
        """What end_headers adds to every response (request() stubs it out)."""
        h, sent = server.Handler.__new__(server.Handler), {}
        h.send_header = sent.__setitem__
        with mock.patch.object(server.BaseHTTPRequestHandler, "end_headers"):
            server.Handler.end_headers(h)
        return sent

    def test_avatar_set_serve_remove(self):
        body = {"data": base64.b64encode(PNG).decode()}
        self.assertEqual(request("POST", "/api/avatar", body, owner=True)[0], 200)
        code, raw, headers = request("GET", "/api/avatar/ceo", owner=True)
        self.assertEqual((code, raw, headers["Content-Type"]), (200, PNG, "image/png"))
        self.assertIn("sandbox", headers["Content-Security-Policy"])
        self.assertEqual(self.global_headers()["X-Content-Type-Options"], "nosniff")
        pid, token = self.join()
        code, out, _ = request("POST", "/api/avatar", {"icon": "zap", "hue": "ocean"}, member=token, host=PUBLIC)
        self.assertEqual(out["avatar"], {"icon": "zap", "hue": "ocean"})
        code, got, _ = request("GET", "/api/avatars", member=token, host=PUBLIC)
        self.assertEqual(set(got["avatars"]), {"ceo", pid})
        self.assertEqual(request("POST", "/api/avatar", {"data": "!!"}, owner=True)[0], 400)
        self.assertEqual(request("POST", "/api/avatar", {"remove": True}, owner=True)[0], 200)
        self.assertEqual(request("GET", "/api/avatar/ceo", owner=True)[0], 404)
        self.assertEqual(list((self.root / "team" / "avatars").glob("ceo.*")), [])

    def test_removed_person_loses_notes_and_picture(self):
        pid, token = self.join()
        _, out, _ = request("POST", "/api/self-note", {"ws": "acme", "text": "mine"}, member=token, host=PUBLIC)
        request("POST", "/api/avatar", {"icon": "zap", "hue": "ocean"}, member=token, host=PUBLIC)
        code, _, _ = request("POST", "/api/people/remove", {"ws": "acme", "id": pid}, owner=True)
        self.assertEqual(code, 200)
        self.assertEqual(request("GET", f"/api/chats/{out['id']}", owner=True)[0], 404)
        self.assertFalse((self.root / "chats" / f"{out['id']}.json").exists())
        self.assertNotIn(pid, request("GET", "/api/avatars", owner=True)[1]["avatars"])
        self.assertNotIn(pid, avatars._presets(self.root))

    def test_avatars_stay_inside_shared_workspaces(self):
        pid_a, tok_a = self.join("acme", name="Pat")
        pid_d, tok_d = self.join("demo", name="Sam")
        request("POST", "/api/avatar", {"icon": "zap", "hue": "ocean"}, member=tok_a, host=PUBLIC)
        body = {"data": base64.b64encode(PNG).decode()}
        self.assertEqual(request("POST", "/api/avatar", body, member=tok_d, host=PUBLIC)[0], 200)
        self.assertEqual(request("POST", "/api/avatar", body, owner=True)[0], 200)
        _, got, _ = request("GET", "/api/avatars", member=tok_a, host=PUBLIC)
        self.assertEqual(set(got["avatars"]), {"ceo", pid_a})
        self.assertEqual(request("GET", f"/api/avatar/{pid_d}", member=tok_a, host=PUBLIC)[0], 404)
        self.assertEqual(request("GET", "/api/avatar/ceo", member=tok_a, host=PUBLIC)[0], 200)
        self.assertEqual(request("GET", f"/api/avatar/{pid_d}", member=tok_d, host=PUBLIC)[0], 200)
        _, got, _ = request("GET", "/api/avatars", owner=True)
        self.assertEqual(set(got["avatars"]), {"ceo", pid_a, pid_d})
        _, tok_b = self.join("demo", name="Pat")  # a teammate in demo sees Sam
        self.assertEqual(request("GET", f"/api/avatar/{pid_d}", member=tok_b, host=PUBLIC)[0], 200)

    def test_notes_room_takes_no_canvas(self):
        pin = {"ws": "acme", "cmd": "create-canvas", "params": {"title": "Plan", "pin": "acme/dm-self"}}
        self.assertEqual(request("POST", "/api/canvases", pin, owner=True)[0], 404)
        self.assertEqual(request("GET", "/api/canvases?ws=acme&room=acme/dm-self", owner=True)[0], 404)
        _, token = self.join()
        self.assertEqual(request("POST", "/api/canvases", pin, member=token, host=PUBLIC)[0], 404)
        self.assertEqual(request("GET", "/api/canvases?ws=acme&room=acme/dm-self",
                                 member=token, host=PUBLIC)[0], 404)
        code, out, _ = request("POST", "/api/canvases", {**pin, "params": {"title": "Plan", "pin": "acme/general"}},
                               owner=True)
        self.assertEqual(code, 200, out)
        again = {"ws": "acme", "cmd": "pin-canvas", "params": {"id": out["id"], "room": "acme/dm-self"}}
        self.assertEqual(request("POST", "/api/canvases", again, owner=True)[0], 404)
        listed = {"ws": "acme", "cmd": "list-canvases", "params": {"room": "acme/dm-self"}}
        self.assertEqual(request("POST", "/api/canvases", listed, owner=True)[0], 404)

    def test_owner_never_reads_a_persons_notes(self):
        _, token = self.join()
        _, out, _ = request("POST", "/api/self-note", {"ws": "acme", "text": "mine"}, member=token, host=PUBLIC)
        request("POST", "/api/self-note", {"ws": "acme", "text": "owner note"}, owner=True)
        _, lst, _ = request("GET", "/api/chats", owner=True)
        ids = [c["id"] for c in lst["chats"]]
        self.assertNotIn(out["id"], ids)
        self.assertIn(self_chat.chat_id("acme", "ceo"), ids)
        self.assertEqual(request("GET", f"/api/chats/{out['id']}", owner=True)[0], 404)
        react = {"i": 0, "emoji": "👍"}
        self.assertEqual(request("POST", f"/api/chats/{out['id']}/react", react, owner=True)[0], 404)
        self.assertEqual(request("POST", f"/api/chats/{out['id']}/delete", {}, owner=True)[0], 404)
        self.assertEqual(request("GET", f"/api/chats/{out['id']}", member=token, host=PUBLIC)[0], 200)

    def test_owner_ask_never_reuses_a_notes_thread(self):
        _, token = self.join()
        _, theirs, _ = request("POST", "/api/self-note", {"ws": "acme", "text": "mine"}, member=token, host=PUBLIC)
        _, own, _ = request("POST", "/api/self-note", {"ws": "acme", "text": "mine too"}, owner=True)
        seen = []

        class Stop(Exception):
            pass

        def capture(scope, chat_id, new=False, linked=None):
            seen.append(chat_id)
            raise Stop

        with mock.patch.object(server, "get_chat_session", side_effect=capture), \
                mock.patch.object(server.traceback, "print_exc"):
            for cid in (theirs["id"], own["id"]):
                body = {"room_id": "acme/general", "chat_id": cid, "messages": [{"role": "user", "content": "hi"}]}
                try:
                    request("POST", "/api/ask", body, owner=True)
                except Stop:
                    pass
        self.assertEqual(seen, [None, None])
        for cid in (theirs["id"], own["id"]):
            self.assertEqual(len(server.chat_store.load(cid)["messages"]), 1)

    def test_notes_are_capped(self):
        with mock.patch.object(self_chat, "MAX_NOTES", 2):
            for t in ("a", "b"):
                self.assertEqual(request("POST", "/api/self-note", {"ws": "acme", "text": t}, owner=True)[0], 200)
            code, out, _ = request("POST", "/api/self-note", {"ws": "acme", "text": "c"}, owner=True)
        self.assertEqual(code, 400)
        self.assertIn("full", out["error"])
        with mock.patch.object(self_chat, "MAX_TOTAL", 5):
            self.assertEqual(request("POST", "/api/self-note", {"ws": "demo", "text": "abcdef"}, owner=True)[0], 400)


class AvatarLock(unittest.TestCase):
    def test_concurrent_presets_all_land(self):
        root = Path(tempfile.mkdtemp())
        real = avatars._presets

        def slow(r=None):
            rows = real(r)
            time.sleep(0.01)  # widen the read-change-write window
            return rows

        who = [f"p-{i}" for i in range(8)]
        with mock.patch.object(avatars, "_presets", slow):
            ts = [threading.Thread(target=avatars.save_preset, args=(w, "zap", "plum", root)) for w in who]
            for t in ts:
                t.start()
            for t in ts:
                t.join()
        self.assertEqual(set(avatars._presets(root)), set(who))


class WorkspaceDelete(Base):
    def setUp(self):
        super().setUp()
        for p in ({"slug": "acme", "name": "Acme"}, {"slug": "demo", "name": "Demo"}):
            (self.root / "projects" / p["slug"] / "project.md").write_text(f"---\nname: {p['name']}\n---\n")
        for p in (mock.patch.object(server.fileops, "ROOT", self.root),
                  mock.patch.object(server.fileops, "reindex", lambda: None)):
            p.start()
            self.addCleanup(p.stop)

    def test_delete_and_reused_slug_start_with_no_notes_or_pictures(self):
        pid, token = self.join("acme")
        _, theirs, _ = request("POST", "/api/self-note", {"ws": "acme", "text": "old"}, member=token, host=PUBLIC)
        _, own, _ = request("POST", "/api/self-note", {"ws": "acme", "text": "old too"}, owner=True)
        _, kept, _ = request("POST", "/api/self-note", {"ws": "demo", "text": "stays"}, owner=True)
        body = {"data": base64.b64encode(PNG).decode()}
        self.assertEqual(request("POST", "/api/avatar", body, member=token, host=PUBLIC)[0], 200)
        code, out, _ = request("POST", "/api/project/acme/delete", {}, owner=True)
        self.assertEqual(code, 200, out)
        for cid in (theirs["id"], own["id"]):
            self.assertFalse((self.root / "chats" / f"{cid}.json").exists())
        self.assertIsNotNone(server.chat_store.load(kept["id"]))
        self.assertEqual(list((self.root / "team" / "avatars").glob(f"{pid}.*")), [])
        self.assertIsNone(people.get(self.root, pid))
        (self.root / "projects" / "acme").mkdir()
        (self.root / "projects" / "acme" / "project.md").write_text("---\nname: New Acme\n---\n")
        _, again, _ = request("POST", "/api/self-note", {"ws": "acme", "text": "new"}, owner=True)
        self.assertEqual(again["id"], own["id"])
        self.assertEqual([m["content"] for m in again["messages"]], ["new"])


class Ui(unittest.TestCase):
    def test_notes_room_skips_canvases_in_the_ui(self):
        self.assertIn('return kind === "self" || (isPerson() && kind === "dm");', APP_JS)
        self.assertNotIn('isPerson() && roomById(room)?.kind === "dm"', APP_JS)
        self.assertEqual(APP_JS.count("noCanvasRoom(room)"), 5)  # the helper and its four uses

    def test_welcome_copy_is_honest(self):
        self.assertIn("Only you see it here, and no bot answers.", APP_JS)
        self.assertNotIn("Nobody else sees it", APP_JS)

    def test_avatars_load_before_first_paint(self):
        self.assertIn("await Promise.all([ensureSchemas(), loadAvatars()]); await renderRail();", APP_JS)
