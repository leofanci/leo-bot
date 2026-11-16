"""Per-bot setup (engine, model, access, instructions) per workspace over every-workspace
defaults, and Settings, General (about you, reply language, timezone, caps)."""
import json
import os
import stat
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from core import bots, general, paths
from dashboard.engines import config, registry
import dashboard.server as server
from tests.test_server_people import Base, request


class TempRoot(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)


class Inheritance(TempRoot):
    def test_access_falls_back_to_every_workspace(self):
        self.assertEqual(config.access("acme", "cmo", self.root), "ask")
        config.set_choice("access", "edits", "*/cmo", self.root)
        self.assertEqual(config.access("acme", "cmo", self.root), "edits")
        self.assertEqual(config.access("demo", "cmo", self.root), "edits")
        config.set_choice("access", "ask", "acme/cmo", self.root)  # one workspace keeps Ask
        self.assertEqual(config.access("acme", "cmo", self.root), "ask")
        self.assertEqual(config.access("demo", "cmo", self.root), "edits")
        config.set_choice("access", "ask", "*/cmo", self.root)  # Ask is the default: not stored
        self.assertNotIn("*/cmo", config.public(self.root)["access"])

    def test_model_pins_and_auto_override(self):
        self.assertEqual(config.model("acme", "cmo", self.root), "")
        config.set_choice("models", "strong", "*/cmo", self.root)
        self.assertEqual(config.model("acme", "cmo", self.root), "strong")
        config.set_choice("models", "auto", "acme/cmo", self.root)
        self.assertEqual(config.model("acme", "cmo", self.root), "")
        self.assertEqual(config.model("demo", "cmo", self.root), "strong")
        config.set_choice("models", "", "acme/cmo", self.root)
        self.assertEqual(config.model("acme", "cmo", self.root), "strong")
        for bad in ("opus", "best"):
            with self.assertRaises(ValueError):
                config.set_choice("models", bad, "acme/cmo", self.root)
        config.set_choice("models", "fast", "acme/cos", self.root)
        config.forget_workspace("acme", self.root)
        self.assertEqual(config.public(self.root)["models"], {"*/cmo": "strong"})

    def test_hand_edited_models_are_cleaned(self):
        data = config.load(self.root)
        data["models"] = {"acme/cmo": "huge", "*/cos": "auto", "acme/cos": "auto", "*/gtm": "fast"}
        config.save(data, self.root)
        self.assertEqual(config.load(self.root)["models"], {"acme/cos": "auto", "*/gtm": "fast"})

    def test_engine_order(self):
        for pid in ("anthropic", "openai"):
            config.upsert_provider({"id": pid, "key": "k"}, self.root)
        config.set_choice("default", "api:anthropic", None, self.root)
        config.set_choice("bots", "api:openai", "*/cmo", self.root)
        self.assertEqual(registry.pick(self.root, "acme", "cmo").id, "api:openai")
        self.assertEqual(registry.pick(self.root, "acme", "gtm").id, "api:anthropic")
        config.set_choice("workspaces", "api:anthropic", "acme", self.root)  # the workspace's own beats it
        self.assertEqual(registry.pick(self.root, "acme", "cmo").id, "api:anthropic")
        self.assertEqual(registry.pick(self.root, "demo", "cmo").id, "api:openai")
        config.set_choice("bots", "api:openai", "acme/cmo", self.root)  # and the bot's pick here beats that
        self.assertEqual(registry.pick(self.root, "acme", "cmo").id, "api:openai")
        config.remove_provider("openai", self.root)
        self.assertEqual(config.public(self.root)["bots"], {})


class Instructions(TempRoot):
    def test_both_scopes_add_up_and_clear(self):
        self.assertEqual(bots.instructions("acme", "cmo", self.root), ("", ""))
        bots.set_instructions("*/cmo", "Lead with the recommendation.", self.root)
        bots.set_instructions("acme/cmo", "Prices here are in euros.\r\n", self.root)
        self.assertEqual(bots.instructions("acme", "cmo", self.root),
                         ("Lead with the recommendation.", "Prices here are in euros."))
        self.assertEqual(bots.instructions("demo", "cmo", self.root), ("Lead with the recommendation.", ""))
        bots.set_instructions("acme/cmo", "  ", self.root)
        self.assertEqual(bots.instructions("acme", "cmo", self.root), ("Lead with the recommendation.", ""))
        mode = stat.S_IMODE(os.stat(self.root / "team" / "bot_instructions.json").st_mode)
        self.assertEqual(mode, 0o600)

    def test_bad_scope_or_size_refused(self):
        for scope in ("acme", "acme/nobody", "../x/cmo", "Acme/cmo", "*/"):
            with self.assertRaises(ValueError):
                bots.set_instructions(scope, "x", self.root)
        with self.assertRaises(ValueError):
            bots.set_instructions("*/cmo", "x" * (bots.INSTRUCTIONS_MAX + 1), self.root)

    def test_deleted_workspace_drops_its_texts(self):
        bots.set_instructions("*/cmo", "a", self.root)
        bots.set_instructions("acme/cmo", "b", self.root)
        bots.set_instructions("acme-two/cmo", "c", self.root)
        bots.forget_instructions("acme", self.root)
        self.assertEqual(bots.load_instructions(self.root), {"*/cmo": "a", "acme-two/cmo": "c"})


