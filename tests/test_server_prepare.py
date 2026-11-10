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
    def run_main(self, events, prepare):
        def lock(port):
            events.append("lock")

        def run(cmd, **k):
            events.append("index")
            return mock.Mock(returncode=0, stdout="", stderr="")
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
        with self.assertRaises(SystemExit):
            self.run_main(events, refuse)
        self.assertEqual(events, ["lock", "prepare", "release"])

    def test_any_failure_releases_lock(self):
        events = []

        def boom():
            raise RuntimeError("disk")
        with self.assertRaises(RuntimeError):
            self.run_main(events, boom)
        self.assertEqual(events, ["lock", "release"])


if __name__ == "__main__":
    unittest.main()
