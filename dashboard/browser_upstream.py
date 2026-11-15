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

A first login (Settings, Browser, "Log in"): the owner gets the bot's bot-mode session in a
headed window (Pool.begin_login, open_login). The bot's child is closed first and every call of
that bot in bot mode answers LOGGING_IN until the session's daemon is gone (Done, the engine's
idle timeout, or LOGIN_SECONDS), so the bot never sees the login page.

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
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from pathlib import Path
from urllib.parse import urlparse

from core import bots, browser, browser_engine
from dashboard import browser_relay
from dashboard.browser_relay import MAX_SESSION, parse_session, session_name, session_part  # noqa: F401 - names live with the keys
from dashboard.engines.base import drain, stop_child

NAMESPACE = "leobot"
IDLE_SECONDS = 600
SWEEP_SECONDS = 60
CALL_SECONDS = 120
CLOSE_SECONDS = 15
CONNECT_SECONDS = 0.5
POLL_SECONDS = 0.25
STATE_EXPIRE_DAYS = 365
DAEMON_WAIT = 5  # seconds a signalled session daemon or its Chrome gets to exit
SETTLE_POLL = 5  # seconds between background checks of a deleted workspace's daemons
TIDY_SECONDS = 900  # how long a delete's background tidy keeps retrying a daemon that lives on
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
WS_DELETED = "This workspace was deleted, so its browser is closed."
LOGGING_IN = ("The owner is logging in to a site in this browser right now, so it is not available. Try again "
              "in a few minutes.")
LOGIN_SECONDS = 1800  # a login window still open after this long is closed (its logins are saved)
LOGIN_POLL = 1  # seconds between checks that the login window's session is still alive
LOGIN_RETRY_MAX = 60  # longest wait between closes of a login window that outlived LOGIN_SECONDS
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


def _engine_env(session, mode, key, config, cdp=None):
    """The child's environment (see the module docstring): an allowlist of the server's own."""
    env = {k: os.environ[k] for k in PARENT_ENV if k in os.environ}
    if not env.get("HOME") and _home():  # the same home state_dir and session_fits measure
        env["HOME"] = _home()
    # No AGENT_BROWSER_AUTO_CONNECT: the engine turns auto-connect on when it is set at all,
    # even to "false" (probed on 0.41.0). Leo Bot's config file sets it off.
    env.update({"AGENT_BROWSER_CONFIG": str(config), "AGENT_BROWSER_HEADED": "false",
                "AGENT_BROWSER_SESSION": session, "AGENT_BROWSER_NAMESPACE": NAMESPACE,
                "AGENT_BROWSER_ENCRYPTION_KEY": key,
                "AGENT_BROWSER_IDLE_TIMEOUT_MS": str(IDLE_SECONDS * 1000),
                "AGENT_BROWSER_STATE_EXPIRE_DAYS": str(STATE_EXPIRE_DAYS)})
    if mode == "bot":
        env["AGENT_BROWSER_RESTORE"] = session  # its own saved state, never another's
    else:
        env["AGENT_BROWSER_PIN_TAB"] = "1"
        if cdp:
            env["AGENT_BROWSER_CDP"] = cdp
    return env


