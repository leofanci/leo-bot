"""The shell's two walls (dashboard/shell_rules.py)."""
import os
import unittest
import unittest.mock

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
                     "network_access=false", 'approval_policy="never"', "hooks.PreToolUse", " -I -c ", self.dir):
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

    def test_claude_approve_for_me_file_tools_ask_the_gate(self):
        import json
        from dashboard import taint
        from dashboard.engines.claude_code import ClaudeCodeEngine
        cmd = ClaudeCodeEngine().build_cmd(self.sess, self.policy("approve"), "haiku", gate=True)
        matcher = json.loads(cmd[cmd.index("--settings") + 1])["hooks"]["PreToolUse"][0]["matcher"]
        self.assertIn("|Edit|Write|MultiEdit|NotebookEdit", matcher)
        full = ClaudeCodeEngine().build_cmd(self.sess, self.policy("full"), "haiku", gate=True)
        self.assertNotIn("Edit", json.loads(full[full.index("--settings") + 1])["hooks"]["PreToolUse"][0]["matcher"])
        tok = taint.GATES.mint(taint.Turn(), writable=(self.dir,))
        self.addCleanup(taint.GATES.revoke, tok)
        self.assertTrue(taint.GATES.may_write(tok, os.path.join(self.dir, "src", "a.py"))[0])
        for bad in (os.path.expanduser("~/.zshrc"), os.path.join(self.dir, "..", "x"), self.dir + "-other/x", "rel/x", None):
            self.assertFalse(taint.GATES.may_write(tok, bad)[0], bad)
        self.assertFalse(taint.GATES.may_write("nope", os.path.join(self.dir, "a"))[0])

    def test_claude_shell_turn_works_in_its_folder_and_still_reaches_osctl(self):
        from dashboard.engines.claude_code import ClaudeCodeEngine
        self.assertEqual(ClaudeCodeEngine.shell_dir(self.policy("full"), True), self.dir)
        self.assertIsNone(ClaudeCodeEngine.shell_dir(self.policy("full"), False))  # no gate
        self.assertIsNone(ClaudeCodeEngine.shell_dir(self.policy("ask", shell=False), True))
        env = ClaudeCodeEngine().turn_env(self.policy("full"), self.tokens(), repo_dir="/repo")
        self.assertTrue(env["PYTHONPATH"].startswith("/repo"))

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

    def box(self, outside=False):
        from pathlib import Path
        from dashboard import taint
        from dashboard.engines import api_tools
        from dashboard.engines.base import TurnTokens
        turn = taint.Turn(outside=outside)
        tok = taint.GATES.mint(turn, writable=(self.dir,))
        self.addCleanup(taint.GATES.revoke, tok)
        return api_tools.ToolBox(self.policy("full"), Path("."), TurnTokens(gate_token=tok, gate_port=8765), "api", {})

    @unittest.skipUnless(os.path.exists(shell_rules.SANDBOX_EXEC), "macOS sandbox only")
    def test_api_command_after_outside_content_runs_sandboxed(self):
        box = self.box(outside=True)
        text, err = box.run("run_command", {"command": "echo ok > in.txt && cat in.txt"})
        self.assertFalse(err, text)
        home = os.path.join(os.path.expanduser("~"), ".leobot-sandbox-probe")
        self.assertTrue(box.run("run_command", {"command": f"echo x > {home}"})[1])
        self.assertFalse(os.path.exists(home))
        text, err = box.run("run_command", {"command": "python3 -c 'import socket;socket.create_connection((\"1.1.1.1\",443),3)'"})
        self.assertTrue(err, text)

    def test_api_command_ends_what_it_left_running(self):
        import time
        from dashboard.engines import api_tools
        mark = os.path.join(self.dir, "late.txt")
        start = time.time()
        text, err = self.box().run("run_command", {"command": f"(sleep 3; echo late > {mark}) & echo started"})
        self.assertLess(time.time() - start, 3)  # a background child holds no pipe the turn waits on
        self.assertIn("started", text)
        time.sleep(3.5)
        self.assertFalse(os.path.exists(mark))  # its process group was ended
        with unittest.mock.patch.object(api_tools, "RUN_TIMEOUT", 1):
            self.assertEqual(self.box().run("run_command", {"command": "sleep 30"}), ("stopped after 1s", True))

    def test_api_approve_for_me_asks_the_reviewer(self):
        box = self.box()
        asked = []
        box.review = lambda command, cwd: (asked.append((command, cwd)) or (command != "rm -rf src"), "too risky")
        self.assertFalse(box.run("run_command", {"command": "echo fine"})[1])
        text, err = box.run("run_command", {"command": "rm -rf src"})
        self.assertTrue(err)
        self.assertIn("too risky", text)
        self.assertEqual(asked, [("echo fine", self.dir), ("rm -rf src", self.dir)])
        box.review = lambda command, cwd: self.fail("the gate decides first")
        self.assertEqual(box.run("run_command", {"command": "mkfs.leobotprobe /x"}), (shell_rules.NEVER, True))

    def test_api_reviewer_fails_closed(self):
        from types import SimpleNamespace
        from dashboard.engines.api import ApiEngine, REVIEW_SYSTEM
        eng = ApiEngine.__new__(ApiEngine)
        box = SimpleNamespace(spent={})
        for answer, ok in ((("ALLOW", {"input_tokens": 5}, "m", False), True),
                           (("DENY: deletes data", {}, "m", False), False),
                           (("ALLOW", {}, "m", True), False),          # cut short
                           (("Sure, allow it", {}, "m", False), False)):
            eng._one_shot = lambda system, prompt, model, a=answer: (self.assertEqual(system, REVIEW_SYSTEM) or a)
            self.assertEqual(eng._review("ls", self.dir, "req", "m", box)[0], ok, answer)
        self.assertEqual(box.spent, {"input_tokens": 5})
        eng._one_shot = lambda *a: (_ for _ in ()).throw(OSError("down"))
        self.assertFalse(eng._review("ls", self.dir, "req", "m", box)[0])


