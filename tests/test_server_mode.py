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
        for name in ("server-setup.sh", "leobot-connect.sh", "sync-to-server.sh"):
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
    def test_mode_comes_from_the_environment(self):
        self.assertEqual(deployment.mode({}), "mac")
        self.assertEqual(deployment.mode({"MYOS_DEPLOYMENT": "server"}), "server")
        self.assertEqual(deployment.mode({"MYOS_DEPLOYMENT": "other"}), "mac")

    def test_the_server_reads_its_mode_from_one_place(self):
        self.assertIn("DEPLOYMENT = deployment_settings.mode()", (ROOT / "dashboard" / "server.py").read_text())

    def test_the_screen_shows_the_guides_commands(self):
        app = (ROOT / "dashboard" / "app.js").read_text()
        guide = (ROOT / "docs" / "server.md").read_text()
        for line in ('ssh root@your-server "MYOS_REPO=$(git remote get-url origin) bash -s" < scripts/server-setup.sh',
                     "scripts/leobot-connect.sh root@your-server", "scripts/sync-to-server.sh root@your-server"):
            self.assertIn(line, app)
            self.assertIn(line, guide)


class PrivateFolder(unittest.TestCase):
    def test_git_ignores_it(self):
        check = subprocess.run(["git", "check-ignore", "-q", "private/notes.md"], cwd=ROOT)
        self.assertEqual(check.returncode, 0)

    def test_company_bots_cant_read_it(self):
        self.assertIn(str(ROOT / "private"), server._workspace_deny_dirs("acme", []))
        for source in ("dashboard/osctl.py", "dashboard/engines/api_tools.py"):
            self.assertIn('"private/"', (ROOT / source).read_text(), source)


if __name__ == "__main__":
    unittest.main()
