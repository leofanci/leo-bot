import importlib
import os
import tempfile
import unittest
from pathlib import Path

from core import paths


class PathsTests(unittest.TestCase):
    def test_default_home(self):
        self.assertEqual(paths.home_for({}), Path.home() / ".leobot")

    def test_env_override(self):
        self.assertEqual(paths.home_for({"LEOBOT_HOME": "~/x"}), Path.home() / "x")

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
