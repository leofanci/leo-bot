"""server.main() runs datadir.prepare() after the pid lock and before the re-index."""
import io
import sys
import unittest
from unittest import mock

from dashboard import server


class Tty(io.StringIO):
    def isatty(self):
        return True


class Stop(Exception):
    pass


class MainWiringCase(unittest.TestCase):
    def run_main(self, events, prepare, index_rc=0):
        def lock(port):
            events.append("lock")

        def run(cmd, **k):
            events.append("index")
            return mock.Mock(returncode=index_rc, stdout="", stderr="bad file" if index_rc else "")
        patches = [
            mock.patch.object(sys, "stdout", Tty()), mock.patch.object(sys, "stderr", Tty()),
            mock.patch.object(sys, "argv", ["server.py", "--port", "0"]),
            mock.patch.object(server, "restrict_new_files"),
            mock.patch.object(server, "acquire_pid_lock", side_effect=lock),
            mock.patch.object(server.datadir, "prepare", side_effect=prepare),
            mock.patch.object(server.subprocess, "run", side_effect=run),
            mock.patch.object(server.signal, "signal"),
            mock.patch.object(server, "ThreadingHTTPServer", side_effect=Stop),
            mock.patch.object(server, "release_pid_lock", side_effect=lambda: events.append("release")),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        server.main()

    def test_prepare_after_lock_before_index(self):
        events = []
        with self.assertRaises(Stop):
            self.run_main(events, lambda: events.append("prepare"))
        self.assertEqual(events[:3], ["lock", "prepare", "index"])

    def test_refusal_releases_lock_and_skips_index(self):
        events = []

        def refuse():
            events.append("prepare")
            sys.exit("refused")
        with mock.patch.dict(server.os.environ, {"LEOBOT_SERVICE": ""}), \
                self.assertRaises(SystemExit) as cm:
            self.run_main(events, refuse)
        self.assertEqual(events, ["lock", "prepare", "release"])
        self.assertEqual(cm.exception.code, server.REFUSED_EXIT)  # systemd: never respawned

    def test_refusal_under_launchd_exits_zero(self):
        # launchd cannot skip one status: its job restarts only after a failure
        def refuse():
            sys.exit("refused")
        with mock.patch.dict(server.os.environ, {"LEOBOT_SERVICE": "launchd"}), \
                self.assertRaises(SystemExit) as cm:
            self.run_main([], refuse)
        self.assertEqual(cm.exception.code, 0)

    def test_failed_reindex_without_index_is_a_refusal(self):
        events = []
        with mock.patch.object(server.db, "db_exists", return_value=False), \
                mock.patch.dict(server.os.environ, {"LEOBOT_SERVICE": "systemd"}), \
                self.assertRaises(SystemExit) as cm:
            self.run_main(events, lambda: None, index_rc=1)
        self.assertEqual(cm.exception.code, server.REFUSED_EXIT)
        self.assertIn("release", events)

    def test_any_failure_releases_lock(self):
        events = []

        def boom():
            raise RuntimeError("disk")
        with self.assertRaises(RuntimeError):
            self.run_main(events, boom)
        self.assertEqual(events, ["lock", "release"])


if __name__ == "__main__":
    unittest.main()
