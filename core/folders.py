"""folders.py: the folders a workspace links, and what a bot turn may do in them.

One file per workspace, in its team folder (core.bots.ws_dir), gitignored:

  {"folders": [{"id": "f1", "path": "/abs/app", "name": "app", "access": "edit"}],
   "rooms":   {"t-launch": {"f1": "read"}},
   "bots":    {"cmo": {"f1": "read"}}}

A turn starts with every folder at its own access. A channel entry, then a bot entry, can
only narrow it: a folder missing from a set layer is gone, access is the lower of the two,
and an empty layer means no folders. Unlinking a folder removes it from every layer.

This file is the only source. A project's old `local_folder` line in project.md moves here
once, as `read`, and leaves project.md. New links default to `edit`.
"""
import fnmatch
import json
import os
import re
import subprocess
from pathlib import Path, PurePath

from core import bots

READ, EDIT = "read", "edit"
ACCESS = (READ, EDIT)
FILE_NAME = "folders.json"

# Never written by a bot, in any folder: git, hook-manager and editor config can make code
# run later (hooks, tasks, MCP servers), and secrets stay unreadable too. A file named like
# one of the directories is refused as well: a `.git` file can point git at hooks elsewhere,
# and `HEAD` is what makes git take a folder for a repository.
NEVER_WRITE_DIRS = (".git", ".husky", ".claude", ".codex", ".agents", ".vscode",
                    ".lefthook", ".githooks", ".cursor", ".gemini")
NEVER_WRITE_FILES = (".mcp.json", ".envrc", ".pre-commit-config.yaml",
                     "lefthook.yml", "lefthook.yaml", ".lefthook.yml", ".lefthook.yaml",
                     "lefthook-local.yml", "lefthook-local.yaml", ".lefthook-local.yml", ".lefthook-local.yaml",
                     "HEAD")
# Secret-looking files stay unreadable even inside the repo or a linked folder
# (Grep/Glob honor Read deny rules too). Verified against claude 2.x:
# "**/x" covers the repo; a linked --add-dir needs its own "//abs/**/x" rule.
SECRET_FILE_GLOBS = [
    ".env", ".env.*", "*.pem", "*.key", ".npmrc", ".netrc",
    "credentials*.json", "secrets.*", "id_rsa*", "id_ed25519*",
    ".auth-token", "owner-token",  # dashboard login token (server.TOKEN_FILE, OWNER_TOKEN_FILE)
    "engines.json", "runners.json",  # API keys (dashboard.engines.config), runner token hashes
    ".myos-runner-token", ".myos-runner-sessions.json",  # a runner's own files on a person's Mac
    "integrations.json",  # Composio key + sessions (core.integrations)
    "people.json", "invites.json", "access.json",  # invited people (core.people)
]

# A deny rule is a glob: a path holding glob characters (or a backslash, which escapes the
# next one, or an extglob opener) could not be matched by its own secret-file rules, so such
# a folder is never linked. Plain parentheses are fine: checked live on Claude Code.
_GLOB_CHARS = set("*?[]{}\\")
_EXTGLOB = ("!(", "@(", "+(")
_ID_RE = re.compile(r"^f([1-9][0-9]{0,5})$")
_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_KINDS = ("rooms", "bots")

# Cloud drives sync under ~/Library (None = any provider folder): an app folder
# inside one is fine, the drive itself (everything synced) is not.
_CLOUD_DRIVES = (("Library", "Mobile Documents", "com~apple~CloudDocs"), ("Library", "CloudStorage", None))


def has_glob_chars(path) -> bool:
    text = str(path)
    return bool(_GLOB_CHARS & set(text)) or any(x in text for x in _EXTGLOB)


_GLOB_MSG = "local folder path cannot contain * ? [ ] { }, a backslash, or !( @( +("


class FolderError(ValueError):
    """A folder that cannot be linked, or a change to the store that is not allowed."""


def _inside_cloud_drive(rel_parts) -> bool:
    return any(len(rel_parts) > len(drive)
               and all(d is None or d == p for d, p in zip(drive, rel_parts))
               for drive in _CLOUD_DRIVES)


