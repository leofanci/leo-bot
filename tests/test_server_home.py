"""The server keeps token, pid and log in the home folder; bots never read them."""
import io
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from core import folders, paths, version
from dashboard import server
from dashboard.engines import claude_code


class HomeFilesTest(unittest.TestCase):
    def setUp(self):
        paths.ensure_dirs()
        self.addCleanup(lambda: paths.PID_FILE.unlink(missing_ok=True))

    def test_token_file_in_run(self):
        self.assertEqual(server.TOKEN_FILE, paths.RUN / "auth-token")
        self.assertEqual(server.OWNER_TOKEN_FILE, paths.DATA / "team" / "owner-token")
        self.addCleanup(lambda: paths.TOKEN_FILE.unlink(missing_ok=True))
        server.write_token_file()
        self.assertEqual(paths.TOKEN_FILE.stat().st_mode & 0o777, 0o600)

    def test_second_start_refused(self):
        paths.PID_FILE.write_text(json.dumps({"pid": os.getpid(), "port": 8765}))
        with mock.patch.object(server, "_server_answers", return_value=True):
            with self.assertRaises(SystemExit) as cm:
                server.acquire_pid_lock(8765)
        self.assertIn("already running", str(cm.exception))

    def test_live_pid_without_proof_is_replaced(self):
        paths.PID_FILE.write_text(json.dumps({"pid": os.getpid(), "port": 8765}))
        with mock.patch.object(server, "_server_answers", return_value=False):
            server.acquire_pid_lock(8766)
        self.assertEqual(json.loads(paths.PID_FILE.read_text())["port"], 8766)

    def test_stale_pid_replaced(self):
        paths.PID_FILE.write_text(json.dumps({"pid": 999999, "port": 8765}))
        server.acquire_pid_lock(8765)
        got = json.loads(paths.PID_FILE.read_text())
        self.assertEqual(got, {"pid": os.getpid(), "port": 8765})
        self.assertEqual(paths.PID_FILE.stat().st_mode & 0o777, 0o600)

    def test_garbage_pid_file_replaced(self):
        paths.PID_FILE.write_text("not json")
        server.acquire_pid_lock(8765)
        self.assertEqual(json.loads(paths.PID_FILE.read_text())["pid"], os.getpid())

    def test_release_removes_only_own_lock(self):
        server.acquire_pid_lock(8765)
        server.release_pid_lock()
        self.assertFalse(paths.PID_FILE.exists())
        paths.PID_FILE.write_text(json.dumps({"pid": 999999, "port": 1}))
        server.release_pid_lock()
        self.assertTrue(paths.PID_FILE.exists())


class DenyListsTest(unittest.TestCase):
    def test_deny_dirs_cover_home_parts(self):
        dirs = server._workspace_deny_dirs("acme", [{"slug": "acme"}, {"slug": "demo"}])
        for want in (paths.RUN, paths.LOGS, paths.BACKUPS, paths.DATA / "chats", paths.DATA / "team",
                     paths.CODE / "private", paths.CODE / ".tokensave"):
            self.assertIn(str(want), dirs)

    def test_deny_files_cover_db_and_settings(self):
        files = server._workspace_deny_files("acme")
        self.assertIn(str(server.db.DB_PATH), files)
        self.assertIn(str(paths.SETTINGS), files)
        self.assertIn(str(paths.CODE / "dashboard" / "os.db"), files)

    def test_private_read_rules_cover_logs_and_run(self):
        rules = claude_code.private_read_rules(paths.CODE)
        for d in (paths.LOGS, paths.RUN, paths.BACKUPS):
            self.assertIn(f"Read(/{d}/**)", rules)
        self.assertIn(f"Read(/{paths.SETTINGS})", rules)
        # legacy clone: the old log name stays denied
        self.assertIn(f"Read(/{paths.CODE.resolve() / 'dashboard' / 'server.log'})", rules)

    def test_hq_turn_denies_them_too(self):
        cmd = server.chat_session.ChatSession(repo_dir=str(paths.CODE), rail="r")._base_cmd()
        denied = cmd[cmd.index("--disallowedTools") + 1:cmd.index("--strict-mcp-config")]
        self.assertIn(f"Read(/{paths.RUN}/**)", denied)
        self.assertIn("Read(**/auth-token)", denied)
        self.assertIn("Read(**/.auth-token)", denied)


class CannotLinkTest(unittest.TestCase):
    def test_cannot_link_home_or_code(self):
        paths.ensure_dirs()
        sub = paths.DATA / "x"
        sub.mkdir(exist_ok=True)
        for p in (paths.HOME, sub, paths.RUN, paths.CODE):
            with self.subTest(p=str(p)), self.assertRaises(folders.FolderError) as cm:
                folders.normalize_path(str(p))
            self.assertIn("own folders", str(cm.exception))

    def test_parent_of_home_refused(self):
        with self.assertRaises(folders.FolderError):
            folders.normalize_path(str(paths.HOME.parent))


class HealthzTest(unittest.TestCase):
    def get(self, path):
        h = server.Handler.__new__(server.Handler)
        h.client_address = ("127.0.0.1", 12345)
        h.server = SimpleNamespace(server_address=("127.0.0.1", 8765))
        h.headers = {"Host": "127.0.0.1:8765", "Cookie": ""}
        h.path = path
        h.wfile = io.BytesIO()
        sent = {}
        h.send_response = lambda code, *a: sent.__setitem__("code", code)
        h.send_header = lambda k, v: None
        h.end_headers = lambda: None
        h.do_GET()
        return json.loads(h.wfile.getvalue())

    def test_healthz_reports_version(self):
        self.assertEqual(self.get("/healthz?nonce=abc")["version"], version.current())
        self.assertEqual(self.get("/healthz")["version"], version.current())

    def test_version_defaults_when_missing(self):
        with mock.patch.object(paths, "CODE", Path(tempfile.gettempdir()) / "no-such-leobot-dir"):
            self.assertEqual(version.current(), "0.0.0")


if __name__ == "__main__":
    unittest.main()
