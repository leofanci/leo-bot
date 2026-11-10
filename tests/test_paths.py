import importlib
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from core import paths


class PathsTests(unittest.TestCase):
    def test_default_home(self):
        self.assertEqual(paths.home_for({}), Path.home() / ".leobot")

    def test_env_override(self):
        self.assertEqual(paths.home_for({"LEOBOT_HOME": "~/x"}), Path.home() / "x")

    def test_relative_home_made_absolute(self):
        self.assertEqual(paths.home_for({"LEOBOT_HOME": "rel/home"}), Path(os.getcwd()) / "rel" / "home")
        self.assertTrue(paths.home_for({"LEOBOT_HOME": "x"}).is_absolute())

    def test_guard_refuses_tests_without_a_home(self):
        with self.assertRaises(RuntimeError) as cm:
            paths.refuse_real_home_in_tests({}, {"unittest": object()})
        self.assertIn("-t . -s tests", str(cm.exception))
        with self.assertRaises(RuntimeError):
            paths.refuse_real_home_in_tests({"LEOBOT_HOME": ""}, {"unittest": object()})
        paths.refuse_real_home_in_tests({"LEOBOT_HOME": "/tmp/x"}, {"unittest": object()})
        paths.refuse_real_home_in_tests({}, {})  # not under unittest: the real home is right

    def test_guard_runs_at_import(self):
        with tempfile.TemporaryDirectory() as fake_home:
            env = {k: v for k, v in os.environ.items() if k != "LEOBOT_HOME"}
            env["HOME"] = fake_home  # even if the guard failed, nothing real is touched
            r = subprocess.run([sys.executable, "-c", "import unittest\nfrom core import paths"],
                               cwd=paths.CODE, env=env, capture_output=True, text=True, timeout=60)
            self.assertNotEqual(r.returncode, 0)
            self.assertIn("LEOBOT_HOME is not set", r.stderr)
            self.assertEqual(os.listdir(fake_home), [])
            r = subprocess.run([sys.executable, "-c", "from core import paths; print(paths.HOME)"],
                               cwd=paths.CODE, env=env, capture_output=True, text=True, timeout=60)
            self.assertEqual(r.returncode, 0, r.stderr)

    def test_empty_env_ignored(self):
        self.assertEqual(paths.home_for({"LEOBOT_HOME": ""}), Path.home() / ".leobot")

    def test_ensure_dirs_modes(self):
        global paths
        with tempfile.TemporaryDirectory() as tmp:
            old = os.environ["LEOBOT_HOME"]
            os.environ["LEOBOT_HOME"] = str(Path(tmp) / "h")
            try:
                paths = importlib.reload(paths)
                paths.ensure_dirs()
                for d in (paths.HOME, paths.DATA, paths.RUN, paths.LOGS, paths.BACKUPS):
                    self.assertTrue(d.is_dir())
                    self.assertEqual(d.stat().st_mode & 0o777, 0o700)
            finally:
                os.environ["LEOBOT_HOME"] = old
                paths = importlib.reload(paths)

    def test_tests_never_use_real_home(self):
        self.assertNotEqual(paths.HOME, Path.home() / ".leobot")
        tmp = Path(tempfile.gettempdir()).resolve()
        self.assertTrue(str(paths.HOME.resolve()).startswith(str(tmp)))

    def test_is_installed_false_in_clone(self):
        self.assertFalse(paths.is_installed())


if __name__ == "__main__":
    unittest.main()
