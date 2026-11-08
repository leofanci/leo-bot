import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from core import bots, folders
from tests.test_index_projects import write


class FolderStoreTest(unittest.TestCase):
    """core.folders: the store, the path checks, the layer rules and the never-write list."""

    def setUp(self):
        self._root = tempfile.TemporaryDirectory()
        self._ext = tempfile.TemporaryDirectory()
        self.root = Path(self._root.name).resolve()
        self.tmp = Path(self._ext.name).resolve()
        self.app = self.tmp / "acme-app"
        self.docs = self.tmp / "acme-docs"
        self.base = self.tmp / "base"
        for d in (self.app, self.docs, self.base):
            d.mkdir()
        write(self.root / "projects/acme/project.md",
              f"---\nname: Acme\nkind: venture\nlocal_folder: {self.app}\n---\nour voice\n")
        write(self.root / "projects/demo/project.md", "---\nname: Demo\n---\n")
        (self.root / "team").mkdir()

    def tearDown(self):
        self._root.cleanup()
        self._ext.cleanup()

    def _file(self, ws="acme"):
        if ws == "hq":
            return self.root / "team/folders.json"
        return self.root / "projects" / ws / "team/folders.json"

    # ---- migration ------------------------------------------------------- #
    def test_migrates_local_folder_as_read_and_removes_the_line(self):
        rows = folders.load("acme", self.root)["folders"]
        self.assertEqual([(r["path"], r["access"]) for r in rows], [(str(self.app), "read")])
        text = (self.root / "projects/acme/project.md").read_text()
        self.assertNotIn("local_folder", text)
        self.assertIn("name: Acme", text)
        self.assertIn("our voice", text)
        self.assertTrue(self._file().exists())

    def test_migration_runs_once(self):
        first = folders.load("acme", self.root)
        folders.set_access("acme", first["folders"][0]["id"], folders.EDIT, self.root)
        # A line put back by hand is not migrated again over the file.
        md = self.root / "projects/acme/project.md"
        md.write_text(f"---\nname: Acme\nlocal_folder: {self.docs}\n---\n")
        second = folders.load("acme", self.root)
        self.assertEqual([(r["id"], r["path"], r["access"]) for r in second["folders"]],
                         [(first["folders"][0]["id"], str(self.app), "edit")])

    def test_migration_keeps_a_stale_path(self):
        write(self.root / "projects/acme/project.md", "---\nname: Acme\nlocal_folder: /nope/gone-app\n---\n")
        rows = folders.load("acme", self.root)["folders"]
        self.assertEqual([r["path"] for r in rows], ["/nope/gone-app"])
        self.assertFalse(folders.usable(rows[0], self.root))
        self.assertEqual(folders.for_turn("acme", None, "cto", self.root), [])

    def test_hq_has_no_folders_until_linked(self):
        self.assertEqual(folders.load("hq", self.root)["folders"], [])
        row = folders.add("hq", str(self.docs), root=self.root)
        self.assertEqual(row["access"], "edit")
        self.assertTrue(self._file("hq").exists())

    # ---- add, remove, access --------------------------------------------- #
    def test_new_links_default_to_edit(self):
        row = folders.add("acme", str(self.docs), root=self.root)
        self.assertEqual((row["path"], row["access"], row["name"]), (str(self.docs), "edit", "acme-docs"))
        self.assertEqual(row["id"], "f2")

    def test_add_same_path_twice_keeps_one_row(self):
        a = folders.add("acme", str(self.docs), root=self.root)
        b = folders.add("acme", str(self.docs) + "/", folders.READ, root=self.root)
        self.assertEqual(a, b)
        self.assertEqual(len(folders.load("acme", self.root)["folders"]), 2)

    def test_add_refuses_unknown_access(self):
        with self.assertRaises(folders.FolderError):
            folders.add("acme", str(self.docs), "write", root=self.root)

    def test_refuses_inside_myos_and_glob_characters(self):
        for name in ("app[1]", "a*", "b?", "c{d}"):
            (self.tmp / name).mkdir()
        for bad in (self.root / "projects", self.root, self.tmp / "app[1]", self.tmp / "a*",
                    self.tmp / "b?", self.tmp / "c{d}"):
            with self.assertRaises(folders.FolderError, msg=str(bad)):
                folders.normalize_path(str(bad), self.root)
        self.assertEqual(folders.normalize_path("", self.root), "")
        self.assertEqual(folders.normalize_path(str(self.app), self.root), str(self.app))

    def test_a_space_in_the_path_is_fine(self):
        spaced = self.tmp / "My App"
        spaced.mkdir()
        self.assertEqual(folders.normalize_path(str(spaced), self.root), str(spaced))

    def test_ids_are_not_reused(self):
        folders.load("acme", self.root)
        f2 = folders.add("acme", str(self.docs), root=self.root)["id"]
        folders.remove("acme", f2, self.root)
        self.assertEqual(folders.add("acme", str(self.base), root=self.root)["id"], "f3")

    def test_remove_and_set_access_refuse_unknown_ids(self):
        with self.assertRaises(folders.FolderError):
            folders.remove("acme", "f9", self.root)
        with self.assertRaises(folders.FolderError):
            folders.set_access("acme", "f1", "admin", self.root)

    # ---- layers ------------------------------------------------------------ #
    def _two(self):
        folders.load("acme", self.root)
        folders.set_access("acme", "f1", folders.EDIT, self.root)
        folders.add("acme", str(self.docs), root=self.root)

    def test_no_layers_gives_every_folder_at_its_access(self):
        self._two()
        rows = folders.for_turn("acme", "general", "cto", self.root)
        self.assertEqual([(r["id"], r["path"], r["access"]) for r in rows],
                         [("f1", str(self.app), "edit"), ("f2", str(self.docs), "edit")])

    def test_layers_only_narrow(self):
        self._two()
        folders.set_access("acme", "f1", folders.READ, self.root)
        folders.set_limit("acme", "rooms", "t-launch", {"f1": "edit", "f2": "edit"}, self.root)
        folders.set_limit("acme", "bots", "cmo", {"f2": "read"}, self.root)
        rows = folders.for_turn("acme", "t-launch", "cmo", self.root)
        self.assertEqual([(r["id"], r["access"]) for r in rows], [("f2", "read")])
        # The channel layer cannot lift f1 above its own read access.
        rows = folders.for_turn("acme", "t-launch", "cto", self.root)
        self.assertEqual([(r["id"], r["access"]) for r in rows], [("f1", "read"), ("f2", "edit")])

    def test_empty_layer_means_no_folders(self):
        self._two()
        folders.set_limit("acme", "bots", "cmo", {}, self.root)
        self.assertEqual(folders.for_turn("acme", "general", "cmo", self.root), [])
        folders.set_limit("acme", "bots", "cmo", None, self.root)
        self.assertEqual(len(folders.for_turn("acme", "general", "cmo", self.root)), 2)

    def test_dm_skips_the_channel_layer(self):
        self._two()
        folders.set_limit("acme", "rooms", "dm-cto", {}, self.root)
        folders.set_limit("acme", "rooms", "general", {}, self.root)
        self.assertEqual(len(folders.for_turn("acme", None, "cto", self.root)), 2)

    def test_set_limit_refuses_bad_input(self):
        self._two()
        for kind, key, limit in (("people", "x", {}), ("bots", "../x", {}), ("bots", "cmo", {"f9": "read"}),
                                 ("bots", "cmo", {"f1": "admin"}), ("bots", "cmo", ["f1"])):
            with self.assertRaises(folders.FolderError, msg=(kind, key, limit)):
                folders.set_limit("acme", kind, key, limit, self.root)

    def test_unlink_removes_from_layers_and_never_widens(self):
        self._two()
        folders.set_limit("acme", "bots", "cmo", {"f2": "read"}, self.root)
        folders.remove("acme", "f2", self.root)
        data = folders.load("acme", self.root)
        self.assertEqual(data["bots"]["cmo"], {})
        self.assertEqual(folders.for_turn("acme", "general", "cmo", self.root), [])

    def test_same_folder_two_workspaces_then_unlink(self):
        folders.load("acme", self.root)
        shared = str(self.app)
        row = folders.add("demo", shared, root=self.root)
        slugs = ["acme", "demo"]
        acme_kept = folders.all_paths("acme", self.root)
        demo_kept = folders.all_paths("demo", self.root)
        self.assertEqual(folders.turn_denies(acme_kept, folders.other_workspace_paths("acme", slugs, self.root), []), ())
        self.assertEqual(folders.turn_denies(demo_kept, folders.other_workspace_paths("demo", slugs, self.root), []), ())
        folders.remove("demo", row["id"], self.root)
        demo_kept = folders.all_paths("demo", self.root)
        others = folders.other_workspace_paths("demo", slugs, self.root)
        self.assertIn(shared, others)
        self.assertEqual(folders.turn_denies(demo_kept, others, []), (shared,))
        # acme keeps its own access.
        self.assertEqual([r["path"] for r in folders.for_turn("acme", None, "cto", self.root)], [shared])

    def test_other_workspace_paths_include_hq_and_hq_gets_none(self):
        folders.add("hq", str(self.base), root=self.root)
        self.assertEqual(sorted(folders.other_workspace_paths("demo", ["acme", "demo"], self.root)),
                         sorted([str(self.app), str(self.base)]))
        self.assertEqual(folders.other_workspace_paths("hq", ["acme", "demo"], self.root), [])

    def test_turn_denies_nested(self):
        kept, inner = "/w/app", "/w/app/vendor"
        self.assertEqual(folders.turn_denies([kept], [inner], []), ())
        self.assertEqual(folders.turn_denies([kept], [], [inner]), (inner,))
        self.assertEqual(folders.turn_denies([inner], [], [kept]), ())
        self.assertEqual(folders.turn_denies([inner], [kept], []), ())
        self.assertEqual(folders.turn_denies(["/w/app2"], [kept], []), (kept,))
        self.assertEqual(folders.turn_denies([], [kept, kept], [kept]), (kept,))

    # ---- never-write list ------------------------------------------------ #
    def test_may_write_refuses_the_list(self):
        for rel in (".git/hooks/pre-commit", "sub/.git/config", ".env", ".envrc", ".mcp.json",
                    ".vscode/tasks.json", ".claude/settings.json", ".husky/pre-push", "a/id_rsa",
                    ".codex/config.toml", ".agents/x.md", ".git", ".GIT/hooks/x", ".Env", "a/.env.local"):
            self.assertFalse(folders.may_write(self.base / rel, self.base), rel)
        self.assertTrue(folders.may_write(self.base / "CLAUDE.md", self.base))
        self.assertTrue(folders.may_write(self.base / "AGENTS.md", self.base))
        self.assertTrue(folders.may_write(self.base / ".github/workflows/ci.yml", self.base))
        self.assertFalse(folders.may_write(self.tmp / "x", self.base))

    def test_secret_globs_live_here_and_claude_code_reexports_them(self):
        from dashboard.engines import claude_code
        self.assertIs(claude_code.SECRET_FILE_GLOBS, folders.SECRET_FILE_GLOBS)
        self.assertIn(".env", folders.SECRET_FILE_GLOBS)

    def test_is_git_repo_and_first_indexed(self):
        (self.app / ".git").mkdir()
        (self.app / "sub").mkdir()
        self.assertTrue(folders.is_git_repo(str(self.app / "sub")))
        self.assertFalse(folders.is_git_repo(str(self.docs)))
        (self.docs / ".tokensave").mkdir()
        (self.docs / ".tokensave/tokensave.db").write_text("")
        rows = [{"path": str(self.app)}, {"path": str(self.docs)}]
        self.assertEqual(folders.first_indexed(rows), str(self.docs))
        self.assertIsNone(folders.first_indexed(rows[:1]))

    # ---- damaged file ---------------------------------------------------- #
    def test_damaged_file_is_safe(self):
        f = self._file()
        f.parent.mkdir(parents=True)
        f.write_text("{not json")
        self.assertEqual(folders.load("acme", self.root)["folders"], [])
        # project.md's local_folder is not migrated over an existing file.
        self.assertIn("local_folder", (self.root / "projects/acme/project.md").read_text())
        f.write_text(json.dumps([1, 2]))
        self.assertEqual(folders.load("acme", self.root), {"folders": [], "rooms": {}, "bots": {}})
        f.write_text(json.dumps({
            "folders": [
                {"id": "f1", "path": str(self.app), "access": "owner"},
                {"id": "f2", "path": "relative/app", "access": "edit"},
                {"path": str(self.docs), "access": "edit"},
                {"id": "f1", "path": str(self.docs), "access": "edit"},
                "junk",
                {"id": "f3", "path": str(self.base), "access": "edit", "name": 7},
            ],
            "rooms": {"t-launch": {"f1": "edit", "f3": "weird"}, "bad": "x"},
            "bots": ["cmo"],
        }))
        data = folders.load("acme", self.root)
        self.assertEqual([(r["id"], r["access"], r["name"]) for r in data["folders"]],
                         [("f1", "read", "acme-app"), ("f3", "edit", "base")])
        self.assertEqual(data["rooms"], {"t-launch": {"f1": "edit", "f3": "read"}})
        self.assertEqual(data["bots"], {})
        rows = folders.for_turn("acme", "t-launch", "cmo", self.root)
        self.assertEqual([(r["id"], r["access"]) for r in rows], [("f1", "read"), ("f3", "read")])

    def test_save_writes_the_three_keys(self):
        folders.add("acme", str(self.docs), root=self.root)
        data = json.loads(self._file().read_text())
        self.assertEqual(set(data), {"folders", "rooms", "bots", "seq"})



