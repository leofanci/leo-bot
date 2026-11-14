"""browser_upstream.py: the agent-browser engine child behind the browser relay.

One child per (workspace, bot), started on the first call and kept in POOL. It runs
`agent-browser mcp --tools core` (core/browser_engine.mcp_argv) and speaks line-delimited
JSON-RPC over stdin and stdout; stderr is drained so a chatty child never blocks.

  bot        the bot's own headless browser. Session "<ws>--<bot>" in namespace "leobot",
             logins saved and restored (encrypted with key_file's key, kept 365 days).
  my_chrome  the owner's running Chrome, attached over its remote debugging port
             (find_chrome_debug_url), each bot in its own pinned tab. Session
             "<ws>--<bot>--chrome", so it never shares a daemon with the bot browser.
             Leo Bot never closes this Chrome. Chrome off: every call answers CHROME_OFF
             and nothing starts.

The child's environment is built from an allowlist of the server's variables plus the ones
set here, so a server-wide AGENT_BROWSER_HEADED (or any other engine setting) never reaches
it. A call that takes longer than CALL_SECONDS stops the child; so does a crash. Either is
answered once as an MCP error and the next call starts a fresh child.

The gate (dashboard/browser_relay.py, approvals.LOCK) is in-process: this module runs only
inside the dashboard server. The server sweeps idle children every minute and closes them
all when it stops.
"""
import copy
import json
import os
import queue
import re
import secrets
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

from core import browser_engine
from dashboard import browser_relay
from dashboard.engines.base import drain, stop_child

NAMESPACE = "leobot"
IDLE_SECONDS = 600
CALL_SECONDS = 120
CLOSE_SECONDS = 15
CONNECT_SECONDS = 0.5
STATE_EXPIRE_DAYS = 365
PROTOCOL = "2024-11-05"
KEY_NAME = "browser-engine-key"
# Server variables the child may see. Nothing else passes: no AGENT_BROWSER_* setting, no token.
PARENT_ENV = ("PATH", "HOME", "USER", "LOGNAME", "SHELL", "TMPDIR", "TMP", "TEMP", "LANG",
              "LC_ALL", "LC_CTYPE", "TZ", "SYSTEMROOT")
_SLUG = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")  # no "--" and no dash at either end
_HEX_KEY = re.compile(r"[0-9a-f]{64}")
# Chrome's user data dirs that hold DevToolsActivePort (docs/browser.md, "Attach to a running Chrome").
CHROME_DIRS = {
    "darwin": ("Library/Application Support/Google/Chrome",
               "Library/Application Support/Google/Chrome Canary",
               "Library/Application Support/BraveSoftware/Brave-Browser",
               "Library/Application Support/Chromium"),
    "linux": (".config/google-chrome", ".config/chromium"),
}

CHROME_OFF = ("Chrome is not open with remote debugging on, so this bot cannot use your Chrome. Open Chrome and "
              "turn on remote debugging (Settings, Browser shows how), then ask again.")
NO_ENGINE = "The browser engine is not installed. Ask the owner to install it in Settings, Browser, then stop."
BAD_NAME = ("This workspace or bot name cannot name a browser session (only lowercase letters, digits and single "
            "dashes), so the browser did not start.")
NO_KEY = ("The browser's saved-login key could not be read, so the browser did not start. Ask the owner to "
          "check the run folder in the data folder.")
NO_START = "The browser engine did not start. Try again; if it keeps failing, ask the owner to check Settings, Browser."
CRASHED = "The browser stopped during this call, so it may not have finished. The next call starts it fresh."
TIMED_OUT = ("The browser did not answer within {s} seconds and was restarted, so this call may not have "
             "finished. The next call starts it fresh.")
CLOSED = "This browser was closed after a while without use. Ask again to start it fresh."

_KEY_LOCK = threading.Lock()
_TOOLS = {}  # engine binary -> its last tools/list result: Chrome off still lists the tools


