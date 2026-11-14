"""dashboard/taint.py: outside content and app data never meet a way out."""
import io
import json
import os
import subprocess
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from dashboard import osctl, shell_rules, taint
from dashboard.engines import api, api_tools, claude_code
from dashboard.engines.base import TurnPolicy, TurnTokens, gate_vars

HOOK = Path(taint.__file__).resolve().parent / "turn_hook.py"


class Rules(unittest.TestCase):
    def test_web_is_off_once_app_data_is_in(self):
        t = taint.Turn()
        self.assertEqual(t.decide("WebSearch"), (True, ""))
        self.assertTrue(t.outside)  # search results are outside content
        self.assertFalse(t.app_data)
        t.saw_app_data()
        self.assertEqual(t.decide("WebSearch"), (False, taint.WEB_OFF))
        self.assertEqual(t.decide("WebFetch")[0], False)

    def test_an_auto_save_waits_after_outside_content(self):
        t = taint.Turn()
        self.assertEqual(t.decide("osctl-write"), (True, ""))
        t.saw_outside()
        self.assertEqual(t.decide("osctl-write"), (False, taint.SAVE_WAITS))

    def test_shell_commands_follow_the_shell_rules(self):
        turn = taint.Turn()
        self.assertEqual(turn.decide("Bash", "git push"), (True, ""))
        self.assertFalse(turn.decide("Bash", "rm -rf ~")[0])
        turn.saw_outside()
        self.assertEqual(turn.decide("Bash", "git push"), (False, shell_rules.SENDS_OFF))
        self.assertEqual(turn.decide("Bash", "npm test"), (True, ""))

    def test_taint_from_the_thread_and_app_data_implies_outside(self):
        self.assertTrue(taint.Turn(app_data=True).outside)
        self.assertTrue(taint.Turn(outside=True).outside)
        self.assertEqual(taint.Turn().decide("Read"), (True, ""))

    def test_gate_tokens(self):
        t = taint.Turn()
        tok = taint.GATES.mint(t)
        self.assertIs(taint.GATES.lookup(tok), t)
        self.assertIsNone(taint.GATES.lookup("nope"))
        self.assertIsNone(taint.GATES.lookup(""))
        taint.GATES.revoke(tok)
        self.assertIsNone(taint.GATES.lookup(tok))
        tok = taint.GATES.mint(t)
        with mock.patch("dashboard.taint.time.time", return_value=taint.time.time() + taint.MAX_AGE + 1):
            self.assertIsNone(taint.GATES.lookup(tok))


class _Gate(BaseHTTPRequestHandler):
    answer = {"allow": True, "reason": ""}
    asked = []

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        type(self).asked.append((self.headers.get("Authorization"), body))
        raw = json.dumps(type(self).answer).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *a):
        pass


class StubGate(unittest.TestCase):
    def setUp(self):
        _Gate.asked = []
        self.srv = HTTPServer(("127.0.0.1", 0), _Gate)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}{taint.GATE_PATH}"


