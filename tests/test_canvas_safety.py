"""Canvas and list files are read only through get-canvas/get-list: never by a shell command, a
Claude Code Read, Grep or Glob. Their per-item labels replace the provenance ledger for them,
a changed canvas is a deliverable, and the index lists them."""
import contextlib
import io
import json
import os
import shlex
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import index
from dashboard import shell_rules
from dashboard.engines import claude_code
from dashboard.engines.base import TurnPolicy

HAS_SANDBOX = os.path.exists(shell_rules.SANDBOX_EXEC)


def write(path, text="x"):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def data_tree(root):
    """A data folder with one canvas, one list, a doc and the HQ canvas folders."""
    root = Path(root)
    write(root / "projects" / "demo" / "canvases" / "cv1.json", json.dumps({"title": "Plan"}))
    write(root / "projects" / "demo" / "lists" / "ls1.json", json.dumps({"title": "Leads"}))
    write(root / "projects" / "demo" / "docs" / "x.md", "hello")
    write(root / "team" / "canvases" / "cv1.json", "{}")
    write(root / "team" / "lists" / "ls1.json", "{}")
    return root


class Denies(unittest.TestCase):
    def test_every_profile_walls_canvas_reads(self):
        data = "/srv/leo data"
        for kw in ({}, {"outside": False}, {"browser": {"paths": [], "home": "/nonexistent"}}):
            profile = shell_rules.sandbox_profile(["/tmp"], data_root=data, **kw)
            for line in shell_rules.canvas_read_denies(data):
                self.assertIn(line, profile, kw)

    def test_denies_name_the_projects_regex_and_the_team_folders(self):
        lines = shell_rules.canvas_read_denies("/srv/leo")
        joined = "\n".join(lines)
        self.assertIn('(deny file-read* file-write* (regex #"^/srv/leo/projects/[^/]+/(canvases|lists)(/|$)"))', joined)
        self.assertIn('(deny file-read* file-write* (subpath "/srv/leo/team/canvases"))', joined)
        self.assertIn('(deny file-read* file-write* (subpath "/srv/leo/team/lists"))', joined)

    def test_the_data_root_is_escaped_for_the_regex(self):
        lines = shell_rules.canvas_read_denies('/srv/a.b+(c)[d]{e}|f^$*?/q"\\z')
        rx = next(x for x in lines if "regex" in x)
        self.assertIn(r'^/srv/a\.b\+\(c\)\[d\]\{e\}\|f\^\$\*\?/q..z/projects/', rx)

    def test_the_data_root_is_the_real_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            real = os.path.realpath(tmp)
            link = os.path.join(real, "link")
            os.makedirs(os.path.join(real, "data"))
            os.symlink(os.path.join(real, "data"), link)
            joined = "\n".join(shell_rules.canvas_read_denies(link))
            self.assertIn(os.path.join(real, "data") + "/team/canvases", joined)
            self.assertNotIn(link + "/", joined)


