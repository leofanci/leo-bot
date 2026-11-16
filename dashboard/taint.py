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
SAVE_WAITS = ("This run read outside content (mail, pages, app data), so it saves nothing by itself: "
              "say what and where, and end with one APPROVAL: line.")


class Turn:
    """Taint for one owner request. Only ever gains: nothing clears it."""

    def __init__(self, app_data=False, outside=False):
        self._lock = threading.Lock()
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
        return shell_rules.plan(command, outside=outside, writable=writable, profile_dir=profile_dir, browser=walls,
                                cwd=cwd)

    def decide(self, tool, command=None):
        """(allow, reason) for one tool about to run: a web tool, "osctl-write" (a save in an
        Auto job) or "Bash" (a Full access shell command, dashboard/shell_rules.py). Anything
        else is not this gate's business."""
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

    def __init__(self):
        self._live = {}
        self._lock = threading.Lock()

    def mint(self, turn, writable=(), wrap=True):
        """`writable`: the run's edit folders, the only folders a sandboxed command may write.
        `wrap`: whether its shell commands come back sandboxed after outside content (an engine
        with its own sandbox, Codex, says no)."""
        token = secrets.token_hex(32)
        with self._lock:
            self._live[token] = (turn, time.time(), tuple(str(d) for d in writable), bool(wrap))
        return token

    def _find(self, token):
        if not isinstance(token, str) or not token:
            return None
        now = time.time()
        with self._lock:
            for live, entry in list(self._live.items()):
                if now - entry[1] > MAX_AGE:
                    del self._live[live]
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

    def shell(self, token, command, profile_dir=None, cwd=None):
        """(allow, reason, run) for a shell command on the run holding `token` (Turn.shell).
        `cwd`: the folder the engine says the command runs in (it may keep an earlier `cd`)."""
        entry = self._find(token)
        if entry is None:
            return False, "no gate for this run", None
        turn, _, writable, wrap = entry
        allow, reason, run = turn.shell(command, writable, profile_dir=profile_dir if wrap else None, cwd=cwd)
        return allow, reason, (run if wrap else None)

    def revoke(self, token):
        with self._lock:
            self._live.pop(token, None)


GATES = Gates()
