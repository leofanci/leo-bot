"""runners.py: a person's own Mac running that person's turns on their own login.

Tokens live hashed in gitignored team/runners.json. The hub is in-memory: the
server queues a job for a person, their runner long-polls for it and streams the
turn's events back. Outbound HTTPS from the Mac only; no tunnel to it.
"""
import hashlib
import json
import os
import queue
import secrets
import tempfile
import threading
import time
import uuid
from pathlib import Path

ONLINE_SECONDS = 45     # a runner polls every 25 s at most
LEASE_SECONDS = 60      # a queued job no runner took
MAX_TURN_SECONDS = 1800 # a runner that keeps polling but never ends a turn holds the thread no longer
MAX_TURN_CHARS = 4_000_000  # what one turn may stream back
MAX_PER_PERSON = 10
_KINDS = ("delta", "tool", "error")
_LOCK = threading.Lock()


def _file(root):
    return Path(root) / "team" / "runners.json"


def _read(root):
    try:
        data = json.loads(_file(root).read_text())
    except (OSError, ValueError):
        return {}
    rows = data.get("runners") if isinstance(data, dict) else None
    return rows if isinstance(rows, dict) else {}


def _write(root, rows):
    path = _file(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    with os.fdopen(fd, "w") as f:
        json.dump({"runners": rows}, f, indent=1)
    os.replace(tmp, path)


def _hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


def create(root, owner, name=""):
    """(runner id, token). The token is shown once and stored hashed."""
    token = "myr_" + secrets.token_urlsafe(32)
    rid = "rn-" + secrets.token_hex(4)
    with _LOCK:
        rows = _read(root)
        if sum(r.get("owner") == owner for r in rows.values()) >= MAX_PER_PERSON:
            raise ValueError("too many connected Macs: remove one first")
        rows[rid] = {"owner": owner, "name": (str(name or "").strip() or "My Mac")[:40],
                     "token_hash": _hash(token), "created": time.time()}
        _write(root, rows)
    return rid, token


def verify(root, token):
    """The runner record for a token, or None."""
    if not isinstance(token, str) or not token.startswith("myr_"):
        return None
    want = _hash(token)
    for rid, row in _read(root).items():
        if secrets.compare_digest(row.get("token_hash") or "", want):
            return {"id": rid, **row}
    return None


def list_for(root, owner):
    return [{"id": rid, "name": r.get("name"), "created": r.get("created"), "online": HUB.seen_recently(rid)}
            for rid, r in _read(root).items() if r.get("owner") == owner]


def revoke(root, rid, owner):
    """Delete a person's own runner token; its leased turns stop at once."""
    with _LOCK:
        rows = _read(root)
        if rows.get(rid, {}).get("owner") != owner:
            return False
        del rows[rid]
        _write(root, rows)
    HUB.drop(rid)
    return True


def revoke_owner(root, owner):
    """Everything a removed person had."""
    for row in list_for(root, owner):
        revoke(root, row["id"], owner)


def clean_event(ev):
    """One event from a runner, which a hostile teammate can write by hand: right shape
    or dropped. A runner's tokens and cost are its own business, so they never count here."""
    if not isinstance(ev, (list, tuple)) or len(ev) != 2:
        return None
    kind, payload = ev
    if kind == "done":
        return ("done", {"result": None, "subtype": "success", "tokens": None, "cost": None})
    if kind in _KINDS and isinstance(payload, str):
        return (kind, payload[:200] if kind == "tool" else payload)
    return None


class Turn:
    def __init__(self, job, owner):
        self.id, self.job, self.owner = job["turn"], job, owner
        self.events = queue.Queue()
        self.runner = None
        self.created = time.time()
        self.cancelled = False
        self.chars = 0


class Hub:
    def __init__(self):
        self._cond = threading.Condition()
        self._waiting = {}  # owner -> [Turn]
        self._turns = {}    # turn id -> Turn
        self._seen = {}     # runner id -> (owner, last poll)

    def seen_recently(self, rid):
        with self._cond:
            row = self._seen.get(rid)
            return bool(row and time.time() - row[1] < ONLINE_SECONDS)

    def online(self, owner):
        with self._cond:
            now = time.time()
            return any(o == owner and now - t < ONLINE_SECONDS for o, t in self._seen.values())

    def submit(self, owner, job):
        job = {**job, "turn": uuid.uuid4().hex}
        turn = Turn(job, owner)
        with self._cond:
            self._turns[turn.id] = turn
            self._waiting.setdefault(owner, []).append(turn)
            self._cond.notify_all()
        return turn

    def next(self, rid, owner, wait=25):
        """Long poll: the next job for this runner's person, or None. Also its heartbeat."""
        deadline = time.time() + wait
        with self._cond:
            while True:
                self._seen[rid] = (owner, time.time())
                rows = self._waiting.get(owner) or []
                while rows:
                    turn = rows.pop(0)
                    if turn.cancelled:
                        continue
                    turn.runner = rid
                    return turn.job
                left = deadline - time.time()
                if left <= 0:
                    return None
                self._cond.wait(min(left, 5))

    def events(self, rid, turn_id, events, end=False):
        """Events from the runner holding this turn. Returns True when it should stop."""
        with self._cond:
            turn = self._turns.get(turn_id)
            if turn is None or turn.runner != rid:
                return True
            if turn.cancelled:  # nobody is listening any more
                return True
            for ev in map(clean_event, events):
                if ev is None:
                    continue
                turn.chars += len(ev[1]) if isinstance(ev[1], str) else 0
                if turn.chars > MAX_TURN_CHARS:
                    turn.cancelled = True
                    turn.events.put(("error", "That reply was too long, so it was stopped."))
                    turn.events.put(None)
                    return True
                turn.events.put(ev)
            if end:
                turn.events.put(None)
        return False

    def holds(self, rid, turn_id):
        with self._cond:
            turn = self._turns.get(turn_id)
            return bool(turn and turn.runner == rid)

    def wait_events(self, turn, poll=2):
        """Yield a turn's events until it ends, its runner vanishes, or nobody takes it."""
        try:
            while True:
                if time.time() - turn.created > MAX_TURN_SECONDS:  # checked always: a trickle never goes quiet
                    yield ("error", "Your Mac took too long on this reply, so it was stopped.")
                    return
                try:
                    ev = turn.events.get(timeout=poll)
                except queue.Empty:
                    if turn.cancelled:  # stopped: the Mac learns on its next call
                        return
                    if turn.runner is None and time.time() - turn.created > LEASE_SECONDS:
                        yield ("error", "Your Mac didn't pick this up. Check that the runner is on.")
                        return
                    if turn.runner is not None and not self.seen_recently(turn.runner):
                        yield ("error", "Your Mac went offline during this reply.")
                        return
                    continue
                if ev is None:
                    return
                yield ev
        finally:
            self.finish(turn)

    def cancel(self, turn):
        with self._cond:
            turn.cancelled = True

    def finish(self, turn):
        with self._cond:
            turn.cancelled = True
            self._turns.pop(turn.id, None)
            rows = self._waiting.get(turn.owner) or []
            if turn in rows:
                rows.remove(turn)

    def drop_turns(self, owner, ws):
        """A person removed from one workspace: their turns there end now (and with them
        the turn tokens their runner holds)."""
        with self._cond:
            for turn in list(self._turns.values()):
                if turn.owner == owner and (turn.job.get("policy") or {}).get("workspace") == ws:
                    turn.cancelled = True
                    turn.events.put(("error", "You were removed from this workspace."))
                    turn.events.put(None)
            self._cond.notify_all()

    def drop_workspace(self, ws):
        """A deleted workspace: every turn there ends now, whoever's runner holds it."""
        with self._cond:
            for turn in list(self._turns.values()):
                if (turn.job.get("policy") or {}).get("workspace") == ws:
                    turn.cancelled = True
                    turn.events.put(("error", "This workspace was deleted."))
                    turn.events.put(None)
            self._cond.notify_all()

    def drop(self, rid):
        with self._cond:
            self._seen.pop(rid, None)
            for turn in self._turns.values():
                if turn.runner == rid:
                    turn.cancelled = True
                    turn.events.put(("error", "This runner was removed."))
                    turn.events.put(None)


HUB = Hub()
