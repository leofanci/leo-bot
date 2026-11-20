"""Notes to self (a room no bot answers in) and people's profile pictures."""
import base64
import tempfile
import unittest
from pathlib import Path

from core import avatars, rooms
from tests.test_server_people import Base, PUBLIC, request

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

    def test_avatar_set_serve_remove(self):
        body = {"data": base64.b64encode(PNG).decode()}
        self.assertEqual(request("POST", "/api/avatar", body, owner=True)[0], 200)
        code, raw, headers = request("GET", "/api/avatar/ceo", owner=True)
        self.assertEqual((code, raw, headers["Content-Type"]), (200, PNG, "image/png"))
        pid, token = self.join()
        code, out, _ = request("POST", "/api/avatar", {"icon": "zap", "hue": "ocean"}, member=token, host=PUBLIC)
        self.assertEqual(out["avatar"], {"icon": "zap", "hue": "ocean"})
        code, got, _ = request("GET", "/api/avatars", member=token, host=PUBLIC)
        self.assertEqual(set(got["avatars"]), {"ceo", pid})
        self.assertEqual(request("POST", "/api/avatar", {"data": "!!"}, owner=True)[0], 400)
        self.assertEqual(request("POST", "/api/avatar", {"remove": True}, owner=True)[0], 200)
        self.assertEqual(request("GET", "/api/avatar/ceo", owner=True)[0], 404)

    def test_removed_person_loses_notes_and_picture(self):
        pid, token = self.join()
        _, out, _ = request("POST", "/api/self-note", {"ws": "acme", "text": "mine"}, member=token, host=PUBLIC)
        request("POST", "/api/avatar", {"icon": "zap", "hue": "ocean"}, member=token, host=PUBLIC)
        code, _, _ = request("POST", "/api/people/remove", {"ws": "acme", "id": pid}, owner=True)
        self.assertEqual(code, 200)
        self.assertEqual(request("GET", f"/api/chats/{out['id']}", owner=True)[0], 404)
        self.assertNotIn(pid, request("GET", "/api/avatars", owner=True)[1]["avatars"])
