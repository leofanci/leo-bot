"""apps_relay.py — the only connected-apps endpoint a bot can reach.

An app turn's `claude -p` gets one HTTP MCP server pointing back at the
dashboard (POST /api/internal/apps/mcp) with a per-request turn token. The
relay forwards to the person's Composio session for that workspace (pinned
to the accounts the workspace picked), adding the key, and gates every call:

  tools/list   only the search, schema and multi-execute meta tools
  tools/call   each app tool inside a multi-execute: read -> runs now,
               a write on an "act" grant (not a delete, not paid) -> runs now and
               is logged in Approvals as done, not a read on a write grant -> held
               in Approvals (several in one call share a batch: one Approve all),
               a Report only job refuses every non-read, else refused;
               with several accounts of one app, the call names one of them

The bot's body is never forwarded as is: reads are rebuilt into a fresh
multi-execute, so extra fields (workbench sync, ...) never reach Composio.
The gate talks to an `Upstream` (one `forward` method), so other MCP servers
can sit behind it later without touching it.
"""
import hmac
import secrets
import threading
import time

from core import app_rules, approvals, bots, composio, integrations

MAX_AGE = 1800  # hard cap; a token normally dies with its chat request
METHODS = ("initialize", "ping", "tools/list", "tools/call")
PASS_THROUGH = ("COMPOSIO_SEARCH_TOOLS", "COMPOSIO_GET_TOOL_SCHEMAS")
HELD = "Held for approval ({id}): {title}. It has not run. Tell the user it is waiting in Approvals."
HELD_BATCH = ("{n} actions wait for one approval (Approve all in Approvals): {titles}. None has run. "
              "Tell the user the plan is waiting for one click.")
RAN = "Ran on its own: {title}. Say in your reply what went out."
CONNECT = ('"{slug}": you have no {toolkit} access in this workspace. Ask the owner to connect a {toolkit} '
           'account in Apps and give you access, then stop. This call was not performed.')
REFUSED = '"{slug}" is not available to this bot. This call was not performed.'
REPORT_ONLY = '"{slug}" is not a read, and this scheduled job only reports. This call was not performed.'
UNREADABLE = "This call could not be read as a list of app tools. It was not performed."
NO_ACCOUNT = ('"{slug}": {toolkit} has no account picked in this workspace. Ask the owner to connect a {toolkit} '
              'account in Apps, then stop. This call was not performed.')
PICK_ACCOUNT = ('"{slug}": set "account" on the tool to one of this workspace\'s {toolkit} accounts: '
                '{choices}. This call was not performed.')


class Tokens:
    """Turn tokens: one per chat request, bound to {ws, bot, chat, room, person}."""

    def __init__(self):
        self._live = {}
        self._lock = threading.Lock()

    def mint(self, ctx):
        token = secrets.token_hex(32)
        with self._lock:
            self._live[token] = (dict(ctx), time.time())
        return token

    def lookup(self, token):
        if not isinstance(token, str) or not token:
            return None
        now = time.time()
        with self._lock:
            for live, (ctx, made) in list(self._live.items()):
                if now - made > MAX_AGE:
                    del self._live[live]
                elif hmac.compare_digest(live.encode(), token.encode()):  # bytes: str raises on non-ASCII
                    return dict(ctx)
        return None

    def revoke(self, token):
        with self._lock:
            self._live.pop(token, None)

    def revoke_where(self, **match):
        """Revoke every live token whose context holds all of these values."""
        with self._lock:
            for live, (ctx, _) in list(self._live.items()):
                if all(ctx.get(k) == v for k, v in match.items()):
                    del self._live[live]


TOKENS = Tokens()


class Upstream:
    def forward(self, frame, mcp_session_id):
        """(JSON-RPC reply or None for a notification, mcp-session-id)."""
        raise NotImplementedError


class ComposioUpstream(Upstream):
    def __init__(self, person, ws, root=bots.ROOT):
        self.person, self.ws, self.root = person, ws, root

    def forward(self, frame, mcp_session_id):
        sess = composio.ensure_session(self.person, self.ws, self.root)
        try:
            return composio.forward(sess["mcp_url"], frame, mcp_session_id, self.root)
        except composio.ComposioError as e:
            if e.status not in (401, 404):
                raise
        # The session is gone (nothing ran): make a new one and send once more.
        composio.reset_session(self.person, self.ws, self.root)
        sess = composio.ensure_session(self.person, self.ws, self.root)
        return composio.forward(sess["mcp_url"], frame, None, self.root)


