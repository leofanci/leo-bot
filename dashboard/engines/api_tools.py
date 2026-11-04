"""api_tools.py: the tools myOS itself defines for the api engine.

No CLI runs here, so nothing else exists: a tool the policy does not allow is
never offered, and every path and host is checked by myOS.

  osctl         always; runs on the server with the turn's workspace bound
  read_file     not shared; repo and linked folder minus every private path
  search_files  not shared, file_search on; same check
  web_fetch     web on; only hosts the user typed, never a private address
  apps tools    apps on, not shared; through the apps relay, same gate as Claude Code
"""
import fnmatch
import html
import ipaddress
import json
import os
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from dashboard.engines.claude_code import SECRET_FILE_GLOBS

MAX_OUT_CHARS = 120_000
MAX_FILE_BYTES = 2_000_000
MAX_FETCH_BYTES = 2_000_000
MAX_REDIRECTS = 4
FETCH_TIMEOUT = 20
MAX_MATCHES = 200
# Every workspace's data at once, or the machine's own state (same list as osctl read-file).
_PRIVATE_PREFIXES = ("chats/", "team/", "portfolio/", "database/", "os.db", ".remember/",
                     ".superpowers/", ".tokensave/", ".claude/", ".codex/", ".git/", "dashboard/vendor/")
_PRIVATE_SUFFIXES = (".db", ".db-wal", ".db-shm", ".db-journal", ".sqlite", ".sqlite3", ".log")

OSCTL_SPEC = {
    "name": "osctl",
    "description": ("Run one myOS command (the osctl CLI) in this workspace. `args` = the words after "
                    "`python3 -m dashboard.osctl`, one per item, e.g. [\"get-project\", \"--slug\", \"acme\"]. "
                    "Long bodies go in `stdin` instead of --text. Returns the command's JSON output."),
    "input_schema": {"type": "object", "properties": {
        "args": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 64},
        "stdin": {"type": "string"}}, "required": ["args"], "additionalProperties": False},
}
READ_SPEC = {
    "name": "read_file",
    "description": "Read one text file by path (relative to the myOS folder, or inside the linked app folder).",
    "input_schema": {"type": "object", "properties": {"path": {"type": "string"}},
                     "required": ["path"], "additionalProperties": False},
}
SEARCH_SPEC = {
    "name": "search_files",
    "description": "Search the linked app folder. `glob` filters file names (e.g. **/*.py), `pattern` is a regex on file contents; either or both.",
    "input_schema": {"type": "object", "properties": {"glob": {"type": "string"}, "pattern": {"type": "string"}},
                     "additionalProperties": False},
}
FETCH_SPEC = {
    "name": "web_fetch",
    "description": "Fetch one web page as text. Only hosts the user named in their message are reachable.",
    "input_schema": {"type": "object", "properties": {"url": {"type": "string"}},
                     "required": ["url"], "additionalProperties": False},
}


class ToolError(Exception):
    """Shown to the model as a failed tool call."""


def is_private_path(path, root, policy):
    """True when a resolved path holds secrets or data this turn may not read."""
    path, root = Path(path), Path(root).resolve()
    name = path.name.lower()
    if any(fnmatch.fnmatch(name, g.lower()) for g in SECRET_FILE_GLOBS):
        return True
    for private in (Path.home() / ".claude", Path.home() / ".codex", Path.home() / ".ssh"):
        if path == private or private in path.parents:
            return True
    for d in policy.deny_dirs:
        if path == Path(d) or Path(d) in path.parents:
            return True
    if str(path) in {str(f) for f in policy.deny_files}:
        return True
    try:
        rel = path.relative_to(root).as_posix().lower()
    except ValueError:
        return False
    if rel.startswith(_PRIVATE_PREFIXES) or rel.endswith(_PRIVATE_SUFFIXES):
        return True
    parts = rel.split("/")
    ws = (policy.workspace or "hq").lower()
    return ws != "hq" and parts[0] == "projects" and len(parts) > 1 and parts[1] != ws


def _inside(path, base):
    return path == base or base in path.parents


def read_file(raw, root, policy):
    root = Path(root).resolve()
    if not isinstance(raw, str) or not raw or "\x00" in raw:
        raise ToolError("path required")
    bases = [root] + ([Path(policy.workspace_dir).resolve()] if policy.workspace_dir else [])
    path = (root / raw).resolve()
    if not any(_inside(path, b) for b in bases) or is_private_path(path, root, policy):
        raise ToolError("that file is not readable here")
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            raise ToolError("that file is too large")
        return path.read_text(errors="replace")[:MAX_OUT_CHARS]
    except OSError:
        raise ToolError("no such file") from None


