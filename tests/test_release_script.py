"""scripts/release.sh: syntax, shellcheck, and the version rule (sourced, so nothing runs)."""
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RELEASE = ROOT / "scripts" / "release.sh"


def action(cur, ver):
    r = subprocess.run(["bash", "-c", 'source "$1"; version_action "$2" "$3"', "x", str(RELEASE), cur, ver],
                       capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        raise AssertionError(r.stderr)
    return r.stdout.strip()


class ReleaseScriptTest(unittest.TestCase):
    def test_bash_syntax(self):
        r = subprocess.run(["bash", "-n", str(RELEASE)], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)

    @unittest.skipUnless(shutil.which("shellcheck"), "shellcheck not installed")
    def test_shellcheck_clean(self):
        r = subprocess.run(["shellcheck", str(RELEASE)], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_same_version_tags_head(self):
        self.assertEqual(action("0.1.0", "0.1.0"), "tag")

    def test_higher_version_bumps(self):
        self.assertEqual(action("0.1.0", "0.2.0"), "bump")
        self.assertEqual(action("0.9.0", "0.10.0"), "bump")
        self.assertEqual(action("1.2.3", "2.0.0"), "bump")

    def test_lower_version_refused(self):
        self.assertEqual(action("0.2.0", "0.1.0"), "refuse")
        self.assertEqual(action("0.10.0", "0.9.0"), "refuse")

    def test_existing_tag_still_refused(self):
        text = RELEASE.read_text()
        self.assertIn('git rev-parse -q --verify "refs/tags/v$ver"', text)
        self.assertIn('git ls-remote --tags origin "refs/tags/v$ver"', text)
        # the tag checks come before anything is written, committed or tagged
        self.assertLess(text.index("refs/tags/v$ver\" | grep -q ."), text.index('git tag "v$ver"'))

    def test_sourcing_runs_nothing(self):
        r = subprocess.run(["bash", "-c", 'source "$1"; echo sourced', "x", str(RELEASE)],
                           capture_output=True, text=True, timeout=30, cwd="/")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "sourced")



class ReleaseDryRunTest(unittest.TestCase):
    """release.sh --dry-run in a temp repo on main (origin a local bare repo), with stub gh and
    render-installer.sh. Dry runs only: nothing is written, committed, tagged or pushed."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="leobot-release-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        env = {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1", "GIT_AUTHOR_NAME": "Test",
               "GIT_AUTHOR_EMAIL": "test@example.com", "GIT_COMMITTER_NAME": "Test",
               "GIT_COMMITTER_EMAIL": "test@example.com"}
        self.env = {**os.environ, **env}
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        (bin_dir / "gh").write_text("#!/bin/sh\necho success\n")
        (bin_dir / "gh").chmod(0o755)
        self.env["PATH"] = f"{bin_dir}:{os.environ['PATH']}"
        self.repo = self.tmp / "repo"
        (self.repo / "scripts").mkdir(parents=True)
        shutil.copy2(RELEASE, self.repo / "scripts" / "release.sh")
        stub = self.repo / "scripts" / "render-installer.sh"
        stub.write_text('#!/bin/sh\necho \'REPO_URL_DEFAULT="https://github.com/acme/demo.git"\' > "$1"\n')
        stub.chmod(0o755)
        (self.repo / "VERSION").write_text("0.1.0\n")
        self.git("init", "-q", "-b", "main")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "first")
        self.git("init", "-q", "--bare", str(self.tmp / "origin.git"), cwd=self.tmp)
        self.git("remote", "add", "origin", (self.tmp / "origin.git").as_uri())
        self.git("push", "-q", "origin", "main")
        self.head = self.git("rev-parse", "HEAD")

    def git(self, *args, cwd=None):
        r = subprocess.run(["git", *args], cwd=cwd or self.repo, env=self.env, capture_output=True,
                           text=True, check=True)
        return r.stdout.strip()

    def release(self, ver):
        r = subprocess.run(["bash", str(self.repo / "scripts" / "release.sh"), ver, "--dry-run"],
                           cwd=self.repo, env=self.env, capture_output=True, text=True, timeout=60)
        self.assertEqual(self.git("rev-parse", "HEAD"), self.head)  # nothing committed
        self.assertEqual(self.git("tag", "--list"), "")
        self.assertEqual(self.git("status", "--porcelain"), "")
        return r

    def test_current_version_is_tagged_without_a_bump(self):
        r = self.release("0.1.0")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("tag v0.1.0 at HEAD", r.stdout)
        self.assertNotIn("write VERSION", r.stdout)
        self.assertIn("[dry-run] push tag", r.stdout)
        self.assertIn("[dry-run] gh release create v0.1.0", r.stdout)

    def test_higher_version_opens_a_pull_request(self):
        # main is protected (pull requests only): the bump goes through a release branch and a
        # pull request, and nothing is tagged or published until a second run after the merge.
        r = self.release("0.2.0")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("[dry-run] create branch release-v0.2.0", r.stdout)
        self.assertIn("[dry-run] write VERSION 0.2.0", r.stdout)
        self.assertIn("open a pull request", r.stdout)
        self.assertIn("run scripts/release.sh 0.2.0 again", r.stdout)
        self.assertNotIn("tag v0.2.0", r.stdout)
        self.assertNotIn("gh release create", r.stdout)

    def test_bump_never_pushes_to_main(self):
        text = RELEASE.read_text()
        self.assertNotIn("git push --atomic origin main", text)
        self.assertNotIn("git push origin main", text)

    def test_existing_release_branch_refused(self):
        self.git("branch", "release-v0.2.0")
        r = self.release("0.2.0")
        self.assertEqual(r.returncode, 1)
        self.assertIn("branch release-v0.2.0 exists already", r.stderr)

    def test_lower_version_refused(self):
        r = self.release("0.0.9")
        self.assertEqual(r.returncode, 1)
        self.assertIn("lower than the current version 0.1.0", r.stderr)

    def test_existing_tag_refused(self):
        self.git("push", "-q", "origin", "HEAD:refs/tags/v0.1.0")  # on origin only
        r = self.release("0.1.0")
        self.assertEqual(r.returncode, 1)
        self.assertIn("tag v0.1.0 exists on origin", r.stderr)


if __name__ == "__main__":
    unittest.main()