def choices_text(ids, root=bots.ROOT):
    known = integrations.known(root)
    return ", ".join(f'"{i}" ({(known.get(i) or {}).get("label") or i})' for i in ids)


def pick_account(call, toolkit, picks, root=bots.ROOT):
    """(account id or None, refusal or None) for one call on a workspace's picks."""
    ids = picks.get(toolkit) or []
    if not ids:
        return None, NO_ACCOUNT.format(slug=call["tool_slug"], toolkit=toolkit)
    asked = call.get("account")
    if len(ids) == 1 and asked in (None, "", ids[0]):
        return ids[0], None
    if asked in ids:
        return asked, None
    return None, PICK_ACCOUNT.format(slug=call["tool_slug"], toolkit=toolkit, choices=choices_text(ids, root))


def _error(rid, code, message):
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}


def _text(rid, lines, is_error=False):
    return {"jsonrpc": "2.0", "id": rid,
            "result": {"content": [{"type": "text", "text": t} for t in lines], "isError": is_error}}


def _gate(frame, ctx, sid, upstream, root, catalog, read_only=None):
    rid = frame.get("id")
    params = frame.get("params") if isinstance(frame.get("params"), dict) else {}
    name = params.get("name")
    if name in PASS_THROUGH:  # rebuilt like a read: only the name and arguments travel
        return upstream.forward({"jsonrpc": "2.0", "id": rid, "method": "tools/call",
                                 "params": {"name": name, "arguments": params.get("arguments")}}, sid)
    if name != app_rules.MULTI_EXECUTE:
        return _text(rid, [REFUSED.format(slug=name)], True), sid
    if callable(ctx.get("on_call")):
        ctx["on_call"]()
    calls = app_rules.calls_in(name, params.get("arguments"))
    if calls is None:
        return _text(rid, [UNREADABLE], True), sid
    grant = integrations.grant(ctx["ws"], ctx["bot"], root)  # re-read: grants change mid-turn
    picks = integrations.accounts_for(ctx["ws"], root)  # and so do a workspace's accounts
    if catalog is not None:
        cat, paid = list(catalog), frozenset()
    else:
        items = composio.catalog(root)
        cat, paid = [a["slug"] for a in items], composio.paid_read_toolkits(items)
    reads, notes, hold, runs = [], [], [], []
    read_only = read_only or (lambda slug: composio.tool_read_only(slug, root))
    for c in calls:
        verdict, tk = app_rules.decide(c["tool_slug"], grant, cat, c["arguments"], read_only, paid)
        if verdict == "refuse":
            # An app Leo Bot knows but this bot may not use here: the owner can fix that in Apps.
            ok = tk and tk not in app_rules.BLOCKED_TOOLKITS and tk not in grant
            notes.append(CONNECT.format(slug=c["tool_slug"], toolkit=tk) if ok else REFUSED.format(slug=c["tool_slug"]))
            continue
        account, refusal = pick_account(c, tk, picks, root)
        if refusal:
            notes.append(refusal)
        elif verdict == "read" or (verdict == "run" and not ctx.get("report")):
            # The account travels only when there is a choice: one account is the pin.
            reads.append({"tool_slug": c["tool_slug"], "arguments": c["arguments"],
                          **({"account": account} if len(picks[tk]) > 1 else {})})
            if verdict == "run":
                runs.append((tk, c, account))
        elif ctx.get("report"):  # a Report only job: nothing waits for a click, nothing goes out
            notes.append(REPORT_ONLY.format(slug=c["tool_slug"]))
        else:
            hold.append((tk, c, account))
    batch = secrets.token_hex(6) if len(hold) > 1 else None
    held = [approvals.add_app_action(root, ws=ctx["ws"], room=ctx.get("room"), chat=ctx.get("chat"),
                                     bot=ctx["bot"], person=ctx["person"], toolkit=tk, slug=c["tool_slug"],
                                     arguments=c["arguments"], account=account, batch=batch)
            for tk, c, account in hold]
    if batch:
        notes.append(HELD_BATCH.format(n=len(held), titles="; ".join(r["preview"]["title"] for r in held[:5])
                                       + (f"; and {len(held) - 5} more" if len(held) > 5 else "")))
    else:
        notes += [HELD.format(id=r["id"], title=r["preview"]["title"]) for r in held]
    if not reads:
        return _text(rid, notes, not held), sid
    rebuilt = {"jsonrpc": "2.0", "id": rid, "method": "tools/call",
               "params": {"name": app_rules.MULTI_EXECUTE, "arguments": {"tools": reads}}}
    if runs:
        return _forward_runs(rebuilt, sid, upstream, ctx, runs, notes, root)
    if not notes:
        return upstream.forward(rebuilt, sid)
    # What was held or refused is told even when the reads fail: a bot that never hears its
    # write was held asks again, and a second card queues the same action twice.
    try:
        reply, sid = upstream.forward(rebuilt, sid)
    except composio.ComposioError as e:
        return _text(rid, [f"The reads did not run ({e})."] + notes, True), sid
    if isinstance(reply, dict) and isinstance(reply.get("result"), dict):
        reply["result"].setdefault("content", []).extend({"type": "text", "text": n} for n in notes)
    elif isinstance(reply, dict):
        failed = (reply.get("error") or {}).get("message") if isinstance(reply.get("error"), dict) else None
        return _text(rid, [f"The reads did not run ({str(failed or 'no reply')[:200]})."] + notes, True), sid
    return reply, sid


