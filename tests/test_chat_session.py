import io, json, os, stat, tempfile, textwrap, unittest
from unittest import mock
from pathlib import Path
import dashboard.chat_session as cs


class ParseEvent(unittest.TestCase):
    def test_text_delta(self):
        ev = {"type": "stream_event",
              "event": {"type": "content_block_delta",
                        "delta": {"type": "text_delta", "text": "Hello"}}}
        self.assertEqual(cs.parse_event(ev), ("delta", "Hello"))

    def test_tool_use_start(self):
        ev = {"type": "stream_event",
              "event": {"type": "content_block_start",
                        "content_block": {"type": "tool_use", "name": "Bash"}}}
        self.assertEqual(cs.parse_event(ev), ("tool", "Bash"))

    def test_result(self):
        ev = {"type": "result", "subtype": "success", "result": "done"}
        kind, payload = cs.parse_event(ev)
        self.assertEqual(kind, "done")

    def test_ignored(self):
        self.assertEqual(cs.parse_event({"type": "system"}), (None, None))


class AskWithStub(unittest.TestCase):
    """Drive ChatSession against a fake `claude` that emits stream-json."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        stub = Path(self.tmp.name) / "fakeclaude"
        stub.write_text(textwrap.dedent('''\
            #!/usr/bin/env python3
            import sys, json
            sys.stdin.readline()  # consume the user turn
            for t in ["He", "llo"]:
                print(json.dumps({"type":"stream_event","event":{
                    "type":"content_block_delta",
                    "delta":{"type":"text_delta","text":t}}}), flush=True)
            print(json.dumps({"type":"result","subtype":"success","result":"Hello"}), flush=True)
        '''))
        stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
        self.stub = str(stub)

    def tearDown(self):
        self.tmp.cleanup()

    def test_ask_streams_then_done(self):
        sess = cs.ChatSession(repo_dir=self.tmp.name, rail="RAIL", claude_bin=self.stub)
        events = list(sess.ask("hi"))
        deltas = "".join(p for k, p in events if k == "delta")
        self.assertEqual(deltas, "Hello")
        self.assertTrue(any(k == "done" for k, _ in events))

    def test_session_meta_and_skill_tracking(self):
        sess = cs.ChatSession(repo_dir=self.tmp.name, rail="RAIL", claude_bin=self.stub)
        self.assertTrue(sess.is_fresh())
        sess.note_skill("workspace")
        list(sess.ask("hi"))
        self.assertEqual(sess._turn_count, 1)
        self.assertEqual(sess._last_skill, "workspace")
        self.assertFalse(sess.is_fresh())
        meta = sess.session_meta()
        self.assertEqual(meta["turn_count"], 1)

    def test_linked_workspace_tools_are_loaded_only_when_needed(self):
        sess = cs.ChatSession(repo_dir=self.tmp.name, rail="RAIL", claude_bin=self.stub)
        ordinary = sess._base_cmd()
        linked = sess._base_cmd(read_dirs=("/tmp/My App",))
        graph = sess._base_cmd()

        ordinary_tools = ordinary[ordinary.index("--tools") + 1:ordinary.index("--allowedTools")]
        linked_tools = linked[linked.index("--tools") + 1:linked.index("--allowedTools")]
        ordinary_allowed = ordinary[ordinary.index("--allowedTools") + 1:
                                    ordinary.index("--strict-mcp-config")]
        linked_allowed = linked[linked.index("--allowedTools") + 1:
                                linked.index("--strict-mcp-config")]
        graph_allowed = graph[graph.index("--allowedTools") + 1:
                              graph.index("--strict-mcp-config")]
        self.assertNotIn("Glob", ordinary_tools)
        self.assertNotIn("Grep", ordinary_tools)
        self.assertNotIn("--add-dir", ordinary)
        self.assertIn("Glob", linked_tools)
        self.assertIn("Grep", linked_tools)
        self.assertNotIn("Glob", ordinary_allowed)
        self.assertIn("Glob", linked_allowed)
        self.assertFalse(any("tokensave_bridge" in item for item in ordinary_allowed))
        self.assertFalse(any("tokensave_bridge" in item for item in linked_allowed))
        self.assertFalse(any("tokensave_bridge" in item for item in graph_allowed))
        self.assertEqual(linked[linked.index("--add-dir") + 1], "/tmp/My App")
        self.assertNotIn("Glob", graph[graph.index("--tools") + 1:
                                       graph.index("--allowedTools")])
        self.assertNotIn("--add-dir", graph)
        self.assertIn("--restricted", ordinary)
        self.assertIn("--restricted", linked)


class WebAndSecretPolicy(unittest.TestCase):
    def test_user_web_domains_only_takes_typed_hosts(self):
        got = cs.user_web_domains(
            "see https://www.acme.example/x and demo.example; not notes.md, app.py or a@example.com")
        self.assertEqual(got, ["acme.example", "demo.example"])

    def test_webfetch_scoped_to_typed_domains(self):
        sess = cs.ChatSession(repo_dir=".", rail="R")
        cmd = sess._base_cmd(with_web=True, web_domains=["acme.example"])
        allowed = cmd[cmd.index("--allowedTools") + 1:cmd.index("--disallowedTools")]
        self.assertIn("WebSearch", allowed)
        self.assertNotIn("WebFetch", allowed)
        self.assertIn("WebFetch(domain:acme.example)", allowed)
        self.assertFalse([a for a in sess._base_cmd(with_web=True)
                          if a.startswith("WebFetch(")])

    def test_secret_files_denied_in_repo_and_linked_folder(self):
        sess = cs.ChatSession(repo_dir=".", rail="R")
        cmd = sess._base_cmd(read_dirs=("/tmp/app",))
        denied = cmd[cmd.index("--disallowedTools") + 1:cmd.index("--strict-mcp-config")]
        self.assertIn("Read(**/.env)", denied)
        self.assertIn("Read(//tmp/app/**/.env)", denied)
        self.assertIn("Read(**/.auth-token)", denied)  # dashboard login token

    def test_integrations_file_unreadable_every_turn(self):
        # HQ has no deny dirs; the Composio key file must still be unreadable.
        sess = cs.ChatSession(repo_dir=".", rail="R")
        cmd = sess._base_cmd()
        denied = cmd[cmd.index("--disallowedTools") + 1:cmd.index("--strict-mcp-config")]
        self.assertIn("Read(**/integrations.json)", denied)

    def test_session_transcripts_and_server_log_are_never_readable(self):
        # Every workspace's transcripts live in ~/.claude/projects; server.log
        # lists their ids. Bare Read must not reach either.
        sess = cs.ChatSession(repo_dir=".", rail="R")
        cmd = sess._base_cmd()
        denied = cmd[cmd.index("--disallowedTools") + 1:cmd.index("--strict-mcp-config")]
        self.assertIn(f"Read(/{Path.home() / '.claude'}/**)", denied)
        self.assertIn(f"Read(/{Path('.').resolve() / 'dashboard' / 'server.log'})", denied)


class AppsMount(unittest.TestCase):
    TOKEN = "f" * 64

    def test_mcp_config_only_with_apps_port(self):
        sess = cs.ChatSession(repo_dir=".", rail="R")
        self.assertNotIn("--mcp-config", sess._base_cmd())
        cmd = sess._base_cmd(apps_port=8765)
        self.assertIn("--strict-mcp-config", cmd)
        cfg = json.loads(cmd[cmd.index("--mcp-config") + 1])["mcpServers"]["apps"]
        self.assertEqual(cfg["type"], "http")
        self.assertEqual(cfg["url"], "http://127.0.0.1:8765/api/internal/apps/mcp")
        self.assertEqual(cfg["headers"], {"Authorization": "Bearer ${LEOBOT_APPS_TOKEN}"})
        allowed = cmd[cmd.index("--allowedTools") + 1:cmd.index("--disallowedTools")]
        self.assertIn("mcp__apps", allowed)

    def test_token_not_in_argv_and_in_env(self):
        sess = cs.ChatSession(repo_dir=".", rail="R")
        seen = {}

        class P:
            def __init__(self, cmd, **kw):
                seen["cmd"], seen["env"] = cmd, kw["env"]
                self.stdin, self.stdout, self.stderr = io.StringIO(), io.StringIO(), io.StringIO()
                self.returncode = 0

            def wait(self, *a, **k):
                return 0

            def poll(self):
                return 0
        with mock.patch.object(cs.subprocess, "Popen", P):
            list(sess.ask("hi", apps_token=self.TOKEN, apps_port=8765))
        self.assertFalse([a for a in seen["cmd"] if self.TOKEN in a])
        self.assertEqual(seen["env"]["LEOBOT_APPS_TOKEN"], self.TOKEN)

    def test_no_token_env_on_ordinary_turn(self):
        sess = cs.ChatSession(repo_dir=".", rail="R")
        seen = {}

        class P:
            def __init__(self, cmd, **kw):
                seen["env"] = kw["env"]
                self.stdin, self.stdout, self.stderr = io.StringIO(), io.StringIO(), io.StringIO()
                self.returncode = 0

            def wait(self, *a, **k):
                return 0

            def poll(self):
                return 0
        with mock.patch.dict(cs.os.environ, {"LEOBOT_APPS_TOKEN": "leak"}), \
                mock.patch.object(cs.subprocess, "Popen", P):
            list(sess.ask("hi"))
        self.assertNotIn("LEOBOT_APPS_TOKEN", seen["env"])


def _turn_env(sess):
    """The env one turn's `claude -p` child gets."""
    seen = {}

    class P:
        def __init__(self, cmd, **kw):
            seen["env"] = kw["env"]
            self.stdin, self.stdout, self.stderr = io.StringIO(), io.StringIO(), io.StringIO()
            self.returncode = 0

        def wait(self, *a, **k):
            return 0

        def poll(self):
            return 0
    with mock.patch.object(cs.subprocess, "Popen", P):
        list(sess.ask("hi"))
    return seen["env"]


