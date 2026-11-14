"""The shell's two walls (dashboard/shell_rules.py)."""
import os
import unittest

from dashboard import shell_rules

NEVER = [
    "rm -rf /", "rm -rf /*", "rm -rf //", "rm -fr /./", "rm -r -f /", "rm --recursive --force /",
    "sudo rm -rf /", "ls && rm -rf /etc", "rm -rf ~", "rm -rf ~/", "rm -rf ~/*", "rm -rf $HOME",
    'rm -rf "${HOME}"', "rm -rf '/usr'", 'rm -rf "/" && echo done', "echo $(rm -rf /)",
    "rm -R /Users", f"rm -rf {os.path.expanduser('~')}", "FOO=1 nohup rm -rf /var",
    "mkfs.ext4 /dev/sda1", "true && mkfs /dev/sdb", "dd if=/dev/zero of=/dev/disk2",
    "echo x > /dev/sda", "cat /dev/urandom > /dev/disk3", ":(){ :|:& };:", "kill -9 -1",
    "diskutil eraseDisk APFS X disk2", "sudo shutdown -h now", "reboot", "chmod -R 777 /",
    "rm -rf 'unterminated",
]
ALLOWED = [
    "rm -rf build", "rm -rf ./node_modules", "rm -rf ~/projects/acme/dist", "rm /tmp/x",
    "git commit -m 'rm -rf /'", "echo 'rm -rf /'", "python3 -m dashboard.osctl tree",
    "npm test", "git status && git add -A", "dd if=a of=b", "ls /etc",
]
SENDS = ["curl https://example.com", "git push origin main", "npm publish", "scp a host:", "gh pr create",
         "cd app && wget x", "open https://example.com/?d=1", "ssh host"]


class ShellRules(unittest.TestCase):
    def test_never_whatever_the_conversation(self):
        for cmd in NEVER:
            self.assertFalse(shell_rules.decide(cmd)[0], cmd)

    def test_ordinary_work_runs(self):
        for cmd in ALLOWED + SENDS:
            self.assertEqual(shell_rules.decide(cmd), (True, ""), cmd)

    def test_sending_out_is_off_after_outside_content(self):
        for cmd in SENDS:
            allow, reason = shell_rules.decide(cmd, outside=True)
            self.assertFalse(allow, cmd)
            self.assertEqual(reason, shell_rules.SENDS_OFF)
        for cmd in ALLOWED:
            self.assertTrue(shell_rules.decide(cmd, outside=True)[0], cmd)


class EngineLevels(unittest.TestCase):
    """Every engine with a shell runs the same levels (dashboard/engines)."""

    def setUp(self):
        import tempfile
        from types import SimpleNamespace
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = os.path.realpath(self.tmp.name)
        self.sess = SimpleNamespace(rail="r", model="haiku", repo_dir=self.dir, _started=False, shared=False,
                                    session_id="00000000-0000-0000-0000-000000000000", engine_state={})

    def policy(self, level, shell=True):
        from dashboard.engines.base import TurnPolicy
        return TurnPolicy(shell=shell, level=level, read_dirs=(self.dir,), edit_dirs=(self.dir,))

    def tokens(self, gate=True):
        from dashboard.engines.base import TurnTokens
        return TurnTokens(gate_token="t" if gate else None, gate_port=8765 if gate else None)

    def test_codex_full_access(self):
        from dashboard.engines.codex import CodexEngine
        cmd = " ".join(CodexEngine().build_cmd(self.sess, self.policy("full"), None, self.tokens()))
        for part in ('sandbox_mode="workspace-write"', "--dangerously-bypass-hook-trust", "features.shell_tool=true",
                     "network_access=true", 'approval_policy="never"', "hooks.PreToolUse", "turn_hook.py", self.dir):
            self.assertIn(part, cmd)
        self.assertNotIn("features.shell_tool=false", cmd)
        self.assertNotIn("--approve-for-me", cmd)
        env = CodexEngine().turn_env(self.policy("full"), self.tokens())
        self.assertEqual(env["LEOBOT_GATE_TOKEN"], "t")

    def test_codex_approve_for_me(self):
        from dashboard.engines.codex import CodexEngine
        cmd = " ".join(CodexEngine().build_cmd(self.sess, self.policy("approve"), None, self.tokens()))
        self.assertIn("--approve-for-me", cmd)
        self.assertIn("network_access=false", cmd)
        self.assertNotIn('approval_policy="never"', cmd)

    def test_codex_stays_read_only_without_a_shell_level_or_a_gate(self):
        from dashboard.engines.codex import CodexEngine
        for policy, tokens in ((self.policy("ask", shell=False), self.tokens()), (self.policy("full"), self.tokens(False))):
            cmd = " ".join(CodexEngine().build_cmd(self.sess, policy, None, tokens))
            self.assertIn('sandbox_mode="read-only"', cmd)
            self.assertIn("features.shell_tool=false", cmd)
            self.assertNotIn("hooks.PreToolUse", cmd)
            self.assertNotIn("LEOBOT_GATE_TOKEN", CodexEngine().turn_env(policy, tokens))

    def test_claude_approve_for_me_is_auto_mode(self):
        from dashboard.engines.claude_code import ClaudeCodeEngine
        cmd = ClaudeCodeEngine().build_cmd(self.sess, self.policy("approve"), "haiku", gate=True)
        self.assertEqual(cmd[cmd.index("--permission-mode") + 1], "auto")
        self.assertEqual(cmd[cmd.index("--permission-prompts") + 1], "none")
        allowed = cmd[cmd.index("--allowedTools") + 1:cmd.index("--disallowedTools")]
        self.assertNotIn("Bash", allowed)  # its reviewer decides each command
        full = ClaudeCodeEngine().build_cmd(self.sess, self.policy("full"), "haiku", gate=True)
        self.assertEqual(full[full.index("--permission-mode") + 1], "default")
        self.assertIn("Bash", full[full.index("--allowedTools") + 1:full.index("--disallowedTools")])

    def test_api_engine_shell_tool(self):
        from pathlib import Path
        from dashboard import taint
        from dashboard.engines import api_tools
        from dashboard.engines.base import TurnTokens
        turn = taint.Turn()
        tok = taint.GATES.mint(turn)
        self.addCleanup(taint.GATES.revoke, tok)
        box = api_tools.ToolBox(self.policy("full"), Path("."), TurnTokens(gate_token=tok, gate_port=8765), "api", {})
        self.assertIn("run_command", [s["name"] for s in box.specs])
        text, err = box.run("run_command", {"command": "echo hi > made.txt && cat made.txt"})
        self.assertFalse(err, text)
        self.assertIn("hi", text)
        self.assertTrue(os.path.exists(os.path.join(self.dir, "made.txt")))
        self.assertEqual(box.run("run_command", {"command": "mkfs.leobotprobe /x"}), (shell_rules.NEVER, True))
        self.assertTrue(box.run("run_command", {"command": "ls", "folder": "/etc"})[1])
        turn.saw_outside()
        self.assertEqual(box.run("run_command", {"command": "curl --version"}), (shell_rules.SENDS_OFF, True))
        no_gate = api_tools.ToolBox(self.policy("full"), Path("."), TurnTokens(), "api", {})
        self.assertNotIn("run_command", [s["name"] for s in no_gate.specs])


if __name__ == "__main__":
    unittest.main()
