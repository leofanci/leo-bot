"""browser_relay.py: the gate between a bot and its browser engine.

A bot's browser turn gets one MCP server (the relay) in front of an `Upstream`: the
agent-browser child for that bot. The relay decides what passes:

  tools/list   only the engine tools in core/browser_engine.py's four sets, plus PLAN_TOOL,
               without the engine-wide inputs a bot must not set
  tools/call   unknown tool: refused. Level off: refused. Level read: reads, navigation, and a
               click on a ref the last snapshot calls a link (refs go stale at every
               navigation or click).
               A url argument must be a full web address (or about:blank) whose site is not
               bound to the other browser mode; read takes no url. press passes only keys that
               type nothing, plus Enter; fill and type take one line of text. Typing is
               remembered; a commit (click, select, check, uncheck, Enter) after typing is a
               send: a Report only job never
               sends, `write` needs an approved send plan for that page and text, `act` sends on
               its own and is logged in Approvals. Both stop at the site's daily cap, counted
               across every bot and workspace. After navigation the engine's own current URL is
               checked again: a redirect onto a site bound to the other mode, or a page it cannot
               name, is hidden and left.

What was typed belongs to the tab it went into (Upstream.typed_key: a bot's own browser,
or that bot's own pinned tab in the owner's Chrome), not to an MCP session, and is kept on
disk (<root>/run/browser_typed.json) so a restart cannot clear it. Calls on one real browser
run one at a time (Upstream.lock_key: the owner Chrome is one lock for every my_chrome bot). Opening a page or a new tab
sets the typed text aside, except an open of the same page with a #fragment (the same
document, its fields keep their text); history, reload and tab moves bring it back, because
the browser restores what was in those fields, though not which field holds which. Typed
text is kept per field: fill replaces its field's text, type adds to it. A refused send
keeps it too: the text is still in the field, so a second click is still a send. On a
listed site, on any site while text is set aside, and after history brought text back,
every commit first reads the page's text field values (the relay's own
fixed eval, FIELDS_SCRIPT): one filled field is the send text whatever was typed, more than
one is refused. Every call but navigation first checks the page the engine is on, so a late
redirect onto a site bound to the other mode is never read or typed on. Residual risk: a send that
needs no typing (a one-click canned reply) is not detected, nor text in cross-origin
frames or closed shadow roots.
"""
import copy
import hashlib
import json
import math
import re
import threading
import unicodedata
from pathlib import Path
from urllib.parse import urlsplit

from core import approvals, bots, browser, browser_engine
from dashboard import shell_rules
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
MIN_TIMEOUT_MS, MAX_TIMEOUT_MS = 1000, 120000
NESTED_URL_KEYS = ("url", "href")  # only a top-level url is checked, so a nested one never travels
OPEN = "agent_browser_open"
NEW_TAB = "agent_browser_tab_new"
READ = "agent_browser_read"
PRESS = "agent_browser_press"
FILL = "agent_browser_fill"  # clears its field, then types (docs/browser.md, core tools)
CLICK = "agent_browser_click"
SNAPSHOT = "agent_browser_snapshot"
LINK_ROLE = "link"  # the role a snapshot's data.refs gives a link (probed on 0.41.0)
EVAL = "agent_browser_eval"  # the relay's own field check only: no bot ever gets eval
EVAL_TIMEOUT_MS = 10000
# The page's text field values, for the relay alone (docs/browser.md, "Reading field values").
# Text-like inputs, textareas and contenteditable roots, through open shadow roots and
# same-origin frames. Fixed text: nothing from the bot or the page is ever put into it.
FIELDS_SCRIPT = """(() => {
  const TEXT = new Set(["text", "email", "search", "tel", "url", "number"]);
  const out = [];
  const walk = (root) => {
    for (const el of root.querySelectorAll("*")) {
      const tag = el.tagName;
      if (tag === "INPUT") {
        if (TEXT.has(String(el.type).toLowerCase()) && el.value && el.value.trim()) out.push(String(el.value));
      } else if (tag === "TEXTAREA") {
        if (el.value && el.value.trim()) out.push(String(el.value));
      } else if (el.isContentEditable && !(el.parentElement && el.parentElement.isContentEditable)) {
        const v = el.innerText;
        if (v && v.trim()) out.push(String(v));
      }
      if (el.shadowRoot) walk(el.shadowRoot);
      if (tag === "IFRAME" || tag === "FRAME") {
        try { const d = el.contentDocument; if (d) walk(d); } catch (e) {}
      }
    }
  };
  walk(document);
  return {fields: out};
})()"""
# Keys that move focus or scroll and type nothing. Enter (alone or in a combo) is a send.
SAFE_KEYS = frozenset({"tab", "shift+tab", "arrowup", "arrowdown", "arrowleft", "arrowright",
                       "escape", "esc", "pageup", "pagedown", "home", "end"})