class BrowserUpstream(browser_relay.Upstream):
    """The engine child of one bot in one mode. Calls run one at a time. `after` is the
    upstream it replaces for the same bot: no child starts before that one has closed."""

    def __init__(self, ws, bot, mode, root, after=None, stopped=False, stopped_line=STOPPING, epoch=None):
        if mode not in ("bot", "my_chrome"):
            raise ValueError(f"unknown browser mode: {mode}")
        self.ws, self.bot, self.mode, self.root = ws, bot, mode, root
        # The workspace's epoch is part of the name: a recreated slug never reaches this session.
        # A stopped upstream starts nothing, so it never needs (or makes) one.
        self.epoch = epoch or browser.epochs(root).get(ws) or (None if stopped else browser.epoch(ws, root))
        self.session = session_name(ws, bot, mode, self.epoch)
        # One lock per real browser (all my_chrome bots share the owner's Chrome); typed text
        # per tab (each my_chrome bot has its own pinned tab).
        self.lock_key = self.browser_key = browser_relay.browser_key_for(mode, ws, bot, self.session)
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
        return _engine_env(self.session, self.mode, key, config, cdp)

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

    def kill(self, line=STOPPING):
        """Shutdown: refuse new calls and kill the child now, without waiting for a call in
        flight. The engine session is closed after (close_all). `line` is what later calls are
        told, set before the child lock is released."""
        with self._child_lock:
            self.closed = True
            self._closed_line = line
            children = [c for c in (self._child, self._pending) if c is not None]
            self._child = self._pending = None
        for child in children:
            child.kill()
            child.stop()  # reaps it; a call in flight then sees it exit


def _clear_if_empty(epoch, root, home=None):
    """A retired epoch with no file left on disk: the start sweep need not look for it again."""
    if not any(parse_session(s)[1] == epoch for s in found_sessions(home)):
        browser.clear_retired([epoch], root)


def _close_session(up):
    try:
        up._engine_close()
    except Exception:  # noqa: BLE001 - a close that breaks never stops the workspace delete
        traceback.print_exc()


class _Login:
    """One owner login window: its own token, and an event that stops its watcher."""

    def __init__(self, ws, bot, epoch, root):
        self.ws, self.bot, self.epoch, self.root = ws, bot, epoch, root
        self.token = secrets.token_hex(8)
        self.stop = threading.Event()


