import tempfile, unittest, uuid
from pathlib import Path
from unittest import mock

import dashboard.server as server
store = server.chat_store  # same module object the server uses


class _Sess:
    def __init__(self, sid=None, turns=1):
        self.session_id = sid or str(uuid.uuid4())
        self._turn_count = turns
        self._last_skill = "workspace"
        self._skill_explicit = True

    def is_fresh(self):
        return False


class ChatStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.p = mock.patch.object(store, "CHATS_DIR", Path(self.tmp.name) / "chats")
        self.p.start()

    def tearDown(self):
        self.p.stop()
        self.tmp.cleanup()

    def test_chat_id_must_match_whole(self):
        s = _Sess()
        store.record_turn(s, None, "hi", "ok")
        self.assertIsNotNone(store.load(s.session_id))
        with self.assertRaises(ValueError):  # "$" alone would admit it
            store._path(s.session_id + "\n")

    def test_job_and_title(self):
        s = _Sess()
        store.record_turn(s, None, "@cmo /positioning review", "reply", meta={"cost": 0.1}, ws="acme",
                          job="j1", title="Job: weekly")
        rec = store.load(s.session_id)
        self.assertEqual((rec["title"], rec["job"], rec["spend"][-1]["job"]), ("Job: weekly", "j1", "j1"))
        self.assertEqual(store.list_meta()[0]["job"], "j1")
        store.record_turn(s, None, "Yes, approved.", "Saved.", meta={"cost": 0.1}, ws="acme")
        rec = store.load(s.session_id)
        self.assertEqual(rec["title"], "Job: weekly")
        self.assertNotIn("job", rec["spend"][-1])  # the owner's own turn counts as theirs

    def test_post_note_starts_a_thread(self):
        rec = store.post_note("Digest 2026-06-01", "hq/general", "cos", "line one")
        got = store.load(rec["id"])
        self.assertEqual((got["title"], got["room"], got["scope"]), ("Digest 2026-06-01", "hq/general", None))
        self.assertEqual([(m["role"], m["bot"], m["content"]) for m in got["messages"]],
                         [("assistant", "cos", "line one")])

    def test_record_list_load_delete(self):
        s = _Sess()
        store.record_turn(s, ("demo", "/app"), "/workspace plan the demo launch", "ok")
        store.record_turn(s, ("demo", "/app"), "next", "")
        rec = store.load(s.session_id)
        self.assertEqual(rec["title"], "plan the demo launch")
        self.assertEqual([m["role"] for m in rec["messages"]], ["user", "assistant", "user"])
        self.assertEqual(rec["scope"], ["demo", "/app"])
        self.assertTrue(rec["started"])
        self.assertEqual([c["id"] for c in store.list_meta()], [s.session_id])
        self.assertTrue(store.delete(s.session_id))
        self.assertIsNone(store.load(s.session_id))
        self.assertEqual(store.list_meta(), [])

    def test_bad_id_rejected(self):
        self.assertIsNone(store.load("../etc/passwd"))
        with self.assertRaises(ValueError):
            store.delete("../x")


class OpenSavedChat(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.p = mock.patch.object(store, "CHATS_DIR", Path(self.tmp.name) / "chats")
        self.p.start()
        self.old = server._CHAT, server._CHAT_SCOPE
        server._CHAT = server._CHAT_SCOPE = None

    def tearDown(self):
        server._CHAT, server._CHAT_SCOPE = self.old
        self.p.stop()
        self.tmp.cleanup()

    def test_saved_chat_resumes_its_session(self):
        s = _Sess(turns=3)
        store.record_turn(s, ("acme", "/a"), "hi", "hello")
        sess = server.get_chat_session(("acme", "/a"), s.session_id)
        self.assertEqual(sess.session_id, s.session_id)
        self.assertFalse(sess.is_fresh())
        self.assertEqual(sess._turn_count, 3)
        self.assertIn("--resume", sess._base_cmd())
        self.assertIs(server.get_chat_session(("acme", "/a"), s.session_id), sess)

    def test_scope_mismatch_starts_new_chat(self):
        s = _Sess()
        store.record_turn(s, ("acme", "/a"), "hi", "hello")
        server.get_chat_session(("acme", "/a"), s.session_id)
        other = server.get_chat_session(("demo", "/b"), s.session_id)
        self.assertNotEqual(other.session_id, s.session_id)
        self.assertTrue(other.is_fresh())
        self.assertIsNotNone(store.load(s.session_id))  # old chat kept

    def test_unscoped_chat_adopts_first_project(self):
        s = _Sess()
        store.record_turn(s, None, "hi", "hello")
        sess = server.get_chat_session(("acme", "/a"), s.session_id)
        self.assertEqual(sess.session_id, s.session_id)
        self.assertIs(server.get_chat_session(("acme", "/a"), s.session_id), sess)
        # once bound, another project still forks
        self.assertNotEqual(server.get_chat_session(("demo", "/b"), s.session_id).session_id,
                            s.session_id)

    def test_unknown_id_starts_fresh_not_active(self):
        active = server.get_chat_session(None)
        fresh = server.get_chat_session(None, str(uuid.uuid4()))
        self.assertIsNot(fresh, active)
        self.assertTrue(fresh.is_fresh())

    def test_new_never_continues_active_chat(self):
        active = server.get_chat_session(("acme", "/a"))
        fresh = server.get_chat_session(("acme", "/a"), None, new=True)
        self.assertIsNot(fresh, active)
        self.assertTrue(fresh.is_fresh())


class AppsUsed(ChatStore):
    def test_apps_used_round_trip(self):
        s = _Sess()
        s.apps_used = True
        rec = store.record_turn(s, None, "summarize my inbox", "done")
        self.assertIs(rec["apps_used"], True)
        sess = server.chat_session.ChatSession.restore(".", "R", store.load(s.session_id))
        self.assertIs(sess.apps_used, True)
        self.assertIs(server.chat_session.ChatSession(repo_dir=".", rail="R").apps_used, False)


if __name__ == "__main__":
    unittest.main()