ENTER_KEYS = frozenset({"enter", "return", "numpadenter"})
RESTORES = frozenset("agent_browser_" + n for n in ("back", "forward", "reload", "tab_switch", "tab_close"))
MAX_PLAN = 20
MAX_TEXT = 10000
MODE_LABELS = {"bot": "the bot browser", "my_chrome": "the owner's Chrome"}
LEVEL_LABELS = {"write": "Read + write", "act": "Acts on its own"}

SITE_MODE = "{site} is set to open only in {mode_label}. This bot uses {other_label}, so it did not open it."
NO_PLAN = ("This would send what you typed, and no approved send plan matches it. Call leobot_plan_sends with "
           "the page address and the exact text, then stop: the owner approves it in Approvals.")
FIELD_NO_PLAN = ("A field on this page holds text, so this click could send it, and no approved send plan matches "
                 "that text. To send it, call leobot_plan_sends with the page address and the exact text, then "
                 "stop: the owner approves it in Approvals. Otherwise clear the field with fill and an empty text, "
                 "then click again. Nothing was clicked.")
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
BAD_KEY = ("This key could type or act on the page, so it was not pressed. Keys allowed: Tab, Shift+Tab, "
           "arrows, Escape, PageUp, PageDown, Home, End, and Enter to send. Use fill or type for text.")
BAD_TEXT = 'fill and type need "text": plain text on one line. This call was not performed.'
CONTROL_TEXT = ("Text with a line break or another control character is not typed, because it could press Enter. "
                "Type it as one line. This call was not performed.")
# In the owner's Chrome the engine sees every tab: a bot keeps to its own pinned tab.
CHROME_TAB_TOOLS = frozenset("agent_browser_" + n for n in ("tab_list", "tab_switch", "tab_close", "tab_new"))
CHROME_TABS = ("Tab tools are off in the owner's Chrome, so other tabs stay private. Use agent_browser_open in "
               "this bot's own tab. This call was not performed.")
# click's newTab opens the link in a new tab and moves the bot there (probed on 0.41.0): in the
# owner's Chrome it is a tab tool too, so it is refused there and left out of click's inputs.
NEW_TAB_ARG = "newTab"
CHROME_NEW_TAB = ("New tabs are off in the owner's Chrome, so this bot stays in its own tab. Click without "
                  "newTab, or open the link's address with agent_browser_open. This call was not performed.")
READ_URL = "Open the page first with agent_browser_open, then read it without a url. This call was not performed."
READ_CLICK = ("This bot may only read in the browser, so it clicks links only: take agent_browser_snapshot, then "
              "click a link by its @ref from that snapshot (a click or a page change makes the refs stale). "
              "This call was not performed.")
READ_SEND = ("A field on this page holds text, so this click could send it, and this bot may only read in the "
             "browser. Nothing was clicked.")
NO_TOOLS = "The browser's tools could not be read, so this call was not performed. Try again in a moment."
UNKNOWN_PAGE = "The browser could not tell which page it is on, so the result was withheld. Open a page to go on."
NO_CHECK = "The page could not be checked for typed text, so nothing was clicked."
HIDDEN_TYPED = ("What you typed is not in any field this page shows, so it could go out unseen. Open the page "
                "again, type the message into its visible box, then try again. Nothing was clicked.")
TYPED_MISMATCH = ("The text in this page's field is not what you typed, so this could send something else. Open the "
                  "page again, type the message once into its box, then try again. Nothing was clicked.")
TOO_LONG = "The text in this page's field is too long to check, so nothing was clicked."
# Characters a field can hold that show nothing: a value of only these counts as empty.
_INVISIBLE = dict.fromkeys([0x200B, 0x200C, 0x200D, 0x2060, 0xFEFF, 0x00AD])
MANY_FIELDS = ("More than one field on this page holds text, so this could send any of it. Clear the other "
               "fields with fill and an empty text, keep only the message, then try again. Nothing was clicked.")

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


MAX_SESSION = 24
_SLUG = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")  # no "--" and no dash at either end
_HASHED = re.compile(r"h[0-9a-f]{8}")  # the shape of a hashed part: such an id is hashed too


