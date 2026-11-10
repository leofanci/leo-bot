"""The server keeps token, pid and log in the home folder; bots never read them."""
import io
import json
import os
import shutil
import subprocess
import sys
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
        self.addCleanup(server.release_pid_lock)
        self.addCleanup(lambda: paths.PID_FILE.unlink(missing_ok=True))

    def test_token_file_in_run(self):
        self.assertEqual(server.TOKEN_FILE, paths.RUN / "auth-token")
        self.assertEqual(server.OWNER_TOKEN_FILE, paths.DATA / "team" / "owner-token")
        self.addCleanup(lambda: paths.TOKEN_FILE.unlink(missing_ok=True))
        server.write_token_file()
        self.assertEqual(paths.TOKEN_FILE.stat().st_mode & 0o777, 0o600)

    def test_second_start_refused(self):
        code = ("import sys,time,json;from dashboard import server;"
                "server.acquire_pid_lock(8765);print('held',flush=True);time.sleep(30)")
        child = subprocess.Popen([sys.executable, "-c", code], cwd=paths.CODE, stdout=subprocess.PIPE,
                                 text=True, env={**os.environ})
        self.addCleanup(child.wait)
        self.addCleanup(child.kill)
        self.assertEqual(child.stdout.readline().strip(), "held")
        with self.assertRaises(SystemExit) as cm:
            server.acquire_pid_lock(8766)
        self.assertIn("already running", str(cm.exception))
        self.assertIn(f"pid {child.pid}", str(cm.exception))
        self.assertIn("port 8765", str(cm.exception))
        self.assertEqual(json.loads(paths.PID_FILE.read_text())["pid"], child.pid)

    def test_stale_pid_replaced(self):
        paths.PID_FILE.write_text(json.dumps({"pid": 999999, "port": 8765}))
        server.acquire_pid_lock(8765)
        got = json.loads(paths.PID_FILE.read_text())
        self.assertEqual(got, {"pid": os.getpid(), "port": 8765})
        self.assertEqual(paths.PID_FILE.stat().st_mode & 0o777, 0o600)

    def test_stale_file_naming_a_live_pid_is_taken_over(self):
        paths.PID_FILE.write_text(json.dumps({"pid": os.getppid(), "port": 8765}))  # live, holds no lock
        server.acquire_pid_lock(8767)
        self.assertEqual(json.loads(paths.PID_FILE.read_text())["port"], 8767)

    def test_garbage_pid_file_replaced(self):
        paths.PID_FILE.write_text("not json")
        server.acquire_pid_lock(8765)
        self.assertEqual(json.loads(paths.PID_FILE.read_text())["pid"], os.getpid())

    def test_release_removes_file_and_frees_lock(self):
        server.acquire_pid_lock(8765)
        server.release_pid_lock()
        self.assertFalse(paths.PID_FILE.exists())
        server.acquire_pid_lock(8766)  # free again
        self.assertEqual(json.loads(paths.PID_FILE.read_text())["port"], 8766)

    def test_token_file_mode_set_before_write(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "t"
            path.write_text("old")
            os.chmod(path, 0o644)
            server.write_token_file(path)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(path.read_text(), server.AUTH_TOKEN)


class DenyListsTest(unittest.TestCase):
    def test_deny_dirs_cover_home_parts(self):
        dirs = server._workspace_deny_dirs("acme", [{"slug": "acme"}, {"slug": "demo"}])
        for want in (paths.RUN, paths.LOGS, paths.BACKUPS, paths.DATA / "chats", paths.DATA / "team",
                     paths.CODE / "private", paths.CODE / ".tokensave"):
            self.assertIn(str(want), dirs)

    def test_deny_dirs_cover_unmigrated_clone_data(self):
        dirs = server._workspace_deny_dirs("acme", [{"slug": "acme"}, {"slug": "demo"}])
        for n in ("chats", "team", "portfolio", "database"):
            self.assertIn(str(paths.CODE / n), dirs)
        self.assertIn(str(paths.CODE / "projects" / "demo"), dirs)
        self.assertNotIn(str(paths.CODE / "projects" / "acme"), dirs)

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

    def leftovers(self):
        """What a restore, a broken one and an interrupted install leave in the home folder, and
        what an old layout leaves at the top of the data folder."""
        paths.ensure_dirs()
        made = [paths.HOME / "data.pre-restore-20260101-000000-000000", paths.HOME / "data.partial-x",
                paths.HOME / ".app-installing", paths.DATA / "dashboard"]
        for d in made:
            d.mkdir(parents=True, exist_ok=True)
            self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        files = [paths.DATA / "os.db", paths.DATA / "dashboard" / "data.db", paths.HOME / "notes.txt"]
        for f in files:
            f.write_text("x")
            self.addCleanup(f.unlink, missing_ok=True)
        (paths.DATA / "projects" / "acme").mkdir(parents=True, exist_ok=True)
        (paths.DATA / "projects" / "demo").mkdir(parents=True, exist_ok=True)
        return made, files

    def test_company_turn_denies_every_home_and_data_entry_but_its_own(self):
        made, files = self.leftovers()
        projects = [{"slug": "acme"}, {"slug": "demo"}]
        dirs = server._workspace_deny_dirs("acme", projects)
        deny_files = server._workspace_deny_files("acme")
        for d in made:
            self.assertIn(str(d), dirs)
        for f in (paths.DATA / "os.db", paths.HOME / "notes.txt"):
            self.assertIn(str(f), deny_files)
        self.assertIn(str(paths.DATA / "projects" / "demo"), dirs)
        for allowed in (paths.DATA / "projects", paths.DATA / "projects" / "acme", paths.HOME / "app",
                        paths.DATA, paths.HOME):
            self.assertNotIn(str(allowed), dirs)
            self.assertNotIn(str(allowed), deny_files)

    def test_hq_turn_denies_home_entries_not_data(self):
        made, _ = self.leftovers()
        dirs = server._workspace_deny_dirs("hq", [{"slug": "acme"}])
        deny_files = server._workspace_deny_files("hq")
        for d in made[:3]:
            self.assertIn(str(d), dirs)
        self.assertIn(str(paths.RUN), dirs)
        self.assertIn(str(paths.SETTINGS), deny_files)
        self.assertIn(str(paths.HOME / "notes.txt"), deny_files)
        self.assertFalse([d for d in dirs if d == str(paths.DATA) or d.startswith(str(paths.DATA) + "/")], dirs)
        self.assertNotIn(str(paths.DATA / "os.db"), deny_files)

    def test_private_read_rules_cover_every_home_entry_but_app_and_data(self):
        made, _ = self.leftovers()
        rules = claude_code.private_read_rules(paths.CODE)
        for d in made[:3]:
            self.assertIn(f"Read(/{d}/**)", rules)
        self.assertIn(f"Read(/{paths.HOME / 'notes.txt'})", rules)
        self.assertNotIn(f"Read(/{paths.DATA}/**)", rules)
        self.assertNotIn(f"Read(/{paths.HOME / 'app'}/**)", rules)

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
