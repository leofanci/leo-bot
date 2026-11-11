"""leobot update: a real git fixture (bare origin over file://, clone at HOME/app) and a fake
server that reports the checked-out VERSION. Never touches the real home folder, launchd or
systemd, and never sleeps for real."""
import contextlib
import fcntl
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import leobot
from core import migrations, paths, updater

GIT_ENV = {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0",
           "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.com",
           "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@example.com"}


def git(cwd, *args) -> str:
    r = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True,
                       env={**os.environ, **GIT_ENV}, check=True)
    return r.stdout.strip()


def commit_version(work: Path, ver: str, tag: "str | None", annotated=False) -> None:
    (work / "VERSION").write_text(ver + "\n")
    (work / "README.md").write_text(f"demo {ver}\n")
    git(work, "add", "-A")
    git(work, "commit", "-q", "-m", f"version {ver}")
    if tag and annotated:
        git(work, "tag", "-a", tag, "-m", tag)
    elif tag:
        git(work, "tag", tag)


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def monotonic(self):
        return self.t

    def sleep(self, s):
        self.t += max(s, 0.01)


class FakeServer:
    """Stands in for the real server: healthz reports whatever VERSION is checked out."""

    def __init__(self, code: Path):
        self.code = code
        self.running = True
        self.launches = []
        self.stops = 0
        self.bad_versions = set()
        self.on_launch = None
        self.on_stop = None

    def version(self):
        return (self.code / "VERSION").read_text().strip()

    def stop(self):
        self.stops += 1
        was = self.running
        self.running = False
        if self.on_stop:
            self.on_stop()
        return 8765 if was else None

    def launch(self, port):
        self.running = True
        self.launches.append((port, self.version()))
        if self.on_launch:
            self.on_launch(self.version())
        return None

    def healthz(self, port):
        if not self.running:
            return None
        v = self.version()
        return {"app": "Leo Bot", "version": "0.0.0-broken" if v in self.bad_versions else v, "proof": "ok"}


def data_tree(root: Path) -> dict:
    out = {}
    for p in sorted(root.rglob("*")):
        if p.is_file():
            out[str(p.relative_to(root))] = p.read_bytes()
    return out


def no_put_back():
    """os.rename that refuses to move the set-aside data folder back into place."""
    real = os.rename

    def rename(src, dst):
        if Path(src).name.startswith("data.pre-restore-") and Path(dst) == paths.DATA:
            raise OSError(16, "Resource busy")
        return real(src, dst)
    return mock.patch.object(leobot.os, "rename", side_effect=rename)


class Template:
    """A work repo with v0.1.0 (lightweight) and v0.2.0 (annotated), and its bare origin."""
    root = None

    @classmethod
    def build(cls):
        if cls.root:
            return cls.root
        cls.root = Path(tempfile.mkdtemp(prefix="leobot-upd-tpl-"))
        work = cls.root / "work"
        work.mkdir()
        git(work, "init", "-q", "-b", "main")
        commit_version(work, "0.1.0", "v0.1.0")
        commit_version(work, "0.2.0", "v0.2.0", annotated=True)
        git(cls.root, "clone", "-q", "--bare", str(work), str(cls.root / "origin.git"))
        git(cls.root, "clone", "-q", (cls.root / "origin.git").as_uri(), str(cls.root / "app"))
        git(cls.root / "app", "checkout", "-q", "--detach", "v0.1.0")
        return cls.root


def tearDownModule():
    if Template.root:
        shutil.rmtree(Template.root, ignore_errors=True)


