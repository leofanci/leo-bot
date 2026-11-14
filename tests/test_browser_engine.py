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


if __name__ == "__main__":
    unittest.main()
