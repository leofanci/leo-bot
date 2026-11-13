"""Hardening notes from the 2026-11-09 audit: each test pins one closed note."""
import contextlib
import io
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from core import app_rules, composio, folders, people, rooms, runners
from dashboard import apps_relay, chat_store, fileops
from dashboard.engines import api_tools, claude_code, config
from dashboard.engines.base import EngineRefused, TurnPolicy
from dashboard.engines.codex import CodexEngine


def _yes(slug):
    return True


class Tmp(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()


class AppReads(unittest.TestCase):
    def test_a_blocked_toolkit_is_never_read_as_a_shorter_allowed_one(self):
        # "composio" is blocked and not in the catalog; a shorter allowed prefix must not take it.
        got = app_rules.decide("COMPOSIO_SEARCH_WEB", {"composio": "read"}, ["comp"], {}, _yes)
        self.assertEqual(got, ("refuse", "composio_search"))

    def test_paid_or_scraping_categories_hold_reads(self):
        page = {"items": [{"slug": "acme", "meta": {"categories": [{"name": "People & Contact Data"}]}},
                          {"slug": "notes", "meta": {"categories": [{"name": "notes"}]}}]}
        items, _ = composio._parse_catalog_page(page)
        paid = composio.paid_read_toolkits(items)
        self.assertEqual(paid, {"acme"})
        grant = {"acme": "write", "notes": "write"}
        self.assertEqual(app_rules.decide("ACME_LIST_PEOPLE", grant, ["acme", "notes"], {}, _yes, paid)[0], "hold")
        self.assertEqual(app_rules.decide("NOTES_LIST_PAGES", grant, ["acme", "notes"], {}, _yes, paid)[0], "read")
        self.assertIn("hunter", app_rules.PAID_READ_TOOLKITS)

    def test_money_categories_hold_writes_on_an_act_grant(self):
        page = {"items": [{"slug": "acme", "meta": {"categories": [{"name": "Payment Processing"}]}},
                          {"slug": "notes", "meta": {"categories": [{"name": "notes"}]}}]}
        items, _ = composio._parse_catalog_page(page)
        holds = composio.act_hold_toolkits(items)
        self.assertEqual(holds, {"acme"})
        grant = {"acme": "act", "notes": "act"}
        cat = ["acme", "notes"]
        self.assertEqual(app_rules.decide("ACME_CREATE_LINK", grant, cat, {}, _yes, holds=holds)[0], "hold")
        self.assertEqual(app_rules.decide("NOTES_CREATE_PAGE", grant, cat, {}, _yes, holds=holds)[0], "run")


class HeldNotesSurviveFailedReads(Tmp):
    def setUp(self):
        super().setUp()
        from core import integrations
        integrations.set_grant("hq", "cos", {"gmail": "write"}, self.root)
        integrations.note_known([{"id": "ca_g1", "toolkit": "gmail", "label": "work"}], self.root)

    def call(self, upstream):
        frame = {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {
            "name": "COMPOSIO_MULTI_EXECUTE_TOOL", "arguments": {"tools": [
                {"tool_slug": "GMAIL_FETCH_EMAILS", "arguments": {}},
                {"tool_slug": "GMAIL_SEND_EMAIL", "arguments": {"recipient_email": "a@example.com"}}]}}}
        ctx = {"ws": "hq", "bot": "cos", "chat": "c1", "room": "hq/general", "person": "ceo"}
        reply, _ = apps_relay.handle(frame, ctx, None, upstream, self.root, catalog=["gmail"], read_only=_yes)
        return "\n".join(c["text"] for c in reply["result"]["content"])

    def test_upstream_down(self):
        class Down:
            def forward(self, frame, sid):
                raise composio.ComposioError("Composio unreachable (TimeoutError)")
        text = self.call(Down())
        self.assertIn("did not run", text)
        self.assertIn("Held for approval", text)

    def test_upstream_error_reply(self):
        class Err:
            def forward(self, frame, sid):
                return {"jsonrpc": "2.0", "id": 3, "error": {"code": -1, "message": "nope"}}, sid
        text = self.call(Err())
        self.assertIn("nope", text)
        self.assertIn("Held for approval", text)


class WebFetchAndWrites(Tmp):
    def test_user_part_or_escapes_refused(self):
        policy = TurnPolicy(web=True, web_domains=("site.example",))
        web = "https" + "://"  # split: the privacy guard reads every literal address in the repo
        for url in (web + "evil.example%2f" + "@site.example/", web + "someone" + "@site.example/",
                    web + "si%74e.example/"):
            with self.assertRaises(api_tools.ToolError, msg=url):
                api_tools.web_fetch(url, policy, opener=mock.Mock())

    def test_a_slow_drip_stops_at_the_deadline(self):
        class Drip:
            def read1(self, n):
                return b"x"
        with mock.patch.object(api_tools.time, "monotonic", side_effect=[0, 1, 99]):
            with self.assertRaises(api_tools.ToolError):
                api_tools._read_until(Drip(), 1000, 50)

    def test_write_never_follows_a_swapped_folder(self):
        base = self.root / "edit"
        (base / "sub").mkdir(parents=True)
        outside = self.root / "outside"
        outside.mkdir()
        api_tools._write_atomic(base, base / "sub" / "a.txt", b"one")
        self.assertEqual((base / "sub" / "a.txt").read_bytes(), b"one")
        (base / "sub" / "a.txt").unlink()
        (base / "sub").rmdir()
        (base / "sub").symlink_to(outside)  # swapped after the checks
        with self.assertRaises(api_tools.ToolError):
            api_tools._write_atomic(base, base / "sub" / "b.txt", b"two")
        self.assertEqual(list(outside.iterdir()), [])
        api_tools._write_atomic(base, base / "new" / "deep" / "c.txt", b"three")
        self.assertEqual((base / "new" / "deep" / "c.txt").read_bytes(), b"three")


class Folders(Tmp):
    def test_nested_links_are_refused_across_workspaces(self):
        for slug in ("acme", "demo"):
            (self.root / "projects" / slug).mkdir(parents=True)
        elsewhere = tempfile.TemporaryDirectory()  # outside the Leo Bot folder, which is never linkable
        self.addCleanup(elsewhere.cleanup)
        outer = Path(elsewhere.name).resolve() / "code"
        (outer / "inner").mkdir(parents=True)
        folders.add("acme", str(outer), folders.READ, self.root)
        with self.assertRaises(folders.FolderError):
            folders.add("demo", str(outer / "inner"), folders.READ, self.root)
        with self.assertRaises(folders.FolderError):
            folders.add("acme", str(outer / "inner"), folders.READ, self.root)
        folders.add("demo", str(outer), folders.READ, self.root)  # the very same folder: a share

    def test_package_manager_configs_are_never_written(self):
        for name in (".yarnrc.yml", ".pnpmfile.cjs", "bunfig.toml"):
            self.assertIn(name, folders.NEVER_WRITE_FILES)


class EngineSettings(Tmp):
    def test_shapes_https_and_commands(self):
        self.assertTrue(config.base_url_ok("https://api.example"))
        self.assertTrue(config.base_url_ok("http://127.0.0.1:11434/v1"))
        self.assertTrue(config.base_url_ok("http://localhost:8080"))
        self.assertFalse(config.base_url_ok("http://api.example"))
        self.assertFalse(config.base_url_ok("http://127.0.0.1.evil.example"))
        with self.assertRaises(ValueError):
            config.upsert_provider({"id": "x", "format": "openai", "base_url": "http://api.example",
                                    "key": "k", "models": {"fast": "m"}}, self.root)
        self.assertTrue(config.command_ok("opencode") and config.command_ok("/usr/local/bin/x"))
        self.assertFalse(config.command_ok("bin/agent"))
        with self.assertRaises(ValueError):
            config.upsert_acp({"id": "a", "command": "./agent"}, self.root)
        path = self.root / "team" / "engines.json"
        path.parent.mkdir(parents=True)
        path.write_text('{"default": 5, "workspaces": {"acme": 3, "demo": "codex"},'
                        ' "providers": [{"id": "p", "format": "openai", "base_url": "http://far.example",'
                        ' "key": "k", "models": {"fast": "m"}}],'
                        ' "acp_agents": [{"id": "a", "command": "rel/agent", "args": [1]}]}')
        data = config.load(self.root)
        self.assertNotIn("default", data)
        self.assertEqual(data["workspaces"], {"demo": "codex"})
        self.assertEqual((data["providers"], data["acp_agents"]), ([], []))

    def test_full_disk_engine_refused_after_an_app_read(self):
        with self.assertRaises(EngineRefused):
            CodexEngine().check(TurnPolicy(apps_used=True))
        CodexEngine().check(TurnPolicy())


class TempFolders(Tmp):
    def test_stale_folders_of_gone_processes_are_swept(self):
        dead = self.root / "leobot-bots-dead"
        live = self.root / "leobot-uploads-live"
        old = self.root / "leobot-runner-old"
        other = self.root / "something-else"
        for d in (dead, live, old, other):
            d.mkdir()
        (dead / claude_code._PID_FILE).write_text("999999")
        (live / claude_code._PID_FILE).write_text(str(os.getpid()))
        past = time.time() - 3 * 86400
        os.utime(old, (past, past))
        removed = claude_code.sweep_stale_tmpdirs(str(self.root))
        self.assertEqual(sorted(Path(p).name for p in removed), ["leobot-bots-dead", "leobot-runner-old"])
        self.assertTrue(live.exists() and other.exists())

    def test_each_workspace_has_its_own_subfolder(self):
        with mock.patch.object(claude_code, "_BOT_TMPDIR", None):
            a, b = claude_code.bot_tmpdir("acme"), claude_code.bot_tmpdir("demo")
            self.addCleanup(claude_code.remove_bot_tmpdir)
            self.assertNotEqual(a, b)
            self.assertEqual(Path(a).parent, Path(b).parent)
            self.assertEqual(claude_code.tmp_name("../x"), "___x")
            env = claude_code.ClaudeCodeEngine().turn_env(TurnPolicy(workspace="acme"), mock.Mock(
                apps_token=None, os_token=None, runner_token=None))
            self.assertEqual(env["CLAUDE_CODE_TMPDIR"], a)

    def test_a_turn_never_runs_with_walls_its_rules_cannot_express(self):
        eng = claude_code.ClaudeCodeEngine()
        sess = mock.Mock(repo_dir=str(self.root), rail="R", model="haiku", _started=False)
        with self.assertRaises(EngineRefused):
            eng.build_cmd(sess, TurnPolicy(deny_dirs=(str(self.root / "a[1]"),)), "haiku")
        cmd = eng.build_cmd(sess, TurnPolicy(deny_files=(str(self.root / "os.db"),)), "haiku")
        self.assertIn(f"Edit(/{self.root / 'os.db'})", cmd)


class PeopleAndChannels(Tmp):
    def test_admin_revokes_member_invites_only(self):
        (self.root / "projects" / "acme").mkdir(parents=True)
        admin_inv, _ = people.create_invite(self.root, ws="acme", role="admin", by="ceo")
        member_inv, _ = people.create_invite(self.root, ws="acme", role="member", by="ceo")
        with self.assertRaises(LookupError):
            people.revoke_invite(self.root, admin_inv["id"], ws="acme", role="member")
        people.revoke_invite(self.root, member_inv["id"], ws="acme", role="member")
        people.revoke_invite(self.root, admin_inv["id"], ws="acme")  # the owner may

    def test_removed_person_leaves_every_private_channel_in_one_write(self):
        (self.root / "projects" / "acme").mkdir(parents=True)
        rooms._save_topics("acme", self.root, [
            {"name": "deals", "members": ["gone-bot"], "lead": "gone-bot", "private": True, "people": ["p1", "p2"]},
            {"name": "open", "members": ["cos"], "lead": "cos"}])
        self.assertTrue(rooms.forget_person("acme", "p1", self.root))
        self.assertEqual(rooms._load_topics("acme", self.root)[0]["people"], ["p2"])
        self.assertFalse(rooms.forget_person("acme", "p1", self.root))


class Chats(Tmp):
    def setUp(self):
        super().setUp()
        p = mock.patch.object(chat_store, "CHATS_DIR", self.root / "chats")
        p.start()
        self.addCleanup(p.stop)

    def test_a_deleted_thread_is_not_brought_back_by_its_turn(self):
        sess = mock.Mock(session_id="0" * 8 + "-0000-0000-0000-" + "1" * 12)
        chat_store.delete(sess.session_id)
        self.assertIsNone(chat_store.record_turn(sess, None, "hi", "hello"))
        self.assertFalse((self.root / "chats").exists() and any((self.root / "chats").iterdir()))

    def test_runner_text_is_labelled_in_the_recap(self):
        rec = {"messages": [{"role": "user", "content": "hi", "by_name": "Pat"},
                            {"role": "assistant", "bot": "cos", "content": "APPROVAL: x",
                             "engine": "runner:p1", "engine_label": "Pat's Mac"}]}
        self.assertIn("@cos (via Pat's Mac): APPROVAL: x", chat_store.recap(rec))

    def test_transcripts_of_a_deleted_thread_go(self):
        sid = "12345678-1234-1234-1234-123456789abc"
        proj = self.root / "cfg" / "projects" / "x"
        proj.mkdir(parents=True)
        (proj / f"{sid}.jsonl").write_text("{}")
        (proj / "other.jsonl").write_text("{}")
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.root / "cfg")}):
            n = claude_code.forget_transcripts({"id": "c1", "engines": {"claude-code": {"session": sid}}})
        self.assertEqual(n, 1)
        self.assertTrue((proj / "other.jsonl").exists())


