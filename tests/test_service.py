import os
import plistlib
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from core import paths, service


def done(rc=0, out="", err=""):
    return mock.Mock(returncode=rc, stdout=out, stderr=err)


class PureTests(unittest.TestCase):
    def test_plist_contents(self):
        data = service.launchd_plist("/usr/bin/python3", Path("/opt/Leo Bot/app"), 8765, Path("/h/My Home"))
        p = plistlib.loads(data.encode() if isinstance(data, str) else data)
        self.assertEqual(p["Label"], "leobot.server")
        self.assertEqual(p["KeepAlive"], {"SuccessfulExit": False})  # a refused start exits 0
        self.assertEqual(p["EnvironmentVariables"]["LEOBOT_SERVICE"], "launchd")
        self.assertIs(p["RunAtLoad"], True)
        self.assertIs(p["AbandonProcessGroup"], True)
        self.assertEqual(p["EnvironmentVariables"]["LEOBOT_HOME"], "/h/My Home")
        self.assertIn("/opt/homebrew/bin", p["EnvironmentVariables"]["PATH"])
        self.assertTrue(p["ProgramArguments"][1].endswith("dashboard/server.py"))
        self.assertEqual(p["ProgramArguments"][2:], ["--port", "8765"])
        self.assertEqual(p["StandardOutPath"], str(paths.SERVER_LOG))

    def test_unit_contents(self):
        u = service.systemd_unit("/usr/bin/python3", Path("/a/app"), 8765, Path("/h"), False)
        for want in ("Restart=on-failure", "RestartSec=3", "RestartPreventExitStatus=78",
                     "Environment=LEOBOT_SERVICE=systemd", "NoNewPrivileges=true", "PrivateTmp=true",
                     "Environment=LEOBOT_HOME=", "WantedBy=default.target"):
            self.assertIn(want, u)
        self.assertIn("ProtectSystem=full", u)
        self.assertIn("PrivateTmp=true", u)
        self.assertNotIn("LEOBOT_DEPLOYMENT=server", u)
        soft = service.systemd_unit("/p", Path("/a"), 1, Path("/h"), False, hardened=False)
        self.assertNotIn("ProtectSystem", soft)
        self.assertNotIn("PrivateTmp", soft)
        self.assertIn("NoNewPrivileges=true", soft)
        s = service.systemd_unit("/usr/bin/python3", Path("/a/app"), 8765, Path("/h"), True)
        self.assertIn("LEOBOT_DEPLOYMENT=server", s)

    def test_unit_quotes_spaces(self):
        u = service.systemd_unit("/usr/bin/python3", Path("/tmp/Ana María/app"), 8765, Path("/h/a b"), False)
        self.assertIn('"/tmp/Ana María/app/dashboard/server.py"', u)
        self.assertIn('Environment="LEOBOT_HOME=/h/a b"', u)

    def test_unit_escapes_specials(self):
        u = service.systemd_unit('/py/a"b', Path("/x/100%/app"), 1, Path("/h"), False)
        exec_line = [l for l in u.splitlines() if l.startswith("ExecStart=")][0]
        self.assertIn('"/py/a\\"b"', exec_line)
        self.assertIn("100%%", exec_line)


class FakeHome(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="leobot-svc-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        for p in (mock.patch.object(Path, "home", return_value=self.tmp / "u"),
                  mock.patch.object(paths, "ensure_dirs")):
            p.start()
            self.addCleanup(p.stop)
        def default(argv, **k):
            if argv[:3] == ["systemctl", "--user", "is-active"]:
                return done(0, "active\n")
            return done()
        r = mock.patch("core.service.subprocess.run", side_effect=default)
        self.run_ = r.start()
        self.addCleanup(r.stop)

    def argvs(self):
        return [c.args[0] for c in self.run_.call_args_list]

    def system(self, name):
        p = mock.patch("core.service.platform.system", return_value=name)
        p.start()
        self.addCleanup(p.stop)


