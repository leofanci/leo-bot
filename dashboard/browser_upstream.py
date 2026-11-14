"""browser_upstream.py: the agent-browser engine child behind the browser relay.

One child per (workspace, bot), started on the first call and kept in POOL. It runs
`agent-browser mcp --tools core` (core/browser_engine.mcp_argv) and speaks line-delimited
JSON-RPC over stdin and stdout; stderr is drained so a chatty child never blocks.

  bot        the bot's own headless browser. Session "<ws>--<bot>" in namespace "leobot",
             logins saved and restored under the session's own name (encrypted with
             key_file's key, kept 365 days).
  my_chrome  the owner's running Chrome, attached over its remote debugging port
             (find_chrome_debug_url, checked again on every call), each bot in its own pinned
             tab. Session "<ws>--<bot>--chrome", so it never shares a daemon with the bot
             browser. Chrome off: every call answers CHROME_OFF and nothing starts. Chrome
             quit or restarted: the session is closed and, when Chrome is back, started again.
             The engine's close leaves an attached Chrome running.

The engine reads ~/.agent-browser/config.json and ./agent-browser.json on its own, and its
environment only overrides keys that are set. So the child gets Leo Bot's own config file
(AGENT_BROWSER_CONFIG, which replaces both), headed and auto-connect set off explicitly, an
empty working folder, and an environment built from an allowlist of the server's variables:
no server-wide AGENT_BROWSER_* setting reaches it.

A call that takes longer than CALL_SECONDS stops the child; so does a crash. Either is
answered once as an MCP error and the next call starts a fresh child.

The gate (dashboard/browser_relay.py, approvals.LOCK) is in-process: this module runs only
inside the dashboard server. The server starts POOL's sweeper (idle children closed after
IDLE_SECONDS) and closes them all when it stops.
"""
import copy
import http.client
import json
import os
import pwd
import queue
import re
import secrets
import socket
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path
from urllib.parse import urlparse

from core import bots, browser, browser_engine
from dashboard import browser_relay
from dashboard.browser_relay import MAX_SESSION, session_name, session_part  # noqa: F401 - names live with the keys
from dashboard.engines.base import drain, stop_child

NAMESPACE = "leobot"
IDLE_SECONDS = 600
SWEEP_SECONDS = 60
CALL_SECONDS = 120
CLOSE_SECONDS = 15
CONNECT_SECONDS = 0.5
POLL_SECONDS = 0.25
STATE_EXPIRE_DAYS = 365
SOCKET_MAX, SOCKET_FIXED = 103, 37
PROTOCOL = "2024-11-05"
KEY_NAME = "browser-engine-key"
CONFIG_NAME = "agent-browser.json"
CWD_NAME = "engine-cwd"
# Leo Bot's engine config: replaces ~/.agent-browser/config.json and ./agent-browser.json.
CONFIG = {"headed": False, "autoConnect": False}
# Server variables the child may see. Nothing else passes: no AGENT_BROWSER_* setting, no token.
PARENT_ENV = ("PATH", "HOME", "USER", "LOGNAME", "SHELL", "TMPDIR", "TMP", "TEMP", "LANG",
              "LC_ALL", "LC_CTYPE", "TZ", "SYSTEMROOT")
_HEX_KEY = re.compile(r"[0-9a-f]{64}")
_WS_PATH = re.compile(r"/devtools/browser/[A-Za-z0-9-]+")
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
NO_KEY = ("The browser's saved-login key could not be read, so the browser did not start. Ask the owner to "
          "check the run folder in the data folder.")
NO_START = "The browser engine did not start. Try again; if it keeps failing, ask the owner to check Settings, Browser."
CRASHED = "The browser stopped during this call, so it may not have finished. The next call starts it fresh."
TIMED_OUT = ("The browser did not answer within {s} seconds and was restarted, so this call may not have "
             "finished. The next call starts it fresh.")
CLOSED = "This browser was closed after a while without use. Ask again to start it fresh."
STOPPING = "Leo Bot is stopping, so the browser did not start."
WS_CLOSING = "This workspace is being deleted, so the browser did not start."
TAB_RESET = ("This bot's tab in your Chrome was gone (Chrome restarted or the tab was closed), so a new empty "
             "tab was opened. This call was not performed. Open the page again.")
