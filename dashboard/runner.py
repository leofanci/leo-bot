"""runner.py: run your own bot turns on this Mac, for a Leo Bot on a server.

    python3 -m dashboard.runner --server https://leobot.example --token-file ~/.leobot-runner-token

Connects out to the server, takes the turns you send, runs them on this machine's
agent CLI and your own login, and streams the answers back. Each turn has no shell
and no file tools: its only Leo Bot tool is osctl, which runs on the server.

Lend my Chrome (off unless you turn it on, here only; the server can never turn it on):

    python3 -m dashboard.runner --lend-chrome on

With it on, a My Chrome bot on the server uses its own tab of this computer's Chrome
through this runner. The server's gate decides what may run; each call is checked again
here (dashboard/browser_lent.runner_check) before Chrome sees it. `--browser-only` lends
Chrome and takes no turns (your turns then run on the server's engine).

Moving a bot browser login to the server: the runner saves the server address and the token
file's path (never the token) in its settings file when it connects. Settings, Browser, "Send
to server" on this computer's Leo Bot then sends that bot's saved login with send_login, on
the runner's own token and the same https-only, no-redirect client.
"""
import argparse
import atexit
import json
import os
import re
import shutil
import signal
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import runners  # noqa: E402
from dashboard import browser_lent  # noqa: E402
from dashboard.engines.base import EngineRefused, SessionLost, TurnPolicy, TurnTokens  # noqa: E402
from dashboard.engines.claude_code import (  # noqa: E402
    ClaudeCodeEngine, private_tmpdir, remove_bot_tmpdir, sweep_stale_tmpdirs)

POLL_TIMEOUT = 40
FLUSH_SECONDS = 0.25
HEARTBEAT_SECONDS = 5   # how soon a Stop reaches a turn that is thinking
STATE_FILE = Path.home() / ".leobot-runner-sessions.json"
SETTINGS_FILE = Path.home() / ".leobot-runner-settings.json"  # this computer's own: Lend my Chrome
BROWSER_ROOT = Path.home() / ".leobot-runner-browser"  # the lent Chrome's engine key, config and tools
MAX_LENT = 20  # bot tabs kept open at once; the least recently used one closes first
SCREENSHOT = "agent_browser_screenshot"
_EPOCH_RE = re.compile(r"[0-9a-f]{6}")
_TURN_RE = re.compile(r"[0-9a-f]{32}")
_CHAT_RE = re.compile(r"[0-9a-f-]{36}")
_MODEL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/\[\]-]{0,99}")
_HOST_RE = re.compile(r"[a-z0-9][a-z0-9.-]{0,252}")
_LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):  # a redirect would carry the token to another host
        return None


def server_url(raw):
    """The server's address. The token travels in every request, so https unless local."""
    parts = urllib.parse.urlsplit(raw.rstrip("/"))
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("--server: an https address, e.g. https://leobot.example")
    if parts.scheme == "http" and parts.hostname not in _LOCAL_HOSTS:
        raise ValueError("--server: use https. Your runner token would travel unencrypted.")
    return raw.rstrip("/")


class Client:
    def __init__(self, server, token):
        self.server, self.token = server.rstrip("/"), token
        self.opener = urllib.request.build_opener(_NoRedirect)

    def post(self, path, body, timeout=30):
        req = urllib.request.Request(
            self.server + path, data=json.dumps(body).encode(), method="POST",
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.token}"})
        with self.opener.open(req, timeout=timeout) as resp:
            return json.loads(resp.read() or b"{}")


class Sessions:
    """Which chats already have a session on this Mac (survives a runner restart)."""

    def __init__(self, path=STATE_FILE):
        self.path, self.lock = Path(path), threading.Lock()

    def started(self, chat):
        try:
            return bool(json.loads(self.path.read_text()).get(chat))
        except (OSError, ValueError):
            return False

    def mark(self, chat):
        with self.lock:
            try:
                data = json.loads(self.path.read_text())
            except (OSError, ValueError):
                data = {}
            data[chat] = True
            self.path.write_text(json.dumps(data))
            self.path.chmod(0o600)


