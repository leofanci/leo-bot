"""taint.py — what outside content a conversation holds, and the two rules that follow.

An agent is safe to leave alone only when no conversation ever holds all three of: private
data, content someone else wrote, and a way to send things out. Leo Bot keeps them apart:

  1. Outside content (an app read's result, a web search or page, a guest's message) is in
     the conversation: an Auto job's saves wait for one approval, and a Full access shell
     sends nothing off the machine. App actions follow the owner's per-app choice instead
     ("Sends without asking" sends; dashboard/apps_relay.py).
  2. App data is in the conversation: web search and fetch are off. A query or a URL can no
     longer carry it to another host.

A `Turn` is one owner request, shared by every run in it (the lead, teammates it consults,
its follow-up, a handoff), since each run's reply reaches the others. It starts from the
thread's saved flags and only ever gains taint. The engines check it at the moment a tool
runs: the apps relay (reads mark it, actions read it), a Claude Code PreToolUse hook
(dashboard/turn_hook.py, asking /api/internal/gate), the API engine's own loop, and osctl
in an Auto job (a save waits once outside content is in).
"""
import hmac
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
SAVE_WAITS = ("This run read outside content (mail, pages, app data), so it saves nothing by itself: "
              "say what and where, and end with one APPROVAL: line.")


class Turn:
    """Taint for one owner request. Only ever gains: nothing clears it."""

    def __init__(self, app_data=False, outside=False):
        self._lock = threading.Lock()
        self.app_data = bool(app_data)
        self.outside = bool(outside or app_data)

    def saw_app_data(self):
        with self._lock:
            self.app_data = self.outside = True

    def saw_outside(self):
        with self._lock:
            self.outside = True

    def web(self):
        """May a web search or fetch run now? Yes marks the turn (its results are outside content)."""
        with self._lock:
            if self.app_data:
                return False
            self.outside = True
            return True

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

    def mint(self, turn):
        token = secrets.token_hex(32)
        with self._lock:
            self._live[token] = (turn, time.time())
        return token

    def lookup(self, token):
        if not isinstance(token, str) or not token:
            return None
        now = time.time()
        with self._lock:
            for live, (turn, made) in list(self._live.items()):
                if now - made > MAX_AGE:
                    del self._live[live]
                elif hmac.compare_digest(live.encode(), token.encode()):
                    return turn
        return None

    def revoke(self, token):
        with self._lock:
            self._live.pop(token, None)


GATES = Gates()