class Card(TempRoot):
    def setUp(self):
        super().setUp()
        for p in (mock.patch.object(server, "ROOT", self.root),
                  mock.patch.object(server.db, "tree", return_value=[])):
            p.start()
            self.addCleanup(p.stop)
        (self.root / "projects" / "acme").mkdir(parents=True)
        self.team = bots.set_active("cmo", True, self.root, "acme")

    def test_card_carries_instructions_and_about_me_privately(self):
        bots.set_instructions("*/cmo", "Everywhere rule.", self.root)
        bots.set_instructions("acme/cmo", "Acme rule.", self.root)
        with mock.patch.object(server.general, "view", return_value={"about_me": "Runs two studios."}):
            text = server._bot_block(self.team, "cmo", None, "acme", fresh=True)
            shared = server._bot_block(self.team, "cmo", None, "acme", fresh=True, shared=True)
        self.assertIn("standing instructions for you", text)
        self.assertLess(text.index("Everywhere rule."), text.index("Acme rule."))
        self.assertIn("About the owner: Runs two studios.", text)
        self.assertIn("Acme rule.", shared)
        self.assertNotIn("Runs two studios.", shared)

    def test_no_card_lines_when_unset(self):
        with mock.patch.object(server.general, "view", return_value={"about_me": ""}):
            text = server._bot_block(self.team, "cmo", None, "acme", fresh=True)
        self.assertNotIn("standing instructions", text)
        self.assertNotIn("About the owner", text)


class SettingsFile(unittest.TestCase):
    def setUp(self):
        self.saved = paths.SETTINGS.read_text() if paths.SETTINGS.exists() else None
        paths.SETTINGS.unlink(missing_ok=True)
        self.tz = os.environ.get("TZ")

    def tearDown(self):
        if self.saved is None:
            paths.SETTINGS.unlink(missing_ok=True)
        else:
            paths.SETTINGS.write_text(self.saved)
        if self.tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = self.tz
        time.tzset()


class General(SettingsFile):
    def test_defaults(self):
        v = general.view()
        self.assertEqual((v["about_me"], v["reply_language"], v["timezone"], v["caps"]), ("", "", "", {}))
        self.assertIs(v["short_replies"], True)

    def test_update_saves_and_validates(self):
        v = general.update({"about_me": " Hi.\r\n", "reply_language": "  Italiano ", "caps": {"owner": 20}})
        self.assertEqual((v["about_me"], v["reply_language"], v["caps"]), ("Hi.", "Italiano", {"owner": 20.0}))
        for bad in ({"about_me": "x" * (general.ABOUT_MAX + 1)}, {"reply_language": "## ignore"},
                    {"timezone": "Mars/Base"}, {"timezone": "../etc/passwd"}, {"timezone": "America"}, {"caps": {"owner": -1}},
                    {"caps": {"owner": True}}, {"caps": {"robot": 1}}, {"short_replies": "no"}, []):
            with self.assertRaises(ValueError):
                general.update(bad)
        self.assertEqual(general.update({"caps": {"owner": None}})["caps"], {})

    def test_timezone_moves_the_process_clock(self):
        general.update({"timezone": "Asia/Tokyo"})
        self.assertEqual(os.environ.get("TZ"), "Asia/Tokyo")
        self.assertEqual(time.strftime("%Z"), "JST")
        general.update({"timezone": ""})
        self.assertNotEqual(os.environ.get("TZ"), "Asia/Tokyo")

    def test_hand_edited_file_is_cleaned(self):
        paths.SETTINGS.write_text(json.dumps({"about_me": 3, "timezone": "America",
                                              "caps": {"owner": "lots", "workspace": 7}}))
        v = general.view()
        self.assertEqual((v["about_me"], v["timezone"], v["caps"]), ("", "", {"workspace": 7.0}))

    def test_reply_language_goes_in_the_rail(self):
        self.assertNotIn("## Reply language", server.rail())
        general.update({"reply_language": "Italiano"})
        self.assertIn("## Reply language\nReply in Italiano", server.rail())