class JobSession:
    """What a driver needs from a chat session, for one job."""

    def __init__(self, job, workdir, started):
        self.session_id, self.rail, self.repo_dir = job["chat"], job["rail"], workdir
        self.model = job.get("model") or "haiku"
        self._started, self._proc, self.engine_state, self.ephemeral = started, None, {}, False


def job_policy(job):
    """Always the shared policy, whatever the job asks: no file, shell or app tools."""
    p = job.get("policy") or {}
    domains = [d for d in p.get("web_domains") or () if isinstance(d, str) and _HOST_RE.fullmatch(d)]
    return TurnPolicy(shared=True, readonly=bool(p.get("readonly")), web=bool(p.get("web")),
                      web_domains=tuple(domains), apps=False,
                      workspace=str(p.get("workspace") or "hq"), apps_used=bool(p.get("apps_used")))


def check_job(job):
    """This Mac trusts the server with a prompt, not with its command line."""
    model = job.get("model")
    if (not isinstance(job.get("chat"), str) or not _CHAT_RE.fullmatch(job["chat"])
            or not isinstance(job.get("turn"), str) or not re.fullmatch(r"[0-9a-f]{32}", job["turn"])
            or not isinstance(job.get("rail"), str) or not isinstance(job.get("text"), str)
            or (model is not None and not (isinstance(model, str) and _MODEL_RE.fullmatch(model)))):
        raise ValueError("bad job from the server")


def lend_on(path=SETTINGS_FILE):
    """Lend my Chrome, from this computer's own settings file. Missing or unreadable: off."""
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return False
    return isinstance(data, dict) and data.get("lend_chrome") is True


def _save_settings(path, **changes):
    path = Path(path)
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        data = {}
    data = data if isinstance(data, dict) else {}
    data.update(changes)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def set_lend(path, on):
    _save_settings(path, lend_chrome=bool(on))


def set_connection(path, server, token_file):
    """The server and token file this runner connects with, for send_login (no token here)."""
    _save_settings(path, server=server, token_file=str(Path(token_file).expanduser().resolve()))


NO_RUNNER = ("This computer has no runner set up for a server yet. Start the runner once with --server and "
             "--token-file, then send again.")


def connection(path=SETTINGS_FILE):
    """(Client, server) from the saved connection, or raises ValueError with the line to show."""
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        raise ValueError(NO_RUNNER) from None
    if not isinstance(data, dict) or not isinstance(data.get("server"), str) or not isinstance(data.get("token_file"), str):
        raise ValueError(NO_RUNNER)
    server = server_url(data["server"])
    token_file = Path(data["token_file"])
    try:
        if token_file.stat().st_mode & 0o077:
            raise ValueError(f"The runner token file can be read by other users. Run: chmod 600 {token_file}")
        token = token_file.read_text().strip()
    except OSError:
        raise ValueError("The runner token file could not be read. Start the runner again with --token-file.") from None
    if not token:
        raise ValueError(NO_RUNNER)
    return Client(server, token), server


def send_login(ws, bot, state, path=SETTINGS_FILE, timeout=120):
    """Send one bot's saved login (plain state JSON, bytes) to the server on this runner's
    token. Returns the server's answer; raises ValueError with the line to show."""
    client, server = connection(path)
    try:
        return client.post("/api/runner/browser-login", {"ws": ws, "bot": bot, "state": state.decode()},
                           timeout=timeout)
    except urllib.error.HTTPError as e:
        try:
            line = json.loads(e.read() or b"{}").get("error")
        except (OSError, ValueError, AttributeError):
            line = None
        if e.code in (401, 403) and not line:
            line = "The server refused this computer's runner token. Create a new one in Leo Bot."
        raise ValueError(str(line) if line else f"The server did not take the login (HTTP {e.code}).") from None
    except (urllib.error.URLError, OSError, ValueError):
        raise ValueError(f"Could not reach {server}. Check that it is up, then send again.") from None


def check_browser_job(job):
    """A browser job from the server: its shape, before anything here acts on it."""
    if (not isinstance(job, dict) or job.get("kind") != "browser" or job.get("op") not in browser_lent.OPS
            or not isinstance(job.get("turn"), str) or not _TURN_RE.fullmatch(job["turn"])
            or not isinstance(job.get("epoch"), str) or not _EPOCH_RE.fullmatch(job["epoch"])
            or any(not isinstance(job.get(k), str) or not 0 < len(job[k]) <= 200
                   for k in (("ws",) if job["op"] == "close" and "bot" not in job else ("ws", "bot")))
            or (job["op"] in ("call", "list") and not isinstance(job.get("frame"), dict))):
        raise ValueError("bad browser job from the server")