LONG_HOME = ("The home folder path is too long for the browser engine, so the browser did not start. Ask the "
             "owner to run Leo Bot from an account with a shorter home folder path.")

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



def _account_home():
    """The home the engine falls back to when HOME is unset: the account's own (passwd)."""
    try:
        return pwd.getpwuid(os.getuid()).pw_dir or ""
    except (KeyError, OSError):
        return ""


def _home(home=None):
    """HOME as the engine's socket path starts: real path, no trailing slash. HOME unset (or
    empty): the account's home, as the engine itself does."""
    home = (os.environ.get("HOME") or _account_home()) if home is None else str(home)
    return os.path.realpath(home).rstrip("/") if home else ""


def session_fits(session, home=None):
    """True when the engine's socket path fits the OS limit (103 bytes):
    <home>/.agent-browser/namespaces/<namespace>/<session> plus 37 bytes measured on 0.41.0."""
    used = SOCKET_FIXED + len(NAMESPACE.encode()) + len(_home(home).encode()) + len(session.encode())
    return used <= SOCKET_MAX



def engine_dirs(root):
    """(config file, working folder) owned by Leo Bot, written before every child start."""
    run = Path(root) / "run"
    config, cwd = run / CONFIG_NAME, run / CWD_NAME
    cwd.mkdir(parents=True, exist_ok=True)
    for stray in (cwd / CONFIG_NAME,):  # an auto-discovered project config must never exist here
        try:
            stray.unlink()
        except FileNotFoundError:
            pass
    bots._atomic_write(config, json.dumps(CONFIG, indent=2) + "\n")
    os.chmod(config, 0o600)
    return config, cwd


def _debug_path(port):
    """The WebSocket path Chrome reports on /json/version, or None when the port gives no such
    answer (another status, a reset, not HTTP: Chrome's built-in switch may not serve it)."""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=CONNECT_SECONDS)
    try:
        conn.request("GET", "/json/version")
        resp = conn.getresponse()
        if resp.status != 200:
            return None
        data = json.loads(resp.read(65536))
    except (OSError, ValueError, http.client.HTTPException):
        return None
    finally:
        conn.close()
    url = data.get("webSocketDebuggerUrl") if isinstance(data, dict) else None
    return urlparse(url).path if isinstance(url, str) else None


def find_chrome_debug_url(home=None, platform=sys.platform):
    """The WebSocket URL of a running Chrome with remote debugging on, or None. Chrome leaves
    DevToolsActivePort behind when it quits, so the port must accept a connection, and when
    /json/version names a browser it must be the file's."""
    home = Path.home() if home is None else Path(home)
    key = "darwin" if platform == "darwin" else "linux" if platform.startswith("linux") else None
    for rel in CHROME_DIRS.get(key, ()):
        try:
            lines = (home / rel / "DevToolsActivePort").read_text().splitlines()
        except (OSError, UnicodeDecodeError, ValueError):
            continue
        if len(lines) < 2:
            continue
        first, ws_path = lines[0].strip(), lines[1].strip()
        if not (first.isascii() and first.isdecimal()) or not _WS_PATH.fullmatch(ws_path):
            continue
        port = int(first)
        if not 0 < port < 65536:
            continue
        try:
            socket.create_connection(("127.0.0.1", port), timeout=CONNECT_SECONDS).close()
        except OSError:
            continue  # stale file: Chrome quit
        reported = _debug_path(port)
        if reported is not None and reported != ws_path:
            continue  # another Chrome run reuses the port: the file is stale
        return f"ws://127.0.0.1:{port}{ws_path}"
    return None


