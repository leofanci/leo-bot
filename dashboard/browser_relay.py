"""browser_relay.py: the gate between a bot and its browser engine.

A bot's browser turn gets one MCP server (the relay) in front of an `Upstream`: the
agent-browser child for that bot. The relay decides what passes:

  tools/list   only the engine tools in core/browser_engine.py's four sets, plus PLAN_TOOL,
               without the engine-wide inputs a bot must not set
  tools/call   unknown tool: refused. Level off: refused. Level read: reads and navigation only.
               A url argument must be a full web address (or about:blank) whose site is not
               bound to the other browser mode. Typing is remembered; a commit tool (click,
               press, select, check, uncheck) after typing is a send: a Report only job never
               sends, `write` needs an approved send plan for that page and text, `act` sends on
               its own and is logged in Approvals. Both stop at the site's daily cap, counted
               across every bot and workspace. After navigation the engine's own current URL is
               checked again: a redirect onto a site bound to the other mode is hidden and left.

What was typed belongs to the browser, not to an MCP session: the state lives on the
upstream object, so a second session on the same browser cannot start clean. Opening a
page or a new tab sets the typed text aside; history, reload and tab moves bring it back,
because the browser restores what was in those fields. A refused send keeps it too: the
text is still in the field, so a second click is still a send.
"""
import threading

from core import approvals, bots, browser
from core.browser_engine import COMMIT_TOOLS, NAV_TOOLS, READ_TOOLS, TYPE_TOOLS

PLAN_TOOL = "leobot_plan_sends"
METHODS = ("initialize", "ping", "tools/list", "tools/call")
KNOWN = READ_TOOLS | TYPE_TOOLS | COMMIT_TOOLS | NAV_TOOLS
# Engine-wide inputs, always dropped (with every key starting with "restore"): Leo Bot sets
# the session and namespace itself (the upstream); the rest reach files, certificates, raw
# flags, other windows, the daemon's lifetime or domain policy. Past these, a call keeps only
# the keys its tool's own engine schema declares (learned from tools/list).
ENGINE_WIDE = frozenset({"session", "namespace", "allowedDomains", "extraArgs", "caCert", "clearCaCert",
                         "useSystemCa", "idleTimeout", "path", "screenshotDir", "headed", "webmcp"})
MAX_TIMEOUT_MS = 120000
OPEN = "agent_browser_open"
NEW_TAB = "agent_browser_tab_new"
RESTORES = frozenset("agent_browser_" + n for n in ("back", "forward", "reload", "tab_switch", "tab_close"))
MAX_PLAN = 20
MAX_TEXT = 10000
MODE_LABELS = {"bot": "the bot browser", "my_chrome": "the owner's Chrome"}

SITE_MODE = "{site} is set to open only in {mode_label}. This bot uses {other_label}, so it did not open it."
NO_PLAN = ("This would send what you typed, and no approved send plan matches it. Call leobot_plan_sends with "
           "the page address and the exact text, then stop: the owner approves it in Approvals.")
CAPPED = ("{site} reached today's limit of {daily} sends. Nothing was sent. Try again tomorrow or ask the owner "
          "to raise the limit in Settings, Browser.")
REPORT_ONLY = "This scheduled job only reports, so it does not send. Nothing was sent."
HELD_PLAN = "{n} sends wait for one approval (Approve all in Approvals). None has gone out. Tell the user."
REFUSED = '"{name}" is not available to this bot. This call was not performed.'
OFF = ("Browsing is off for this bot. Ask the owner to turn it on in Settings, Browser, then stop. "
       "This call was not performed.")
READ_LEVEL = ('"{name}" changes the page, and this bot may only read in the browser. Ask the owner to allow more '
              'in Settings, Browser, then stop. This call was not performed.')
BAD_URL = "This address could not be read safely, so it was not opened. Use the full https address of the page."
NO_PAGE = "The address of this page is unknown, so no send could be checked. Nothing was sent."
BAD_PLAN = ('leobot_plan_sends takes "sends": a list of up to {max} items, each {{"url", "text"}}: the full https '
            'address of the page and the exact text. Nothing was planned.')
