"""taint.py — what outside content a conversation holds, and the two rules that follow.

An agent is safe to leave alone only when no conversation ever holds all three of: private
data, content someone else wrote, and a way to send things out. Leo Bot keeps them apart:

  1. Outside content (an app read's result, a web search or page, a guest's message) is in
     the conversation: an Auto job's saves wait for one approval, and a shell level sends
     nothing off the machine (named sending commands are refused, every other command runs in
     the OS sandbox with no internet: dashboard/shell_rules.py). App actions follow the owner's per-app choice instead
     ("Sends without asking" sends; dashboard/apps_relay.py).
  2. App data no longer turns web search off; a fetch still reaches only hosts the owner
     typed (the engine's own rules), so a page cannot steer a fetch to another host.

A `Turn` is one owner request, shared by every run in it (the lead, teammates it consults,
its follow-up, a handoff), since each run's reply reaches the others. It starts from the
thread's saved flags and only ever gains taint. The engines check it at the moment a tool
runs: the apps relay (reads mark it, actions read it), a Claude Code PreToolUse hook
(dashboard/turn_hook.py, asking /api/internal/gate), the API engine's own loop, and osctl
in an Auto job (a save waits once outside content is in).
"""
import hmac
import os
import secrets
import threading
import time

GATE_PATH = "/api/internal/gate"
GATE_TOKEN_ENV = "LEOBOT_GATE_TOKEN"
GATE_URL_ENV = "LEOBOT_GATE_URL"
MAX_AGE = 3600  # a token normally dies with its request

WEB_TOOLS = frozenset({"WebSearch", "WebFetch", "web_search", "web_fetch"})
WEB_OFF = ("Web search and pages are off in this conversation: it holds data from connected apps, "
           "and a query must never carry it out. Work from what you have; for web research the owner "
           "can start a new thread.")
WRITE_OFF = "You change files only in your edit folders ({folders}). Say what you would change elsewhere."
FILE_TOOLS = frozenset({"Edit", "Write", "MultiEdit", "NotebookEdit"})
SHELL_UNCONFIRMED = ("Leo Bot could not confirm this command runs in its sandbox: Claude Code may have changed; "
                     "Approve for me shell is off until Leo Bot is updated.")
SAVE_WAITS = ("This run read outside content (mail, pages, app data), so it saves nothing by itself: "
              "say what and where, and end with one APPROVAL: line.")


class Turn:
    """Taint for one owner request. Only ever gains: nothing clears it."""

    def __init__(self, app_data=False, outside=False, who=None):
        self._lock = threading.Lock()
        self.who = dict(who or {})  # {"ws", "chat", "person", "job"}: who this request is, for the action log
        self.app_data = bool(app_data)
        self.outside = bool(outside or app_data)
        self.browser_root = None  # a run of this request has a browser: its data root
        self.browser_sends = False  # ... at a level that can send (write, act)
        self.unwalled_shell = None  # {"shell", "engine"} of a run whose shell Leo Bot cannot wall

    def browsing(self, root, sends=False, unwalled=None):
        """A run of this request has the browser (dashboard/shell_rules.py, the third wall): from
        now on every shell command runs sandboxed away from it. `unwalled`: that run's shell cannot
        be walled ({"shell", "engine"} labels), so the browser relay refuses every send. Only ever gains."""
        with self._lock:
            self.browser_root = str(root)
            self.browser_sends = self.browser_sends or bool(sends)
            if unwalled and self.unwalled_shell is None:
                self.unwalled_shell = dict(unwalled)

    def saw_app_data(self):
        with self._lock:
            self.app_data = self.outside = True

    def saw_outside(self):
        with self._lock:
            self.outside = True

    def web(self):
        """A web search or fetch runs now: it marks the turn (its results are outside content).
        App data no longer turns web off; a fetch still reaches only hosts the owner typed."""
        with self._lock:
            self.outside = True
            return True

    def shell(self, command, writable=(), profile_dir=None, cwd=None):
        """(allow, reason, run) for one shell command (dashboard/shell_rules.plan): an allowed
        command runs sandboxed, as `run` (the code wall on every level; after outside content
        also offline and write-limited). `cwd`: where it runs (default the first edit folder)."""
        from dashboard import shell_rules
        with self._lock:
            outside, root, sends = self.outside, self.browser_root, self.browser_sends
        walls = shell_rules.browser_walls(root, required=sends) if root else None
        allow, reason, run = shell_rules.plan(command, outside=outside, writable=writable, profile_dir=profile_dir,
                                              browser=walls, cwd=cwd)
        if allow:  # every command a bot's gate lets through is in the action log (core/action_log.py)
            from core import action_log
            action_log.record(None, "shell", command=command, cwd=cwd, outside=outside,
                              sandboxed=bool(run and run != command), **self.who)
        return allow, reason, run

    def shell_profile(self, writable=(), profile_dir=None):
        """The sandbox profile file the review shell runs every command in, for this request's
        state now (shell_rules.review_shell), or None with no sandbox."""
        from dashboard import shell_rules
        with self._lock:
            outside, root, sends = self.outside, self.browser_root, self.browser_sends
        walls = shell_rules.browser_walls(root, required=sends) if root else None
        return shell_rules.profile_path(writable, browser=walls, outside=outside, profile_dir=profile_dir)

    def decide(self, tool, command=None):
        """(allow, reason) for one tool about to run: a web tool, "osctl-write" (a save in an
        Auto job) or "Bash" (a shell command on Full access or Approve for me,
        dashboard/shell_rules.py). Anything else is not this gate's business."""
        if tool == "Bash":
            from dashboard import shell_rules
            with self._lock:
                outside = self.outside
            return shell_rules.decide(command, outside=outside)
        if tool in WEB_TOOLS:
            return (True, "") if self.web() else (False, WEB_OFF)
        if tool == "osctl-write":
            with self._lock:
                return (False, SAVE_WAITS) if self.outside else (True, "")
        return True, ""


