"""browser_engine.py: the pinned agent-browser engine bots drive through the browser relay.

The engine is an npm package run as a child process. Nothing here talks to it: this module
finds the binary, installs it, and names which of its MCP tools the relay may pass.

  resolve   LEOBOT_AGENT_BROWSER, else the pinned copy under the data dir, else PATH
  install   npm install of the pinned version, then the engine's own Chrome download
  mcp_argv  the stdio MCP server command, "core" tool profile only (see docs/browser.md)

Tool names come from a probe of the pinned version (docs/browser.md, "Engine facts"). Any
tool in none of the four sets is refused by the relay: eval, close, profile listing, and
every file, download, upload, cookie or storage tool.
"""
import os
import shutil
import subprocess
from pathlib import Path

from core import paths

VERSION = "0.41.0"
BINARY_NAME = "agent-browser"
ENV_VAR = "LEOBOT_AGENT_BROWSER"
INSTALL_TIMEOUT = 600
_INSTALL_HINT = "Install it from Settings, or run: npm install --prefix {dir} agent-browser@{v}"

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
    return _tool_dir() / "node_modules" / ".bin" / BINARY_NAME


def _usable(p):
    return p.is_file() and os.access(p, os.X_OK)


def resolve(env=os.environ):
    """The engine binary to run, or None. Env override, then the pinned data-dir copy, then PATH.

    A set but unusable LEOBOT_AGENT_BROWSER returns None and does not fall through."""
    given = env.get(ENV_VAR)
    if given:
        p = Path(given).expanduser()
        return p if _usable(p) else None
    pinned = _pinned_binary()
    if _usable(pinned):
        return pinned
    found = shutil.which(BINARY_NAME)
    return Path(found) if found else None


def status():
    binary = resolve()
    if binary:
        return {"state": "ready", "detail": str(binary)}
    return {"state": "missing",
            "detail": _INSTALL_HINT.format(dir=_tool_dir(), v=VERSION)}


def install():
    """Install the pinned version and its Chrome. Returns status(); detail says what failed."""
    folder = _tool_dir()
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return {"state": "missing", "detail": f"install failed: {e}"}
    for step in ("npm", "engine"):
        if step == "npm":
            argv = ["npm", "install", "--prefix", str(folder), f"{BINARY_NAME}@{VERSION}"]
        else:
            if not _usable(_pinned_binary()):
                return {"state": "missing", "detail": "npm installed but the engine binary is missing"}
            argv = [str(_pinned_binary()), "install"]
        try:
            r = subprocess.run(argv, capture_output=True, text=True, timeout=INSTALL_TIMEOUT)
        except FileNotFoundError:
            what = "npm not found. Install Node.js, then try again." if step == "npm" \
                else f"engine binary could not be launched: {argv[0]}"
            return {"state": "missing", "detail": what}
        except subprocess.TimeoutExpired:
            return {"state": "missing", "detail": f"{step} install timed out after {INSTALL_TIMEOUT}s"}
        except OSError as e:
            return {"state": "missing", "detail": f"{step} install failed: {e}"}
        if r.returncode != 0:
            tail = (r.stderr or r.stdout or "").strip().splitlines()[-3:]
            return {"state": "missing", "detail": f"{step} failed: " + " ".join(tail)}
    if not _usable(_pinned_binary()):
        return {"state": "missing", "detail": "npm installed but the engine binary is missing"}
    return status()


def mcp_argv(binary):
    """Stdio MCP server command. The "core" profile has no file, cookie, storage or network tools."""
    return [str(binary), "mcp", "--tools", "core"]
