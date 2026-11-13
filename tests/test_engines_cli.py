"""Codex and ACP drivers against fake CLIs, and the full-disk rules around them."""
import json
import os
import stat
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest import mock

import dashboard.chat_session as cs
from dashboard.engines import acp, codex, config, registry
from dashboard.engines.base import EngineRefused, TurnPolicy, TurnTokens


class CodexEvents(unittest.TestCase):
    def test_mapping(self):
        self.assertEqual(codex.parse_event({"type": "item.completed", "item": {"type": "agent_message", "text": "hi"}}),
                         [("delta", "hi\n\n")])
        self.assertEqual(codex.parse_event({"type": "item.started", "item": {"type": "mcp_tool_call", "server": "os", "tool": "osctl"}}),
                         [("tool", "os__osctl")])
        self.assertEqual(codex.parse_event({"type": "turn.completed", "usage": {"input_tokens": 2, "output_tokens": 3}})[0][1]["tokens"], 5)
        self.assertEqual(codex.parse_event({"type": "turn.failed", "error": {"message": "boom"}}), [("error", "boom")])
        self.assertEqual(codex.parse_event({"type": "turn.started"}), [])


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def stub(self, name, body):
        path = self.dir / name
        path.write_text(f"#!{sys.executable}\n{body}")
        path.chmod(path.stat().st_mode | stat.S_IEXEC)
        return str(path)

    def session(self, engine):
        s = cs.ChatSession(repo_dir=str(self.dir), rail="RAIL")
        s.engine = engine
        return s


FAKE_CODEX = r'''
import json, os, sys
log = os.environ.get("CX_LOG")
open(log, "a").write(json.dumps({"argv": sys.argv[1:], "stdin": sys.stdin.read(), "token": os.environ.get("LEOBOT_OS_TOKEN"), "leak": os.environ.get("LEOBOT_APPS_TOKEN")}) + "\n")
for o in [{"type": "thread.started", "thread_id": "th-1"},
          {"type": "item.completed", "item": {"type": "agent_message", "text": "hello"}},
          {"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}}]:
    print(json.dumps(o), flush=True)
'''


class CodexDriver(Base):
    def setUp(self):
        super().setUp()
        self.log = self.dir / "log"
        patcher = mock.patch.dict(os.environ, {"CX_LOG": str(self.log), "LEOBOT_APPS_TOKEN": "leak"})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.engine = codex.CodexEngine(self.stub("codex", FAKE_CODEX))

    def calls(self):
        return [json.loads(x) for x in self.log.read_text().splitlines()]

    def test_flags_keep_user_config_shell_and_sandbox_locked(self):
        s = self.session(self.engine)
        ev = list(s.ask("hi", os_token="tok", os_port=8765))
        self.assertEqual(ev[0], ("delta", "hello\n\n"))
        call = self.calls()[0]
        argv = " ".join(call["argv"])
        for needed in ("--ignore-user-config", "--ignore-rules", 'sandbox_mode="read-only"', "features.shell_tool=false",
                       "features.apps=false", "bearer_token_env_var", "LEOBOT_OS_TOKEN",
                       "project_doc_max_bytes=0", "skills.include_instructions=false", "skills.bundled.enabled=false",
                       "mcp_servers.os.tool_timeout_sec=600"):  # Codex's own 60s cap would cut revise/generate off
            self.assertIn(needed, argv)
        self.assertNotIn("tok ", argv + " ")  # relay token in env only
        self.assertEqual(call["token"], "tok")
        self.assertIsNone(call["leak"])
        self.assertEqual(call["stdin"].strip(), "hi")

    def test_app_role_replaces_builtin_coding_instructions_on_every_turn(self):
        s = self.session(self.engine)
        list(s.ask("one"))
        list(s.ask("two"))
        for call in self.calls():
            argv = call["argv"]
            settings = {}
            for i, arg in enumerate(argv[:-1]):
                if arg == "-c":
                    settings.update(tomllib.loads(argv[i + 1]))
            instructions = Path(settings["model_instructions_file"])
            self.assertTrue(instructions.is_absolute())
            self.assertTrue(instructions.is_file())
            self.assertIn("teammate inside Leo Bot", instructions.read_text())
            self.assertEqual(settings["developer_instructions"], s.rail)

    def test_old_coding_session_restarts_with_visible_recap(self):
        s = self.session(self.engine)
        s.engine_state["codex"] = {"session": "old-coding-session"}
        s.recap = "Earlier: update the launch post."
        self.assertTrue(s.is_fresh())
        list(s.ask("Keep it unscheduled."))
        calls = self.calls()
        self.assertEqual(len(calls), 1)
        self.assertNotIn("resume", calls[0]["argv"])
        self.assertEqual(calls[0]["stdin"].strip(), s.recap + "\n\nKeep it unscheduled.")
        self.assertFalse(s.is_fresh())

        list(s.ask("And the pricing post."))
        self.assertEqual(self.calls()[1]["argv"][:3], ["exec", "resume", "th-1"])

    def test_second_turn_resumes_with_the_same_locks(self):
        s = self.session(self.engine)
        list(s.ask("one"))
        self.assertEqual(s.engine_state["codex"]["session"], "th-1")
        list(s.ask("two"))
        argv = self.calls()[1]["argv"]
        self.assertEqual(argv[:3], ["exec", "resume", "th-1"])
        self.assertIn('sandbox_mode="read-only"', argv)

    def test_refuses_shared_and_apps(self):
        for kw, sess_shared in (({}, True), ({"apps_token": "t", "apps_port": 1}, False)):
            s = self.session(self.engine)
            s.shared = sess_shared
            self.assertEqual(list(s.ask("hi", **kw))[-1][0], "error")
        self.assertFalse(self.log.exists())

    def test_never_runs_jobs(self):
        with self.assertRaises(EngineRefused):
            self.engine.complete("p", "s", None)