class UpdaterBase(unittest.TestCase):
    def setUp(self):
        tpl = Template.build()
        self.tmp = Path(tempfile.mkdtemp(prefix="leobot-upd-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        env = mock.patch.dict(os.environ, GIT_ENV)
        env.start()
        self.addCleanup(env.stop)
        self.home = self.tmp / "home"
        self.work = self.tmp / "work"
        shutil.copytree(tpl / "work", self.work)
        self.origin = self.tmp / "origin.git"
        shutil.copytree(tpl / "origin.git", self.origin)
        self.app = self.home / "app"
        for name, val in (("HOME", self.home), ("DATA", self.home / "data"), ("RUN", self.home / "run"),
                          ("LOGS", self.home / "logs"), ("BACKUPS", self.home / "backups"),
                          ("SETTINGS", self.home / "leobot.json"),
                          ("TOKEN_FILE", self.home / "run" / "auth-token"),
                          ("PID_FILE", self.home / "run" / "server.pid"),
                          ("SERVER_LOG", self.home / "logs" / "server.log"),
                          ("UPDATE_LOG", self.home / "logs" / "update.log"),
                          ("CODE", self.app)):
            p = mock.patch.object(paths, name, val)
            p.start()
            self.addCleanup(p.stop)
        paths.ensure_dirs()
        shutil.copytree(tpl / "app", self.app, symlinks=True)
        git(self.app, "remote", "set-url", "origin", self.origin.as_uri())
        self.write_data("projects/demo/a.md", "one")
        migrations.write_marker(paths.DATA, {"schema": 1})
        self.server = FakeServer(self.app)
        for name, fn in (("_stop_if_running", self.server.stop), ("_launch", self.server.launch),
                         ("_healthz", self.server.healthz)):
            p = mock.patch.object(leobot, name, side_effect=fn)
            p.start()
            self.addCleanup(p.stop)
        for target, name in ((leobot, "_reindex"), (updater, "_ensure_app")):
            p = mock.patch.object(target, name)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(leobot, "service")  # never the real launchd/systemd
        self.leobot_service = p.start()
        self.leobot_service.installed.return_value = False
        self.addCleanup(p.stop)
        p = mock.patch.object(updater, "service")
        self.service = p.start()
        self.service.installed.return_value = False
        self.service.kind.return_value = None
        self.addCleanup(p.stop)
        self.clock = FakeClock()
        p = mock.patch.object(updater, "time", self.clock)
        p.start()
        self.addCleanup(p.stop)

    def write_data(self, rel, text):
        f = paths.DATA / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text)
        return f

    def head(self):
        return git(self.app, "rev-parse", "HEAD")

    def sha(self, ref):
        return git(self.app, "rev-parse", f"{ref}^{{commit}}")

    def call(self, fn, *args, **kw):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = fn(*args, **kw)
        return rc, out.getvalue() + err.getvalue()

    def pre_backups(self):
        return sorted(p.name for p in paths.BACKUPS.glob("pre-*.tar.gz") if not p.name.startswith("pre-restore-"))


class CheckTests(UpdaterBase):
    def test_check_finds_newer_tag(self):
        res = updater.check()
        self.assertTrue(res["available"])
        self.assertEqual(res["latest"], "v0.2.0")
        self.assertEqual(res["current"], "0.1.0")
        self.assertEqual(res["channel"], "stable")
        s = updater.settings()
        self.assertEqual(s["latest"], "v0.2.0")
        self.assertTrue(s["checked_at"])

    def test_check_offline(self):
        shutil.rmtree(self.origin)
        res = updater.check()
        self.assertIsNone(res["latest"])
        self.assertFalse(res["available"])

    def test_check_up_to_date(self):
        git(self.app, "checkout", "-q", "--detach", "v0.2.0")
        res = updater.check()
        self.assertFalse(res["available"])
        self.assertEqual(res["latest"], "v0.2.0")

    def test_check_main_channel_compares_sha(self):
        updater.save_settings({"channel": "main"})
        commit_version(self.work, "0.3.0", None)
        git(self.work, "push", "-q", self.origin.as_uri(), "main")
        res = updater.check()
        self.assertTrue(res["available"])
        self.assertEqual(res["channel"], "main")
        self.assertEqual(res["latest"], git(self.work, "rev-parse", "HEAD"))

    def test_release_notes_url(self):
        self.assertIsNone(updater.release_notes_url(self.app, "v0.2.0"))  # file:// origin
        git(self.app, "remote", "set-url", "origin", "https://github.com/acme/demo.git")
        self.assertEqual(updater.release_notes_url(self.app, "v0.2.0"),
                         "https://github.com/acme/demo/releases/tag/v0.2.0")
        self.assertEqual(updater.release_notes_url(self.app), "https://github.com/acme/demo/commits/main")

    def test_settings_defaults_and_atomic_private_write(self):
        self.assertEqual(updater.settings()["channel"], "stable")
        paths.SETTINGS.write_text(json.dumps({"other": 1}))
        updater.save_settings({"channel": "main"})
        self.assertEqual(paths.SETTINGS.stat().st_mode & 0o777, 0o600)
        raw = json.loads(paths.SETTINGS.read_text())
        self.assertEqual(raw["channel"], "main")
        self.assertEqual(raw["other"], 1)
        self.assertFalse([p for p in paths.HOME.iterdir() if p.name.startswith(".leobot-json-")])

    def test_bad_channel_falls_back_to_stable(self):
        paths.SETTINGS.write_text(json.dumps({"channel": "nightly"}))
        self.assertEqual(updater.settings()["channel"], "stable")


class RefuseTests(UpdaterBase):
    def test_nothing_newer(self):
        git(self.app, "checkout", "-q", "--detach", "v0.2.0")
        rc, out = self.call(updater.apply)
        self.assertEqual(rc, 0)
        self.assertIn("up to date", out)
        self.assertEqual(self.pre_backups(), [])
        self.assertEqual(self.server.stops, 0)

    def test_dev_checkout_refused(self):
        with mock.patch.object(paths, "is_installed", return_value=False):
            rc, out = self.call(updater.apply)
        self.assertEqual(rc, 1)
        self.assertIn("development checkout", out)
        self.assertEqual(self.pre_backups(), [])

    def test_dirty_checkout_refused(self):
        (self.app / "README.md").write_text("local edit\n")
        rc, out = self.call(updater.apply)
        self.assertEqual(rc, 1)
        self.assertIn("local changes", out)
        self.assertEqual(self.pre_backups(), [])
        self.assertEqual(self.head(), self.sha("v0.1.0"))
        self.assertEqual(self.server.stops, 0)

    def test_non_https_origin_refused(self):
        git(self.app, "remote", "set-url", "origin", "ssh://git.example.com/demo.git")
        rc, out = self.call(updater.apply)
        self.assertEqual(rc, 1)
        self.assertIn("https://", out)
        self.assertEqual(self.pre_backups(), [])

    def test_offline_apply_changes_nothing(self):
        shutil.rmtree(self.origin)
        rc, out = self.call(updater.apply)
        self.assertEqual(rc, 1)
        self.assertEqual(self.head(), self.sha("v0.1.0"))
        self.assertEqual(self.server.stops, 0)

    def test_bad_version_flag_refused(self):
        rc, out = self.call(updater.apply, "main")
        self.assertEqual(rc, 1)
        self.assertEqual(self.head(), self.sha("v0.1.0"))


class ApplyTests(UpdaterBase):
    def test_apply_moves_to_target_and_backs_up(self):
        old = self.head()
        rc, out = self.call(updater.apply)
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.head(), self.sha("v0.2.0"))
        backups = self.pre_backups()
        self.assertEqual(len(backups), 1)
        self.assertTrue(backups[0].startswith("pre-v0.2.0-"))
        self.assertEqual(updater.settings()["previous_ref"], old)
        with tarfile.open(paths.BACKUPS / backups[0]) as t:
            self.assertIn("projects/demo/a.md", t.getnames())
        self.assertEqual(self.server.launches[-1][1], "0.2.0")
        self.assertTrue(self.server.running)
        log = paths.UPDATE_LOG
        self.assertEqual(log.stat().st_mode & 0o777, 0o600)
        self.assertIn("v0.2.0", log.read_text())
        self.assertRegex(log.read_text(), r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d")

    def test_stable_ignores_untagged_main(self):
        commit_version(self.work, "0.3.0", None)
        git(self.work, "push", "-q", self.origin.as_uri(), "main")
        rc, out = self.call(updater.apply)
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.head(), self.sha("v0.2.0"))
        self.assertEqual((self.app / "VERSION").read_text().strip(), "0.2.0")

    def test_main_channel_moves_to_origin_main(self):
        commit_version(self.work, "0.3.0", None)
        git(self.work, "push", "-q", self.origin.as_uri(), "main")
        rc, out = self.call(updater.apply, channel="main")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.head(), git(self.work, "rev-parse", "HEAD"))
        self.assertTrue(self.pre_backups()[0].startswith("pre-main-"))

    def untagged_main_at(self, ver):
        """The app on an untagged origin/main commit whose VERSION is `ver` (a main-channel
        install that switched to stable)."""
        (self.work / "VERSION").write_text(ver + "\n")
        (self.work / "NOTES.md").write_text(f"after the release, still {ver}\n")
        git(self.work, "add", "-A")
        git(self.work, "commit", "-q", "-m", f"untagged {ver}")
        git(self.work, "push", "-q", self.origin.as_uri(), "main")
        git(self.app, "fetch", "-q", "origin", "+refs/heads/main:refs/remotes/origin/main")
        git(self.app, "checkout", "-q", "--detach", "origin/main")
        return self.head()

    def test_stable_same_version_on_untagged_main_checks_out_the_tag(self):
        main = self.untagged_main_at("0.2.0")
        self.assertNotEqual(main, self.sha("v0.2.0"))
        self.assertTrue(updater.check()["available"])
        self.assertEqual(updater.settings()["latest_commit"], self.sha("v0.2.0"))
        rc, out = self.call(updater.apply)
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.head(), self.sha("v0.2.0"))
        self.assertFalse(updater.check()["available"])

    def test_stable_never_moves_back_from_a_newer_main(self):
        main = self.untagged_main_at("0.3.0")
        self.assertFalse(updater.check()["available"])
        rc, out = self.call(updater.apply)
        self.assertEqual(rc, 0, out)
        self.assertIn("up to date", out)
        self.assertEqual(self.head(), main)
        self.assertEqual(self.server.stops, 0)

    def test_explicit_same_version_on_another_commit_checks_out_the_tag(self):
        self.untagged_main_at("0.2.0")
        rc, out = self.call(updater.apply, "v0.2.0")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.head(), self.sha("v0.2.0"))

    def test_explicit_same_version_on_its_commit_is_up_to_date(self):
        git(self.app, "checkout", "-q", "--detach", "v0.2.0")
        rc, out = self.call(updater.apply, "v0.2.0")
        self.assertEqual(rc, 0, out)
        self.assertIn("up to date", out)
        self.assertEqual(self.server.stops, 0)

    def test_failed_same_version_target_is_not_offered_again(self):
        self.untagged_main_at("0.2.0")
        self.server.bad_versions = {"0.2.0"}  # say a schema bump without a VERSION bump
        rc, out = self.call(updater.apply)
        self.assertEqual(rc, 1)
        tag_commit = self.sha("v0.2.0")
        self.assertEqual(updater.settings()["failed_commit"], tag_commit)
        self.assertFalse(updater.check()["available"])
        self.assertFalse(updater.cached_available(updater.settings(), "0.2.0", self.head))
        stops = self.server.stops
        rc, out = self.call(updater.apply)
        self.assertEqual(rc, 1)
        self.assertIn("did not start when it was tried last time", out)
        self.assertIn("leobot update --version v0.2.0", out)
        self.assertEqual(self.server.stops, stops)  # refused before anything was stopped
        self.server.bad_versions = set()
        rc, out = self.call(updater.apply, "v0.2.0")  # asked for explicitly: tried again
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.head(), tag_commit)
        self.assertIsNone(updater.settings()["failed_commit"])

    def test_failed_commit_cleared_when_a_newer_release_appears(self):
        self.server.bad_versions = {"0.2.0"}
        rc, _ = self.call(updater.apply)
        self.assertEqual(rc, 1)
        self.assertEqual(updater.settings()["failed_commit"], self.sha("v0.2.0"))
        self.assertFalse(updater.check()["available"])
        commit_version(self.work, "0.3.0", "v0.3.0")
        git(self.work, "push", "-q", "--tags", self.origin.as_uri(), "main")
        self.assertTrue(updater.check()["available"])
        self.assertIsNone(updater.settings()["failed_commit"])

    def test_main_channel_failed_commit_not_offered(self):
        commit_version(self.work, "0.3.0", None)
        git(self.work, "push", "-q", self.origin.as_uri(), "main")
        self.server.bad_versions = {"0.3.0"}
        rc, _ = self.call(updater.apply, channel="main")
        self.assertEqual(rc, 1)
        updater.save_settings({"channel": "main"})
        self.assertFalse(updater.check()["available"])
        rc, out = self.call(updater.apply, channel="main")
        self.assertEqual(rc, 1)
        self.assertIn("once main moves on", out)

    def test_stable_update_needed_rules(self):
        a, b = "a" * 40, "b" * 40
        self.assertTrue(updater.stable_update_needed("0.1.0", a, "v0.2.0", b))
        self.assertTrue(updater.stable_update_needed("0.2.0", a, "v0.2.0", b))
        self.assertFalse(updater.stable_update_needed("0.2.0", a, "v0.2.0", a))
        self.assertFalse(updater.stable_update_needed("0.3.0", a, "v0.2.0", b))
        self.assertFalse(updater.stable_update_needed("0.2.0", a, a, None))  # a main sha cached as latest
        self.assertFalse(updater.stable_update_needed("0.2.0", a, "v0.2.0", None))
        self.assertFalse(updater.stable_update_needed("0.1.0", a, "v0.2.0", b, failed=b))
        self.assertTrue(updater.stable_update_needed("0.1.0", a, "v0.2.0", b, failed=a))

    def test_explicit_version(self):
        git(self.app, "checkout", "-q", "--detach", "v0.2.0")
        rc, out = self.call(updater.apply, "v0.1.0")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.head(), self.sha("v0.1.0"))

    def test_unknown_version_tag_refused_and_restarted(self):
        rc, out = self.call(updater.apply, "v9.9.9")
        self.assertEqual(rc, 1)
        self.assertEqual(self.head(), self.sha("v0.1.0"))

    def test_fetch_failure_leaves_old_code(self):
        self.assertTrue(updater.check()["available"])
        shutil.rmtree(self.origin)  # origin gone after the check
        rc, out = self.call(updater.apply)
        self.assertEqual(rc, 1)
        self.assertEqual(self.head(), self.sha("v0.1.0"))
        self.assertTrue(self.server.running)

    def fail_git(self, verb, times=None):
        real = updater._git
        left = [times]

        def fake(code, *args, **kw):
            if args and args[0] == verb and (left[0] is None or left[0] > 0):
                if left[0] is not None:
                    left[0] -= 1
                return subprocess.CompletedProcess(args, 1, "", f"fatal: {verb} broke")
            return real(code, *args, **kw)
        return mock.patch.object(updater, "_git", side_effect=fake)

    def test_download_failure_keeps_server_running(self):
        with self.fail_git("fetch"):
            rc, out = self.call(updater.apply)
        self.assertEqual(rc, 1)
        self.assertIn("fetch broke", out)
        self.assertEqual(self.head(), self.sha("v0.1.0"))
        self.assertEqual(self.server.stops, 0)  # downloaded before stopping anything
        self.assertTrue(self.server.running)

    def test_checkout_failure_restarts_old_code(self):
        with self.fail_git("checkout", times=1):
            rc, out = self.call(updater.apply)
        self.assertEqual(rc, 1)
        self.assertEqual(self.head(), self.sha("v0.1.0"))
        self.assertTrue(self.server.running)
        self.assertEqual(self.server.launches[-1][1], "0.1.0")
        self.assertEqual(len(self.pre_backups()), 1)

    def test_checkout_back_failure_stays_stopped_with_commands(self):
        head = self.head()
        with self.fail_git("checkout"):
            rc, out = self.call(updater.apply)
        self.assertEqual(rc, 1)
        self.assertFalse(self.server.running)  # never the new code on old data, nor a broken tree
        self.assertIn(f"checkout --force --detach {head}", out)
        self.assertIn("leobot start", out)

    def test_settings_failure_before_stop_changes_nothing(self):
        with mock.patch.object(updater, "save_settings", side_effect=OSError(28, "No space left on device")):
            rc, out = self.call(updater.apply)
        self.assertEqual(rc, 1)
        self.assertEqual(self.server.stops, 0)
        self.assertTrue(self.server.running)
        self.assertEqual(self.head(), self.sha("v0.1.0"))

    def test_error_after_stop_restarts_server(self):
        with mock.patch.object(updater, "_prune", side_effect=OSError(5, "I/O error")):
            rc, out = self.call(updater.apply)
        self.assertEqual(rc, 1)
        self.assertEqual(self.head(), self.sha("v0.1.0"))
        self.assertTrue(self.server.running)
        self.assertEqual(self.server.launches[-1][1], "0.1.0")

    def test_error_after_checkout_goes_back(self):
        with mock.patch.object(updater, "_ensure_app", side_effect=[OSError(5, "I/O error"), None]) as ens:
            rc, out = self.call(updater.apply)
        self.assertEqual(rc, 1)
        self.assertEqual(self.head(), self.sha("v0.1.0"))
        self.assertTrue(self.server.running)
        self.assertEqual(self.server.launches[-1][1], "0.1.0")
        self.assertEqual(ens.call_count, 2)  # also after going back

    def test_service_is_stopped_even_when_not_running(self):
        self.server.running = False
        self.leobot_service.installed.return_value = True
        self.leobot_service.port.return_value = 9100
        with mock.patch.object(leobot, "cmd_stop", return_value=0) as stop:
            rc, out = self.call(updater.apply)
        self.assertEqual(rc, 0, out)
        stop.assert_called()
        self.assertEqual(self.server.launches[-1], (9100, "0.2.0"))

    def test_restore_failure_in_recovery_keeps_target_and_stops(self):
        self.server.bad_versions = {"0.2.0"}
        self.server.on_launch = lambda v: v == "0.2.0" and migrations.write_marker(paths.DATA, {"schema": 2})
        head = self.head()
        with mock.patch.object(leobot, "_safe_extract", side_effect=ValueError("bad archive")):
            rc, out = self.call(updater.apply)
        self.assertEqual(rc, 1)
        self.assertEqual(self.head(), self.sha("v0.2.0"))  # older code never opens newer data
        self.assertFalse(self.server.running)
        self.assertEqual(self.server.launches[-1][1], "0.2.0")
        pre = paths.BACKUPS / self.pre_backups()[0]
        self.assertIn(f"leobot backup --restore {pre} --yes", out)
        self.assertIn(f"checkout --force --detach {head}", out)
        self.assertNotIn("did not change", out)
        self.assertEqual(migrations.read_marker(paths.DATA)["schema"], 2)

    def test_migration_cut_short_counts_as_changed(self):
        before = data_tree(paths.DATA)
        self.server.bad_versions = {"0.2.0"}

        def half(ver):
            if ver == "0.2.0":  # killed mid-step: schema still 1, but the data is half migrated
                migrations.write_marker(paths.DATA, {"schema": 1, "migrating": 2})
                self.write_data("projects/demo/a.md", "half migrated")
        self.server.on_launch = half
        rc, out = self.call(updater.apply)
        self.assertEqual(rc, 1)
        self.assertEqual(len(list(paths.BACKUPS.glob("failed-v0.2.0-*.tar.gz"))), 1)
        self.assertEqual(data_tree(paths.DATA), before)

    def test_any_marker_change_counts_as_changed(self):
        self.server.bad_versions = {"0.2.0"}
        self.server.on_launch = lambda v: v == "0.2.0" and migrations.write_marker(
            paths.DATA, {"schema": 1, "note": "touched"})
        rc, out = self.call(updater.apply)
        self.assertEqual(rc, 1)
        self.assertEqual(len(list(paths.BACKUPS.glob("failed-v0.2.0-*.tar.gz"))), 1)
        self.assertNotIn("note", migrations.read_marker(paths.DATA))

    def test_failed_health_rolls_back_and_restores(self):
        before = data_tree(paths.DATA)
        self.server.bad_versions = {"0.2.0"}

        def migrate(ver):
            if ver == "0.2.0":  # the new code migrates and writes data
                migrations.write_marker(paths.DATA, {"schema": 2})
                self.write_data("projects/demo/new.md", "written by the new version")
        self.server.on_launch = migrate
        rc, out = self.call(updater.apply)
        self.assertEqual(rc, 1)
        self.assertEqual(self.head(), self.sha("v0.1.0"))
        failed = list(paths.BACKUPS.glob("failed-v0.2.0-*.tar.gz"))
        self.assertEqual(len(failed), 1)
        with tarfile.open(failed[0]) as t:
            self.assertIn("projects/demo/new.md", t.getnames())
        self.assertEqual(data_tree(paths.DATA), before)
        pre = paths.BACKUPS / self.pre_backups()[0]
        self.assertIn(str(pre), out)
        self.assertIn(str(failed[0]), out)
        self.assertTrue(self.server.running)
        self.assertEqual(self.server.launches[-1][1], "0.1.0")
        self.assertFalse(list(paths.HOME.glob("data.pre-restore-*")))

    def test_failed_health_without_schema_change_keeps_data(self):
        self.server.bad_versions = {"0.2.0"}
        self.server.on_launch = lambda v: v == "0.2.0" and self.write_data("projects/demo/new.md", "n")
        rc, out = self.call(updater.apply)
        self.assertEqual(rc, 1)
        self.assertEqual(self.head(), self.sha("v0.1.0"))
        self.assertFalse(list(paths.BACKUPS.glob("failed-*")))
        self.assertTrue((paths.DATA / "projects/demo/new.md").exists())  # nothing written is lost

    def test_backups_pruned_to_five(self):
        for i in range(7):
            (paths.BACKUPS / f"pre-v0.0.{i}-2026010{i}-000000-000000.tar.gz").write_bytes(b"x")
        keep = ["manual-20260101-000000-000000.tar.gz", "legacy-20260101-000000-000000.tar.gz",
                "failed-v0.0.1-20260101-000000-000000.tar.gz", "replaced-20260101-000000-000000.tar.gz",
                "pre-restore-20260101-000000-000000.tar.gz"]
        for n in keep:
            (paths.BACKUPS / n).write_bytes(b"x")
        rc, out = self.call(updater.apply)
        self.assertEqual(rc, 0, out)
        pre = self.pre_backups()
        self.assertEqual(len(pre), 5)
        self.assertTrue(any(n.startswith("pre-v0.2.0-") for n in pre))
        self.assertNotIn("pre-v0.0.0-20260100-000000-000000.tar.gz", pre)
        for n in keep:
            self.assertTrue((paths.BACKUPS / n).exists(), n)

    def test_stop_failure_changes_nothing_but_backup(self):
        leobot._stop_if_running.side_effect = SystemExit("Could not stop Leo Bot. Nothing was changed.")
        rc, out = self.call(updater.apply)
        self.assertEqual(rc, 1)
        self.assertEqual(self.head(), self.sha("v0.1.0"))

    def test_service_that_cannot_be_stopped_stops_the_update(self):
        self.leobot_service.installed.return_value = True
        self.leobot_service.stop.side_effect = RuntimeError("systemctl --user stop failed: Failed to connect to bus")
        rc, out = self.call(updater.apply)
        self.assertEqual(rc, 1)
        self.assertIn("could not be stopped", out)
        self.assertIn("Failed to connect to bus", out)
        self.assertEqual(self.pre_backups(), [])
        self.assertEqual(self.head(), self.sha("v0.1.0"))
        self.assertIsNone(updater.settings()["previous_ref"])

    def test_server_down_after_a_failed_service_stop_is_started_again(self):
        self.leobot_service.installed.return_value = True
        self.server.running = False
        with mock.patch.object(leobot, "status_info",
                               side_effect=[{"running": True, "port": 8799}, {"running": False, "port": None}]), \
                mock.patch.object(leobot, "cmd_stop", return_value=1):
            rc, out = self.call(updater.apply)
        self.assertEqual(rc, 1)
        self.assertEqual(self.server.launches, [(8799, "0.1.0")])
        self.assertEqual(self.head(), self.sha("v0.1.0"))

    def test_failed_health_and_unstoppable_server_changes_nothing_more(self):
        self.server.bad_versions = {"0.2.0"}
        calls = []

        def stop():
            calls.append(1)
            if len(calls) > 1:
                raise SystemExit("Could not stop Leo Bot. Nothing was changed.")
            return self.server.stop()
        leobot._stop_if_running.side_effect = stop
        rc, out = self.call(updater.apply)
        self.assertEqual(rc, 1)
        self.assertIn("--rollback", out)
        self.assertIn(str(paths.BACKUPS / self.pre_backups()[0]), out)

    def test_previous_ref_kept_when_stop_fails(self):
        updater.save_settings({"previous_ref": "a" * 40})
        leobot._stop_if_running.side_effect = SystemExit("Could not stop Leo Bot. Nothing was changed.")
        rc, _ = self.call(updater.apply)
        self.assertEqual(rc, 1)
        self.assertEqual(updater.settings()["previous_ref"], "a" * 40)

    def test_previous_ref_kept_when_backup_fails(self):
        updater.save_settings({"previous_ref": "b" * 40})
        with mock.patch.object(leobot, "_tar_data", side_effect=OSError(28, "No space left on device")):
            rc, out = self.call(updater.apply)
        self.assertEqual(rc, 1)
        self.assertIn("Backing up your data failed", out)
        self.assertEqual(updater.settings()["previous_ref"], "b" * 40)
        self.assertTrue(self.server.running)

    def test_previous_ref_kept_when_update_goes_back(self):
        updater.save_settings({"previous_ref": "c" * 40})
        self.server.bad_versions = {"0.2.0"}
        rc, _ = self.call(updater.apply)
        self.assertEqual(rc, 1)
        self.assertEqual(updater.settings()["previous_ref"], "c" * 40)

    def test_failure_names_the_step(self):
        with mock.patch.object(updater, "_prune", side_effect=OSError(5, "I/O error")):
            rc, out = self.call(updater.apply)
        self.assertEqual(rc, 1)
        self.assertIn("Removing old update backups failed", out)
        self.assertNotIn("Backing up your data failed", out)

    def test_recovery_restore_that_cannot_put_data_back_starts_nothing(self):
        self.server.bad_versions = {"0.2.0"}
        self.server.on_launch = lambda v: v == "0.2.0" and migrations.write_marker(paths.DATA, {"schema": 2})
        with mock.patch.object(leobot, "_safe_extract", side_effect=ValueError("bad archive")), no_put_back():
            rc, out = self.call(updater.apply)
        self.assertEqual(rc, 1)
        self.assertFalse(self.server.running)
        self.assertEqual(self.server.launches[-1][1], "0.2.0")  # nothing started after the stop
        self.assertFalse(paths.DATA.exists())
        aside = next(paths.HOME.glob("data.pre-restore-*"))
        put_back = f"&& mv {aside} {paths.DATA}"
        self.assertIn(put_back, out)
        self.assertNotIn("rm -rf", out)
        pre = paths.BACKUPS / self.pre_backups()[0]
        restore = f"leobot backup --restore {pre} --yes"
        self.assertLess(out.index(put_back), out.index(restore))  # restore only after the put-back

    def test_manual_commands_quote_paths(self):
        code = Path("/tmp/a folder/app")
        self.assertEqual(updater._checkout_cmd(code, "abc"),
                         "git -C '/tmp/a folder/app' checkout --force --detach abc")
        self.assertEqual(updater._restore_cmd(Path("/tmp/my backups/pre-x.tar.gz")),
                         "leobot backup --restore '/tmp/my backups/pre-x.tar.gz' --yes")

    def test_lock_holder_names_its_purpose(self):
        from core import updater as u
        with u._update_lock("restore") as mine:
            self.assertTrue(mine)
            self.assertEqual(u.lock_purpose(), "restore")
            self.assertEqual(u.busy_text(), "A data restore is running")
            rc, out = self.call(updater.apply)
            self.assertEqual(rc, 1)
            self.assertIn("A data restore is running, so the update did not start", out)
            self.assertNotIn("already running", out)
            with self.assertRaises(RuntimeError) as cm:
                updater.spawn_detached()
            self.assertIn("A data restore is running", str(cm.exception))
        self.assertIsNone(u.lock_purpose())

    def test_concurrent_update_refused(self):
        paths.RUN.mkdir(parents=True, exist_ok=True)
        fd = os.open(paths.RUN / "update.lock", os.O_RDWR | os.O_CREAT, 0o600)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX)
        self.assertTrue(updater.is_updating())
        rc, out = self.call(updater.apply)
        self.assertEqual(rc, 1)
        self.assertIn("An update is running, so the update did not start", out)
        self.assertEqual(self.head(), self.sha("v0.1.0"))


