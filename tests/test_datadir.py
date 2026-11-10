import errno
import json
import os
import shutil
import stat
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

    def _crash_on_nth(self, n):
        real = datadir._move_entry
        calls = []

        def fake(src, dst):
            calls.append(src)
            if len(calls) == n:
                raise OSError("boom")
            real(src, dst)
        return mock.patch.object(datadir, "_move_entry", fake)

    def _prepare(self, source=None):
        with mock.patch.object(paths, "DATA", self.data), mock.patch.object(paths, "CODE", source or self.source), \
                mock.patch.object(paths, "ensure_dirs"):
            datadir.prepare()

    def assert_all_moved(self):
        self.assertTrue((self.data / "projects/demo/project.md").exists())
        self.assertTrue((self.data / "chats/x.json").exists())
        self.assertTrue((self.data / "os.db").exists())
        self.assertFalse((self.source / "chats").exists())
        self.assertFalse((self.source / "os.db").exists())
        self.assertFalse((self.source / "projects/demo").exists())
        self.assertTrue((self.source / "projects/.gitkeep").exists())
        self.assertEqual(self.marker()["schema"], 1)
        self.assertFalse((self.data / datadir.JOURNAL).exists())

    def test_crash_after_first_item_resumes(self):
        with self._crash_on_nth(2):
            with self.assertRaises(OSError):
                datadir.move_legacy(self.source, self.data)
        self.assertFalse((self.data / datadir.MARKER).exists())
        self.assertTrue((self.data / datadir.JOURNAL).exists())
        self._prepare()
        self.assert_all_moved()

    def test_replace_refused_while_journal_exists(self):
        with self._crash_on_nth(2):
            with self.assertRaises(OSError):
                datadir.move_legacy(self.source, self.data)
        r = datadir.move_legacy(self.source, self.data, replace=True)
        self.assertIn("did not finish", r.refused)
        self._prepare()
        self.assert_all_moved()

    def test_journal_from_other_source_refuses(self):
        with self._crash_on_nth(2):
            with self.assertRaises(OSError):
                datadir.move_legacy(self.source, self.data)
        other = self.root / "other"
        other.mkdir()
        r = datadir.move_legacy(other, self.data)
        self.assertIn("unfinished move", r.refused)

    def test_crash_during_source_delete_resumes(self):
        # cross-volume path: copy done, source delete half done (one file already gone)
        write(self.source / "projects" / "demo" / "notes.md", "n")

        def exdev(a, b):
            raise OSError(errno.EXDEV, "cross-device")
        real_remove = datadir._remove
        state = {"n": 0}

        def flaky(p):
            state["n"] += 1
            if state["n"] == 1:
                (p / "project.md").unlink()  # half-finished delete
                raise OSError("boom")
            real_remove(p)
        with mock.patch.object(datadir.os, "rename", exdev), mock.patch.object(datadir, "_remove", flaky):
            with self.assertRaises(OSError):
                datadir.move_legacy(self.source, self.data)
        self.assertEqual([p.name for p in (self.source / "projects/demo").iterdir()], ["notes.md"])
        self._prepare()
        self.assert_all_moved()
        self.assertEqual(sorted(p.name for p in (self.data / "projects/demo").iterdir()),
                         ["notes.md", "project.md"])

    def test_partial_copy_is_discarded_on_resume(self):
        journal = {"source": str(self.source), "items": ["chats"], "dropped": [], "stamp": "s",
                   "backup": None, "stage": "moving"}
        (self.data / datadir.JOURNAL).write_text(json.dumps(journal))
        write(self.data / "chats.partial" / "junk.json", "{")
        with mock.patch.object(datadir.os, "rename", side_effect=OSError(errno.EXDEV, "x")):
            self._prepare()
        self.assertTrue((self.data / "chats/x.json").exists())
        self.assertFalse((self.data / "chats.partial").exists())

    def test_rewrite_temp_cleanup_only_on_resume_and_exact_pattern(self):
        write(self.source / "chats" / ".rewrite-notes.json", "mine")
        write(self.source / "chats" / ".rewrite-abcd1234.tmp", "user file")
        write(self.source / "chats" / "c.json", json.dumps({"p": f"{self.source}/projects/demo"}))
        datadir.move_legacy(self.source, self.data)  # first pass: nothing is cleaned
        self.assertTrue((self.data / "chats/.rewrite-notes.json").exists())
        self.assertTrue((self.data / "chats/.rewrite-abcd1234.tmp").exists())

    def test_rewrite_resume_cleans_its_own_temps(self):
        write(self.source / "chats" / "c.json", json.dumps({"p": f"{self.source}/projects/demo"}))
        write(self.source / "chats" / ".rewrite-notes.json", "mine")
        with mock.patch.object(datadir, "_replace_text", side_effect=OSError("boom")):
            with self.assertRaises(OSError):
                datadir.move_legacy(self.source, self.data)
        write(self.data / "chats" / ".rewrite-abcd1234.tmp", "stray")
        self._prepare()
        self.assertFalse((self.data / "chats/.rewrite-abcd1234.tmp").exists())
        self.assertTrue((self.data / "chats/.rewrite-notes.json").exists())

    def test_other_source_journal_names_the_fix(self):
        with self._crash_on_nth(2):
            with self.assertRaises(OSError):
                datadir.move_legacy(self.source, self.data)
        other = self.root / "other"
        other.mkdir()
        for replace in (False, True):
            r = datadir.move_legacy(other, self.data, replace=replace)
            self.assertIn(f"leobot move-data --from {self.source}", r.refused)
            self.assertIn("without --replace", r.refused)

    def test_unreadable_or_malformed_journal_refuses_clearly(self):
        for text in ("{not json", json.dumps({"source": str(self.source)}),
                     json.dumps({"source": str(self.source), "items": "chats", "stage": "moving",
                                 "backup": "/b/legacy-1.tar.gz"})):
            (self.data / datadir.JOURNAL).write_text(text)
            r = datadir.move_legacy(self.source, self.data)
            self.assertIn("unreadable or damaged", r.refused)
            self.assertIn(str(self.data / datadir.JOURNAL), r.refused)
            self.assertTrue((self.source / "chats/x.json").exists())
        self.assertIn("/b/legacy-1.tar.gz", r.refused)
        self.assertIn("legacy-*.tar.gz", self._bad_no_backup())

    def _bad_no_backup(self):
        (self.data / datadir.JOURNAL).write_text("{x")
        return datadir.move_legacy(self.source, self.data).refused

    def test_dropped_directory_is_removed(self):
        src = self.root / "dirs"
        write(src / "dashboard/server.log/inner.txt", "x")
        write(self.data / "projects/acme/project.md")
        self._prepare(src)
        self.assertFalse((src / "dashboard/server.log").exists())

    def test_linked_parent_of_item_refused(self):
        outside = self.root / "outside2"
        write(outside / "data" / "a.db", "x")
        write(self.source / "database" / "keep.sql", "tracked")
        shutil.rmtree(self.source / "database")
        os.symlink(outside, self.source / "database")
        r = datadir.move_legacy(self.source, self.data)
        self.assertIn("database", r.refused)
        self.assertIn("symbolic link", r.refused)
        self.assertTrue((outside / "data" / "a.db").exists())

    def test_crash_inside_rewrite_resumes(self):
        write(self.source / "chats" / "c.json", json.dumps({"p": f"{self.source}/projects/demo"}))
        with mock.patch.object(datadir, "_replace_text", side_effect=OSError("boom")):
            with self.assertRaises(OSError):
                datadir.move_legacy(self.source, self.data)
        self.assertEqual(datadir.read_journal(self.data)["stage"], "rewriting")
        self._prepare()
        self.assert_all_moved()
        self.assertIn(f"{self.data}/projects/demo", (self.data / "chats/c.json").read_text())

    def test_rewrite_is_atomic_and_keeps_mode(self):
        write(self.source / "chats" / "c.json", json.dumps({"p": f"{self.source}/projects/demo"}))
        os.chmod(self.source / "chats" / "c.json", 0o640)
        datadir.move_legacy(self.source, self.data)
        self.assertEqual(stat.S_IMODE((self.data / "chats/c.json").stat().st_mode), 0o640)
        self.assertEqual([p.name for p in (self.data / "chats").iterdir() if p.name.startswith(".")], [])

    def test_stale_token_or_log_alone_never_refuses(self):
        src = self.root / "stale"
        write(src / "dashboard/server.log", "log")
        write(src / "dashboard/.auth-token", "t")
        write(self.data / "projects/acme/project.md")
        self._prepare(src)
        self.assertFalse((src / "dashboard/server.log").exists())
        self.assertFalse((src / "dashboard/.auth-token").exists())
        self.assertTrue((self.data / "projects/acme/project.md").exists())
        self.assertEqual(self.marker()["schema"], 1)
        self.assertEqual(list(self.backups.glob("*")), [])

    def test_failing_tar_leaves_nothing(self):
        with mock.patch.object(datadir.tarfile.TarFile, "add", side_effect=OSError("disk")):
            with self.assertRaises(OSError):
                datadir.move_legacy(self.source, self.data)
        self.assertEqual(list(self.backups.glob("*")), [])
        self.assertTrue((self.source / "projects/demo/project.md").exists())
        self.assertEqual(list(self.data.iterdir()), [])

    def test_junk_does_not_count_as_data(self):
        write(self.data / ".DS_Store")
        write(self.data / "._x")
        write(self.data / ".marker-abc123")
        self.assertTrue(datadir.data_is_empty(self.data))
        r = datadir.move_legacy(self.source, self.data)
        self.assertIsNone(r.refused)

    def test_symlinked_top_level_item_refused(self):
        outside = self.root / "outside"
        write(outside / "a.json", "{}")
        shutil.rmtree(self.source / "chats")
        os.symlink(outside, self.source / "chats")
        r = datadir.move_legacy(self.source, self.data)
        self.assertIn("chats", r.refused)
        self.assertIn("symbolic link", r.refused)
        self.assertTrue((outside / "a.json").exists())
        self.assertTrue((self.source / "projects/demo").exists())

    def test_other_spellings_of_source_rewritten(self):
        alias = self.root / "alias"
        os.symlink(self.root, alias)
        src = alias / "clone"
        real = self.source.resolve()
        write(self.source / "chats" / "c.json", json.dumps(
            {"a": f"{src}/projects/demo", "b": f"{real}/projects/demo"}))
        datadir.move_legacy(src, self.data)
        c = json.loads((self.data / "chats/c.json").read_text())
        self.assertEqual(c["a"], f"{self.data}/projects/demo")
        self.assertEqual(c["b"], f"{self.data}/projects/demo")

    def test_low_disk_space_refuses_before_moving(self):
        usage = shutil._ntuple_diskusage(100, 100, 0)
        with mock.patch.object(datadir.shutil, "disk_usage", return_value=usage):
            r = datadir.move_legacy(self.source, self.data)
        self.assertIn("Not enough free disk space", r.refused)
        self.assertTrue((self.source / "projects/demo/project.md").exists())
        self.assertEqual(list(self.data.iterdir()), [])
        self.assertEqual(list(self.backups.glob("*")), [])

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
        self.assertIn("newer Leo Bot (schema", str(cm.exception))
        self.assertIn("leobot update", str(cm.exception))

    def test_marker_records_version_and_message_names_it(self):
        with mock.patch.object(migrations.version, "current", return_value="9.9.9"):
            migrations.write_marker(self.data, {"schema": migrations.LATEST + 1})
        self.assertEqual(json.loads((self.data / datadir.MARKER).read_text())["version"], "9.9.9")
        with self.assertRaises(SystemExit) as cm:
            migrations.run(self.data)
        self.assertIn("schema %d, version 9.9.9" % (migrations.LATEST + 1), str(cm.exception))


if __name__ == "__main__":
    unittest.main()