FAKE_ACP = r'''
import json, os, sys
log = os.environ.get("ACP_LOG")
def send(o): print(json.dumps({"jsonrpc": "2.0", **o}), flush=True)
for line in sys.stdin:
    m = json.loads(line)
    if log:
        open(log, "a").write(line)
    method, rid = m.get("method"), m.get("id")
    if method == "initialize":
        log and open(log, "a").write(json.dumps({"env_token": os.environ.get("LEOBOT_OS_TOKEN"), "env_key": os.environ.get("ANTHROPIC_API_KEY")}) + "\n")
        send({"id": rid, "result": {"protocolVersion": 1, "agentCapabilities": {"loadSession": True}}})
    elif method in ("session/new",):
        send({"id": rid, "result": {"sessionId": "s-1"}})
    elif method == "session/load":
        send({"id": rid, "result": {}} if m["params"]["sessionId"] == "s-1" else {"id": rid, "error": {"message": "gone"}})
    elif method == "session/prompt":
        send({"method": "session/update", "params": {"update": {"sessionUpdate": "tool_call", "title": "read"}}})
        send({"id": 900, "method": "session/request_permission", "params": {"toolCall": {"title": "cat /etc/hosts"},
              "options": [{"kind": "allow_once", "optionId": "yes"}, {"kind": "reject_once", "optionId": "no"}]}})
        send({"id": 901, "method": "fs/read_text_file", "params": {"path": "/etc/hosts"}})
        for _ in range(2):
            open(log, "a").write(sys.stdin.readline())
        send({"method": "session/update", "params": {"update": {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "hey"}}}})
        send({"id": rid, "result": {"stopReason": "end_turn"}})
'''