def _tab_gone(reply):
    result = reply.get("result") if isinstance(reply, dict) else None
    if not isinstance(result, dict) or not result.get("isError"):
        return False
    content = result.get("content")
    first = content[0] if isinstance(content, list) and content and isinstance(content[0], dict) else {}
    return isinstance(first.get("text"), str) and first["text"].startswith("tab_gone:")


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
        try:  # closed here, at its end: closing it from stop() blocks while a grandchild holds it
            self.proc.stdout.close()
        except (OSError, ValueError):
            pass
        self.lines.put(None)  # the child's stdout ended

    def send(self, msg):
        self.proc.stdin.write((json.dumps(msg) + "\n").encode())
        self.proc.stdin.flush()

    def _next(self, deadline):
        """The child's next message; None when it ended. A child that exited while a process it
        started still holds its stdout gives no end of stream, so its exit counts too."""
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                raise TimeoutError
            try:
                return self.lines.get(timeout=min(left, POLL_SECONDS))
            except queue.Empty:
                if self.proc.poll() is not None:
                    try:
                        return self.lines.get(timeout=POLL_SECONDS)
                    except queue.Empty:
                        return None

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
            try:
                msg = self._next(deadline)
            except TimeoutError:
                raise _Failed(TIMED_OUT.format(s=int(timeout)))
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
        try:  # stdout is left to the reader: a process the engine started may keep it open
            self.proc.stdin.close()
        except (OSError, ValueError):
            pass
        code = self.proc.returncode

        def report():  # off the caller's locks: the stream may stay open in the engine's daemon
            tail = self.stderr().strip()
            if tail and code not in (0, -15, -9, None):
                print("browser engine: " + tail.splitlines()[-1][:300], file=sys.stderr)
        threading.Thread(target=report, name="browser-engine-err", daemon=True).start()

    def kill(self):
        try:
            self.proc.kill()
        except OSError:
            pass


