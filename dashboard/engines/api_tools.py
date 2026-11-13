"""api_tools.py: the tools Leo Bot itself defines for the api engine.

No CLI runs here, so nothing else exists: a tool the policy does not allow is
never offered, and every path and host is checked by Leo Bot.

  osctl         always; runs on the server with the turn's workspace bound
  read_file     not shared; repo and the turn's linked folders minus every private path
  search_files  not shared, linked folders this turn; same check
  write_file    not shared, edit folders this turn; never the never-write list (core.folders)
  edit_file     same as write_file; one exact match, UTF-8 text only
  web_fetch     web on; only hosts the user typed, never a private address
  apps tools    apps on, not shared; through the apps relay, same gate as Claude Code
"""
import fnmatch
import html
import http.client
import ipaddress
import json
import os
import re
import socket
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from core import folders
from dashboard.engines.claude_code import SECRET_FILE_GLOBS

MAX_OUT_CHARS = 120_000
MAX_FILE_BYTES = 2_000_000
MAX_FETCH_BYTES = 2_000_000
MAX_REDIRECTS = 4
FETCH_TIMEOUT = 20
FETCH_DEADLINE = 60  # the whole fetch, redirects included
MAX_MATCHES = 200
# Every workspace's data at once, or the machine's own state (same list as osctl read-file).
_PRIVATE_PREFIXES = ("chats/", "team/", "portfolio/", "private/", "database/", "os.db", ".remember/",
                     ".superpowers/", ".tokensave/", ".claude/", ".codex/", ".git/", "dashboard/vendor/")
_PRIVATE_SUFFIXES = (".db", ".db-wal", ".db-shm", ".db-journal", ".sqlite", ".sqlite3", ".log")
_HOME_PRIVATE = (".claude", ".codex", ".ssh", ".aws", ".gnupg", ".kube", ".docker", ".config")
SEARCH_TIMEOUT = 20
MAX_PATTERN_CHARS = 1000
_NAT64 = ipaddress.ip_network("64:ff9b::/96")
_SKIP_TAGS = (b"script", b"style", b"noscript")
# A model's regex runs in its own process: re holds the GIL, so a catastrophic
# pattern here would freeze every request on the server. This one can be killed.
_GREP_WORKER = """
import json, re, sys
job = json.load(sys.stdin)
regex, hits = re.compile(job["pattern"]), []
for rel, full in job["files"]:
    try:
        with open(full, errors="ignore") as f:
            for i, line in enumerate(f, 1):
                if regex.search(line[:2000]):
                    hits.append(f"{rel}:{i}: {line[:200].rstrip()}")
                    break
    except OSError:
        continue
    if len(hits) >= job["max"]:
        break
print(json.dumps(hits))
"""

