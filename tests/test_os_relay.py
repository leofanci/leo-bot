"""Shared threads: the osctl relay is the bot's only tool (no shell, no file reads)."""
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "dashboard"))

import chat_session  # noqa: E402
import os_relay  # noqa: E402
from dashboard import osctl  # noqa: E402

CTX = {"ws": "acme", "readonly": False, "apps_used": False}


def call(args=None, stdin=None, name="osctl"):
    arguments = {"args": args} if args is not None else {}
    if stdin is not None:
        arguments["stdin"] = stdin
    return {"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {"name": name, "arguments": arguments}}


class Protocol(unittest.TestCase):
    def test_handshake_and_list(self):
        init = os_relay.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                "params": {"protocolVersion": "2025-03-26"}}, CTX)
        self.assertEqual(init["result"]["protocolVersion"], "2025-03-26")
        self.assertIn("tools", init["result"]["capabilities"])
        self.assertIsNone(os_relay.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}, CTX))
        tools = os_relay.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, CTX)["result"]["tools"]
        self.assertEqual([t["name"] for t in tools], ["osctl"])

    def test_call_runs_osctl_with_the_args(self):
        runner = mock.Mock(return_value=('{"ok": true}', False))
        reply = os_relay.handle(call(["get-project", "--slug", "acme"], "body"), CTX, runner=runner)
        runner.assert_called_once()
        self.assertEqual(runner.call_args.args[:3], (["get-project", "--slug", "acme"], "body", CTX))
        self.assertEqual(reply["result"]["content"][0]["text"], '{"ok": true}')
        self.assertFalse(reply["result"]["isError"])

    def test_bad_calls_never_run(self):
        runner = mock.Mock()
        for frame in (call([]), call(["a", 3]), call(["a\x00"]), call(["tree"], stdin=5),
                      call(["tree"], name="Bash"), call(["x"] * (os_relay.MAX_ARGS + 1)),
                      {"jsonrpc": "2.0", "id": 1, "method": "resources/list"}, "nope"):
            reply = os_relay.handle(frame, CTX, runner=runner)
            self.assertTrue(reply.get("error") or reply["result"]["isError"], frame)
        runner.assert_not_called()


class Environment(unittest.TestCase):
    def test_workspace_comes_from_the_token_never_the_caller(self):
        with mock.patch.dict(os.environ, {"MYOS_WORKSPACE": "hq", "MYOS_APPS_TOKEN": "t", "MYOS_OS_TOKEN": "o"}):
            env = os_relay.env_for({"ws": "acme", "readonly": True, "apps_used": True})
        self.assertEqual((env["MYOS_WORKSPACE"], env["MYOS_SHARED"], env["MYOS_READONLY"]), ("acme", "1", "1"))
        self.assertEqual(env[chat_session.APPS_USED_ENV], "1")
        self.assertNotIn("MYOS_APPS_TOKEN", env)
        self.assertNotIn("MYOS_OS_TOKEN", env)
        self.assertNotIn("MYOS_READONLY", os_relay.env_for(CTX))

    def test_memory_is_not_written_from_a_shared_thread(self):
        text, failed = os_relay.run_osctl(["remember", "--bot", "cos", "--text", "x"], "", CTX, ROOT)
        self.assertTrue(failed)
        self.assertIn("not available in a thread invited people can see", json.loads(text)["error"])

    def test_args_are_not_a_shell(self):
        out = subprocess.run([sys.executable, "-c", "import sys; print(sys.argv[1:])", "tree; cat /etc/hosts"],
                             capture_output=True, text=True)
        self.assertIn("'tree; cat /etc/hosts'", out.stdout)  # one argv item, as run_osctl passes it


class SharedReads(unittest.TestCase):
    def guard(self, path, shared=True):
        args = mock.Mock(cmd="read-file", path=path, project=None)
        env = {"MYOS_SHARED": "1"} if shared else {}
        with mock.patch.object(osctl, "_owners", return_value={}), mock.patch.dict(os.environ, env, clear=False):
            if not shared:
                os.environ.pop("MYOS_SHARED", None)
            osctl._guard(args, "acme")

    def test_shared_reads_only_this_workspace_and_public_docs(self):
        self.guard("projects/acme/intake.md")
        self.guard("skills/demo/SKILL.md")
        for path in ("README.md", "dashboard/server.py", "projects/demo/intake.md", "/etc/hosts", "../x"):
            with self.assertRaises(osctl.fileops.ActionError, msg=path):
                self.guard(path)

    def test_owner_machine_state_is_private_in_every_workspace(self):
        for path in (".remember/now.md", ".tokensave/x", "dashboard/os.db", "dashboard/server.log",
                     ".claude/settings.local.json", ".git/config", "os.db"):
            with self.assertRaises(osctl.fileops.ActionError, msg=path):
                self.guard(path, shared=False)
        self.guard("README.md", shared=False)


class SharedCommand(unittest.TestCase):
    def cmd(self, **kw):
        s = chat_session.ChatSession(repo_dir=str(ROOT), rail="r")
        s.shared = True
        return s._base_cmd(**kw)

    def flag(self, cmd, name):
        i = cmd.index(name) + 1
        out = []
        while i < len(cmd) and not cmd[i].startswith("--"):
            out.append(cmd[i])
            i += 1
        return out

    def test_no_shell_no_files_no_apps(self):
        cmd = self.cmd(workspace_dir="/x/app", file_search_mode=True, apps_port=8765, os_port=8765)
        self.assertEqual(self.flag(cmd, "--tools"), [""])
        self.assertEqual(self.flag(cmd, "--allowedTools"), ["mcp__os"])
        self.assertNotIn("--add-dir", cmd)
        cfg = json.loads(self.flag(cmd, "--mcp-config")[0])
        self.assertEqual(list(cfg["mcpServers"]), ["os"])
        self.assertTrue(cfg["mcpServers"]["os"]["url"].endswith(chat_session.OS_MCP_PATH))
        self.assertNotIn("MYOS_OS_TOKEN", " ".join(cmd).replace("${MYOS_OS_TOKEN}", ""))  # token rides in env

    def test_web_stays_as_usual(self):
        cmd = self.cmd(with_web=True, os_port=8765, web_domains=["example.org"])
        self.assertEqual(self.flag(cmd, "--tools"), ["WebSearch", "WebFetch"])
        self.assertIn("mcp__os", self.flag(cmd, "--allowedTools"))
        self.assertIn("WebFetch(domain:example.org)", self.flag(cmd, "--allowedTools"))

    def test_owner_threads_unchanged(self):
        s = chat_session.ChatSession(repo_dir=str(ROOT), rail="r")
        cmd = s._base_cmd(os_port=8765)
        self.assertEqual(self.flag(cmd, "--tools"), ["Bash", "Read"])
        self.assertNotIn("--mcp-config", cmd)


if __name__ == "__main__":
    unittest.main()
