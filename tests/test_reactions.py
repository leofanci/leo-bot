"""Emoji reactions on thread messages: people toggle them, bots add them with a
`REACT:` line, and bots see them as context on their next turn."""
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

import dashboard.server as server
from dashboard import ai_rules
from tests.test_chat_store import _Sess
from tests.test_server_people import PUBLIC, Base, request
from tests import test_teammates

store = server.chat_store


def _thread(root, cid=None, room="acme/general", shared=True, **extra):
    rec = {"id": cid or str(uuid.uuid4()), "room": room, "shared": shared,
           "messages": [{"role": "user", "content": "ship it?"},
                        {"role": "assistant", "bot": "cos", "content": "Ready when you are.", "ts": 1.0}],
           **extra}
    store.save(rec)
    return rec["id"]


class Emoji(unittest.TestCase):
    def test_accepts_emoji(self):
        for e in ("👍", "❤️", "🇮🇹", "1️⃣", "👍🏽", "👩‍💻", "🏴󠁧󠁢󠁥󠁮󠁧󠁿", " 🎉 "):
            self.assertEqual(store.clean_emoji(e), e.strip(), repr(e))

    def test_refuses_text_markup_and_controls(self):
        for e in ("", "ok", "<b>", "👍 ok", "x" * 3, "‮👍", "^", "1", "👍" * 9, None, 5):
            self.assertIsNone(store.clean_emoji(e), repr(e))


class Toggle(Base):
    def test_toggle_adds_then_takes_back(self):
        cid = _thread(self.root)
        self.assertEqual(store.toggle_reaction(cid, 1, "👍", by="ceo")[0]["emoji"], "👍")
        self.assertEqual(store.toggle_reaction(cid, 1, "👍", by="p-1", by_name="Pat")[1]["by_name"], "Pat")
        self.assertEqual([r["by"] for r in store.toggle_reaction(cid, 1, "👍", by="ceo")], ["p-1"])
        store.toggle_reaction(cid, 1, "👍", by="p-1")
        self.assertNotIn("reactions", store.load(cid)["messages"][1])

    def test_bad_index_or_emoji(self):
        cid = _thread(self.root)
        for i in (-1, 2, "0", True, None):
            self.assertIsNone(store.toggle_reaction(cid, i, "👍", by="ceo"), repr(i))
        with self.assertRaises(ValueError):
            store.toggle_reaction(cid, 0, "nope", by="ceo")
        self.assertIsNone(store.toggle_reaction(str(uuid.uuid4()), 0, "👍", by="ceo"))

    def test_capped_per_message(self):
        cid = _thread(self.root)
        for n in range(store.MAX_REACTIONS):
            store.toggle_reaction(cid, 0, "👍", by=f"p-{n}")
        with self.assertRaises(ValueError):
            store.toggle_reaction(cid, 0, "👍", by="p-late")

    def test_reacting_does_not_reorder_threads(self):
        cid = _thread(self.root, updated=5.0)
        store.toggle_reaction(cid, 0, "👍", by="ceo")
        self.assertEqual(store.load(cid)["updated"], 5.0)


class Routes(Base):
    def setUp(self):
        super().setUp()
        self.pid, self.tok = self.join()

    def react(self, cid, i=1, emoji="👍", **who):
        return request("POST", f"/api/chats/{cid}/react", {"i": i, "emoji": emoji}, **who)

    def test_owner_reacts(self):
        cid = _thread(self.root, shared=False)
        code, data, _ = self.react(cid, owner=True)
        self.assertEqual((code, data["reactions"]), (200, [{**data["reactions"][0], "emoji": "👍", "by": "ceo"}]))
        self.assertEqual(self.react(cid, i=9, owner=True)[0], 404)
        self.assertEqual(self.react(cid, emoji="hi", owner=True)[0], 400)

    def test_person_reacts_only_where_they_can_read(self):
        cid = _thread(self.root)
        code, data, _ = self.react(cid, member=self.tok, host=PUBLIC)
        self.assertEqual(code, 200)
        self.assertEqual((data["reactions"][0]["by"], data["reactions"][0]["by_name"]), (self.pid, "Pat"))
        owner_only = _thread(self.root, shared=False)  # the owner's own thread in #general
        dm = _thread(self.root, room="acme/dm-cos")  # someone else's DM thread
        other_ws = _thread(self.root, room="demo/general")
        for c in (owner_only, dm, other_ws, str(uuid.uuid4()), "../x"):
            self.assertEqual(self.react(c, member=self.tok, host=PUBLIC)[0], 404, c)
        self.assertNotIn("reactions", store.load(owner_only)["messages"][1])

    def test_no_cookie_refused(self):
        cid = _thread(self.root)
        self.assertEqual(self.react(cid, host=PUBLIC)[0], 403)


