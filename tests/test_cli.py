import contextlib
import fcntl
import io
import json
import os
import shutil
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import leobot
from core import datadir, migrations, paths, version


def run(*argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = leobot.main(list(argv))
    return code, out.getvalue(), err.getvalue()


class CliBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="leobot-cli-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.home = self.tmp / "home"
        self.patches = []
        for name, val in (("HOME", self.home), ("DATA", self.home / "data"), ("RUN", self.home / "run"),
                          ("LOGS", self.home / "logs"), ("BACKUPS", self.home / "backups"),
                          ("SETTINGS", self.home / "leobot.json"),
                          ("TOKEN_FILE", self.home / "run" / "auth-token"),
                          ("PID_FILE", self.home / "run" / "server.pid"),
                          ("SERVER_LOG", self.home / "logs" / "server.log")):
            p = mock.patch.object(paths, name, val)
            p.start()
            self.patches.append(p)
        self.addCleanup(lambda: [p.stop() for p in self.patches])
        paths.ensure_dirs()
        p = mock.patch.object(leobot, "_reindex")
        self.reindex = p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(leobot, "service")  # never touch the real launchd/systemd
        self.service = p.start()
        self.service.installed.return_value = False
        self.service.status.return_value = {"installed": False, "running": False, "kind": None}
        self.addCleanup(p.stop)

    def write(self, rel, text="x", base=None):
        f = Path(base or paths.DATA) / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text)
        return f

    def archive(self, members):
        """members: {name: bytes}; returns path of a tar.gz."""
        dest = self.tmp / "in.tar.gz"
        with tarfile.open(dest, "w:gz") as t:
            for name, data in members.items():
                ti = tarfile.TarInfo(name)
                ti.size = len(data)
                t.addfile(ti, io.BytesIO(data))
        return dest


class BasicTests(CliBase):
    def test_version_prints(self):
        code, out, _ = run("version")
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), version.current())

    def test_status_not_running(self):
        self.assertFalse(leobot.status_info()["running"])
        self.assertIsNone(leobot.status_info()["pid"])

    def test_status_running_via_lock(self):
        fd = os.open(paths.PID_FILE, os.O_RDWR | os.O_CREAT, 0o600)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX)
        os.write(fd, json.dumps({"pid": 4242, "port": 9999}).encode())
        info = leobot.status_info()
        self.assertTrue(info["running"])
        self.assertEqual((info["pid"], info["port"]), (4242, 9999))

    def test_stale_pid_file_is_not_running(self):
        paths.PID_FILE.write_text(json.dumps({"pid": os.getpid(), "port": 1}))
        self.assertFalse(leobot.status_info()["running"])

    def test_status_json(self):
        code, out, _ = run("status", "--json")
        self.assertEqual(code, 0)
        self.assertIn("running", json.loads(out))

    def test_start_spawns_server_from_code(self):
        proc = mock.Mock()
        proc.poll.return_value = None
        with mock.patch.object(leobot.subprocess, "Popen", return_value=proc) as popen, \
                mock.patch.object(leobot, "_proof_ok", return_value=True):
            code, out, _ = run("start", "--port", "9123")
        self.assertEqual(code, 0)
        argv = popen.call_args[0][0]
        self.assertIn(str(paths.CODE / "dashboard" / "server.py"), argv)
        self.assertIn("9123", argv)
        self.assertTrue(popen.call_args[1]["start_new_session"])
        self.assertIn("9123", out)
        self.assertNotIn("token", out)

    def test_start_reports_server_exit(self):
        proc = mock.Mock()
        proc.poll.return_value = 1
        with mock.patch.object(leobot.subprocess, "Popen", return_value=proc), \
                mock.patch.object(leobot, "_proof_ok", return_value=False):
            code, _, err = run("start")
        self.assertEqual(code, 1)
        self.assertIn("server.log", err)

    def test_stop_not_running(self):
        with mock.patch.object(leobot.os, "kill") as kill:
            code, out, _ = run("stop")
        self.assertEqual(code, 0)
        kill.assert_not_called()

    def test_stop_refuses_unproven_pid(self):
        fd = os.open(paths.PID_FILE, os.O_RDWR | os.O_CREAT, 0o600)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX)
        os.write(fd, json.dumps({"pid": 4242, "port": 9999}).encode())
        with mock.patch.object(leobot, "_proof_ok", return_value=False), \
                mock.patch.object(leobot, "_cmdline", return_value="vim notes.txt"), \
                mock.patch.object(leobot.os, "kill") as kill:
            code, _, _ = run("stop")
        self.assertEqual(code, 1)
        kill.assert_not_called()

    def test_stop_signals_proven_pid(self):
        fd = os.open(paths.PID_FILE, os.O_RDWR | os.O_CREAT, 0o600)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX)
        os.write(fd, json.dumps({"pid": 4242, "port": 9999}).encode())
        with mock.patch.object(leobot, "_proof_ok", return_value=True), \
                mock.patch.object(leobot, "_lock_held", side_effect=[True, False]), \
                mock.patch.object(leobot, "_wait_stopped", return_value=True), \
                mock.patch.object(leobot.os, "kill") as kill:
            code, _, _ = run("stop")
        self.assertEqual(code, 0)
        kill.assert_called_once_with(4242, leobot.signal.SIGTERM)