def key_file(root):
    """<root>/run/browser-engine-key: 64 hex characters, created once with mode 0600. Losing it
    makes every saved login unreadable (back up <root>/run with the data)."""
    path = Path(root) / "run" / KEY_NAME
    with _KEY_LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            return path
        with os.fdopen(fd, "w") as f:
            f.write(secrets.token_hex(32))
    return path


def _read_key(root):
    try:
        key = key_file(root).read_text().strip()
    except OSError:
        return None
    return key if _HEX_KEY.fullmatch(key) else None


def find_chrome_debug_url(home=None, platform=sys.platform):
    """The WebSocket URL of a running Chrome with remote debugging on, or None. Chrome leaves
    DevToolsActivePort behind when it quits, so the port must also accept a connection."""
    home = Path.home() if home is None else Path(home)
    key = "darwin" if platform == "darwin" else "linux" if platform.startswith("linux") else None
    for rel in CHROME_DIRS.get(key, ()):
        try:
            lines = (home / rel / "DevToolsActivePort").read_text().splitlines()
        except (OSError, UnicodeDecodeError):
            continue
        if len(lines) < 2 or not lines[0].strip().isdigit():
            continue
        port, ws_path = int(lines[0].strip()), lines[1].strip()
        if not 0 < port < 65536 or not re.fullmatch(r"/devtools/browser/[A-Za-z0-9-]+", ws_path):
            continue
        try:
            socket.create_connection(("127.0.0.1", port), timeout=CONNECT_SECONDS).close()
        except OSError:
            continue  # stale file: Chrome quit
        return f"ws://127.0.0.1:{port}{ws_path}"
    return None


def _call_error(frame, line):
    """One MCP error for this frame: an error result for a tool call, a JSON-RPC error otherwise."""
    rid = frame.get("id") if isinstance(frame, dict) else None
    if isinstance(frame, dict) and frame.get("method") == "tools/call":
        return {"jsonrpc": "2.0", "id": rid, "result": {"content": [{"type": "text", "text": line}], "isError": True}}
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32000, "message": line}}


class _Failed(Exception):
    """The child could not start or answer: its message is the line the bot sees."""


class _Child:
    def __init__(self, proc):
        self.proc = proc
        self.lines = queue.Queue()
        self.stderr = drain(proc.stderr)
        self.next_id = 0
        threading.Thread(target=self._read, name="browser-engine-out", daemon=True).start()

    def _read(self):
        try:
            for raw in self.proc.stdout:
                try:
                    msg = json.loads(raw)
                except ValueError:
                    continue
                if isinstance(msg, dict):
                    self.lines.put(msg)
        except (OSError, ValueError):
            pass
        self.lines.put(None)  # the child's stdout ended

    def send(self, msg):
        self.proc.stdin.write((json.dumps(msg) + "\n").encode())
        self.proc.stdin.flush()

    def request(self, method, params, timeout):
        """The child's reply to one request (own id), or raises _Failed on a crash or timeout."""
        self.next_id += 1
        rid = self.next_id
        try:
            self.send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        except (OSError, ValueError):
            raise _Failed(CRASHED)
        deadline = time.monotonic() + timeout
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                raise _Failed(TIMED_OUT.format(s=int(timeout)))
            try:
                msg = self.lines.get(timeout=left)
            except queue.Empty:
                continue
            if msg is None:
                raise _Failed(CRASHED)
            if "method" in msg:  # the child asks or tells something: no request of ours
                if "id" in msg:
                    try:
                        self.send({"jsonrpc": "2.0", "id": msg["id"],
                                   "error": {"code": -32601, "message": "method not available"}})
                    except (OSError, ValueError):
                        pass
                continue
            if msg.get("id") == rid:
                return msg

    def stop(self):
        stop_child(self.proc)
        for stream in (self.proc.stdin, self.proc.stdout):
            try:
                stream.close()
            except (OSError, ValueError):
                pass
        tail = self.stderr().strip()
        if tail and self.proc.returncode not in (0, -15, None):
            print("browser engine: " + tail.splitlines()[-1][:300], file=sys.stderr)


