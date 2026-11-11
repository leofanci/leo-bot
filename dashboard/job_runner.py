"""job_runner.py: runs scheduled jobs (core/jobs.py) inside the server, and posts the daily
HQ digest.

A tick thread wakes every TICK seconds and runs each due job, one at a time across all
workspaces, through `run_turn` (server.run_job: the normal ask path, read-only). The job's
state is written before the run (`running`) and after it, so a restart never runs it twice;
a run cut off by a stop is marked `interrupted` on the next start. A run longer than
JOB_TURN_SECONDS is stopped (`stop_turn`) and ends as `timeout`.

Once a day, from DIGEST_HOUR local time, one thread in HQ's #general lists what ran, what it
cost, which memory files were tidied and what waits for approval. Built from the saved
threads, the job ledger and the approvals file: no model call. Nothing to say, no thread.
"""
import threading
import time
import traceback

from core import approvals, bots, jobs
from dashboard import chat_store, job_ledger

TICK = 60
JOB_TURN_SECONDS = 1800
DIGEST_HOUR = 8
DIGEST_MAX_DAYS = 7
DIGEST_ROOM = f"{bots.HQ}/general"
APPROVALS_LISTED = 5


def _money(value):
    return f"${value:.2f}" if value >= 0.01 else f"${value:.4f}"


def _num(value):
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def digest_text(root, since, now, threads=None):
    """The digest for [since, now), or None when nothing happened and nothing waits."""
    in_window = lambda ts: since <= _num(ts) < now  # noqa: E731
    known = {j["id"]: j for j in jobs.load(root)}
    runs = {}  # job id -> {"name", "where", "runs", "cost"}

    def slot(jid, name=None, where=""):
        job = known.get(jid) or {}
        return runs.setdefault(jid, {"name": job.get("name") or name or jid, "runs": 0, "cost": 0.0,
                                     "where": where or (f"{job['workspace']}, @{job['bot']}" if job else ""),
                                     "status": None})

    for rec in (threads or chat_store.records)():
        jid = rec.get("job")
        if not isinstance(jid, str) or not jid:
            continue
        row = None
        if in_window(rec.get("created")):
            title = str(rec.get("title") or "")
            row = slot(jid, title[5:] if title.startswith("Job: ") else title)
            row["runs"] += 1
        for s in rec.get("spend") or ():
            if isinstance(s, dict) and s.get("job") == jid and in_window(s.get("ts")):
                (row or slot(jid))["cost"] += _num(s.get("cost"))
    for jid, job in known.items():
        if in_window(job.get("last_run")):
            row = slot(jid)
            row["status"] = job.get("last_status")
            if not row["runs"]:
                row["runs"] = 1  # a run that left no thread (refused, capped before the model)

    tidy, gen_runs, gen_cost = [], 0, 0.0
    for r in job_ledger.rows(root):
        if not in_window(r.get("ts")):
            continue
        if r.get("job") == "memory-consolidate":
            tidy.append(f"- @{r.get('bot')} in {r.get('ws') or bots.HQ}: {r.get('before')} → {r.get('after')} entries")
        elif r.get("job") == "generate":
            gen_runs += 1
            gen_cost += _num(r.get("cost"))
    waiting = approvals.pending(root)

    parts = []
    if runs:
        lines = []
        for row in runs.values():
            where = f" ({row['where']})" if row["where"] else ""
            last = f", last: {row['status']}" if row["status"] else ""
            times = "1 run" if row["runs"] == 1 else f"{row['runs']} runs"
            lines.append(f"- {row['name']}{where}: {times}, {_money(row['cost'])}{last}")
        parts.append("Scheduled jobs\n" + "\n".join(lines))
    if tidy:
        parts.append("Memory tidy-ups\n" + "\n".join(tidy))
    if gen_runs:
        parts.append(f"Generation runs\n- {gen_runs} {'run' if gen_runs == 1 else 'runs'}, {_money(gen_cost)}")
    if waiting:
        lines = [f"- {r.get('ws')}: {r.get('summary') or (r.get('action') or {}).get('slug') or 'an app action'}"
                 for r in waiting[:APPROVALS_LISTED]]
        more = len(waiting) - APPROVALS_LISTED
        if more > 0:
            lines.append(f"- and {more} more")
        parts.append(f"Waiting for your approval ({len(waiting)})\n" + "\n".join(lines))
    if not parts:
        return None
    head = f"Since {time.strftime('%a %d %b %H:%M', time.localtime(since))}."
    return head + "\n\n" + "\n\n".join(parts)


class Runner:
    """`run_turn(job) -> {"status", "chat", ...}` runs one job; `stop_turn()` stops the one in
    flight. `threads` and `post` are the saved-thread source and the note writer (the server
    passes its own chat_store copy)."""

    def __init__(self, root, run_turn, stop_turn=None, threads=None, post=None, now=time.time):
        self.root, self.run_turn, self.stop_turn = root, run_turn, stop_turn
        self.threads = threads or chat_store.records
        self.post = post or chat_store.post_note
        self.now = now
        self._halt = threading.Event()

    def tick(self):
        """Run every due job, oldest due first, one at a time; then the digest. The ids run."""
        now = self.now()
        due = []
        for job in jobs.load(self.root):
            if jobs.is_due(job, now):
                due.append((jobs.next_due(job), job))
        ran = []
        for _, job in sorted(due, key=lambda d: d[0]):
            if self._run(job):
                ran.append(job["id"])
        try:
            self.maybe_digest()
        except Exception:  # noqa: BLE001 - a digest never stops the jobs
            traceback.print_exc()
        return ran

    def _run(self, job):
        try:
            jobs.mark_start(self.root, job["id"], self.now())
        except LookupError:  # deleted since the tick read the file
            return False
        fired = threading.Event()

        def stop():
            fired.set()
            if self.stop_turn:
                self.stop_turn()
        timer = threading.Timer(JOB_TURN_SECONDS, stop)
        timer.daemon = True
        timer.start()
        try:
            out = self.run_turn(job) or {}
        except Exception:  # noqa: BLE001 - one job's failure never stops the others
            traceback.print_exc()
            out = {"status": "failed"}
        finally:
            timer.cancel()
        status = "timeout" if fired.is_set() else out.get("status")
        jobs.mark_end(self.root, job["id"], status if status in jobs.STATUSES else "failed", chat=out.get("chat"))
        return True

    def maybe_digest(self):
        """Post the day's digest once, from DIGEST_HOUR local. The new thread id, or None."""
        now = self.now()
        today = time.localtime(now)
        if today.tm_hour < DIGEST_HOUR:
            return None
        last = _num(jobs.digest_state(self.root).get("last")) or None
        if last and time.localtime(last)[:3] == today[:3]:
            return None
        since = max(last or now - 86400, now - DIGEST_MAX_DAYS * 86400)
        text = digest_text(self.root, since, now, self.threads)
        jobs.set_digest(self.root, now)
        if not text:
            return None
        return self.post(f"Digest {time.strftime('%Y-%m-%d', today)}", DIGEST_ROOM, bots.ROOT_BOT, text)["id"]

    def start(self):
        """Mark runs a stop cut off, then tick every TICK seconds in a daemon thread."""
        try:
            jobs.recover(self.root)
        except OSError:
            traceback.print_exc()

        def loop():
            while not self._halt.wait(TICK):
                try:
                    self.tick()
                except Exception:  # noqa: BLE001 - the runner never takes the server down
                    traceback.print_exc()
        thread = threading.Thread(target=loop, name="job-runner", daemon=True)
        thread.start()
        return thread

    def halt(self):
        self._halt.set()