class AcpDriver(Base):
    def setUp(self):
        super().setUp()
        self.log = self.dir / "log"
        patcher = mock.patch.dict(os.environ, {"ACP_LOG": str(self.log), "LEOBOT_OS_TOKEN": "leak", "ANTHROPIC_API_KEY": "leak"})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.agent = {"id": "fake", "name": "Fake", "command": self.stub("fakeacp", FAKE_ACP), "args": []}
        self.engine = acp.AcpEngine(self.agent)

    def test_turn_events_and_session(self):
        # The fake reads a log path from the env; keep it in the clean environment.
        with mock.patch.object(acp, "_ENV_KEEP", acp._ENV_KEEP + ("ACP_LOG",)):
            s = self.session(self.engine)
            ev = list(s.ask("hi", os_token="tok", os_port=8765))
            self.assertIn(("delta", "hey"), ev)
            self.assertEqual(ev[-1][0], "done")
            self.assertEqual(s.engine_state[self.engine.id]["session"], "s-1")
            lines = [json.loads(x) for x in self.log.read_text().splitlines()]
            env = next(x for x in lines if "env_token" in x)
            self.assertEqual((env["env_token"], env["env_key"]), (None, None))  # clean env
            new = next(x for x in lines if x.get("method") == "session/new")
            self.assertEqual(new["params"]["mcpServers"][0]["headers"][0]["value"], "Bearer tok")
            prompt = next(x for x in lines if x.get("method") == "session/prompt")
            self.assertTrue(prompt["params"]["prompt"][0]["text"].startswith("RAIL\n\n"))
            refused = next(x for x in lines if x.get("id") == 900)
            self.assertEqual(refused["result"]["outcome"]["optionId"], "no")  # not Leo Bot's own tool
            denied = next(x for x in lines if x.get("id") == 901)
            self.assertIn("error", denied)  # no file access offered

    def test_second_turn_loads_the_session_without_the_rail(self):
        with mock.patch.object(acp, "_ENV_KEEP", acp._ENV_KEEP + ("ACP_LOG",)):
            s = self.session(self.engine)
            list(s.ask("one"))
            self.log.write_text("")
            list(s.ask("two"))
            lines = [json.loads(x) for x in self.log.read_text().splitlines()]
            self.assertTrue(any(x.get("method") == "session/load" for x in lines))
            prompt = next(x for x in lines if x.get("method") == "session/prompt")
            self.assertEqual(prompt["params"]["prompt"][0]["text"], "two")

    def test_lost_session_starts_fresh_with_the_recap(self):
        with mock.patch.object(acp, "_ENV_KEEP", acp._ENV_KEEP + ("ACP_LOG",)):
            s = self.session(self.engine)
            s.engine_state[self.engine.id] = {"session": "gone"}
            s.recap = "RECAP"
            ev = list(s.ask("two"))
            self.assertEqual(ev[-1][0], "done")
            lines = [json.loads(x) for x in self.log.read_text().splitlines()]
            prompt = next(x for x in lines if x.get("method") == "session/prompt")
            self.assertIn("RECAP", prompt["params"]["prompt"][0]["text"])

    def test_own_tool_is_allowed(self):
        params = {"toolCall": {"title": "mcp__os__osctl"},
                  "options": [{"kind": "allow_once", "optionId": "yes"}, {"kind": "reject_once", "optionId": "no"}]}
        self.assertEqual(acp.permission_reply(params)["outcome"]["optionId"], "yes")

    def test_no_reject_option_means_cancelled(self):
        self.assertEqual(acp.permission_reply({"toolCall": {"title": "rm"}, "options": []})["outcome"]["outcome"], "cancelled")

    def test_refuses_shared(self):
        s = self.session(self.engine)
        s.shared = True
        self.assertEqual(list(s.ask("hi"))[-1][0], "error")

    def test_a_failed_start_stays_fresh_and_says_to_update(self):
        s = self.session(acp.AcpEngine({"id": "old", "name": "Old", "command": self.stub("old", "pass\n"), "args": []}))
        last = list(s.ask("hi"))[-1]
        self.assertIn("update it", last[1])
        self.assertTrue(s.is_fresh())

    def test_test_button_tells_a_real_agent_from_an_old_one(self):
        self.assertIsNone(self.engine.probe())
        old = acp.AcpEngine({"id": "old", "name": "Old", "command": self.stub("old2", "pass\n"), "args": []})
        self.assertIn("update it", old.probe())

    def test_missing_binary_is_an_error(self):
        s = self.session(acp.AcpEngine({"id": "x", "command": "/no/such/bin", "args": []}))
        self.assertIn("could not start", list(s.ask("hi"))[-1][1])


