"""runners.py: a person's own Mac running that person's turns on their own login.

Tokens live hashed in gitignored team/runners.json. The hub is in-memory: the
server queues a job for a person, their runner long-polls for it and streams the
turn's events back. Outbound HTTPS from the Mac only; no tunnel to it.

A `browser` job (kind "browser") is one MCP frame for that person's own Chrome, lent by
their runner (dashboard/browser_lent.py): one reply back, the same per-person queue and
holder check as a turn. Each poll says what the runner does: "turns" (default on, as older
runners send nothing) and "lend" (its owner turned on Lend my Chrome on that computer).
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
BROWSER_LEASE = 15     # a browser job no lending runner took: that computer is not answering
BROWSER_SECONDS = 300  # one browser call on the runner: an engine start plus the call itself
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
        self._caps = {}     # runner id -> {"turns": bool, "lend": bool}, from its last poll

    def seen_recently(self, rid):
        with self._cond:
            row = self._seen.get(rid)
            return bool(row and time.time() - row[1] < ONLINE_SECONDS)

    def _live(self, owner):
        now = time.time()
        return [rid for rid, (o, t) in self._seen.items() if o == owner and now - t < ONLINE_SECONDS]

    def online(self, owner):
        """A runner of this person that takes turns is on (the turn engine)."""
        with self._cond:
            return any(self._caps.get(rid, {}).get("turns", True) for rid in self._live(owner))

    def reachable(self, owner):
        """Any runner of this person is on."""
        with self._cond:
            return bool(self._live(owner))

    def lenders(self, owner):
        """This person's runners that are on and lend their Chrome, oldest id first."""
        with self._cond:
            return sorted(rid for rid in self._live(owner) if self._caps.get(rid, {}).get("lend"))

    def submit(self, owner, job):
        job = {**job, "turn": uuid.uuid4().hex}
        turn = Turn(job, owner)
        with self._cond:
            self._turns[turn.id] = turn
            self._waiting.setdefault(owner, []).append(turn)
            self._cond.notify_all()
        return turn

    def next(self, rid, owner, wait=25, caps=None):
        """Long poll: the next job for this runner's person, or None. Also its heartbeat.
        A browser job goes only to a lending runner (the one it names, when it names one);
        a turn only to a runner that takes turns."""
        caps = caps if isinstance(caps, dict) else {}
        mine = {"turns": caps.get("turns", True) is not False, "lend": caps.get("lend") is True}
        deadline = time.time() + wait
        with self._cond:
            self._caps[rid] = mine
            while True:
                self._seen[rid] = (owner, time.time())
                rows = self._waiting.get(owner) or []
                for turn in list(rows):
                    if turn.cancelled:
                        rows.remove(turn)
                        continue
                    if turn.job.get("kind") == "browser":
                        if not mine["lend"] or turn.job.get("runner") not in (None, rid):
                            continue
                    elif not mine["turns"]:
                        continue
                    rows.remove(turn)
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
            if turn is None or turn.runner != rid or turn.job.get("kind") == "browser":
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

    def reply(self, rid, turn_id, payload):
        """The one answer to a browser job, from the runner holding it. False when this runner
        does not hold it, it was answered or ended, or it is not a browser job. An answer over
        MAX_TURN_CHARS is dropped and the caller hears that it was too long."""
        try:
            size = len(json.dumps(payload))
        except (TypeError, ValueError):
            size = None
        with self._cond:
            turn = self._turns.get(turn_id)
            if (turn is None or turn.runner != rid or turn.cancelled
                    or turn.job.get("kind") != "browser"):
                return False
            turn.cancelled = True  # one answer per job
            if size is None:
                turn.events.put(("error", "bad reply"))
            elif size > MAX_TURN_CHARS:
                turn.events.put(("too_long", None))
            else:
                turn.events.put(("reply", payload))
        return True

    def call(self, owner, job, lease=BROWSER_LEASE, limit=BROWSER_SECONDS, poll=0.5):
        """Queue one browser job for this person and wait for its answer:
        ("reply", payload), ("too_long", None), ("offline", None), ("timeout", None) or
        ("error", line) when it was ended here (runner removed, workspace deleted)."""
        turn = self.submit(owner, {**job, "kind": "browser"})
        try:
            while True:
                try:
                    ev = turn.events.get(timeout=poll)
                except queue.Empty:
                    age = time.time() - turn.created
                    if age > limit:
                        return ("timeout", None)
                    if turn.runner is None and age > lease:
                        return ("offline", None)
                    if turn.runner is not None and not self.seen_recently(turn.runner):
                        return ("offline", None)
                    continue
                return ev if ev is not None else ("error", "This call was ended.")
        finally:
            self.finish(turn)

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
            self._caps.pop(rid, None)
            for turn in self._turns.values():
                if turn.runner == rid:
                    turn.cancelled = True
                    turn.events.put(("error", "This runner was removed."))
                    turn.events.put(None)


HUB = Hub()
