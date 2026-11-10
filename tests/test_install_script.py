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

    def run_install(self, *args, **env):
        return subprocess.run(["bash", str(INSTALL), *args], env=self.env(**env),
                              stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=120)

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
