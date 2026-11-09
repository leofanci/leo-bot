"""Public-repository guards for local usage data and common secret files."""

import re
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROJECTS = ROOT / "projects"

# Fixture terms intentionally used throughout tests and documentation.
ALLOWED = {
    "demo", "acme", "acme-app", "demo-project", "demo-channel",
    "profile-a", "profile-b", "acme-tiktok",
    "instagram", "tiktok", "youtube", "linkedin", "threads", "facebook",
    "pinterest", "reddit", "bluesky",
}

FORBIDDEN_BRANDING_PATTERNS = (
    re.compile(rb"gtm(?:(?:&nbsp;)|[\s_-])*os", re.IGNORECASE),
)

RETIRED_PRODUCT_NAME_PATTERNS = (
    re.compile(rb"local(?:(?:&nbsp;)|[\s_-])*workspace", re.IGNORECASE),
)

SECRET_PATTERNS = (
    re.compile(rb"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(rb"(?:AKIA|ASIA|A3T[A-Z0-9]|AGPA|AIDA|AROA|ANPA|ANVA|ASCA)[0-9A-Z]{16}"),
    re.compile(rb"AIza[0-9A-Za-z_-]{30,}"),
    re.compile(rb"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(rb"gh[pousr]_[A-Za-z0-9_]{20,}"),
    re.compile(rb"glpat-[A-Za-z0-9_-]{20,}"),
    re.compile(rb"sk-(?:proj-|ant-(?:api[0-9]+-)?)?[A-Za-z0-9_-]{20,}"),
    re.compile(rb"(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{16,}"),
    re.compile(rb"gsk_[A-Za-z0-9_-]{20,}"),
    re.compile(rb"xox[baprs]-[A-Za-z0-9-]{10,}"),
    re.compile(rb"SG\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{20,}"),
    re.compile(rb"npm_[A-Za-z0-9]{20,}"),
    re.compile(rb"pypi-[A-Za-z0-9_-]{20,}"),
    re.compile(rb"eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
    re.compile(rb"https?://[^/\s:@]+:[^/\s@]+@", re.IGNORECASE),
)

FORBIDDEN_SUFFIXES = {
    ".pem", ".key", ".p12", ".pfx", ".crt", ".cer", ".der",
    ".db", ".sqlite", ".sqlite3", ".log", ".bak", ".backup",
    ".zip", ".tar", ".tgz", ".gz", ".kdbx", ".ovpn",
    ".mobileprovision", ".tfstate",
}

FORBIDDEN_BASENAMES = {
    ".envrc", ".netrc", ".npmrc", ".pypirc",
    "auth.json", "credentials.json", "secrets.json",
    "id_rsa", "id_ed25519", "settings.local.json",
}

FORBIDDEN_DIRECTORIES = {
    ".aws", ".direnv", ".netlify", ".ssh", ".terraform", ".vercel", ".wrangler",
}

# Everything Leo Bot authors for the user is private, regardless of its contents.
# These path rules make `git add -f` insufficient to bypass the privacy gate.
PRIVATE_DATA_PREFIXES = (
    ("projects",),
    ("portfolio",),
    ("private",),
    ("database", "data"),
    (".remember",),
    (".superpowers",),
    (".tokensave",),
)
PRIVATE_DATA_EXCEPTIONS = {("projects", ".gitkeep")}

EMAIL_PATTERN = re.compile(
    rb"(?i)(?<![A-Z0-9._%+-])([A-Z0-9._%+-]+)@([A-Z0-9.-]+\.[A-Z]{2,})",
)
GENERIC_EMAIL_DOMAINS = {b"example.com", b"example.net", b"example.org", b"example.invalid"}

HOME_PATH_PATTERNS = (
    re.compile(rb"/Users/([A-Za-z0-9._-]+)"),
    re.compile(rb"/home/([A-Za-z0-9._-]+)"),
    re.compile(rb"(?i)[A-Z]:\\\\Users\\\\([A-Za-z0-9._-]+)"),
)
GENERIC_HOME_NAMES = {b"you", b"user", b"username", b"example", b"<user>"}

# Specs and plans describe the design, not the conversation that produced it:
# no quoted owner feedback, no "decided/confirmed with the user" provenance.
# Checked on the current tree only (older history predates the rule).
DESIGN_DOC_PREFIXES = ("docs/",)
PERSONAL_REASONING_PATTERNS = (
    re.compile(rb"(?i)\b(?:decided|confirmed|agreed|chosen|picked)\s+(?:by|with)\s+(?:the\s+)?user\b"),
    re.compile(rb"(?i)\buser\s+(?:feedback|decisions?|may override)\b"),
    re.compile(rb"(?i)\bthe user(?:'s)?\s*(?::|found|flagged|said|asked|wants|prefers|liked|hated|go-ahead)"),
    re.compile(rb"(?i)\bclaude'?s calls\b"),
    re.compile(rb"(?i)\bduring brainstorming\b"),
)

NETWORK_HOST_PATTERN = re.compile(rb"(?i)(?:https?|wss?)://([A-Z0-9.-]+)")
PUBLIC_NETWORK_HOSTS = {
    b"127.0.0.1", b"localhost", b"attacker.example",
    b"bellard.org", b"cdn.jsdelivr.net", b"github.com",
    b"www.apache.org", b"www.apple.com", b"www.w3.org",
    # Public hosts quoted verbatim in LICENSE, dashboard/vendor/LICENSE-xterm and
    # dashboard/vendor/fonts/OFL.txt.
    b"polyformproject.org", b"example.com", b"www.sourcelair.com", b"scripts.sil.org",
    # Composio REST API (connected apps, core/composio.py): a public vendor host.
    b"backend.composio.dev", b"composio.dev",  # + sign-up link in the Integrations panel
    b"docs.composio.dev",  # "Where to find it" link for the project API key (Integrations panel)
    # API engine provider presets (dashboard/engines/config.py).
    b"api.anthropic.com", b"api.openai.com", b"openrouter.ai",
    # Server install (scripts/server-setup.sh, docs/server.md).
    b"claude.ai",
}


def _frontmatter_values(path, keys=frozenset({"name", "handle"})):
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return set()
    if not text.startswith("---"):
        return set()
    block = text.split("---", 2)[1]
    values = set()
    for line in block.splitlines():
        key, sep, value = line.partition(":")
        if sep and key.strip() in keys:
            value = value.strip().strip("'\"").lstrip("@").strip()
            if len(value) >= 4:
                values.add(value)
    return values


def project_dirs():
    """This checkout's projects/ plus the main checkout's: a linked worktree has
    none of its own, and the private-term check must not skip silently there."""
    common = subprocess.run(
        ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"], cwd=ROOT,
        capture_output=True, text=True, check=False,
    ).stdout.strip()
    dirs = [PROJECTS]
    if common and (Path(common).parent / "projects").resolve() != PROJECTS.resolve():
        dirs.append(Path(common).parent / "projects")
    return [d for d in dirs if d.is_dir()]


def private_terms():
    terms = set()
    for projects in project_dirs():
        for project in projects.iterdir():
            if not project.is_dir() or project.name.startswith("."):
                continue
            terms.add(project.name)
            terms.update(_frontmatter_values(project / "project.md"))
            for collection, metadata in (("profiles", "profile.md"), ("products", "product.md")):
                parent = project / collection
                if not parent.is_dir():
                    continue
                for entity in parent.iterdir():
                    if not entity.is_dir():
                        continue
                    terms.add(entity.name)
                    terms.update(_frontmatter_values(entity / metadata))
                    if collection == "profiles":
                        channels = entity / "channels"
                        if channels.is_dir():
                            for channel in channels.iterdir():
                                if channel.is_dir():
                                    terms.add(channel.name)
                                    terms.update(_frontmatter_values(
                                        channel / "channel.md", frozenset({"handle"}),
                                    ))

    # Local Git identity and hosting account stay outside tracked files. Derive
    # them at test time so the guard catches names/handles without publishing them.
    for key in ("user.name", "user.email"):
        value = subprocess.run(
            ["git", "config", "--get", key], cwd=ROOT,
            capture_output=True, text=True, check=False,
        ).stdout.strip()
        if value:
            terms.add(value)
            terms.update(part for part in re.split(r"[^A-Za-z0-9._-]+", value) if len(part) >= 4)
            if "@" in value:
                terms.add(value.split("@", 1)[0])
    remotes = subprocess.run(
        ["git", "remote"], cwd=ROOT, capture_output=True, text=True, check=True,
    ).stdout.splitlines()
    for remote in remotes:
        urls = subprocess.run(
            ["git", "remote", "get-url", "--all", remote], cwd=ROOT,
            capture_output=True, text=True, check=False,
        ).stdout.splitlines()
        for url in urls:
            match = re.search(r"github\.com[:/]([^/]+)/", url, re.IGNORECASE)
            if match:
                terms.add(match.group(1))
    return {
        term for term in terms
        if term.lower() not in ALLOWED and len(term.strip()) >= 4
    }


def tracked_paths():
    output = subprocess.run(
        ["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True,
    ).stdout
    return [path for path in output.splitlines() if path]


def staged_entries():
    """Return (path, blob oid) pairs from the exact stage-0 Git index."""
    output = subprocess.run(
        ["git", "ls-files", "--stage", "-z"], cwd=ROOT,
        capture_output=True, check=True,
    ).stdout
    entries = []
    for record in output.split(b"\0"):
        if not record:
            continue
        metadata, path = record.split(b"\t", 1)
        _mode, oid, stage = metadata.split(b" ")
        if stage == b"0":
            entries.append((path.decode("utf-8", "surrogateescape"), oid.decode("ascii")))
    return entries


def _read_blobs(entries):
    """Read many Git blobs in one process and yield (label, bytes)."""
    if not entries:
        return
    request = "".join(f"{oid}\n" for _label, oid in entries).encode("ascii")
    output = subprocess.run(
        ["git", "cat-file", "--batch"], cwd=ROOT, input=request,
        capture_output=True, check=True,
    ).stdout
    cursor = 0
    for label, expected_oid in entries:
        header_end = output.index(b"\n", cursor)
        header = output[cursor:header_end].decode("ascii")
        oid, kind, size_text = header.split(" ")
        if oid != expected_oid or kind != "blob":
            raise RuntimeError(f"unexpected git object for {label}: {header}")
        size = int(size_text)
        start = header_end + 1
        end = start + size
        yield label, output[start:end]
        cursor = end + 1


def staged_records():
    entries = [(f"staged:{path}", oid) for path, oid in staged_entries()]
    yield from _read_blobs(entries)


def working_tree_records():
    for rel in tracked_paths():
        path = ROOT / rel
        if path.is_file():
            yield f"working-tree:{rel}", path.read_bytes()


def reachable_blobs():
    """Yield each unique blob reachable from refs, including binary history."""
    output = subprocess.run(
        ["git", "rev-list", "--objects", "--all"], cwd=ROOT,
        capture_output=True, text=True, check=True,
    ).stdout
    seen = set()
    objects = []
    for line in output.splitlines():
        oid, _, path = line.partition(" ")
        if not path or oid in seen:
            continue
        seen.add(oid)
        objects.append((path, oid))

    request = "".join(f"{oid}\n" for _path, oid in objects)
    kinds = subprocess.run(
        ["git", "cat-file", "--batch-check=%(objectname) %(objecttype)"], cwd=ROOT,
        input=request, capture_output=True, text=True, check=True,
    ).stdout.splitlines()
    entries = []
    for (path, expected_oid), row in zip(objects, kinds, strict=True):
        oid, kind = row.split(" ")
        if oid != expected_oid:
            raise RuntimeError(f"unexpected git object while reading {path}")
        if kind == "blob":
            entries.append((f"{oid[:12]}:{path}", oid))
    yield from _read_blobs(entries)


def reachable_paths():
    """Return every file path present in any commit reachable from a ref."""
    commits = subprocess.run(
        ["git", "rev-list", "--all"], cwd=ROOT,
        capture_output=True, text=True, check=True,
    ).stdout.splitlines()
    paths = set()
    for commit in commits:
        output = subprocess.run(
            ["git", "ls-tree", "-r", "--name-only", commit], cwd=ROOT,
            capture_output=True, text=True, check=True,
        ).stdout
        paths.update(path for path in output.splitlines() if path)
    return sorted(paths)


def reachable_metadata():
    """Commit messages and ref names visible in published history.

    Author identity is checked separately so the repository owner's explicitly
    approved GitHub identity can appear in commit metadata without also being
    allowed in source files or documentation.
    """
    log = subprocess.run(
        ["git", "log", "--all", "--format=%H%n%P%n%B"], cwd=ROOT,
        capture_output=True, check=True,
    ).stdout
    refs = subprocess.run(
        ["git", "for-each-ref", "--format=%(refname)"], cwd=ROOT,
        capture_output=True, check=True,
    ).stdout
    return log + refs


def disclosure_records(include_metadata=True):
    yield from working_tree_records()
    yield from staged_records()
    yield from reachable_blobs()
    if include_metadata:
        yield "git-metadata", reachable_metadata()
        yield "git-paths", "\n".join(reachable_paths()).encode()


def sensitive_path(rel):
    path = Path(rel)
    name = path.name.lower()
    parts = tuple(part.lower() for part in path.parts)
    private_data = (
        parts not in PRIVATE_DATA_EXCEPTIONS
        and any(parts[:len(prefix)] == prefix for prefix in PRIVATE_DATA_PREFIXES)
    )
    return (
        private_data
        or name == ".env"
        or name.startswith(".env.")
        or name.endswith(".env")
        or ".env." in name
        or name in FORBIDDEN_BASENAMES
        or name.startswith("credentials.")
        or name.startswith("secrets.")
        or name.startswith("service-account") and name.endswith(".json")
        or path.suffix.lower() in FORBIDDEN_SUFFIXES
        or any(part in FORBIDDEN_DIRECTORIES for part in parts)
    )


class TestNoUsageData(unittest.TestCase):
    def test_leobot_authored_data_paths_are_always_private(self):
        private_paths = (
            "projects/example/project.md",
            "projects/example/profiles/profile/content/plan.json",
            "portfolio/activities.md",
            "private/notes.md",
            "database/data/os.db",
            ".remember/session.log",
            ".superpowers/state.json",
            ".tokensave/tokensave.db",
        )
        self.assertTrue(all(sensitive_path(path) for path in private_paths))
        self.assertFalse(sensitive_path("projects/.gitkeep"))

    def test_linked_worktree_still_guards_main_checkout_terms(self):
        # A worktree has no projects/ of its own: without the main checkout's,
        # the private-term check would skip silently there.
        main = subprocess.run(
            ["git", "worktree", "list", "--porcelain"], cwd=ROOT,
            capture_output=True, text=True, check=True,
        ).stdout.splitlines()[0].split(" ", 1)[1]
        self.assertIn((Path(main) / "projects").resolve(),
                      [p.resolve() for p in project_dirs()])

    def test_staged_snapshot_contains_no_private_data_paths(self):
        hits = [path for path, _oid in staged_entries() if sensitive_path(path)]
        self.assertEqual(hits, [], "Private app data is staged for commit:\n" + "\n".join(hits))

    def test_current_and_reachable_history_contain_no_private_terms(self):
        needles = private_terms()
        if not needles:
            self.skipTest("no local project metadata to guard against")
        encoded = [(term, term.casefold().encode("utf-8")) for term in sorted(needles)]
        hits = []
        for label, data in disclosure_records():
            folded = data.lower()
            for term, needle in encoded:
                if needle in folded:
                    hits.append(f"{label}: private local term ({len(term)} chars)")
        self.assertEqual(hits, [], "Private usage data is reachable from Git:\n" + "\n".join(hits))

    def test_old_branding_is_absent_from_current_tree_and_history(self):
        hits = []
        for label, data in disclosure_records():
            if any(pattern.search(data) for pattern in FORBIDDEN_BRANDING_PATTERNS):
                hits.append(label)
        self.assertEqual(hits, [], "Old project branding is reachable from Git:\n" + "\n".join(hits))

    def test_current_tree_uses_leobot_product_name(self):
        hits = []
        for rel in tracked_paths():
            data = (ROOT / rel).read_bytes()
            if any(pattern.search(data) for pattern in RETIRED_PRODUCT_NAME_PATTERNS):
                hits.append(rel)
        self.assertEqual(hits, [], "Retired product name remains in the current tree:\n" + "\n".join(hits))

    def test_no_secret_like_content_or_sensitive_file_types_are_tracked(self):
        bad_paths = []
        secret_hits = []
        for rel in tracked_paths():
            if sensitive_path(rel):
                bad_paths.append(rel)
        for records in (working_tree_records(), staged_records()):
            for label, data in records:
                if any(pattern.search(data) for pattern in SECRET_PATTERNS):
                    secret_hits.append(label)
        self.assertEqual(bad_paths, [], "Sensitive file types are tracked:\n" + "\n".join(bad_paths))
        self.assertEqual(secret_hits, [], "Secret-like values are tracked:\n" + "\n".join(secret_hits))

    def test_no_secret_like_content_is_reachable_in_history(self):
        hits = []
        for label, data in reachable_blobs():
            if any(pattern.search(data) for pattern in SECRET_PATTERNS):
                hits.append(label)
        self.assertEqual(hits, [], "Secret-like values are reachable from Git:\n" + "\n".join(hits))

    def test_no_sensitive_file_types_are_reachable_in_history(self):
        hits = [path for path in reachable_paths() if sensitive_path(path)]
        self.assertEqual(hits, [], "Sensitive paths are reachable from Git:\n" + "\n".join(hits))

    def test_no_personal_email_or_home_path_is_tracked_or_reachable(self):
        hits = []
        for label, data in disclosure_records():
            for match in EMAIL_PATTERN.finditer(data):
                if match.group(2).lower() not in GENERIC_EMAIL_DOMAINS:
                    hits.append(f"{label}: non-example email")
            for pattern in HOME_PATH_PATTERNS:
                for match in pattern.finditer(data):
                    if match.group(1).lower() not in GENERIC_HOME_NAMES:
                        hits.append(f"{label}: machine-specific home path")
        self.assertEqual(hits, [], "Personal identifiers are reachable from Git:\n" + "\n".join(hits))

    def test_design_docs_carry_no_personal_reasoning(self):
        hits = []
        for records in (working_tree_records(), staged_records()):
            for label, data in records:
                rel = label.split(":", 1)[1]
                if not rel.startswith(DESIGN_DOC_PREFIXES):
                    continue
                for line_no, line in enumerate(data.splitlines(), 1):
                    if any(pattern.search(line) for pattern in PERSONAL_REASONING_PATTERNS):
                        hits.append(f"{label}:{line_no}")
        self.assertEqual(hits, [], "Design docs quote the owner or their decisions:\n" + "\n".join(hits))

    def test_personal_reasoning_patterns_match_known_leaks(self):
        leaks = (
            b"## Decided by the user",
            b"Manual selection only, confirmed with the user",
            b"(the user found the first proposal too big)",
            b"Keep the canvas (the user: \"looks better\")",
            b"## Claude's calls",
            b"(confirmed with user during brainstorming)",
            b"without the user's go-ahead.",
        )
        for leak in leaks:
            self.assertTrue(any(p.search(leak) for p in PERSONAL_REASONING_PATTERNS), leak)
        self.assertFalse(any(p.search(b"Tell the user it is waiting in Approvals.")
                             for p in PERSONAL_REASONING_PATTERNS))

    def test_no_unapproved_network_host_is_tracked_or_reachable(self):
        hits = []
        for label, data in disclosure_records():
            for match in NETWORK_HOST_PATTERN.finditer(data):
                host = match.group(1).lower()
                if host not in PUBLIC_NETWORK_HOSTS and not host.endswith(b".example"):
                    hits.append(f"{label}: unapproved network host")
        self.assertEqual(hits, [], "Network endpoints are reachable from Git:\n" + "\n".join(hits))

    def test_commit_identity_matches_repository_owner(self):
        identities = subprocess.run(
            ["git", "log", "--all", "--format=author %an <%ae>%ncommitter %cn <%ce>"], cwd=ROOT,
            capture_output=True, text=True, check=True,
        ).stdout.splitlines()

        owners = set()
        for remote in subprocess.run(
            ["git", "remote"], cwd=ROOT, capture_output=True, text=True, check=True,
        ).stdout.splitlines():
            for url in subprocess.run(
                ["git", "remote", "get-url", "--all", remote], cwd=ROOT,
                capture_output=True, text=True, check=False,
            ).stdout.splitlines():
                match = re.search(r"github\.com[:/]([^/]+)/", url, re.IGNORECASE)
                if match:
                    owners.add(match.group(1))

        self.assertEqual(len(owners), 1, "Expected one GitHub repository owner")
        owner = next(iter(owners))
        # Any name is fine (GitHub merges stamp the public profile name); the email
        # must be the owner's noreply address so no personal mailbox leaks.
        expected = re.compile(
            rf"[^<>]+ <\d+\+{re.escape(owner)}@users\.noreply\.github\.com>"
        )
        # Squash/merge-commit merges on GitHub commit as the generic web-flow identity.
        web_flow_committer = re.compile(r"committer GitHub <noreply@github\.com>")
        self.assertTrue(identities)
        self.assertEqual(
            [
                identity for identity in identities
                if not web_flow_committer.fullmatch(identity)
                and not expected.fullmatch(identity.split(" ", 1)[1])
            ], [],
            "Commit identity does not match the repository owner's GitHub noreply identity",
        )


if __name__ == "__main__":
    unittest.main()