class Sandbox(unittest.TestCase):
    def test_plan(self):
        self.assertEqual(shell_rules.plan("npm test"), (True, "", None))  # no outside content: as written
        self.assertEqual(shell_rules.plan("curl x", outside=True), (False, shell_rules.SENDS_OFF, None))
        self.assertEqual(shell_rules.plan("rm -rf /", outside=True), (False, shell_rules.NEVER, None))
        # Leo Bot's own command line keeps its own rules and must write its data: it runs outside
        # the sandbox, but as Leo Bot's own code only (OsctlExempt)
        run = shell_rules.plan("python3 -m dashboard.osctl tree", outside=True)[2]
        self.assertTrue(run.startswith("cd "), run)
        self.assertNotIn(shell_rules.SANDBOX_EXEC, run)
        for sneaky in ("python3 -m dashboard.osctl tree; python3 x.py", "python3 -m dashboard.osctl tree > f",
                       "python3 -m dashboard.osctl $(cat x)", "python3 -m dashboard.osctlx"):
            ok, _, run = shell_rules.plan(sneaky, outside=True)
            if os.path.exists(shell_rules.SANDBOX_EXEC):
                self.assertTrue(run and run.startswith(shell_rules.SANDBOX_EXEC), sneaky)

    def test_profile(self):
        prof = shell_rules.sandbox_profile(["/work/acme"])
        for part in ("(deny network-outbound (remote ip))", '"localhost:*"', "mDNSResponder", "(deny file-write*)",
                     '(subpath "/work/acme")', "/usr/bin/osascript", "/bin/launchctl", "(deny appleevent-send)",
                     ".git/(hooks|config$)"):
            self.assertIn(part, prof)

    @unittest.skipUnless(os.path.exists(shell_rules.SANDBOX_EXEC), "macOS sandbox only")
    def test_profile_file_is_short_and_out_of_reach(self):
        import subprocess
        import tempfile
        with tempfile.TemporaryDirectory() as data, tempfile.TemporaryDirectory() as work:
            pdir = os.path.join(os.path.expanduser("~"), ".leobot-test-sandbox-profiles")
            self.addCleanup(lambda: __import__("shutil").rmtree(pdir, ignore_errors=True))
            run = shell_rules.sandboxed("echo ok > in.txt", [work], profile_dir=pdir)
            self.assertIn(" -f ", run)
            self.assertLess(len(run), 300)
            self.assertEqual(subprocess.run(run, shell=True, cwd=work).returncode, 0)
            prof = next(f for f in os.listdir(pdir) if f.endswith(".sb"))
            tamper = shell_rules.sandboxed(f"echo '(version 1)(allow default)' > {os.path.join(pdir, prof)}", [work],
                                           profile_dir=pdir)
            self.assertNotEqual(subprocess.run(tamper, shell=True, cwd=work, capture_output=True).returncode, 0)

    def test_gate_wraps_only_where_asked(self):
        from dashboard import taint
        t = taint.Turn(outside=True)
        wrap, own = taint.GATES.mint(t, writable=("/w",)), taint.GATES.mint(t, writable=("/w",), wrap=False)
        self.addCleanup(taint.GATES.revoke, wrap)
        self.addCleanup(taint.GATES.revoke, own)
        self.assertIsNone(taint.GATES.shell(own, "npm test")[2])  # Codex: its own sandbox
        if os.path.exists(shell_rules.SANDBOX_EXEC):
            self.assertIn("/w", taint.GATES.shell(wrap, "npm test")[2])
        self.assertFalse(taint.GATES.shell("nope", "ls")[0])