def _forward_runs(rebuilt, sid, upstream, ctx, runs, notes, root):
    """Send reads with the writes an "act" grant runs on its own, and log each write in Approvals
    (status done, or the may-have-run note when the call broke), so the owner sees what went out."""
    try:
        reply, sid = upstream.forward(rebuilt, sid)
        status, line = "done", "Ran on its own."
    except composio.ComposioError as e:
        reply, status, line = None, "failed", approvals.STALE_NOTE if e.status is None or e.status >= 500 else f"Failed: {e}"
    ok = isinstance(reply, dict) and isinstance(reply.get("result"), dict)
    if reply is not None and not ok:  # an error reply: some calls may have gone out
        status, line = "failed", approvals.STALE_NOTE
    done = [approvals.log_app_run(root, ws=ctx["ws"], room=ctx.get("room"), chat=ctx.get("chat"), bot=ctx["bot"],
                                  person=ctx["person"], toolkit=tk, slug=c["tool_slug"], arguments=c["arguments"],
                                  account=account, status=status, result=line)
            for tk, c, account in runs]
    if status == "done":
        notes = [RAN.format(title=r["preview"]["title"]) for r in done] + notes
    if not ok:
        return _text(rebuilt.get("id"), [f"The calls did not finish ({line})"] + notes, True), sid
    reply["result"].setdefault("content", []).extend({"type": "text", "text": n} for n in notes)
    return reply, sid


def handle(frame, ctx, mcp_session_id, upstream, root=bots.ROOT, catalog=None, read_only=None):
    """One JSON-RPC frame from a bot -> (reply or None, mcp-session-id). `catalog` and
    `read_only` stand in for Composio's toolkit list and tool tags (tests)."""
    if not isinstance(frame, dict):
        return _error(None, -32600, "invalid request"), mcp_session_id
    method = frame.get("method") or ""
    if method.startswith("notifications/"):
        return upstream.forward(frame, mcp_session_id)
    if method not in METHODS:
        return _error(frame.get("id"), -32601, "method not available"), mcp_session_id
    if method == "tools/call":
        return _gate(frame, ctx, mcp_session_id, upstream, root, catalog, read_only)
    reply, sid = upstream.forward(frame, mcp_session_id)
    if method == "tools/list" and isinstance(reply, dict) and isinstance(reply.get("result"), dict):
        tools = reply["result"].get("tools") or []
        reply["result"]["tools"] = [t for t in tools if isinstance(t, dict) and t.get("name") in app_rules.META_EXPOSED]
    return reply, sid