class Lender:
    """This computer's Chrome, lent to the server's My Chrome bots: one local my_chrome
    BrowserUpstream per (workspace, bot, epoch), each in its own pinned tab."""

    def __init__(self, root=BROWSER_ROOT, settings=SETTINGS_FILE, make=None):
        self.root, self.settings = Path(root), Path(settings)
        self._make = make or self._browser
        self._ups, self._lock = {}, threading.Lock()

    def _browser(self, ws, bot, epoch):
        from dashboard.browser_upstream import BrowserUpstream
        self.root.mkdir(parents=True, exist_ok=True)
        return BrowserUpstream(ws, bot, "my_chrome", self.root, epoch=epoch)

    def _upstream(self, ws, bot, epoch):
        key, drop = (ws, bot, epoch), []
        with self._lock:
            up = self._ups.get(key)
            if up is None or getattr(up, "closed", False):
                up = self._ups[key] = self._make(ws, bot, epoch)
            up._lent_used = time.time()
            while len(self._ups) > MAX_LENT:
                old = min((k for k in self._ups if k != key), key=lambda k: getattr(self._ups[k], "_lent_used", 0))
                drop.append(self._ups.pop(old))
        for old in drop:
            _close(old)
        return up

    def answer(self, job):
        """The reply to one browser job (check_browser_job first)."""
        check_browser_job(job)
        if job["op"] == "close":  # cleanup is allowed even after lending is turned off
            self.close_where(job["ws"], job["epoch"], job.get("bot"))
            return None
        if not lend_on(self.settings):  # read again for every call: off takes effect at once
            return {"refused": "lend_off"}
        up = self._upstream(job["ws"], job["bot"], job["epoch"])
        if job["op"] == "url":
            return {"url": up.current_url()}
        frame, refusal = browser_lent.runner_check(job["frame"], up)
        if refusal is not None:
            return refusal
        if frame["method"] == "tools/call" and not browser_lent.browser_relay._state(up)["schemas"]:
            up.forward({"jsonrpc": "2.0", "id": 0, "method": "tools/list", "params": {}}, None)
            frame, refusal = browser_lent.runner_check(job["frame"], up)  # now with the tool's schema
            if refusal is not None:
                return refusal
        reply, _ = up.forward(frame, None)
        if frame["method"] == "tools/list":
            return browser_lent.runner_list(reply)
        if frame["params"].get("name") == SCREENSHOT and isinstance(reply, dict) and isinstance(reply.get("result"), dict):
            # The image bytes travel in the reply; the server cannot reach this computer's file.
            browser_lent.browser_relay.prune_screenshot(reply["result"])
        return reply

    def sweep(self, now=None):
        now = time.time() if now is None else now
        with self._lock:
            items = list(self._ups.items())
        for key, up in items:
            closer = getattr(up, "close_if_idle", None)
            if closer is not None and closer(now):
                with self._lock:
                    if self._ups.get(key) is up:
                        del self._ups[key]

    def close_where(self, ws, epoch, bot=None):
        with self._lock:
            keys = [k for k in self._ups if k[0] == ws and k[2] == epoch and (bot is None or k[1] == bot)]
            ups = [self._ups.pop(k) for k in keys]
        for up in ups:
            _close(up, interrupt=bot is not None)

    def close_all(self):
        with self._lock:
            ups, self._ups = list(self._ups.values()), {}
        for up in ups:
            _close(up)


def _close(up, interrupt=False):
    try:
        killer = getattr(up, "kill", None) if interrupt else None
        if killer is not None:
            killer()  # Stop must not wait for the call holding the upstream's lock.
        closer = getattr(up, "close", None)
        if closer is not None:
            closer()
    except Exception as e:  # noqa: BLE001 - one tab that fails to close never stops the rest
        print(f"A lent Chrome tab did not close: {type(e).__name__}: {e}", file=sys.stderr)


