"""Settings holds the setup screens, and the per-bot engine picker lists the real team."""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from dashboard import server

APP_JS = (Path(server.HERE) / "app.js").read_text()


class BotPickerRoster(unittest.TestCase):
    def view(self):
        spaces = [{"id": "hq", "name": "HQ", "kind": "hq"}, {"id": "acme", "name": "Acme", "kind": "venture"}]
        tree = [{"slug": "acme", "kind": "venture", "profiles": []}]
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "projects" / "acme").mkdir(parents=True)
            with mock.patch.object(server, "ROOT", Path(tmp)), mock.patch.object(server.db, "tree", return_value=tree), \
                    mock.patch.object(server, "_workspaces", return_value=spaces), \
                    mock.patch.object(server.engine_registry, "statuses", return_value=[]):
                return {g["ws"]: [b["id"] for b in g["bots"]] for g in server._engines_view()["bot_list"]}

    def test_hq_lists_only_chief_of_staff(self):
        self.assertEqual(self.view()["hq"], ["cos"])

    def test_workspace_lists_only_bots_switched_on(self):
        listed = self.view()["acme"]
        self.assertIn("cmo", listed)
        self.assertNotIn("cto", listed)  # off until a folder is linked
        self.assertNotIn("social", listed)  # off until the workspace has profiles


class SettingsScreen(unittest.TestCase):
    def test_rail_has_settings_and_not_the_screens_it_holds(self):
        self.assertIn('data-view="settings"', APP_JS)
        for view in ("integrations", "engines", "folders"):
            self.assertNotIn(f'data-view="{view}"', APP_JS)

    def test_each_tab_has_a_route_and_old_links_still_land(self):
        self.assertIn("/^\\/settings(?:\\/(engines|integrations|folders))?$/", APP_JS)
        self.assertIn("/^\\/(engines|integrations|folders)$/", APP_JS)

    def test_settings_opens_from_any_workspace(self):
        self.assertNotIn("Engines live in HQ", APP_JS)
        self.assertNotIn("Integrations live in HQ", APP_JS)

    def test_invited_people_stay_out(self):
        self.assertNotIn("settings", APP_JS.split("const PERSON_ROUTE =", 1)[1].split("\n", 1)[0])

    def test_hq_bots_do_not_promise_a_workspace_engine(self):
        # HQ has no workspace-level pick: its bots fall back to your messages.
        self.assertIn('g.ws === "hq" ? "Same as your messages" : "Same as its workspace"', APP_JS)


if __name__ == "__main__":
    unittest.main()
