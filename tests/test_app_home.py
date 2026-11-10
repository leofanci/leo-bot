"""LeoBot.app finds code and token in the home folder (static checks on the app sources), and
ensure-app.sh rebuilds only on a source change (run against a temp copy with stub tools)."""
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RES = ROOT / "LeoBot.app" / "Contents" / "Resources"


def read(p):
    return Path(p).read_text(encoding="utf-8")


class WebviewSwift(unittest.TestCase):
    def setUp(self):
        self.src = read(RES / "webview.swift")

    def test_home_layout(self):
        self.assertIn('appendingPathComponent(".leobot")', self.src)
        self.assertIn('appendingPathComponent("app")', self.src)
        self.assertIn("run/auth-token", self.src)
        self.assertIn("LEOBOT_HOME", self.src)
        self.assertNotIn("dashboard/.auth-token", self.src)
        self.assertNotIn("dashboard/server.log", self.src)

    def test_dev_clone_check(self):
        self.assertIn("dashboard/server.py", self.src)

    def test_failure_alert_names_log(self):
        self.assertIn("logs/server.log", self.src)

    def test_retry_constants(self):
        self.assertRegex(self.src, r"let retryInterval: TimeInterval = 2\.0")
        self.assertRegex(self.src, r"let startGrace: TimeInterval = 30\.0")
        self.assertRegex(self.src, r"let totalWait: TimeInterval = 90\.0")
        self.assertIn("elapsed >= totalWait", self.src)
        self.assertIn("Thread.sleep(forTimeInterval: retryInterval)", self.src)

    def test_wait_clock_starts_again_while_updating(self):
        wait = self.src[self.src.index("private func waitUp"):]
        wait = wait[:wait.index("private func resolvePort")]
        self.assertIn("var begin = Date()", wait)
        reset = wait.index("if isUpdating() {")
        self.assertLess(reset, wait.index("elapsed >= totalWait"))
        self.assertIn("begin = Date()", wait[reset:wait.index("continue", reset)])

    def test_port_comes_from_the_running_server_then_the_service(self):
        self.assertNotIn("let port = 8765", self.src)
        self.assertIn("let defaultPort = 8765", self.src)
        self.assertIn('"run/server.pid"', self.src)
        self.assertIn("Library/LaunchAgents/leobot.server.plist", self.src)
        self.assertIn('"ProgramArguments"', self.src)
        resolve = self.src[self.src.index("private func resolvePort"):]
        resolve = resolve[:resolve.index("\n    }\n")]
        self.assertLess(resolve.index("pidFilePort()"), resolve.index("servicePort()"))
        self.assertLess(resolve.index("servicePort()"), resolve.index("defaultPort"))
        ping = self.src[self.src.index("private func ping"):]
        self.assertIn("port = resolvePort()", ping[:200])
        pid = self.src[self.src.index("private func pidFilePort"):]
        self.assertIn("LOCK_SH | LOCK_NB", pid[:600])  # a file nobody holds is a leftover

    def test_home_must_be_absolute(self):
        home = self.src[self.src.index("private func homePath"):]
        home = home[:home.index("private func repoPath")]
        self.assertEqual(home.count('hasPrefix("/")'), 2)

    def test_start_gated_by_grace_and_update_lock(self):
        self.assertIn("!started && elapsed >= startAfter && !isUpdating()", self.src)
        self.assertIn("run/update.lock", self.src)
        self.assertIn("LOCK_SH | LOCK_NB", self.src)
        # watch() must not start the server directly
        watch = self.src[self.src.index("private func watch"):]
        watch = watch[:watch.index("private func readToken") if "private func readToken" in watch else len(watch)]
        self.assertNotIn("runStartScript(", watch.split("waitUp(")[0])

    def test_server_pid_reset_and_checked(self):
        run = self.src[self.src.index("private func runStartScript"):]
        self.assertIn("serverPID = 0", run[:120])
        self.assertIn("isLeoBotServer(serverPID)", self.src)
        self.assertIn('"dashboard/server.py"', self.src.split("func isLeoBotServer")[1])

    def test_home_file_fallback(self):
        self.assertIn('"leobot-home"', self.src)

    def test_token_file_checks(self):
        self.assertIn("lstat(file", self.src)
        self.assertIn("st.st_uid != getuid()", self.src)
        self.assertIn("st.st_mode & 0o077", self.src)

    def test_passes_home_to_script(self):
        self.assertIn("environment", self.src)


