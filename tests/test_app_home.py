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

    def test_retry_90s_every_2s(self):
        self.assertIn("90", self.src)
        self.assertIn("2.0", self.src)

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

    def test_ensure_rebuilds_installed_app(self):
        s = read(ROOT / "scripts" / "ensure-app.sh")
        self.assertIn("Applications/LeoBot.app", s)
        self.assertIn("--out", s)
        self.assertIn("LEOBOT_HOME", s)


if __name__ == "__main__":
    unittest.main()
