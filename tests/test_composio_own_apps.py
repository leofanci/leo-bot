"""Apps with no Composio-managed login connect through the project's own auth config."""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from core import composio, integrations

MCP = "https://backend.composio.dev/tool_router/s/mcp"
APP_JS = (Path(__file__).resolve().parent.parent / "dashboard" / "app.js").read_text()
NO_MANAGED = composio.ComposioError("Composio HTTP 400", status=400, reason=composio.NO_MANAGED_AUTH)


def config(id="ac_mine", slug="tiktok", status="ENABLED", managed=False):
    return {"id": id, "status": status, "is_composio_managed": managed, "toolkit": {"slug": slug}}


class OwnApps(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        integrations.set_key("ak_x", self.root)
        self.calls = []
        self.configs = []
        self.link_results = []

    def fake(self, method, path, body, root):
        self.calls.append((method, path, body))
        if path.startswith("/auth_configs"):
            return {"items": self.configs}
        if path == "/tool_router/session":
            return {"session_id": f"s{len(self.calls)}", "mcp": {"url": MCP}}
        result = self.link_results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    def connect(self, toolkit="tiktok"):
        with mock.patch.object(composio, "_json", side_effect=self.fake):
            return composio.connect_link("ceo", toolkit, self.root)

    def test_no_own_config_says_what_to_set_up(self):
        self.link_results = [NO_MANAGED]
        got = self.connect()
        self.assertIs(got["needs_own_app"], True)
        self.assertEqual(got["redirect_uri"], "https://backend.composio.dev/api/v3/toolkits/auth/callback")
        self.assertEqual(got["auth_configs_page"], "https://dashboard.composio.dev/~/project/auth-configs")
        self.assertNotIn("url", got)

    def test_own_config_is_used_for_a_one_off_session(self):
        self.configs = [config()]
        self.link_results = [NO_MANAGED, {"redirect_url": "https://connect.composio.dev/l/1", "connected_account_id": "ca_new"}]
        got = self.connect()
        self.assertEqual(got, {"url": "https://connect.composio.dev/l/1", "account_id": "ca_new"})
        retry = [c for c in self.calls if c[1] == "/tool_router/session"][-1]
        self.assertEqual(retry[2]["auth_configs"], {"tiktok": "ac_mine"})
        # The shared link session must stay free of one app's config.
        self.assertNotIn("auth_configs", integrations.session("ceo", integrations.LINK_WS, self.root))

    def test_only_this_apps_enabled_custom_config_counts(self):
        self.configs = [config(id="ac_off", status="DISABLED"), config(id="ac_managed", managed=True),
                        config(id="ac_other", slug="threads"), config(id="not-a-config-id")]
        with mock.patch.object(composio, "_json", side_effect=self.fake):
            self.assertIsNone(composio.own_auth_config("tiktok", self.root))

    def test_other_errors_are_not_mistaken_for_setup(self):
        self.link_results = [composio.ComposioError("Composio HTTP 500", status=500, reason="Boom")]
        with self.assertRaises(composio.ComposioError):
            self.connect("gmail")

    def test_workspace_session_falls_back_to_own_config(self):
        self.configs = [config()]
        attempts = []

        def fake(method, path, body, root):
            if path.startswith("/auth_configs"):
                return {"items": self.configs}
            attempts.append(body)
            if len(attempts) == 1:
                raise NO_MANAGED
            return {"session_id": "s9", "mcp": {"url": MCP}}

        with mock.patch.object(composio, "_json", side_effect=fake), \
                mock.patch.object(integrations, "accounts_for", return_value={"tiktok": ["ca_1"]}):
            sess = composio.ensure_session("ceo", "hq", self.root)
        self.assertEqual(sess["session_id"], "s9")
        self.assertNotIn("auth_configs", attempts[0])
        self.assertEqual(attempts[1]["auth_configs"], {"tiktok": "ac_mine"})


class ConnectButton(unittest.TestCase):
    def test_setup_steps_replace_the_error(self):
        self.assertIn("needs_own_app", APP_JS)
        self.assertIn("integ-setup", APP_JS)

    def test_sign_in_link_is_always_visible(self):
        # The Mac window can block a popup opened after a network wait.
        self.assertIn("Open sign-in page", APP_JS)

    def test_waiting_does_not_stick_forever(self):
        self.assertIn("if (++polls > 100) return draw();", APP_JS)


if __name__ == "__main__":
    unittest.main()