def session_part(value):
    """A workspace or bot id as one part of a session name: itself when it is a plain slug,
    else "h" + 8 hex of its sha256, so "<ws>--<bot>" always names one pair."""
    text = value if isinstance(value, str) else str(value)
    if _SLUG.fullmatch(text) and not _HASHED.fullmatch(text):
        return text
    return "h" + hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()[:8]


_EPOCH = re.compile(r"[0-9a-f]{6}")
_PLAIN = re.compile(r"(?P<ws>[a-z0-9]+(?:-[a-z0-9]+)*)-(?P<epoch>[0-9a-f]{6})--(?P<bot>[a-z0-9]+(?:-[a-z0-9]+)*)"
                    r"(?P<chrome>--chrome)?")
_LONG = re.compile(r"x(?P<epoch>[0-9a-f]{6})[0-9a-f]{10}(?P<chrome>c?)")


def session_name(ws, bot, mode, epoch=None):
    """bot: "<ws>-<epoch>--<bot>"; my_chrome: "<ws>-<epoch>--<bot>--chrome". The epoch is the
    workspace's (core/browser.py), so a recreated slug gets new names. A name over MAX_SESSION
    characters becomes "x" + epoch + 10 hex of its sha256 (+ "c" for my_chrome): no "--" in it,
    so it never equals a plain name, the two modes still differ, and its epoch stays readable.
    The name does not depend on HOME. Without an epoch (tests, old names): "<ws>--<bot>", and a
    long one is "x" + 16 hex."""
    head = session_part(ws) if epoch is None else f"{session_part(ws)}-{epoch}"
    base = f"{head}--{session_part(bot)}"
    name = base if mode == "bot" else base + "--chrome"
    if len(name) <= MAX_SESSION:
        return name
    digest = hashlib.sha256(base.encode()).hexdigest()
    short = "x" + (digest[:16] if epoch is None else epoch + digest[:10])
    return short if mode == "bot" else short + "c"


def parse_session(name):
    """(ws part or None, epoch, mode) of a Leo Bot session name with an epoch, else None.
    A long (hashed) name gives its epoch but no ws part."""
    m = _PLAIN.fullmatch(name) if isinstance(name, str) and len(name) <= MAX_SESSION else None
    if m:
        return m["ws"], m["epoch"], "my_chrome" if m["chrome"] else "bot"
    m = _LONG.fullmatch(name) if isinstance(name, str) else None
    if m:
        return None, m["epoch"], "my_chrome" if m["chrome"] else "bot"
    return None


def browser_key_for(mode, ws, bot, session=None):
    """Which real browser a bot drives: its own bot browser ("bot:<session>", so a new epoch is a
    new browser), or the one owner Chrome every my_chrome bot shares. The call lock follows this
    key (lock_key); in bot mode typed text does too."""
    if mode == "my_chrome":
        return "my_chrome"
    return f"bot:{session}" if session else f"bot:{ws}--{bot}"


def typed_key_for(mode, ws, bot, session=None):
    """Where typed text lives: the bot browser, or one bot's own pinned tab in the owner's
    Chrome ("my_chrome:<session>"), so another bot's tab never moves it."""
    if mode != "my_chrome":
        return browser_key_for(mode, ws, bot, session)
    return f"my_chrome:{session or session_name(ws, bot, mode)}"


PROTOCOL = "2024-11-05"  # the MCP version spoken to the engine child


def local_reply(frame):
    """The reply an upstream gives itself, without its engine: initialize and ping. None for
    any other method (notifications are the caller's to drop)."""
    rid, method = frame.get("id"), frame.get("method")
    if method == "initialize":
        params = frame.get("params") if isinstance(frame.get("params"), dict) else {}
        version = params.get("protocolVersion")
        return {"jsonrpc": "2.0", "id": rid, "result": {
            "protocolVersion": version if isinstance(version, str) and version else PROTOCOL,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "agent-browser", "version": browser_engine.VERSION}}}
    if method == "ping":
        return {"jsonrpc": "2.0", "id": rid, "result": {}}
    return None


class Upstream:
    """The browser behind the gate. `mode` is "bot" or "my_chrome". `lock_key` (alias
    `browser_key`) is browser_key_for(mode, ws, bot); `typed_key` is typed_key_for(...)."""
    mode = "bot"
    browser_key = None
    lock_key = None
    typed_key = None

    def forward(self, frame, mcp_session_id):
        """(JSON-RPC reply or None for a notification, mcp-session-id)."""
        raise NotImplementedError

    def current_url(self):
        """The engine's own URL for the active tab, or None when it cannot tell."""
        raise NotImplementedError

    def known_tools(self):
        """The engine's last tools/list (a list of tools) without starting anything, or None."""
        return None


_BROWSER_LOCKS = {}


