"""Profile pictures for people: the owner (id "ceo") and invited people (their id).

A picture is either an uploaded image (`team/avatars/<id>.<ext>`, 0600) or a preset:
an icon on one of the identity hues (`team/avatars/presets.json`). One or the other,
never both: choosing one drops the other. The image type comes from the file's own
first bytes, never from the client: only PNG, JPEG and WebP. SVG is refused, since
an SVG can carry script.
"""
import json
import os
import re
import tempfile
from pathlib import Path

from core import paths, ws_logos

ROOT = paths.DATA
MAX_BYTES = ws_logos.MAX_BYTES
TYPES = ws_logos.TYPES
HUES = ("ocean", "clay", "plum", "ochre", "slate", "rose")
ICONS = ("smile", "zap", "flag", "globe", "compass", "chart", "flask", "map",
         "route", "mic", "book", "layers")
_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}")


def _dir(root=None):
    return Path(root or ROOT) / "team" / "avatars"


def _id(who):
    if not isinstance(who, str) or not _ID_RE.fullmatch(who):
        raise ValueError("invalid person")
    return who


def _path(who, ext, root=None):
    base = os.path.realpath(_dir(root))
    full = os.path.normpath(os.path.join(base, f"{_id(who)}.{ext}"))
    if not full.startswith(base + os.sep):
        raise ValueError("invalid person")
    return Path(full)


def _find(who, root=None):
    for ext in TYPES:
        p = _path(who, ext, root)
        if p.is_file():
            return p, ext
    return None, None


def _presets(root=None):
    try:
        data = json.loads((_dir(root) / "presets.json").read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write(path, raw, mode=0o600):
    d = path.parent
    d.mkdir(parents=True, exist_ok=True)
    os.chmod(d, 0o700)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".av-")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(raw)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _drop_image(who, root=None):
    for ext in TYPES:
        _path(who, ext, root).unlink(missing_ok=True)


def _save_presets(rows, root=None):
    _write(_dir(root) / "presets.json", (json.dumps(rows, indent=2) + "\n").encode())


def save_image(who, data, root=None):
    who = _id(who)
    if not isinstance(data, (bytes, bytearray)) or not data:
        raise ValueError("empty image")
    if len(data) > MAX_BYTES:
        raise ValueError("picture too large (max 512 KB)")
    ext = ws_logos.sniff(bytes(data))
    if not ext:
        raise ValueError("picture must be a PNG, JPEG or WebP image")
    _drop_image(who, root)
    _write(_path(who, ext, root), bytes(data))
    rows = _presets(root)
    if rows.pop(who, None) is not None:
        _save_presets(rows, root)
    return view(who, root)


def save_preset(who, icon, hue, root=None):
    who = _id(who)
    if icon not in ICONS or hue not in HUES:
        raise ValueError("unknown preset")
    _drop_image(who, root)
    rows = _presets(root)
    rows[who] = {"icon": icon, "hue": hue}
    _save_presets(rows, root)
    return view(who, root)


def remove(who, root=None):
    who = _id(who)
    _drop_image(who, root)
    rows = _presets(root)
    if rows.pop(who, None) is not None:
        _save_presets(rows, root)
    return None


def get_image(who, root=None):
    """(bytes, content type) or None."""
    try:
        p, ext = _find(who, root)
    except ValueError:
        return None
    return (p.read_bytes(), TYPES[ext]) if p else None


def view(who, root=None):
    """{"image": <cache token>} or {"icon", "hue"} or None."""
    try:
        p, _ = _find(who, root)
    except ValueError:
        return None
    if p:
        return {"image": str(p.stat().st_mtime_ns)}
    row = _presets(root).get(who)
    if isinstance(row, dict) and row.get("icon") in ICONS and row.get("hue") in HUES:
        return {"icon": row["icon"], "hue": row["hue"]}
    return None


def all_views(root=None):
    out = {}
    d = _dir(root)
    names = {p.stem for p in d.iterdir() if p.suffix[1:] in TYPES} if d.is_dir() else set()
    for who in names | set(_presets(root)):
        v = view(who, root)
        if v:
            out[who] = v
    return out