class BackupTests(CliBase):
    def test_backup_creates_archive(self):
        self.write("projects/demo/x.md", "hello")
        code, _, _ = run("backup", "--yes")
        self.assertEqual(code, 0)
        found = list(paths.BACKUPS.glob("manual-*.tar.gz"))
        self.assertEqual(len(found), 1)
        with tarfile.open(found[0]) as t:
            self.assertIn("projects/demo/x.md", t.getnames())

    def test_restore_snapshots_first(self):
        self.write("projects/demo/old.md", "old")
        arc = self.archive({"projects/demo/new.md": b"new"})
        with mock.patch.object(leobot, "status_info", return_value={"running": False, "pid": None, "port": None}):
            code, _, _ = run("backup", "--restore", str(arc), "--yes")
        self.assertEqual(code, 0)
        snaps = list(paths.BACKUPS.glob("pre-restore-*.tar.gz"))
        self.assertEqual(len(snaps), 1)
        with tarfile.open(snaps[0]) as t:
            self.assertIn("projects/demo/old.md", t.getnames())
        self.assertFalse((paths.DATA / "projects/demo/old.md").exists())
        self.assertEqual((paths.DATA / "projects/demo/new.md").read_text(), "new")

    def test_restore_rejects_path_traversal(self):
        self.write("projects/demo/keep.md", "keep")
        for bad in ("../evil", "/abs/evil"):
            arc = self.archive({"ok.md": b"1", bad: b"2"})
            code, _, err = run("backup", "--restore", str(arc), "--yes")
            self.assertNotEqual(code, 0)
        self.assertEqual((paths.DATA / "projects/demo/keep.md").read_text(), "keep")
        self.assertFalse((self.home / "evil").exists())
        self.assertFalse(list(paths.BACKUPS.glob("pre-restore-*")))

    def test_restore_rejects_hardlink(self):
        dest = self.tmp / "ln.tar.gz"
        with tarfile.open(dest, "w:gz") as t:
            ti = tarfile.TarInfo("link")
            ti.type = tarfile.LNKTYPE
            ti.linkname = "/etc/passwd"
            t.addfile(ti)
        code, _, _ = run("backup", "--restore", str(dest), "--yes")
        self.assertNotEqual(code, 0)

    def archive_with_links(self, entries):
        dest = self.tmp / "ln2.tar.gz"
        with tarfile.open(dest, "w:gz") as t:
            for name, link in entries:
                ti = tarfile.TarInfo(name)
                if link is None:
                    ti.size = 1
                    t.addfile(ti, io.BytesIO(b"p"))
                else:
                    ti.type = tarfile.SYMTYPE
                    ti.linkname = link
                    t.addfile(ti)
        return dest

    def test_restore_chained_symlink_slip_contained(self):
        self.write("keep.md", "keep")
        arc = self.archive_with_links([("x", "."), ("x/y", ".."), ("y/pwned", None)])
        code, _, _ = run("backup", "--restore", str(arc), "--yes")
        self.assertNotEqual(code, 0)
        self.assertFalse((paths.HOME / "pwned").exists())
        self.assertFalse((paths.HOME.parent / "pwned").exists())
        self.assertEqual((paths.DATA / "keep.md").read_text(), "keep")

    def test_restore_keeps_outside_symlink(self):
        outside = self.tmp / "outside-folder"
        outside.mkdir()
        arc = self.archive_with_links([("linked", str(outside)), ("a.md", None)])
        code, _, _ = run("backup", "--restore", str(arc), "--yes")
        self.assertEqual(code, 0)
        self.assertTrue((paths.DATA / "linked").is_symlink())
        self.assertEqual(os.readlink(paths.DATA / "linked"), str(outside))
        self.assertEqual(oct(paths.DATA.stat().st_mode & 0o777), "0o700")
        self.reindex.assert_called()

    def test_restore_failure_rolls_back(self):
        self.write("keep.md", "keep")
        arc = self.archive({"ok.md": b"1"})
        with mock.patch.object(leobot, "_safe_extract", side_effect=OSError("disk full")):
            code, _, err = run("backup", "--restore", str(arc), "--yes")
        self.assertEqual(code, 1)
        self.assertEqual((paths.DATA / "keep.md").read_text(), "keep")
        self.assertIn("pre-restore-", err)
        self.assertFalse(list(paths.HOME.glob("data.pre-restore-*")))

    def test_restore_that_cannot_put_data_back_does_not_start(self):
        self.write("keep.md", "keep")
        arc = self.archive({"ok.md": b"1"})
        real = os.rename

        def rename(src, dst):
            if Path(src).name.startswith("data.pre-restore-") and Path(dst) == paths.DATA:
                raise OSError(16, "Resource busy")
            return real(src, dst)
        with mock.patch.object(leobot, "_safe_extract", side_effect=OSError("disk full")), \
                mock.patch.object(leobot.os, "rename", side_effect=rename), \
                mock.patch.object(leobot, "_stop_if_running", return_value=9123), \
                mock.patch.object(leobot, "cmd_start") as start:
            code, _, err = run("backup", "--restore", str(arc), "--yes")
        self.assertEqual(code, 1)
        start.assert_not_called()  # it would create an empty data folder
        aside = next(paths.HOME.glob("data.pre-restore-*"))
        self.assertEqual((aside / "keep.md").read_text(), "keep")
        self.assertIn(f"&& mv {aside} {paths.DATA}", err)
        self.assertNotIn("rm -rf", err)
        self.assertIn("not started", err)

    def test_put_back_command_moves_partial_aside_and_restores(self):
        import subprocess
        base = self.tmp / "with space"
        base.mkdir()
        data, aside = base / "data", base / "data.pre-restore-1"
        (aside / "keep.md").parent.mkdir()
        (aside / "keep.md").write_text("keep")
        data.mkdir()
        (data / "half.md").write_text("half")
        with mock.patch.object(paths, "DATA", data):
            cmd = leobot._put_back_command(aside)
            subprocess.run(["/bin/sh", "-c", cmd], check=True)
            self.assertEqual((data / "keep.md").read_text(), "keep")
            self.assertFalse(aside.exists())
            partial = [p for p in base.iterdir() if p.name.startswith("data.partial-")]
            self.assertEqual(len(partial), 1)
            self.assertEqual((partial[0] / "half.md").read_text(), "half")  # moved, never deleted
            shutil.rmtree(data)
            aside.mkdir()
            subprocess.run(["/bin/sh", "-c", leobot._put_back_command(aside)], check=True)  # no DATA
            self.assertTrue(data.is_dir())

    def test_restore_midway_failure_rolls_back(self):
        self.write("keep.md", "keep")
        arc = self.archive_with_links([("good.md", None), ("x", "."), ("x/y", None)])
        code, _, _ = run("backup", "--restore", str(arc), "--yes")
        self.assertEqual(code, 1)
        self.assertEqual((paths.DATA / "keep.md").read_text(), "keep")
        self.assertFalse((paths.DATA / "good.md").exists())

    def test_hardlinked_files_round_trip(self):
        a = self.write("a.md", "same")
        os.link(a, paths.DATA / "b.md")
        self.assertEqual(run("backup", "--yes")[0], 0)
        arc = next(paths.BACKUPS.glob("manual-*.tar.gz"))
        with tarfile.open(arc) as t:
            self.assertFalse(any(m.islnk() for m in t.getmembers()))
        code, _, _ = run("backup", "--restore", str(arc), "--yes")
        self.assertEqual(code, 0)
        self.assertEqual((paths.DATA / "a.md").read_text(), "same")
        self.assertEqual((paths.DATA / "b.md").read_text(), "same")

    def test_legacy_tar_has_no_hardlinks(self):
        src = self.tmp / "clone"
        f = self.write("projects/demo/a.md", "x", base=src)
        os.link(f, src / "projects/demo/b.md")
        dest = datadir._tar(["projects"], src, self.tmp / "l.tar.gz")
        with tarfile.open(dest) as t:
            self.assertFalse(any(m.islnk() for m in t.getmembers()))

    def test_restore_refuses_symlinked_data(self):
        real = self.tmp / "realdata"
        real.mkdir()
        shutil.rmtree(paths.DATA)
        os.symlink(real, paths.DATA)
        code, _, err = run("backup", "--restore", str(self.archive({"a": b"1"})), "--yes")
        self.assertEqual(code, 1)
        self.assertIn(str(real), err)
        self.assertTrue(paths.DATA.is_symlink())

    def test_strict_fsync_failure_aborts_before_destructive_step(self):
        self.write("keep.md", "keep")
        with mock.patch.object(datadir, "_fsync_path", side_effect=OSError("io")):
            code, _, _ = run("backup", "--restore", str(self.archive({"n": b"1"})), "--yes")
        self.assertEqual(code, 1)
        self.assertEqual((paths.DATA / "keep.md").read_text(), "keep")

    def test_post_restore_reindex_failure_not_reported_as_unchanged(self):
        self.reindex.side_effect = OSError("boom")
        code, out, err = run("backup", "--restore", str(self.archive({"n.md": b"1"})), "--yes")
        self.assertEqual(code, 0)
        self.assertIn("Re-index failed", err)
        self.assertNotIn("Nothing", err)
        self.assertTrue((paths.DATA / "n.md").exists())

    def test_restore_source_copy_deleted_on_success(self):
        arc = paths.DATA / "inside.tar.gz"
        with tarfile.open(arc, "w:gz") as t:
            ti = tarfile.TarInfo("new.md")
            ti.size = 1
            t.addfile(ti, io.BytesIO(b"n"))
        run("backup", "--restore", str(arc), "--yes")
        self.assertFalse(list(paths.BACKUPS.glob("restore-source-*")))

    def test_restore_archive_inside_data(self):
        self.write("old.md", "old")
        arc = paths.DATA / "inside.tar.gz"
        with tarfile.open(arc, "w:gz") as t:
            ti = tarfile.TarInfo("new.md")
            ti.size = 3
            t.addfile(ti, io.BytesIO(b"new"))
        code, _, _ = run("backup", "--restore", str(arc), "--yes")
        self.assertEqual(code, 0)
        self.assertEqual((paths.DATA / "new.md").read_text(), "new")

    def test_snapshot_failure_restarts_server(self):
        arc = self.archive({"ok.md": b"1"})
        with mock.patch.object(leobot, "_stop_if_running", return_value=9123), \
                mock.patch.object(leobot, "_tar_data", side_effect=OSError("no space")), \
                mock.patch.object(leobot, "cmd_start") as start:
            code, _, _ = run("backup", "--restore", str(arc), "--yes")
        self.assertEqual(code, 1)
        self.assertEqual(start.call_args[0][0].port, 9123)

    def test_declined_prompt_changes_nothing(self):
        self.write("a.md")
        with mock.patch("builtins.input", return_value="n"):
            code, _, _ = run("backup", "--restore", str(self.archive({"b.md": b"1"})))
        self.assertNotEqual(code, 0)
        self.assertTrue((paths.DATA / "a.md").exists())