def _job_error(rid, line):
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32000, "message": line}}


def safe_browser_job(job, client, lender):
    """One browser job on its own thread: the answer always goes back. A job this runner
    cannot read still gets an error reply when its id can be read, so the server does not
    wait out BROWSER_SECONDS for it; otherwise it is skipped."""
    turn = job.get("turn") if isinstance(job, dict) else None
    if not (isinstance(turn, str) and _TURN_RE.fullmatch(turn)):
        print("Skipped a browser job: bad browser job from the server", file=sys.stderr)
        return
    frame = job.get("frame") if isinstance(job.get("frame"), dict) else {}
    rid = frame.get("id") if isinstance(frame.get("id"), (int, str)) else None
    try:
        reply = lender.answer(job)
    except ValueError as e:
        print(f"Skipped a browser job: {e}", file=sys.stderr)
        if job.get("note"):
            return
        reply = _job_error(rid, browser_lent.BAD_JOB)
    except Exception as e:  # noqa: BLE001
        reply = _job_error(rid, f"The runner failed: {type(e).__name__}")
    if job.get("op") == "close" or job.get("note"):
        return  # a note: nobody waits for its answer
    try:
        size = len(json.dumps(reply))
    except (TypeError, ValueError):
        reply, size = _job_error(rid, "unreadable reply"), 0
    if size > runners.MAX_TURN_CHARS:  # the server drops it anyway: say so instead of sending it
        reply = _job_error(rid, browser_lent.TOO_LONG)
    for attempt in range(3):
        try:
            client.post(f"/api/runner/reply/{job['turn']}", {"reply": reply})
            return
        except (urllib.error.URLError, OSError, ValueError):
            if attempt < 2:
                time.sleep(1)


def run_job(job, client, engine, sessions, workdir):
    check_job(job)
    session = JobSession(job, workdir, sessions.started(job["chat"]))
    tokens = TurnTokens(os_token=job.get("os_token"), os_url=client.server + "/api/runner/os/mcp",
                        runner_token=client.token)
    pending, last, lock, sending, finished = [], time.time(), threading.Lock(), threading.Lock(), threading.Event()

    def flush(end=False):
        nonlocal pending, last
        with sending:  # one at a time, so the end never overtakes the last words
            with lock:
                batch, pending = pending, []
                last = time.time()
            for attempt in range(3 if end else 1):  # a lost end would hold the thread until it timed out
                try:
                    reply = client.post(f"/api/runner/events/{job['turn']}", {"events": batch, "end": end})
                    return bool(reply.get("cancel"))
                except (urllib.error.URLError, OSError, ValueError):
                    if end and attempt < 2:
                        time.sleep(1)
            with lock:
                pending = batch + pending  # kept for the next call
            return False

    def stop():
        proc = session._proc
        if proc is not None and proc.poll() is None:
            proc.terminate()

    def heartbeat():
        while not finished.wait(HEARTBEAT_SECONDS):
            if flush():
                stop()

    threading.Thread(target=heartbeat, daemon=True).start()
    text = job["text"]
    try:
        for attempt in (0, 1):
            try:
                for ev in engine.turn(session, text, job_policy(job), job.get("model"), tokens):
                    with lock:
                        pending.append(list(ev))
                    if (ev[0] != "delta" or time.time() - last > FLUSH_SECONDS) and flush():
                        stop()
                break
            except SessionLost:
                if attempt:
                    raise
                text = f"{job['recap']}\n\n{text}" if job.get("recap") else text
    except (EngineRefused, SessionLost) as e:
        with lock:
            pending.append(["error", str(e) or "This engine could not run the turn."])
    except Exception as e:  # noqa: BLE001  the answer must always come back
        with lock:
            pending.append(["error", f"The runner failed: {type(e).__name__}"])
    else:
        sessions.mark(job["chat"])
    finished.set()
    flush(end=True)