def search_files(glob, pattern, root, policy):
    if not policy.workspace_dir:
        raise ToolError("no linked app folder this turn")
    base = Path(policy.workspace_dir).resolve()
    try:
        regex = re.compile(pattern) if pattern else None
    except re.error as e:
        raise ToolError(f"bad pattern: {e}") from None
    hits, scanned = [], 0
    for dirpath, dirs, files in os.walk(base):
        dirs[:] = [d for d in dirs if d not in (".git", "node_modules", "__pycache__", ".venv", "dist", "build")]
        for name in files:
            full = Path(dirpath) / name
            rel = full.relative_to(base).as_posix()
            if glob and not (fnmatch.fnmatch(rel, glob) or fnmatch.fnmatch(name, glob)):
                continue
            if is_private_path(full.resolve(), root, policy) or not _inside(full.resolve(), base):
                continue
            if not regex:
                hits.append(rel)
            else:
                scanned += 1
                if scanned > 5000:
                    return "\n".join(hits + ["(stopped: too many files)"])
                try:
                    if full.stat().st_size > MAX_FILE_BYTES:
                        continue
                    for i, line in enumerate(full.read_text(errors="ignore").splitlines(), 1):
                        if regex.search(line[:2000]):
                            hits.append(f"{rel}:{i}: {line[:200]}")
                            break
                except OSError:
                    continue
            if len(hits) >= MAX_MATCHES:
                return "\n".join(hits + ["(stopped: too many matches)"])
    return "\n".join(hits) or "(no matches)"


def _public_host(host):
    """Every address the host resolves to must be public (no cloud metadata, no localhost)."""
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return False
    for info in infos:
        addr = ipaddress.ip_address(info[4][0].split("%")[0])
        if not addr.is_global:
            return False
    return bool(infos)


def _host_allowed(host, domains):
    host = (host or "").lower().rstrip(".")
    host = host[4:] if host.startswith("www.") else host
    return host in {d.lower() for d in domains}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


def web_fetch(url, policy, opener=None):
    opener = opener or urllib.request.build_opener(_NoRedirect)
    for _ in range(MAX_REDIRECTS + 1):
        parts = urllib.parse.urlsplit(url if isinstance(url, str) else "")
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ToolError("an http(s) address is required")
        if not _host_allowed(parts.hostname, policy.web_domains):
            raise ToolError(f"{parts.hostname} was not named in the message, so it cannot be fetched")
        if not _public_host(parts.hostname):
            raise ToolError(f"{parts.hostname} is not a public address")
        req = urllib.request.Request(url, headers={"User-Agent": "myOS", "Accept": "text/html,text/plain,*/*"})
        try:
            with opener.open(req, timeout=FETCH_TIMEOUT) as resp:
                raw = resp.read(MAX_FETCH_BYTES)
                ctype = resp.headers.get("Content-Type", "")
            return _page_text(raw, ctype)[:MAX_OUT_CHARS]
        except urllib.error.HTTPError as e:
            if e.code in (301, 302, 303, 307, 308) and e.headers.get("Location"):
                url = urllib.parse.urljoin(url, e.headers["Location"])
                continue
            raise ToolError(f"the page returned {e.code}") from None
        except (urllib.error.URLError, OSError) as e:
            raise ToolError(f"could not reach the page: {e}") from None
    raise ToolError("too many redirects")


def _page_text(raw, ctype):
    text = raw.decode("utf-8", "replace")
    if "html" not in ctype.lower():
        return text
    text = re.sub(r"(?is)<(script|style|noscript)\b.*?</\1\s*>", " ", text)
    text = re.sub(r"(?s)<[^>]{0,2000}>", " ", text)
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


class ToolBox:
    """The tools one turn may use, and how to run them."""

    def __init__(self, policy, root, tokens, engine_id, backends):
        self.policy, self.root, self.tokens = policy, root, tokens
        self.engine_id, self.backends = engine_id, backends
        self.specs, self._apps = [OSCTL_SPEC], set()
        if not policy.shared:
            self.specs.append(READ_SPEC)
            if policy.file_search and policy.workspace_dir:
                self.specs.append(SEARCH_SPEC)
        if policy.web and policy.web_domains:
            self.specs.append(FETCH_SPEC)
        if policy.apps and not policy.shared and tokens.apps_token and backends.get("apps_list"):
            for tool in backends["apps_list"](tokens.apps_token):
                self.specs.append({"name": tool["name"], "description": tool.get("description") or "",
                                   "input_schema": tool.get("inputSchema") or {"type": "object", "properties": {}}})
                self._apps.add(tool["name"])

    def osctl_ctx(self):
        p = self.policy
        return {"ws": p.workspace or "hq", "readonly": p.readonly, "apps_used": p.apps_used,
                "shared": p.shared, "engine": self.engine_id}

    def run(self, name, arguments):
        """(text, is_error) for one tool call. Never raises for the model's own mistakes."""
        args = arguments if isinstance(arguments, dict) else {}
        try:
            if name == "osctl":
                runner = self.backends.get("osctl")
                clean = self.backends.get("osctl_clean")
                if not runner:
                    raise ToolError("osctl is not available")
                argv, stdin = clean(args) if clean else (args.get("args"), args.get("stdin") or "")
                text, failed = runner(argv, stdin, self.osctl_ctx())
                return text, bool(failed)
            if name == "read_file" and not self.policy.shared:
                return read_file(args.get("path"), self.root, self.policy), False
            if name == "search_files" and not self.policy.shared and self.policy.file_search:
                return search_files(args.get("glob"), args.get("pattern"), self.root, self.policy), False
            if name == "web_fetch" and self.policy.web:
                return web_fetch(args.get("url"), self.policy), False
            if name in self._apps:
                return self.backends["apps_call"](self.tokens.apps_token, name, args)
            return f'"{name}" is not available here.', True
        except (ToolError, ValueError) as e:
            return str(e), True
        except Exception as e:  # noqa: BLE001  a tool failure must not end the turn
            return f"tool failed: {type(e).__name__}", True


def as_json(value):
    return json.dumps(value, ensure_ascii=False)