class MoveAndDoctorTests(CliBase):
    def legacy(self):
        src = self.tmp / "clone"
        self.write("projects/demo/p.md", "p", base=src)
        return src

    def test_move_data_refuses_when_both_have_data(self):
        src = self.legacy()
        self.write("projects/acme/q.md", "q")
        code, out, err = run("move-data", "--from", str(src))
        self.assertEqual(code, 1)
        res = datadir.move_legacy(src)
        self.assertIn(res.refused, out + err)

    def test_move_data_moves_and_reindexes(self):
        src = self.legacy()
        re_ = self.reindex
        if True:
            code, _, _ = run("move-data", "--from", str(src))
        self.assertEqual(code, 0)
        self.assertTrue((paths.DATA / "projects/demo/p.md").exists())
        re_.assert_called_once()

    def test_doctor_reports_legacy(self):
        src = self.legacy()
        code, out, _ = run("doctor", "--from", str(src))
        self.assertIn("leobot move-data --from", out)
        self.assertIn(str(src), out)

    def test_doctor_gives_both_choices_when_both_have_data(self):
        src = self.legacy()
        self.write("projects/acme/q.md", "q")
        code, out, _ = run("doctor", "--from", str(src))
        self.assertEqual(code, 1)
        self.assertIn(f"leobot move-data --from {src} --archive", out)
        self.assertIn(f"leobot move-data --from {src} --replace", out)

    def test_move_data_archive_ends_the_two_places_loop(self):
        src = self.legacy()
        self.write("projects/acme/q.md", "q")
        code, out, err = run("move-data", "--from", str(src), "--archive")
        self.assertEqual(code, 0, err)
        self.assertIn("legacy-archived-", out)
        self.assertTrue((paths.DATA / "projects/acme/q.md").exists())
        self.assertFalse((paths.DATA / "projects/demo").exists())
        self.assertEqual(len(list(paths.BACKUPS.glob("legacy-archived-*.tar.gz"))), 1)
        _, out, _ = run("doctor", "--from", str(src))
        self.assertIn(f"no old data in {src}", out)

    def test_move_data_archive_and_replace_exclusive(self):
        src = self.legacy()
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            leobot.main(["move-data", "--from", str(src), "--archive", "--replace"])

    def test_doctor_reports_newer_schema_on_dev_checkout(self):
        migrations.write_marker(paths.DATA, {"schema": migrations.LATEST + 1})
        _, out, _ = run("doctor")
        self.assertIn("git pull", out)

    def test_doctor_reports_interrupted_move(self):
        paths.DATA.mkdir(parents=True, exist_ok=True)
        (paths.DATA / datadir.JOURNAL).write_text(json.dumps(
            {"source": "/somewhere/else", "items": ["projects"], "stage": "moving"}))
        _, out, _ = run("doctor")
        self.assertIn("leobot move-data --from /somewhere/else", out)

    def test_doctor_reports_newer_version(self):
        paths.SETTINGS.write_text(json.dumps({"latest": "v99.0.0"}))
        _, out, _ = run("doctor")
        self.assertIn("version v99.0.0 is available", out)

    def test_doctor_marker_and_modes(self):
        migrations.write_marker(paths.DATA, {"schema": migrations.LATEST})
        _, out, _ = run("doctor")
        self.assertIn("OK", out)
        os.chmod(paths.DATA, 0o755)
        _, out, _ = run("doctor")
        self.assertIn("FIX", out)
        self.assertIn("chmod 700", out)