def _state(upstream):
    """Per browser: the page block and the engine's tool schemas."""
    with _LOCK:
        st = getattr(upstream, "_gate_state", None)
        if st is None:
            st = {"blocked": None, "schemas": {}, "refs": {}}  # refs: {ref: role} of the last snapshot
            upstream._gate_state = st
        return st


def _browser_lock(key):
    """One call at a time per real browser, so typing and its click never race, even
    between two bots in the same owner Chrome."""
    with _LOCK:
        return _BROWSER_LOCKS.setdefault(key, threading.Lock())


def _keys_of(upstream, mode, ctx, root=bots.ROOT):
    """(lock key, typed key) of this upstream, or the ones its mode and bot give."""
    def given(name):
        key = getattr(upstream, name, None)
        return key if isinstance(key, str) and key else None
    epoch = browser.epochs(root).get(ctx["ws"])  # the upstream's own keys carry it; this is the fallback
    session = session_name(ctx["ws"], ctx["bot"], mode, epoch) if epoch else None
    lock = given("lock_key") or given("browser_key") or browser_key_for(mode, ctx["ws"], ctx["bot"], session)
    typed = given("typed_key") or (lock if mode != "my_chrome" else typed_key_for(mode, ctx["ws"], ctx["bot"], session))
    return lock, typed


def _typed_path(root):
    return Path(root) / "run" / "browser_typed.json"


def _typed_all(root):
    """{browser key: {"typed", "aside", "by"}}; None when the file exists but cannot be read."""
    try:
        data = json.loads(_typed_path(root).read_text())
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


UNKNOWN_FIELD = ""  # "by" key of text whose field is not known (brought back by history or a tab move)
NO_SELECTOR = "?focus"  # "by" key of a type or fill call that names no selector
MAX_SELECTOR_KEY = 200


def _field_key(selector):
    """The "by" key of one type or fill target: "s:" + the selector, a long one "h:" + its
    sha256. The prefixes keep every key a bot can name apart from the others, so no selector
    it picks can reach text typed through another."""
    if not isinstance(selector, str) or not selector:
        return NO_SELECTOR
    if len(selector) > MAX_SELECTOR_KEY:
        return "h:" + hashlib.sha256(selector.encode("utf-8", "surrogatepass")).hexdigest()
    return "s:" + selector


def _typed_of(by):
    """All typed text of a tab: each field's text, in the order it was first typed into."""
    out = ""
    for text in by.values():
        out = _join(out, text)
    return out


def typed_state(key, root=bots.ROOT):
    """{"typed", "aside", "by"} of one browser, kept on disk so a restart does not clear it, or
    None when the file is corrupt (the gate then refuses sends: it cannot tell). "by" is
    {field: text} of this page: fill replaces its field's text, type adds to it, and text
    whose field is unknown sits under UNKNOWN_FIELD. "typed" is all of it joined."""
    data = _typed_all(root)
    if data is None:
        return None
    row = data.get(key) if isinstance(data.get(key), dict) else {}
    out = {k: row[k] if isinstance(row.get(k), str) else "" for k in ("typed", "aside")}
    by = row.get("by")
    if isinstance(by, dict):
        out["by"] = {k: v for k, v in by.items() if isinstance(k, str) and isinstance(v, str) and v}
    else:  # a row written before "by": its text's field is unknown
        out["by"] = {UNKNOWN_FIELD: out["typed"]} if out["typed"] else {}
    out["typed"] = _typed_of(out["by"])
    return out


def _save_typed(key, tt, root):
    """Write one browser's typed text; a corrupt file is replaced by a clean one."""
    by = tt.get("by")
    if by is None:  # a caller that knows only the text: its field is unknown
        by = {UNKNOWN_FIELD: tt["typed"]} if tt.get("typed") else {}
    by = {k: v for k, v in by.items() if v}
    typed = _typed_of(by)
    with _LOCK:
        data = _typed_all(root) or {}
        if typed or tt["aside"]:
            data[key] = {"typed": typed, "aside": tt["aside"], "by": by}
        else:
            data.pop(key, None)
        _typed_path(root).parent.mkdir(parents=True, exist_ok=True)
        bots._atomic_write(_typed_path(root), json.dumps(data, indent=2) + "\n")


def forget_typed(sessions, root=bots.ROOT):
    """A tidied session's typed text goes: rows keyed exactly "bot:<s>" or "my_chrome:<s>" for
    each of these sessions, never another's."""
    keys = {f"{kind}:{s}" for s in sessions for kind in ("bot", "my_chrome")}
    with _LOCK:
        data = _typed_all(root)
        if not data or not keys & set(data):
            return
        for k in keys:
            data.pop(k, None)
        bots._atomic_write(_typed_path(root), json.dumps(data, indent=2) + "\n")


