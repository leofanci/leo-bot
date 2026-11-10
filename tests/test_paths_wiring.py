import json
import re
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

from core import bots, paths
from dashboard import fileops

REPO = paths.CODE
# Files allowed to derive a root from __file__ for sys.path setup only.
SYSPATH_ALLOWED = {
    "core/paths.py",
    "dashboard/runner.py",    # inserts the checkout on sys.path before core imports
    "dashboard/osctl.py",     # same
    "dashboard/os_relay.py",  # CODE_DIR is the bot working dir (code)
    "dashboard/server.py",    # Task 3 rewires server.py
}
ROOT_RE = re.compile(r"=\s*Path\(__file__\)\.resolve\(\)\.parent\.parent(\.parent)?\s*$")


class PathsWiringTests(unittest.TestCase):
    def _fresh(self, expr):
        """Evaluate in a new interpreter: other tests override ROOT/DB_PATH in-process."""
        out = subprocess.run([sys.executable, "-c", f"import json;{expr}"], cwd=REPO,
                             capture_output=True, text=True, check=True).stdout
        return json.loads(out)

    def test_data_roots_point_at_data(self):
        got = self._fresh(
            "from core import bots, paths, ws_logos;"
            "from dashboard import db, fileops;"
            "from dashboard.engines import config;"
            "print(json.dumps({'data': str(paths.DATA), 'roots': [str(m.ROOT) for m in "
            "(bots, fileops, config, ws_logos, db)]}))")
        self.assertEqual(got["roots"], [got["data"]] * 5)
        self.assertEqual(got["data"], str(paths.DATA))

    def test_db_path_under_data(self):
        got = self._fresh("from dashboard import db;print(json.dumps(str(db.DB_PATH)))")
        self.assertEqual(got, str(paths.DATA / "database" / "data" / "os.db"))

    def test_bot_defs_from_code_with_empty_data(self):
        self.assertFalse(paths.DATA.joinpath("bots").exists())
        self.assertTrue({"cos", "cmo", "cto", "gtm", "product", "research", "social"}
                        <= set(bots.load_defs()))

    def test_generate_script_from_code(self):
        post = {"post": {"status": "planned"}, "profile_slug": "profile-a"}
        done = subprocess.CompletedProcess([], 0, stdout="ok", stderr="")
        with mock.patch.object(fileops, "find_post", return_value=post), \
                mock.patch.object(fileops.subprocess, "run", return_value=done) as run:
            fileops.generate_brief("post-1")
        argv = run.call_args[0][0]
        self.assertEqual(argv[1], str(paths.CODE / "generate.py"))
        self.assertEqual(argv[argv.index("--workspace") + 1], str(fileops.ROOT))

    def test_other_scripts_from_code(self):
        self.assertEqual(fileops._INDEX_SCRIPT, paths.CODE / "index.py")
        args = fileops._plan_args("profile-a", {"period": "2026-07-01 to 2026-07-14"})
        self.assertEqual(args[1], str(paths.CODE / "generate.py"))

    def test_no_module_defines_its_own_root(self):
        files = [p for pat in ("core/**/*.py", "dashboard/**/*.py", "hooks/*.py")
                 for p in REPO.glob(pat)] + [REPO / "generate.py", REPO / "index.py"]
        bad = []
        for f in files:
            rel = f.relative_to(REPO).as_posix()
            if rel in SYSPATH_ALLOWED:
                continue
            for line in f.read_text(encoding="utf-8").splitlines():
                if ROOT_RE.search(line):
                    bad.append(f"{rel}: {line.strip()}")
        self.assertEqual(bad, [])


if __name__ == "__main__":
    unittest.main()