class Pool:
    """One BrowserUpstream per (ws, bot). The pool lock only guards the table: closing runs
    outside it, and a replacement waits for its predecessor to close before it starts."""

    def __init__(self):
        self._lock = threading.Lock()
        self._items = {}
        self._logins = {}  # bot-mode session name -> _Login: an owner's login window is open
        self._stopping = False
        self._halt = threading.Event()

    def get(self, ws, bot, mode, root):
        old = None
        # Read on every frame, never made here (a turn's mint makes it): a recreated slug has a
        # new one, and a deleted workspace has none, so a late frame starts nothing.
        e = browser.epochs(root).get(ws)
        if e is None:
            return BrowserUpstream(ws, bot, mode, root, stopped=True, stopped_line=WS_DELETED)
        with self._lock:
            if self._stopping:
                return BrowserUpstream(ws, bot, mode, root, stopped=True, epoch=e)
            if mode == "bot" and session_name(ws, bot, "bot", e) in self._logins:
                # The owner's login window drives this session: the bot never sees that page.
                return BrowserUpstream(ws, bot, mode, root, stopped=True, stopped_line=LOGGING_IN, epoch=e)
            up = self._items.get((ws, bot))
            if up is not None and (up.mode != mode or str(up.root) != str(root) or up.closed or up.epoch != e):
                old, up = up, None
            if up is None:
                up = BrowserUpstream(ws, bot, mode, root, after=old, epoch=e)
                self._items[(ws, bot)] = up
        if old is not None:
            old.close()
        return up

    def kill_where(self, ws, epoch):
        """A deleted workspace: take its children of that epoch out of the table and kill them now
        (no wait for a call in flight). A child of another epoch is left alone. Returns them."""
        with self._lock:
            items = [(k, up) for k, up in self._items.items() if k[0] == ws and up.epoch == epoch]
            for k, _ in items:
                del self._items[k]
            for sess in [s for s, x in self._logins.items() if x.ws == ws and x.epoch == epoch]:
                self._logins.pop(sess).stop.set()  # its window's daemon is settled by the delete tidy
        ups = [up for _, up in items]
        for up in ups:
            try:
                up.kill(WS_DELETED)
            except Exception:  # noqa: BLE001 - one child that fails to stop never stops the rest
                traceback.print_exc()
        return ups

    def close_where(self, ws, epoch, bound=CLOSE_SECONDS + 5, ups=None):
        """kill_where (unless `ups` were killed already), then close their engine sessions in
        parallel within `bound` seconds, as close_all does. Returns the closed upstreams."""
        ups = self.kill_where(ws, epoch) if ups is None else ups
        _parallel([lambda up=up: _close_session(up) for up in ups], bound)
        for up in ups:
            up._done.set()
        return ups

    def tidy_workspace(self, ws, epoch, bot_ids, root, home=None, bound=CLOSE_SECONDS + 5,
                       poll=SETTLE_POLL, limit=TIDY_SECONDS, ups=None):
        """Best-effort tidying after a workspace delete, of its old epoch's sessions only: close
        its children, forget their state, close or kill any daemon still alive (confirmed pids
        only), forget again. A daemon not gone yet is retried in the background for up to `limit`
        seconds (the startup sweep catches what is left). It never touches another epoch's names,
        so a recreated slug is never reached. True when everything is gone now."""
        if not (isinstance(epoch, str) and browser.EPOCH_RE.fullmatch(epoch)):
            return True  # no epoch: this workspace never browsed
        self.close_where(ws, epoch, bound, ups)
        sessions = sessions_of(ws, epoch, bot_ids, home)
        forget_sessions(sessions, home)
        browser_relay.forget_typed(sessions, root)
        if settle_sessions(sessions, root, home, bound):
            forget_sessions(sessions, home)
            forget_run_files(sessions, home)
            _clear_if_empty(epoch, root, home)
            return True

        def finish():
            deadline = time.monotonic() + limit
            while time.monotonic() < deadline and not self._halt.wait(poll):
                try:
                    if settle_sessions(sessions, root, home, bound):
                        forget_sessions(sessions, home)
                        forget_run_files(sessions, home)
                        _clear_if_empty(epoch, root, home)
                        return
                except Exception:  # noqa: BLE001 - keep trying until the limit
                    traceback.print_exc()
        threading.Thread(target=finish, name="browser-tidy", daemon=True).start()
        return False

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

    # -- the owner's login window --------------------------------------------------------------

    def begin_login(self, ws, bot, epoch, root):
        """Hand this bot's bot-mode session to the owner for a login window. Cheap (no process
        runs), so it may run under browser.LOCK. Returns (session, the bot's upstream to close,
        this login's token), or None when a login window is open for it already or the server is
        stopping. From now on every call of the bot in bot mode answers LOGGING_IN, until this
        login ends. Only its own token ends it: a later login of the same session has another."""
        sess = session_name(ws, bot, "bot", epoch)
        with self._lock:
            if self._stopping or sess in self._logins:
                return None
            login = _Login(ws, bot, epoch, root)
            self._logins[sess] = login
            up = self._items.pop((ws, bot), None)
        return sess, up, login.token

    def end_login(self, sess, token):
        """End this login (by its token) and stop its watcher. A newer login of the session stays."""
        with self._lock:
            login = self._logins.get(sess)
            if login is None or login.token != token:
                return
            del self._logins[sess]
        login.stop.set()

    def _login(self, sess, token):
        with self._lock:
            login = self._logins.get(sess)
            return login if login is not None and login.token == token else None

    def logins(self):
        """[(ws, bot)] with a login window open now."""
        with self._lock:
            return sorted((x.ws, x.bot) for x in self._logins.values())

    def login_session(self, ws, bot):
        """(session, token) of this bot's open login window, or None."""
        with self._lock:
            return next(((s, x.token) for s, x in self._logins.items() if (x.ws, x.bot) == (ws, bot)), None)

    def open_login(self, sess, token, up, url, root, home=None):
        """Close the bot's own child (`up`, from begin_login) and any daemon left of its session,
        then run `<binary> --session <s> --namespace leobot open <url>` headed, with the same
        environment as the bot's own browser (its saved logins restored, saved again on close)
        and the engine's idle timeout raised to LOGIN_SECONDS. A watcher ends the login once that
        session's daemon is gone. Raises _Failed with the line the owner sees; the login is
        ended then."""
        try:
            if up is not None:
                up.kill(LOGGING_IN)
                _close_session(up)
                up._done.set()
            if not _settle_one(sess, "bot", root, home):
                raise _Failed("This bot's browser is still closing. Try again in a minute.")
            binary = browser_engine.resolve()
            if binary is None:
                raise _Failed("The browser engine is not installed. Install it first.")
            key = _read_key(root)
            if key is None:
                raise _Failed(NO_KEY)
            if not session_fits(sess, home):
                raise _Failed(LONG_HOME)
            config, cwd = engine_dirs(root)
            env = _engine_env(sess, "bot", key, config)
            env["AGENT_BROWSER_HEADED"] = "true"  # the owner sees the window
            # A person logging in sends the engine no commands: its idle timer must outlast them.
            env["AGENT_BROWSER_IDLE_TIMEOUT_MS"] = str(LOGIN_SECONDS * 1000)
            # Files, not pipes: the session daemon this starts may keep its output open.
            with tempfile.TemporaryFile() as out:
                r = subprocess.run([str(binary), "--session", sess, "--namespace", NAMESPACE, "open", url],
                                   env=env, cwd=str(cwd), stdin=subprocess.DEVNULL, stdout=out,
                                   stderr=subprocess.STDOUT, timeout=CALL_SECONDS)
                if r.returncode != 0:
                    out.seek(0)
                    tail = out.read(4096).decode("utf-8", "replace").strip().splitlines()[-1:]
                    raise _Failed("The login window did not open." + (f" The engine said: {tail[0][:200]}" if tail else ""))
        except (_Failed, OSError, subprocess.SubprocessError) as e:
            engine_close_session(sess, "bot", root)
            self.end_login(sess, token)
            if isinstance(e, _Failed):
                raise
            raise _Failed("The login window did not open.") from None
        login = self._login(sess, token)
        if login is None:  # ended meanwhile (workspace deleted, server stopping)
            return
        threading.Thread(target=self._watch_login, args=(sess, login, root, home), name="browser-login",
                         daemon=True).start()

    def _watch_login(self, sess, login, root, home=None, limit=None, poll=None):
        """This login is over when its session daemon is gone (Done, or the engine's idle
        timeout closed it), or after `limit` seconds, when it is closed here. It stops as soon as
        its own login ends (Done, a delete, shutdown) and never ends a newer login's token. A
        close that fails past the deadline is retried with a growing wait, and logged."""
        limit = LOGIN_SECONDS if limit is None else limit
        wait = LOGIN_POLL if poll is None else poll
        deadline = time.monotonic() + limit
        try:
            while not login.stop.wait(wait) and not self._halt.is_set():
                if not daemon_alive(sess, home):
                    break
                if time.monotonic() > deadline:
                    if _settle_one(sess, "bot", root, home):
                        break
                    wait = min(max(wait * 2, LOGIN_POLL), LOGIN_RETRY_MAX)  # still alive: the bot stays refused
                    print(f"browser login: session {sess} did not close after {int(limit)}s; "
                          f"retrying in {int(wait)}s", file=sys.stderr)
        except Exception:  # noqa: BLE001 - never leave the bot refused for good
            traceback.print_exc()
        self.end_login(sess, login.token)

    def finish_login(self, ws, bot, root, home=None):
        """Done: close the login window (the engine saves its logins). True when it is gone."""
        found = self.login_session(ws, bot)
        return True if found is None else self.close_login(found[0], root, found[1], home)

    def close_login(self, sess, root, token, home=None):
        """Close this login window's session, by name; end that login (its token only) and stop
        its watcher. True when the session is gone."""
        if not _settle_one(sess, "bot", root, home):
            return False
        self.end_login(sess, token)
        return True

    def close_stale_daemons(self, root, home=None, bound=CLOSE_SECONDS + 2 * DAEMON_WAIT + 5):
        """Server start: a daemon of one of this root's live epochs that no child or login of
        this pool owns was left by a stop that did not finish (a crash, maybe mid-login with a
        headed window). Close it (engine close, which saves its logins; then the confirmed-pid
        kill), so a bot never attaches to it. Other roots' epochs and names are never touched.
        Returns the sessions handled."""
        live = set(browser.epochs(root).values())
        with self._lock:
            owned = {up.session for up in self._items.values()} | set(self._logins)
        stale = {sess: mode for sess, mode in found_sessions(home).items()
                 if parse_session(sess)[1] in live and sess not in owned and daemon_alive(sess, home)}
        if stale:
            settle_sessions(stale, root, home, bound)
            forget_run_files(stale, home)
        return stale

    def close_all(self, bound=CLOSE_SECONDS):
        """Server shutdown: refuse new browsers, kill every child, then close their engine
        sessions (and any login window) in parallel, all within `bound` seconds."""
        with self._lock:
            self._stopping = True
            items, self._items = list(self._items.values()), {}
            logins, self._logins = list(self._logins.items()), {}
        self._halt.set()
        for _, login in logins:
            login.stop.set()
        for up in items:
            up.kill()
        threads = [threading.Thread(target=up._engine_close, name="browser-close", daemon=True) for up in items]
        threads += [threading.Thread(target=engine_close_session, args=(sess, "bot", login.root),
                                     name="browser-close", daemon=True) for sess, login in logins]
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