class DataLockTests(CliBase):
    """Restore and move-data hold the update lock and the pid lock for the whole operation."""

    def not_running(self):
        return mock.patch.object(leobot, "status_info", return_value={"running": False, "pid": None, "port": None})

    def hold(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX)

    def test_restore_holds_both_locks(self):
        from core import updater
        arc = self.archive({"projects/demo/new.md": b"new"})
        seen = {}
        real = leobot._replace_data

        def replace(src, snap):
            seen["pid"] = leobot._lock_held()
            seen["update"] = updater.is_updating()
            return real(src, snap)
        with self.not_running(), mock.patch.object(leobot, "_replace_data", side_effect=replace):
            code, _, err = run("backup", "--restore", str(arc), "--yes")
        self.assertEqual(code, 0, err)
        self.assertEqual(seen, {"pid": True, "update": True})
        self.assertFalse(leobot._lock_held())
        self.assertFalse(updater.is_updating())

    def test_restore_refused_while_updating(self):
        self.write("projects/demo/old.md", "old")
        arc = self.archive({"projects/demo/new.md": b"new"})
        self.hold(paths.RUN / "update.lock")
        with self.not_running():
            code, _, err = run("backup", "--restore", str(arc), "--yes")
        self.assertEqual(code, 1)
        self.assertIn("An update is running", err)
        self.assertTrue((paths.DATA / "projects/demo/old.md").exists())
        self.assertEqual(list(paths.BACKUPS.glob("pre-restore-*")), [])

    def test_restore_refused_when_a_server_holds_the_pid_lock(self):
        self.write("projects/demo/old.md", "old")
        arc = self.archive({"projects/demo/new.md": b"new"})
        self.hold(paths.PID_FILE)
        with self.not_running():
            code, _, err = run("backup", "--restore", str(arc), "--yes")
        self.assertEqual(code, 1)
        self.assertIn("started again", err)
        self.assertTrue((paths.DATA / "projects/demo/old.md").exists())

    def test_move_data_holds_both_locks_and_restarts_after(self):
        from core import updater
        src = self.tmp / "clone"
        self.write("projects/demo/p.md", "p", base=src)
        seen = {}
        real = datadir.move_legacy
        order = []

        def move(*a, **k):
            seen["pid"] = leobot._lock_held()
            seen["update"] = updater.is_updating()
            return real(*a, **k)

        def restart(port):
            order.append((port, leobot._lock_held(), updater.is_updating()))
        with mock.patch.object(leobot, "_stop_if_running", return_value=8765), \
                mock.patch.object(datadir, "move_legacy", side_effect=move), \
                mock.patch.object(leobot, "_restart", side_effect=restart):
            code, _, err = run("move-data", "--from", str(src))
        self.assertEqual(code, 0, err)
        self.assertEqual(seen, {"pid": True, "update": True})
        self.assertEqual(order, [(8765, False, True)])  # pid lock gone, update lock still held

    def test_move_data_refused_while_updating(self):
        src = self.tmp / "clone"
        self.write("projects/demo/p.md", "p", base=src)
        self.hold(paths.RUN / "update.lock")
        code, _, err = run("move-data", "--from", str(src))
        self.assertEqual(code, 1)
        self.assertIn("An update is running", err)
        self.assertTrue((src / "projects/demo/p.md").exists())

    def test_stale_pid_is_cleared_while_held(self):
        paths.PID_FILE.write_text(json.dumps({"pid": 999999, "port": 8765}))
        with leobot._pid_lock() as held:
            self.assertTrue(held)
            self.assertEqual(paths.PID_FILE.read_text(), "")