class BrowserUpstream(browser_relay.Upstream):
    """The engine child of one bot in one mode. Calls run one at a time."""

    def __init__(self, ws, bot, mode, root):
        if mode not in ("bot", "my_chrome"):
            raise ValueError(f"unknown browser mode: {mode}")
        self.ws, self.bot, self.mode, self.root = ws, bot, mode, root
        self.browser_key = browser_relay.browser_key_for(mode, ws, bot)
        self.named = all(isinstance(p, str) and _SLUG.fullmatch(p) for p in (ws, bot))
        self.session = (f"{ws}--{bot}" if mode == "bot" else f"{ws}--{bot}--chrome") if self.named else None
        self.last_used = time.time()
        self.closed = False
        self._lock = threading.Lock()
        self._child = None
        self._binary = None

    # -- the child --------------------------------------------------------------------------

    def _env(self, key, cdp=None):
        env = {k: os.environ[k] for k in PARENT_ENV if k in os.environ}
        env.update({"AGENT_BROWSER_SESSION": self.session, "AGENT_BROWSER_NAMESPACE": NAMESPACE,
                    "AGENT_BROWSER_ENCRYPTION_KEY": key,
                    "AGENT_BROWSER_IDLE_TIMEOUT_MS": str(IDLE_SECONDS * 1000),
                    "AGENT_BROWSER_STATE_EXPIRE_DAYS": str(STATE_EXPIRE_DAYS)})
        if self.mode == "bot":
            env["AGENT_BROWSER_RESTORE"] = "1"
        else:
            env.update({"AGENT_BROWSER_CDP": cdp, "AGENT_BROWSER_PIN_TAB": "1"})
        return env

    def _start(self):
        """A running child, started with the handshake and tools/list. Raises _Failed."""
        if self._child is not None and self._child.proc.poll() is None:
            return self._child
        self._drop()
        if not self.named:
            raise _Failed(BAD_NAME)
        binary = browser_engine.resolve()
        if binary is None:
            raise _Failed(NO_ENGINE)
        cdp = None
        if self.mode == "my_chrome":
            cdp = find_chrome_debug_url()
            if cdp is None:
                raise _Failed(CHROME_OFF)
        key = _read_key(self.root)
        if key is None:
            raise _Failed(NO_KEY)
        try:
            proc = subprocess.Popen(browser_engine.mcp_argv(binary), stdin=subprocess.PIPE,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    env=self._env(key, cdp), cwd=str(Path(self.root)))
        except OSError:
            raise _Failed(NO_START)
        child = _Child(proc)
        try:
            init = child.request("initialize", {"protocolVersion": PROTOCOL, "capabilities": {},
                                                "clientInfo": {"name": "leobot", "version": "1"}}, CALL_SECONDS)
            child.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
            listed = child.request("tools/list", {}, CALL_SECONDS)
        except (_Failed, OSError, ValueError):
            child.stop()
            raise _Failed(NO_START)
        tools = (listed.get("result") or {}).get("tools") if isinstance(listed.get("result"), dict) else None
        if "error" in init or not isinstance(tools, list):
            child.stop()
            raise _Failed(NO_START)
        _TOOLS[str(binary)] = {"tools": tools}
        browser_relay.learn_schemas(self, tools)  # the relay's per-tool input allowlist
        self._child, self._binary = child, binary
        return child

    def _drop(self):
        if self._child is not None:
            self._child.stop()
            self._child = None

    def _cached_tools(self):
        binary = self._binary or browser_engine.resolve()
        listed = _TOOLS.get(str(binary)) if binary else None
        return copy.deepcopy(listed) if listed else None

    # -- the Upstream interface ---------------------------------------------------------------

    def forward(self, frame, mcp_session_id):
        if not isinstance(frame, dict):
            return _call_error({}, "invalid request"), mcp_session_id
        method = frame.get("method")
        if isinstance(method, str) and method.startswith("notifications/"):
            return None, mcp_session_id  # the handshake is this upstream's own; ids are rewritten
        rid = frame.get("id")
        if method == "initialize":
            params = frame.get("params") if isinstance(frame.get("params"), dict) else {}
            version = params.get("protocolVersion")
            return {"jsonrpc": "2.0", "id": rid, "result": {
                "protocolVersion": version if isinstance(version, str) and version else PROTOCOL,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "agent-browser", "version": browser_engine.VERSION}}}, mcp_session_id
        if method == "ping":
            return {"jsonrpc": "2.0", "id": rid, "result": {}}, mcp_session_id
        with self._lock:
            self.last_used = time.time()
            try:
                if self.closed:
                    raise _Failed(CLOSED)
                try:
                    child = self._start()
                except _Failed as e:
                    listed = self._cached_tools() if method == "tools/list" else None
                    if listed is not None:  # the tools are known: the call then says what is wrong
                        return {"jsonrpc": "2.0", "id": rid, "result": listed}, mcp_session_id
                    raise e
                params = frame.get("params") if isinstance(frame.get("params"), dict) else {}
                try:
                    reply = child.request(method, params, CALL_SECONDS)
                except _Failed:
                    self._drop()  # crash or timeout: stopped now, the next call starts fresh
                    raise
                reply = dict(reply)
                reply["id"] = rid
                return reply, mcp_session_id
            except _Failed as e:
                return _call_error(frame, str(e)), mcp_session_id
            finally:
                self.last_used = time.time()

    def current_url(self):
        """The active tab's URL from the engine's get_url, or None (no child, error, odd reply)."""
        with self._lock:
            child = self._child
            if self.closed or child is None or child.proc.poll() is not None:
                return None
            try:
                reply = child.request("tools/call", {"name": "agent_browser_get_url", "arguments": {}}, CALL_SECONDS)
            except _Failed:
                self._drop()
                return None
            self.last_used = time.time()
        result = reply.get("result")
        if not isinstance(result, dict) or result.get("isError"):
            return None
        content = result.get("content")
        if not isinstance(content, list) or not content or not isinstance(content[0], dict):
            return None
        text = content[0].get("text")
        return text.strip() if isinstance(text, str) and text.strip() else None

    def close(self):
        """Stop the child. The bot browser's engine session is closed too (its logins are saved
        on close); the owner's Chrome is never closed."""
        with self._lock:
            self.closed = True
            self._drop()
            binary = self._binary or browser_engine.resolve()
            if self.mode != "bot" or not self.named or binary is None:
                return
            key = _read_key(self.root) or ""
            try:
                subprocess.run([str(binary), "--session", self.session, "--namespace", NAMESPACE, "close"],
                               env=self._env(key), cwd=str(Path(self.root)), capture_output=True,
                               timeout=CLOSE_SECONDS)
            except (OSError, subprocess.SubprocessError):
                pass

    def busy(self):
        return self._lock.locked()


class Pool:
    """One BrowserUpstream per (ws, bot). get holds the pool lock, so two turns never make two."""

    def __init__(self):
        self._lock = threading.Lock()
        self._items = {}

    def get(self, ws, bot, mode, root):
        with self._lock:
            up = self._items.get((ws, bot))
            if up is not None and (up.mode != mode or str(up.root) != str(root) or up.closed):
                up.close()  # under the lock: the old session ends before a new one can start
                up = None
            if up is None:
                up = BrowserUpstream(ws, bot, mode, root)
                self._items[(ws, bot)] = up
            return up

    def sweep(self, now=None):
        """Close children with no call for IDLE_SECONDS. A call in flight is left alone."""
        now = time.time() if now is None else now
        with self._lock:
            idle = [(k, up) for k, up in self._items.items()
                    if now - up.last_used >= IDLE_SECONDS and not up.busy()]
            for k, up in idle:  # under the lock: a session ends before get can start it again
                del self._items[k]
                up.close()
        return len(idle)

    def close_all(self):
        with self._lock:
            items, self._items = list(self._items.values()), {}
        for up in items:
            try:
                up.close()
            except Exception:  # noqa: BLE001 - shutdown closes every other one
                pass


POOL = Pool()