def run_dir(home=None):
    """Where a live session keeps its socket and pid file (docs/browser.md, Session daemons)."""
    return Path(_home(home) or "/nonexistent") / ".agent-browser" / "namespaces" / NAMESPACE / "run"


def state_names(session):
    """The exact state file names a session can have: the restore key is the session name
    (<key>-<session>), or the namespace when the engine picks it."""
    return [f"{key}-{session}{ext}" for key in (session, NAMESPACE) for ext in (".json.enc", ".json")]


def state_files(ws, bot, epoch=None):
    """The state file names of one bot's sessions in `ws` and that epoch, both modes."""
    return [n for mode in browser.MODES for n in state_names(session_name(ws, bot, mode, epoch))]


def _session_of_state(name):
    """The session a state file name belongs to, or None."""
    stem = next((name[:-len(x)] for x in (".json.enc", ".json") if name.endswith(x)), None)
    if not stem:
        return None
    rest = stem[len(NAMESPACE) + 1:] if stem.startswith(NAMESPACE + "-") else None
    if rest and parse_session(rest):
        return rest
    half = (len(stem) - 1) // 2
    if len(stem) % 2 == 1 and stem[half] == "-" and stem[:half] == stem[half + 1:]:
        return stem[:half]
    return None


RUN_EXTS = ("pid", "sock", "config", "engine", "stream", "target", "version")


