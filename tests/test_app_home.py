"""LeoBot.app finds code and token in the home folder (static checks on the app sources)."""
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RES = ROOT / "LeoBot.app" / "Contents" / "Resources"


def read(p):
    return Path(p).read_text(encoding="utf-8")


class WebviewSwift(unittest.TestCase):
    def setUp(self):
        self.src = read(RES / "webview.swift")

    def test_home_layout(self):
        self.assertIn('".leobot/app"', self.src)
        self.assertIn("run/auth-token", self.src)
        self.assertIn("LEOBOT_HOME", self.src)
        self.assertNotIn("dashboard/.auth-token", self.src)
        self.assertNotIn("dashboard/server.log", self.src)

    def test_dev_clone_check(self):
        self.assertIn("dashboard/server.py", self.src)

    def test_failure_alert_names_log(self):
        self.assertIn("logs/server.log", self.src)

    def test_retry_constants(self):
        self.assertRegex(self.src, r"let retryInterval: TimeInterval = 2\.0")
        self.assertRegex(self.src, r"let startGrace: TimeInterval = 30\.0")
        self.assertRegex(self.src, r"let totalWait: TimeInterval = 90\.0")
        self.assertIn("elapsed >= totalWait", self.src)
        self.assertIn("Thread.sleep(forTimeInterval: retryInterval)", self.src)

    def test_start_gated_by_grace_and_update_lock(self):
        self.assertIn("!started && elapsed >= startAfter && !isUpdating()", self.src)
        self.assertIn("run/update.lock", self.src)
        self.assertIn("LOCK_SH | LOCK_NB", self.src)
        # watch() must not start the server directly
        watch = self.src[self.src.index("private func watch"):]
        watch = watch[:watch.index("private func readToken") if "private func readToken" in watch else len(watch)]
        self.assertNotIn("runStartScript(", watch.split("waitUp(")[0])

    def test_server_pid_reset_and_checked(self):
        run = self.src[self.src.index("private func runStartScript"):]
        self.assertIn("serverPID = 0", run[:120])
        self.assertIn("isLeoBotServer(serverPID)", self.src)
        self.assertIn('"dashboard/server.py"', self.src.split("func isLeoBotServer")[1])

    def test_home_file_fallback(self):
        self.assertIn('"leobot-home"', self.src)

    def test_token_file_checks(self):
        self.assertIn("lstat(file", self.src)
        self.assertIn("st.st_uid != getuid()", self.src)
        self.assertIn("st.st_mode & 0o077", self.src)

    def test_passes_home_to_script(self):
        self.assertIn("environment", self.src)


class StartServer(unittest.TestCase):
    def setUp(self):
        self.src = read(RES / "start-server.sh")

    def test_paths(self):
        self.assertIn("run/auth-token", self.src)
        self.assertIn("logs/server.log", self.src)
        self.assertIn("LEOBOT_HOME", self.src)
        self.assertNotIn("dashboard/.auth-token", self.src)
        self.assertNotIn("dashboard/server.log", self.src)

    def test_log_appended_not_truncated(self):
        self.assertNotIn(': > "$LOGFILE"', self.src)
        self.assertIn('>> "$LOGFILE"', self.src)

    def test_stale_match_kept_and_narrowed(self):
        self.assertIn("dashboard/server.py", self.src)
        self.assertIn("server.pid", self.src)


class Scripts(unittest.TestCase):
    def test_build_accepts_out(self):
        self.assertIn("--out", read(ROOT / "scripts" / "build-app.sh"))

    def test_build_records_custom_home(self):
        b = read(ROOT / "scripts" / "build-app.sh")
        self.assertIn("leobot-home", b)
        self.assertIn("LEOBOT_HOME", b)

    def test_install_exports_home_and_builds(self):
        i = read(ROOT / "install.sh")
        # the build runs in a function called after main() has exported the home
        self.assertIn('export LEOBOT_HOME="$HOME_DIR"', i)
        self.assertIn("/scripts/build-app.sh\" --out", i)

    def test_ensure_rebuilds_installed_app(self):
        s = read(ROOT / "scripts" / "ensure-app.sh")
        self.assertIn("Applications/LeoBot.app", s)
        self.assertIn("--out", s)
        self.assertIn("LEOBOT_HOME", s)


if __name__ == "__main__":
    unittest.main()
