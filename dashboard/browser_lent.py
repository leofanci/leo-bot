"""browser_lent.py: a server Leo Bot uses the owner's own Chrome through the owner's runner.

On a server (LEOBOT_DEPLOYMENT=server) a my_chrome bot's upstream is a RunnerChromeUpstream
(browser_upstream.Pool.get). The gate (browser_relay: levels, send plans, daily caps, site
modes, the field read) still runs here, in the dashboard process. Each frame the gate passes
becomes one `browser` job in core/runners.HUB for the turn's own person (ctx "runner_owner"),
so a frame of one person's turn never reaches another person's runner. The runner answers it
with its own local my_chrome BrowserUpstream (dashboard/runner.py, Lender).

  no runner of that person on      RUNNER_OFF, and nothing else is tried: never the bot
                                   browser, never this machine's Chrome (the logins differ)
  runner on, Lend my Chrome off    LEND_OFF (the setting lives on that computer only)
  a thread invited people can see  SHARED: no browser at all
  answer over MAX_TURN_CHARS       TOO_LONG

The runner checks each frame again before its Chrome sees it (runner_check): only the
engine tools of core/browser_engine.py's four sets, never a tab tool or a newTab click, the
always-dropped inputs dropped, only web addresses opened, and eval only as the relay's own
fixed field read. A server that is not to be trusted still reaches no eval, file or cookie
tool through the lend.
"""
import copy
import json
import threading
from pathlib import Path

from core import bots, browser, runners
from dashboard import browser_relay
from dashboard.browser_relay import (BAD_URL, CHROME_NEW_TAB, CHROME_TAB_TOOLS, CHROME_TABS, EVAL,
                                     EVAL_TIMEOUT_MS, FIELDS_SCRIPT, KNOWN, NEW_TAB_ARG, READ, READ_URL,
                                     REFUSED, UNREADABLE)

RUNNER_OFF = ("Your computer is offline, so this bot cannot use your Chrome. Open Leo Bot on your computer, "
              "then ask again.")
LEND_OFF = ("Lend my Chrome is off on your computer, so this bot cannot use your Chrome. Run the runner there "
            "with --lend-chrome on, then ask again.")
SHARED = "This thread is shared with invited people, so it has no browser. This call was not performed."
TOO_LONG = "Your computer's answer was too long, so it was dropped. This call may have run."
SLOW = ("Your computer did not answer within {s} seconds, so this browser call may not have finished. "
        "Ask again.")
BAD_JOB = "Your computer could not read this browser call, so it did not run."
BAD_REPLY = "Your computer's answer could not be read. This call may have run."
LISTS_NAME = "browser-lent-tools.json"  # <root>/run: the last tools a runner listed, per person
OPS = ("call", "list", "url", "close")  # close: a deleted workspace's tabs (a note, no answer)
# A tools/list must answer fast (an MCP client lists tools as it connects): a runner that does
# not take it within LIST_LEASE seconds is skipped and the last list this server saw is used.
LIST_LEASE, LIST_SECONDS = 3, 20
_LISTS_LOCK = threading.Lock()


def _error(frame, line):
    from dashboard.browser_upstream import _call_error
    return _call_error(frame, line)


def _lists_path(root):
    return Path(root) / "run" / LISTS_NAME


def _saved_tools(root, owner):
    try:
        data = json.loads(_lists_path(root).read_text())
    except (OSError, ValueError):
        return None
    tools = data.get(owner) if isinstance(data, dict) else None
    if not isinstance(tools, list):
        return None
    tools = [t for t in tools if isinstance(t, dict) and isinstance(t.get("name"), str)]
    return tools or None


