"""runner.py: run your own bot turns on this Mac, for a myOS on a server.

    python3 -m dashboard.runner --server https://myos.example --token-file ~/.myos-runner-token

Connects out to the server, takes the turns you send, runs them on this machine's
agent CLI and your own login, and streams the answers back. Each turn has no shell
and no file tools: its only myOS tool is osctl, which runs on the server.
"""
import argparse
import json
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dashboard.engines.base import EngineRefused, SessionLost, TurnPolicy, TurnTokens  # noqa: E402
from dashboard.engines.claude_code import ClaudeCodeEngine  # noqa: E402

POLL_TIMEOUT = 40
FLUSH_SECONDS = 0.25
STATE_FILE = Path.home() / ".myos-runner-sessions.json"


class Client:
    def __init__(self, server, token):
        self.server, self.token = server.rstrip("/"), token

    def post(self, path, body, timeout=30):
        req = urllib.request.Request(
            self.server + path, data=json.dumps(body).encode(), method="POST",
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.token}"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
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
    return TurnPolicy(shared=True, readonly=bool(p.get("readonly")), web=bool(p.get("web")),
                      web_domains=tuple(p.get("web_domains") or ()), apps=False,
                      workspace=str(p.get("workspace") or "hq"), apps_used=bool(p.get("apps_used")))


def run_job(job, client, engine, sessions, workdir):
    session = JobSession(job, workdir, sessions.started(job["chat"]))
    tokens = TurnTokens(os_token=job.get("os_token"), os_url=client.server + "/api/runner/os/mcp",
                        runner_token=client.token)
    pending, last = [], time.time()

    def flush(end=False):
        nonlocal pending, last
        batch, pending = pending, []
        last = time.time()
        try:
            reply = client.post(f"/api/runner/events/{job['turn']}", {"events": batch, "end": end})
        except (urllib.error.URLError, OSError, ValueError):
            return False
        return bool(reply.get("cancel"))

    def stop():
        proc = session._proc
        if proc is not None and proc.poll() is None:
            proc.terminate()

    text = job["text"]
    try:
        for attempt in (0, 1):
            try:
                for ev in engine.turn(session, text, job_policy(job), job.get("model"), tokens):
                    pending.append(list(ev))
                    if (ev[0] != "delta" or time.time() - last > FLUSH_SECONDS) and flush():
                        stop()
                break
            except SessionLost:
                if attempt or not job.get("recap"):
                    raise
                text = f"{job['recap']}\n\n{text}"
    except (EngineRefused, SessionLost) as e:
        pending.append(["error", str(e) or "This engine could not run the turn."])
    except Exception as e:  # noqa: BLE001  the answer must always come back
        pending.append(["error", f"The runner failed: {type(e).__name__}"])
    else:
        sessions.mark(job["chat"])
    flush(end=True)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Run your own myOS turns on this Mac")
    ap.add_argument("--server", required=True, help="your myOS address, e.g. https://myos.example")
    ap.add_argument("--token-file", required=True, help="file holding the runner token")
    ap.add_argument("--claude-bin", default="claude")
    args = ap.parse_args(argv)
    token = Path(args.token_file).expanduser().read_text().strip()
    client = Client(args.server, token)
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
                sys.exit("The server refused this token. Create a new one in myOS.")
            time.sleep(backoff)
            backoff = min(backoff * 2, 30)
            continue
        except (urllib.error.URLError, OSError, ValueError):
            time.sleep(backoff)
            backoff = min(backoff * 2, 30)
            continue
        job = reply.get("job")
        if job:
            threading.Thread(target=run_job, args=(job, client, engine, sessions, workdir), daemon=True).start()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped. Your messages go back to the server's engine.")