@unittest.skipUnless(HAS_SANDBOX, "macOS sandbox only")
class LiveSandbox(unittest.TestCase):
    """The real sandbox-exec: a canvas file cannot be read, a doc next to it can."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()  # under /var/folders on macOS: realpath is /private/var
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name
        self.work = os.path.join(self.tmp, "work")
        os.makedirs(self.work)

    def runs(self, command, data, outside=False):
        _, _, run = shell_rules.plan(command, outside=outside, writable=[self.work], data_root=data)
        self.assertTrue(run and run.startswith(shell_rules.SANDBOX_EXEC), run)
        return subprocess.run(run, shell=True, cwd=self.work, capture_output=True).returncode == 0

    def test_cat_of_a_canvas_fails_and_a_doc_works(self):
        data = str(data_tree(os.path.join(self.tmp, "data")))  # the /var spelling, not the real one
        for outside in (False, True):
            for rel in ("projects/demo/canvases/cv1.json", "projects/demo/lists/ls1.json",
                        "team/canvases/cv1.json", "team/lists/ls1.json"):
                self.assertFalse(self.runs(f"cat {shlex.quote(os.path.join(data, rel))}", data, outside), rel)
            self.assertFalse(self.runs(f"ls {shlex.quote(os.path.join(data, 'projects/demo/canvases'))}", data))
            self.assertTrue(self.runs(f"cat {shlex.quote(os.path.join(data, 'projects/demo/docs/x.md'))}",
                                      data, outside))
        real = os.path.realpath(data)
        self.assertFalse(self.runs(f"cat {shlex.quote(os.path.join(real, 'projects/demo/canvases/cv1.json'))}", data))
        self.assertFalse(self.runs(f"cat {shlex.quote(os.path.join(data, 'projects/demo/CANVASES/cv1.json'))}", data))

    def test_no_link_copy_or_rename_gets_around_it(self):
        data = str(data_tree(os.path.join(self.tmp, "data")))
        src = shlex.quote(os.path.join(data, "projects/demo/canvases/cv1.json"))
        q = lambda *p: shlex.quote(os.path.join(*p))  # noqa: E731
        for cmd in (f"ln {src} {q(self.work, 'h')} && cat {q(self.work, 'h')}",
                    f"ln -s {src} {q(self.work, 's')} && cat {q(self.work, 's')}",
                    f"cp {src} {q(self.work, 'c')}",
                    f"mv {q(data, 'projects', 'demo')} {q(self.work, 'm')} && cat {q(self.work, 'm', 'canvases', 'cv1.json')}",
                    f"mv {q(data, 'projects', 'demo', 'canvases')} {q(self.work, 'm2')}",
                    f"mv {q(data, 'projects')} {q(data, 'p2')}", f"mv {q(data, 'team')} {q(data, 't2')}",
                    f"mv {q(data)} {q(self.tmp, 'd2')}", f"echo x > {q(data, 'projects', 'demo', 'lists', 'ls2.json')}"):
            self.assertFalse(self.runs(cmd, data), cmd)
        self.assertTrue(os.path.isfile(os.path.join(data, "projects/demo/canvases/cv1.json")))
        self.assertTrue(self.runs(f"mkdir {q(data, 'projects', 'acme')}", data))  # a new workspace folder is fine

    def test_a_metachar_data_root_is_matched_literally(self):
        data = str(data_tree(os.path.join(self.tmp, "a.b+(c)[1] d")))
        sibling = str(data_tree(os.path.join(self.tmp, "aXb+(c)[1] d")))  # "." unescaped would match X
        self.assertFalse(self.runs(f"cat {shlex.quote(os.path.join(data, 'projects/demo/canvases/cv1.json'))}", data))
        self.assertTrue(self.runs(f"cat {shlex.quote(os.path.join(sibling, 'projects/demo/canvases/cv1.json'))}",
                                  data))


class NoSandbox(unittest.TestCase):
    """With no sandbox, a command that names a canvas or list path is refused before it runs."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.data = str(data_tree(os.path.join(tmp.name, "data")))
        self.work = os.path.join(tmp.name, "work")
        os.makedirs(self.work)
        patch = mock.patch.object(shell_rules, "can_sandbox", return_value=False)
        patch.start()
        self.addCleanup(patch.stop)

    def plan(self, command, cwd=None):
        return shell_rules.plan(command, writable=[self.work], data_root=self.data, cwd=cwd)

    def test_refused(self):
        d = self.data
        for cmd in (f"cat {d}/projects/demo/canvases/cv1.json", f"head -n1 {d}/projects/demo/lists/ls1.json",
                    f"cat {d}/team/canvases/cv1.json", f"cat < {d}/team/lists/ls1.json",
                    f"cat {d}/projects/*/canvases/*.json", f"cat {d}/projects/demo/{{canvases,docs}}/cv1.json",
                    f"cd {d}/projects/demo && cat canvases/cv1.json", f"bash -c 'cat {d}/projects/demo/lists/ls1.json'",
                    f"grep -r x {d}/projects", f"cp -r {d} /tmp/copy", f"tar czf /tmp/x.tgz {d}/projects/demo",
                    f"grep -R x {os.path.dirname(d)}"):
            allow, reason, run = self.plan(cmd)
            self.assertFalse(allow, cmd)
            self.assertEqual(reason, shell_rules.CANVAS_OFF)
        allow, _, _ = self.plan("cat canvases/cv1.json", cwd=os.path.join(self.data, "projects", "demo"))
        self.assertFalse(allow)

    def test_recursive_reads_of_the_folder_it_runs_in(self):
        ws = os.path.join(self.data, "projects", "demo")
        for cmd, cwd in ((f"cd {ws} && rg title", None), (f"cd {ws} && grep -r title", None),
                         ("rg title", ws), ("find -name '*.json' -exec cat {} +", ws),
                         ("cat cv1.json", os.path.join(ws, "canvases")), ("rg title", os.path.dirname(self.data))):
            allow, reason, _ = self.plan(cmd, cwd=cwd)
            self.assertFalse(allow, (cmd, cwd))
            self.assertEqual(reason, shell_rules.CANVAS_OFF)
        self.assertTrue(self.plan("rg title", cwd=self.work)[0])

    def test_paths_inside_inline_code_and_assignments(self):
        cv = os.path.join(self.data, "projects", "demo", "canvases")
        for cmd in (f"python3 -c 'print(open(\"{cv}/cv1.json\").read())'", f"D={cv}; cat $D/cv1.json",
                    f"D={cv} sh -c 'cat $D/cv1.json'"):
            allow, reason, _ = self.plan(cmd)
            self.assertFalse(allow, cmd)
            self.assertEqual(reason, shell_rules.CANVAS_OFF)
        self.assertTrue(self.plan(f"python3 -c 'print(open(\"{self.data}/projects/demo/docs/x.md\").read())'")[0])

    def test_allowed(self):
        d = self.data
        for cmd in (f"cat {d}/projects/demo/docs/x.md", f"ls {d}/projects/demo", "echo canvases/cv1.json",
                    f"cat {d}/projects/demo/docs/*.md", "ls", "python3 build.py"):
            allow, reason, _ = self.plan(cmd)
            self.assertTrue(allow, (cmd, reason))

    def test_reads_canvases_alone(self):
        self.assertTrue(shell_rules.reads_canvases(f"cat {self.data}/projects/demo/canvases/cv1.json", None, self.data))
        self.assertFalse(shell_rules.reads_canvases(f"cat {self.data}/projects/demo/docs/x.md", None, self.data))


