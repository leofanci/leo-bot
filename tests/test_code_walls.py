"""No bot writes Leo Bot's own code (dashboard/shell_rules.py code_walls), on every level and engine.

Every allowed shell command but the plain osctl form runs sandboxed, before outside content too,
with writes denied to the code folder, the Python that runs osctl, the turns' working folder and
the rename of every folder above them. With no sandbox, a command whose parsed target writes there
is refused. The engines' own file tools are walled off the same folders.
"""
import os
import shlex
import subprocess
import tempfile
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from core import paths
from dashboard import chat_session, shell_rules, taint
from dashboard.engines.base import TurnPolicy, TurnTokens


def _profile_of(run):
    """The sandbox profile text a planned command runs under (inline or from its file)."""
    words = shlex.split(run)
    if "-p" in words:
        return words[words.index("-p") + 1]
    return Path(words[words.index("-f") + 1]).read_text(encoding="utf-8")


def _bash(run, cwd):
    return subprocess.run(["/bin/bash", "-c", run], cwd=cwd, capture_output=True, text=True, timeout=30)


class CodeWalls(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(os.path.realpath(self.tmp.name))
        self.parent = base / "code-parent"
        self.repo = self.parent / "leo-bot"
        (self.repo / "core").mkdir(parents=True)
        (self.repo / "existing.py").write_text("keep\n")
        self.edit = base / "edit"
        self.edit.mkdir()
        self.pydir = base / "python"
        self.pydir.mkdir()

    def fake_code(self):
        """The code walls point at temp copies: the real checkout is never a target."""
        repo = mock.patch.object(shell_rules, "REPO", self.repo)
        py = mock.patch.object(shell_rules, "_python_dirs", return_value=[str(self.pydir)])
        repo.start(), py.start()
        self.addCleanup(repo.stop)
        self.addCleanup(py.stop)

    def run_planned(self, command, outside=False):
        allow, reason, run = shell_rules.plan(command, outside=outside, writable=(str(self.edit),))
        self.assertTrue(allow, reason)
        self.assertIsNotNone(run)
        return _bash(run, str(self.edit))

    # --- the walls themselves ------------------------------------------------------------------

    def test_code_walls_shape(self):
        walls = shell_rules.code_walls()
        self.assertIn(os.path.realpath(str(shell_rules.REPO)), walls["write_paths"])
        for d in shell_rules._python_dirs():
            self.assertIn(d, walls["write_paths"])
        self.assertIn(os.path.realpath(str(paths.turn_cwd())), walls["write_paths"])
        self.assertEqual(walls["write_paths"], sorted(walls["write_paths"]))
        self.assertIn(os.path.dirname(os.path.realpath(str(shell_rules.REPO))), walls["ancestors"])
        self.assertEqual(walls["home"], os.path.realpath(str(Path.home())))

    def test_turn_cwd_is_private_and_not_code(self):
        cwd = paths.turn_cwd()
        self.assertTrue(cwd.is_dir())
        self.assertEqual(cwd, paths.DATA / "run" / "turns")
        self.assertEqual(os.stat(cwd).st_mode & 0o777, 0o700)
        self.assertNotIn(paths.CODE.resolve(), [cwd.resolve(), *cwd.resolve().parents])

    # --- sandboxed at every level ---------------------------------------------------------------

    @unittest.skipUnless(shell_rules.can_sandbox(), "sandbox-exec")
    def test_full_access_before_outside_runs_sandboxed_with_code_denies(self):
        allow, _, run = shell_rules.plan("touch x", outside=False, writable=(str(self.edit),))
        self.assertTrue(allow)
        self.assertIsNotNone(run)
        profile = _profile_of(run)
        self.assertIn(os.path.realpath(str(shell_rules.REPO)), profile)
        self.assertNotIn("(deny network-outbound (remote ip))", profile)  # the internet stays
        self.assertNotIn("(deny file-write*)(allow file-write*", profile)  # writes as today

    @unittest.skipUnless(shell_rules.can_sandbox(), "sandbox-exec")
    def test_profile_file_holds_code_denies(self):
        pdir = Path(self.tmp.name) / "profiles"
        _, _, run = shell_rules.plan("touch x", writable=(str(self.edit),), profile_dir=str(pdir))
        self.assertIn(" -f ", run)
        self.assertIn(os.path.realpath(str(shell_rules.REPO)), _profile_of(run))

    @unittest.skipUnless(shell_rules.can_sandbox(), "sandbox-exec")
    def test_cannot_create_change_or_rename_in_code_folder(self):
        self.fake_code()
        r, parent = shlex.quote(str(self.repo)), shlex.quote(str(self.parent))
        for outside in (False, True):
            for cmd in (f"touch {r}/new.txt", f"echo x >> {r}/existing.py", f"rm -f {r}/existing.py",
                        f"mv {r} {r}-moved", f"mv {parent} {parent}-moved", f"mkdir {r}/core/sub"):
                proc = self.run_planned(cmd, outside=outside)
                self.assertNotEqual(proc.returncode, 0, cmd)
                self.assertFalse((self.repo / "new.txt").exists(), cmd)
                self.assertEqual((self.repo / "existing.py").read_text(), "keep\n", cmd)
                self.assertTrue(self.repo.is_dir(), cmd)
                self.assertTrue(self.parent.is_dir(), cmd)
                self.assertFalse((self.repo / "core" / "sub").exists(), cmd)

    @unittest.skipUnless(shell_rules.can_sandbox(), "sandbox-exec")
    def test_cannot_write_osctl_python(self):
        self.fake_code()
        target = self.pydir / "site.py"
        for outside in (False, True):
            proc = self.run_planned(f"touch {shlex.quote(str(target))}", outside=outside)
            self.assertNotEqual(proc.returncode, 0)
            self.assertFalse(target.exists())

    @unittest.skipUnless(shell_rules.can_sandbox(), "sandbox-exec")
    def test_cannot_write_turns_folder(self):
        target = paths.turn_cwd() / "dashboard"
        proc = self.run_planned(f"mkdir {shlex.quote(str(target))}")
        self.assertNotEqual(proc.returncode, 0)
        self.assertFalse(target.exists())

    @unittest.skipUnless(shell_rules.can_sandbox(), "sandbox-exec")
    def test_edit_folder_still_writable(self):
        self.fake_code()
        for outside in (False, True):
            name = f"ok-{outside}.txt"
            proc = self.run_planned(f"touch {shlex.quote(str(self.edit / name))} && echo hi > rel-{outside}.txt",
                                    outside=outside)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertTrue((self.edit / name).exists())
            self.assertTrue((self.edit / f"rel-{outside}.txt").exists())

    def test_network_rules_unchanged_before_outside(self):
        code = shell_rules.code_walls()
        profile = shell_rules.sandbox_profile((), outside=False, code=code)
        self.assertNotIn("(deny network-outbound (remote ip))", profile)
        self.assertNotIn("(deny file-write*)(allow", profile)
        self.assertIn(f"(deny file-write* (subpath {shell_rules._sb_str(code['write_paths'][0])}))", profile)
        after = shell_rules.sandbox_profile((), outside=True, code=code)
        self.assertIn("(deny network-outbound (remote ip))", after)
        self.assertIn(f"(deny file-write* (subpath {shell_rules._sb_str(code['write_paths'][0])}))", after)

    def test_browser_walls_carry_the_code_walls(self):
        walls = shell_rules.browser_walls(paths.DATA)
        code = shell_rules.code_walls()
        for p in code["write_paths"]:
            self.assertIn(p, walls["write_paths"])
        for a in code["ancestors"]:
            self.assertIn(a, walls["ancestors"])

    # --- no sandbox: the command check ----------------------------------------------------------

    def test_no_sandbox_refuses_write_into_code(self):
        repo = shell_rules.REPO
        with mock.patch.object(shell_rules, "can_sandbox", return_value=False):
            allow, reason, run = shell_rules.plan(f"rm -rf {repo}/core", outside=False)
            self.assertFalse(allow)
            self.assertEqual(reason, shell_rules.CODE_OFF)
            self.assertIsNone(run)
            self.assertTrue(shell_rules.plan(f"cat {repo}/README.md")[0])
            self.assertFalse(shell_rules.plan(f"echo x > {repo}/a")[0])
            self.assertFalse(shell_rules.plan(f"cat {repo}/README.md > {repo}/b")[0])
            ok = shell_rules.plan("touch notes.txt", writable=(str(self.edit),))
            self.assertEqual(ok, (True, "", None))

    def test_writes_code(self):
        repo = os.path.realpath(str(shell_rules.REPO))
        parent = os.path.dirname(repo)
        edit = str(self.edit)
        writes = [f"touch {repo}/x", f"echo x >{repo}/x", f"echo x 2>> {repo}/log", f"cp a {repo}/b",
                  f"sed -i '' s/a/b/ {repo}/core/paths.py", f"mv {repo} /tmp/x", f"mv {parent} /tmp/x",
                  f"dd if=a of={repo}/x", f"git -C {repo} commit -m x", f"cd {repo} && touch x",
                  f"find {repo} -delete", f"ls && rm -rf {repo}/core", "touch core/x",
                  f"python3 -c 1 > {repo}/x", f"tee {repo}/x"]
        for cmd in writes:
            cwd = repo if cmd == "touch core/x" else edit
            self.assertTrue(shell_rules.writes_code(cmd, cwd), cmd)
        reads = [f"cat {repo}/README.md", f"ls {repo}", f"grep -r x {repo}", f"git -C {repo} status",
                 f"git -C {repo} log", f"diff {repo}/a {repo}/b", f"head {repo}/x | wc -l", "touch notes.txt",
                 f"rm -rf {edit}/build", f"cat {repo}/x > {edit}/copy", f"find {repo} -name '*.py'"]
        for cmd in reads:
            self.assertFalse(shell_rules.writes_code(cmd, edit), cmd)
        home_repo = "~/" + os.path.relpath(repo, os.path.expanduser("~")) if repo.startswith(
            os.path.expanduser("~") + "/") else None
        if home_repo:
            self.assertTrue(shell_rules.writes_code(f"touch {home_repo}/x", edit))
            self.assertTrue(shell_rules.writes_code(f"touch $HOME/{home_repo[2:]}/x", edit))

    def test_osctl_form_still_rewritten(self):
        run = shell_rules.plan("python3 -m dashboard.osctl tree")[2]
        self.assertIn("-I -c", run)
        self.assertNotIn(shell_rules.SANDBOX_EXEC, run)

    # --- engine and gate file tools -------------------------------------------------------------

    def test_gate_may_write_refuses_code_even_in_edit_folder(self):
        tok = taint.GATES.mint(taint.Turn(), writable=(str(shell_rules.REPO),))
        self.addCleanup(taint.GATES.revoke, tok)
        self.assertFalse(taint.GATES.may_write(tok, str(shell_rules.REPO / "core" / "x.py"))[0])
        self.assertFalse(taint.GATES.may_write(tok, str(paths.turn_cwd() / "x.py"))[0])
        tok2 = taint.GATES.mint(taint.Turn(), writable=(str(self.edit),))
        self.addCleanup(taint.GATES.revoke, tok2)
        self.assertTrue(taint.GATES.may_write(tok2, str(self.edit / "x.py"))[0])

    def test_claude_code_denies_edit_write_in_code(self):
        from dashboard.engines.claude_code import ClaudeCodeEngine
        repo = os.path.realpath(str(shell_rules.REPO))
        for policy in (TurnPolicy(), TurnPolicy(read_dirs=(str(self.edit),), edit_dirs=(str(self.edit),))):
            argv = ClaudeCodeEngine().build_cmd(chat_session.ChatSession(repo_dir=str(paths.turn_cwd()), rail="R"),
                                                policy, None)
            denied = argv[argv.index("--disallowedTools") + 1:]
            self.assertIn(f"Edit(/{repo}/**)", denied)
            self.assertIn(f"Write(/{repo}/**)", denied)
            for d in shell_rules._python_dirs():
                self.assertIn(f"Write(/{d}/**)", denied)
            for lit in shell_rules.code_walls()["literals"]:
                self.assertIn(f"Edit(/{lit})", denied)
            self.assertIn(f"Write(/{os.path.realpath(str(Path.home() / '.zshrc'))})", denied)
            log = f"Read(/{paths.CODE.resolve() / 'dashboard' / 'server.log'})"
            self.assertIn(log, denied)

    def test_codex_writable_roots_never_hold_code(self):
        from dashboard.engines.codex import CodexEngine
        repo = str(shell_rules.REPO)
        policy = TurnPolicy(shell=True, level="full", read_dirs=(repo, str(self.edit)),
                            edit_dirs=(repo, str(self.edit), str(shell_rules.REPO / "core")))
        tokens = TurnTokens(gate_token="t", gate_port=8765)
        self.assertEqual(CodexEngine.shell_dirs(policy, tokens), (str(self.edit),))
        sess = SimpleNamespace(rail="r", model=None, repo_dir=str(paths.turn_cwd()), _started=False, shared=False,
                               session_id=str(uuid.uuid4()), engine_state={})
        argv = CodexEngine().build_cmd(sess, policy, None, tokens)
        roots = next(a for a in argv if a.startswith("sandbox_workspace_write.writable_roots="))
        self.assertIn(str(self.edit), roots)
        self.assertNotIn(repo, roots)
        only_code = TurnPolicy(shell=True, level="full", read_dirs=(repo,), edit_dirs=(repo,))
        self.assertEqual(CodexEngine.shell_dirs(only_code, tokens), ())

    def test_api_engine_write_tool_refuses_code(self):
        from dashboard.engines import api_tools
        self.fake_code()
        policy = TurnPolicy(read_dirs=(str(self.repo),), edit_dirs=(str(self.repo),))
        box = api_tools.ToolBox(policy, paths.DATA, TurnTokens(), "api", {})
        text, err = box.run("write_file", {"path": str(self.repo / "core" / "x.py"), "content": "x"})
        self.assertTrue(err, text)
        self.assertFalse((self.repo / "core" / "x.py").exists())
        text, err = box.run("edit_file", {"path": str(self.repo / "existing.py"), "old_text": "keep", "new_text": "x"})
        self.assertTrue(err, text)
        self.assertEqual((self.repo / "existing.py").read_text(), "keep\n")

    def test_api_engine_shell_passes_its_folder(self):
        from dashboard.engines import api_tools
        self.fake_code()
        turn = taint.Turn()
        tok = taint.GATES.mint(turn)
        self.addCleanup(taint.GATES.revoke, tok)
        policy = TurnPolicy(shell=True, level="full", read_dirs=(str(self.edit),), edit_dirs=(str(self.edit),))
        box = api_tools.ToolBox(policy, paths.DATA, TurnTokens(gate_token=tok, gate_port=8765), "api", {})
        with mock.patch.object(shell_rules, "can_sandbox", return_value=False):
            text, err = box.run("run_command", {"command": f"touch {shlex.quote(str(self.repo))}/x"})
        self.assertTrue(err)
        self.assertEqual(text, shell_rules.CODE_OFF)
        self.assertFalse((self.repo / "x").exists())

    # --- the chat session's working folder ------------------------------------------------------

    def test_chat_session_cwd_is_not_code(self):
        import dashboard.server as server
        sess = server.new_chat_session()
        self.assertEqual(Path(sess.repo_dir), paths.turn_cwd())
        self.assertNotEqual(Path(sess.repo_dir).resolve(), paths.CODE.resolve())
        back = server._reopen({"id": str(uuid.uuid4())}, None, None)
        self.assertEqual(Path(back.repo_dir), paths.turn_cwd())

    def test_non_shell_turn_env_finds_osctl(self):
        from dashboard.engines.claude_code import ClaudeCodeEngine
        env = ClaudeCodeEngine().turn_env(TurnPolicy(), TurnTokens())
        self.assertEqual(env["PYTHONPATH"].split(os.pathsep)[0], str(paths.CODE))
        env = ClaudeCodeEngine().turn_env(TurnPolicy(), TurnTokens(), repo_dir="/elsewhere")
        self.assertEqual(env["PYTHONPATH"].split(os.pathsep)[0], str(paths.CODE))

    # --- review round 1 -------------------------------------------------------------------------

    def _sandbox(self, profile, command):
        return subprocess.run([shell_rules.SANDBOX_EXEC, "-p", profile, "/bin/bash", "-c", command],
                              cwd=str(self.edit), capture_output=True, text=True, timeout=30)

    def _python_link(self):
        """bin/python3 -> ../hop/python3 -> cellar/python3.12: the executable as a link chain."""
        base = Path(os.path.realpath(self.tmp.name))
        cellar, hop, bindir = base / "cellar", base / "hop", base / "bin"
        for d in (cellar, hop, bindir):
            d.mkdir()
        real = cellar / "python3.12"
        real.write_text("#!/bin/sh\n")
        (hop / "python3").symlink_to(real)
        (bindir / "python3").symlink_to("../hop/python3")
        return bindir / "python3", hop / "python3", real

    def test_python_executable_links_are_walled(self):
        link, hop, real = self._python_link()
        with mock.patch.object(shell_rules.sys, "executable", str(link)):
            walls = shell_rules.code_walls()
        for p in (link, hop, real):
            self.assertIn(str(p), walls["literals"])
        self.assertIn(str(link.parent), walls["ancestors"])
        self.assertNotIn(str(link.parent), walls["write_paths"])  # the bin folder itself stays usable

    @unittest.skipUnless(shell_rules.can_sandbox(), "sandbox-exec")
    def test_python_executable_link_cannot_be_swapped(self):
        self.fake_code()
        link, hop, real = self._python_link()
        evil = self.edit / "evil"
        evil.write_text("#!/bin/sh\n")
        q = shlex.quote
        with mock.patch.object(shell_rules.sys, "executable", str(link)):
            for outside in (False, True):
                for cmd in (f"ln -sf {q(str(evil))} {q(str(link))}", f"rm -f {q(str(link))}",
                            f"mv {q(str(link))} {q(str(link))}.x", f"rm -f {q(str(hop))}",
                            f"mv {q(str(link.parent))} {q(str(link.parent))}-moved"):
                    proc = self.run_planned(cmd, outside=outside)
                    self.assertNotEqual(proc.returncode, 0, cmd)
                    self.assertEqual(os.readlink(link), "../hop/python3", cmd)
                    self.assertEqual(os.readlink(hop), str(real), cmd)
            proc = self.run_planned(f"touch {q(str(link.parent / 'other-tool'))}")
            self.assertEqual(proc.returncode, 0, proc.stderr)  # brief and brew still write the bin folder

    def test_startup_files_and_launch_agents_are_walled(self):
        home = Path.home()
        walls = shell_rules.code_walls()
        for name in shell_rules.STARTUP_FILES:
            self.assertIn(os.path.realpath(str(home / name)), walls["literals"])
        self.assertIn(os.path.realpath(str(home / "Library" / "LaunchAgents")), walls["write_paths"])
        edit = str(self.edit)
        for cmd in ("echo x >> ~/.zshrc", "cp a ~/Library/LaunchAgents/x.plist", "touch $HOME/.bash_profile",
                    "rm ~/.profile", "mv ~/Library/LaunchAgents /tmp/x"):
            self.assertTrue(shell_rules.writes_code(cmd, edit), cmd)
        self.assertFalse(shell_rules.writes_code("cat ~/.zshrc", edit))

    @unittest.skipUnless(shell_rules.can_sandbox(), "sandbox-exec")
    def test_startup_files_and_launch_agents_cannot_be_written(self):
        home = Path(os.path.realpath(self.tmp.name)) / "home"
        (home / "Library" / "LaunchAgents").mkdir(parents=True)
        (home / ".zshrc").write_text("keep\n")
        code = shell_rules.code_walls(home=home)
        q = shlex.quote
        for outside in (False, True):
            profile = shell_rules.sandbox_profile((str(self.edit), str(home)), outside=outside, code=code)
            for cmd in (f"echo x >> {q(str(home / '.zshrc'))}", f"touch {q(str(home / '.bashrc'))}",
                        f"touch {q(str(home / 'Library/LaunchAgents/x.plist'))}",
                        f"mv {q(str(home / 'Library/LaunchAgents'))} {q(str(home / 'la'))}"):
                self.assertNotEqual(self._sandbox(profile, cmd).returncode, 0, cmd)
            self.assertEqual(self._sandbox(profile, f"touch {q(str(home / 'notes.txt'))}").returncode, 0)
        self.assertEqual((home / ".zshrc").read_text(), "keep\n")
        self.assertFalse((home / ".bashrc").exists())
        self.assertEqual(os.listdir(home / "Library" / "LaunchAgents"), [])

    def test_writes_code_sees_through_shells_globs_and_options(self):
        repo = os.path.realpath(str(shell_rules.REPO))
        parent, name = os.path.split(repo)
        edit = str(self.edit)
        q = shlex.quote
        for cmd in (f"bash -c {q('rm ' + repo + '/x')}", f"sh -c {q('rm ' + repo + '/x')}",
                    f"zsh -lc {q('touch ' + repo + '/x')}", f"eval {q('rm -rf ' + repo + '/core')}",
                    f"bash -c {q('bash -c ' + q('rm ' + repo + '/x'))}",
                    f"rm -rf {parent}/{name[:3]}*", f"rm -rf {parent}/{name[:-2]}?{name[-1]}",
                    f"rm -rf {parent}/[a-z]*", f"gcc -o{repo}/x a.c", f"git diff --output={repo}/x",
                    f"git log -o {repo}/x", f"sort -o{repo}/x a"):
            self.assertTrue(shell_rules.writes_code(cmd, edit), cmd)
        for cmd in (f"ls {repo}/*", f"cat {parent}/{name[:3]}*/README.md", f"rm -rf {edit}/*",
                    f"bash -c {q('ls ' + repo)}", "gcc -o build/x a.c", f"grep -r x {repo}"):
            self.assertFalse(shell_rules.writes_code(cmd, edit), cmd)

    def test_gate_checks_the_engine_cwd(self):
        tok = taint.GATES.mint(taint.Turn(), writable=(str(self.edit),))
        self.addCleanup(taint.GATES.revoke, tok)
        with mock.patch.object(shell_rules, "can_sandbox", return_value=False):
            self.assertEqual(taint.GATES.shell(tok, "rm -rf core", cwd=str(shell_rules.REPO))[:2],
                             (False, shell_rules.CODE_OFF))
            self.assertTrue(taint.GATES.shell(tok, "rm -rf build", cwd=str(self.edit))[0])

    def test_hook_sends_the_engine_cwd(self):
        import io
        import json
        from dashboard import turn_hook
        sent = []

        class Resp(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def urlopen(req, timeout=None):
            sent.append(json.loads(req.data))
            return Resp(b'{"allow": true}')
        event = {"tool_name": "Bash", "cwd": "/work/acme", "tool_input": {"command": "ls"}}
        env = {turn_hook.TOKEN_ENV: "t", turn_hook.URL_ENV: "http://127.0.0.1:1/api/internal/gate"}
        with mock.patch.dict(os.environ, env), mock.patch.object(turn_hook.sys, "stdin", io.StringIO(json.dumps(event))), \
                mock.patch.object(turn_hook.urllib.request, "urlopen", urlopen):
            turn_hook.main()
        self.assertEqual(sent[0]["cwd"], "/work/acme")

    def test_codex_drops_an_edit_folder_holding_code(self):
        from dashboard.engines.codex import CodexEngine
        outer = Path(os.path.realpath(self.tmp.name)) / "outer"
        (outer / "py").mkdir(parents=True)
        with mock.patch.object(shell_rules, "_python_dirs", return_value=[str(outer / "py")]):
            policy = TurnPolicy(shell=True, level="full", read_dirs=(str(outer), str(self.edit)),
                                edit_dirs=(str(outer), str(self.edit)))
            self.assertEqual(CodexEngine.shell_dirs(policy, TurnTokens(gate_token="t", gate_port=8765)),
                             (str(self.edit),))

    def test_folders_refuse_python_dirs_and_their_parents(self):
        from core import folders
        outer = Path(os.path.realpath(self.tmp.name)) / "outer"
        (outer / "py" / "site").mkdir(parents=True)
        with mock.patch.object(shell_rules, "_python_dirs", return_value=[str(outer / "py")]):
            for bad in (outer, outer / "py", outer / "py" / "site"):
                with self.assertRaises(folders.FolderError, msg=str(bad)):
                    folders.normalize_path(str(bad), root=paths.DATA)
            self.assertEqual(folders.normalize_path(str(self.edit), root=paths.DATA), str(self.edit))

    def test_code_walls_are_cached_per_process(self):
        calls = []
        with mock.patch.object(shell_rules, "_python_dirs", side_effect=lambda: calls.append(1) or [str(self.pydir)]):
            first = shell_rules.code_walls()
            shell_rules.in_code("/x")
            shell_rules.in_code("/y")
            self.assertEqual(shell_rules.code_walls(), first)
            self.assertEqual(len(calls), 1)
            with mock.patch.object(shell_rules, "REPO", self.repo):
                self.assertIn(str(self.repo), shell_rules.code_walls()["write_paths"])

    # --- review round 2 -------------------------------------------------------------------------

    def test_git_config_and_fish_are_walled(self):
        home = Path.home()
        walls = shell_rules.code_walls()
        self.assertIn(os.path.realpath(str(home / ".gitconfig")), walls["literals"])
        for sub in (".config/git", ".config/fish"):
            self.assertIn(os.path.realpath(str(home / sub)), walls["write_paths"])
        edit = str(self.edit)
        for cmd in ("echo x >> ~/.gitconfig", "touch ~/.config/git/config", "cp a ~/.config/fish/config.fish",
                    "git config --global core.fsmonitor x", "git config --system a.b c"):
            self.assertTrue(shell_rules.writes_code(cmd, edit), cmd)
        for cmd in ("git config --get user.name", "git config user.name x", "cat ~/.gitconfig"):
            self.assertFalse(shell_rules.writes_code(cmd, edit), cmd)

    @unittest.skipUnless(shell_rules.can_sandbox(), "sandbox-exec")
    def test_git_config_and_fish_cannot_be_written(self):
        home = Path(os.path.realpath(self.tmp.name)) / "home"
        (home / ".config").mkdir(parents=True)
        code = shell_rules.code_walls(home=home)
        q = shlex.quote
        for outside in (False, True):
            profile = shell_rules.sandbox_profile((str(self.edit), str(home)), outside=outside, code=code)
            for cmd in (f"echo x >> {q(str(home / '.gitconfig'))}", f"mkdir -p {q(str(home / '.config/git'))}",
                        f"mkdir -p {q(str(home / '.config/fish'))}"):
                self.assertNotEqual(self._sandbox(profile, cmd).returncode, 0, cmd)
            self.assertEqual(self._sandbox(profile, f"mkdir -p {q(str(home / '.config/other'))}").returncode, 0)
        self.assertFalse((home / ".gitconfig").exists())
        self.assertFalse((home / ".config" / "git").exists())

    def test_output_option_only_where_it_writes(self):
        repo = os.path.realpath(str(shell_rules.REPO))
        edit = str(self.edit)
        for cmd in (f"ls -o {repo}", f"grep -o x {repo}/README.md", f"find {repo} -name a -o -name b",
                    f"rg -o x {repo}", f"ls -o{repo}"):
            self.assertFalse(shell_rules.writes_code(cmd, edit), cmd)
        self.assertTrue(shell_rules.writes_code(f"git diff --output={repo}/x", edit))

    def test_attached_relative_option_path(self):
        repo = os.path.realpath(str(shell_rules.REPO))
        parent, name = os.path.split(repo)
        other = os.path.join(parent, "other-app")
        for cmd in (f"gcc -o../{name}/x a.c", f"sort -o../{name}/x a", "cc -o./x a.c"):
            cwd = repo if cmd.startswith("cc") else other
            self.assertTrue(shell_rules.writes_code(cmd, cwd), cmd)
        self.assertFalse(shell_rules.writes_code("gcc -o../build/x a.c", str(self.edit)))

    def test_link_chain_is_read_again_each_call(self):
        link, hop, real = self._python_link()
        newer = real.parent / "python3.13"
        newer.write_text("#!/bin/sh\n")
        with mock.patch.object(shell_rules.sys, "executable", str(link)):
            self.assertNotIn(str(newer), shell_rules.code_walls()["literals"])
            hop.unlink()
            hop.symlink_to(newer)  # an upgrade moves the link on, the server keeps running
            self.assertIn(str(newer), shell_rules.code_walls()["literals"])

    # --- round 3: Approve for me keeps Claude Code's reviewer, its shell runs sandboxed ------------

    def _stub_gate(self, answer):
        """A local gate answering `answer(body)`; returns its URL and the bodies it got."""
        import http.server
        import json
        import threading
        got = []

        class H(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                got.append(body)
                raw = json.dumps(answer(body)).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, *a):
                pass
        srv = http.server.HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        return f"http://127.0.0.1:{srv.server_address[1]}/api/internal/gate", got

    def _wrapper(self, env_extra, script):
        wrapper = shell_rules.review_shell()
        env = {k: v for k, v in os.environ.items() if not k.startswith("LEOBOT_GATE")}
        env.update(env_extra)
        return subprocess.run([wrapper, "-c", script], env=env, cwd=str(self.edit), capture_output=True, text=True,
                              timeout=30)

    def test_review_shell_is_walled_and_named_for_claude_code(self):
        wrapper = shell_rules.review_shell()
        self.assertEqual(os.path.basename(wrapper), "zsh")  # Claude Code takes a CLAUDE_CODE_SHELL naming zsh
        self.assertTrue(os.access(wrapper, os.X_OK))
        self.assertTrue(shell_rules.in_code(wrapper))
        self.assertEqual(os.stat(os.path.dirname(wrapper)).st_mode & 0o777, 0o700)

    def test_review_shell_fails_closed(self):
        mark = self.edit / "ran"
        proc = self._wrapper({}, f"touch {shlex.quote(str(mark))}")
        self.assertEqual(proc.returncode, 126)
        url, _ = self._stub_gate(lambda body: {"allow": False, "reason": "no"})
        proc = self._wrapper({"LEOBOT_GATE_TOKEN": "t", "LEOBOT_GATE_URL": url}, f"touch {shlex.quote(str(mark))}")
        self.assertEqual(proc.returncode, 126)
        proc = self._wrapper({"LEOBOT_GATE_TOKEN": "t", "LEOBOT_GATE_URL": "http://127.0.0.1:9/x"},
                             f"touch {shlex.quote(str(mark))}")
        self.assertEqual(proc.returncode, 126)
        url, _ = self._stub_gate(lambda body: {"allow": True, "profile": "relative.sb"})
        proc = self._wrapper({"LEOBOT_GATE_TOKEN": "t", "LEOBOT_GATE_URL": url}, f"touch {shlex.quote(str(mark))}")
        self.assertEqual(proc.returncode, 126)
        self.assertFalse(mark.exists())

    @unittest.skipUnless(shell_rules.can_sandbox(), "sandbox-exec")
    def test_review_shell_runs_inside_the_gate_profile(self):
        self.fake_code()
        pdir = Path(paths.SANDBOX_DIR) / "profiles-test"  # the review shell takes a profile from there only
        tok = taint.GATES.mint(taint.Turn(), writable=(str(self.edit),), review=True)
        self.addCleanup(taint.GATES.revoke, tok)
        url, got = self._stub_gate(lambda body: dict(zip(("allow", "reason", "profile"),
                                                         taint.GATES.shell_profile(tok, profile_dir=str(pdir)))))
        q = shlex.quote
        proc = self._wrapper({"LEOBOT_GATE_TOKEN": tok, "LEOBOT_GATE_URL": url},
                             f"touch {q(str(self.repo / 'x'))}; touch {q(str(self.edit / 'ok'))}")
        self.assertFalse((self.repo / "x").exists())
        self.assertTrue((self.edit / "ok").exists(), proc.stderr)
        self.assertEqual(got[0]["tool"], "Bash-shell")

    def test_review_shell_redeems_an_osctl_ticket(self):
        mark = self.edit / "osctl-ran"
        url, got = self._stub_gate(lambda body: {"allow": True, "command": f"touch {shlex.quote(str(mark))}"}
                                   if body.get("ticket") == "a" * 32 else {"allow": False})
        proc = self._wrapper({"LEOBOT_GATE_TOKEN": "t", "LEOBOT_GATE_URL": url},
                             "source x && eval ': leobot-osctl-ticket " + "a" * 32 + "' < /dev/null")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(mark.exists())
        proc = self._wrapper({"LEOBOT_GATE_TOKEN": "t", "LEOBOT_GATE_URL": url},
                             ": leobot-osctl-ticket " + "b" * 32)
        self.assertEqual(proc.returncode, 126)  # a ticket the gate did not issue runs nothing

    def test_review_shell_hands_commands_the_real_shell(self):
        """$SHELL inside a command names the owner's shell, not this wrapper (no nested sandbox,
        no extra gate call), and CLAUDE_CODE_SHELL is gone."""
        out = self.edit / "env-ticket"
        url, _ = self._stub_gate(lambda body: {"allow": True, "command": f"env > {shlex.quote(str(out))}"})
        wrapper = shell_rules.review_shell()
        for real, want in (("/bin/bash", "/bin/bash"), ("", "/bin/zsh"), ("relative/sh", "/bin/zsh"),
                           (wrapper, "/bin/zsh")):
            proc = self._wrapper({"LEOBOT_GATE_TOKEN": "t", "LEOBOT_GATE_URL": url, "SHELL": wrapper,
                                  "CLAUDE_CODE_SHELL": wrapper, "LEOBOT_REAL_SHELL": real},
                                 "eval ': leobot-osctl-ticket " + "c" * 32 + "' < /dev/null")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            env = dict(line.split("=", 1) for line in out.read_text().splitlines() if "=" in line)
            self.assertEqual(env.get("SHELL"), want, real)
            self.assertNotIn("CLAUDE_CODE_SHELL", env)
            self.assertNotIn("LEOBOT_REAL_SHELL", env)

    @unittest.skipUnless(shell_rules.can_sandbox(), "sandbox-exec")
    def test_review_shell_sandboxed_command_gets_the_real_shell(self):
        self.fake_code()
        pdir = Path(paths.SANDBOX_DIR) / "profiles-test"
        tok = taint.GATES.mint(taint.Turn(), writable=(str(self.edit),), review=True)
        self.addCleanup(taint.GATES.revoke, tok)
        url, _ = self._stub_gate(lambda body: dict(zip(("allow", "reason", "profile"),
                                                       taint.GATES.shell_profile(tok, profile_dir=str(pdir)))))
        out = self.edit / "shell-name"
        wrapper = shell_rules.review_shell()
        proc = self._wrapper({"LEOBOT_GATE_TOKEN": tok, "LEOBOT_GATE_URL": url, "SHELL": wrapper,
                              "CLAUDE_CODE_SHELL": wrapper, "LEOBOT_REAL_SHELL": "/bin/bash"},
                             f'echo "$SHELL ${{CLAUDE_CODE_SHELL:-none}}" > {shlex.quote(str(out))}')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(out.read_text().strip(), "/bin/bash none")

    def test_turn_env_passes_the_owners_shell_to_the_review_shell(self):
        from dashboard.engines.claude_code import ClaudeCodeEngine
        tokens = TurnTokens(gate_token="t", gate_port=8765)
        mk = lambda level: TurnPolicy(shell=True, level=level, read_dirs=(str(self.edit),),  # noqa: E731
                                      edit_dirs=(str(self.edit),))
        with mock.patch.dict(os.environ, {"SHELL": "/bin/bash", "LEOBOT_REAL_SHELL": "/tmp/stale"}):
            full = ClaudeCodeEngine().turn_env(mk("full"), tokens)
            self.assertNotIn("LEOBOT_REAL_SHELL", full)
            if shell_rules.can_sandbox():
                env = ClaudeCodeEngine().turn_env(mk("approve"), tokens)
                self.assertEqual(env["LEOBOT_REAL_SHELL"], "/bin/bash")
                self.assertEqual(env["SHELL"], shell_rules.review_shell())

    def test_gate_review_tokens(self):
        turn = taint.Turn()
        tok = taint.GATES.mint(turn, writable=(str(self.edit),), review=True)
        full = taint.GATES.mint(turn, writable=(str(self.edit),))
        self.addCleanup(taint.GATES.revoke, tok)
        self.addCleanup(taint.GATES.revoke, full)
        self.assertTrue(taint.GATES.review(tok))
        self.assertFalse(taint.GATES.review(full))
        self.assertEqual(taint.GATES.shell(tok, "npm test"), (True, "", None))
        self.assertEqual(taint.GATES.shell(tok, "rm -rf /")[:2], (False, shell_rules.NEVER))
        run = taint.GATES.shell(tok, "python3 -m dashboard.osctl tree")[2]
        nonce = run.split()[-1]
        self.assertIsNone(taint.GATES.redeem(full, nonce))  # bound to its token
        self.assertIn("-I -c", taint.GATES.redeem(tok, nonce))
        self.assertIsNone(taint.GATES.redeem(tok, nonce))
        self.assertFalse(taint.GATES.shell_profile(full)[0])
        if shell_rules.can_sandbox():
            allow, _, profile = taint.GATES.shell_profile(tok)
            self.assertTrue(allow)
            self.assertIn(os.path.realpath(str(shell_rules.REPO)), Path(profile).read_text())

    def test_turn_env_names_the_review_shell_on_approve_only(self):
        from dashboard.engines.claude_code import ClaudeCodeEngine
        tokens = TurnTokens(gate_token="t", gate_port=8765)
        mk = lambda level: TurnPolicy(shell=True, level=level, read_dirs=(str(self.edit),),  # noqa: E731
                                      edit_dirs=(str(self.edit),))
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_SHELL": "/tmp/evil/zsh", "CLAUDE_CODE_SHELL_PREFIX": "x"}):
            env = ClaudeCodeEngine().turn_env(mk("approve"), tokens)
            if shell_rules.can_sandbox():
                self.assertEqual(env["CLAUDE_CODE_SHELL"], shell_rules.review_shell())
            self.assertNotIn("CLAUDE_CODE_SHELL_PREFIX", env)
            for env in (ClaudeCodeEngine().turn_env(mk("full"), tokens),
                        ClaudeCodeEngine().turn_env(mk("approve"), TurnTokens())):
                self.assertNotIn("CLAUDE_CODE_SHELL", env)
                self.assertNotIn("CLAUDE_CODE_SHELL_PREFIX", env)

    def test_hook_is_silent_for_a_review_answer(self):
        import io
        import json
        from dashboard import turn_hook

        def run(answer):
            class Resp(io.BytesIO):
                def __enter__(self):
                    return self

                def __exit__(self, *a):
                    return False
            out = io.StringIO()
            event = {"tool_name": "Bash", "tool_input": {"command": "ls"}}
            env = {turn_hook.TOKEN_ENV: "t", turn_hook.URL_ENV: "http://127.0.0.1:1/api/internal/gate"}
            with mock.patch.dict(os.environ, env), mock.patch.object(turn_hook.sys, "stdin", io.StringIO(json.dumps(event))), \
                    mock.patch.object(turn_hook.urllib.request, "urlopen", lambda *a, **k: Resp(json.dumps(answer).encode())), \
                    mock.patch.object(turn_hook.sys, "stdout", out):
                turn_hook.main()
            return out.getvalue()
        self.assertEqual(run({"allow": True, "review": True}), "")
        full = json.loads(run({"allow": True, "command": "sandbox-exec -f p /bin/bash -c ls"}))
        self.assertEqual(full["hookSpecificOutput"]["permissionDecision"], "allow")

    # --- round 4: the review shell fails closed if Claude Code stops using it ---------------------

    def _review_token(self):
        tok = taint.GATES.mint(taint.Turn(), writable=(str(self.edit),), review=True)
        self.addCleanup(taint.GATES.revoke, tok)
        return tok

    @unittest.skipUnless(shell_rules.can_sandbox(), "sandbox-exec")
    def test_review_gate_denies_once_a_command_ran_outside_its_shell(self):
        """A Claude Code that ignores CLAUDE_CODE_SHELL and SHELL: the first command ran with no
        Bash-shell call, so the second (and every later one, osctl too) is denied."""
        tok = self._review_token()
        self.assertEqual(taint.GATES.shell(tok, "echo one", call_id="a"), (True, "", None))
        taint.GATES.ran(tok, "a")  # PostToolUse: it ran, and its shell never asked the gate
        for cmd in ("echo two", "ls", "python3 -m dashboard.osctl tree"):
            allow, reason, run = taint.GATES.shell(tok, cmd, call_id="b")
            self.assertEqual((allow, run), (False, None), cmd)
            self.assertEqual(reason, taint.SHELL_UNCONFIRMED)
        self.assertIn("Approve for me shell is off until Leo Bot is updated", taint.SHELL_UNCONFIRMED)
        self.assertFalse(taint.GATES.shell_profile(tok)[0])  # a late shell call does not reopen it

    @unittest.skipUnless(shell_rules.can_sandbox(), "sandbox-exec")
    def test_review_gate_keeps_going_while_its_shell_runs_each_command(self):
        tok = self._review_token()
        # one command at a time
        self.assertTrue(taint.GATES.shell(tok, "echo one", call_id="a")[0])
        self.assertTrue(taint.GATES.shell_profile(tok)[0])
        taint.GATES.ran(tok, "a")
        self.assertTrue(taint.GATES.shell(tok, "echo two", call_id="b")[0])
        self.assertTrue(taint.GATES.shell_profile(tok)[0])
        taint.GATES.ran(tok, "b")
        # two calls in one response: both checks come before either shell runs
        self.assertTrue(taint.GATES.shell(tok, "echo three", call_id="c")[0])
        self.assertTrue(taint.GATES.shell(tok, "echo four", call_id="d")[0])
        self.assertTrue(taint.GATES.shell_profile(tok)[0])
        self.assertTrue(taint.GATES.shell_profile(tok)[0])
        taint.GATES.ran(tok, "c")
        taint.GATES.ran(tok, "d")
        # the reviewer refuses one: no shell call and no PostToolUse, the next still runs
        self.assertTrue(taint.GATES.shell(tok, "rm -rf build", call_id="e")[0])
        self.assertTrue(taint.GATES.shell(tok, "echo five", call_id="f")[0])
        self.assertTrue(taint.GATES.shell_profile(tok)[0])
        taint.GATES.ran(tok, "f")
        # osctl: the ticket redeem is the shell call
        run = taint.GATES.shell(tok, "python3 -m dashboard.osctl tree", call_id="g")[2]
        self.assertIsNotNone(taint.GATES.redeem(tok, run.split()[-1]))
        taint.GATES.ran(tok, "g")
        self.assertTrue(taint.GATES.shell(tok, "echo six", call_id="h")[0])

    @unittest.skipUnless(shell_rules.can_sandbox(), "sandbox-exec")
    def test_review_gate_judges_a_late_or_unnamed_report_too(self):
        tok = self._review_token()
        self.assertTrue(taint.GATES.shell(tok, "sleep 5 &", call_id="a")[0])
        taint.GATES.ran(tok, None)  # no id: the latest allowed check is the one it ran
        self.assertFalse(taint.GATES.shell(tok, "echo two", call_id="b")[0])
        full = taint.GATES.mint(taint.Turn(), writable=(str(self.edit),))  # Full access: rewritten, never watched
        self.addCleanup(taint.GATES.revoke, full)
        self.assertTrue(taint.GATES.shell(full, "echo one", call_id="a")[0])
        taint.GATES.ran(full, "a")
        self.assertTrue(taint.GATES.shell(full, "echo two", call_id="b")[0])

    @unittest.skipUnless(shell_rules.can_sandbox(), "sandbox-exec")
    def test_review_gate_fails_closed_through_the_server(self):
        """The stub flow over the real gate endpoint, as the hook and the review shell call it."""
        import http.server
        import json
        import threading
        import urllib.request
        from dashboard import server
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        url = f"http://127.0.0.1:{srv.server_address[1]}/api/internal/gate"
        tok = self._review_token()

        def ask(body):
            req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                         headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                return json.loads(resp.read())
        ok = ask({"tool": "Bash", "command": "echo one", "id": "a"})
        self.assertEqual((ok["allow"], ok.get("review")), (True, True))
        self.assertTrue(ask({"tool": "Bash-ran", "id": "a"})["allow"])  # the PostToolUse report
        no = ask({"tool": "Bash", "command": "echo two", "id": "b"})
        self.assertFalse(no["allow"])
        self.assertEqual(no["reason"], taint.SHELL_UNCONFIRMED)

    def test_hook_sends_the_call_id_and_reports_a_run(self):
        import io
        import json
        from dashboard import turn_hook
        sent = []

        class Resp(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def fake_urlopen(req, timeout=None):
            sent.append(json.loads(req.data))
            return Resp(json.dumps({"allow": True, "review": True}).encode())

        def run(event):
            out = io.StringIO()
            env = {turn_hook.TOKEN_ENV: "t", turn_hook.URL_ENV: "http://127.0.0.1:1/api/internal/gate"}
            with mock.patch.dict(os.environ, env), mock.patch.object(turn_hook.sys, "stdin", io.StringIO(json.dumps(event))), \
                    mock.patch.object(turn_hook.urllib.request, "urlopen", fake_urlopen), \
                    mock.patch.object(turn_hook.sys, "stdout", out):
                turn_hook.main()
            return out.getvalue()
        base = {"tool_name": "Bash", "tool_input": {"command": "ls"}, "tool_use_id": "toolu_1"}
        self.assertEqual(run({**base, "hook_event_name": "PreToolUse"}), "")
        self.assertEqual(sent[-1]["id"], "toolu_1")
        self.assertEqual(sent[-1]["tool"], "Bash")
        for name in ("PostToolUse", "PostToolUseFailure"):
            self.assertEqual(run({**base, "hook_event_name": name}), "")  # never a decision after the run
            self.assertEqual(sent[-1], {"tool": "Bash-ran", "id": "toolu_1"})
        with mock.patch.object(turn_hook.urllib.request, "urlopen", side_effect=OSError("down")):
            out = io.StringIO()
            with mock.patch.object(turn_hook.sys, "stdin", io.StringIO(json.dumps({**base, "hook_event_name": "PostToolUse"}))), \
                    mock.patch.dict(os.environ, {turn_hook.TOKEN_ENV: "t", turn_hook.URL_ENV: "http://127.0.0.1:1/x"}), \
                    mock.patch.object(turn_hook.sys, "stdout", out):
                turn_hook.main()
            self.assertEqual(out.getvalue(), "")

    def test_review_settings_report_each_bash_run(self):
        import json
        from dashboard.engines.claude_code import HOOK_COMMAND, gate_settings
        hooks = json.loads(gate_settings(True, True))["hooks"]
        for name in ("PostToolUse", "PostToolUseFailure"):
            self.assertEqual(hooks[name], [{"matcher": "Bash", "hooks": [{"type": "command", "command": HOOK_COMMAND}]}])
        for args in ((True, False), (False, False)):
            self.assertEqual(set(json.loads(gate_settings(*args))["hooks"]), {"PreToolUse"})

    def test_turn_env_sets_shell_to_the_review_shell_too(self):
        from dashboard.engines.claude_code import ClaudeCodeEngine
        tokens = TurnTokens(gate_token="t", gate_port=8765)
        mk = lambda level: TurnPolicy(shell=True, level=level, read_dirs=(str(self.edit),),  # noqa: E731
                                      edit_dirs=(str(self.edit),))
        with mock.patch.dict(os.environ, {"SHELL": "/bin/zsh"}):
            env = ClaudeCodeEngine().turn_env(mk("approve"), tokens)
            if shell_rules.can_sandbox():
                self.assertEqual(env["SHELL"], shell_rules.review_shell())
            self.assertEqual(ClaudeCodeEngine().turn_env(mk("full"), tokens)["SHELL"], "/bin/zsh")

    def test_review_shell_takes_only_the_exact_ticket_command(self):
        mark = self.edit / "osctl-ran"
        nonce = "a" * 32
        url, got = self._stub_gate(lambda body: {"allow": True, "command": f"touch {shlex.quote(str(mark))}"}
                                   if body.get("ticket") == nonce else {"allow": False, "reason": "no"})
        env = {"LEOBOT_GATE_TOKEN": "t", "LEOBOT_GATE_URL": url}
        # what Claude Code 2.1.325 runs for `: leobot-osctl-ticket <nonce>` (verified live)
        exact = ("source /x/snapshot-zsh-1.sh 2>/dev/null || true && \\builtin setopt NO_EXTENDED_GLOB 2>/dev/null"
                 f" || true && eval ': leobot-osctl-ticket {nonce}' < /dev/null && pwd -P >| /tmp/claude-ab12-cwd")
        for script in (f"source x && eval 'echo leobot-osctl-ticket {nonce}' < /dev/null && pwd -P >| /tmp/c",
                       f"source x && eval 'grep leobot-osctl-ticket {nonce} f' < /dev/null && pwd -P >| /tmp/c",
                       f"source x && eval ': leobot-osctl-ticket {nonce}; touch y' < /dev/null && pwd -P >| /tmp/c",
                       f"echo : leobot-osctl-ticket {nonce}"):
            got.clear()
            self._wrapper(env, script)
            self.assertNotIn("ticket", got[0], script)
            self.assertFalse(mark.exists(), script)
        got.clear()
        proc = self._wrapper(env, exact)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(got[0].get("ticket"), nonce)
        self.assertTrue(mark.exists())

    def test_review_shell_takes_a_profile_only_from_the_sandbox_folder(self):
        elsewhere = self.edit / "open.sb"
        elsewhere.write_text("(version 1)(allow default)\n")
        mark = self.edit / "ran"
        url, _ = self._stub_gate(lambda body: {"allow": True, "profile": str(elsewhere)})
        proc = self._wrapper({"LEOBOT_GATE_TOKEN": "t", "LEOBOT_GATE_URL": url}, f"touch {shlex.quote(str(mark))}")
        self.assertEqual(proc.returncode, 126)
        self.assertFalse(mark.exists())
        link = Path(paths.SANDBOX_DIR) / "link-out.sb"
        link.unlink(missing_ok=True)
        link.symlink_to(elsewhere)  # inside by name, outside for real
        self.addCleanup(link.unlink, missing_ok=True)
        url, _ = self._stub_gate(lambda body: {"allow": True, "profile": str(link)})
        proc = self._wrapper({"LEOBOT_GATE_TOKEN": "t", "LEOBOT_GATE_URL": url}, f"touch {shlex.quote(str(mark))}")
        self.assertEqual(proc.returncode, 126)
        self.assertFalse(mark.exists())

    def test_tickets_are_pruned_capped_and_dropped_on_revoke(self):
        tok = self._review_token()
        if not shell_rules.can_sandbox():
            self.skipTest("sandbox-exec")
        osctl = "python3 -m dashboard.osctl tree"
        first = taint.GATES.shell(tok, osctl)[2].split()[-1]
        with mock.patch.object(taint.time, "time", return_value=taint.time.time() + taint.Gates.TICKET_AGE + 1):
            taint.GATES.shell(tok, osctl)  # issuing a ticket purges expired ones
            self.assertNotIn(first, taint.GATES._tickets)
        for _ in range(taint.Gates.TICKETS_PER_TOKEN + 10):
            taint.GATES.shell(tok, osctl)
        mine = [k for k, v in taint.GATES._tickets.items() if v[0] == tok]
        self.assertEqual(len(mine), taint.Gates.TICKETS_PER_TOKEN)
        taint.GATES.revoke(tok)
        self.assertFalse([k for k, v in taint.GATES._tickets.items() if v[0] == tok])

    def test_agent_and_package_configs_are_walled(self):
        home = Path.home()
        walls = shell_rules.code_walls()
        names = (".claude/settings.json", ".claude/settings.local.json", ".ssh/config", ".ssh/rc", ".npmrc", ".pypirc")
        for name in names:
            self.assertIn(name, shell_rules.STARTUP_FILES)
            self.assertIn(os.path.realpath(str(home / name)), walls["literals"])
        edit = str(self.edit)
        for cmd in ("echo x >> ~/.npmrc", "cp a ~/.claude/settings.json", "rm ~/.ssh/rc", "mv x ~/.pypirc"):
            self.assertTrue(shell_rules.writes_code(cmd, edit), cmd)
        self.assertFalse(shell_rules.writes_code("cat ~/.ssh/config", edit))

    @unittest.skipUnless(shell_rules.can_sandbox(), "sandbox-exec")
    def test_agent_and_package_configs_cannot_be_written(self):
        home = Path(os.path.realpath(self.tmp.name)) / "home"
        (home / ".claude").mkdir(parents=True)
        (home / ".ssh").mkdir()
        code = shell_rules.code_walls(home=home)
        q = shlex.quote
        for outside in (False, True):
            profile = shell_rules.sandbox_profile((str(self.edit), str(home)), outside=outside, code=code)
            for name in (".claude/settings.json", ".claude/settings.local.json", ".ssh/config", ".ssh/rc",
                         ".npmrc", ".pypirc"):
                self.assertNotEqual(self._sandbox(profile, f"echo x >> {q(str(home / name))}").returncode, 0,
                                    (outside, name))
                self.assertFalse((home / name).exists(), (outside, name))
            proc = self._sandbox(profile, f"touch {q(str(home / '.claude' / 'other.json'))}")
            self.assertEqual(proc.returncode, 0, proc.stderr)  # the folder itself stays usable


if __name__ == "__main__":
    unittest.main()