class BrowserUpstream(browser_relay.Upstream):
    """The engine child of one bot in one mode. Calls run one at a time. `after` is the
    upstream it replaces for the same bot: no child starts before that one has closed."""

    def __init__(self, ws, bot, mode, root, after=None, stopped=False, stopped_line=STOPPING):
        if mode not in ("bot", "my_chrome"):
            raise ValueError(f"unknown browser mode: {mode}")
        self.ws, self.bot, self.mode, self.root = ws, bot, mode, root
        self.session = session_name(ws, bot, mode)
        # One lock per real browser (all my_chrome bots share the owner's Chrome); typed text
        # per tab (each my_chrome bot has its own pinned tab).
        self.lock_key = self.browser_key = browser_relay.browser_key_for(mode, ws, bot)
        self.typed_key = browser_relay.typed_key_for(mode, ws, bot, self.session)
        self.last_used = time.time()
        self.closed = stopped
        self._closed_line = stopped_line if stopped else CLOSED
        self._after = after
        self._done = threading.Event()
        self._lock = threading.Lock()
        self._end_lock = threading.Lock()
        self._child_lock = threading.Lock()  # kill() and a child start never cross
        self._pending = None  # a child started, its handshake not done yet
        self._child = None
        self._binary = None
        self._cdp = None
        self._started = False  # a child ran since the engine session was last closed

    # -- the child --------------------------------------------------------------------------

    def _env(self, key, config, cdp=None):
        env = {k: os.environ[k] for k in PARENT_ENV if k in os.environ}
        if not env.get("HOME") and _home():  # the same home state_dir and session_fits measure
            env["HOME"] = _home()
        # No AGENT_BROWSER_AUTO_CONNECT: the engine turns auto-connect on when it is set at all,
        # even to "false" (probed on 0.41.0). Leo Bot's config file sets it off.
        env.update({"AGENT_BROWSER_CONFIG": str(config), "AGENT_BROWSER_HEADED": "false",
                    "AGENT_BROWSER_SESSION": self.session, "AGENT_BROWSER_NAMESPACE": NAMESPACE,
                    "AGENT_BROWSER_ENCRYPTION_KEY": key,
                    "AGENT_BROWSER_IDLE_TIMEOUT_MS": str(IDLE_SECONDS * 1000),
                    "AGENT_BROWSER_STATE_EXPIRE_DAYS": str(STATE_EXPIRE_DAYS)})
        if self.mode == "bot":
            env["AGENT_BROWSER_RESTORE"] = self.session  # its own saved state, never another's
        else:
            env["AGENT_BROWSER_PIN_TAB"] = "1"
            if cdp:
                env["AGENT_BROWSER_CDP"] = cdp
        return env

    def _start(self, cdp=None):
        """A running child, started with the handshake and tools/list. Raises _Failed."""
        if self._child is not None and self._child.proc.poll() is None:
            return self._child
        self._drop()
        if self._after is not None:  # the bot's previous browser must be closed first
            self._after._done.wait(CALL_SECONDS + 2 * CLOSE_SECONDS)
            self._after = None
        binary = browser_engine.resolve()
        if binary is None:
            raise _Failed(NO_ENGINE)
        key = _read_key(self.root)
        if key is None:
            raise _Failed(NO_KEY)
        if not session_fits(self.session):
            raise _Failed(LONG_HOME)
        try:
            config, cwd = engine_dirs(self.root)
        except OSError:
            raise _Failed(NO_START)
        with self._child_lock:
            if self.closed:  # shutdown came after the call's own check
                raise _Failed(self._closed_line)
            try:
                proc = subprocess.Popen(browser_engine.mcp_argv(binary), stdin=subprocess.PIPE,
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                        env=self._env(key, config, cdp), cwd=str(cwd))
            except OSError:
                raise _Failed(NO_START)
            self._binary, self._started, self._cdp = binary, True, cdp
            child = self._pending = _Child(proc)
        if self.closed:  # killed while it started: kill() saw the pending child too
            child.kill()
            child.stop()
            raise _Failed(self._closed_line)
        try:
            init = child.request("initialize", {"protocolVersion": PROTOCOL, "capabilities": {},
                                                "clientInfo": {"name": "leobot", "version": "1"}}, CALL_SECONDS)
            child.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
            listed = child.request("tools/list", {}, CALL_SECONDS)
        except (_Failed, OSError, ValueError):
            self._pending = None
            child.stop()
            raise _Failed(self._closed_line if self.closed else NO_START)
        tools = (listed.get("result") or {}).get("tools") if isinstance(listed.get("result"), dict) else None
        if "error" in init or not isinstance(tools, list):
            self._pending = None
            child.stop()
            raise _Failed(NO_START)
        with self._child_lock:
            self._pending = None
            if self.closed:
                child.kill()
                child.stop()
                raise _Failed(self._closed_line)
            self._child = child
        _TOOLS[str(binary)] = {"tools": tools}
        browser_relay.learn_schemas(self, tools)  # the relay's per-tool input allowlist
        return child

    def _drop(self):
        if self._child is not None:
            self._child.stop()
            self._child = None

    def _engine_close(self):
        """`<binary> --session <s> --namespace leobot close`, once per started session. The bot
        browser saves its logins on close; an attached Chrome keeps running. Skipped when no
        child ran or the key cannot be read (never an empty key)."""
        with self._end_lock:
            if not self._started:
                return
            self._started = False
            binary = self._binary or browser_engine.resolve()
            key = _read_key(self.root)
            if binary is None or key is None:
                return
            try:
                config, cwd = engine_dirs(self.root)
                subprocess.run([str(binary), "--session", self.session, "--namespace", NAMESPACE, "close"],
                               env=self._env(key, config), cwd=str(cwd), capture_output=True,
                               timeout=CLOSE_SECONDS)
            except (OSError, subprocess.SubprocessError):
                pass

    def _end_session(self):
        self._drop()
        self._engine_close()

    def _chrome(self):
        """my_chrome: Chrome's URL now. Gone: the session ends and CHROME_OFF. Another Chrome
        run (new port or browser id): the old session ends and a new one starts."""
        cdp = find_chrome_debug_url()
        if cdp is None:
            self._end_session()
            raise _Failed(CHROME_OFF)
        if self._cdp is not None and cdp != self._cdp:
            self._end_session()
            self._cdp = None
        return cdp

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
                    raise _Failed(self._closed_line)
                try:
                    child = self._start(self._chrome() if self.mode == "my_chrome" else None)
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
                if self.mode == "my_chrome" and method == "tools/call" and _tab_gone(reply):
                    # The engine keeps the bot's pinned tab across sessions; after Chrome restarts
                    # (or the owner closed that tab) it is gone. Bind a new empty one, act on nothing.
                    try:
                        child.request("tools/call", {"name": "agent_browser_tab_new", "arguments": {}}, CALL_SECONDS)
                    except _Failed:
                        self._drop()
                        raise
                    browser_relay.set_aside(self.typed_key, self.root)  # a new page, as after open
                    raise _Failed(TAB_RESET)
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

    def _close_locked(self):
        try:
            self.closed = True
            self._end_session()
        finally:
            self._done.set()

    def close(self):
        """Stop the child and close its engine session (the bot browser saves its logins; an
        attached Chrome keeps running). Idempotent."""
        with self._lock:
            self._close_locked()

    def close_if_idle(self, now):
        """Close when no call ran for IDLE_SECONDS, checked under this upstream's own lock; a
        call in flight leaves it open. True when it is closed now."""
        if not self._lock.acquire(blocking=False):
            return False
        try:
            if not self.closed and now - self.last_used < IDLE_SECONDS:
                return False
            self._close_locked()
            return True
        finally:
            self._lock.release()

    def kill(self):
        """Shutdown: refuse new calls and kill the child now, without waiting for a call in
        flight. The engine session is closed after (close_all)."""
        with self._child_lock:
            self.closed = True
            self._closed_line = STOPPING
            children = [c for c in (self._child, self._pending) if c is not None]
            self._child = self._pending = None
        for child in children:
            child.kill()
            child.stop()  # reaps it; a call in flight then sees it exit