def _session_of_run(name):
    sess, _, ext = name.rpartition(".")
    return sess if sess and ext in RUN_EXTS else None


def _listed(folder):
    try:
        return sorted(os.listdir(folder))
    except OSError:
        return []


def found_sessions(home=None):
    """{session name: mode} of every session with an epoch that has a state file or a run file."""
    out = {}
    for sess in [_session_of_state(n) for n in _listed(state_dir(home))] + \
            [_session_of_run(n) for n in _listed(run_dir(home))]:
        if sess and parse_session(sess):
            out[sess] = parse_session(sess)[2]
    return out


def sessions_of(ws, epoch, bot_ids, home=None):
    """{session name: mode} of `ws` in that epoch: each known bot's names in both modes, plus any
    plain-named session of that exact ws part and epoch found on disk (a bot no list knows).
    Exact matches only: "acme" never reaches "acme-2", and another epoch is never listed."""
    out = {}
    for bot in {b for b in bot_ids if isinstance(b, str) and b}:
        for mode in browser.MODES:
            out[session_name(ws, bot, mode, epoch)] = mode
    wsp = session_part(ws)
    for sess, mode in found_sessions(home).items():
        parsed = parse_session(sess)
        if parsed[0] == wsp and parsed[1] == epoch:
            out.setdefault(sess, mode)
    return out