def normalize_path(value, root=bots.ROOT) -> str:
    """Return one safe folder as a resolved absolute path, or "" for empty input."""
    raw = str(value or "").strip()
    if not raw:
        return ""
    if any(ord(char) < 32 for char in raw):
        raise FolderError("local folder path cannot contain control characters")
    if has_glob_chars(raw):
        raise FolderError(_GLOB_MSG)
    path = Path(raw).expanduser()
    if not path.is_absolute():
        raise FolderError("local folder must be an absolute path")
    if not path.exists():
        raise FolderError(f"local folder does not exist: {path}")
    if not path.is_dir():
        raise FolderError(f"local folder is not a directory: {path}")
    resolved = path.resolve()
    if has_glob_chars(resolved):
        raise FolderError(_GLOB_MSG)
    workspace_root = Path(root).resolve()
    home = Path.home().resolve()
    filesystem_root = Path(resolved.anchor).resolve()
    if resolved in {filesystem_root, home, workspace_root}:
        raise FolderError("local folder must be a specific app directory")
    # A parent of myOS (for example ~/Documents/GitHub) would expose this repo
    # and every sibling app when passed to Claude as an allowed directory.
    if workspace_root.is_relative_to(resolved):
        raise FolderError("local folder cannot contain the myOS workspace")
    # Claude's config holds every bot session's transcript, all workspaces.
    if resolved.is_relative_to(home / ".claude"):
        raise FolderError("local folder cannot be inside the Claude config folder")
    # ~/.ssh, ~/.config, ~/.aws, ~/Library: keys and CLI tokens, never app code.
    if resolved.is_relative_to(home):
        rel = resolved.relative_to(home).parts
        if (rel[0].startswith(".") or rel[0] == "Library") and not _inside_cloud_drive(rel):
            raise FolderError("local folder cannot be a hidden or Library folder in your home")
    # "Open folder" would launch an app bundle instead of showing it.
    if any(part.lower().endswith(".app") for part in resolved.parts):
        raise FolderError("local folder cannot be an app bundle")
    # myOS's own data is written only through osctl; a link inside it would bypass that.
    if resolved.is_relative_to(workspace_root):
        raise FolderError("local folder cannot be inside the myOS folder")
    return str(resolved)


# ---- store ------------------------------------------------------------------ #
def _inside_root(path, root) -> Path:
    """`path` resolved, refused unless it stays inside the myOS folder: a project folder that
    is a symlink to elsewhere never gets the store written or read through it."""
    real = os.path.normpath(os.path.realpath(path))
    base = os.path.realpath(root)
    if not real.startswith(base + os.sep):
        raise ValueError("unknown workspace")
    return Path(real)


def _path(ws, root):
    return _inside_root(bots.ws_dir(ws, root) / FILE_NAME, root)


def _empty():
    return {"folders": [], "rooms": {}, "bots": {}}


def _clean_layer(value):
    if not isinstance(value, dict):
        return None
    return {k: (v if v in ACCESS else READ) for k, v in value.items()
            if isinstance(k, str) and _ID_RE.match(k)}


def _clean(data) -> dict:
    """Keep what is well formed: bad rows go, unknown access reads as `read`."""
    out = _empty()
    if not isinstance(data, dict):
        return out
    seen_ids, seen_paths = set(), set()
    for row in data.get("folders") or []:
        if not isinstance(row, dict):
            continue
        fid, path = row.get("id"), row.get("path")
        if not (isinstance(fid, str) and _ID_RE.match(fid)) or fid in seen_ids:
            continue
        if not isinstance(path, str) or not path.strip() or not PurePath(path).is_absolute():
            continue
        if any(ord(c) < 32 for c in path) or path in seen_paths:
            continue
        name = row.get("name")
        if not isinstance(name, str) or not name.strip() or any(ord(c) < 32 for c in name):
            name = PurePath(path).name or path
        access = row.get("access") if row.get("access") in ACCESS else READ
        out["folders"].append({"id": fid, "path": path, "name": name.strip()[:80], "access": access})
        seen_ids.add(fid)
        seen_paths.add(path)
    seq = data.get("seq")
    if isinstance(seq, int) and not isinstance(seq, bool) and 0 <= seq < 10 ** 6:
        out["seq"] = seq
    for kind in _KINDS:
        layers = data.get(kind)
        if not isinstance(layers, dict):
            continue
        for key, layer in layers.items():
            clean = _clean_layer(layer)
            if isinstance(key, str) and _KEY_RE.match(key) and clean is not None:
                out[kind][key] = clean
    return out


def _split_frontmatter(text):
    """(frontmatter lines, rest) of a project.md, or (None, text) without frontmatter."""
    if not text.startswith("---\n"):
        return None, text
    end = text.find("\n---", 4)
    if end < 0:
        return None, text
    return text[4:end].split("\n"), text[end:]


def _migrate(ws, root) -> dict:
    """Move a project's old `local_folder` line to the store, as `read`, once."""
    data = _empty()
    if not ws or ws == bots.HQ:
        return data
    try:
        md = _inside_root(Path(root) / "projects" / ws / "project.md", root)
    except ValueError:
        return data
    try:
        text = md.read_text(encoding="utf-8")
    except OSError:
        return data
    lines, rest = _split_frontmatter(text.replace("\r\n", "\n"))
    if lines is None:
        return data
    raw = None
    kept = []
    for line in lines:
        key, sep, value = line.partition(":")
        if sep and key.strip() == "local_folder":
            raw = value.strip()
        else:
            kept.append(line)
    if raw is None:
        return data
    if raw:
        try:
            path = normalize_path(raw, root)
        except FolderError:
            path = str(Path(raw).expanduser()) if Path(raw).expanduser().is_absolute() else ""
        if path and not any(ord(c) < 32 for c in path):
            data["folders"].append({"id": "f1", "path": path, "name": PurePath(path).name or path,
                                    "access": READ})
    # The store is written first: if the second write fails, the line stays but is never read.
    save(ws, data, root)
    bots._atomic_write(md, "---\n" + "\n".join(kept) + rest)
    return data


