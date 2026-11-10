"""install.sh: syntax, shellcheck, dry runs in a temp HOME, and the release render step.

Every run uses a temp HOME (with a space in it) and a local bare repo, never the real home,
and only ever --dry-run, so nothing is installed on the machine running the tests.
"""
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INSTALL = ROOT / "install.sh"
RENDER = ROOT / "scripts" / "render-installer.sh"
PLACEHOLDER = "__LEOBOT_" + "REPO_URL__"
MARKER = "# leobot wrapper (installed by install.sh)"
# macOS ships bash 3.2 as /bin/bash; CI and Homebrew have bash 5. Run under each one found.
BASHES = sorted({b for b in ("/bin/bash", shutil.which("bash")) if b and os.path.exists(b)})


def _git(cwd, *args):
    subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=test@example.com",
                    "-c", "init.defaultBranch=main", "-c", "commit.gpgsign=false",
                    "-c", "tag.gpgsign=false", *args],
                   cwd=cwd, check=True, capture_output=True, text=True)


def make_bare_repo(base: Path, tags=("v0.1.0",)) -> str:
    """A bare repo with one commit on main and the given tags. Returns its file:// URL."""
    work = base / "work"
    work.mkdir()
    _git(work, "init", "-q")
    (work / "VERSION").write_text("0.1.0\n")
    _git(work, "add", "VERSION")
    _git(work, "commit", "-q", "-m", "first")
    for t in tags:
        _git(work, "tag", t)
    bare = base / "repo.git"
    _git(base, "clone", "-q", "--bare", str(work), str(bare))
    return "file://" + str(bare)


class InstallScriptTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="leobot-install-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.home = self.tmp / "home dir"
        self.home.mkdir()

    def env(self, **extra):
        env = {k: v for k, v in os.environ.items()
               if not k.startswith("LEOBOT_") and k not in ("DISPLAY", "WAYLAND_DISPLAY")}
        env.update(HOME=str(self.home), SHELL="/bin/zsh")
        env.update(extra)
        return env

    def run_install(self, *args, _bash="bash", **env):
        return subprocess.run([_bash, str(INSTALL), *args], env=self.env(**env),
                              stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=120)

    @staticmethod
    def wrapper_body(out: str) -> str:
        """The wrapper text a dry run prints, indented four spaces under `would: write`."""
        lines = out.splitlines()
        start = next(i for i, ln in enumerate(lines) if ln.startswith("would: write ")) + 1
        body = []
        for ln in lines[start:]:
            if not ln.startswith("    "):
                break
            body.append(ln[4:])
        return "\n".join(body) + "\n"

    def fake_bin(self, name: str, script: str) -> Path:
        d = self.tmp / "fakebin"
        d.mkdir(exist_ok=True)
        f = d / name
        f.write_text("#!/bin/sh\n" + script)
        f.chmod(0o755)
        return d

    def assertNothingInstalled(self):
        self.assertFalse((self.home / ".leobot").exists(), "dry run created the home folder")
        self.assertFalse((self.home / ".local").exists(), "dry run created ~/.local")
        self.assertFalse((self.home / ".zshrc").exists(), "dry run edited the rc file")

    def test_bash_syntax(self):
        r = subprocess.run(["bash", "-n", str(INSTALL)], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)

    @unittest.skipUnless(shutil.which("shellcheck"), "shellcheck not installed")
    def test_shellcheck_clean(self):
        r = subprocess.run(["shellcheck", str(INSTALL)], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_body_wrapped_in_main(self):
        lines = [ln for ln in INSTALL.read_text().splitlines() if ln.strip()]
        self.assertEqual(lines[-1], 'main "$@"')

    def test_setup_home_only_makes_folders_and_marker(self):
        text = INSTALL.read_text()
        body = text[text.index("setup_home() {"):]
        body = body[:body.index("\n}\n")]
        self.assertIn("datadir.init_home()", body)
        self.assertNotIn("prepare()", body)
        self.assertNotIn("move_legacy", body)

    def test_placeholder_line_present_for_render(self):
        self.assertIn(f'\nREPO_URL_DEFAULT="{PLACEHOLDER}"\n', INSTALL.read_text())

    def test_placeholder_refuses(self):
        r = self.run_install("--dry-run", "--yes")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("latest release", r.stdout + r.stderr)
        self.assertNothingInstalled()

    def test_dry_run_plans_clone_and_wrapper(self):
        url = make_bare_repo(self.tmp)
        r = self.run_install("--dry-run", "--yes", LEOBOT_REPO_URL=url)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        out = r.stdout
        self.assertIn("would: git clone", out)
        self.assertIn("v0.1.0", out)
        self.assertIn(".leobot/app", out)
        self.assertIn(".local/bin/leobot", out)
        self.assertIn("Run: ", out)
        self.assertNothingInstalled()

    def test_dry_run_picks_highest_semver_tag(self):
        url = make_bare_repo(self.tmp, tags=("v0.1.0", "v0.10.0", "v0.9.9", "v1.0.0-rc1", "latest"))
        r = self.run_install("--dry-run", "--yes", LEOBOT_REPO_URL=url)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("refs/tags/v0.10.0", r.stdout)
        self.assertNotIn("rc1", r.stdout)

    def test_version_pin_must_exist(self):
        url = make_bare_repo(self.tmp)
        r = self.run_install("--dry-run", "--yes", "--version", "v9.9.9", LEOBOT_REPO_URL=url)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("v9.9.9", r.stderr)

    def test_no_tags_suggests_main_channel(self):
        url = make_bare_repo(self.tmp, tags=())
        r = self.run_install("--dry-run", "--yes", LEOBOT_REPO_URL=url)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("--channel main", r.stderr)

    def test_main_channel_uses_main(self):
        url = make_bare_repo(self.tmp)
        r = self.run_install("--channel", "main", "--dry-run", "--yes", LEOBOT_REPO_URL=url)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("main", r.stdout)
        self.assertNotIn("v0.1.0", r.stdout)
        self.assertNotIn("checkout", r.stdout)
        self.assertNothingInstalled()

    def test_existing_install_hands_to_update(self):
        url = make_bare_repo(self.tmp)
        (self.home / ".leobot" / "app" / ".git").mkdir(parents=True)
        r = self.run_install("--dry-run", "--yes", "--channel", "main", LEOBOT_REPO_URL=url)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertRegex(r.stdout, r"would: .*leobot\.py'? update --yes --channel main")
        self.assertNotIn("would: git clone", r.stdout)

    def test_non_https_repo_refused(self):
        r = self.run_install("--dry-run", "--yes", LEOBOT_REPO_URL="git" + "://example.com/x.git")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("https://", r.stderr)

    def test_env_equivalents(self):
        url = make_bare_repo(self.tmp)
        r = self.run_install(LEOBOT_REPO_URL=url, LEOBOT_DRY_RUN="1", LEOBOT_YES="1",
                             LEOBOT_CHANNEL="main", LEOBOT_SERVICE="0", LEOBOT_NO_APP="1")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotRegex(r.stdout, r"would: .*service install")
        self.assertNotIn("build-app", r.stdout)
        self.assertNotIn("v0.1.0", r.stdout)
        self.assertNothingInstalled()

    def test_service_flag_plans_service(self):
        url = make_bare_repo(self.tmp)
        r = self.run_install("--dry-run", "--yes", "--service", LEOBOT_REPO_URL=url,
                             LEOBOT_DEPLOYMENT="server")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertRegex(r.stdout, r"would: .*leobot\.py'? service install.* --server")

    def test_custom_home_is_exported_by_wrapper(self):
        url = make_bare_repo(self.tmp)
        custom = self.home / "elsewhere"
        r = self.run_install("--dry-run", "--yes", LEOBOT_REPO_URL=url, LEOBOT_HOME=str(custom))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("export LEOBOT_HOME=", r.stdout)
        self.assertIn("elsewhere/app", r.stdout)
        self.assertFalse(custom.exists())

    def test_quotes_in_home_survive_every_bash(self):
        url = make_bare_repo(self.tmp)
        custom = self.home / "it's $HOME `x` dir"
        fake = self.fake_bin("python3", 'printf "%s\\n" "$LEOBOT_HOME" "$@"\n')
        for bash in BASHES:
            with self.subTest(bash=bash):
                r = self.run_install("--dry-run", "--yes", _bash=bash, LEOBOT_REPO_URL=url,
                                     LEOBOT_HOME=str(custom))
                self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
                body = self.wrapper_body(r.stdout)
                wrapper = self.tmp / "wrapper"
                wrapper.write_text(body)
                chk = subprocess.run(["sh", "-n", str(wrapper)], capture_output=True, text=True)
                self.assertEqual(chk.returncode, 0, body + chk.stderr)
                ran = subprocess.run(["sh", str(wrapper), "status", "a b"], capture_output=True, text=True,
                                     env={**os.environ, "PATH": f"{fake}:{os.environ['PATH']}"})
                self.assertEqual(ran.stdout.splitlines(),
                                 [str(custom), str(custom / "app" / "leobot.py"), "status", "a b"],
                                 body + ran.stderr)
                self.assertIn(MARKER, body.splitlines())
                line = next(ln for ln in r.stdout.splitlines() if ln.startswith("would: git -C "))
                words = subprocess.run(["sh", "-c", "printf '%s\\n' " + line[len("would: "):]],
                                       capture_output=True, text=True).stdout.splitlines()
                self.assertEqual(words[:3], ["git", "-C", str(custom / "app")], line)

    def test_foreign_leobot_command_left_alone(self):
        url = make_bare_repo(self.tmp)
        bin_dir = self.home / ".local" / "bin"
        bin_dir.mkdir(parents=True)
        (bin_dir / "leobot").write_text("#!/bin/sh\nexec python3 /opt/other/leobot.py \"$@\"\n")
        r = self.run_install("--dry-run", "--yes", LEOBOT_REPO_URL=url)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn("would: write", r.stdout)
        self.assertIn("left alone", r.stderr)

    def test_own_wrapper_is_replaced(self):
        url = make_bare_repo(self.tmp)
        bin_dir = self.home / ".local" / "bin"
        bin_dir.mkdir(parents=True)
        (bin_dir / "leobot").write_text(f"#!/bin/sh\n{MARKER}\nexec python3 '/old/app/leobot.py' \"$@\"\n")
        r = self.run_install("--dry-run", "--yes", LEOBOT_REPO_URL=url)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("would: write", r.stdout)

    def linux_run(self, **env):
        url = make_bare_repo(self.tmp)
        fake = self.fake_bin("uname", 'if [ "$1" = -s ]; then echo Linux; else exec /usr/bin/uname "$@"; fi\n')
        return self.run_install("--dry-run", "--yes", LEOBOT_REPO_URL=url,
                                PATH=f"{fake}:{os.environ['PATH']}", **env)

    def test_linux_desktop_defaults_to_no_service(self):
        r = self.linux_run(DISPLAY=":0")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotRegex(r.stdout, r"would: .*service install")

    def test_linux_headless_defaults_to_server_service(self):
        r = self.linux_run()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertRegex(r.stdout, r"would: .*leobot\.py'? service install --server")

    def test_render_fills_placeholder(self):
        clone = self.tmp / "clone"
        (clone / "scripts").mkdir(parents=True)
        shutil.copy2(INSTALL, clone / "install.sh")
        shutil.copy2(RENDER, clone / "scripts" / "render-installer.sh")
        _git(clone, "init", "-q")
        origin = "git" + "@" + "github.com:acme/leo-bot.git"  # split so it is not read as an email
        _git(clone, "remote", "add", "origin", origin)
        out = self.tmp / "rendered.sh"
        r = subprocess.run(["bash", str(clone / "scripts" / "render-installer.sh"), str(out)],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        text = out.read_text()
        self.assertIn("https://github.com/acme/leo-bot.git", text)
        self.assertNotIn(PLACEHOLDER, text)
        self.assertIn('REPO_URL_DEFAULT="https://github.com/acme/leo-bot.git"', text.splitlines())


if __name__ == "__main__":
    unittest.main()
