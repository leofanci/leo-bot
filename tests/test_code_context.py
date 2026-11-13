"""`osctl code-context`: a bot reads the code index of the turn's own linked folder on demand."""
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from core import bots, folders
from dashboard import osctl, tokensave_bridge

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "dashboard"))
import os_relay  # noqa: E402  (the server's own module copy, like tests/test_os_relay.py)
from engines import base  # noqa: E402


def run_osctl(*argv):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        osctl.main(list(argv))
    return json.loads(out.getvalue().strip().splitlines()[-1])


class CodeContext(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.app = self.root / "app"
        (self.app / ".tokensave").mkdir(parents=True)
        (self.app / ".tokensave" / "tokensave.db").write_text("")
        folders.save("hq", {"folders": [{"id": "f1", "path": str(self.app), "access": "read"}]}, self.root)
        self.calls = []
        for p in (mock.patch.object(bots, "ROOT", self.root),
                  mock.patch.object(osctl.shutil, "which", return_value="/bin/tokensave"),
                  mock.patch.object(tokensave_bridge, "context",
                                    side_effect=lambda *a, **k: self.calls.append((a, k)) or "auth lives in login.py"),
                  mock.patch.dict(os.environ, {})):
            p.start()
            self.addCleanup(p.stop)
        for k in ("LEOBOT_WORKSPACE", "LEOBOT_SHARED", "LEOBOT_READONLY", "LEOBOT_CHAT", "LEOBOT_JOB",
                  "LEOBOT_JOB_MODE", "LEOBOT_CODE_FOLDER", "LEOBOT_APPS_USED"):
            os.environ.pop(k, None)
        os.environ["LEOBOT_CODE_FOLDER"] = str(self.app)

    def test_prints_the_index_answer(self):
        r = run_osctl("code-context", "--query", "where is auth?")
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["context"], "auth lives in login.py")
        (args, kw), = self.calls
        self.assertEqual(args[1], "where is auth?")
        self.assertEqual(kw["folder"], str(self.app))
        self.assertTrue(kw["include_code"])

    def test_refused_in_a_shared_thread(self):
        os.environ.update(LEOBOT_SHARED="1", LEOBOT_WORKSPACE="acme")
        r = run_osctl("code-context", "--query", "where is auth?")
        self.assertFalse(r["ok"])
        self.assertEqual(self.calls, [])

    def test_refused_without_an_indexed_folder_this_turn(self):
        del os.environ["LEOBOT_CODE_FOLDER"]
        r = run_osctl("code-context", "--query", "where is auth?")
        self.assertIn("no linked folder has a code index", r["error"])
        self.assertEqual(self.calls, [])

    def test_refused_for_a_folder_the_workspace_never_linked(self):
        os.environ["LEOBOT_CODE_FOLDER"] = str(self.root)
        r = run_osctl("code-context", "--query", "where is auth?")
        self.assertIn("no linked folder has a code index", r["error"])
        self.assertEqual(self.calls, [])

    def test_refused_without_tokensave(self):
        with mock.patch.object(osctl.shutil, "which", return_value=None):
            r = run_osctl("code-context", "--query", "where is auth?")
        self.assertIn("tokensave", r["error"])
        self.assertEqual(self.calls, [])

    def test_bridge_error_is_reported(self):
        with mock.patch.object(tokensave_bridge, "context", side_effect=tokensave_bridge.BridgeError("stale")):
            r = run_osctl("code-context", "--query", "where is auth?")
        self.assertFalse(r["ok"])
        self.assertIn("stale", r["error"])

    def test_allow_sets(self):
        for s in (osctl._READ_ONLY_CMDS, osctl._LOCAL_CMDS, osctl._AUTO_ALLOWED):
            self.assertIn("code-context", s)
        self.assertNotIn("code-context", osctl._SHARED_ALLOWED)
        self.assertIn("code-context", osctl._SHARED_BLOCKED)

    def test_turn_folder_reaches_osctl(self):
        env = base.turn_vars(base.TurnPolicy(code_folder="/x/app"))
        self.assertEqual(env["LEOBOT_CODE_FOLDER"], "/x/app")
        self.assertNotIn("LEOBOT_CODE_FOLDER", base.turn_vars(base.TurnPolicy()))
        self.assertIn("LEOBOT_CODE_FOLDER", base.TURN_ENV)
        env = os_relay.env_for({"ws": "acme", "shared": False, "code_folder": "/x/app"})
        self.assertEqual(env["LEOBOT_CODE_FOLDER"], "/x/app")
        env = os_relay.env_for({"ws": "acme", "shared": True, "code_folder": "/x/app"})
        self.assertNotIn("LEOBOT_CODE_FOLDER", env)


if __name__ == "__main__":
    unittest.main()