class UninstallTests(CliBase):
    def test_uninstall_keeps_data_without_flag(self):
        app = paths.HOME / "app"
        (app / "dashboard").mkdir(parents=True)
        (app / "dashboard" / "server.py").write_text("")
        (app / "leobot.py").write_text("")
        (app / ".git").mkdir()
        self.write("projects/demo/x.md")
        bindir = self.tmp / "fakehome"
        with mock.patch.object(leobot.Path, "home", return_value=bindir), \
                mock.patch.object(leobot, "_stop_if_running"):
            code, _, _ = run("uninstall", "--yes")
        self.assertEqual(code, 0)
        self.assertFalse((paths.HOME / "app").exists())
        self.assertTrue((paths.DATA / "projects/demo/x.md").exists())

    def test_uninstall_skips_foreign_app_folder(self):
        app = paths.HOME / "app"
        app.mkdir()
        (app / "mine.txt").write_text("keep")
        with mock.patch.object(leobot.Path, "home", return_value=self.tmp / "fakehome"), \
                mock.patch.object(leobot, "_stop_if_running"):
            code, _, err = run("uninstall", "--yes")
        self.assertEqual(code, 0)
        self.assertTrue((app / "mine.txt").exists())
        self.assertIn("Left", err)

    def test_uninstall_refuses_home_that_contains_user_home(self):
        user = self.tmp / "fakehome"
        user.mkdir()
        with mock.patch.object(leobot.Path, "home", return_value=user), \
                mock.patch.object(paths, "HOME", self.tmp), \
                mock.patch.object(leobot, "_stop_if_running") as st:
            code, _, _ = run("uninstall", "--yes")
        self.assertEqual(code, 1)
        st.assert_not_called()

    def test_home_problem_code_containment(self):
        user = self.tmp / "fakehome"
        user.mkdir()
        h = self.tmp / "h"
        (h / "data").mkdir(parents=True)
        with mock.patch.object(leobot.Path, "home", return_value=user), \
                mock.patch.object(paths, "HOME", h):
            with mock.patch.object(paths, "CODE", h / "elsewhere"):
                self.assertIn("code folder", leobot._home_problem())
            with mock.patch.object(paths, "CODE", h):
                self.assertIn("code folder", leobot._home_problem())
            with mock.patch.object(paths, "CODE", h / "app"):
                self.assertIsNone(leobot._home_problem())

    def test_uninstall_data_flag_removes_home(self):
        self.write("projects/demo/x.md")
        with mock.patch.object(leobot.Path, "home", return_value=self.tmp / "fakehome"), \
                mock.patch.object(leobot, "_stop_if_running"):
            code, _, _ = run("uninstall", "--data", "--yes")
        self.assertEqual(code, 0)
        self.assertFalse(paths.HOME.exists())

    def test_uninstall_refuses_unsafe_home(self):
        user = self.tmp / "fakehome"
        user.mkdir()
        with mock.patch.object(leobot.Path, "home", return_value=user), \
                mock.patch.object(leobot, "_stop_if_running"):
            for bad in (user, self.tmp):  # the user's home, and a folder containing it
                with mock.patch.object(paths, "HOME", bad):
                    code, _, err = run("uninstall", "--data", "--yes")
                    self.assertEqual(code, 1, bad)
                    self.assertIn("Refusing", err)
            stuffed = self.tmp / "stuffed"
            (stuffed / "data").mkdir(parents=True)
            (stuffed / "photos").mkdir()
            with mock.patch.object(paths, "HOME", stuffed):
                code, _, err = run("uninstall", "--data", "--yes")
            self.assertEqual(code, 1)
            self.assertIn("photos", err)
            empty = self.tmp / "emptyhome"
            empty.mkdir()
            with mock.patch.object(paths, "HOME", empty):
                self.assertEqual(run("uninstall", "--data", "--yes")[0], 1)
        self.assertTrue(self.tmp.exists() and user.exists() and stuffed.exists())

    def test_uninstall_leaves_foreign_launcher(self):
        user = self.tmp / "fakehome"
        link = user / ".local" / "bin" / "leobot"
        link.parent.mkdir(parents=True)
        link.write_text("#!/bin/sh\necho other tool\n")
        with mock.patch.object(leobot.Path, "home", return_value=user), \
                mock.patch.object(leobot, "_stop_if_running"):
            run("uninstall", "--yes")
            self.assertTrue(link.exists())
            link.write_text("#!/bin/sh\nexec python3 /x/leobot.py \"$@\"\n")
            run("uninstall", "--yes")
        self.assertFalse(link.exists())

    def test_uninstall_declined(self):
        (paths.HOME / "app").mkdir()
        with mock.patch.object(leobot.Path, "home", return_value=self.tmp / "fakehome"), \
                mock.patch("builtins.input", return_value=""):
            code, _, _ = run("uninstall")
        self.assertNotEqual(code, 0)
        self.assertTrue((paths.HOME / "app").exists())