def _save_tools(root, owner, tools):
    with _LISTS_LOCK:
        try:
            data = json.loads(_lists_path(root).read_text())
        except (OSError, ValueError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        data[owner] = tools
        try:
            _lists_path(root).parent.mkdir(parents=True, exist_ok=True)
            bots._atomic_write(_lists_path(root), json.dumps(data) + "\n")
        except OSError:
            pass  # only a convenience: the bot then hears why instead of seeing the tools


class RunnerChromeUpstream(browser_relay.Upstream):
    """One server bot's tab in its person's own Chrome, reached through that person's runner.
    Kept per (ws, bot, person) by the pool, so the gate's page block and schemas stay with it.
    It sticks to one lending runner; when another one takes over, the typed text is set aside
    (another computer's Chrome has nothing typed)."""
    mode = "my_chrome"
    remote = True  # Its engine files are on the person's computer, never this one's.
    POLL = 0.5

    def __init__(self, ws, bot, root, owner, epoch, shared=False, hub=None):
        self.ws, self.bot, self.root, self.owner, self.epoch = ws, bot, root, owner, epoch
        self.shared = bool(shared) or not (isinstance(owner, str) and owner)
        self.session = browser_relay.session_name(ws, bot, "my_chrome", epoch)
        # One lock per person's Chrome; typed text per bot tab, as in a local my_chrome.
        self.lock_key = self.browser_key = f"lent:{browser_relay.session_part(str(owner))}"
        self.typed_key = browser_relay.typed_key_for("my_chrome", ws, bot, self.session)
        self.hub = hub or runners.HUB
        self.closed = False
        self._closed_line = None
        self._runner = None
        self._lock = threading.Lock()

    def close(self, line):
        self.closed, self._closed_line = True, line

    def _ask(self, op, frame=None, lease=runners.BROWSER_LEASE, limit=runners.BROWSER_SECONDS):
        """(status, payload) of one job on this person's lending runner."""
        lending = self.hub.lenders(self.owner)
        if not lending:
            return ("lend_off" if self.hub.reachable(self.owner) else "offline"), None
        with self._lock:
            rid = self._runner if self._runner in lending else lending[0]
            moved = self._runner is not None and rid != self._runner
            self._runner = rid
        if moved:
            browser_relay.set_aside(self.typed_key, self.root)
        job = {"op": op, "ws": self.ws, "bot": self.bot, "epoch": self.epoch, "runner": rid,
               "policy": {"workspace": self.ws}}
        if frame is not None:
            job["frame"] = frame
        status, payload = self.hub.call(self.owner, job, lease=lease, limit=limit, poll=self.POLL)
        if status == "reply" and isinstance(payload, dict) and payload.get("refused") == "lend_off":
            return "lend_off", None
        return status, payload

    def _line(self, status, payload):
        return {"offline": RUNNER_OFF, "lend_off": LEND_OFF, "too_long": TOO_LONG,
                "timeout": SLOW.format(s=runners.BROWSER_SECONDS)}.get(
                    status, payload if status == "error" and isinstance(payload, str) else BAD_REPLY)

    def forward(self, frame, mcp_session_id):
        if not isinstance(frame, dict):
            return _error({}, "invalid request"), mcp_session_id
        method = frame.get("method")
        if isinstance(method, str) and method.startswith("notifications/"):
            return None, mcp_session_id  # the handshake is this upstream's own
        rid = frame.get("id")
        local = browser_relay.local_reply(frame)
        if local is not None:
            return local, mcp_session_id
        if self.shared:
            return _error(frame, SHARED), mcp_session_id
        if self.closed:
            return _error(frame, self._closed_line), mcp_session_id
        if method not in ("tools/list", "tools/call"):
            return _error(frame, "method not available"), mcp_session_id
        op = "list" if method == "tools/list" else "call"
        timing = {"lease": LIST_LEASE, "limit": LIST_SECONDS} if op == "list" else {}
        status, payload = self._ask(op, {"jsonrpc": "2.0", "id": rid, "method": method,
                                         "params": frame.get("params") if isinstance(frame.get("params"), dict) else {}},
                                    **timing)
        ok = (status == "reply" and isinstance(payload, dict)
              and (isinstance(payload.get("result"), dict) or isinstance(payload.get("error"), dict)))
        if op == "list":
            tools = payload["result"].get("tools") if ok and isinstance(payload.get("result"), dict) else None
            if isinstance(tools, list):
                _save_tools(self.root, self.owner, tools)
                return {"jsonrpc": "2.0", "id": rid, "result": {"tools": tools}}, mcp_session_id
            saved = _saved_tools(self.root, self.owner)
            if saved is not None:  # the tools are known: a call then says what is wrong
                return {"jsonrpc": "2.0", "id": rid, "result": {"tools": copy.deepcopy(saved)}}, mcp_session_id
            return _error(frame, self._line(status, payload) if not ok else BAD_REPLY), mcp_session_id
        if not ok:
            return _error(frame, self._line(status, payload)), mcp_session_id
        from dashboard.browser_upstream import TAB_RESET
        reply = {k: payload[k] for k in ("result", "error") if k in payload}
        reply.update(jsonrpc="2.0", id=rid)
        content = (reply.get("result") or {}).get("content") if isinstance(reply.get("result"), dict) else None
        first = content[0] if isinstance(content, list) and content and isinstance(content[0], dict) else {}
        if first.get("text") == TAB_RESET:  # a new empty tab on that computer: typed text set aside
            browser_relay.set_aside(self.typed_key, self.root)
        return reply, mcp_session_id

    def known_tools(self):
        """This person's runner's last tools/list, kept on the server, or None."""
        if self.shared:
            return None
        return copy.deepcopy(_saved_tools(self.root, self.owner))

    def current_url(self):
        if self.shared or self.closed:
            return None
        status, payload = self._ask("url")
        url = payload.get("url") if status == "reply" and isinstance(payload, dict) else None
        return url.strip() if isinstance(url, str) and url.strip() else None


# -- the runner's own check, before its Chrome sees a frame ------------------------------------

def _refusal(rid, line):
    return browser_relay._text(rid, line)


def runner_check(frame, upstream):
    """(frame to forward, None) or (None, refusal reply). Runs on the runner, on what the server
    sent: only tools/list and tools/call of the four engine sets (no tab tools, no newTab click),
    engine-wide inputs dropped and, with the tool's schema known, only its declared keys; a url
    only when it is a web address or about:blank; eval only as the relay's own field read."""
    if not isinstance(frame, dict):
        return None, browser_relay._error(None, -32600, "invalid request")
    rid, method = frame.get("id"), frame.get("method")
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": rid, "method": "tools/list", "params": {}}, None
    if method != "tools/call":
        return None, browser_relay._error(rid, -32601, "method not available")
    params = frame.get("params") if isinstance(frame.get("params"), dict) else {}
    name, arguments = params.get("name"), params.get("arguments")
    arguments = {} if arguments is None else arguments
    if not isinstance(arguments, dict):
        return None, _refusal(rid, UNREADABLE)
    if name == EVAL:
        if arguments.get("script") != FIELDS_SCRIPT:
            return None, _refusal(rid, REFUSED.format(name=name))
        args = {"script": FIELDS_SCRIPT, "timeoutMs": EVAL_TIMEOUT_MS}
    else:
        if not isinstance(name, str) or name not in KNOWN:
            return None, _refusal(rid, REFUSED.format(name=name))
        if name in CHROME_TAB_TOOLS:
            return None, _refusal(rid, CHROME_TABS)
        if arguments.get(NEW_TAB_ARG):
            return None, _refusal(rid, CHROME_NEW_TAB)
        if name == READ and "url" in arguments:
            return None, _refusal(rid, READ_URL)
        args = browser_relay._clean(arguments, browser_relay._state(upstream)["schemas"].get(name))
        args.pop(NEW_TAB_ARG, None)
        if "url" in args:
            url = args["url"]
            blank = isinstance(url, str) and url.strip().lower() == "about:blank"
            if not blank and not (isinstance(url, str) and browser.is_web_url(url) and browser.host_of(url)):
                return None, _refusal(rid, BAD_URL)
    return {"jsonrpc": "2.0", "id": rid, "method": "tools/call", "params": {"name": name, "arguments": args}}, None


def runner_list(reply):
    """The runner's tools/list answer: only the tools runner_check passes, without the inputs
    it drops (newTab included)."""
    if not (isinstance(reply, dict) and isinstance(reply.get("result"), dict)):
        return reply
    tools = reply["result"].get("tools") or []
    listed = []
    for t in tools:
        if not (isinstance(t, dict) and t.get("name") in KNOWN and t["name"] not in CHROME_TAB_TOOLS):
            continue
        t = browser_relay._strip_schema(copy.deepcopy(t), {})
        props = (t.get("inputSchema") or {}).get("properties")
        if isinstance(props, dict):
            props.pop(NEW_TAB_ARG, None)
        listed.append(t)
    out = dict(reply)
    out["result"] = {**reply["result"], "tools": listed}
    return out