class ReadersUseTheStore(unittest.TestCase):
    """Task 2: every reader of the old `local_folder` line reads folders.json instead."""

    def setUp(self):
        import index
        from dashboard import db, fileops
        self.db, self.fileops, self.index = db, fileops, index
        self._root = tempfile.TemporaryDirectory()
        self._ext = tempfile.TemporaryDirectory()
        self.root = Path(self._root.name).resolve()
        self.app = Path(self._ext.name).resolve() / "acme-app"
        self.docs = Path(self._ext.name).resolve() / "acme-docs"
        self.app.mkdir()
        self.docs.mkdir()
        write(self.root / "projects/acme/project.md",
              f"---\nname: Acme\nkind: venture\nlocal_folder: {self.app}\n---\nbody\n")
        write(self.root / "projects/demo/project.md", "---\nname: Demo\nkind: venture\n---\n")
        patches = [mock.patch.object(fileops, "ROOT", self.root),
                   mock.patch.object(db, "DB_PATH", self.root / "database/data/os.db"),
                   mock.patch.object(fileops, "reindex", lambda: index.build(self.root))]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        with mock.patch("sys.stdout"):
            index.build(self.root)

    def tearDown(self):
        self._root.cleanup()
        self._ext.cleanup()

    def test_project_tree_lists_folders(self):
        tree = {p["slug"]: p for p in self.db.tree()}
        self.assertNotIn("local_folder", tree["acme"])
        self.assertEqual([(r["path"], r["access"]) for r in tree["acme"]["folders"]], [(str(self.app), "read")])
        self.assertEqual(tree["demo"]["folders"], [])

    def test_default_active_turns_cto_on_with_any_folder(self):
        tree = {p["slug"]: p for p in self.db.tree()}
        self.assertIn("cto", bots.default_active(tree["acme"]))
        self.assertNotIn("cto", bots.default_active(tree["demo"]))

    def test_update_project_adds_and_never_rewrites_the_line(self):
        with mock.patch("sys.stdout"):
            out = self.fileops.update_project("acme", {"name": "Acme Inc", "local_folder": str(self.docs)})
        self.assertEqual(set(out), {"slug", "folders"})
        self.assertEqual([(r["path"], r["access"]) for r in out["folders"]],
                         [(str(self.app), "read"), (str(self.docs), "edit")])
        md = (self.root / "projects/acme/project.md").read_text()
        self.assertNotIn("local_folder", md)
        self.assertIn("name: Acme Inc", md)
        with mock.patch("sys.stdout"):
            out = self.fileops.update_project("acme", {"local_folder": ""})
        self.assertEqual(len(out["folders"]), 2)

    def test_create_project_links_one_folder_at_edit(self):
        with mock.patch("sys.stdout"):
            out = self.fileops.create_project("newco", {"local_folder": str(self.docs)})
        self.assertEqual([(r["path"], r["access"]) for r in out["folders"]], [(str(self.docs), "edit")])
        self.assertNotIn("local_folder", (self.root / "projects/newco/project.md").read_text())

    def test_create_project_refuses_a_bad_folder_before_writing(self):
        with self.assertRaises(self.fileops.ActionError):
            self.fileops.create_project("newco", {"local_folder": str(self.root / "projects")})
        self.assertFalse((self.root / "projects/newco").exists())

    def test_index_never_stores_the_path(self):
        rows = [e for e in self.db.entities() if e["slug"] == "acme"]
        self.assertIsNone(rows[0].get("local_folder"))