class BrowserWall(unittest.TestCase):
    """A run with a browser: its shell never reaches the browser past the relay."""

    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = os.path.realpath(self.tmp.name)
        self.home, self.root = os.path.join(base, "h"), os.path.join(base, "d")
        os.makedirs(os.path.join(self.home, ".agent-browser"))
        os.makedirs(os.path.join(self.root, "run"))
        chrome = os.path.join(self.home, "Library", "Application Support", "Google", "Chrome")
        os.makedirs(chrome)
        with open(os.path.join(chrome, "DevToolsActivePort"), "w") as f:
            f.write("45678\n/devtools/browser/abc\n")

    def walls(self, required=True):
        return shell_rules.browser_walls(self.root, required=required, home=self.home)

    def test_walls_name_the_folders(self):
        w = self.walls()
        self.assertEqual(w["paths"], sorted([os.path.join(self.home, ".agent-browser"), os.path.join(self.root, "run")]))
        self.assertTrue(w["required"])

    def test_profile_before_outside_content_is_the_browser_wall_alone(self):
        prof = shell_rules.sandbox_profile(["/work/acme"], browser=self.walls(), outside=False)
        for part in ('(deny network-outbound (remote ip "localhost:*"))', "(deny appleevent-send)",
                     f'(deny file-read* file-write* (subpath "{self.root}/run"))',
                     f'(remote unix-socket (subpath "{self.home}/.agent-browser"))'):
            self.assertIn(part, prof)
        self.assertNotIn("(deny network-outbound (remote ip))", prof)  # the internet stays, as before
        self.assertNotIn("(deny file-write*)", prof)

    def test_profile_after_outside_content_denies_every_localhost_port(self):
        prof = shell_rules.sandbox_profile(["/work/acme"], browser=self.walls(), outside=True)
        self.assertIn("(deny network-outbound (remote ip))", prof)
        self.assertNotIn('(allow network-outbound (remote ip "localhost:*"))', prof)
        self.assertIn(f'(deny file-read* file-write* (subpath "{self.home}/.agent-browser"))', prof)
        self.assertIn('"localhost:*"', shell_rules.sandbox_profile(["/work/acme"]))  # no browser: as before

    def test_plan_sandboxes_from_the_first_command(self):
        allow, _, run = shell_rules.plan("npm test", outside=False, writable=["/w"], browser=self.walls())
        self.assertTrue(allow)
        if shell_rules.can_sandbox():
            self.assertTrue(run.startswith(shell_rules.SANDBOX_EXEC))
        self.assertEqual(shell_rules.plan("rm -rf /", browser=self.walls()), (False, shell_rules.NEVER, None))
        osctl = shell_rules.plan("python3 -m dashboard.osctl tree", browser=self.walls())[2]
        self.assertNotIn(shell_rules.SANDBOX_EXEC, osctl)  # Leo Bot's own osctl, outside the sandbox

    def test_no_sandbox_refuses_when_the_browser_can_send(self):
        with unittest.mock.patch.object(shell_rules, "SANDBOX_EXEC", "/nonexistent/sandbox-exec"):
            self.assertEqual(shell_rules.plan("ls", browser=self.walls(required=True)),
                             (False, shell_rules.NO_SANDBOX, None))
            self.assertEqual(shell_rules.plan("ls", browser=self.walls(required=False)), (True, "", None))
            self.assertEqual(shell_rules.plan("ls", outside=True, browser=self.walls(required=True)),
                             (False, shell_rules.NO_SANDBOX, None))
        self.assertNotIn("\u2014", shell_rules.NO_SANDBOX)

    def test_turn_with_a_browser_walls_every_command(self):
        from dashboard import taint
        t = taint.Turn()
        self.assertIsNone(t.shell("npm test", ("/w",))[2])  # no browser: as written before outside content
        t.browsing(self.root, sends=False)
        with unittest.mock.patch.object(shell_rules, "browser_walls", wraps=shell_rules.browser_walls) as walls:
            run = t.shell("npm test", ("/w",))[2]
        self.assertEqual(walls.call_args.kwargs.get("required"), False)
        if shell_rules.can_sandbox():
            self.assertTrue(run.startswith(shell_rules.SANDBOX_EXEC))
        t.browsing(self.root, sends=True)
        t.browsing(self.root, sends=False)  # only ever gains
        self.assertTrue(t.browser_sends)

    @unittest.skipUnless(os.path.exists(shell_rules.SANDBOX_EXEC), "macOS sandbox only")
    def test_live_sandbox_keeps_the_browser_out(self):
        import socket
        import subprocess
        import sys
        import tempfile

        def listen():
            s = socket.socket()
            s.bind(("127.0.0.1", 0))
            s.listen(5)
            self.addCleanup(s.close)
            return s.getsockname()[1]
        debug, other = listen(), listen()
        chrome = os.path.join(self.home, "Library", "Application Support", "Google", "Chrome", "DevToolsActivePort")
        with open(chrome, "w") as f:
            f.write(f"{debug}\n/devtools/browser/abc\n")
        with open(os.path.join(self.root, "run", "browser-engine-key"), "w") as f:
            f.write("k" * 64)
        sock_dir = tempfile.mkdtemp(dir="/tmp")  # a short path: a Unix socket name is at most 104 bytes
        self.addCleanup(__import__("shutil").rmtree, sock_dir, True)
        sock_path = os.path.join(sock_dir, "s.sock")
        u = socket.socket(socket.AF_UNIX)
        u.bind(sock_path)
        u.listen(5)
        self.addCleanup(u.close)
        walls = self.walls()
        walls["paths"] = walls["paths"] + [os.path.realpath(sock_dir)]  # stands in for ~/.agent-browser

        def runs(code, outside):
            cmd = shell_rules.sandboxed(f"{sys.executable} -c {__import__('shlex').quote(code)}", [self.tmp.name],
                                        browser=walls, outside=outside)
            return subprocess.run(cmd, shell=True, capture_output=True).returncode == 0
        tcp = "import socket; socket.create_connection(('127.0.0.1', {}), 3).close()"
        unix = f"import socket; s = socket.socket(socket.AF_UNIX); s.connect({sock_path!r})"
        key = f"open({os.path.join(self.root, 'run', 'browser-engine-key')!r}).read()"
        self.assertFalse(runs(tcp.format(debug), outside=False))
        self.assertFalse(runs(tcp.format(other), outside=False))  # every localhost port (round 2, B3)
        self.assertFalse(runs(unix, outside=False))
        self.assertFalse(runs(key, outside=False))
        self.assertFalse(runs(tcp.format(other), outside=True))  # after outside content: no localhost at all
        self.assertFalse(runs(key, outside=True))



