import json
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from core import datadir, migrations, paths


def write(p: Path, text="x"):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


class DataDirCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.source = self.root / "clone"
        self.data = self.root / "home" / "data"
        self.backups = self.root / "home" / "backups"
        self.data.mkdir(parents=True)
        p = mock.patch.object(paths, "BACKUPS", self.backups)
        p.start()
        self.addCleanup(p.stop)
        write(self.source / "projects" / "demo" / "project.md", "demo")
        write(self.source / "projects" / ".gitkeep", "")
        write(self.source / "chats" / "x.json", "{}")
        write(self.source / "os.db", "db")

    def marker(self, data=None):
        return json.loads(((data or self.data) / datadir.MARKER).read_text())

    def test_no_legacy_creates_marker(self):
        src = self.root / "empty"
        src.mkdir()
        with mock.patch.object(paths, "DATA", self.data), mock.patch.object(paths, "CODE", src), \
                mock.patch.object(paths, "ensure_dirs"):
            datadir.prepare()
        self.assertEqual(self.marker()["schema"], migrations.LATEST)
        self.assertEqual([p.name for p in self.data.iterdir()], [datadir.MARKER])

    def test_gitkeep_only_is_not_legacy(self):
        src = self.root / "k"
        write(src / "projects" / ".gitkeep", "")
        self.assertEqual(datadir.legacy_items(src), [])

    def test_moves_into_empty_data(self):
        r = datadir.move_legacy(self.source, self.data)
        self.assertIsNone(r.refused)
        self.assertTrue((self.data / "projects/demo/project.md").exists())
        self.assertTrue((self.data / "chats/x.json").exists())
        self.assertTrue((self.data / "os.db").exists())
        self.assertFalse((self.source / "projects/demo").exists())
        self.assertFalse((self.source / "chats").exists())
        self.assertTrue((self.source / "projects/.gitkeep").exists())
        self.assertFalse((self.data / "projects/.gitkeep").exists())
        self.assertEqual(self.marker()["schema"], 1)
        with tarfile.open(r.backup) as t:
            self.assertIn("projects/demo/project.md", t.getnames())
            self.assertNotIn("projects/.gitkeep", t.getnames())

    def test_marker_alone_does_not_block(self):
        (self.data / datadir.MARKER).write_text('{"schema": 1}')
        self.assertTrue(datadir.data_is_empty(self.data))
        r = datadir.move_legacy(self.source, self.data)
        self.assertIsNone(r.refused)
        self.assertTrue((self.data / "projects/demo/project.md").exists())

    def test_both_sides_have_data_refuses(self):
        write(self.data / "projects/acme/project.md")
        r = datadir.move_legacy(self.source, self.data)
        self.assertIn("leobot doctor", r.refused)
        self.assertIn(str(self.source), r.refused)
        self.assertIn(str(self.data), r.refused)
        self.assertEqual(r.moved, [])
        self.assertTrue((self.source / "projects/demo/project.md").exists())
        self.assertTrue((self.data / "projects/acme/project.md").exists())
        self.assertFalse((self.data / "projects/demo").exists())

    def test_replace_backs_up_then_moves(self):
        write(self.data / "projects/acme/project.md")
        r = datadir.move_legacy(self.source, self.data, replace=True)
        self.assertIsNone(r.refused)
        rep = list(self.backups.glob("replaced-*.tar.gz"))
        self.assertEqual(len(rep), 1)
        with tarfile.open(rep[0]) as t:
            self.assertIn("projects/acme/project.md", t.getnames())
        self.assertTrue((self.data / "projects/demo").exists())
        self.assertFalse((self.data / "projects/acme").exists())

    def test_token_and_log_not_moved(self):
        write(self.source / "dashboard/.auth-token", "secret")
        write(self.source / "dashboard/server.log", "log")
        datadir.move_legacy(self.source, self.data)
        self.assertFalse((self.source / "dashboard/.auth-token").exists())
        self.assertFalse((self.source / "dashboard/server.log").exists())
        self.assertFalse((self.data / "dashboard").exists())

    def test_tracked_files_never_moved(self):
        # a tracked placeholder next to real data stays in the source
        write(self.source / "team" / ".gitkeep", "")
        write(self.source / "team" / "approvals.json", "{}")
        datadir.move_legacy(self.source, self.data)
        self.assertTrue((self.source / "team" / ".gitkeep").exists())
        self.assertTrue((self.data / "team/approvals.json").exists())
        self.assertFalse((self.data / "team/.gitkeep").exists())

    def test_move_with_space_in_home(self):
        home = self.root / "Ana María" / ".leobot"
        data = home / "data"
        data.mkdir(parents=True)
        with mock.patch.object(paths, "BACKUPS", home / "backups"):
            (home / "backups").mkdir()
            r = datadir.move_legacy(self.source, data)
        self.assertIsNone(r.refused)
        self.assertTrue((data / "projects/demo/project.md").exists())
        self.assertTrue(r.backup.exists())

    def test_absolute_paths_rewritten(self):
        src = self.source
        chat = {"folder_paths": [f"{src}/projects/demo"], "messages": [
            {"t": f"see {src}/chats/x.json and {src}/dashboard/app.js and {src}-other/chats/y"}]}
        write(src / "portfolio" / "a.md", "a")
        write(src / "chats" / "c.json", json.dumps(chat))
        write(src / "chats" / "c.abc.jsonl", json.dumps({"p": f"{src}/projects/demo"}) + "\n")
        write(src / "team" / "ws" / "folders.json", json.dumps(
            {"folders": [{"id": "f1", "path": f"{src}/portfolio"}, {"id": "f2", "path": "/elsewhere/app"}]}))
        datadir.move_legacy(src, self.data)
        c = (self.data / "chats/c.json").read_text()
        self.assertIn(f"{self.data}/projects/demo", c)
        self.assertIn(f"{self.data}/chats/x.json", c)
        self.assertIn(f"{src}/dashboard/app.js", c)  # code path stays
        self.assertIn(f"{src}-other/chats/y", c)
        self.assertIn(f"{self.data}/projects/demo", (self.data / "chats/c.abc.jsonl").read_text())
        f = json.loads((self.data / "team/ws/folders.json").read_text())
        self.assertEqual(f["folders"][0]["path"], f"{self.data}/portfolio")
        self.assertEqual(f["folders"][1]["path"], "/elsewhere/app")

    def test_absolute_paths_rewritten_non_ascii(self):
        src = self.root / "Ana María" / "clone"
        write(src / "portfolio" / "a.md", "a")
        write(src / "chats" / "c.json", json.dumps({"p": f"{src}/projects/demo"}))  # í escaped
        write(src / "projects" / "demo" / "project.md", f"folder: {src}/portfolio\n")
        datadir.move_legacy(src, self.data)
        self.assertEqual(json.loads((self.data / "chats/c.json").read_text())["p"], f"{self.data}/projects/demo")
        self.assertIn(f"{self.data}/portfolio", (self.data / "projects/demo/project.md").read_text())

    def test_failed_move_leaves_no_marker(self):
        with mock.patch.object(datadir.shutil, "move", side_effect=OSError("boom")):
            with self.assertRaises(OSError):
                datadir.move_legacy(self.source, self.data)
        self.assertFalse((self.data / datadir.MARKER).exists())
        self.assertTrue(datadir.legacy_items(self.source))

    def test_prepare_refuses_with_exit(self):
        write(self.data / "projects/acme/project.md")
        with mock.patch.object(paths, "DATA", self.data), mock.patch.object(paths, "CODE", self.source), \
                mock.patch.object(paths, "ensure_dirs"):
            with self.assertRaises(SystemExit) as cm:
                datadir.prepare()
        self.assertIn("leobot doctor", str(cm.exception))


class MigrationsCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.data = Path(self._tmp.name)
        (self.data / datadir.MARKER).write_text(json.dumps({"schema": 1, "created": "t"}))

    def test_run_in_order_and_idempotent(self):
        seen = []
        fake = [migrations.MIGRATIONS[0], lambda d: seen.append(2), lambda d: seen.append(3)]
        with mock.patch.object(migrations, "MIGRATIONS", fake), mock.patch.object(migrations, "LATEST", 3):
            self.assertEqual(migrations.run(self.data), 3)
            self.assertEqual(migrations.run(self.data), 3)
        self.assertEqual(seen, [2, 3])
        self.assertEqual(json.loads((self.data / datadir.MARKER).read_text())["schema"], 3)
        self.assertEqual(json.loads((self.data / datadir.MARKER).read_text())["created"], "t")

    def test_failure_keeps_last_good_schema(self):
        def boom(d):
            raise RuntimeError("no")
        fake = [migrations.MIGRATIONS[0], lambda d: None, boom]
        with mock.patch.object(migrations, "MIGRATIONS", fake), mock.patch.object(migrations, "LATEST", 3):
            with self.assertRaises(RuntimeError):
                migrations.run(self.data)
        self.assertEqual(json.loads((self.data / datadir.MARKER).read_text())["schema"], 2)

    def test_newer_schema_refuses(self):
        (self.data / datadir.MARKER).write_text(json.dumps({"schema": migrations.LATEST + 1}))
        with self.assertRaises(SystemExit) as cm:
            migrations.run(self.data)
        self.assertIn("newer Leo Bot", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
