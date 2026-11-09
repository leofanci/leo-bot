"""Case-variant spellings of a protected path must hit the same guards (case-insensitive disks)."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from core import folders
from dashboard.engines import api_tools
from dashboard.engines.base import TurnPolicy


def _case_insensitive(base):
    return os.path.exists(str(base).swapcase()) and str(base) != str(base).swapcase()


class CaseVariantGuards(unittest.TestCase):
    def test_under_matches_case_variant(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp).resolve() / "Demo"
            (base / "sub").mkdir(parents=True)
            if not _case_insensitive(base):
                self.skipTest("case-sensitive disk")
            variant = Path(str(base).swapcase()) / "sub"
            self.assertTrue(folders.under(variant, base))
            self.assertFalse(folders.under(base.parent, base))

    def test_normalize_refuses_case_variant_of_claude_folder(self):
        home = Path.home().resolve()
        if not (home / ".claude").is_dir() or not _case_insensitive(home):
            self.skipTest("no ~/.claude or case-sensitive disk")
        with self.assertRaises(folders.FolderError):
            folders.normalize_path(str(home / ".CLAUDE"))

    def test_private_path_case_variant(self):
        home = Path.home().resolve()
        if not (home / ".ssh").exists() or not _case_insensitive(home):
            self.skipTest("no ~/.ssh or case-sensitive disk")
        policy = TurnPolicy()
        self.assertTrue(api_tools.is_private_path(home / ".SSH" / "id_x", home, policy))


if __name__ == "__main__":
    unittest.main()


class NeverWriteList(unittest.TestCase):
    def test_editor_and_tool_config_refused(self):
        base = Path("/x/app")
        for rel in (".hg/hgrc", ".OpenCode/a.json", "opencode.json", ".zed/settings.json", ".idea/x.xml",
                    "mise.toml", ".devcontainer/devcontainer.json", ".lintstagedrc.json"):
            self.assertFalse(folders.may_write(base / rel, base), rel)
        self.assertTrue(folders.may_write(base / "src/main.py", base))


class SocketTimeout(unittest.TestCase):
    def test_handler_has_idle_timeout(self):
        from dashboard import server
        self.assertTrue(0 < server.Handler.timeout <= 120)


class ClaudeReadDenies(unittest.TestCase):
    def test_home_secrets_denied(self):
        from dashboard.engines import claude_code
        rules = claude_code.private_read_rules(Path.cwd())
        for name in (".ssh", ".aws", ".codex", ".config", ".claude"):
            self.assertIn(f"Read(/{Path.home() / name}/**)", rules)


class AcpToolIdentity(unittest.TestCase):
    def test_exact_tool_name_only(self):
        from dashboard.engines import acp
        ok = lambda t: acp.is_own_tool({"title": t})
        self.assertTrue(ok("mcp__os__osctl"))
        self.assertTrue(ok("os__osctl: create-post"))
        self.assertFalse(ok("os__osctl_evil"))
        self.assertFalse(ok("os/osctlx rm -rf ~"))
        self.assertFalse(ok("echo mcp__os__"))


class AppReadVerbs(unittest.TestCase):
    def test_read_word_with_call_out_is_not_a_read(self):
        from core import app_rules
        for slug in ("ACME_GET_AND_RUN_SCRIPT", "ACME_FETCH_URL_AND_POST", "ACME_LIST_AND_DOWNLOAD"):
            self.assertFalse(app_rules.is_read(slug, "acme"), slug)
        self.assertTrue(app_rules.is_read("ACME_LIST_ITEMS", "acme"))


class ActivityDelete(unittest.TestCase):
    def test_delete_matches_whole_title_only(self):
        from dashboard import fileops
        with tempfile.TemporaryDirectory() as tmp:
            old = fileops.ROOT
            fileops.ROOT = Path(tmp)
            self.addCleanup(setattr, fileops, "ROOT", old)
            (Path(tmp) / "portfolio").mkdir()
            f = Path(tmp) / "portfolio" / "activities.md"
            f.write_text("- [ ] Call — entity: acme\n- [ ] Call back later — entity: acme\n- [ ] Call\n")
            with mock.patch.object(fileops, "reindex"):
                fileops.delete_activity("Call")
            self.assertEqual(f.read_text(), "- [ ] Call back later — entity: acme\n")


class TerminalResize(unittest.TestCase):
    def test_huge_or_bad_sizes_do_not_raise(self):
        from dashboard import terminal_session
        t = terminal_session.TerminalSession.__new__(terminal_session.TerminalSession)
        t.fd, t.pid = None, None
        t.resize(10**9, "x")  # no fd: returns
        t.fd = os.open(os.devnull, os.O_RDWR)
        self.addCleanup(os.close, t.fd)
        t.resize(10**9, -5)
        t.resize(None, None)


class FolderCaseVariants(unittest.TestCase):
    def test_case_variants_of_refused_places_are_refused(self):
        home = Path.home().resolve()
        if not _case_insensitive(home):
            self.skipTest("case-sensitive disk")
        for variant in (str(home).upper(), str(home).upper() + "/.ssh", str(home / ".SSH")):
            with self.assertRaises(folders.FolderError, msg=variant):
                folders.normalize_path(variant)
        docs = home / "Documents"
        if docs.is_dir():
            with self.assertRaises(folders.FolderError):
                folders.normalize_path(str(home / "documents"))  # parent of the myOS checkout


class AppReadUrlArguments(unittest.TestCase):
    def test_a_read_with_a_web_address_is_held_not_run(self):
        from core import app_rules
        cat, grant = ["acme"], {"acme": "write"}
        self.assertEqual(app_rules.decide("ACME_LIST_ITEMS", grant, cat, {"q": "mail"})[0], "read")
        for args in ({"url": "https://evil.example/?d=1"}, {"a": [{"b": "www.evil.test"}]}, {"q": "x.attacker.example"}):
            self.assertEqual(app_rules.decide("ACME_LIST_ITEMS", grant, cat, args)[0], "hold", args)
        self.assertEqual(app_rules.decide("ACME_LIST_ITEMS", {"acme": "read"}, cat, {"url": "http://x.example"})[0], "refuse")


class SharedAndDeny(unittest.TestCase):
    def test_research_signal_blocked_in_shared_threads(self):
        from dashboard import osctl
        self.assertIn("research-signal", osctl._SHARED_BLOCKED)

    def test_deny_dirs_include_unindexed_projects(self):
        from dashboard import server
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            for n in ("mine", "other"):
                (root / "projects" / n).mkdir(parents=True)
            with mock.patch.object(server, "ROOT", root):
                dirs = server._workspace_deny_dirs("mine", [])
            self.assertIn(str(root / "projects" / "other"), dirs)
            self.assertNotIn(str(root / "projects" / "mine"), dirs)