def safe_job(job, client, engine, sessions, workdir):
    """One job on its own thread: a bad one is reported and the runner carries on."""
    try:
        run_job(job, client, engine, sessions, workdir)
    except ValueError as e:
        print(f"Skipped a job: {e}", file=sys.stderr)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Run your own Leo Bot turns on this Mac")
    ap.add_argument("--server", help="your Leo Bot address, e.g. https://leobot.example")
    ap.add_argument("--token-file", help="file holding the runner token")
    ap.add_argument("--claude-bin", default="claude")
    ap.add_argument("--lend-chrome", choices=("on", "off"),
                    help="Lend my Chrome: let the server's My Chrome bots use this computer's Chrome (saved here)")
    ap.add_argument("--browser-only", action="store_true", help="lend Chrome only, take no turns")
    ap.add_argument("--settings-file", default=str(SETTINGS_FILE), help=argparse.SUPPRESS)
    ap.add_argument("--browser-root", default=str(BROWSER_ROOT), help=argparse.SUPPRESS)
    args = ap.parse_args(argv)
    os.umask(0o077)  # the session list and the turn's temp folder are this person's alone
    settings = Path(args.settings_file).expanduser()
    if args.lend_chrome:
        set_lend(settings, args.lend_chrome == "on")
        print(f"Lend my Chrome is {args.lend_chrome} on this computer.")
        if not args.server:
            sys.exit(0)
    if not args.server or not args.token_file:
        ap.error("--server and --token-file are needed to connect")
    try:
        server = server_url(args.server)
    except ValueError as e:
        sys.exit(str(e))
    token_file = Path(args.token_file).expanduser()
    try:
        if token_file.stat().st_mode & 0o077:
            sys.exit(f"{token_file} can be read by other users. Run: chmod 600 {token_file}")
        token = token_file.read_text().strip()
    except OSError as e:
        sys.exit(f"Could not read the token file: {e}")
    client = Client(server, token)
    set_connection(settings, server, token_file)  # Settings, Browser, "Send to server" uses it
    engine = None
    if not args.browser_only:
        engine = ClaudeCodeEngine(args.claude_bin)
        status = engine.status()
        if not status["ok"]:
            sys.exit(f"Claude Code is not ready here: {status['detail']}")
        sweep_stale_tmpdirs()  # what a crashed runner left behind
    workdir = private_tmpdir("leobot-runner-")  # no project files: nothing here to read
    atexit.register(shutil.rmtree, workdir, True)  # and its bot temp folder: gone on exit
    atexit.register(remove_bot_tmpdir)
    sessions = Sessions()
    lender = Lender(Path(args.browser_root).expanduser(), settings)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))  # a plain kill closes the lent tabs too

    def sweeper():
        while True:
            time.sleep(60)
            try:
                lender.sweep()
            except Exception:  # noqa: BLE001 - the sweeper never stops the runner
                pass
    threading.Thread(target=sweeper, daemon=True).start()
    what = "Your Chrome is lent to My Chrome bots there" if args.browser_only else "Your messages there now run on this Mac"
    print(f"Connected to {args.server}. {what}. Lend my Chrome: {'on' if lend_on(settings) else 'off'}. Ctrl-C to stop.")
    if lend_on(settings) and not args.browser_only:
        print("Your chats there run on this computer, and those turns have no browser. "
              "For My Chrome in chats, run with --browser-only.")
    backoff = 1
    try:
        while True:
            try:
                reply = client.post("/api/runner/next", {"turns": not args.browser_only, "lend": lend_on(settings)},
                                    timeout=POLL_TIMEOUT)
                backoff = 1
            except urllib.error.HTTPError as e:
                if e.code in (401, 403):
                    sys.exit("The server refused this token. Create a new one in Leo Bot.")
                time.sleep(backoff)
                backoff = min(backoff * 2, 30)
                continue
            except (urllib.error.URLError, OSError, ValueError):
                time.sleep(backoff)
                backoff = min(backoff * 2, 30)
                continue
            job = reply.get("job")
            if isinstance(job, dict) and job.get("kind") == "browser":
                threading.Thread(target=safe_browser_job, args=(job, client, lender), daemon=True).start()
            elif job and engine is not None:
                threading.Thread(target=safe_job, args=(job, client, engine, sessions, workdir), daemon=True).start()
    finally:
        # Each lent tab's engine session ends (Chrome keeps running). Here, not in atexit: at
        # interpreter shutdown the child's stop cannot start its thread, and the close is lost.
        lender.close_all()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped. Your messages go back to the server's engine.")