class OwnSandboxEngines(unittest.TestCase):
    """Codex runs commands in its own sandbox (gate token wrap=False): the text check still holds,
    even where sandbox-exec exists and the wrapped command is dropped."""

    def test_codex_shell_refuses_canvas_reads(self):
        from core import paths
        from dashboard import taint
        t = taint.Turn(outside=True)
        own = taint.GATES.mint(t, writable=("/w",), wrap=False)
        self.addCleanup(taint.GATES.revoke, own)
        with mock.patch.object(shell_rules, "can_sandbox", return_value=True):
            allow, reason, run = taint.GATES.shell(own, f"cat {paths.DATA}/projects/demo/canvases/cv1.json")
            self.assertEqual((allow, reason, run), (False, shell_rules.CANVAS_OFF, None))
            self.assertTrue(taint.GATES.shell(own, f"cat {paths.DATA}/projects/demo/docs/x.md")[0])


class ClaudeCodeDenies(unittest.TestCase):
    def argv(self, policy):
        from dashboard import chat_session
        sess = chat_session.ChatSession(repo_dir=".", rail="R")
        cmd = claude_code.ClaudeCodeEngine().build_cmd(sess, policy, None)
        return cmd[cmd.index("--disallowedTools") + 1:cmd.index("--strict-mcp-config")]

    def test_a_data_folder_whose_real_path_holds_glob_chars_is_refused(self):
        from dashboard.engines.base import EngineRefused
        with tempfile.TemporaryDirectory() as tmp:
            real = os.path.join(tmp, "data[1]")
            os.makedirs(real)
            link = os.path.join(tmp, "plain")
            os.symlink(real, link)
            with mock.patch.object(claude_code.paths, "DATA", Path(link)), self.assertRaises(EngineRefused):
                self.argv(TurnPolicy())

    def test_read_denies_on_every_turn_and_search_with_folders(self):
        from core import paths
        spellings = dict.fromkeys((str(paths.DATA), str(Path(paths.DATA).resolve())))
        plain = self.argv(TurnPolicy())
        with tempfile.TemporaryDirectory() as tmp:
            linked = self.argv(TurnPolicy(read_dirs=(os.path.realpath(tmp),)))
        for s in spellings:
            for rel in ("projects/*/canvases/**", "projects/*/lists/**", "team/canvases/**", "team/lists/**"):
                self.assertIn(f"Read(/{s}/{rel})", plain)
                self.assertNotIn(f"Grep(/{s}/{rel})", plain)
                self.assertIn(f"Grep(/{s}/{rel})", linked)
                self.assertIn(f"Glob(/{s}/{rel})", linked)