if __name__ == "__main__":
    unittest.main()


class ReviewFixes(unittest.TestCase):
    """Findings of the whole-branch review, each pinned."""

    def setUp(self):
        self._root, self._ext = tempfile.TemporaryDirectory(), tempfile.TemporaryDirectory()
        self.addCleanup(self._root.cleanup)
        self.addCleanup(self._ext.cleanup)
        self.root = Path(self._root.name).resolve()
        self.tmp = Path(self._ext.name).resolve()
        write(self.root / "projects/acme/project.md", "---\nname: Acme\n---\n")
        (self.root / "team").mkdir()

    def test_backslash_and_extglob_are_refused(self):
        for name in ("a\\b", "x!(y)", "x@(y)", "x+(y)"):
            (self.tmp / name).mkdir()
            with self.assertRaises(folders.FolderError, msg=name):
                folders.normalize_path(str(self.tmp / name), self.root)
            self.assertTrue(folders.has_glob_chars(str(self.tmp / name)), name)
        (self.tmp / "My App (old)").mkdir()
        self.assertEqual(folders.normalize_path(str(self.tmp / "My App (old)"), self.root), str(self.tmp / "My App (old)"))

    def test_hook_managers_and_git_files_are_never_written(self):
        base = self.tmp
        for rel in (".pre-commit-config.yaml", "lefthook.yml", ".lefthook.yml", "lefthook-local.yml",
                    ".lefthook/pre-commit/x", ".githooks/pre-commit", ".cursor/mcp.json", ".gemini/settings.json",
                    "sub/.git", "sub/HEAD", "LEFTHOOK.YML"):
            self.assertFalse(folders.may_write(base / rel, base), rel)

    def test_hook_dirs_reads_core_hooks_path(self):
        import shutil, subprocess
        if not shutil.which("git"):
            self.skipTest("git not installed")
        repo = self.tmp / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        self.assertEqual(folders.hook_dirs(str(repo)), [])
        subprocess.run(["git", "-C", str(repo), "config", "core.hooksPath", "scripts/hooks"], check=True)
        self.assertEqual(folders.hook_dirs(str(repo)), [str(repo / "scripts" / "hooks")])
        self.assertEqual(folders.hook_dirs(str(self.tmp / "missing")), [])

    def test_all_paths_are_resolved_like_turn_rows(self):
        real = self.tmp / "ext" / "app"
        real.mkdir(parents=True)
        link = self.tmp / "app"
        link.symlink_to(real)
        folders.save("acme", {"folders": [{"id": "f1", "path": str(link), "access": "edit"}]}, self.root)
        self.assertEqual(folders.all_paths("acme", self.root), [str(real)])
        self.assertEqual([r["path"] for r in folders.for_turn("acme", None, None, self.root)], [str(real)])

    def test_a_damaged_file_is_never_written_over(self):
        f = self.root / "projects/acme/team/folders.json"
        f.parent.mkdir(parents=True)
        f.write_text('{"folders": [{"id": "f1", "path": "/x", "access": "edit"},')
        self.assertTrue(folders.damaged("acme", self.root))
        self.assertEqual(folders.load("acme", self.root)["folders"], [])
        (self.tmp / "app").mkdir()
        for call in (lambda: folders.add("acme", str(self.tmp / "app"), root=self.root),
                     lambda: folders.remove("acme", "f1", self.root),
                     lambda: folders.set_access("acme", "f1", "read", self.root),
                     lambda: folders.set_limit("acme", "bots", "cmo", {}, self.root)):
            with self.assertRaises(folders.FolderError):
                call()
        self.assertTrue(f.read_text().startswith('{"folders": [{"id": "f1"'))
        self.assertFalse(folders.damaged("demo-none", self.root))

    def test_crlf_project_md_migrates(self):
        app = self.tmp / "app"
        app.mkdir()
        (self.root / "projects/acme/project.md").write_bytes(
            f"---\r\nname: Acme\r\nlocal_folder: {app}\r\n---\r\nbody\r\n".encode())
        rows = folders.load("acme", self.root)["folders"]
        self.assertEqual([r["path"] for r in rows], [str(app)])
        text = (self.root / "projects/acme/project.md").read_text()
        self.assertNotIn("local_folder", text)
        self.assertIn("name: Acme", text)
