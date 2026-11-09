"""Settings is machine-wide; what belongs to one project stays in that project."""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from dashboard import server

APP_JS = (Path(server.HERE) / "app.js").read_text()
APP_CSS = (Path(server.HERE) / "app.css").read_text()


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
    def test_gear_lives_at_the_bottom_of_the_workspace_bar(self):
        bar = APP_JS.split("function renderWsBar(){", 1)[1].split("async function switchWorkspace", 1)[0]
        self.assertIn('id="ws-settings"', bar)
        self.assertNotIn('data-view="settings"', APP_JS)
        self.assertIn(".ws-settings{margin-top:auto", APP_CSS)

    def test_tabs_hold_only_machine_wide_things(self):
        self.assertIn("/^\\/settings(?:\\/(engines|bots|integrations|access))?$/", APP_JS)
        self.assertIn('["engines", "Engines"], ["bots", "Bots"], ["integrations", "Integrations"], ["access", "Access"]', APP_JS)

    def test_old_links_still_land(self):
        self.assertIn("/^\\/(engines|integrations)$/", APP_JS)
        self.assertIn("/^\\/settings\\/folders$/", APP_JS)

    def test_settings_is_not_inside_a_workspace(self):
        crumbs = APP_JS.split("function canvasCrumbs(){", 1)[1].split("\n}", 1)[0]
        self.assertIn('if (/^\\/settings\\b/.test(path)) return [];', crumbs)
        self.assertIn('classList.toggle("settings-open"', APP_JS.split("function parseRoute(hash){", 1)[1].split("\n}", 1)[0])
        self.assertIn(".app.settings-open #rail", APP_CSS)

    def test_linked_folders_are_per_project(self):
        self.assertIn('data-view="folders"', APP_JS)
        self.assertIn("/^\\/folders$/", APP_JS)
        self.assertNotIn('["folders", "Folders"]', APP_JS)

    def test_sharing_address_moved_out_of_team(self):
        team = APP_JS.split("async function renderTeam(){", 1)[1].split("\n}\n", 1)[0]
        self.assertNotIn("sharingSection", team)
        self.assertIn("sharingSection", APP_JS.split("async function renderAccess(){", 1)[1].split("\n}\n", 1)[0])
        self.assertIn("Settings, Access", APP_JS)

    def test_invited_people_stay_out(self):
        self.assertNotIn("settings", APP_JS.split("const PERSON_ROUTE =", 1)[1].split("\n", 1)[0])
        self.assertIn('isPerson() ? "" : `<button class="ws-btn ws-settings"', APP_JS)

    def test_no_message_points_at_a_screen_that_is_gone(self):
        # Engines, Integrations and sharing left HQ for Settings: errors must say where they are now.
        import re
        gone = re.compile(r"HQ, (?:Engines|Integrations|Team|Folders)|HQ · settings")
        base = Path(server.HERE).parent
        files = [*base.joinpath("dashboard").rglob("*.py"), *base.joinpath("dashboard").glob("*.js"),
                 *base.joinpath("dashboard").glob("*.html"), *base.joinpath("dashboard").glob("*.css"),
                 *base.joinpath("core").rglob("*.py"), *base.joinpath("docs").glob("*.md"), base / "README.md"]
        stale = [str(f.relative_to(base)) for f in files if gone.search(f.read_text(errors="replace"))]
        self.assertEqual(stale, [])

    def test_hq_total_says_it_covers_every_workspace(self):
        # HQ's spend adds up all workspaces, so it can exceed what HQ's own bots show.
        self.assertIn('_WS === "hq" ? "All workspaces, last" : "Last"', APP_JS)

    def test_hq_bots_do_not_promise_a_workspace_engine(self):
        # HQ has no workspace-level pick: its bots fall back to your messages.
        self.assertIn('g.ws === "hq" ? "Same as your messages" : "Same as its workspace"', APP_JS)


if __name__ == "__main__":
    unittest.main()