class StartServer(unittest.TestCase):
    def setUp(self):
        self.src = read(RES / "start-server.sh")

    def test_paths(self):
        self.assertIn("run/auth-token", self.src)
        self.assertIn("logs/server.log", self.src)
        self.assertIn("LEOBOT_HOME", self.src)
        self.assertNotIn("dashboard/.auth-token", self.src)
        self.assertNotIn("dashboard/server.log", self.src)

    def test_log_appended_not_truncated(self):
        self.assertNotIn(': > "$LOGFILE"', self.src)
        self.assertIn('>> "$LOGFILE"', self.src)

    def test_stale_match_kept_and_narrowed(self):
        self.assertIn("dashboard/server.py", self.src)
        self.assertIn("server.pid", self.src)

    def test_python_version_checked_per_candidate(self):
        self.assertIn("sys.version_info < (3, 10)", self.src)
        loop = self.src[self.src.index("PYTHON3=\"\"\nfor _p in"):]
        loop = loop[:loop.index("done")]
        self.assertIn('py_ok "$_p" && PYTHON3="$_p"', loop)
        self.assertIn("command -v python3", loop)  # the PATH one is checked too
        self.assertNotIn('[ -z "$PYTHON3" ] && PYTHON3="$(command -v python3', self.src)

    def test_python_check_rejects_old_python(self):
        line = next(ln for ln in self.src.splitlines() if ln.startswith("py_ok()"))
        stub = Path(tempfile.mkdtemp(prefix="leobot-py-"))
        self.addCleanup(shutil.rmtree, stub, ignore_errors=True)
        old = stub / "old-python"
        old.write_text("#!/bin/sh\nexit 1\n")  # what `python3 -c '...< (3, 10)'` returns on 3.9
        old.chmod(0o755)
        script = f'{line}\npy_ok "$1" && echo yes || echo no\n'
        out = [subprocess.run(["bash", "-c", script, "x", p], capture_output=True, text=True).stdout.strip()
               for p in (str(old), shutil.which("python3"))]
        self.assertEqual(out, ["no", "yes"])


class Scripts(unittest.TestCase):
    def test_build_accepts_out(self):
        self.assertIn("--out", read(ROOT / "scripts" / "build-app.sh"))

    def test_build_records_custom_home(self):
        b = read(ROOT / "scripts" / "build-app.sh")
        self.assertIn("leobot-home", b)
        self.assertIn("LEOBOT_HOME", b)

    def test_install_exports_home_and_builds(self):
        i = read(ROOT / "install.sh")
        # the build runs in a function called after main() has exported the home
        self.assertIn('export LEOBOT_HOME="$HOME_DIR"', i)
        self.assertIn("/scripts/build-app.sh\" --out", i)

    def test_ensure_rebuilds_installed_app(self):
        s = read(ROOT / "scripts" / "ensure-app.sh")
        self.assertIn("Applications/LeoBot.app", s)
        self.assertIn("--out", s)
        self.assertIn("LEOBOT_HOME", s)

    def test_ensure_compares_source_hash_not_times(self):
        s = read(ROOT / "scripts" / "ensure-app.sh")
        self.assertIn("build-app.sh --hash", s)
        self.assertIn("source-hash", s)
        self.assertNotIn("-nt", s)
        b = read(ROOT / "scripts" / "build-app.sh")
        self.assertIn('source_hash > "$APP/Contents/Resources/source-hash"', b)
        self.assertLess(b.index("source_hash >"), b.index("codesign"))  # the signature covers it
        self.assertIn("LeoBot.app/Contents/Resources/source-hash", read(ROOT / ".gitignore"))

    def test_build_records_absolute_home(self):
        b = read(ROOT / "scripts" / "build-app.sh")
        self.assertIn('HOME_REC="$(absolute "$LEOBOT_HOME")"', b)
        self.assertIn('START_DIR="$(pwd)"', b)
        self.assertLess(b.index('START_DIR="$(pwd)"'), b.index('cd "$(dirname "$0")/.."'))


@unittest.skipUnless(shutil.which("shasum"), "shasum not installed")
class EnsureAppRun(unittest.TestCase):
    """ensure-app.sh in a temp copy of the app sources, with stub swiftc and codesign that only
    record a build: it rebuilds when the binary is missing or a source changes, never when only
    file times change."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="leobot-ensure-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.repo = self.tmp / "repo"
        (self.repo / "scripts").mkdir(parents=True)
        for f in ("build-app.sh", "ensure-app.sh"):
            shutil.copy2(ROOT / "scripts" / f, self.repo / "scripts" / f)
        shutil.copytree(ROOT / "LeoBot.app", self.repo / "LeoBot.app",
                        ignore=shutil.ignore_patterns("MacOS", "_CodeSignature", "source-hash"))
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        self.builds = self.tmp / "builds.log"
        (self.bin / "swiftc").write_text(
            '#!/bin/sh\nwhile [ $# -gt 0 ]; do [ "$1" = -o ] && out="$2"; shift; done\n'
            f'printf "#!/bin/sh\\n" > "$out"; chmod 755 "$out"; echo build >> "{self.builds}"\n')
        (self.bin / "codesign").write_text("#!/bin/sh\nexit 0\n")
        for f in self.bin.iterdir():
            f.chmod(0o755)
        self.home = self.tmp / "home"
        self.home.mkdir()

    def ensure(self):
        env = {k: v for k, v in os.environ.items() if not k.startswith("LEOBOT_")}
        env.update(HOME=str(self.home), PATH=f"{self.bin}:{os.environ['PATH']}")
        r = subprocess.run(["sh", str(self.repo / "scripts" / "ensure-app.sh")], env=env,
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        return self.builds.read_text().count("build") if self.builds.exists() else 0

    def test_rebuilds_only_on_a_source_change(self):
        self.assertEqual(self.ensure(), 1)  # no binary yet
        self.assertEqual(self.ensure(), 1)  # nothing changed
        swift = self.repo / "LeoBot.app" / "Contents" / "Resources" / "webview.swift"
        os.utime(swift, (2_000_000_000, 2_000_000_000))  # newer file time, same text
        self.assertEqual(self.ensure(), 1)
        swift.write_text(swift.read_text() + "\n// changed\n")
        self.assertEqual(self.ensure(), 2)
        self.assertEqual(self.ensure(), 2)


if __name__ == "__main__":
    unittest.main()
