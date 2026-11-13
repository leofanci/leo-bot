"""Short replies: a Leo Bot-owned reply style in the chat rail, on by default, off in Settings."""
import json
import unittest

from core import paths, updater
from dashboard import ai_rules
import dashboard.server as server
from tests.test_server_people import Base, request


class Rail(unittest.TestCase):
    def setUp(self):
        self.saved = paths.SETTINGS.read_text() if paths.SETTINGS.exists() else None
        paths.SETTINGS.unlink(missing_ok=True)

    def tearDown(self):
        if self.saved is None:
            paths.SETTINGS.unlink(missing_ok=True)
        else:
            paths.SETTINGS.write_text(self.saved)

    def write(self, **d):
        paths.SETTINGS.parent.mkdir(parents=True, exist_ok=True)
        paths.SETTINGS.write_text(json.dumps(d))

    def test_on_by_default(self):
        self.assertIs(updater.settings()["short_replies"], True)
        self.assertIn("## Reply style\n" + ai_rules.SHORT_REPLIES, server.rail())
        self.assertTrue(server.rail().startswith(ai_rules.CHAT_RAIL))

    def test_off_removes_the_block(self):
        self.write(short_replies=False)
        self.assertEqual(server.rail(), ai_rules.CHAT_RAIL)

    def test_non_bool_reads_as_on(self):
        self.write(short_replies="no")
        self.assertIs(updater.settings()["short_replies"], True)
        self.assertIn("## Reply style", server.rail())

    def test_style_keeps_saved_text_in_full_prose(self):
        text = ai_rules.SHORT_REPLIES.lower()
        for word in ("osctl", "memos", "full", "warnings", "questions"):
            self.assertIn(word, text)

    def test_new_session_gets_the_current_rail(self):
        self.write(short_replies=False)
        self.assertNotIn("## Reply style", server.new_chat_session().rail)
        self.write(short_replies=True)
        self.assertIn("## Reply style", server.new_chat_session().rail)


class Endpoint(Base):
    def tearDown(self):
        paths.SETTINGS.unlink(missing_ok=True)
        super().tearDown()

    def test_owner_reads_and_toggles(self):
        code, data, _ = request("GET", "/api/settings/chat", owner=True)
        self.assertEqual((code, data), (200, {"short_replies": True}))
        code, data, _ = request("POST", "/api/settings/chat", {"short_replies": False}, owner=True)
        self.assertEqual((code, data), (200, {"short_replies": False}))
        self.assertIs(updater.settings()["short_replies"], False)
        self.assertEqual(request("POST", "/api/settings/chat", {"short_replies": "off"}, owner=True)[0], 400)
        self.assertIs(updater.settings()["short_replies"], False)

    def test_cross_origin_refused(self):
        code, _, _ = request("POST", "/api/settings/chat", {"short_replies": False}, owner=True,
                             origin="https://evil.example")
        self.assertEqual(code, 403)


class NoFrozenRail(unittest.TestCase):
    def test_every_session_reads_the_setting(self):
        src = (paths.CODE / "dashboard" / "server.py").read_text()
        self.assertNotIn("rail=RAIL", src)
        self.assertNotIn(", RAIL, rec)", src)


class SettingsScreen(unittest.TestCase):
    def test_bots_tab_has_the_toggle(self):
        js = (paths.CODE / "dashboard" / "app.js").read_text()
        self.assertIn("data-short-replies", js)
        self.assertIn('jpost("/api/settings/chat", { short_replies: shortBox.checked })', js)


if __name__ == "__main__":
    unittest.main()