OSCTL_SPEC = {
    "name": "osctl",
    "description": ("Run one Leo Bot command (the osctl CLI) in this workspace. `args` = the words after "
                    "`python3 -m dashboard.osctl`, one per item, e.g. [\"get-project\", \"--slug\", \"acme\"]. "
                    "Long bodies go in `stdin` instead of --text. Returns the command's JSON output."),
    "input_schema": {"type": "object", "properties": {
        "args": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 64},
        "stdin": {"type": "string"}}, "required": ["args"], "additionalProperties": False},
}
READ_SPEC = {
    "name": "read_file",
    "description": "Read one text file by path (relative to the Leo Bot folder, or an absolute path inside a linked folder).",
    "input_schema": {"type": "object", "properties": {"path": {"type": "string"}},
                     "required": ["path"], "additionalProperties": False},
}
SEARCH_SPEC = {
    "name": "search_files",
    "description": ("Search the linked folders (all of them, or `folder`, one folder's path). `glob` filters "
                    "file names (e.g. **/*.py), `pattern` is a regex on file contents; either or both. "
                    "Returns absolute paths."),
    "input_schema": {"type": "object", "properties": {"glob": {"type": "string"}, "pattern": {"type": "string"},
                                                      "folder": {"type": "string"}},
                     "additionalProperties": False},
}
WRITE_SPEC = {
    "name": "write_file",
    "description": ("Create or replace one text file inside a linked folder with edit access. `path` is "
                    "absolute, or relative to the first edit folder. Git, editor and secret files are refused."),
    "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                     "required": ["path", "content"], "additionalProperties": False},
}
EDIT_SPEC = {
    "name": "edit_file",
    "description": ("Change one text file inside a linked folder with edit access: `old_text` must appear "
                    "exactly once and is replaced by `new_text`. Read the file first."),
    "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "old_text": {"type": "string"},
                                                      "new_text": {"type": "string"}},
                     "required": ["path", "old_text", "new_text"], "additionalProperties": False},
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
    for private in _HOME_PRIVATE:
        if folders.under(path, Path.home() / private):
            return True
    for d in policy.deny_dirs:
        if folders.under(path, d):
            return True
    if any(folders.under(path, f) for f in policy.deny_files):
        return True
    if not folders.under(path, root):
        return False
    try:
        rel = path.relative_to(root).as_posix().lower()
    except ValueError:
        # a case variant of the root: walk up to the ancestor that is the root
        up = next(a for a in (path, *path.parents) if folders.under(a, root) and folders.under(root, a))
        rel = path.relative_to(up).as_posix().lower()
    if rel.startswith(_PRIVATE_PREFIXES) or rel.endswith(_PRIVATE_SUFFIXES):
        return True
    parts = rel.split("/")
    ws = (policy.workspace or "hq").lower()
    return ws != "hq" and parts[0] == "projects" and len(parts) > 1 and parts[1] != ws


def _inside(path, base):
    return folders.under(path, base)


def read_file(raw, root, policy):
    root = Path(root).resolve()
    if not isinstance(raw, str) or not raw or "\x00" in raw:
        raise ToolError("path required")
    bases = [root] + [Path(d).resolve() for d in policy.read_dirs]
    path = (root / raw).resolve()
    if not any(_inside(path, b) for b in bases) or is_private_path(path, root, policy):
        raise ToolError("that file is not readable here")
    try:
        # A pipe or device in a linked folder would hang the turn forever.
        if not path.is_file():
            raise ToolError("no such file")
        if path.stat().st_size > MAX_FILE_BYTES:
            raise ToolError("that file is too large")
        return path.read_text(errors="replace")[:MAX_OUT_CHARS]
    except OSError:
        raise ToolError("no such file") from None


def search_files(glob, pattern, root, policy, folder=None):
    """Search every read folder of the turn, or the one named by `folder`. Absolute paths out."""
    if not policy.read_dirs:
        raise ToolError("no linked folder this turn")
    bases = [Path(d).resolve() for d in policy.read_dirs]
    if folder:
        wanted = Path(str(folder)).expanduser().resolve() if isinstance(folder, str) else None
        if wanted not in bases:
            raise ToolError("that folder is not linked this turn")
        bases = [wanted]
    if len(str(glob or "")) > MAX_PATTERN_CHARS or len(str(pattern or "")) > MAX_PATTERN_CHARS:
        raise ToolError("that search is too long")
    if pattern:
        try:
            re.compile(pattern)
        except re.error as e:
            raise ToolError(f"bad pattern: {e}") from None
    names, stopped = [], None
    for base in bases:
        for dirpath, dirs, files in os.walk(base):
            dirs[:] = [d for d in dirs if d not in (".git", "node_modules", "__pycache__", ".venv", "dist", "build")]
            for name in files:
                full = Path(dirpath) / name
                rel = full.relative_to(base).as_posix()
                if glob and not (fnmatch.fnmatch(rel, glob) or fnmatch.fnmatch(name, glob)):
                    continue
                real = full.resolve()
                if is_private_path(real, root, policy) or not _inside(real, base):
                    continue
                try:
                    if not real.is_file() or (pattern and real.stat().st_size > MAX_FILE_BYTES):
                        continue
                except OSError:
                    continue
                names.append((str(full), str(real)))
                if len(names) >= (5000 if pattern else MAX_MATCHES):
                    stopped = "(stopped: too many files)" if pattern else "(stopped: too many matches)"
                    break
            if stopped:
                break
        if stopped:
            break
    hits = _grep(pattern, names) if pattern else [rel for rel, _ in names]
    return "\n".join(hits + ([stopped] if stopped else [])) or "(no matches)"