class RollbackTests(UpdaterBase):
    def test_rollback_to_previous_tag(self):
        self.assertEqual(self.call(updater.apply)[0], 0)
        rc, out = self.call(updater.rollback, True)
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.head(), self.sha("v0.1.0"))
        self.assertEqual(self.server.launches[-1][1], "0.1.0")

    def test_rollback_across_schema_change_restores_after_prompt(self):
        before = data_tree(paths.DATA)
        self.server.on_launch = lambda v: v == "0.2.0" and migrations.write_marker(paths.DATA, {"schema": 2})
        self.assertEqual(self.call(updater.apply)[0], 0)
        self.write_data("projects/demo/later.md", "later")
        with mock.patch("builtins.input", return_value="n"):
            rc, _ = self.call(updater.rollback, False)
        self.assertEqual(rc, 1)
        self.assertEqual(self.head(), self.sha("v0.2.0"))
        rc, out = self.call(updater.rollback, True)
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.head(), self.sha("v0.1.0"))
        self.assertEqual(data_tree(paths.DATA), before)
        snaps = list(paths.BACKUPS.glob("rollback-v0.2.0-*.tar.gz"))
        self.assertEqual(len(snaps), 1)
        with tarfile.open(snaps[0]) as t:
            self.assertIn("projects/demo/later.md", t.getnames())

    def test_rollback_uses_previous_ref_without_older_tag(self):
        prev = self.head()
        git(self.app, "tag", "-d", "v0.1.0")
        git(self.app, "checkout", "-q", "--detach", "v0.2.0")
        updater.save_settings({"previous_ref": prev})
        rc, out = self.call(updater.rollback, True)
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.head(), prev)

    def add_tag_commit(self, ver, base):
        git(self.app, "checkout", "-q", "--detach", base)
        (self.app / "VERSION").write_text(ver + "\n")
        git(self.app, "commit", "-q", "-am", f"version {ver}")
        git(self.app, "tag", f"v{ver}")
        return self.head()

    def test_rollback_prefers_previous_ref_release(self):
        prev = self.sha("v0.1.0")
        self.add_tag_commit("0.1.9", "v0.1.0")
        git(self.app, "checkout", "-q", "--detach", "v0.2.0")
        updater.save_settings({"previous_ref": prev})
        rc, out = self.call(updater.rollback, True)
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.head(), prev)

    def test_rollback_untagged_previous_ref_on_stable_uses_highest_lower_tag(self):
        git(self.app, "checkout", "-q", "--detach", "v0.1.0")
        (self.app / "README.md").write_text("untagged\n")
        git(self.app, "commit", "-q", "-am", "untagged")
        untagged = self.head()
        middle = self.add_tag_commit("0.1.9", "v0.1.0")
        git(self.app, "checkout", "-q", "--detach", "v0.2.0")
        updater.save_settings({"previous_ref": untagged})
        rc, out = self.call(updater.rollback, True)
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.head(), middle)

    def test_rollback_main_channel_uses_previous_ref(self):
        git(self.app, "checkout", "-q", "--detach", "v0.1.0")
        (self.app / "README.md").write_text("untagged\n")
        git(self.app, "commit", "-q", "-am", "untagged")
        untagged = self.head()
        git(self.app, "checkout", "-q", "--detach", "v0.2.0")
        updater.save_settings({"previous_ref": untagged, "channel": "main"})
        rc, out = self.call(updater.rollback, True)
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.head(), untagged)

    def test_rollback_start_failure_without_restore_goes_back(self):
        git(self.app, "checkout", "-q", "--detach", "v0.2.0")
        self.server.bad_versions = {"0.1.0"}  # the older code refuses the data
        rc, out = self.call(updater.rollback, True)
        self.assertEqual(rc, 1)
        self.assertEqual(self.head(), self.sha("v0.2.0"))
        self.assertTrue(self.server.running)
        self.assertEqual(self.server.launches[-1][1], "0.2.0")

    def test_rollback_restore_failure_goes_back_and_restarts(self):
        self.server.on_launch = lambda v: v == "0.2.0" and migrations.write_marker(paths.DATA, {"schema": 2})
        self.assertEqual(self.call(updater.apply)[0], 0)
        with mock.patch.object(leobot, "_safe_extract", side_effect=ValueError("bad archive")):
            rc, out = self.call(updater.rollback, True)
        self.assertEqual(rc, 1)
        self.assertEqual(self.head(), self.sha("v0.2.0"))
        self.assertTrue(self.server.running)
        self.assertEqual(self.server.launches[-1][1], "0.2.0")

    def test_rollback_restore_failure_and_checkout_back_failure_names_command(self):
        self.server.on_launch = lambda v: v == "0.2.0" and migrations.write_marker(paths.DATA, {"schema": 2})
        self.assertEqual(self.call(updater.apply)[0], 0)
        head = self.head()
        real = updater._git

        def fake(code, *args, **kw):
            if args[:2] == ("checkout", "--quiet") and "--force" in args:
                return subprocess.CompletedProcess(args, 1, "", "fatal: checkout broke")
            return real(code, *args, **kw)
        with mock.patch.object(leobot, "_safe_extract", side_effect=ValueError("bad archive")), \
                mock.patch.object(updater, "_git", side_effect=fake):
            rc, out = self.call(updater.rollback, True)
        self.assertEqual(rc, 1)
        self.assertIn(f"checkout --force --detach {head}", out)

    def test_rollback_settings_failure_changes_nothing(self):
        self.assertEqual(self.call(updater.apply)[0], 0)
        stops = self.server.stops
        with mock.patch.object(updater, "save_settings", side_effect=OSError(28, "No space left on device")):
            rc, _ = self.call(updater.rollback, True)
        self.assertEqual(rc, 1)
        self.assertEqual(self.server.stops, stops)
        self.assertEqual(self.head(), self.sha("v0.2.0"))

    def test_double_rollback_on_stable_never_moves_forward(self):
        self.assertEqual(self.call(updater.apply)[0], 0)
        self.assertEqual(self.call(updater.rollback, True)[0], 0)
        self.assertEqual(self.head(), self.sha("v0.1.0"))
        rc, out = self.call(updater.rollback, True)
        self.assertEqual(rc, 1)
        self.assertIn("no earlier version", out)
        self.assertEqual(self.head(), self.sha("v0.1.0"))

    def test_rollback_stop_failure_after_bad_start_starts_nothing(self):
        git(self.app, "checkout", "-q", "--detach", "v0.2.0")
        head = self.head()
        self.server.bad_versions = {"0.1.0"}
        real = updater._stop_server
        n = [0]

        def stop(cli):
            n[0] += 1
            if n[0] == 2:
                raise SystemExit("cannot stop")
            return real(cli)
        with mock.patch.object(updater, "_stop_server", side_effect=stop):
            rc, out = self.call(updater.rollback, True)
        self.assertEqual(rc, 1)
        self.assertEqual(len(self.server.launches), 1)  # only the attempt at 0.1.0
        self.assertEqual(self.head(), self.sha("v0.1.0"))
        self.assertIn(f"checkout --force --detach {head}", out)

    def test_rollback_bad_start_after_restore_brings_current_data_back(self):
        self.server.on_launch = lambda v: v == "0.2.0" and migrations.write_marker(paths.DATA, {"schema": 2})
        self.assertEqual(self.call(updater.apply)[0], 0)
        self.write_data("projects/demo/later.md", "later")
        current_data = data_tree(paths.DATA)
        self.server.bad_versions = {"0.1.0"}
        updater.save_settings({"previous_ref": "d" * 40})
        prev = updater.settings()["previous_ref"]
        rc, out = self.call(updater.rollback, True)
        self.assertEqual(rc, 1)
        self.assertEqual(self.head(), self.sha("v0.2.0"))
        self.assertEqual(data_tree(paths.DATA), current_data)
        self.assertTrue(self.server.running)
        self.assertEqual(self.server.launches[-1][1], "0.2.0")
        snap = next(paths.BACKUPS.glob("rollback-v0.2.0-*.tar.gz"))
        self.assertIn(str(snap), out)
        self.assertEqual(updater.settings()["previous_ref"], prev)

    def test_rollback_restore_that_cannot_put_data_back_starts_nothing(self):
        self.server.on_launch = lambda v: v == "0.2.0" and migrations.write_marker(paths.DATA, {"schema": 2})
        self.assertEqual(self.call(updater.apply)[0], 0)
        launches = len(self.server.launches)
        with mock.patch.object(leobot, "_safe_extract", side_effect=ValueError("bad archive")), no_put_back():
            rc, out = self.call(updater.rollback, True)
        self.assertEqual(rc, 1)
        self.assertEqual(len(self.server.launches), launches)
        self.assertFalse(self.server.running)
        self.assertEqual(self.head(), self.sha("v0.2.0"))
        aside = next(paths.HOME.glob("data.pre-restore-*"))
        self.assertIn(f"&& mv {aside} {paths.DATA}", out)
        self.assertNotIn("rm -rf", out)

    def test_rollback_nothing_to_go_back_to(self):
        rc, out = self.call(updater.rollback, True)
        self.assertEqual(rc, 1)
        self.assertEqual(self.head(), self.sha("v0.1.0"))