def _close_session(up):
    try:
        up._engine_close()
    except Exception:  # noqa: BLE001 - a close that breaks never stops the workspace delete
        traceback.print_exc()


class Pool:
    """One BrowserUpstream per (ws, bot). The pool lock only guards the table: closing runs
    outside it, and a replacement waits for its predecessor to close before it starts."""

    def __init__(self):
        self._lock = threading.Lock()
        self._items = {}
        self._deleting = set()  # workspaces close_where is closing: no browser starts there till reopen
        self._stopping = False
        self._halt = threading.Event()

    def get(self, ws, bot, mode, root):
        old = None
        with self._lock:
            if self._stopping:
                return BrowserUpstream(ws, bot, mode, root, stopped=True)
            if ws in self._deleting:
                return BrowserUpstream(ws, bot, mode, root, stopped=True, stopped_line=WS_CLOSING)
            up = self._items.get((ws, bot))
            if up is not None and (up.mode != mode or str(up.root) != str(root) or up.closed):
                old, up = up, None
            if up is None:
                up = BrowserUpstream(ws, bot, mode, root, after=old)
                self._items[(ws, bot)] = up
        if old is not None:
            old.close()
        return up

    def close_where(self, ws, bound=CLOSE_SECONDS + 5):
        """A deleted workspace: mark it (get starts nothing there until reopen), take its children
        out of the table, kill them and close their engine sessions in parallel within `bound`
        seconds, as close_all does. Closing saves state, so forget_state runs after this.
        Returns the bot ids closed."""
        with self._lock:
            self._deleting.add(ws)
            items = [(k, up) for k, up in self._items.items() if k[0] == ws]
            for k, _ in items:
                del self._items[k]
        ups = [up for _, up in items]
        for up in ups:
            try:
                up.kill()
                up._closed_line = WS_CLOSING
            except Exception:  # noqa: BLE001 - one child that fails to stop never stops the rest
                traceback.print_exc()
        threads = [threading.Thread(target=_close_session, args=(up,), name="browser-close", daemon=True)
                   for up in ups]
        for t in threads:
            t.start()
        deadline = time.monotonic() + bound
        for t in threads:
            t.join(max(0.0, deadline - time.monotonic()))
        for up in ups:
            up._done.set()
        return [k[1] for k, _ in items]

    def reopen(self, ws):
        """After forget_state: browsers may start in this workspace again (a reused slug)."""
        with self._lock:
            self._deleting.discard(ws)

    def sweep(self, now=None):
        """Close children with no call for IDLE_SECONDS. A call in flight is left alone."""
        now = time.time() if now is None else now
        with self._lock:
            items = list(self._items.items())
        closed = 0
        for k, up in items:
            if up.close_if_idle(now):
                closed += 1
                with self._lock:
                    if self._items.get(k) is up:
                        del self._items[k]
        return closed

    def start_sweeper(self, interval=SWEEP_SECONDS):
        """A daemon thread that sweeps every `interval` seconds until close_all."""
        def loop():
            while not self._halt.wait(interval):
                try:
                    self.sweep()
                except Exception:  # noqa: BLE001 - the sweeper never takes the server down
                    traceback.print_exc()
        thread = threading.Thread(target=loop, name="browser-sweep", daemon=True)
        thread.start()
        return thread

    def close_all(self, bound=CLOSE_SECONDS):
        """Server shutdown: refuse new browsers, kill every child, then close their engine
        sessions in parallel, all within `bound` seconds."""
        with self._lock:
            self._stopping = True
            items, self._items = list(self._items.values()), {}
        self._halt.set()
        for up in items:
            up.kill()
        threads = [threading.Thread(target=up._engine_close, name="browser-close", daemon=True) for up in items]
        for t in threads:
            t.start()
        deadline = time.monotonic() + bound
        for t in threads:
            t.join(max(0.0, deadline - time.monotonic()))
        for up in items:
            up._done.set()