UNREADABLE = "This call could not be read. It was not performed."
SEND_BROKE = "The send did not finish. " + approvals.STALE_NOTE

PLAN_SCHEMA = {
    "name": PLAN_TOOL,
    "description": ("Plan messages to send from the browser. Each item is the full address of the page where it "
                    "goes and the exact text. The owner approves the plan in Approvals; then type that text on "
                    "that page and send it. Nothing is sent by this call."),
    "inputSchema": {
        "type": "object",
        "properties": {"sends": {"type": "array", "maxItems": MAX_PLAN, "items": {
            "type": "object", "additionalProperties": False, "required": ["url", "text"],
            "properties": {"url": {"type": "string"}, "text": {"type": "string"}}}}},
        "required": ["sends"], "additionalProperties": False},
}

_LOCK = threading.Lock()


class Upstream:
    """The browser behind the gate. `mode` is "bot" or "my_chrome"."""
    mode = "bot"

    def forward(self, frame, mcp_session_id):
        """(JSON-RPC reply or None for a notification, mcp-session-id)."""
        raise NotImplementedError

    def current_url(self):
        """The engine's own URL for the active tab, or None when it cannot tell."""
        raise NotImplementedError


def _state(upstream):
    with _LOCK:
        st = getattr(upstream, "_gate_state", None)
        if st is None:
            st = {"lock": threading.Lock(), "typed": "", "aside": "", "blocked": None, "schemas": {}}
            upstream._gate_state = st
        return st


def _join(a, b):
    return (a + " " + b).strip() if a and b else a or b


def _error(rid, code, message):
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}


def _text(rid, line, is_error=True):
    return {"jsonrpc": "2.0", "id": rid, "result": {"content": [{"type": "text", "text": line}], "isError": is_error}}


def _ok(reply):
    return isinstance(reply, dict) and isinstance(reply.get("result"), dict) and not reply["result"].get("isError")


def _dropped(k):
    return not isinstance(k, str) or k in ENGINE_WIDE or k.startswith("restore")


def _clean(arguments, declared=None):
    """The arguments that may travel: never the engine-wide ones; with the tool's schema
    known, only the keys it declares. timeoutMs is held to MAX_TIMEOUT_MS."""
    out = {k: v for k, v in arguments.items() if not _dropped(k) and (declared is None or k in declared)}
    if "timeoutMs" in out:
        t = out["timeoutMs"]
        if isinstance(t, bool) or not isinstance(t, (int, float)) or t < 0:
            del out["timeoutMs"]
        else:
            out["timeoutMs"] = min(t, MAX_TIMEOUT_MS)
    return out


def _mode_of(upstream, setting):
    mode = getattr(upstream, "mode", None)
    return mode if mode in browser.MODES else setting["mode"]


def _mode_refusal(site, mode, root):
    if not site:
        return None
    bound = (browser.sites(root).get(site) or {}).get("mode")
    if bound and bound != mode:
        return SITE_MODE.format(site=site, mode_label=MODE_LABELS[bound], other_label=MODE_LABELS[mode])
    return None


def _url_refusal(url, mode, root, blank_ok=True):
    """A refusal line for a url argument, or None. Fails closed: only http(s) with a host
    that parses (or about:blank) passes, so javascript:, file: and the like never open."""
    if not isinstance(url, str):
        return BAD_URL
    if blank_ok and url.strip().lower() == "about:blank":
        return None
    if not browser.is_web_url(url) or browser.host_of(url) is None:
        return BAD_URL
    return _mode_refusal(browser.site_of(url, root), mode, root)


def _current(upstream):
    try:
        url = upstream.current_url()
    except Exception:
        return None
    return url if isinstance(url, str) and url else None


def _forward(upstream, name, arguments, rid, sid, ctx):
    turn = ctx.get("taint")
    if turn is not None:  # whatever the page says is outside content from here on
        turn.saw_outside()
    return upstream.forward({"jsonrpc": "2.0", "id": rid, "method": "tools/call",
                             "params": {"name": name, "arguments": arguments}}, sid)


