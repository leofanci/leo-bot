"""Private topic channels and per-person DM threads (core.rooms visibility)."""
import tempfile
import unittest
from pathlib import Path

from core import bots, rooms

OWNER = {"kind": "owner"}
PAT = {"kind": "person", "id": "p-pat"}
SAM = {"kind": "person", "id": "p-sam"}


class Private(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "projects" / "acme").mkdir(parents=True)
        self.addCleanup(self.tmp.cleanup)
        self.team = bots.team(self.root, "acme", {"slug": "acme"})

    def room(self, rid):
        return rooms.get_room(rid, self.team, self.root)

    def test_private_channel_shows_to_owner_and_its_people(self):
        rid = rooms.save_topic("acme", "deals", ["cos"], "cos", self.root, private=True, people=["p-pat"])
        r = self.room(rid)
        self.assertTrue(r["private"])
        self.assertEqual(r["people"], ["p-pat"])
        self.assertTrue(rooms.can_see(r, OWNER))
        self.assertTrue(rooms.can_see(r, PAT))
        self.assertFalse(rooms.can_see(r, SAM))
        self.assertFalse(rooms.can_see(r, None))

    def test_open_channel_and_general_show_to_every_person(self):
        rid = rooms.save_topic("acme", "launch", ["cos"], "cos", self.root)
        self.assertFalse(self.room(rid)["private"])
        self.assertTrue(rooms.can_see(self.room(rid), SAM))
        self.assertTrue(rooms.can_see(self.room("acme/general"), SAM))

    def test_turning_private_off_drops_people(self):
        rooms.save_topic("acme", "deals", ["cos"], "cos", self.root, private=True, people=["p-pat"])
        rid = rooms.save_topic("acme", "deals", ["cos"], "cos", self.root, replace="deals")
        self.assertEqual(self.room(rid)["people"], [])
        self.assertTrue(rooms.can_see(self.room(rid), SAM))

    def test_dm_threads_belong_to_their_starter(self):
        dm = self.room("acme/dm-cos")
        self.assertTrue(rooms.chat_visible({"by": "p-pat"}, dm, PAT))
        self.assertFalse(rooms.chat_visible({"by": "p-pat"}, dm, SAM))
        self.assertFalse(rooms.chat_visible({"by": "p-pat"}, dm, OWNER))
        self.assertTrue(rooms.chat_visible({}, dm, OWNER))  # threads from before people existed
        self.assertFalse(rooms.chat_visible({}, dm, PAT))

    def test_topic_threads_are_shared_with_whoever_sees_the_channel(self):
        rid = rooms.save_topic("acme", "deals", ["cos"], "cos", self.root, private=True, people=["p-pat"])
        r = self.room(rid)
        self.assertTrue(rooms.chat_visible({"by": "ceo"}, r, PAT))
        self.assertFalse(rooms.chat_visible({"by": "p-pat"}, r, SAM))


if __name__ == "__main__":
    unittest.main()