def _put_aside(tt):
    """A new page: what was typed is set aside (it may still sit in a field after the move)."""
    tt["aside"], tt["typed"], tt["by"] = _join(tt["aside"], tt["typed"]), "", {}


def _bring_back(tt):
    """History, reload or a tab move: the browser restores those fields, so everything set aside
    or typed is typed again, in fields this gate cannot name."""
    text = _join(tt["aside"], tt["typed"])
    tt["typed"], tt["aside"], tt["by"] = text, "", ({UNKNOWN_FIELD: text} if text else {})


def set_aside(key, root=bots.ROOT):
    """A new page the gate did not open itself (the upstream's tab_gone recovery, or the gate's
    own move to about:blank): the typed text is set aside as for agent_browser_open. Called
    inside that browser's call, so it takes no browser lock."""
    tt = typed_state(key, root)
    if tt is None or not tt["typed"]:
        return
    _put_aside(tt)
    _save_typed(key, tt, root)


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


def _no_nested_urls(value):
    if isinstance(value, dict):
        return {k: _no_nested_urls(v) for k, v in value.items() if k not in NESTED_URL_KEYS}
    if isinstance(value, list):
        return [_no_nested_urls(v) for v in value]
    return value


def _clean(arguments, declared=None):
    """The arguments that may travel: never the engine-wide ones; with the tool's schema
    known, only the keys it declares. A url or href below the top level, or an href at it,
    is dropped (only the top-level url is checked). timeoutMs is held to 1 to 120 seconds."""
    out = {k: _no_nested_urls(v) for k, v in arguments.items()
           if not _dropped(k) and k != "href" and (declared is None or k in declared)}
    if "timeoutMs" in out:
        t = out["timeoutMs"]
        if isinstance(t, bool) or not isinstance(t, (int, float)) or not math.isfinite(t):
            del out["timeoutMs"]
        else:
            out["timeoutMs"] = int(min(max(t, MIN_TIMEOUT_MS), MAX_TIMEOUT_MS))
    return out


def _key_kind(key):
    """"send" for Enter (alone or in a combo), "safe" for a key that types nothing, else None."""
    if not isinstance(key, str) or not key.strip():
        return None
    parts = [p.strip().lower() for p in key.split("+")]
    if any(p in ENTER_KEYS for p in parts):
        return "send"
    return "safe" if "+".join(parts) in SAFE_KEYS else None


def _has_control(text):
    return any(unicodedata.category(c) == "Cc" or c in "\u2028\u2029" for c in text)


def _read_fields(upstream, rid, sid, ctx):
    """(non-empty field values or None when they cannot be read, sid): one eval of
    FIELDS_SCRIPT. An error, a timeout or any other reply shape is None."""
    try:
        reply, sid = _forward(upstream, EVAL, {"script": FIELDS_SCRIPT, "timeoutMs": EVAL_TIMEOUT_MS}, rid, sid, ctx)
    except Exception:
        return None, sid
    if not _ok(reply):
        return None, sid
    sc = reply["result"].get("structuredContent")
    resp = sc.get("response") if isinstance(sc, dict) else None
    data = resp.get("data") if isinstance(resp, dict) else None
    result = data.get("result") if isinstance(data, dict) else None
    fields = result.get("fields") if isinstance(result, dict) else None
    if not isinstance(fields, list) or not all(isinstance(f, str) for f in fields):
        return None, sid
    return [v for v in (" ".join(f.split()) for f in fields) if v.translate(_INVISIBLE).strip()], sid


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


def _page_refusal(upstream, mode, root, url=None):
    """A refusal line for the page the engine is on (`url` when already read), or None."""
    url = _current(upstream) if url is None else url
    if url is None:
        return UNKNOWN_PAGE
    return _mode_refusal(browser.site_of(url, root), mode, root)


def _after_nav(upstream, st, mode, rid, sid, ctx, root, bkey, url=None):
    """Check the page the engine really landed on (`url` when just read). On a site bound to
    the other mode, or when the engine cannot tell the page, the reply is withheld and the tab
    is sent to about:blank, with the typed text set aside as for an open; until a later
    navigation lands on an allowed page, only navigation passes."""
    refusal = _page_refusal(upstream, mode, root, url)
    st["blocked"] = refusal
    if refusal:
        try:
            _forward(upstream, OPEN, {"url": "about:blank"}, rid, sid, ctx)
        except Exception:
            pass
        st["refs"] = {}
        set_aside(bkey, root)  # the tab may have moved: what was typed is on the page it left
        if not _page_refusal(upstream, mode, root):
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