def _after_nav(upstream, st, mode, rid, sid, ctx, root):
    """Check the page the engine really landed on. On a site bound to the other mode the
    reply is withheld and the tab is sent to about:blank; until a later navigation lands on
    an allowed page, only navigation passes."""
    refusal = _mode_refusal(browser.site_of(_current(upstream), root), mode, root)
    st["blocked"] = refusal
    if refusal:
        try:
            _forward(upstream, OPEN, {"url": "about:blank"}, rid, sid, ctx)
        except Exception:
            pass
        if not _mode_refusal(browser.site_of(_current(upstream), root), mode, root):
            st["blocked"] = None
    return refusal


def _plan(rid, arguments, ctx, setting, mode, root):
    if ctx.get("report"):
        return _text(rid, REPORT_ONLY)
    sends = arguments.get("sends")
    bad = BAD_PLAN.format(max=MAX_PLAN)
    if not isinstance(sends, list) or not 1 <= len(sends) <= MAX_PLAN:
        return _text(rid, bad)
    items = []
    for it in sends:
        if not isinstance(it, dict) or not isinstance(it.get("text"), str) or not it["text"].strip() \
                or len(it["text"]) > MAX_TEXT:
            return _text(rid, bad)
        refusal = _url_refusal(it.get("url"), mode, root, blank_ok=False)
        if refusal:
            return _text(rid, refusal)
        url = it["url"].strip()
        items.append({"url": url, "text": it["text"],
                      "site": browser.site_of(url, root) or browser.host_of(url)})
    held = approvals.add_send_plan(root, ws=ctx["ws"], room=ctx.get("room"), chat=ctx.get("chat"),
                                   bot=ctx["bot"], person=ctx.get("person"), items=items)
    return _text(rid, HELD_PLAN.format(n=len(held)), False)


def _send(rid, name, arguments, sid, ctx, upstream, st, setting, mode, root):
    """A commit tool after typing: it would send what was typed."""
    if ctx.get("report"):
        return _text(rid, REPORT_ONLY), sid
    url = _current(upstream)
    if not url:
        return _text(rid, NO_PAGE), sid
    site = browser.site_of(url, root)
    refusal = _mode_refusal(site, mode, root)
    if refusal:
        return _text(rid, refusal), sid
    daily = (browser.sites(root).get(site) or {}).get("daily") if site else None
    if daily and approvals.sends_today(site, root) >= daily:
        return _text(rid, CAPPED.format(site=site, daily=daily)), sid
    typed, taken = st["typed"], None
    if setting["level"] == "write":
        taken = approvals.take_send(root, ws=ctx["ws"], bot=ctx["bot"], url=url, text=typed)
        if taken is None:
            return _text(rid, NO_PLAN), sid
    # From here the click is sent: whatever comes back, the typed text may have gone out.
    st["typed"] = st["aside"] = ""
    try:
        reply, sid = _forward(upstream, name, arguments, rid, sid, ctx)
    except Exception:
        reply = None
    ok = _ok(reply)
    if taken is not None and not ok:
        approvals.fail_send(taken["id"], approvals.STALE_NOTE, root)
    if setting["level"] == "act":
        approvals.log_app_run(root, ws=ctx["ws"], room=ctx.get("room"), chat=ctx.get("chat"), bot=ctx["bot"],
                              person=ctx.get("person"), toolkit=approvals.BROWSER, slug=approvals.BROWSER_SEND,
                              arguments={"url": url, "text": typed, "site": site or browser.host_of(url)},
                              account=None, status="done" if ok else "failed",
                              result=approvals.SENT if ok else approvals.STALE_NOTE)
    if reply is None:
        return _text(rid, SEND_BROKE), sid
    return reply, sid


