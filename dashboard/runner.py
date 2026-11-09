"""runner.py: run your own bot turns on this Mac, for a Leo Bot on a server.

    python3 -m dashboard.runner --server https://myos.example --token-file ~/.myos-runner-token

Connects out to the server, takes the turns you send, runs them on this machine's
agent CLI and your own login, and streams the answers back. Each turn has no shell
and no file tools: its only Leo Bot tool is osctl, which runs on the server.
"""
import argparse
import json
import os
import re
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dashboard.engines.base import EngineRefused, SessionLost, TurnPolicy, TurnTokens  # noqa: E402
from dashboard.engines.claude_code import ClaudeCodeEngine  # noqa: E402

POLL_TIMEOUT = 40
FLUSH_SECONDS = 0.25
HEARTBEAT_SECONDS = 5   # how soon a Stop reaches a turn that is thinking
STATE_FILE = Path.home() / ".myos-runner-sessions.json"
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
        raise ValueError("--server: an https address, e.g. https://myos.example")
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
    ap.add_argument("--server", required=True, help="your Leo Bot address, e.g. https://myos.example")
    ap.add_argument("--token-file", required=True, help="file holding the runner token")
    ap.add_argument("--claude-bin", default="claude")
    args = ap.parse_args(argv)
    os.umask(0o077)  # the session list and the turn's temp folder are this person's alone
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
    engine = ClaudeCodeEngine(args.claude_bin)
    status = engine.status()
    if not status["ok"]:
        sys.exit(f"Claude Code is not ready here: {status['detail']}")
    workdir = tempfile.mkdtemp(prefix="myos-runner-")  # no project files: nothing here to read
    sessions = Sessions()
    print(f"Connected to {args.server}. Your messages there now run on this Mac. Ctrl-C to stop.")
    backoff = 1
    while True:
        try:
            reply = client.post("/api/runner/next", {}, timeout=POLL_TIMEOUT)
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
        if job:
            threading.Thread(target=safe_job, args=(job, client, engine, sessions, workdir), daemon=True).start()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped. Your messages go back to the server's engine.")