def _send(rid, name, arguments, sid, ctx, upstream, bkey, tt, typed, url, setting, mode, root, no_plan=NO_PLAN):
    """A commit after typing: it would send `typed` from the page at `url`. `no_plan`: the line
    when no plan matches (what the bot typed, or text that was already in the page's field)."""
    if ctx.get("report"):
        return _text(rid, REPORT_ONLY), sid
    unwalled = getattr(ctx.get("taint"), "unwalled_shell", None)
    if unwalled and setting["level"] in LEVEL_LABELS:  # a shell beside it could send past the gate
        return _text(rid, shell_rules.BROWSER_SHELL_OFF.format(
            bot=ctx["bot"], browser=LEVEL_LABELS[setting["level"]], shell=unwalled.get("shell", "a shell"),
            engine=unwalled.get("engine", "this engine"))), sid
    site = browser.site_of(url, root)
    refusal = _mode_refusal(site, mode, root)
    if refusal:
        return _text(rid, refusal), sid
    daily = (browser.sites(root).get(site) or {}).get("daily") if site else None
    status, rec = approvals.claim_send(root, ws=ctx["ws"], room=ctx.get("room"), chat=ctx.get("chat"),
                                       bot=ctx["bot"], person=ctx.get("person"), url=url, text=typed,
                                       daily=daily, planned=setting["level"] == "write")
    if status == "capped":
        return _text(rid, CAPPED.format(site=site, daily=daily)), sid
    if status != "ok":
        return _text(rid, no_plan), sid
    # From here the click is sent: whatever comes back, the typed text may have gone out.
    tt["typed"] = tt["aside"] = ""
    tt["by"] = {}
    _save_typed(bkey, tt, root)
    try:
        reply, sid = _forward(upstream, name, arguments, rid, sid, ctx)
    except Exception:
        reply = None
    approvals.end_send(rec["id"], _ok(reply), root)
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
    if level == "read" and name not in READ_TOOLS and name not in NAV_TOOLS and name != CLICK:
        return _text(rid, READ_LEVEL.format(name=name)), sid
    mode = _mode_of(upstream, setting)
    if mode == "my_chrome" and name in CHROME_TAB_TOOLS:
        return _text(rid, CHROME_TABS), sid
    if mode == "my_chrome" and arguments.get(NEW_TAB_ARG):
        return _text(rid, CHROME_NEW_TAB), sid
    if name == PLAN_TOOL:
        return _plan(rid, arguments, ctx, setting, mode, root), sid
    if name == READ and "url" in arguments:  # a fetch of another page is a hidden navigation
        return _text(rid, READ_URL), sid
    st = _state(upstream)
    lkey, bkey = _keys_of(upstream, mode, ctx, root)  # bkey: where this tab's typed text lives
    if not st["schemas"]:  # a fresh upstream: learn every tool's inputs before the first call passes
        with _browser_lock(lkey):
            sid = _seed_schemas(upstream, rid, sid)
        if not st["schemas"]:
            return _text(rid, NO_TOOLS), sid
    args = _clean(arguments, st["schemas"].get(name))
    if mode == "my_chrome":
        args.pop(NEW_TAB_ARG, None)
    key = _key_kind(args.get("key")) if name == PRESS else None
    if name == PRESS and key is None:
        return _text(rid, BAD_KEY), sid
    if name in TYPE_TOOLS:
        if not isinstance(args.get("text"), str):
            return _text(rid, BAD_TEXT), sid
        if _has_control(args["text"]):
            return _text(rid, CONTROL_TEXT), sid
    sends = name in COMMIT_TOOLS and key != "safe"  # click, select, check, uncheck, Enter
    with _browser_lock(lkey):
        if "url" in args:
            refusal = _url_refusal(args["url"], mode, root)
            if refusal:
                return _text(rid, refusal), sid
        if st["blocked"] and name not in NAV_TOOLS:
            return _text(rid, st["blocked"]), sid
        here = None
        if name not in NAV_TOOLS:  # a late redirect may have moved the tab since the last call
            here = _current(upstream)
            if here is None or here != st.get("refs_url"):  # moved by itself, or unknown: refs point anywhere
                st["refs"] = {}
            bad = UNKNOWN_PAGE if here is None else _mode_refusal(browser.site_of(here, root), mode, root)
            refusal = _after_nav(upstream, st, mode, rid, sid, ctx, root, bkey, here) if bad else None
            if refusal:
                return _text(rid, refusal), sid
        if level == "read" and name == CLICK:
            sel = args.get("selector")
            ref = sel[1:] if isinstance(sel, str) and sel.startswith("@") else None
            if ref is None or st["refs"].get(ref) != LINK_ROLE:
                return _text(rid, READ_CLICK), sid
        tt = typed_state(bkey, root)
        unknown_typed = tt is None
        if unknown_typed:  # a corrupt record: typing still works and writes a clean one
            tt = {"typed": "", "aside": "", "by": {}}
        if sends:
            if unknown_typed:
                return _text(rid, NO_CHECK), sid
            url = here if here is not None else _current(upstream)  # here: nothing has run since
            if url is None:
                return _text(rid, NO_PAGE if tt["typed"] else UNKNOWN_PAGE), sid
            typed = " ".join(tt["typed"].split())  # kept raw, compared normalized
            restored = tt["by"].get(UNKNOWN_FIELD)
            from_field = False
            if browser.site_of(url, root) or tt["aside"] or restored:
                # On a listed site the page's own fields decide: text can sit there without a
                # type call (prefilled, restored, typed onto), so it is what would go out. On any
                # site while text is set aside too: a page can keep it in a field after a move.
                # And after history or a tab move: the restored text's field is unknown.
                fields, sid = _read_fields(upstream, rid, sid, ctx)
                if fields is None:
                    return _text(rid, NO_CHECK), sid
                if len(fields) > 1:
                    return _text(rid, MANY_FIELDS), sid
                if fields and len(fields[0]) > MAX_TEXT:
                    return _text(rid, TOO_LONG), sid
                if typed and not fields:  # typed into something the read cannot see
                    return _text(rid, HIDDEN_TYPED), sid
                if restored and fields:
                    # Restored text may be another page's: the field holds what goes out, and
                    # each field this page was typed into since must be in it.
                    if any(" ".join(v.split()) not in fields[0] for k, v in tt["by"].items() if k != UNKNOWN_FIELD):
                        return _text(rid, TYPED_MISMATCH), sid
                elif typed and fields[0] != typed:
                    return _text(rid, TYPED_MISMATCH), sid
                if fields:
                    from_field, typed = not typed, fields[0]
            if typed and level == "read":  # nothing is typed at read: this is text the page holds
                return _text(rid, READ_SEND), sid
            if typed:
                reply, sid = _send(rid, name, args, sid, ctx, upstream, bkey, tt, typed, url, setting, mode, root,
                                   no_plan=FIELD_NO_PLAN if from_field else NO_PLAN)
                st["refs"] = {}
                refusal = _after_nav(upstream, st, mode, rid, sid, ctx, root, bkey) if _ok(reply) else None
                return (_text(rid, refusal) if refusal else reply), sid
        before = dict(tt)
        field = _field_key(args.get("selector")) if name in TYPE_TOOLS else None
        if name in TYPE_TOOLS:  # counted before it runs: a failed call still leaves the field touched
            # Added, raw and unseparated, so two parts of one word stay one word. A fill clears
            # its field first (engine facts), but only a fill that worked replaces the text below:
            # one that failed may have left the old text in place. Text under UNKNOWN_FIELD stays.
            by = dict(tt["by"])
            by[field] = by.get(field, "") + args["text"]
            if not by[field]:
                del by[field]
            if len(_typed_of(by)) > MAX_TEXT:  # more than any send could check
                return _text(rid, TOO_LONG), sid
            tt["by"], tt["typed"] = by, _typed_of(by)
            _save_typed(bkey, tt, root)
        same_doc = name == OPEN and _same_document(args.get("url"), _current(upstream) if "url" in args else None)
        reply, sid = _forward(upstream, name, args, rid, sid, ctx)
        if name == SNAPSHOT:  # refs only from a page the engine can name
            st["refs"], st["refs_url"] = (_refs_of(reply), here) if here is not None else ({}, None)
        elif name in NAV_TOOLS or name in COMMIT_TOOLS or "url" in args:
            st["refs"] = {}  # the page may have changed: an old ref could point anywhere
        fresh = typed_state(bkey, root)  # the upstream may have set it aside (set_aside)
        if fresh is not None:
            tt, before = fresh, dict(fresh)
        if name == FILL and _ok(reply) and field in tt["by"]:
            by = dict(tt["by"])
            if args["text"]:
                by[field] = args["text"]
            else:
                del by[field]
            tt["by"], tt["typed"] = by, _typed_of(by)
        if name in (OPEN, NEW_TAB) and not same_doc:
            _put_aside(tt)
        elif name in RESTORES:
            _bring_back(tt)
        if tt != before:
            _save_typed(bkey, tt, root)
        if name in NAV_TOOLS or name in COMMIT_TOOLS or "url" in args:
            refusal = _after_nav(upstream, st, mode, rid, sid, ctx, root, bkey)
            if refusal:
                return _text(rid, refusal), sid
        return reply, sid