def _gate(frame, ctx, sid, upstream, root):
    rid = frame.get("id")
    params = frame.get("params") if isinstance(frame.get("params"), dict) else {}
    name = params.get("name")
    arguments = params.get("arguments")
    if arguments is None:
        arguments = {}
    if not isinstance(name, str) or (name not in KNOWN and name != PLAN_TOOL):
        return _text(rid, REFUSED.format(name=name)), sid
    if not isinstance(arguments, dict):
        return _text(rid, UNREADABLE), sid
    setting = browser.bot_setting(ctx["ws"], ctx["bot"], root)  # re-read: the owner may change it mid-turn
    level = setting["level"]
    if level == "off":
        return _text(rid, OFF), sid
    if level == "read" and name not in READ_TOOLS and name not in NAV_TOOLS:
        return _text(rid, READ_LEVEL.format(name=name)), sid
    mode = _mode_of(upstream, setting)
    if name == PLAN_TOOL:
        return _plan(rid, arguments, ctx, setting, mode, root), sid
    st = _state(upstream)
    args = _clean(arguments, st["schemas"].get(name))
    with st["lock"]:  # one call at a time per browser: typing and its click never race
        if "url" in args:
            refusal = _url_refusal(args["url"], mode, root)
            if refusal:
                return _text(rid, refusal), sid
        if st["blocked"] and name not in NAV_TOOLS:
            return _text(rid, st["blocked"]), sid
        if name in COMMIT_TOOLS and st["typed"]:
            reply, sid = _send(rid, name, args, sid, ctx, upstream, st, setting, mode, root)
            refusal = _after_nav(upstream, st, mode, rid, sid, ctx, root) if _ok(reply) else None
            return (_text(rid, refusal) if refusal else reply), sid
        if name in TYPE_TOOLS:  # counted before it runs: a failed fill still leaves the field touched
            text = args.get("text")
            st["typed"] = _join(st["typed"], " ".join(text.split()) if isinstance(text, str) else "")
        reply, sid = _forward(upstream, name, args, rid, sid, ctx)
        if name in (OPEN, NEW_TAB):
            st["aside"], st["typed"] = _join(st["aside"], st["typed"]), ""
        elif name in RESTORES:
            st["typed"], st["aside"] = _join(st["aside"], st["typed"]), ""
        if name in NAV_TOOLS or name in COMMIT_TOOLS or "url" in args:
            refusal = _after_nav(upstream, st, mode, rid, sid, ctx, root)
            if refusal:
                return _text(rid, refusal), sid
        return reply, sid


def _strip_schema(tool, schemas):
    schema = tool.get("inputSchema")
    if isinstance(schema, dict) and isinstance(schema.get("properties"), dict):
        schemas[tool["name"]] = frozenset(k for k in schema["properties"] if isinstance(k, str))
        schema["properties"] = {k: v for k, v in schema["properties"].items() if not _dropped(k)}
        if isinstance(schema.get("required"), list):
            schema["required"] = [k for k in schema["required"] if k in schema["properties"]]
    return tool


def handle(frame, ctx, mcp_session_id, upstream, root=bots.ROOT):
    """One JSON-RPC frame from a bot -> (reply or None, mcp-session-id)."""
    if not isinstance(frame, dict):
        return _error(None, -32600, "invalid request"), mcp_session_id
    method = frame.get("method") or ""
    if method.startswith("notifications/"):
        return upstream.forward(frame, mcp_session_id)
    if method not in METHODS:
        return _error(frame.get("id"), -32601, "method not available"), mcp_session_id
    if method == "tools/call":
        return _gate(frame, ctx, mcp_session_id, upstream, root)
    reply, sid = upstream.forward(frame, mcp_session_id)
    if method == "tools/list" and isinstance(reply, dict) and isinstance(reply.get("result"), dict):
        tools = reply["result"].get("tools") or []
        schemas = _state(upstream)["schemas"]  # the engine's own inputs per tool, for _clean
        reply["result"]["tools"] = [_strip_schema(t, schemas) for t in tools
                                    if isinstance(t, dict) and t.get("name") in KNOWN] + [dict(PLAN_SCHEMA)]
    return reply, sid