class MacTests(FakeHome):
    def setUp(self):
        super().setUp()
        self.system("Darwin")

    def test_install_calls_launchctl(self):
        msg = service.install(port=9000)
        self.assertIn("bootstrap", [a for argv in self.argvs() for a in argv])
        self.assertTrue(any(argv[0] == "launchctl" and "bootstrap" in argv for argv in self.argvs()))
        f = self.tmp / "u" / "Library" / "LaunchAgents" / "leobot.server.plist"
        self.assertTrue(f.exists())
        self.assertIn("9000", plistlib.loads(f.read_bytes())["ProgramArguments"])
        self.assertTrue(msg)

    def test_install_passes_the_home_folder(self):
        service.install()
        f = self.tmp / "u" / "Library" / "LaunchAgents" / "leobot.server.plist"
        self.assertEqual(plistlib.loads(f.read_bytes())["EnvironmentVariables"]["LEOBOT_HOME"], str(paths.HOME))
        self.assertTrue(paths.HOME.is_absolute())

    def test_install_boots_out_before_bootstrap(self):
        service.install()
        verbs = [argv[1] for argv in self.argvs() if argv[0] == "launchctl"]
        self.assertLess(verbs.index("bootout"), verbs.index("bootstrap"))

    def test_bootstrap_failure_raises(self):
        self.run_.side_effect = lambda argv, **k: done(5, "", "boom") if argv[1] == "bootstrap" else done()
        with self.assertRaises(RuntimeError):
            service.install()

    def test_port_from_plist(self):
        service.install(port=9124)
        self.assertEqual(service.port(), 9124)

    def test_stop_boots_out_and_start_bootstraps(self):
        service.install()
        self.run_.reset_mock()
        service.stop()
        self.assertEqual(self.argvs()[0][:2], ["launchctl", "bootout"])
        self.assertTrue(self.argvs()[0][2].endswith("/leobot.server"))
        self.run_.reset_mock()
        service.start()
        self.assertTrue(any("bootstrap" in a for a in self.argvs()))

    def test_uninstall_removes_file(self):
        service.install()
        self.assertTrue(service.installed())
        service.uninstall()
        self.assertFalse(service.installed())
        self.assertEqual(service.status(), {"installed": False, "running": False, "kind": None})

    def test_status_running(self):
        service.install()
        self.run_.side_effect = None
        self.run_.return_value = done(0, "state = running\n")
        self.assertEqual(service.status(), {"installed": True, "running": True, "kind": "launchd"})
        self.run_.return_value = done(113, "", "not found")
        self.assertFalse(service.status()["running"])

    def test_restart_kickstarts_or_starts(self):
        service.install()
        self.run_.reset_mock()
        service.restart()
        self.assertIn("kickstart", self.argvs()[0])


class LinuxTests(FakeHome):
    def setUp(self):
        super().setUp()
        self.system("Linux")

    def test_install_calls_systemctl_user(self):
        service.install(server=True)
        self.assertTrue(any(argv[:2] == ["systemctl", "--user"] and "enable" in argv and "--now" in argv
                            for argv in self.argvs()))
        self.assertTrue(any("daemon-reload" in argv for argv in self.argvs()))
        f = self.tmp / "u" / ".config" / "systemd" / "user" / "leobot.service"
        self.assertIn("LEOBOT_DEPLOYMENT=server", f.read_text())

    def _fake(self, active, status, recovers=False):
        state = {"active": active}

        def run(argv, **k):
            if "restart" in argv and recovers:
                state["active"] = True
            if argv[:3] == ["systemctl", "--user", "is-active"]:
                on = state["active"]
                return done(0 if on else 3, "active\n" if on else "failed\n")
            if "show" in argv:
                return done(0, f"ExecMainStatus={status}\nResult=exit-code\n")
            return done()
        self.run_.side_effect = run

    def test_sandbox_failure_falls_back_to_reduced_hardening(self):
        self._fake(False, 226, recovers=True)
        with mock.patch("core.service.time.sleep"), \
                mock.patch("core.service.time.monotonic", side_effect=[0, 1, 5, 11, 12, 13]):
            msg = service.install()
        f = self.tmp / "u" / ".config" / "systemd" / "user" / "leobot.service"
        self.assertNotIn("ProtectSystem", f.read_text())
        self.assertIn("Hardening was reduced", msg)
        self.assertTrue(any(a[:3] == ["systemctl", "--user", "restart"] for a in self.argvs()))

    def test_still_failing_after_reduced_hardening_raises(self):
        self._fake(False, 226)
        with mock.patch("core.service.time.sleep"), \
                mock.patch("core.service.time.monotonic", side_effect=[0, 11, 12, 20, 30]):
            with self.assertRaises(RuntimeError):
                service.install()

    def test_other_failure_keeps_hardening(self):
        self._fake(False, 1)
        with mock.patch("core.service.time.sleep"), \
                mock.patch("core.service.time.monotonic", side_effect=[0, 11, 12]):
            msg = service.install()
        f = self.tmp / "u" / ".config" / "systemd" / "user" / "leobot.service"
        self.assertIn("ProtectSystem=full", f.read_text())
        self.assertNotIn("Hardening", msg)

    def test_unit_passes_the_home_folder(self):
        service.install()
        f = self.tmp / "u" / ".config" / "systemd" / "user" / "leobot.service"
        self.assertIn(service._sd_env("LEOBOT_HOME", str(paths.HOME)), f.read_text().splitlines())

    def test_port_from_installed_file(self):
        service.install(port=9123)
        self.assertEqual(service.port(), 9123)

    def test_linger_hint(self):
        self.run_.side_effect = lambda argv, **k: (
            done(0, "Linger=no\n") if argv[0] == "loginctl"
            else done(0, "active\n") if "is-active" in argv else done())
        self.assertIn("enable-linger", service.install())
        self.run_.side_effect = lambda argv, **k: (
            done(0, "Linger=yes\n") if argv[0] == "loginctl"
            else done(0, "active\n") if "is-active" in argv else done())
        self.assertNotIn("enable-linger", service.install())

    def test_stop_start_restart_status_uninstall(self):
        service.install()
        for fn, verb in ((service.stop, "stop"), (service.start, "start"), (service.restart, "restart")):
            self.run_.reset_mock()
            fn()
            self.assertEqual(self.argvs()[0], ["systemctl", "--user", verb, "leobot"])
        self.assertEqual(service.status(), {"installed": True, "running": True, "kind": "systemd"})
        service.uninstall()
        self.assertFalse(service.installed())
        self.assertTrue(any("disable" in a for a in self.argvs()))