class SpawnTests(UpdaterBase):
    def argv_tail(self):
        return [sys.executable, str(paths.CODE / "leobot.py"), "update", "--yes"]

    def test_spawn_detached_linux_uses_systemd_run(self):
        self.service.installed.return_value = True
        self.service.kind.return_value = "systemd"
        with mock.patch.object(updater.subprocess, "run",
                               return_value=subprocess.CompletedProcess([], 0, "", "")) as run:
            updater.spawn_detached()
        argv = run.call_args[0][0]
        self.assertEqual(argv[:2], ["systemd-run", "--user"])
        self.assertIn("leobot-update", argv)
        self.assertIn("--collect", argv)
        self.assertIn(f"--setenv=LEOBOT_HOME={paths.HOME}", argv)
        self.assertEqual(argv[-4:], self.argv_tail())

    def test_spawn_detached_systemd_run_gets_the_user_bus(self):
        self.service.installed.return_value = True
        self.service.kind.return_value = "systemd"
        self.service.user_env.return_value = {"XDG_RUNTIME_DIR": "/run/user/1001"}
        with mock.patch.object(updater.subprocess, "run",
                               return_value=subprocess.CompletedProcess([], 0, "", "")) as run:
            updater.spawn_detached()
        self.assertEqual(run.call_args[1]["env"], {"XDG_RUNTIME_DIR": "/run/user/1001"})

    def test_spawn_detached_macos_uses_launchctl_submit(self):
        self.service.installed.return_value = True
        self.service.kind.return_value = "launchd"
        with mock.patch.object(updater.subprocess, "run",
                               return_value=subprocess.CompletedProcess([], 0, "", "")) as run:
            updater.spawn_detached()
        calls = [c[0][0] for c in run.call_args_list]
        self.assertEqual(calls[0][:3], ["launchctl", "remove", "leobot.update"])
        submit = calls[-1]
        self.assertEqual(submit[:4], ["launchctl", "submit", "-l", "leobot.update"])
        joined = " ".join(submit)
        self.assertIn("update --yes", joined)
        self.assertIn(str(paths.UPDATE_LOG), joined)
        self.assertIn("LEOBOT_HOME", joined)

    def test_spawn_detached_plain_new_session(self):
        with mock.patch.object(updater.subprocess, "Popen") as popen:
            updater.spawn_detached()
        argv = popen.call_args[0][0]
        self.assertEqual(argv, self.argv_tail())
        self.assertTrue(popen.call_args[1]["start_new_session"])
        self.assertEqual(popen.call_args[1]["env"]["LEOBOT_HOME"], str(paths.HOME))

    def test_spawn_refused_while_updating(self):
        fd = os.open(paths.RUN / "update.lock", os.O_RDWR | os.O_CREAT, 0o600)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX)
        with mock.patch.object(updater.subprocess, "Popen") as popen:
            with self.assertRaises(RuntimeError):
                updater.spawn_detached()
        popen.assert_not_called()

    def test_spawn_systemd_failure_raises(self):
        self.service.installed.return_value = True
        self.service.kind.return_value = "systemd"
        with mock.patch.object(updater.subprocess, "run",
                               return_value=subprocess.CompletedProcess([], 1, "", "unit exists")):
            with self.assertRaises(RuntimeError):
                updater.spawn_detached()


