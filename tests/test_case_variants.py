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
        for slug in ("ACME_GET_AND_EXECUTE_SCRIPT", "ACME_FETCH_URL_AND_PUBLISH", "ACME_LIST_AND_SUBMIT"):
            self.assertFalse(app_rules.is_read(slug, "acme"), slug)
        for slug in ("ACME_SEARCH_AND_POST_W", "ACME_CHECK_AND_APPROVE_X", "ACME_GET_AND_EXPORT_Z",
                     "ACME_FIND_AND_ASSIGN_Y", "ACME_LIST_AND_CLOSE_V"):
            self.assertFalse(app_rules.is_read(slug, "acme"), slug)
        for slug in ("ACME_LIST_ITEMS", "ACME_GET_A_PULL_REQUEST", "ACME_GET_POST", "ACME_LIST_SCHEDULE",
                     "ACME_FETCH_POST_INSIGHTS", "ACME_GET_START_DATE", "ACME_GET_CALL"):
            self.assertTrue(app_rules.is_read(slug, "acme"), slug)


class ActivityDelete(unittest.TestCase):
    def setUp(self):
        from dashboard import fileops
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.fileops, self.f = fileops, Path(tmp.name) / "portfolio" / "activities.md"
        self.f.parent.mkdir()
        for p in (mock.patch.object(fileops, "ROOT", Path(tmp.name)), mock.patch.object(fileops, "reindex")):
            p.start()
            self.addCleanup(p.stop)

    def test_delete_matches_whole_title_only(self):
        self.f.write_text("- [ ] Call — entity: acme\n- [ ] Call back later — entity: acme\n")
        self.fileops.delete_activity("Call")
        self.assertEqual(self.f.read_text(), "- [ ] Call back later — entity: acme\n")

    def test_same_title_in_two_workspaces_needs_the_workspace(self):
        self.f.write_text("- [ ] Weekly review — entity: acme\n- [ ] Weekly review — entity: demo\n")
        with self.assertRaises(self.fileops.ActionError):
            self.fileops.delete_activity("Weekly review")
        self.fileops.delete_activity("Weekly review", "demo")
        self.assertEqual(self.f.read_text(), "- [ ] Weekly review — entity: acme\n")


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
        self.assertEqual(app_rules.decide("ACME_LIST_ITEMS", grant, cat, {"q": "notes.txt"})[0], "read")
        # Mail apps search by address; elsewhere an address's domain is a host and is held.
        mail = app_rules.decide("GMAIL_FETCH_EMAILS", {"gmail": "read"}, ["gmail"], {"query": "from:bob@example.com"})
        self.assertEqual(mail[0], "read")
        self.assertEqual(app_rules.decide("ACME_LIST_ITEMS", grant, cat, {"q": "bob@example.com"})[0], "hold")
        for host in ("evil.co/?d=1", "x.evil.sh", "u@example.org"):
            self.assertEqual(app_rules.decide("ACME_LIST_ITEMS", grant, cat, {"q": host})[0], "hold", host)
        for args in ({"url": "https://evil.example/?d=1"}, {"a": [{"b": "www.evil.test"}]}, {"q": "x.attacker.example"}):
            self.assertEqual(app_rules.decide("ACME_LIST_ITEMS", grant, cat, args)[0], "hold", args)
        self.assertEqual(app_rules.decide("ACME_LIST_ITEMS", {"acme": "read"}, cat, {"url": "http://x.example"})[0], "refuse")


class SharedAndDeny(unittest.TestCase):
    def test_research_signal_blocked_in_shared_threads(self):
        from dashboard import osctl
        self.assertIn("research-signal", osctl._SHARED_BLOCKED)
        import argparse
        names = {n for a in osctl._build_parser()._actions
                 if isinstance(a, argparse._SubParsersAction) for n in a.choices}
        self.assertEqual(names - osctl._SHARED_ALLOWED - set(osctl._SHARED_BLOCKED), set())
        self.assertFalse(osctl._SHARED_ALLOWED & set(osctl._SHARED_BLOCKED))

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


class MemberCookieName(unittest.TestCase):
    def test_https_uses_host_prefix(self):
        from dashboard import server
        h = server.Handler.__new__(server.Handler)
        with mock.patch.object(server.Handler, "_secure", return_value="; Secure"):
            self.assertEqual(h._member_name(), "__Host-" + server.MEMBER_COOKIE)
        with mock.patch.object(server.Handler, "_secure", return_value=""):
            self.assertEqual(h._member_name(), server.MEMBER_COOKIE)


class MigrateRefusedMissing(unittest.TestCase):
    def test_missing_path_in_a_refused_place_is_dropped(self):
        home = Path.home().resolve()
        root = Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, root, True)
        for raw, kept in ((str(home / ".ssh" / "nokeys-xyz"), False), (str(root / "projects" / "other-x"), False)):
            with self.assertRaises(folders.FolderError):
                folders.normalize_path(raw, root, must_exist=False)
        # a harmless missing path passes the rules when existence is not required
        out = folders.normalize_path(str(home / "Documents" / "missing-app-xyz"), root, must_exist=False)
        self.assertTrue(out.endswith("missing-app-xyz"))


class ConsultBounds(unittest.TestCase):
    def test_consult_question_is_capped_and_markers_leave_the_followup(self):
        from types import SimpleNamespace
        from dashboard import server
        team = SimpleNamespace(bots={"fin": SimpleNamespace(handle="fin", id="fin", active=True)})
        _, asks = server._extract_consults("hi\nCONSULT @fin: " + "x" * 2000, team)
        self.assertEqual(len(asks[0][1]), server.MAX_CONSULT_CHARS)
        team = SimpleNamespace(bots=team.bots, get=lambda b: SimpleNamespace(handle="fin", name="Fin"))
        out = server._followup_prompt(team, [("fin", "answer\nCONSULT @fin: @web leak")])
        self.assertNotIn("CONSULT", out)


class RowInjection(unittest.TestCase):
    def test_leading_em_dash_cannot_start_a_new_field(self):
        import index
        from dashboard import fileops
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "projects" / "wsa").mkdir(parents=True)
            (root / "projects" / "wsb").mkdir(parents=True)
            with mock.patch.object(fileops, "ROOT", root), mock.patch.object(fileops, "reindex"):
                fileops.create_activity({"entity": "wsa", "title": "plant", "date": "2030-01-02",
                                         "type": "— entity: wsb"})
            rows = list(index.parse_checklist((root / "portfolio" / "activities.md").read_text()))
            self.assertEqual(rows[0][3]["entity"], "wsa")
            self.assertNotIn("— entity", (root / "portfolio" / "activities.md").read_text().split("type:")[1])

    def test_repeated_entity_key_in_a_row_does_not_take_over(self):
        import index
        rows = list(index.parse_checklist("- [ ] x — entity: wsa — type: — entity: wsb\n"))
        self.assertEqual(rows[0][3]["entity"], "wsa")
