"""browser_engine.py: the pinned agent-browser engine bots drive through the browser relay.

The engine is a native program run as a child process. Nothing here talks to it: this module
finds the binary, installs it, and names which of its MCP tools the relay may pass.

  resolve   LEOBOT_AGENT_BROWSER, else the pinned copy under the data dir, else PATH
  install   download of the pinned release binary for this computer, checked against SHA256,
            then the engine's own Chrome download. No Node or npm needed.
  mcp_argv  the stdio MCP server command, "core" tool profile only (see docs/browser.md)

Tool names come from a probe of the pinned version (docs/browser.md, "Engine facts"). Any
tool in none of the four sets is refused by the relay: eval, close, profile listing, and
every file, download, upload, cookie or storage tool.
"""
import hashlib
import os
import platform
import re
import shutil
import subprocess
import tempfile
import urllib.request
from pathlib import Path

from core import paths

VERSION = "0.41.0"
BINARY_NAME = "agent-browser"
ENV_VAR = "LEOBOT_AGENT_BROWSER"
INSTALL_TIMEOUT = 600
_INSTALL_HINT = "Install it from Settings, Browser (a {v} download into {dir})."
RELEASE_URL = "https://github.com/vercel-labs/agent-browser/releases/download/v{v}/{asset}"
# SHA-256 of each release binary of VERSION. Taken from the npm package of VERSION (its registry
# integrity checked) and equal to the GitHub release copies. A download that differs is refused.
# Changing VERSION means taking these again.
SHA256 = {
    "agent-browser-darwin-arm64": "a402fef2489c221b17522aa21f103ca5dc47f00ff5879d4a72c39881544730ab",
    "agent-browser-darwin-x64": "f108d4aa02f41eee05d6b1b05817475fd5d576d802826783ef80800da16f33e2",
    "agent-browser-linux-arm64": "e196e9cabe6cb1b01bd5912973628361b9f40a332683a12426abad007f0d3589",
    "agent-browser-linux-musl-arm64": "77d0dd11dd5e2e9b30e4987603960c2a3f9b4794af782292259a90f3cc1010a1",
    "agent-browser-linux-musl-x64": "9585897e9453517683a4ca42f3c3f8d8376282f3160268d911ed219b2e0ad62a",
    "agent-browser-linux-x64": "399c7e9f06bd29aa933fdf25c0bd958eea06927fc0ce6f536ba6347aabd64628",
    "agent-browser-win32-x64.exe": "dee86b78071cbe7217f71ec1c9df6a50efa73cd44d64a18877bb36556fc8e270",
}
MAX_DOWNLOAD = 64 * 1024 * 1024  # the release binaries are 16 to 19 MB
DOWNLOAD_TIMEOUT = 60  # seconds without data before a download gives up

_P = "agent_browser_"
# Reads that change nothing on the page. screenshot writes an image file, and its path and
# screenshotDir inputs are caller-controlled: the relay must strip them.
READ_TOOLS = frozenset(_P + n for n in (
    "read", "snapshot", "screenshot", "scroll", "get_text", "get_url", "get_title",
    "wait_ms", "wait_for_selector", "wait_for_text", "wait_for_load"))
# Put text into a field. Nothing leaves the page until a commit tool runs.
TYPE_TOOLS = frozenset(_P + n for n in ("fill", "type"))
# Anything that can submit, send, buy or toggle. After a type tool these wait on approval.
COMMIT_TOOLS = frozenset(_P + n for n in ("click", "press", "select", "check", "uncheck"))
# Moves between pages or tabs.
NAV_TOOLS = frozenset(_P + n for n in (
    "open", "back", "forward", "reload", "tab_new", "tab_list", "tab_switch", "tab_close"))


def _tool_dir():
    return paths.DATA / "tools" / BINARY_NAME / VERSION


def _pinned_binary():
    """The downloaded release binary."""
    return _tool_dir() / (BINARY_NAME + (".exe" if os.name == "nt" else ""))


def _npm_binary():
    """Where an older Leo Bot put the engine with npm: still used when present."""
    return _tool_dir() / "node_modules" / ".bin" / BINARY_NAME


def _is_musl():
    return any(Path(p).exists() for p in ("/lib/ld-musl-x86_64.so.1", "/lib/ld-musl-aarch64.so.1"))


def asset_name(system=None, machine=None, musl=None):
    """The release binary for this computer, or None when the engine ships none for it.
    Windows on ARM runs the x64 build, as the engine's own installer does."""
    system = (system or platform.system()).lower()
    arch = {"x86_64": "x64", "amd64": "x64", "x64": "x64",
            "arm64": "arm64", "aarch64": "arm64"}.get((machine or platform.machine()).lower())
    if arch is None:
        return None
    if system == "darwin":
        name = f"agent-browser-darwin-{arch}"
    elif system == "linux":
        name = f"agent-browser-{'linux-musl' if (_is_musl() if musl is None else musl) else 'linux'}-{arch}"
    elif system == "windows":
        name = "agent-browser-win32-x64.exe"
    else:
        return None
    return name if name in SHA256 else None