def state_dir(home=None):
    """Where the engine saves this namespace's session state (docs/browser.md)."""
    return Path(_home(home) or "/nonexistent") / ".agent-browser" / "namespaces" / NAMESPACE / "state" / "sessions"


def state_files(ws, bot):
    """The exact state file names one bot's sessions in `ws` can have, in both modes: the restore key
    is the session name (<key>-<session>), or the namespace when the engine picks it. Exact names,
    never a prefix: "acme" must not reach "acme-2", and a hashed name is computed, not guessed."""
    names = []
    for mode in browser.MODES:
        s = session_name(ws, bot, mode)
        for key in (s, NAMESPACE):
            names += [f"{key}-{s}.json.enc", f"{key}-{s}.json"]
    return names


_PART = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")  # one session part (a slug, or a hashed "h" + 8 hex)


def plain_state_names(ws, names):
    """Of these file names, the state files of plain (unhashed) sessions in `ws`, whichever bot:
    "<key>-<s>.json[.enc]" with <s> = "<ws part>--<bot part>[--chrome]" and <key> the session
    name or the namespace. The ws part must match exactly, so "acme" never reaches "acme-2"."""
    wsp = session_part(ws)
    found = []
    for name in names:
        stem = next((name[:-len(x)] for x in (".json.enc", ".json") if name.endswith(x)), None)
        if not stem:
            continue
        start = 0
        while True:
            i = stem.find(f"-{wsp}--", start)
            if i < 0:
                break
            key, sess = stem[:i], stem[i + 1:]
            parts = sess.split("--")
            if (key in (sess, NAMESPACE) and len(sess) <= MAX_SESSION and parts[0] == wsp
                    and (len(parts) == 2 or (len(parts) == 3 and parts[2] == "chrome"))
                    and _PART.fullmatch(parts[1])):
                found.append(name)
                break
            start = i + 1
    return found


def forget_state(ws, bot_ids, home=None):
    """A deleted workspace: remove its bots' saved logins, so a reused slug starts with none.
    Exact names only: each known bot's computed names (hashed ones too), plus any plain-named
    session of this workspace found in the folder (a bot no list knows). Returns the files removed."""
    folder = state_dir(home)
    names = []
    for bot in sorted({b for b in bot_ids if isinstance(b, str) and b}):
        names += state_files(ws, bot)
    try:
        names += plain_state_names(ws, sorted(os.listdir(folder)))
    except OSError:
        pass
    removed = []
    for name in dict.fromkeys(names):
        path = folder / name
        try:
            if path.is_symlink() or path.is_file():  # a link is removed, never followed
                path.unlink()
                removed.append(path)
        except OSError:
            traceback.print_exc()
    return removed


POOL = Pool()
