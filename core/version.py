"""The app version: the VERSION file in the checkout, plus semver helpers."""
import re

from core import paths

_SEMVER = re.compile(r"v?(\d+)\.(\d+)\.(\d+)")


def current() -> str:
    try:
        return (paths.CODE / "VERSION").read_text(encoding="utf-8").strip() or "0.0.0"
    except OSError:
        return "0.0.0"


def parse(tag: str) -> tuple[int, int, int] | None:
    """`v1.2.3` or `1.2.3` to (1, 2, 3); anything else (pre-releases too) is None."""
    m = _SEMVER.fullmatch((tag or "").strip())
    if not m:
        return None
    return int(m.group(1)), int(m.group(2)), int(m.group(3))


def latest_tag(ls_remote_output: str) -> str | None:
    """Highest vX.Y.Z tag in `git ls-remote --tags` output. Peeled `^{}` lines
    and non-semver tags are ignored."""
    best: tuple[int, int, int] | None = None
    best_tag: str | None = None
    for line in (ls_remote_output or "").splitlines():
        ref = line.split("\t")[-1].strip()
        if not ref.startswith("refs/tags/") or ref.endswith("^{}"):
            continue
        tag = ref[len("refs/tags/"):]
        if not tag.startswith("v"):
            continue
        parsed = parse(tag)
        if parsed and (best is None or parsed > best):
            best, best_tag = parsed, tag
    return best_tag


def newer(a: str, b: str) -> bool:
    """True when b is a newer version than a (unparseable means False)."""
    pa, pb = parse(a), parse(b)
    return pa is not None and pb is not None and pb > pa
