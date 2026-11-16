"""Pinned browser engine: where the binary lives, its status, and the relay's tool name sets."""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from core import browser_engine as be
from core import paths


def _exe(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n")
    path.chmod(0o755)
    return path


class Resolve(unittest.TestCase):
    def test_resolve_prefers_env_then_data_then_path(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            env_bin = _exe(d / "env" / "ab")
            path_bin = _exe(d / "bin" / "agent-browser")
            data_bin = _exe(d / "data" / "tools" / "agent-browser" / be.VERSION
                            / "node_modules" / ".bin" / "agent-browser")
            with mock.patch.object(paths, "DATA", d / "data"), \
                    mock.patch("shutil.which", return_value=str(path_bin)):
                self.assertEqual(be.resolve({"LEOBOT_AGENT_BROWSER": str(env_bin)}), env_bin)
                self.assertEqual(be.resolve({}), data_bin)
                data_bin.unlink()
                self.assertEqual(be.resolve({}), path_bin)
            with mock.patch.object(paths, "DATA", d / "data"), mock.patch("shutil.which", return_value=None):
                self.assertIsNone(be.resolve({}))
                self.assertIsNone(be.resolve({"LEOBOT_AGENT_BROWSER": str(d / "nope")}))

    def test_status_missing_says_how_to_install(self):
        with mock.patch.object(be, "resolve", return_value=None):
            st = be.status()
        self.assertEqual(st["state"], "missing")
        self.assertIn("install", st["detail"].lower())

    def test_status_ready_names_binary(self):
        with mock.patch.object(be, "resolve", return_value=Path("/x/agent-browser")):
            st = be.status()
        self.assertEqual(st["state"], "ready")
        self.assertIn("/x/agent-browser", st["detail"])


class Argv(unittest.TestCase):
    def test_mcp_argv_core_tools_only(self):
        argv = be.mcp_argv(Path("/x/agent-browser"))
        self.assertEqual(argv, ["/x/agent-browser", "mcp", "--tools", "core"])
        for bad in ("all", "debug", "state", "network"):
            self.assertNotIn(bad, " ".join(argv))


class ToolSets(unittest.TestCase):
    SETS = ("READ_TOOLS", "TYPE_TOOLS", "COMMIT_TOOLS", "NAV_TOOLS")

    def test_tool_sets_disjoint(self):
        seen = {}
        for name in self.SETS:
            s = getattr(be, name)
            self.assertIsInstance(s, frozenset)
            for t in s:
                self.assertNotIn(t, seen, f"{t} in {name} and {seen.get(t)}")
                seen[t] = name

    def test_names_are_engine_tools(self):
        for name in self.SETS:
            for t in getattr(be, name):
                self.assertTrue(t.startswith("agent_browser_"), t)

    def test_dangerous_tools_in_no_set(self):
        every = set().union(*(getattr(be, n) for n in self.SETS))
        for t in ("agent_browser_eval", "agent_browser_close", "agent_browser_tools_profiles",
                  "agent_browser_upload", "agent_browser_download", "agent_browser_pdf"):
            self.assertNotIn(t, every)

    def test_commit_tools_cover_clicks_and_keys(self):
        for t in ("click", "press", "select", "check", "uncheck"):
            self.assertIn("agent_browser_" + t, be.COMMIT_TOOLS)
        for t in ("fill", "type"):
            self.assertIn("agent_browser_" + t, be.TYPE_TOOLS)


class Install(unittest.TestCase):
    def _run(self, side_effect, make_binary=True):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        data = Path(d.name) / "data"
        binary = data / "tools" / "agent-browser" / be.VERSION / "node_modules" / ".bin" / "agent-browser"
        calls = []

        def fake(argv, **kw):
            calls.append(argv)
            if make_binary and argv[0] == "npm":
                _exe(binary)
            if isinstance(side_effect, BaseException):
                raise side_effect
            return side_effect

        with mock.patch.object(paths, "DATA", data), mock.patch("subprocess.run", fake), \
                mock.patch("shutil.which", return_value=None):
            return be.install(), calls

    def test_npm_missing(self):
        st, _ = self._run(FileNotFoundError())
        self.assertEqual(st["state"], "missing")
        self.assertIn("npm not found", st["detail"])

    def test_timeout(self):
        import subprocess
        st, _ = self._run(subprocess.TimeoutExpired("npm", 1))
        self.assertEqual(st["state"], "missing")
        self.assertIn("timed out", st["detail"])

    def test_nonzero_exit(self):
        r = mock.Mock(returncode=1, stderr="boom\n", stdout="")
        st, calls = self._run(r)
        self.assertEqual(st["state"], "missing")
        self.assertIn("boom", st["detail"])
        self.assertEqual(len(calls), 1)

    def test_binary_missing_after_npm(self):
        r = mock.Mock(returncode=0, stderr="", stdout="")
        st, calls = self._run(r, make_binary=False)
        self.assertEqual(st["state"], "missing")
        self.assertIn("engine binary is missing", st["detail"])
        self.assertEqual(len(calls), 1)

    def test_success_runs_npm_then_engine_install(self):
        r = mock.Mock(returncode=0, stderr="", stdout="")
        st, calls = self._run(r)
        self.assertEqual(st["state"], "ready")
        self.assertEqual([c[0] == "npm" for c in calls], [True, False])
        self.assertEqual(calls[1][1:], ["install"])

    def test_engine_install_that_deletes_the_binary_reads_missing(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        data = Path(d.name) / "data"
        binary = data / "tools" / "agent-browser" / be.VERSION / "node_modules" / ".bin" / "agent-browser"
        ok = mock.Mock(returncode=0, stderr="", stdout="")

        def fake(argv, **kw):
            if argv[0] == "npm":
                _exe(binary)
            else:  # `<binary> install` ends fine but leaves no binary behind
                binary.unlink()
            return ok

        with mock.patch.object(paths, "DATA", data), mock.patch("subprocess.run", fake), \
                mock.patch("shutil.which", return_value=None):
            st = be.install()
        self.assertEqual(st["state"], "missing")
        self.assertIn("engine binary is missing", st["detail"])


if __name__ == "__main__":
    unittest.main()