def _same_document(target, current):
    """An open of `target` from `current` stays in the same document: a #fragment move on the
    same address (scheme, host, path and query alike). Such a page keeps its fields' text."""
    if not isinstance(target, str) or "#" not in target or not isinstance(current, str):
        return False
    try:
        a, b = urlsplit(target.strip()), urlsplit(current.strip())
    except ValueError:
        return False
    return (a.scheme.lower(), a.netloc.lower(), a.path or "/", a.query) == \
        (b.scheme.lower(), b.netloc.lower(), b.path or "/", b.query)


def _refs_of(reply):
    """{ref: role} from a snapshot reply's structuredContent.response.data.refs ({} when absent)."""
    if not _ok(reply):
        return {}
    sc = reply["result"].get("structuredContent")
    resp = sc.get("response") if isinstance(sc, dict) else None
    data = resp.get("data") if isinstance(resp, dict) else None
    refs = data.get("refs") if isinstance(data, dict) else None
    if not isinstance(refs, dict):
        return {}
    return {k: v["role"] for k, v in refs.items()
            if isinstance(k, str) and isinstance(v, dict) and isinstance(v.get("role"), str)}


def _seed_schemas(upstream, rid, sid):
    """Learn the engine's tool inputs: its last known tools/list, else one tools/list now."""
    try:
        tools = upstream.known_tools()
    except Exception:  # noqa: BLE001 - unknown is the same as none: ask the engine
        tools = None
    if not isinstance(tools, list) or not tools:
        try:
            reply, sid = upstream.forward({"jsonrpc": "2.0", "id": rid, "method": "tools/list", "params": {}}, sid)
        except Exception:  # noqa: BLE001 - the caller refuses when nothing was learned
            return sid
        result = reply.get("result") if isinstance(reply, dict) else None
        tools = result.get("tools") if isinstance(result, dict) else None
    if isinstance(tools, list):
        learn_schemas(upstream, tools)
    return sid