def _read(f):
    """(parsed data, damaged). Damaged: the file exists but is not a JSON object."""
    try:
        raw = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None, True
    return raw, not isinstance(raw, dict)


def load(ws, root=bots.ROOT) -> dict:
    """The workspace's folders and layers, migrating `local_folder` when no file exists.
    A damaged file reads as no folders: chat never breaks over it."""
    f = _path(ws, root)
    if not f.exists():
        return _migrate(ws, root)
    raw, bad = _read(f)
    return _empty() if bad else _clean(raw)


def damaged(ws, root=bots.ROOT) -> bool:
    """The file exists and is not readable JSON: writes refuse until it is fixed or removed,
    so a hand edit gone wrong never loses its links to the next change."""
    try:
        f = _path(ws, root)
    except ValueError:
        return False
    return f.exists() and _read(f)[1]


def _load_for_write(ws, root):
    if damaged(ws, root):
        raise FolderError(f"{_path(ws, root)} is damaged: fix it or delete it, then try again")
    return load(ws, root)


def save(ws, data, root=bots.ROOT) -> None:
    out = _clean(data)
    ids = [int(r["id"][1:]) for r in out["folders"]]
    if ids or "seq" in out:
        out["seq"] = max(ids + [out.get("seq", 0)])
    bots._atomic_write(_path(ws, root), json.dumps(out, indent=2) + "\n")


def _next_id(data) -> str:
    """The next free `f<N>`. `seq` remembers the highest id ever given, so an unlinked
    folder's id is never handed to another folder."""
    used = [int(r["id"][1:]) for r in data["folders"]]
    for kind in _KINDS:
        used += [int(k[1:]) for layer in data[kind].values() for k in layer]
    return f"f{max(used + [data.get('seq', 0)]) + 1}"


def _find(data, folder_id):
    for row in data["folders"]:
        if row["id"] == folder_id:
            return row
    raise FolderError(f"no linked folder '{folder_id}'")


def _need_access(access):
    if access not in ACCESS:
        raise FolderError("access: read or edit")


def add(ws, path, access=EDIT, root=bots.ROOT) -> dict:
    """Link a folder; a path already linked returns its row unchanged."""
    _need_access(access)
    resolved = normalize_path(path, root)
    if not resolved:
        raise FolderError("pick a folder to link")
    data = _load_for_write(ws, root)
    for row in data["folders"]:
        if row["path"] == resolved:
            return dict(row)
    row = {"id": _next_id(data), "path": resolved, "name": PurePath(resolved).name, "access": access}
    data["folders"].append(row)
    save(ws, data, root)
    return dict(row)


def remove(ws, folder_id, root=bots.ROOT) -> None:
    """Unlink a folder and take it out of every layer. A layer left empty stays, meaning no
    folders: dropping it would widen that channel or bot to every folder."""
    data = _load_for_write(ws, root)
    row = _find(data, folder_id)
    data["folders"].remove(row)
    for kind in _KINDS:
        for layer in data[kind].values():
            layer.pop(folder_id, None)
    save(ws, data, root)


def set_access(ws, folder_id, access, root=bots.ROOT) -> None:
    _need_access(access)
    data = _load_for_write(ws, root)
    _find(data, folder_id)["access"] = access
    save(ws, data, root)


def set_limit(ws, kind, key, limit, root=bots.ROOT) -> None:
    """Limit a channel (`rooms`, by its local id) or a bot (`bots`) to some folders.
    `limit` is {folder_id: access}; `None` clears the limit."""
    if kind not in _KINDS:
        raise FolderError("kind: rooms or bots")
    if not isinstance(key, str) or not _KEY_RE.match(key):
        raise FolderError(f"unknown {kind[:-1]}: {key!r}")
    data = _load_for_write(ws, root)
    if limit is None:
        data[kind].pop(key, None)
    else:
        if not isinstance(limit, dict):
            raise FolderError("limit: {folder id: read or edit}")
        ids = {r["id"] for r in data["folders"]}
        for fid, access in limit.items():
            if fid not in ids:
                raise FolderError(f"no linked folder '{fid}'")
            _need_access(access)
        data[kind][key] = dict(limit)
    save(ws, data, root)