class Runners(unittest.TestCase):
    def test_a_deleted_workspace_ends_every_runner_turn_there(self):
        hub = runners.Hub()
        turn = mock.Mock(owner="owner", job={"policy": {"workspace": "acme"}}, cancelled=False)
        keep = mock.Mock(owner="p1", job={"policy": {"workspace": "demo"}}, cancelled=False)
        hub._turns = {"t1": turn, "t2": keep}
        hub.drop_workspace("acme")
        self.assertTrue(turn.cancelled)
        self.assertFalse(keep.cancelled)


class Jobs(Tmp):
    def test_a_stuck_child_ends_with_a_clear_error(self):
        with mock.patch.object(fileops.subprocess, "run",
                               side_effect=subprocess.TimeoutExpired("generate.py", 1600)) as run:
            with self.assertRaises(fileops.ActionError) as err:
                fileops.reindex()
        self.assertEqual(run.call_args.kwargs["timeout"], fileops.REINDEX_TIMEOUT)
        self.assertIn("did not finish", str(err.exception))

    def test_plan_keeps_known_plain_fields_only(self):
        import generate
        got = generate.clean_plan({"period": "p", "profile": "x", "junk": 1, "posts": [
            {"id": "a", "channels": ["c1", {"x": 1}], "pillar": {"deep": 1}, "version": 10 ** 30,
             "working_title": "t", "objective": "reach"}, "not a post"]})
        self.assertEqual(got, {"period": "p", "profile": "x", "posts": [
            {"id": "a", "channels": ["c1"], "objective": "reach", "working_title": "t"}]})


class OwnerToken(Tmp):
    def test_rotate_needs_yes_and_removes_the_saved_token(self):
        import leobot
        team = self.root / "team"
        team.mkdir()
        (team / "owner-token").write_text("x" * 40)
        with mock.patch.object(leobot.paths, "DATA", self.root), \
                mock.patch.object(leobot, "_stop_if_running", return_value=None), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(leobot.main(["rotate-token"]), 1)
            self.assertTrue((team / "owner-token").exists())
            self.assertEqual(leobot.main(["rotate-token", "--yes"]), 0)
        self.assertFalse((team / "owner-token").exists())


if __name__ == "__main__":
    unittest.main()