def _edit_target(raw, root, policy):
    """(edit folder, target path) for a write, or ToolError. The parent is resolved with
    symlinks followed; the target itself must be missing or a regular file, never a link."""
    if policy.shared or not policy.edit_dirs:
        raise ToolError("no folder can be changed this turn")
    if not isinstance(raw, str) or not raw.strip() or "\x00" in raw:
        raise ToolError("path required")
    reads = {Path(d).resolve() for d in policy.read_dirs}
    edits = [b for b in (Path(d).resolve() for d in policy.edit_dirs) if b in reads]
    if not edits:
        raise ToolError("no folder can be changed this turn")
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = edits[0] / path
    if path.name in ("", ".", ".."):
        raise ToolError("a file path is required")
    target = path.parent.resolve() / path.name
    base = next((b for b in edits if b in target.parents), None)
    if base is None:
        raise ToolError("only files inside a linked folder with edit access can be changed")
    # The deepest folder holding the target decides: a Read folder nested in an Edit one stays read-only.
    holder = max((b for b in reads if b in target.parents), key=lambda b: len(b.parts))
    if holder not in edits:
        raise ToolError("that folder is read-only this turn")
    hooks = [Path(h) for h in folders.hook_dirs(base)]
    if (not folders.may_write(target, base) or is_private_path(target, root, policy)
            or any(h == target or h in target.parents for h in hooks)):
        raise ToolError("that file cannot be changed: git, hook, editor and secret files stay as they are")
    try:
        st = os.lstat(target)
    except FileNotFoundError:
        return base, target
    except OSError:
        raise ToolError("that file cannot be changed") from None
    if not stat.S_ISREG(st.st_mode):
        raise ToolError("only regular files can be replaced")
    return base, target


def _write_atomic(base, target, data):
    """Write through a temp file in the same folder, then swap it in, keeping the mode. The walk
    from the edit folder down opens one folder at a time without following links (openat-style),
    so a folder swapped for a symlink after the checks is refused, never written through."""
    try:
        parts = Path(target).relative_to(base).parts
    except ValueError:
        raise ToolError("only files inside a linked folder with edit access can be changed") from None
    if not parts:
        raise ToolError("a file path is required")
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    tmp = None
    try:
        dfd = os.open(base, os.O_RDONLY | os.O_DIRECTORY)
    except OSError as e:
        raise ToolError(f"could not write that file: {e.strerror or e}") from None
    try:
        for part in parts[:-1]:
            try:
                os.mkdir(part, 0o755, dir_fd=dfd)
            except FileExistsError:
                pass
            nfd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | nofollow, dir_fd=dfd)
            os.close(dfd)
            dfd = nfd
        name = parts[-1]
        try:
            st = os.stat(name, dir_fd=dfd, follow_symlinks=False)
            if not stat.S_ISREG(st.st_mode):
                raise ToolError("only regular files can be replaced")
            mode = stat.S_IMODE(st.st_mode)
        except FileNotFoundError:
            mode = 0o644
        tmp = f".leobot-{os.urandom(6).hex()}.tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | nofollow, 0o600, dir_fd=dfd)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            os.fchmod(f.fileno(), mode)
        os.replace(tmp, name, src_dir_fd=dfd, dst_dir_fd=dfd)
        tmp = None
    except OSError as e:
        raise ToolError(f"could not write that file: {e.strerror or e}") from None
    finally:
        if tmp:
            try:
                os.unlink(tmp, dir_fd=dfd)
            except OSError:
                pass
        os.close(dfd)


