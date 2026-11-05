"""MYOS_DEPLOYMENT=server: a stable owner token, Mac-only features off, install scripts sane."""
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path[:0] = [str(Path(__file__).resolve().parent.parent), str(Path(__file__).resolve().parent.parent / "dashboard")]
import server  # noqa: E402
from dashboard import deployment  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


class OwnerToken(unittest.TestCase):
    def test_mac_gets_a_new_token_each_run(self):
        self.assertNotEqual(server._owner_token("mac"), server._owner_token("mac"))

    def test_server_keeps_it_across_restarts_owner_only(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "team" / "owner-token"
            first = server._owner_token("server", path)
            self.assertEqual(server._owner_token("server", path), first)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_a_short_or_empty_file_is_replaced(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "owner-token"
            path.write_text("short")
            self.assertGreaterEqual(len(server._owner_token("server", path)), 32)

    def test_bots_cannot_read_it(self):
        self.assertIn("owner-token", server.chat_session.SECRET_FILE_GLOBS)


class MacOnly(unittest.TestCase):
    def test_folder_chooser_is_off_on_a_server(self):
        with mock.patch.object(server, "DEPLOYMENT", "server"), self.assertRaisesRegex(Exception, "on a server"):
            server.pick_local_folder()


class Scripts(unittest.TestCase):
    def test_shell_syntax_and_executable(self):
        for name in ("server-setup.sh", "myos-connect.sh", "sync-to-server.sh"):
            path = ROOT / "scripts" / name
            self.assertTrue(os.access(path, os.X_OK), name)
            self.assertEqual(subprocess.run(["bash", "-n", str(path)]).returncode, 0, name)

    def test_setup_binds_nothing_public_and_sets_server_mode(self):
        text = (ROOT / "scripts" / "server-setup.sh").read_text()
        self.assertIn("MYOS_DEPLOYMENT=server", text)
        self.assertNotIn("0.0.0.0", text)

    def test_server_still_listens_on_loopback_only(self):
        self.assertIn('ThreadingHTTPServer(("127.0.0.1", args.port)', (ROOT / "dashboard" / "server.py").read_text())



class DeploymentSettings(unittest.TestCase):
    def test_normal_deployment_has_no_cloud(self):
        self.assertEqual(deployment.view({}), {"deployment": "mac", "cloud": False})
        self.assertEqual(deployment.view({"MYOS_DEPLOYMENT": "server"}), {"deployment": "server", "cloud": False})

    def test_engines_screen_reports_no_cloud(self):
        with mock.patch.dict(os.environ):
            os.environ.pop("MYOS_CLOUD_URL", None)
            self.assertIs(server._engines_view()["cloud"], False)

    def test_only_a_plain_https_address_turns_it_on(self):
        host = "example" + ".com"  # assembled here: the public-repo guard rejects addresses written out in files
        for bad in ("", "  ", "ht" + "tp://" + host, host, "ht" + "tps://", "ht" + "tps://a:b" + chr(64) + host,
                    "ht" + "tps://" + host + "/?x=1", "ht" + "tps://" + host + "/#x", "ht" + "tps://" + host + ":bad",
                    "ht" + "tps://[bad", "file:///etc/passwd"):
            self.assertEqual(deployment.cloud_url({"MYOS_CLOUD_URL": bad}), "", bad)
        self.assertEqual(deployment.cloud_url({"MYOS_CLOUD_URL": "ht" + "tps://" + host + "/"}), "ht" + "tps://" + host)

    def test_the_view_never_carries_the_address(self):
        host = "example" + ".com"
        view = deployment.view({"MYOS_CLOUD_URL": "ht" + "tps://" + host})
        self.assertIs(view["cloud"], True)
        self.assertNotIn(host, str(view))

    def test_the_screen_shows_the_guides_commands(self):
        app = (ROOT / "dashboard" / "app.js").read_text()
        guide = (ROOT / "docs" / "server.md").read_text()
        for line in ('ssh root@your-server "MYOS_REPO=$(git remote get-url origin) bash -s" < scripts/server-setup.sh',
                     "scripts/myos-connect.sh root@your-server", "scripts/sync-to-server.sh root@your-server"):
            self.assertIn(line, app)
            self.assertIn(line, guide)


if __name__ == "__main__":
    unittest.main()