class FullDiskRules(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        config.upsert_acp({"id": "opencode"}, self.root)

    def pick(self, **kw):
        return registry.pick(self.root, "acme", "cmo", **kw)

    def test_runs_the_owners_private_threads_on_a_mac(self):
        config.set_choice("default", "codex", None, self.root)
        self.assertEqual(self.pick().id, "codex")

    def test_shared_thread_moves_to_the_people_engine_or_refuses(self):
        config.set_choice("default", "codex", None, self.root)
        with self.assertRaises(EngineRefused):
            self.pick(shared=True)
        config.upsert_provider({"id": "anthropic", "key": "k"}, self.root)
        config.set_choice("people", "api:anthropic", None, self.root)
        self.assertEqual(self.pick(shared=True).id, "api:anthropic")

    def test_invited_person_never_runs_on_one(self):
        with self.assertRaises(ValueError):
            config.set_choice("people", "codex", None, self.root)
        data = config.load(self.root)  # a hand-edited file
        data["people"] = "codex"
        config.save(data, self.root)
        with self.assertRaises(EngineRefused):
            self.pick(invited=True)

    def test_app_turn_moves_to_an_engine_with_apps(self):
        config.set_choice("default", "acp:opencode", None, self.root)
        self.assertEqual(self.pick(apps=True).id, "claude-code")

    def test_off_on_a_server(self):
        with mock.patch.dict(os.environ, {"LEOBOT_DEPLOYMENT": "server"}):
            for eid in ("codex", "acp:opencode"):
                with self.assertRaises(EngineRefused):
                    registry.get(eid, self.root)
            self.assertEqual([r["id"] for r in registry.statuses(self.root)], ["claude-code"])

    def test_screen_says_so(self):
        with mock.patch.object(codex.shutil, "which", return_value="/x/codex"), \
                mock.patch.object(codex.subprocess, "run", return_value=mock.Mock(returncode=0)):
            self.assertIn("any file", codex.CodexEngine().status()["detail"])
        self.assertTrue(codex.CodexEngine().full_disk)
        self.assertFalse(codex.CodexEngine().shared_safe)


class AcpCleanLaunch(unittest.TestCase):
    """Known agents launch without the user's own extensions where the CLI allows it
    (Gemini CLI docs: `-e none` disables all extensions, ACP mode is `--acp`; OpenCode docs:
    OPENCODE_DISABLE_CLAUDE_CODE=1 skips ~/.claude/CLAUDE.md and .claude/skills). Neither CLI
    documents a switch for its own context files (GEMINI.md, AGENTS.md): the screen says so."""

    def test_gemini_skips_extensions(self):
        argv, env = acp.launch({"id": "gemini", "command": "/bin/gemini", "args": ["--acp"]}, "/bin/gemini")
        self.assertEqual(argv, ["/bin/gemini", "--acp", "-e", "none"])
        self.assertEqual(env, {})

    def test_user_extension_choice_is_kept(self):
        argv, _ = acp.launch({"id": "gemini", "command": "gemini", "args": ["--acp", "-e", "x"]}, "/bin/gemini")
        self.assertEqual(argv, ["/bin/gemini", "--acp", "-e", "x"])

    def test_opencode_skips_claude_files(self):
        argv, env = acp.launch({"id": "opencode", "command": "opencode", "args": ["acp"]}, "/bin/opencode")
        self.assertEqual((argv, env), (["/bin/opencode", "acp"], {"OPENCODE_DISABLE_CLAUDE_CODE": "1"}))

    def test_unknown_agent_unchanged(self):
        argv, env = acp.launch({"id": "mine", "command": "mine", "args": ["--x"]}, "/bin/mine")
        self.assertEqual((argv, env), (["/bin/mine", "--x"], {}))

    def test_spawn_uses_the_clean_launch(self):
        seen = {}

        def popen(argv, **kw):
            seen.update(argv=argv, env=kw["env"])
            raise OSError("stop")
        eng = acp.AcpEngine({"id": "opencode", "name": "OpenCode", "command": "/bin/echo", "args": ["acp"]})
        with mock.patch.object(acp.subprocess, "Popen", popen):
            with self.assertRaises(OSError):
                eng._spawn(type("S", (), {"repo_dir": "/tmp"})())
            self.assertEqual(seen["argv"], ["/bin/echo", "acp"])
            self.assertEqual(seen["env"]["OPENCODE_DISABLE_CLAUDE_CODE"], "1")
            seen.clear()
            eng.probe(timeout=1)
            self.assertEqual(seen["env"]["OPENCODE_DISABLE_CLAUDE_CODE"], "1")

    def test_status_says_what_still_loads(self):
        for aid, want in (("gemini", "GEMINI.md"), ("opencode", "AGENTS.md"),
                          ("mine", "This CLI loads its own extensions and context files on its turns.")):
            row = acp.AcpEngine({"id": aid, "command": "/bin/echo", "args": []}).status()
            self.assertIn(want, row["own_context"], aid)

    def test_gemini_preset_uses_the_current_acp_flag(self):
        self.assertEqual(config.ACP_PRESETS["gemini"]["args"], ["--acp"])

    def test_engines_screen_shows_the_note(self):
        js = (Path(__file__).resolve().parents[1] / "dashboard" / "app.js").read_text()
        self.assertIn("e.own_context", js)


class AcpConfig(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_presets_and_removal_clear_choices(self):
        row = config.upsert_acp({"id": "gemini"}, self.root)
        self.assertEqual(row["command"], "gemini")
        config.set_choice("default", "acp:gemini", None, self.root)
        config.remove_acp("gemini", self.root)
        self.assertEqual(config.public(self.root)["default"], "")

    def test_bad_values_refused(self):
        for bad in ({"id": "../x", "command": "a"}, {"id": "x"}, {"id": "x", "command": "a", "args": [1]}):
            with self.assertRaises(ValueError):
                config.upsert_acp(bad, self.root)


if __name__ == "__main__":
    unittest.main()