class Gates:
    """Gate tokens: one per run, bound to its request's Turn."""

    TICKET_AGE = 120  # seconds an osctl ticket of the review shell stays redeemable
    TICKETS_PER_TOKEN = 32  # live tickets one run may hold: the oldest goes first

    def __init__(self):
        self._live = {}
        self._tickets = {}
        # Per review token: is the review shell really the shell Claude Code runs commands in?
        # {"seq": event counter, "allowed": {call id: seq of its allow}, "last": seq of the latest
        # allow, "shell": seq of the latest Bash-shell call, "ran": [seq of each allow reported run],
        # "broken": a command ran with no Bash-shell call after its allow}. See `ran`.
        self._watch = {}
        self._lock = threading.Lock()

    def mint(self, turn, writable=(), wrap=True, review=False):
        """`writable`: the run's edit folders, the only folders a sandboxed command may write.
        `wrap`: whether its shell commands come back sandboxed (an engine with its own sandbox,
        Codex, says no). `review`: Approve for me on Claude Code: a command is never rewritten (a
        rewrite skips Claude Code's reviewer); its shell (shell_rules.review_shell) sandboxes it."""
        token = secrets.token_hex(32)
        with self._lock:
            self._live[token] = (turn, time.time(), tuple(str(d) for d in writable), bool(wrap), bool(review))
        return token

    def review(self, token):
        entry = self._find(token)
        return bool(entry and entry[4])

    def _find(self, token):
        if not isinstance(token, str) or not token:
            return None
        now = time.time()
        with self._lock:
            for live, entry in list(self._live.items()):
                if now - entry[1] > MAX_AGE:
                    del self._live[live]
                    self._watch.pop(live, None)
                elif hmac.compare_digest(live.encode(), token.encode()):
                    return entry
        return None

    def may_write(self, token, path):
        """(allow, reason) for a file tool writing `path` on the run holding `token`: only inside
        its edit folders, and never in Leo Bot's own code (shell_rules.code_walls), even when an
        edit folder holds it. Claude Code's auto mode (Approve for me) would otherwise leave a
        write anywhere to its reviewer."""
        from dashboard import shell_rules
        entry = self._find(token)
        dirs = [os.path.realpath(d) for d in (entry[2] if entry else ())]
        if not isinstance(path, str) or not path.startswith("/") or not dirs:
            return False, WRITE_OFF.format(folders="none")
        real = os.path.realpath(path)
        if shell_rules.in_code(real):
            return False, shell_rules.CODE_OFF
        if any(real == d or real.startswith(d.rstrip("/") + "/") for d in dirs):
            return True, ""
        return False, WRITE_OFF.format(folders=", ".join(dirs))

    def lookup(self, token):
        entry = self._find(token)
        return entry[0] if entry else None

    def shell(self, token, command, profile_dir=None, cwd=None, call_id=None):
        """(allow, reason, run) for a shell command on the run holding `token` (Turn.shell).
        `cwd`: the folder the engine says the command runs in (it may keep an earlier `cd`).
        `call_id`: Claude Code's tool_use_id for it, matched by `ran`."""
        entry = self._find(token)
        if entry is None:
            return False, "no gate for this run", None
        turn, _, writable, wrap, review = entry
        if review and wrap and self._unconfirmed(token):
            return False, SHELL_UNCONFIRMED, None
        allow, reason, run = turn.shell(command, writable, profile_dir=profile_dir if wrap else None, cwd=cwd)
        if allow and not wrap:
            # An engine with its own sandbox (Codex) drops `run`, so Leo Bot's canvas deny never
            # applies: the text check stands in. A path the command builds at run time is not seen
            # (an accepted limit: such a run is outside content, offline, private threads only).
            from dashboard import shell_rules
            where = {c for c in (cwd, str(writable[0]) if writable else None) if c} or {None}
            if any(shell_rules.reads_canvases(command, c) for c in where):
                return False, shell_rules.CANVAS_OFF, None
        if not (allow and wrap and review and run):
            return allow, reason, (run if wrap else None)
        from dashboard import shell_rules
        if shell_rules.osctl_command(command) == run:  # Leo Bot's own osctl: a ticket the review shell redeems
            nonce = secrets.token_hex(16)
            with self._lock:
                self._prune_tickets(token)
                self._tickets[nonce] = (token, run, time.time())
                self._allowed(token, call_id)
            return True, "", f": {shell_rules.REVIEW_SHELL_TICKET} {nonce}"
        with self._lock:
            self._allowed(token, call_id)
        return True, "", None  # Claude Code's reviewer judges it; the review shell sandboxes it

    # The review shell is only a sandbox while Claude Code runs every command in it: it takes
    # CLAUDE_CODE_SHELL and SHELL today (2.1.325), and an update could stop reading them. So the
    # gate checks it: each allowed command must be followed by a Bash-shell call (the review shell
    # asking for its profile or redeeming a ticket) before Claude Code reports that command ran
    # (its PostToolUse or PostToolUseFailure hook, `ran`). Once one ran without, this and every
    # later command on the token is refused (fail closed). Checking at the report, not at the next
    # command, keeps two commands of one response (both checked before either runs) and a command
    # the reviewer refused (it never runs, nothing is reported) from tripping it.

    def _watched(self, token):
        return self._watch.setdefault(token, {"seq": 0, "allowed": {}, "last": None, "shell": -1, "ran": [],
                                              "broken": False})

    def _allowed(self, token, call_id):
        w = self._watched(token)
        w["seq"] += 1
        w["last"] = w["seq"]
        if isinstance(call_id, str) and call_id:
            w["allowed"][call_id] = w["seq"]
            if len(w["allowed"]) > 256:  # a long turn: the oldest ids have long been judged
                w["allowed"].pop(next(iter(w["allowed"])))

    def _shell_seen(self, token):
        with self._lock:
            w = self._watched(token)
            w["seq"] += 1
            w["shell"] = w["seq"]

    def _unconfirmed(self, token):
        with self._lock:
            w = self._watch.get(token)
            if w is None:
                return False
            if any(seq > w["shell"] for seq in w["ran"]):
                w["broken"] = True
            w["ran"] = []
            return w["broken"]

    def ran(self, token, call_id):
        """Claude Code reports a Bash command ran (or failed after it started). `call_id`: its
        tool_use_id; unknown or missing, the latest allowed command is the one it ran. A command
        started in the background reports at once, maybe before its shell asked, so the verdict
        waits for the next command or Bash-shell call."""
        entry = self._find(token)
        if entry is None or not entry[4]:
            return
        with self._lock:
            w = self._watch.get(token)
            if w is None:
                return
            seq = w["allowed"].pop(call_id, None) if isinstance(call_id, str) else None
            seq = w["last"] if seq is None else seq
            if seq is not None:
                w["ran"].append(seq)

    def _prune_tickets(self, token=None):
        """Drop expired tickets, and the oldest of `token` past TICKETS_PER_TOKEN - 1 (lock held)."""
        now = time.time()
        for k, (_, _, at) in list(self._tickets.items()):
            if now - at > self.TICKET_AGE:
                del self._tickets[k]
        if token is not None:
            mine = sorted((at, k) for k, (t, _, at) in self._tickets.items() if t == token)
            for _, k in mine[:max(0, len(mine) - self.TICKETS_PER_TOKEN + 1)]:
                del self._tickets[k]

    def redeem(self, token, nonce):
        """The osctl command a review-shell ticket stands for, once, for the token it was issued to."""
        found = self._find(token)
        if found is None:
            return None
        if found[4]:
            self._shell_seen(token)
        if not isinstance(nonce, str) or self._unconfirmed(token):
            return None
        with self._lock:
            self._prune_tickets()
            entry = self._tickets.get(nonce)
            if entry is None or not hmac.compare_digest(entry[0].encode(), str(token).encode()):
                return None  # another run's ticket stays for its own run
            del self._tickets[nonce]
        return entry[1]

    def shell_profile(self, token, profile_dir=None):
        """(allow, reason, profile path) for the review shell of the run holding `token`."""
        from core import paths
        entry = self._find(token)
        if entry is None or not entry[4]:
            return False, "no review shell for this run", None
        self._shell_seen(token)
        if self._unconfirmed(token):
            return False, SHELL_UNCONFIRMED, None
        path = entry[0].shell_profile(entry[2], profile_dir=profile_dir or str(paths.SANDBOX_DIR))
        return (True, "", path) if path else (False, "this machine has no sandbox", None)

    def revoke(self, token):
        with self._lock:
            self._live.pop(token, None)
            self._watch.pop(token, None)
            for k in [k for k, v in self._tickets.items() if v[0] == token]:
                del self._tickets[k]

    def revoke_all(self):
        """Stop everything (core/halt.py): every run's gate refuses from now on."""
        with self._lock:
            self._live.clear()
            self._watch.clear()
            self._tickets.clear()


GATES = Gates()