class Hook(StubGate):
    def hook(self, env, event=None):
        env = {k: v for k, v in os.environ.items() if not k.startswith("LEOBOT_GATE")} | env
        out = subprocess.run([sys.executable, "-I", str(HOOK)], input=json.dumps(event or {"tool_name": "WebSearch"}),
                             capture_output=True, text=True, env=env, timeout=30)
        return out.stdout.strip()

    def denied(self, out):
        return json.loads(out)["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_allow_prints_nothing_so_the_normal_permissions_still_apply(self):
        _Gate.answer = {"allow": True}
        self.assertEqual(self.hook({"LEOBOT_GATE_TOKEN": "t1", "LEOBOT_GATE_URL": self.url}), "")
        self.assertEqual(_Gate.asked, [("Bearer t1", {"tool": "WebSearch"})])

    def test_deny_carries_the_reason(self):
        _Gate.answer = {"allow": False, "reason": taint.WEB_OFF}
        out = self.hook({"LEOBOT_GATE_TOKEN": "t1", "LEOBOT_GATE_URL": self.url})
        self.assertTrue(self.denied(out))
        self.assertIn("off in this conversation", out)

    def test_a_shell_command_goes_to_the_gate(self):
        _Gate.answer = {"allow": True}
        self.hook({"LEOBOT_GATE_TOKEN": "t1", "LEOBOT_GATE_URL": self.url},
                  {"tool_name": "Bash", "tool_input": {"command": "npm test"}})
        self.assertEqual(_Gate.asked, [("Bearer t1", {"tool": "Bash", "command": "npm test"})])

    def test_fails_closed(self):
        for env in ({}, {"LEOBOT_GATE_TOKEN": "t1"},
                    {"LEOBOT_GATE_TOKEN": "t1", "LEOBOT_GATE_URL": "http://127.0.0.1:9/api/internal/gate"},
                    {"LEOBOT_GATE_TOKEN": "t1", "LEOBOT_GATE_URL": "http://evil.example/api/internal/gate"}):
            self.assertTrue(self.denied(self.hook(env)), env)
        _Gate.answer = {"allow": "yes"}  # anything but true
        self.assertTrue(self.denied(self.hook({"LEOBOT_GATE_TOKEN": "t1", "LEOBOT_GATE_URL": self.url})))


class OsctlAutoSave(StubGate):
    def run_osctl(self, env):
        keep = {k: v for k, v in os.environ.items() if not k.startswith("LEOBOT_")}
        with mock.patch.dict(os.environ, {**keep, "LEOBOT_JOB": "jb1", "LEOBOT_JOB_MODE": "auto", **env}, clear=True), \
                mock.patch("sys.stdout", new_callable=io.StringIO) as out, \
                mock.patch.object(osctl, "_auto_reached_past_its_skill", return_value=False):
            osctl.main(["add-evidence", "--project", "acme", "--text", "x"])
        return out.getvalue()

    def test_a_save_after_outside_content_waits(self):
        _Gate.answer = {"allow": False, "reason": taint.SAVE_WAITS}
        out = self.run_osctl({"LEOBOT_GATE_TOKEN": "t1", "LEOBOT_GATE_URL": self.url})
        self.assertIn("saves nothing by itself", out)
        self.assertEqual(_Gate.asked, [("Bearer t1", {"tool": "osctl-write"})])

    def test_an_unreachable_gate_refuses(self):
        out = self.run_osctl({"LEOBOT_GATE_TOKEN": "t1", "LEOBOT_GATE_URL": "http://127.0.0.1:9/api/internal/gate"})
        self.assertEqual(json.loads(out)["error"], osctl.READONLY_TEXT["approval"])


class ClaudeCodeFlags(unittest.TestCase):
    def cmd(self, gate, apps_port=None, web=True):
        sess = SimpleNamespace(rail="r", model="haiku", repo_dir=str(Path.cwd()), _started=False,
                               session_id="00000000-0000-0000-0000-000000000000", engine_state={})
        return claude_code.ClaudeCodeEngine().build_cmd(sess, TurnPolicy(web=web), "haiku", apps_port=apps_port,
                                                        gate=gate)

    def tools(self, cmd):
        i = cmd.index("--tools") + 1
        return cmd[i:cmd.index("--allowedTools")]

    def test_web_carries_the_gate_hook(self):
        cmd = self.cmd(gate=True, apps_port=8765)
        self.assertIn("WebSearch", self.tools(cmd))
        hooks = json.loads(cmd[cmd.index("--settings") + 1])["hooks"]["PreToolUse"][0]
        self.assertEqual(hooks["matcher"], "WebSearch|WebFetch")
        self.assertIn("turn_hook.py", hooks["hooks"][0]["command"])
        self.assertIn(" -I ", hooks["hooks"][0]["command"])

    def shell_cmd(self, gate, edit=True, shared=False):
        d = str(Path.cwd())
        sess = SimpleNamespace(rail="r", model="haiku", repo_dir=d, _started=False,
                               session_id="00000000-0000-0000-0000-000000000000", engine_state={})
        policy = TurnPolicy(shell=True, shared=shared, read_dirs=(d,), edit_dirs=(d,) if edit else ())
        return claude_code.ClaudeCodeEngine().build_cmd(sess, policy, "haiku", gate=gate)

    def allowed(self, cmd):
        i = cmd.index("--allowedTools") + 1
        return cmd[i:cmd.index("--disallowedTools")]

    def test_full_access_shell_needs_the_gate_and_an_edit_folder(self):
        cmd = self.shell_cmd(gate=True)
        self.assertIn("Bash", self.allowed(cmd))
        hooks = json.loads(cmd[cmd.index("--settings") + 1])["hooks"]["PreToolUse"][0]
        self.assertEqual(hooks["matcher"], "WebSearch|WebFetch|Bash")
        for cmd in (self.shell_cmd(gate=False), self.shell_cmd(gate=True, edit=False),
                    self.shell_cmd(gate=True, shared=True)):
            self.assertNotIn("Bash", self.allowed(cmd))
            self.assertNotIn("--settings", cmd)

    def test_sub_agents_on_private_turns_only(self):
        cmd = self.cmd(gate=True)
        self.assertIn("Task", self.tools(cmd))
        self.assertIn("Task", self.allowed(cmd))
        self.assertNotIn("Task", self.allowed(self.shell_cmd(gate=True, shared=True)))

    def test_sub_agent_spend_is_counted_once_per_message(self):
        run = {}
        msg = {"id": "m1", "model": "claude-haiku-5-5",
               "usage": {"input_tokens": 1000, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0,
                         "output_tokens": 1000}}
        sub = {"type": "assistant", "parent_tool_use_id": "t1", "message": msg}
        self.assertTrue(claude_code.call_usage(sub, run, "haiku"))
        self.assertFalse(claude_code.call_usage(sub, run, "haiku"))  # the same message's next block
        self.assertAlmostEqual(run["cost"], (1000 * 0.10 + 1000 * 0.50) / 1_000_000)
        self.assertFalse(claude_code.call_usage({"type": "assistant", "message": msg}, {}, "haiku"))  # the lead's

    def test_apps_without_a_gate_never_get_web(self):
        cmd = self.cmd(gate=False, apps_port=8765)
        self.assertNotIn("WebSearch", self.tools(cmd))
        self.assertNotIn("--settings", cmd)

    def test_env_carries_the_gate_only_with_one(self):
        self.assertEqual(gate_vars(TurnTokens()), {})
        env = gate_vars(TurnTokens(gate_token="t1", gate_port=8765))
        self.assertEqual(env["LEOBOT_GATE_URL"], "http://127.0.0.1:8765/api/internal/gate")
        with mock.patch.dict(os.environ, {"LEOBOT_GATE_TOKEN": "server-own"}):
            self.assertNotIn("LEOBOT_GATE_TOKEN", claude_code.ClaudeCodeEngine().turn_env(TurnPolicy(), TurnTokens()))


class ApiEngine(unittest.TestCase):
    def box(self, turn=None, apps=False):
        tok = taint.GATES.mint(turn) if turn else None
        if tok:
            self.addCleanup(taint.GATES.revoke, tok)
        backends = {"apps_list": lambda t: [{"name": "COMPOSIO_MULTI_EXECUTE_TOOL"}]} if apps else {}
        policy = TurnPolicy(web=True, web_domains=("example.com",), apps=apps)
        return api_tools.ToolBox(policy, Path("."), TurnTokens(apps_token="a" if apps else None, gate_token=tok),
                                 "api", backends)

    def test_web_follows_the_turn(self):
        t = taint.Turn()
        box = self.box(t, apps=True)
        self.assertTrue(box.web_open())
        t.saw_app_data()
        self.assertFalse(box.web_open())
        self.assertEqual(box.run("web_fetch", {"url": "https://example.com"}), (taint.WEB_OFF, True))

    def test_apps_without_a_gate_never_get_web(self):
        self.assertFalse(self.box(apps=True).web_open())
        self.assertTrue(self.box().web_open())

    def test_a_search_marks_the_turn(self):
        t = taint.Turn()
        self.box(t).saw_web()
        self.assertTrue(t.outside)

    def test_history_without_web_blocks(self):
        msgs = [{"role": "user", "content": "hi"},
                {"role": "assistant", "content": [
                    {"type": "server_tool_use", "id": "s1", "name": "web_search", "input": {}},
                    {"type": "web_search_tool_result", "tool_use_id": "s1", "content": []},
                    {"type": "text", "text": "found", "citations": [{"type": "web_search_result_location"}]}]}]
        out = api._without_web(msgs)
        self.assertEqual(out[0], msgs[0])
        self.assertEqual(out[1]["content"], [{"type": "text", "text": "found"}])
        self.assertEqual(len(msgs[1]["content"]), 3)  # the stored history is untouched


class ThreadMemory(unittest.TestCase):
    def test_a_thread_keeps_its_taint_and_old_app_threads_count_as_app_data(self):
        from dashboard import chat_session
        rec = {"id": "c1", "app_data": False, "outside": True}
        s = chat_session.ChatSession.restore(".", "r", rec)
        self.assertEqual((s.app_data, s.outside), (False, True))
        old = chat_session.ChatSession.restore(".", "r", {"id": "c2", "apps_used": True})
        self.assertEqual((old.app_data, old.outside), (True, True))
        fresh = chat_session.ChatSession.restore(".", "r", {"id": "c3"})
        self.assertEqual((fresh.app_data, fresh.outside), (False, False))


if __name__ == "__main__":
    unittest.main()
