"""hooks/reindex.py: an edit to an indexed source file in the data folder re-indexes it."""
import importlib.util
import io
import json
import unittest
from pathlib import Path
from unittest import mock

from core import paths

HOOK = paths.CODE / "hooks" / "reindex.py"


def load_hook():
    spec = importlib.util.spec_from_file_location("leobot_reindex_hook", HOOK)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class ReindexHookTest(unittest.TestCase):
    def setUp(self):
        self.hook = load_hook()

    def fire(self, file_path):
        event = io.StringIO(json.dumps({"tool_input": {"file_path": str(file_path)}}))
        ok = mock.Mock(returncode=0, stdout="", stderr="")
        with mock.patch.object(self.hook.sys, "stdin", event), \
                mock.patch.object(self.hook.subprocess, "run", return_value=ok) as run, \
                mock.patch("builtins.print"):
            self.hook.main()
        return run

    def test_watches_what_index_reads(self):
        self.assertEqual(self.hook.DATA_DIRS, {"projects", "portfolio"})
        self.assertEqual(self.hook.ROOT, paths.DATA)

    def test_project_and_portfolio_edits_reindex_the_data_folder(self):
        for rel in ("projects/demo/strategy/intake.md", "projects/demo/profiles/profile-a/profile.md",
                    "portfolio/activities.md", "portfolio/milestones.json"):
            with self.subTest(rel):
                run = self.fire(paths.DATA / rel)
                run.assert_called_once()
                argv = run.call_args.args[0]
                self.assertEqual(argv[1:], [str(paths.CODE / "index.py"), str(paths.DATA)])

    def test_other_edits_do_nothing(self):
        for f in (paths.CODE / "dashboard" / "server.py", paths.DATA / "chats" / "x.json",
                  paths.DATA / "projects" / "demo" / "logo.png", Path("/elsewhere/projects/a.md")):
            with self.subTest(str(f)):
                self.fire(f).assert_not_called()


if __name__ == "__main__":
    unittest.main()
