"""Pinned browser engine: where the binary lives, its status, and the relay's tool name sets."""
import hashlib
import io
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from core import browser_engine as be
from core import paths


def _exe(path, version=be.VERSION):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!/bin/sh\necho 'agent-browser {version}'\n")
    path.chmod(0o755)
    return path


class Resolve(unittest.TestCase):
    def test_resolve_prefers_env_then_data_then_path(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            env_bin = _exe(d / "env" / "ab")
            path_bin = _exe(d / "bin" / "agent-browser")
            data_bin = _exe(d / "data" / "tools" / "agent-browser" / be.VERSION
                            / "node_modules" / ".bin" / "agent-browser")
            with mock.patch.object(paths, "DATA", d / "data"), \
                    mock.patch("shutil.which", return_value=str(path_bin)):
                self.assertEqual(be.resolve({"LEOBOT_AGENT_BROWSER": str(env_bin)}), env_bin)
                self.assertEqual(be.resolve({}), data_bin)
                data_bin.unlink()
                self.assertEqual(be.resolve({}), path_bin)
            with mock.patch.object(paths, "DATA", d / "data"), mock.patch("shutil.which", return_value=None):
                self.assertIsNone(be.resolve({}))
                self.assertIsNone(be.resolve({"LEOBOT_AGENT_BROWSER": str(d / "nope")}))

    def test_path_binary_of_another_version_is_not_used(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            other = _exe(d / "bin" / "agent-browser", version="0.40.2")
            with mock.patch.object(paths, "DATA", d / "data"), \
                    mock.patch("shutil.which", return_value=str(other)), mock.patch.dict("os.environ", clear=True):
                self.assertIsNone(be.resolve({}))
                st = be.status()
            self.assertEqual(st["state"], "missing")
            self.assertIn("0.40.2", st["detail"])
            self.assertIn(be.VERSION, st["detail"])

    def test_version_is_asked_again_for_a_new_file(self):
        with tempfile.TemporaryDirectory() as d:
            p = _exe(Path(d) / "agent-browser", version="0.40.2")
            self.assertEqual(be.version_of(p), "0.40.2")
            _exe(p, version=be.VERSION + "  ")  # another size: a new binary at the same path
            self.assertEqual(be.version_of(p), be.VERSION)
            p.write_text("#!/bin/sh\nexit 3\n")
            self.assertIsNone(be.version_of(p))

    def test_status_missing_says_how_to_install(self):
        with mock.patch.object(be, "resolve", return_value=None):
            st = be.status()
        self.assertEqual(st["state"], "missing")
        self.assertIn("install", st["detail"].lower())

    def test_status_ready_names_binary(self):
        with mock.patch.object(be, "resolve", return_value=Path("/x/agent-browser")):
            st = be.status()
        self.assertEqual(st["state"], "ready")
        self.assertIn("/x/agent-browser", st["detail"])


class Argv(unittest.TestCase):
    def test_mcp_argv_core_tools_only(self):
        argv = be.mcp_argv(Path("/x/agent-browser"))
        self.assertEqual(argv, ["/x/agent-browser", "mcp", "--tools", "core"])
        for bad in ("all", "debug", "state", "network"):
            self.assertNotIn(bad, " ".join(argv))


class ToolSets(unittest.TestCase):
    SETS = ("READ_TOOLS", "TYPE_TOOLS", "COMMIT_TOOLS", "NAV_TOOLS")

    def test_tool_sets_disjoint(self):
        seen = {}
        for name in self.SETS:
            s = getattr(be, name)
            self.assertIsInstance(s, frozenset)
            for t in s:
                self.assertNotIn(t, seen, f"{t} in {name} and {seen.get(t)}")
                seen[t] = name

    def test_names_are_engine_tools(self):
        for name in self.SETS:
            for t in getattr(be, name):
                self.assertTrue(t.startswith("agent_browser_"), t)

    def test_dangerous_tools_in_no_set(self):
        every = set().union(*(getattr(be, n) for n in self.SETS))
        for t in ("agent_browser_eval", "agent_browser_close", "agent_browser_tools_profiles",
                  "agent_browser_upload", "agent_browser_download", "agent_browser_pdf"):
            self.assertNotIn(t, every)

    def test_commit_tools_cover_clicks_and_keys(self):
        for t in ("click", "press", "select", "check", "uncheck"):
            self.assertIn("agent_browser_" + t, be.COMMIT_TOOLS)
        for t in ("fill", "type"):
            self.assertIn("agent_browser_" + t, be.TYPE_TOOLS)


class Fetch:
    """A fake urlopen: serves `body` and records the URL."""
    def __init__(self, body=b"", error=None):
        self.body, self.error, self.urls = body, error, []

    def __call__(self, url, timeout=None):
        self.urls.append(url)
        if self.error:
            raise self.error
        return io.BytesIO(self.body)


def _engine_script(version=be.VERSION):
    return f"#!/bin/sh\necho 'agent-browser {version}'\n".encode()


class Platform(unittest.TestCase):
    def test_asset_for_each_supported_computer(self):
        cases = {
            ("Darwin", "arm64", False): "agent-browser-darwin-arm64",
            ("Darwin", "x86_64", False): "agent-browser-darwin-x64",
            ("Linux", "x86_64", False): "agent-browser-linux-x64",
            ("Linux", "aarch64", False): "agent-browser-linux-arm64",
            ("Linux", "x86_64", True): "agent-browser-linux-musl-x64",
            ("Linux", "aarch64", True): "agent-browser-linux-musl-arm64",
            ("Windows", "AMD64", False): "agent-browser-win32-x64.exe",
            ("Windows", "ARM64", False): "agent-browser-win32-x64.exe",
        }
        for (system, machine, musl), want in cases.items():
            self.assertEqual(be.asset_name(system, machine, musl), want, (system, machine, musl))
            self.assertIn(want, be.SHA256)

    def test_unsupported_computer_has_no_asset(self):
        self.assertIsNone(be.asset_name("FreeBSD", "x86_64", False))
        self.assertIsNone(be.asset_name("Linux", "riscv64", False))

    def test_pinned_hashes_are_sha256(self):
        for asset, h in be.SHA256.items():
            self.assertRegex(h, r"^[0-9a-f]{64}$", asset)


class Install(unittest.TestCase):
    ASSET = "agent-browser-darwin-arm64"

    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.data = Path(d.name) / "data"
        self.binary = self.data / "tools" / "agent-browser" / be.VERSION / "agent-browser"
        for p in (mock.patch.object(paths, "DATA", self.data), mock.patch("shutil.which", return_value=None),
                  mock.patch.object(be, "asset_name", return_value=self.ASSET), mock.patch("os.name", "posix")):
            p.start()
            self.addCleanup(p.stop)

    def _install(self, body, run=None, error=None):
        fetch = Fetch(body, error)
        pinned = {self.ASSET: hashlib.sha256(_engine_script()).hexdigest()}
        calls = []
        real_run = subprocess.run

        def fake_run(argv, **kw):  # --version really runs the fake engine; `install` is recorded
            if argv[1:] == ["--version"]:
                return real_run(argv, **kw)
            calls.append(argv)
            if isinstance(run, BaseException):
                raise run
            return run or mock.Mock(returncode=0, stderr="", stdout="")
        with mock.patch.dict(be.SHA256, pinned), mock.patch("subprocess.run", fake_run):
            return be.install(opener=fetch), fetch, calls

    def test_downloads_the_pinned_release_then_installs_chrome(self):
        st, fetch, calls = self._install(_engine_script())
        self.assertEqual(st["state"], "ready", st)
        self.assertEqual(fetch.urls, [be.RELEASE_URL.format(v=be.VERSION, asset=self.ASSET)])
        self.assertTrue(fetch.urls[0].startswith("https://github.com/vercel-labs/agent-browser/releases/"))
        self.assertTrue(os.access(self.binary, os.X_OK))
        self.assertEqual(calls, [[str(self.binary), "install"]])

    def test_checksum_mismatch_keeps_nothing(self):
        st, _, calls = self._install(b"not the engine")
        self.assertEqual(st["state"], "missing")
        self.assertIn("checksum", st["detail"])
        self.assertFalse(self.binary.exists())
        self.assertEqual(list(self.binary.parent.iterdir()), [])  # no temp file left
        self.assertEqual(calls, [])

    def test_oversized_download_is_refused(self):
        with mock.patch.object(be, "MAX_DOWNLOAD", 10):
            st, _, _ = self._install(_engine_script())
        self.assertEqual(st["state"], "missing")
        self.assertIn("larger", st["detail"])
        self.assertFalse(self.binary.exists())

    def test_network_failure(self):
        st, _, calls = self._install(b"", error=OSError("no route to host"))
        self.assertEqual(st["state"], "missing")
        self.assertIn("no route to host", st["detail"])
        self.assertEqual(calls, [])

    def test_unsupported_computer(self):
        with mock.patch.object(be, "asset_name", return_value=None):
            st = be.install(opener=Fetch(error=AssertionError("no download")))
        self.assertEqual(st["state"], "missing")
        self.assertIn("no build for this computer", st["detail"])

    def test_chrome_install_failure(self):
        st, _, _ = self._install(_engine_script(), run=mock.Mock(returncode=1, stderr="boom\n", stdout=""))
        self.assertEqual(st["state"], "missing")
        self.assertIn("boom", st["detail"])

    def test_chrome_install_timeout(self):
        st, _, _ = self._install(_engine_script(), run=subprocess.TimeoutExpired("x", 1))
        self.assertIn("timed out", st["detail"])

    def test_present_pinned_binary_is_not_downloaded_again(self):
        _exe(self.binary)
        st, fetch, calls = self._install(b"", error=AssertionError("downloaded again"))
        self.assertEqual(st["state"], "ready", st)
        self.assertEqual(fetch.urls, [])
        self.assertEqual(calls, [[str(self.binary), "install"]])

    def test_pinned_binary_of_another_version_is_replaced(self):
        _exe(self.binary, version="0.40.2")
        st, fetch, _ = self._install(_engine_script())
        self.assertEqual(st["state"], "ready", st)
        self.assertEqual(len(fetch.urls), 1)
        self.assertEqual(be.version_of(self.binary), be.VERSION)

    def test_downloaded_binary_wins_over_an_older_npm_install(self):
        npm = _exe(self.data / "tools" / "agent-browser" / be.VERSION / "node_modules" / ".bin" / "agent-browser")
        self.assertEqual(be.resolve({}), npm)  # an older Leo Bot's npm install keeps working
        _exe(self.binary)
        self.assertEqual(be.resolve({}), self.binary)


if __name__ == "__main__":
    unittest.main()
