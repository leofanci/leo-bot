"""Workspace logos: one small image per workspace (HQ or a project).

Stored as `team/logos/<ws>.<ext>` (0600, gitignored with the rest of `team/`).
The type comes from the file's own first bytes, never from the client: only
PNG, JPEG and WebP. SVG is refused, since an SVG can carry script.
"""
import os
import re
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MAX_BYTES = 512 * 1024
_WS_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}")
TYPES = {"png": "image/png", "jpg": "image/jpeg", "webp": "image/webp"}


def _dir(root=None):
    return Path(root or ROOT) / "team" / "logos"


def _ws(ws):
    if not isinstance(ws, str) or not _WS_RE.fullmatch(ws):
        raise ValueError("invalid workspace")
    return ws


def sniff(data):
    """The image type from its magic bytes, or None."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


def _path(ws, ext, root=None):
    """`team/logos/<ws>.<ext>`, refused unless it stays inside that folder."""
    base = os.path.realpath(_dir(root))
    full = os.path.normpath(os.path.join(base, f"{_ws(ws)}.{ext}"))
    if not full.startswith(base + os.sep):
        raise ValueError("invalid workspace")
    return Path(full)


def _find(ws, root=None):
    for ext in TYPES:
        p = _path(ws, ext, root)
        if p.is_file():
            return p, ext
    return None, None


def save(ws, data, root=None):
    ws = _ws(ws)
    if not isinstance(data, (bytes, bytearray)) or not data:
        raise ValueError("empty image")
    if len(data) > MAX_BYTES:
        raise ValueError("logo too large (max 512 KB)")
    ext = sniff(bytes(data))
    if not ext:
        raise ValueError("logo must be a PNG, JPEG or WebP image")
    d = _dir(root)
    d.mkdir(parents=True, exist_ok=True)
    os.chmod(d, 0o700)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".logo-")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.chmod(tmp, 0o600)
        remove(ws, root)
        os.replace(tmp, _path(ws, ext, root))
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return version(ws, root)


def get(ws, root=None):
    """(bytes, content type) or None."""
    try:
        ws = _ws(ws)
    except ValueError:
        return None
    p, ext = _find(ws, root)
    return (p.read_bytes(), TYPES[ext]) if p else None


def version(ws, root=None):
    """A short token that changes with the image (for cache-busting), or None."""
    try:
        ws = _ws(ws)
    except ValueError:
        return None
    p, _ = _find(ws, root)
    return str(p.stat().st_mtime_ns) if p else None


def remove(ws, root=None):
    for ext in TYPES:
        _path(ws, ext, root).unlink(missing_ok=True)