def download(asset, dest, opener=urllib.request.urlopen):
    """Fetch the pinned release `asset` to `dest`, executable. Raises OSError on a network or
    disk failure and ValueError when the bytes are not the pinned ones (nothing is kept then)."""
    url = RELEASE_URL.format(v=VERSION, asset=asset)
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".download-", dir=dest.parent)
    try:
        digest, size = hashlib.sha256(), 0
        with os.fdopen(fd, "wb") as out, opener(url, timeout=DOWNLOAD_TIMEOUT) as resp:
            while True:
                chunk = resp.read(1 << 16)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_DOWNLOAD:
                    raise ValueError("the download is larger than any engine release")
                digest.update(chunk)
                out.write(chunk)
        if digest.hexdigest() != SHA256[asset]:
            raise ValueError("the download did not match the expected checksum")
        os.chmod(tmp, 0o755)
        os.replace(tmp, dest)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _usable(p):
    return p.is_file() and os.access(p, os.X_OK)


VERSION_SECONDS = 10
_VERSIONS = {}  # (path, mtime_ns, size) -> the version that binary reports, or None


def version_of(p):
    """The x.y.z an engine binary reports for --version, or None (no answer, other output).
    Asked once per binary file: a new file at the same path is asked again."""
    try:
        st = p.stat()
    except OSError:
        return None
    key = (str(p), st.st_mtime_ns, st.st_size)
    if key not in _VERSIONS:
        try:
            r = subprocess.run([str(p), "--version"], capture_output=True, text=True, timeout=VERSION_SECONDS)
            m = re.search(r"\b(\d+\.\d+\.\d+)\b", r.stdout or "") if r.returncode == 0 else None
        except (OSError, subprocess.SubprocessError):
            m = None
        _VERSIONS[key] = m.group(1) if m else None
    return _VERSIONS[key]


def _on_path():
    found = shutil.which(BINARY_NAME)
    return Path(found) if found else None


def resolve(env=os.environ):
    """The engine binary to run, or None. Env override, then the pinned data-dir copy, then PATH.

    A set but unusable LEOBOT_AGENT_BROWSER returns None and does not fall through. A binary on
    PATH is used only when it reports VERSION: the relay and login transfer rely on facts
    probed on that version (tool names, eval reply shape, state file layout)."""
    given = env.get(ENV_VAR)
    if given:
        p = Path(given).expanduser()
        return p if _usable(p) else None
    for pinned in (_pinned_binary(), _npm_binary()):
        if _usable(pinned):
            return pinned
    found = _on_path()
    return found if found and version_of(found) == VERSION else None


def status():
    binary = resolve()
    if binary:
        return {"state": "ready", "detail": str(binary)}
    hint = _INSTALL_HINT.format(dir=_tool_dir(), v=VERSION)
    found = None if os.environ.get(ENV_VAR) else _on_path()
    if found:
        other = version_of(found) or "unknown"
        hint = f"{found} is version {other}, and Leo Bot needs {VERSION}. " + hint
    return {"state": "missing", "detail": hint}


def install(opener=urllib.request.urlopen):
    """Download the pinned binary for this computer, then its Chrome. Returns status(); detail
    says what failed."""
    asset = asset_name()
    if asset is None:
        return {"state": "missing",
                "detail": f"agent-browser has no build for this computer ({platform.system()} {platform.machine()})"}
    binary = _pinned_binary()
    if not (_usable(binary) and version_of(binary) == VERSION):
        try:
            download(asset, binary, opener)
        except ValueError as e:
            return {"state": "missing", "detail": f"download failed: {e}"}
        except OSError as e:
            return {"state": "missing", "detail": f"download failed: {e}. Check the internet connection and try again."}
    try:
        r = subprocess.run([str(binary), "install"], capture_output=True, text=True, timeout=INSTALL_TIMEOUT)
    except subprocess.TimeoutExpired:
        return {"state": "missing", "detail": f"Chrome install timed out after {INSTALL_TIMEOUT}s"}
    except OSError as e:
        return {"state": "missing", "detail": f"engine binary could not be launched: {e}"}
    if r.returncode != 0:
        tail = (r.stderr or r.stdout or "").strip().splitlines()[-3:]
        return {"state": "missing", "detail": "Chrome install failed: " + " ".join(tail)}
    if not _usable(binary):
        return {"state": "missing", "detail": "the engine binary is missing after install"}
    return status()


def mcp_argv(binary):
    """Stdio MCP server command. The "core" profile has no file, cookie, storage or network tools."""
    return [str(binary), "mcp", "--tools", "core"]