def write_file(raw_path, content, root, policy):
    if not isinstance(content, str):
        raise ToolError("content must be text")
    data = content.encode("utf-8")
    if len(data) > MAX_FILE_BYTES:
        raise ToolError("that content is too large")
    base, target = _edit_target(raw_path, root, policy)
    _write_atomic(base, target, data)
    return f"wrote {target} ({len(data)} bytes)"


def edit_file(raw_path, old, new, root, policy):
    if not isinstance(old, str) or not old or not isinstance(new, str):
        raise ToolError("old_text and new_text are required")
    base, target = _edit_target(raw_path, root, policy)
    try:
        if not target.is_file():
            raise ToolError("no such file")
        if target.stat().st_size > MAX_FILE_BYTES:
            raise ToolError("that file is too large")
        raw = target.read_bytes()
    except OSError:
        raise ToolError("no such file") from None
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise ToolError("that file is not UTF-8 text, so it cannot be edited") from None
    count = text.count(old)
    if count != 1:
        raise ToolError("old_text was not found" if not count
                        else f"old_text appears {count} times: include more of the lines around it")
    data = text.replace(old, new, 1).encode("utf-8")
    if len(data) > MAX_FILE_BYTES:
        raise ToolError("the edited file would be too large")
    _write_atomic(base, target, data)
    return f"edited {target}"


def _grep(pattern, names):
    job = json.dumps({"pattern": pattern, "files": names, "max": MAX_MATCHES})
    try:
        out = subprocess.run([sys.executable, "-I", "-c", _GREP_WORKER], input=job, capture_output=True,
                             text=True, timeout=SEARCH_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise ToolError("that pattern took too long. Try a simpler one.") from None
    try:
        return json.loads(out.stdout)
    except ValueError:
        raise ToolError("the search failed") from None


def _is_public_ip(raw):
    addr = ipaddress.ip_address(str(raw).split("%")[0])
    # IPv6 forms that carry an IPv4 address: judge the one inside.
    inner = [getattr(addr, "ipv4_mapped", None), getattr(addr, "sixtofour", None),
             (getattr(addr, "teredo", None) or (None, None))[1]]
    if addr.version == 6 and addr in _NAT64:
        inner.append(ipaddress.ip_address(int(addr) & 0xffffffff))
    return all(a.is_global and not a.is_multicast for a in [addr] + [i for i in inner if i is not None])


def _public_host(host):
    """Every address the host resolves to must be public (no cloud metadata, no localhost)."""
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return False
    return bool(infos) and all(_is_public_ip(info[4][0]) for info in infos)


def _host_allowed(host, domains):
    host = (host or "").lower().rstrip(".")
    host = host[4:] if host.startswith("www.") else host
    return host in {d.lower() for d in domains}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


def _public_connect(address, timeout=None, source_address=None, **kw):
    """socket.create_connection, refusing a private peer. The check before the request
    resolved the name once; a host that rebinds to 127.0.0.1 afterwards is caught here."""
    sock = socket.create_connection(address, timeout, source_address, **kw)
    if not _is_public_ip(sock.getpeername()[0]):
        sock.close()
        raise OSError("that host points at a private address")
    return sock


class _PublicHTTPConnection(http.client.HTTPConnection):
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self._create_connection = _public_connect


class _PublicHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self._create_connection = _public_connect


class _PublicHTTP(urllib.request.HTTPHandler):
    def http_open(self, req):
        return self.do_open(_PublicHTTPConnection, req)


class _PublicHTTPS(urllib.request.HTTPSHandler):
    def https_open(self, req):
        return self.do_open(_PublicHTTPSConnection, req, context=self._context)


def _fetch_opener():
    # No proxy handler: a proxy would connect for us, so the peer check would see the proxy.
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect, _PublicHTTP, _PublicHTTPS)