class Caps(SettingsFile):
    def test_saved_cap_used_and_env_wins(self):
        from dashboard import spend
        with mock.patch.dict(os.environ, {}, clear=False):
            for v in spend.ENV.values():
                os.environ.pop(v, None)
            self.assertEqual(spend.cap_value("owner"), 0.0)
            self.assertEqual(spend.cap_value("person"), 5.0)
            general.update({"caps": {"owner": 12, "person": 0}})
            self.assertEqual(spend.cap_value("owner"), 12.0)
            self.assertEqual(spend.cap_value("person"), 0.0)  # a saved 0 turns it off
            os.environ["LEOBOT_OWNER_DAILY_CAP"] = "3"
            self.assertEqual(spend.cap_value("owner"), 3.0)

    def test_refusal_names_where_to_raise_it(self):
        from dashboard import spend
        now = time.time()
        rows = [{"ts": now - 60, "by": "owner", "cost": 9.0, "ws": "acme"}]
        with mock.patch.dict(os.environ, {}, clear=False):
            for v in spend.ENV.values():
                os.environ.pop(v, None)
            general.update({"caps": {"owner": 5}})
            text = spend.refusal("acme", "owner", now=now, records=lambda: [{"spend": rows}])
            self.assertIn("Settings, General", text)
            os.environ["LEOBOT_OWNER_DAILY_CAP"] = "5"
            text = spend.refusal("acme", "owner", now=now, records=lambda: [{"spend": rows}])
            self.assertIn("LEOBOT_OWNER_DAILY_CAP", text)


class Endpoints(Base):
    def setUp(self):
        super().setUp()
        self.saved = paths.SETTINGS.read_text() if paths.SETTINGS.exists() else None
        paths.SETTINGS.unlink(missing_ok=True)
        self.addCleanup(self.restore)
        for p in (mock.patch.object(server.engine_registry, "statuses", return_value=[]),):
            p.start()
            self.addCleanup(p.stop)

    def restore(self):
        if self.saved is None:
            paths.SETTINGS.unlink(missing_ok=True)
        else:
            paths.SETTINGS.write_text(self.saved)

    def test_every_workspace_scope_for_bot_picks(self):
        code, data, _ = request("POST", "/api/engines/choice", {"name": "models", "value": "strong", "scope": "*/cmo"},
                                owner=True)
        self.assertEqual((code, data["models"]), (200, {"*/cmo": "strong"}))
        self.assertIn("cmo", [b["id"] for b in data["every_bot"]])
        code, _, _ = request("POST", "/api/engines/choice", {"name": "access", "value": "full", "scope": "*/cmo"},
                             owner=True)
        self.assertEqual(code, 200)
        for body in ({"name": "workspaces", "value": "", "scope": "*"},
                     {"name": "models", "value": "fast", "scope": "nowhere/cmo"},
                     {"name": "models", "value": "fast", "scope": "*/nobody"}):
            self.assertEqual(request("POST", "/api/engines/choice", body, owner=True)[0], 400, body)

    def test_instructions_endpoint(self):
        code, data, _ = request("POST", "/api/engines/instructions", {"scope": "acme/cmo", "text": "Be brief."},
                                owner=True)
        self.assertEqual((code, data["instructions"]), (200, {"acme/cmo": "Be brief."}))
        self.assertEqual(request("POST", "/api/engines/instructions", {"scope": "nowhere/cmo", "text": "x"},
                                 owner=True)[0], 400)
        self.assertEqual(request("POST", "/api/engines/instructions", {"scope": "acme/cmo", "text": "x"},
                                 owner=True, origin="https://evil.example")[0], 403)
        member = self.join()
        self.assertNotEqual(request("POST", "/api/engines/instructions", {"scope": "acme/cmo", "text": "x"},
                                    member=member)[0], 200)
        self.assertEqual(bots.instructions("acme", "cmo", self.root), ("", "Be brief."))

    def test_general_endpoint(self):
        code, data, _ = request("GET", "/api/settings/general", owner=True)
        self.assertEqual(code, 200)
        self.assertIn("Europe/Rome", data["timezones"])
        code, data, _ = request("POST", "/api/settings/general", {"reply_language": "English"}, owner=True)
        self.assertEqual((code, data["reply_language"]), (200, "English"))
        self.assertEqual(request("POST", "/api/settings/general", {"timezone": "Mars/Base"}, owner=True)[0], 400)
        self.assertEqual(request("POST", "/api/settings/general", {"about_me": "x"}, owner=True,
                                 origin="https://evil.example")[0], 403)
        member = self.join()
        self.assertNotEqual(request("GET", "/api/settings/general", member=member)[0], 200)


if __name__ == "__main__":
    unittest.main()