def forget_sessions(sessions, home=None):
    """Remove the saved state of these sessions by exact name (a link is removed, never
    followed). Returns the files removed."""
    folder = state_dir(home)
    removed = []
    for sess in sorted(sessions):
        for name in state_names(sess):
            path = folder / name
            try:
                if path.is_symlink() or path.is_file():
                    path.unlink()
                    removed.append(path)
            except OSError:
                traceback.print_exc()
    return removed


def forget_run_files(sessions, home=None):
    """Remove what dead sessions left in the run folder (a .target stays after a close; a killed
    daemon leaves its .pid and .sock). A session whose daemon still lives is left alone."""
    folder = run_dir(home)
    for sess in sorted(sessions):
        if daemon_alive(sess, home):
            continue
        for ext in RUN_EXTS:
            path = folder / f"{sess}.{ext}"
            try:
                if path.is_symlink() or path.exists():
                    path.unlink()
            except OSError:
                traceback.print_exc()


def _socket_live(path):
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(1)
    try:
        sock.connect(str(path))
        return True
    except OSError:
        return False
    finally:
        sock.close()


def _pid_exists(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _proc_env(pid):
    """The environment words of a process of this user, or None."""
    try:
        return Path(f"/proc/{pid}/environ").read_bytes().decode("utf-8", "replace").split("\0")
    except OSError:
        pass
    try:
        out = subprocess.run(["ps", "-E", "-ww", "-o", "command=", "-p", str(pid)], capture_output=True,
                             text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.split() if out.returncode == 0 else None


def _is_session(pid, session, daemon=False):
    """Does this process's environment name this session in this namespace (and, for the
    daemon, AGENT_BROWSER_DAEMON=1)? Its Chrome inherits the same words (probed)."""
    if pid <= 1 or pid == os.getpid() or not _pid_exists(pid):
        return False
    need = {f"AGENT_BROWSER_SESSION={session}", f"AGENT_BROWSER_NAMESPACE={NAMESPACE}"}
    if daemon:
        need.add("AGENT_BROWSER_DAEMON=1")
    return need <= set(_proc_env(pid) or ())


def daemon_pid(session, home=None):
    """The pid of this session's daemon, only when that process really is it, else None."""
    try:
        pid = int((run_dir(home) / f"{session}.pid").read_text().strip())
    except (OSError, ValueError):
        return None
    return pid if _is_session(pid, session, daemon=True) else None


def daemon_alive(session, home=None):
    return _socket_live(run_dir(home) / f"{session}.sock") or daemon_pid(session, home) is not None


def _children(pid):
    """[(child pid, its process group)] of `pid`, from ps."""
    try:
        out = subprocess.run(["ps", "-axo", "pid=,ppid=,pgid="], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return []
    rows = []
    for line in out.stdout.splitlines():
        parts = line.split()
        if len(parts) == 3 and all(x.isdigit() for x in parts) and int(parts[1]) == pid:
            rows.append((int(parts[0]), int(parts[2])))
    return rows


def _wait_gone(pids, seconds):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if not any(_pid_exists(p) for p in pids):
            return True
        time.sleep(0.1)
    return not any(_pid_exists(p) for p in pids)


def _signal_chrome(pid, pgid, session, sig):
    """Signal a Chrome the daemon started: its whole group when it leads one, else itself; only
    after its environment is checked again to name this session (a pid may be reused)."""
    if not _is_session(pid, session):
        return
    try:
        if pgid == pid:
            os.killpg(pgid, sig)
        else:
            os.kill(pid, sig)
    except (ProcessLookupError, PermissionError):
        pass


def kill_daemon(session, home=None, wait=DAEMON_WAIT):
    """Stop this session's daemon and the Chrome it launched: SIGTERM first (it closes Chrome and
    saves state), then SIGKILL, then each Chrome group it started. Each signal goes only to a pid
    checked again, just before it, as that session's. True when the session is gone."""
    pid = daemon_pid(session, home)
    if pid is None:
        return not daemon_alive(session, home)
    own = os.getpgid(0)
    kids = [(c, g) for c, g in _children(pid) if g != own]
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    if not _wait_gone([pid], wait) and daemon_pid(session, home) == pid:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        _wait_gone([pid], wait)
    for c, g in kids:  # a Chrome left behind: its whole group (helpers and crash handlers)
        _signal_chrome(c, g, session, signal.SIGTERM)
    if not _wait_gone([c for c, _ in kids], wait):
        for c, g in kids:
            _signal_chrome(c, g, session, signal.SIGKILL)
        _wait_gone([c for c, _ in kids], wait)
    return not daemon_alive(session, home) and not any(_is_session(c, session) for c, _ in kids)


def engine_close_session(session, mode, root):
    """`<binary> --session <s> --namespace leobot close` for a session by name. True when it ran."""
    binary = browser_engine.resolve()
    key = _read_key(root)
    if binary is None or key is None:
        return False
    try:
        config, cwd = engine_dirs(root)
        subprocess.run([str(binary), "--session", session, "--namespace", NAMESPACE, "close"],
                       env=_engine_env(session, mode, key, config), cwd=str(cwd), capture_output=True,
                       timeout=CLOSE_SECONDS)
        return True
    except (OSError, subprocess.SubprocessError):
        return False


def _parallel(jobs, bound):
    """Run each job in its own thread, all joined within one bound. [result or None]."""
    out = [None] * len(jobs)

    def run(i, job):
        try:
            out[i] = job()
        except Exception:  # noqa: BLE001 - one job that breaks never stops the rest
            traceback.print_exc()
    threads = [threading.Thread(target=run, args=(i, j), name="browser-close", daemon=True)
               for i, j in enumerate(jobs)]
    for t in threads:
        t.start()
    deadline = time.monotonic() + bound
    for t in threads:
        t.join(max(0.0, deadline - time.monotonic()))
    return out


def _settle_one(session, mode, root, home):
    if not daemon_alive(session, home):
        return True
    engine_close_session(session, mode, root)
    return not daemon_alive(session, home) or kill_daemon(session, home)


def settle_sessions(sessions, root, home=None, bound=CLOSE_SECONDS + 2 * DAEMON_WAIT + 5):
    """No daemon of these sessions left: each live one is closed once more, then killed (with its
    Chrome), all in parallel within one bound. True when none is left."""
    items = sorted(sessions.items())
    done = _parallel([lambda s=s, m=m: _settle_one(s, m, root, home) for s, m in items], bound)
    return all(d is True for d in done)


def orphan_sessions(root, home=None):
    """{session: mode} on disk of an epoch this data root retired (a deleted workspace). Only
    retired epochs: another Leo Bot data root on this machine shares the namespace, and its
    sessions are not ours. A live epoch is never listed (core/browser.retired)."""
    old = set(browser.retired(root))
    return {sess: mode for sess, mode in found_sessions(home).items() if parse_session(sess)[1] in old}


def sweep_orphans(root, home=None):
    """Server start: settle (confirmed daemons only) and forget every session of a retired epoch,
    a delete whose tidy did not finish. An epoch with nothing left is cleared from the list.
    Returns the orphan sessions handled."""
    orphans = orphan_sessions(root, home)
    if orphans:
        forget_sessions(orphans, home)
        browser_relay.forget_typed(orphans, root)
        settled = settle_sessions(orphans, root, home)
        forget_sessions(orphans, home)
        forget_run_files(orphans, home)
        if not settled:
            return orphans
    left = {parse_session(s)[1] for s in found_sessions(home)}
    browser.clear_retired([e for e in browser.retired(root) if e not in left], root)
    return orphans


POOL = Pool()