def _read_until(resp, limit, deadline):
    """Up to `limit` bytes, one socket read at a time, stopping at `deadline`: the socket
    timeout covers each read, so a server dripping a byte at a time could hold a turn."""
    chunks, got = [], 0
    read = getattr(resp, "read1", None) or resp.read
    while got < limit:
        if time.monotonic() > deadline:
            raise ToolError("the page took too long to load")
        chunk = read(min(65536, limit - got))
        if not chunk:
            break
        chunks.append(chunk)
        got += len(chunk)
    return b"".join(chunks)


def web_fetch(url, policy, opener=None):
    opener = opener or _fetch_opener()
    deadline = time.monotonic() + FETCH_DEADLINE
    for _ in range(MAX_REDIRECTS + 1):
        parts = urllib.parse.urlsplit(url if isinstance(url, str) else "")
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ToolError("an http(s) address is required")
        if "@" in parts.netloc or "\\" in parts.netloc or "%" in parts.netloc:
            # urllib connects to the raw netloc: a user part or escapes could name another host
            raise ToolError("an address with a user name or escaped characters is not fetched")
        if not _host_allowed(parts.hostname, policy.web_domains):
            raise ToolError(f"{parts.hostname} was not named in the message, so it cannot be fetched")
        if not _public_host(parts.hostname):
            raise ToolError(f"{parts.hostname} is not a public address")
        req = urllib.request.Request(url, headers={"User-Agent": "Leo Bot", "Accept": "text/html,text/plain,*/*"})
        try:
            with opener.open(req, timeout=FETCH_TIMEOUT) as resp:
                raw = _read_until(resp, MAX_FETCH_BYTES, deadline)
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


def _strip_tags(raw):
    """HTML bytes to text bytes in one pass. Every step moves forward and an unclosed tag
    ends the scan, so a hostile page can't make this slow (a regex here was quadratic)."""
    lower, out, i, n = raw.lower(), [], 0, len(raw)
    while i < n:
        lt = raw.find(b"<", i)
        if lt < 0:
            out.append(raw[i:])
            break
        out.append(raw[i:lt])
        gt = raw.find(b">", lt)
        if gt < 0:  # a stray "<", not a tag: the words after it are still the page
            out.append(raw[lt + 1:])
            break
        i = gt + 1
        name = lower[lt + 1:lt + 12]
        tag = next((t for t in _SKIP_TAGS if name.startswith(t) and name[len(t):len(t) + 1] in b" \t\r\n/>"), None)
        if tag:
            close = lower.find(b"</" + tag, i)
            end = lower.find(b">", close) if close >= 0 else -1
            if end < 0:
                break
            i = end + 1
        out.append(b" ")
    return b"".join(out)


def _page_text(raw, ctype):
    if "html" not in ctype.lower():
        return raw.decode("utf-8", "replace")
    text = html.unescape(_strip_tags(raw).decode("utf-8", "replace"))
    return " ".join(text.split())


class ToolBox:
    """The tools one turn may use, and how to run them."""

    def __init__(self, policy, root, tokens, engine_id, backends):
        self.policy, self.root, self.tokens = policy, root, tokens
        self.engine_id, self.backends = engine_id, backends
        self.specs, self._apps = [OSCTL_SPEC], set()
        if not policy.shared:
            self.specs.append(READ_SPEC)
            if policy.read_dirs:
                self.specs.append(SEARCH_SPEC)
            if policy.edit_dirs:
                self.specs += [WRITE_SPEC, EDIT_SPEC]
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
                "shared": p.shared, "engine": self.engine_id,
                "chat": p.chat, "job": p.job, "job_mode": p.job_mode, "code_folder": p.code_folder}

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
            if name == "search_files" and not self.policy.shared and self.policy.read_dirs:
                return search_files(args.get("glob"), args.get("pattern"), self.root, self.policy,
                                    folder=args.get("folder")), False
            if name == "write_file" and not self.policy.shared and self.policy.edit_dirs:
                return write_file(args.get("path"), args.get("content"), self.root, self.policy), False
            if name == "edit_file" and not self.policy.shared and self.policy.edit_dirs:
                return edit_file(args.get("path"), args.get("old_text"), args.get("new_text"),
                                 self.root, self.policy), False
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
