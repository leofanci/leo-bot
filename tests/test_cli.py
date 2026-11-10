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

    def test_restore_rejects_link_escape(self):
        dest = self.tmp / "ln.tar.gz"
        with tarfile.open(dest, "w:gz") as t:
            ti = tarfile.TarInfo("link")
            ti.type = tarfile.SYMTYPE
            ti.linkname = "../../outside"
            t.addfile(ti)
        code, _, _ = run("backup", "--restore", str(dest), "--yes")
        self.assertNotEqual(code, 0)

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
        with mock.patch.object(leobot, "_reindex") as re_:
            code, _, _ = run("move-data", "--from", str(src))
        self.assertEqual(code, 0)
        self.assertTrue((paths.DATA / "projects/demo/p.md").exists())
        re_.assert_called_once()

    def test_doctor_reports_legacy(self):
        src = self.legacy()
        code, out, _ = run("doctor", "--from", str(src))
        self.assertIn("leobot move-data --from", out)
        self.assertIn(str(src), out)

    def test_doctor_reports_interrupted_move(self):
        paths.DATA.mkdir(parents=True, exist_ok=True)
        (paths.DATA / datadir.JOURNAL).write_text(json.dumps(
            {"source": "/somewhere/else", "items": ["projects"], "stage": "moving"}))
        _, out, _ = run("doctor")
        self.assertIn("leobot move-data --from /somewhere/else", out)

    def test_doctor_marker_and_modes(self):
        migrations.write_marker(paths.DATA, {"schema": migrations.LATEST})
        _, out, _ = run("doctor")
        self.assertIn("OK", out)
        os.chmod(paths.DATA, 0o755)
        _, out, _ = run("doctor")
        self.assertIn("FIX", out)
        self.assertIn("chmod 700", out)


class UninstallTests(CliBase):
    def test_uninstall_keeps_data_without_flag(self):
        (paths.HOME / "app").mkdir()
        self.write("projects/demo/x.md")
        bindir = self.tmp / "fakehome"
        with mock.patch.object(leobot.Path, "home", return_value=bindir), \
                mock.patch.object(leobot, "_stop_if_running"):
            code, _, _ = run("uninstall", "--yes")
        self.assertEqual(code, 0)
        self.assertFalse((paths.HOME / "app").exists())
        self.assertTrue((paths.DATA / "projects/demo/x.md").exists())

    def test_uninstall_data_flag_removes_home(self):
        self.write("projects/demo/x.md")
        with mock.patch.object(leobot.Path, "home", return_value=self.tmp / "fakehome"), \
                mock.patch.object(leobot, "_stop_if_running"):
            code, _, _ = run("uninstall", "--data", "--yes")
        self.assertEqual(code, 0)
        self.assertFalse(paths.HOME.exists())

    def test_uninstall_declined(self):
        (paths.HOME / "app").mkdir()
        with mock.patch("builtins.input", return_value=""):
            code, _, _ = run("uninstall")
        self.assertNotEqual(code, 0)
        self.assertTrue((paths.HOME / "app").exists())


if __name__ == "__main__":
    unittest.main()