class UserEnvTests(unittest.TestCase):
    """`sudo -u leobot leobot update` has no XDG_RUNTIME_DIR: systemctl --user must still find
    the user manager."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="leobot-xdg-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        p = mock.patch.object(service, "_RUNTIME_ROOT", str(self.tmp))
        p.start()
        self.addCleanup(p.stop)

    def test_defaults_runtime_dir_and_bus_when_the_folder_exists(self):
        (self.tmp / "1001").mkdir()
        env = service.user_env({"PATH": "/bin"}, uid=1001)
        self.assertEqual(env["XDG_RUNTIME_DIR"], str(self.tmp / "1001"))
        self.assertEqual(env["DBUS_SESSION_BUS_ADDRESS"], f"unix:path={self.tmp / '1001'}/bus")
        self.assertEqual(env["PATH"], "/bin")

    def test_keeps_what_is_set(self):
        (self.tmp / "1001").mkdir()
        env = service.user_env({"XDG_RUNTIME_DIR": str(self.tmp), "DBUS_SESSION_BUS_ADDRESS": "x"}, uid=1001)
        self.assertEqual(env["XDG_RUNTIME_DIR"], str(self.tmp))
        self.assertEqual(env["DBUS_SESSION_BUS_ADDRESS"], "x")

    def test_no_folder_no_default(self):
        env = service.user_env({}, uid=1001)
        self.assertNotIn("XDG_RUNTIME_DIR", env)
        self.assertNotIn("DBUS_SESSION_BUS_ADDRESS", env)

    def test_every_service_command_gets_it(self):
        (self.tmp / str(os.getuid())).mkdir()
        with mock.patch.dict(os.environ, {}, clear=False), \
                mock.patch("core.service.subprocess.run", return_value=done()) as run:
            os.environ.pop("XDG_RUNTIME_DIR", None)
            os.environ.pop("DBUS_SESSION_BUS_ADDRESS", None)
            service._run(["systemctl", "--user", "stop", "leobot"])
        self.assertEqual(run.call_args[1]["env"]["XDG_RUNTIME_DIR"], str(self.tmp / str(os.getuid())))


class StopFailureTests(FakeHome):
    def test_systemd_stop_failure_raises(self):
        self.system("Linux")
        self.run_.side_effect = lambda argv, **k: (done(1, "", "Failed to connect to bus") if "stop" in argv
                                                   else done())
        with self.assertRaises(RuntimeError) as cm:
            service.stop()
        self.assertIn("Failed to connect to bus", str(cm.exception))

    def test_launchd_bootout_failure_raises_only_while_still_loaded(self):
        self.system("Darwin")
        loaded = {"yes": True}
        self.run_.side_effect = lambda argv, **k: (
            done(5, "", "Boot-out failed") if "bootout" in argv
            else done(0 if loaded["yes"] else 113) if "print" in argv else done())
        with self.assertRaises(RuntimeError):
            service.stop()
        loaded["yes"] = False  # not loaded: already stopped, which is fine
        service.stop()


class UnsupportedTests(FakeHome):
    def test_other_platform(self):
        self.system("Windows")
        with self.assertRaises(RuntimeError):
            service.install()
        self.assertFalse(service.installed())


if __name__ == "__main__":
    unittest.main()