class BotSandboxEnv(unittest.TestCase):
    def tearDown(self):
        cs.remove_bot_tmpdir()

    def test_bot_temp_dir_is_private_not_the_shared_claude_one(self):
        # Claude lets a turn Read its project's temp folder: the shared one holds
        # every dev session's scratch and task output for this repo.
        with mock.patch.dict(cs.os.environ, {"CLAUDE_CODE_TMPDIR": "/tmp/shared"}):
            env = _turn_env(cs.ChatSession(repo_dir=".", rail="R"))
        d = Path(env["CLAUDE_CODE_TMPDIR"])
        self.assertNotEqual(str(d), "/tmp/shared")
        self.assertTrue(d.is_dir())
        self.assertEqual(stat.S_IMODE(d.stat().st_mode), 0o700)
        cs.remove_bot_tmpdir()
        self.assertFalse(d.exists())

    def test_concurrent_first_turns_share_one_temp_dir(self):
        # Two chat requests at once must not each make one (the loser would leak).
        import threading, time
        real, calls = cs.tempfile.mkdtemp, []

        def slow_mkdtemp(**kw):
            calls.append(1)
            time.sleep(0.05)
            return real(**kw)
        with mock.patch.object(cs.tempfile, "mkdtemp", slow_mkdtemp):
            got = []
            threads = [threading.Thread(target=lambda: got.append(cs.bot_tmpdir())) for _ in range(6)]
            for th in threads:
                th.start()
            for th in threads:
                th.join()
        self.assertEqual((len(calls), len(set(got))), (1, 1))

    def test_thread_that_read_an_app_tells_osctl(self):
        sess = cs.ChatSession(repo_dir=".", rail="R")
        with mock.patch.dict(cs.os.environ, {"LEOBOT_APPS_USED": "1"}):  # never inherited
            self.assertNotIn("LEOBOT_APPS_USED", _turn_env(sess))
        sess.apps_used = True
        self.assertEqual(_turn_env(sess)["LEOBOT_APPS_USED"], "1")


if __name__ == "__main__":
    unittest.main()