class ServiceWiringTests(CliBase):
    def test_stop_goes_through_service(self):
        self.service.installed.return_value = True
        self.service.kind.return_value = "launchd"
        with mock.patch.object(leobot, "status_info", return_value={"running": True, "pid": 4242, "port": 9001}), \
                mock.patch.object(leobot, "_wait_stopped", return_value=True), \
                mock.patch.object(leobot.os, "kill") as kill:
            code, out, _ = run("stop")
        self.assertEqual(code, 0)
        self.service.stop.assert_called_once()
        kill.assert_not_called()
        self.assertIn("stopped", out)
        self.assertIn("next login", out)

    def test_start_notes_port_mismatch(self):
        self.service.installed.return_value = True
        self.service.port.return_value = 9001
        up = {"running": True, "pid": 1, "port": 9001}
        down = {"running": False, "pid": None, "port": None}
        with mock.patch.object(leobot, "_proof_ok", return_value=True):
            with mock.patch.object(leobot, "status_info", side_effect=[down, up, up]):
                code, _, err = run("start", "--port", "9555")
            self.assertIn("9001", err)
            with mock.patch.object(leobot, "status_info", side_effect=[down, up, up]):
                code, _, err = run("start")
            self.assertEqual(err, "")

    def test_service_install_reports_no_answer(self):
        self.service.install.return_value = "ok"
        with mock.patch.object(leobot, "_stop_if_running", return_value=None), \
                mock.patch.object(leobot, "_wait_ready", return_value=None):
            code, _, err = run("service", "install")
        self.assertEqual(code, 1)
        self.assertIn("server.log", err)

    def test_start_goes_through_service(self):
        self.service.installed.return_value = True
        infos = iter([{"running": False, "pid": None, "port": None},
                      {"running": True, "pid": 1, "port": 9001}, {"running": True, "pid": 1, "port": 9001}])
        with mock.patch.object(leobot, "status_info", side_effect=lambda: next(infos)), \
                mock.patch.object(leobot, "_proof_ok", return_value=True), \
                mock.patch.object(leobot.subprocess, "Popen") as popen:
            code, _, _ = run("start")
        self.assertEqual(code, 0)
        self.service.start.assert_called_once()
        popen.assert_not_called()

    def test_service_cli(self):
        self.service.install.return_value = "installed it"
        with mock.patch.object(leobot, "_stop_if_running", return_value=None), \
                mock.patch.object(leobot, "_wait_ready", return_value=9100):
            code, out, _ = run("service", "install", "--port", "9100", "--server")
        self.assertEqual(code, 0)
        self.service.install.assert_called_once_with(port=9100, server=True)
        self.assertIn("installed it", out)
        self.assertEqual(run("service", "uninstall")[0], 0)
        self.service.uninstall.assert_called_once()
        self.service.status.return_value = {"installed": True, "running": True, "kind": "launchd"}
        code, out, _ = run("service", "status")
        self.assertIn("launchd", out)

    def test_service_install_failure(self):
        self.service.install.side_effect = RuntimeError("nope")
        with mock.patch.object(leobot, "_stop_if_running", return_value=None):
            code, _, err = run("service", "install")
        self.assertEqual(code, 1)
        self.assertIn("nope", err)

    def test_uninstall_refusal_before_service_removal(self):
        user = self.tmp / "fakehome"
        user.mkdir()
        with mock.patch.object(leobot.Path, "home", return_value=user), \
                mock.patch.object(paths, "HOME", self.tmp), \
                mock.patch.object(leobot, "_stop_if_running") as st:
            code, _, _ = run("uninstall", "--yes")
        self.assertEqual(code, 1)
        self.service.uninstall.assert_not_called()
        st.assert_not_called()

    def test_uninstall_removes_service(self):
        with mock.patch.object(leobot.Path, "home", return_value=self.tmp / "fakehome"), \
                mock.patch.object(leobot, "_stop_if_running"):
            run("uninstall", "--yes")
        self.service.uninstall.assert_called_once()

    def test_restore_snapshot_failure_removes_source_copy(self):
        self.write("a.txt")
        src = paths.DATA / "inside.tar.gz"
        with tarfile.open(src, "w:gz"):
            pass
        with mock.patch.object(leobot, "_tar_data", side_effect=OSError("disk full")):
            code, _, _ = run("backup", "--restore", str(src), "--yes")
        self.assertEqual(code, 1)
        self.assertEqual(list(paths.BACKUPS.glob("restore-source-*")), [])


if __name__ == "__main__":
    unittest.main()