class OsctlExempt(unittest.TestCase):
    """The one command that runs outside the sandbox never loads code a bot wrote (round 2, B1)."""

    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cwd = os.path.realpath(self.tmp.name)
        self.mark = os.path.join(self.cwd, "shadowed.txt")
        os.makedirs(os.path.join(self.cwd, "dashboard"))
        with open(os.path.join(self.cwd, "dashboard", "__init__.py"), "w") as f:
            f.write("")
        with open(os.path.join(self.cwd, "dashboard", "osctl.py"), "w") as f:
            f.write(f"open({self.mark!r}, 'w').write('bot code ran')\n")
        with open(os.path.join(self.cwd, "sitecustomize.py"), "w") as f:
            f.write(f"open({self.mark!r}, 'w').write('bot site ran')\n")

    def test_every_path_rewrites_it(self):
        import sys
        for kw in ({}, {"outside": True}, {"browser": {"paths": [], "required": True}}):
            allow, _, run = shell_rules.plan("python3 -m dashboard.osctl tree", **kw)
            self.assertTrue(allow, kw)
            self.assertIn(sys.executable, run)
            self.assertIn(" -I -c ", run)
            self.assertTrue(run.startswith("cd " + __import__("shlex").quote(str(shell_rules.REPO)) + " && "), run)

    def test_a_shadow_package_in_the_folder_is_not_loaded(self):
        import subprocess
        _, _, run = shell_rules.plan("python3 -m dashboard.osctl --help", outside=True)
        out = subprocess.run(["/bin/bash", "-c", run], cwd=self.cwd, capture_output=True, text=True,
                             env={**os.environ, "PYTHONPATH": self.cwd})
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("usage: osctl", out.stdout)  # Leo Bot's own osctl ran
        self.assertFalse(os.path.exists(self.mark))
        # as written, the same command would have run the bot's code
        subprocess.run(["/bin/bash", "-c", "python3 -m dashboard.osctl --help"], cwd=self.cwd, capture_output=True)
        self.assertTrue(os.path.exists(self.mark))

    def test_a_lookalike_is_not_exempt(self):
        for cmd in ("./python3 -m dashboard.osctl tree", "/tmp/x/python3 -m dashboard.osctl tree",
                    "python3 -m dashboard.osctl tree; ls", "PYTHONPATH=. python3 -m dashboard.osctl tree",
                    "python3 -I -m dashboard.osctl tree"):
            run = shell_rules.plan(cmd, outside=True)[2]
            if shell_rules.can_sandbox():
                self.assertTrue(run and run.startswith(shell_rules.SANDBOX_EXEC), cmd)
        self.assertIsNone(shell_rules.plan("./python3 -m dashboard.osctl tree")[2])  # before outside: as written