# ---- turns ------------------------------------------------------------------ #
def usable(row, root=bots.ROOT) -> bool:
    """The row's path still passes every link check on this machine."""
    try:
        return bool(normalize_path(row.get("path"), root))
    except (FolderError, OSError):
        return False


def _lower(a, b):
    return READ if READ in (a, b) else EDIT


def for_turn(ws, room_local, bot_id, root=bots.ROOT) -> list:
    """The folders one turn gets, after the channel layer (none for a DM: room_local None)
    and the bot layer. Rows carry the resolved path and access; unusable folders drop."""
    data = load(ws, root)
    access = {r["id"]: r["access"] for r in data["folders"]}
    for kind, key in (("rooms", room_local), ("bots", bot_id)):
        layer = data[kind].get(key) if key is not None else None
        if layer is None:
            continue
        access = {fid: _lower(a, layer[fid]) for fid, a in access.items() if fid in layer}
    rows = []
    for row in data["folders"]:
        if row["id"] not in access:
            continue
        try:
            path = normalize_path(row["path"], root)
        except (FolderError, OSError):
            continue
        rows.append({**row, "path": path, "access": access[row["id"]]})
    return rows


def _inside(child, parent) -> bool:
    return PurePath(child).is_relative_to(PurePath(parent))


def turn_denies(kept_paths, other_paths, own_removed_paths) -> tuple:
    """Folders a turn must refuse. Another workspace's folder is left out when it is the
    same as, inside or contains a kept folder (a deny would block the owner's own link).
    An own folder a layer removed is left out when it is the same as or contains a kept one;
    one inside a kept folder is denied, since the kept folder's access would reach it."""
    kept = [str(k) for k in kept_paths]
    out = []
    for p in other_paths:
        p = str(p)
        if any(_inside(p, k) or _inside(k, p) for k in kept):
            continue
        if p not in out:
            out.append(p)
    for p in own_removed_paths:
        p = str(p)
        if any(_inside(k, p) for k in kept):
            continue
        if p not in out:
            out.append(p)
    return tuple(out)


def all_paths(ws, root=bots.ROOT) -> list:
    """Every linked path of a workspace, resolved like a turn's rows (symlinks followed),
    so a stored path and the path a turn used compare equal. None for an unknown workspace."""
    try:
        rows = load(ws, root)["folders"]
    except ValueError:
        return []
    return list(dict.fromkeys(os.path.realpath(r["path"]) for r in rows))


def other_workspace_paths(ws, slugs, root=bots.ROOT) -> list:
    """Every folder linked by another workspace, HQ included. HQ itself gets none: it is the
    owner's office and keeps today's behaviour of no deny list."""
    if not ws or ws == bots.HQ:
        return []
    out = []
    for other in [bots.HQ, *slugs]:
        if other == ws:
            continue
        try:
            paths = all_paths(other, root)
        except ValueError:
            continue
        out += [p for p in paths if p not in out]
    return out


# ---- file rules ------------------------------------------------------------- #
def may_write(path, base) -> bool:
    """False when a bot may not write `path` inside folder `base`: outside it, under a
    never-write directory, or a never-write or secret-looking file name. Names compare
    case-blind, as on a Mac's disk."""
    try:
        rel = PurePath(path).relative_to(PurePath(base))
    except ValueError:
        return False
    parts = [p.lower() for p in rel.parts]
    if not parts or ".." in parts:
        return False
    if any(p in NEVER_WRITE_DIRS for p in parts):
        return False
    name = parts[-1]
    if name in {f.lower() for f in NEVER_WRITE_FILES}:
        return False
    return not any(fnmatch.fnmatchcase(name, g.lower()) for g in SECRET_FILE_GLOBS)


def hook_dirs(folder) -> list:
    """The repo's own `core.hooksPath` when it sits inside `folder` (a bot must never write
    hooks there). Read-only `git config`; none when git or the setting is missing."""
    try:
        out = subprocess.run(["git", "-C", str(folder), "config", "--get", "core.hooksPath"],
                             capture_output=True, text=True, timeout=5)
        if out.returncode != 0 or not out.stdout.strip():
            return []
        top = subprocess.run(["git", "-C", str(folder), "rev-parse", "--show-toplevel"],
                             capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return []
    if top.returncode != 0:
        return []
    hp = Path(os.path.realpath(Path(top.stdout.strip()) / Path(out.stdout.strip()).expanduser()))
    base = Path(os.path.realpath(folder))
    return [str(hp)] if hp == base or base in hp.parents else []


def is_git_repo(path) -> bool:
    p = Path(path)
    return any((d / ".git").exists() for d in (p, *p.parents))


def first_indexed(rows):
    """The first folder with a TokenSave index, or None."""
    for row in rows:
        if (Path(row["path"]) / ".tokensave" / "tokensave.db").is_file():
            return row["path"]
    return None