class ApiEngineTools(unittest.TestCase):
    """The api engine's read_file and search_files never open a canvas or list file."""

    def test_read_file_refuses_canvas_and_list_files(self):
        from dashboard.engines import api_tools
        with tempfile.TemporaryDirectory() as tmp:
            root = data_tree(os.path.realpath(tmp))
            policy = TurnPolicy(workspace="demo")
            for rel in ("projects/demo/canvases/cv1.json", "projects/demo/lists/ls1.json",
                        "projects/demo/CANVASES/cv1.json"):
                with self.assertRaises(api_tools.ToolError, msg=rel):
                    api_tools.read_file(rel, root, policy)
            self.assertEqual(api_tools.read_file("projects/demo/docs/x.md", root, policy), "hello")


class Index(unittest.TestCase):
    def test_rebuild_lists_canvases_and_lists(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write(root / "projects" / "demo" / "project.md", "---\nname: Demo\n---")
            write(root / "projects" / "demo" / "canvases" / "cv1.json",
                  json.dumps({"id": "cv1", "title": "Plan", "created_by": "owner", "blocks": []}))
            write(root / "projects" / "demo" / "canvases" / "pins.json", "{}")
            write(root / "projects" / "demo" / "canvases" / "templates" / "tp1.json", json.dumps({"title": "T"}))
            write(root / "projects" / "demo" / "lists" / "ls1.json",
                  json.dumps({"id": "ls1", "title": "Leads", "rows": [{"id": "rw1"}, {"id": "rw2"}]}))
            write(root / "team" / "canvases" / "cv1.json", json.dumps({"id": "cv1", "title": "HQ plan"}))
            with contextlib.redirect_stdout(io.StringIO()):
                index.build(root)
            con = sqlite3.connect(root / "database" / "data" / "os.db")
            got = sorted(con.execute("SELECT ws, id, title, created_by FROM canvases"))
            self.assertEqual(got, [("demo", "cv1", "Plan", "owner"), ("hq", "cv1", "HQ plan", None)])
            self.assertEqual(list(con.execute('SELECT ws, id, title, "rows" FROM canvas_lists')),
                             [("demo", "ls1", "Leads", 2)])
            con.close()
            import dashboard.db as db
            with mock.patch.object(db, "DB_PATH", root / "database" / "data" / "os.db"):
                rows = db.canvases("demo")
            self.assertEqual([r["id"] for r in rows], ["cv1"])
            self.assertEqual(rows[0]["title"], "Plan")


if __name__ == "__main__":
    unittest.main()