class Round2Walls(unittest.TestCase):
    """Round 2: no localhost at all beside a browser (B3); the profile folder and <data>/team stay
    unwritable (B2)."""

    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = os.path.realpath(self.tmp.name)
        self.home, self.root, self.work = (os.path.join(base, n) for n in ("h", "d", "w"))
        for d in (os.path.join(self.home, ".agent-browser"), os.path.join(self.root, "run"),
                  os.path.join(self.root, "team", "sandbox"), self.work):
            os.makedirs(d)
        self.pdir = os.path.join(self.root, "team", "sandbox")

    def walls(self):
        return shell_rules.browser_walls(self.root, required=True, home=self.home)

    def test_walls_and_profiles(self):
        w = self.walls()
        self.assertEqual(w["write_paths"], [os.path.join(self.root, "team")])
        self.assertNotIn("ports", w)
        for outside in (False, True):
            prof = shell_rules.sandbox_profile([self.work], browser=w, outside=outside, profile_dir=self.pdir)
            self.assertIn('(deny network-outbound (remote ip "localhost:*"))', prof)
            self.assertIn(f'(deny file-write* (subpath "{self.root}/team"))', prof)
            self.assertIn(f'(deny file-write* (subpath "{self.pdir}"))', prof)
        plain = shell_rules.sandbox_profile([self.work], profile_dir=self.pdir)
        self.assertIn(f'(deny file-write* (subpath "{self.pdir}"))', plain)

    @unittest.skipUnless(os.path.exists(shell_rules.SANDBOX_EXEC), "macOS sandbox only")
    def test_live(self):
        import shlex
        import socket
        import subprocess
        import sys
        s4 = socket.socket()
        s4.bind(("127.0.0.1", 0))
        s4.listen(5)
        self.addCleanup(s4.close)
        s6 = socket.socket(socket.AF_INET6)
        s6.bind(("::1", 0))
        s6.listen(5)
        self.addCleanup(s6.close)
        approvals = os.path.join(self.root, "team", "approvals.json")

        def runs(code, outside, browser=True):
            cmd = shell_rules.sandboxed(f"{sys.executable} -c {shlex.quote(code)}", [self.work],
                                        profile_dir=self.pdir, browser=self.walls() if browser else None,
                                        outside=outside)
            return subprocess.run(cmd, shell=True, cwd=self.work, capture_output=True).returncode == 0
        tcp4 = f"import socket; socket.create_connection(('127.0.0.1', {s4.getsockname()[1]}), 3).close()"
        tcp6 = f"import socket; s = socket.socket(socket.AF_INET6); s.settimeout(3); s.connect(('::1', {s6.getsockname()[1]}))"
        for outside in (False, True):
            self.assertFalse(runs(tcp4, outside), outside)  # no localhost port from the first command
            self.assertFalse(runs(tcp6, outside), outside)
            self.assertFalse(runs(f"open({approvals!r}, 'w').write('{{}}')", outside), outside)
            self.assertFalse(runs(f"open({os.path.join(self.pdir, 'x.sb')!r}, 'w').write('(allow default)')", outside))
            self.assertTrue(runs(f"open({os.path.join(self.work, 'ok.txt')!r}, 'w').write('ok')", outside), outside)
        # the profile file itself stays out of reach of a command it wraps
        self.assertFalse(runs(f"open({os.path.join(self.pdir, 'y.sb')!r}, 'w').write('x')", True, browser=False))
        self.assertFalse(os.path.exists(approvals))


if __name__ == "__main__":
    unittest.main()