class EnsureAppTests(unittest.TestCase):
    def test_runs_script_only_on_mac_with_bundle(self):
        tmp = Path(tempfile.mkdtemp(prefix="leobot-upd-app-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        (tmp / "Applications" / "LeoBot.app").mkdir(parents=True)
        code = tmp / "app"
        (code / "scripts").mkdir(parents=True)
        (code / "scripts" / "ensure-app.sh").write_text("exit 0\n")
        with mock.patch.object(updater.Path, "home", return_value=tmp), \
                mock.patch.object(updater.sys, "platform", "darwin"), \
                mock.patch.object(updater.subprocess, "run",
                                  return_value=subprocess.CompletedProcess([], 0, "", "")) as run, \
                mock.patch.object(updater, "_log"):
            updater._ensure_app(code)
            self.assertEqual(run.call_args[0][0], ["/bin/sh", str(code / "scripts" / "ensure-app.sh")])
            run.reset_mock()
            with mock.patch.object(updater.sys, "platform", "linux"):
                updater._ensure_app(code)
            run.assert_not_called()


class HealthzTests(unittest.TestCase):
    def test_healthz_returns_body_only_with_valid_proof(self):
        import hmac
        tmp = Path(tempfile.mkdtemp(prefix="leobot-upd-hz-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        token = tmp / "auth-token"
        token.write_text("secret")
        seen = {}

        class Resp(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def opener_open(url, timeout):
            nonce = url.split("nonce=")[1]
            seen["proof"] = hmac.new(b"secret", b"leobot-healthz:" + nonce.encode(), "sha256").hexdigest()
            return Resp(json.dumps({"version": "0.2.0", "proof": seen.get("use", seen["proof"])}).encode())

        opener = mock.Mock()
        opener.open.side_effect = opener_open
        with mock.patch.object(paths, "TOKEN_FILE", token), \
                mock.patch.object(leobot.urllib.request, "build_opener", return_value=opener):
            self.assertEqual(leobot._healthz(8765)["version"], "0.2.0")
            self.assertTrue(leobot._proof_ok(8765))
            seen["use"] = "forged"
            self.assertIsNone(leobot._healthz(8765))
            self.assertFalse(leobot._proof_ok(8765))
            seen["use"] = "\u00e9t\u00e9"  # non-ASCII proof: not healthy, no TypeError
            self.assertIsNone(leobot._healthz(8765))


class CliTests(UpdaterBase):
    def run_cli(self, *argv):
        return self.call(leobot.main, ["update", *argv])

    def test_cli_check(self):
        rc, out = self.run_cli("--check")
        self.assertEqual(rc, 0)
        self.assertIn("v0.2.0", out)

    def test_cli_channel_persists(self):
        with mock.patch.object(updater, "apply", return_value=0) as apply:
            rc, _ = self.run_cli("--channel", "main", "--yes")
        self.assertEqual(rc, 0)
        self.assertEqual(updater.settings()["channel"], "main")
        self.assertEqual(apply.call_args[1]["channel"], "main")

    def test_cli_channel_not_saved_when_the_update_is_refused(self):
        with mock.patch.object(paths, "is_installed", return_value=False):
            rc, out = self.run_cli("--channel", "main", "--yes")
        self.assertEqual(rc, 1)
        self.assertIn("development checkout", out)
        self.assertEqual(updater.settings()["channel"], "stable")
        (self.app / "README.md").write_text("local edit\n")
        rc, out = self.run_cli("--channel", "main", "--check")
        self.assertEqual(rc, 1)
        self.assertIn("local changes", out)
        self.assertEqual(updater.settings()["channel"], "stable")

    def test_cli_version_and_rollback_routes(self):
        with mock.patch.object(updater, "apply", return_value=0) as apply:
            self.run_cli("--version", "v0.2.0", "--yes")
        self.assertEqual(apply.call_args[0][0], "v0.2.0")
        self.assertTrue(apply.call_args[1]["yes"])
        with mock.patch.object(updater, "rollback", return_value=0) as rb:
            self.run_cli("--rollback")
        rb.assert_called_once_with(False)

    def test_cli_update_end_to_end(self):
        rc, out = self.run_cli("--yes")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.head(), self.sha("v0.2.0"))


if __name__ == "__main__":
    unittest.main()