class Markers(unittest.TestCase):
    def test_extract_reacts(self):
        clean, got = server._extract_reacts("On it.\nREACT: 👀 ✅\n**react: 🎉**\nREACT: not-an-emoji")
        self.assertEqual(clean, "On it.\nREACT: not-an-emoji")  # prose that only looks like one stays
        self.assertEqual(got, ["👀", "✅", "🎉"])
        self.assertEqual(server._extract_reacts("React: we ship Friday."), ("React: we ship Friday.", []))
        self.assertEqual(server._extract_reacts("REACT: " + " ".join("👍👎🎉🔥🚀"))[1],
                         list("👍👎🎉")[:server.MAX_BOT_REACTS])
        self.assertEqual(server._extract_reacts("We react: fast."), ("We react: fast.", []))

    def test_rule_is_in_the_teammate_prompt(self):
        self.assertIn("REACT:", ai_rules.TEAMMATE)


class Context(unittest.TestCase):
    def test_recap_and_note_show_people_reactions(self):
        rec = {"messages": [
            {"role": "user", "content": "plan?", "reactions": [{"emoji": "👀", "bot": "cos", "ts": 1}]},
            {"role": "assistant", "bot": "cos", "content": "Plan A.", "ts": 2,
             "reactions": [{"emoji": "👍", "by": "ceo", "ts": 3},
                           {"emoji": "👎", "by": "p-1", "by_name": "Pat", "ts": 4}]}]}
        recap = store.recap(rec)
        self.assertIn("@cos: Plan A. [👍 You, 👎 Pat]", recap)
        self.assertIn("You: plan? [👀 @cos]", recap)
        note = store.reaction_note(rec)
        self.assertIn('@cos "Plan A.": 👍 You, 👎 Pat', note)
        self.assertNotIn("👀", note)  # a bot's own reaction is not news to it
        rec["messages"].append({"role": "assistant", "bot": "cos", "content": "ok", "ts": 9})
        self.assertEqual(store.reaction_note(rec), "")  # older than the last reply

    def test_note_reaches_the_next_turn(self):
        rec = {"messages": [{"role": "assistant", "bot": "cos", "content": "Plan A.", "ts": 2,
                             "reactions": [{"emoji": "👍", "by": "ceo", "ts": 3}]}]}
        self.assertTrue(store.reaction_note(rec).startswith(store.REACTIONS_HEAD))


class BotTurn(unittest.TestCase):
    _F = test_teammates.Flow
    SID = _F.SID
    setUp, tearDown, turn = _F.setUp, _F.tearDown, _F.turn  # its helper, not its tests

    def test_bot_reacts_to_the_user_message(self):
        events, _, _ = self.turn([[("delta", "On it.\nREACT: 👀"), ("done", {})]], user_msg="@cmo pricing?")
        msgs = store.load(self.SID)["messages"]
        self.assertEqual(msgs[1]["content"], "On it.")
        self.assertEqual([(r["emoji"], r["bot"]) for r in msgs[0]["reactions"]], [("👀", "cmo")])
        self.assertIn({"emoji": "👀", "bot": "cmo"}, [{k: r[k] for k in ("emoji", "bot")}
                                                       for e in events if "reactions" in e for r in e["reactions"]])
        self.assertEqual([e["clean"] for e in events if "clean" in e][-1], "On it.")

    def test_reaction_only_reply_leaves_no_message(self):
        self.turn([[("delta", "REACT: 👍"), ("done", {"tokens": 10, "cost": 0.001})]], user_msg="@cmo thanks")
        rec = store.load(self.SID)
        self.assertEqual([m["role"] for m in rec["messages"]], ["user"])
        self.assertEqual(rec["messages"][0]["reactions"][0]["emoji"], "👍")
        self.assertEqual(rec["tokens"], 10)  # the run still counts

    def test_marker_never_reaches_a_teammate(self):
        _, mates, _ = self.turn([
            [("delta", "Checking.\nREACT: 👀\nCONSULT @research: rivals?"), ("done", {})],
            [("delta", "Done."), ("done", {})],
        ])
        self.assertNotIn("REACT", mates[0].prompt)


class Record(unittest.TestCase):
    def test_record_turn_puts_bot_reactions_on_the_user_message(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(store, "CHATS_DIR", Path(tmp) / "chats"):
            s = _Sess()
            rec = store.record_turn(s, None, "hi", "", reactions=[("cos", "👋"), ("cos", "👋")])
            self.assertEqual([(r["emoji"], r["bot"]) for r in rec["messages"][0]["reactions"]], [("👋", "cos")])


if __name__ == "__main__":
    unittest.main()