def _strip_schema(tool, schemas):
    schema = tool.get("inputSchema")
    if isinstance(schema, dict) and isinstance(schema.get("properties"), dict):
        schemas[tool["name"]] = frozenset(k for k in schema["properties"] if isinstance(k, str))
        schema["properties"] = {k: v for k, v in schema["properties"].items() if not _dropped(k)}
        if isinstance(schema.get("required"), list):
            schema["required"] = [k for k in schema["required"] if k in schema["properties"]]
    return tool


def learn_schemas(upstream, tools):
    """Fill the per-tool input allowlist from an engine tools/list, before any bot asks for
    one (the upstream calls it when its child starts)."""
    schemas = _state(upstream)["schemas"]
    for tool in tools:
        if isinstance(tool, dict) and isinstance(tool.get("name"), str):
            _strip_schema(copy.deepcopy(tool), schemas)


def handle(frame, ctx, mcp_session_id, upstream, root=bots.ROOT):
    """One JSON-RPC frame from a bot -> (reply or None, mcp-session-id)."""
    if not isinstance(frame, dict):
        return _error(None, -32600, "invalid request"), mcp_session_id
    method = frame.get("method")
    if not isinstance(method, str):
        return _error(frame.get("id"), -32600, "invalid request"), mcp_session_id
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
        setting = browser.bot_setting(ctx["ws"], ctx["bot"], root)
        chrome = _mode_of(upstream, setting) == "my_chrome"
        hidden = CHROME_TAB_TOOLS if chrome else frozenset()
        if setting["level"] == "read":  # always refused there; click stays (links only)
            hidden = hidden | ((TYPE_TOOLS | COMMIT_TOOLS) - {CLICK})
        # The plan tool only where a send can happen: at read (or off) it is refused anyway.
        plan = [dict(PLAN_SCHEMA)] if setting["level"] in ("write", "act") else []
        listed = [_strip_schema(t, schemas) for t in tools
                  if isinstance(t, dict) and t.get("name") in KNOWN and t["name"] not in hidden]
        if chrome:
            for t in listed:
                props = (t.get("inputSchema") or {}).get("properties")
                if isinstance(props, dict):
                    props.pop(NEW_TAB_ARG, None)
        reply["result"]["tools"] = listed + plan
    return reply, sid
