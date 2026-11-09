"""composio.py — stdlib client for the user's own Composio project (connected apps).

Only the dashboard server calls this; the key never leaves its process. Every
request goes through one opener that refuses redirects (urllib would copy the
x-api-key header to the redirect target). `execute` is never retried: a retry
after a timeout can send an email twice.

Response shapes follow Composio's v3.1 docs (Oct 2026); each endpoint parses in
one `_parse_*` helper so a live check can correct it in one place.
"""
import http.client
import ipaddress
import json
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request

from core import app_rules, bots, integrations

BASE = "https://backend.composio.dev/api/v3.1"
TIMEOUT = 30
CATALOG_TTL = 3600
CATALOG_MAX_PAGES = 20
ACCOUNTS_MAX_PAGES = 10
# Composio's answer for an app it has no shared login for, and what using your own takes.
NO_MANAGED_AUTH = "ToolRouterV2_NoManagedAuth"
OAUTH_REDIRECT = "https://backend.composio.dev/api/v3/toolkits/auth/callback"
AUTH_CONFIGS_PAGE = "https://dashboard.composio.dev/~/project/auth-configs"
_AUTH_CONFIG_RE = re.compile(r"ac_[A-Za-z0-9_-]{1,64}")
_CATALOG = {}  # {"at": ts, "items": [...]}: one install, one Composio project
LOGO_MAX_BYTES = 256 * 1024
LOGO_TYPES = ("image/png", "image/jpeg", "image/webp", "image/gif", "image/svg+xml",
              "image/x-icon", "image/vnd.microsoft.icon")
_LOGOS = {}  # slug -> (bytes, content type) or None; capped below
LOGO_CACHE_MAX = 1000
# Account names come from the provider (a display name anyone in a shared
# workspace may set) and are quoted into the bot's prompt: plain text only.
_LABEL_DROP_RE = re.compile(r"[^\w .@+'-]+")
LABEL_MAX = 60


class ComposioError(Exception):
    def __init__(self, message, status=None, reason=None):
        super().__init__(message)
        self.status = status
        self.reason = reason  # Composio's error slug (e.g. APIKey_InvalidAPIKey), never its message


_REASON_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,63}")


def _reason(raw):
    """The error slug from a Composio error body. Its message can quote part of the key: dropped."""
    try:
        slug = (json.loads(raw.decode(errors="replace")).get("error") or {}).get("slug")
    except (ValueError, AttributeError):
        return None
    return slug if isinstance(slug, str) and _REASON_RE.fullmatch(slug) else None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # 3xx surfaces as HTTPError → ComposioError


_opener = urllib.request.build_opener(_NoRedirect)


def trusted_mcp_url(url):
    try:
        u = urllib.parse.urlsplit(url or "")
    except ValueError:
        return False
    host = (u.hostname or "").lower()
    return u.scheme == "https" and (host == "composio.dev" or host.endswith(".composio.dev"))


def _key(root):
    key = integrations.api_key(root)
    if not key:
        raise ComposioError("Composio is not configured")
    return key


def _send(method, url, body, root, headers=None, key=None):
    """(status, headers, raw body). Errors never carry request headers."""
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("x-api-key", key or _key(root))
    if data is not None:
        req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with _opener.open(req, timeout=TIMEOUT) as resp:
            return resp.status, resp.headers, resp.read()
    except urllib.error.HTTPError as e:
        try:
            reason = _reason(e.read(4096))
        except (OSError, http.client.HTTPException):
            reason = None
        raise ComposioError(f"Composio HTTP {e.code}", status=e.code, reason=reason) from None
    except (urllib.error.URLError, OSError, http.client.HTTPException) as e:  # TimeoutError is an OSError
        # A cut-off reply (IncompleteRead) is "unreachable" too: an execute may have run.
        raise ComposioError(f"Composio unreachable ({type(e).__name__})") from None


def _json(method, path, body, root):
    _, _, raw = _send(method, BASE + path, body, root)
    try:
        return json.loads(raw.decode() or "{}")
    except ValueError:
        raise ComposioError("Composio returned invalid JSON") from None


def check_key(key, root=bots.ROOT):
    """One cheap read with this key (not the saved one); ComposioError on refusal."""
    _send("GET", BASE + "/toolkits?limit=1", None, root, key=key)


def is_refused(err):
    return isinstance(err, ComposioError) and err.status in (401, 403)


def _parse_session(data):
    sid = data.get("session_id")
    url = (data.get("mcp") or {}).get("url")
    if not sid or not trusted_mcp_url(url):
        raise ComposioError("Composio returned an untrusted session")
    return {"session_id": sid, "mcp_url": url}


def _pin(accounts):
    return json.dumps(accounts, sort_keys=True)


def _session_body(user_id, accounts=None):
    body = {"user_id": user_id,
            "manage_connections": {"enable": False},
            "workbench": {"enable": False}}
    if accounts is None:
        # The link-only session: several accounts per app may be added.
        body["toolkits"] = {"disable": sorted(app_rules.BLOCKED_TOOLKITS)}
        body["multi_account"] = {"enable": True, "max_accounts_per_toolkit": integrations.MAX_ACCOUNTS,
                                 "require_explicit_selection": True}
        return body
    # A workspace sees only its picked accounts: toolkits outside the pin are off,
    # so Composio never falls back to another account of the same person.
    body["toolkits"] = {"enable": sorted(accounts)}
    body["connected_accounts"] = {tk: list(ids) for tk, ids in sorted(accounts.items())}
    most = max(len(ids) for ids in accounts.values())
    if most > 1:
        body["multi_account"] = {"enable": True, "max_accounts_per_toolkit": max(2, most),
                                 "require_explicit_selection": True}
    return body


def _session(pid, key, accounts, root):
    pin = _pin(accounts or {})
    saved = integrations.session(pid, key, root)
    if saved.get("session_id") and saved.get("pin") == pin and trusted_mcp_url(saved.get("mcp_url")):
        return {"session_id": saved["session_id"], "mcp_url": saved["mcp_url"]}
    user_id = integrations.person(pid, root)["composio_user_id"]
    body = _session_body(user_id, accounts)
    try:
        data = _json("POST", "/tool_router/session", body, root)
    except ComposioError as e:
        configs = {tk: own_auth_config(tk, root) for tk in accounts or {}} if e.reason == NO_MANAGED_AUTH else {}
        configs = {tk: c for tk, c in configs.items() if c}
        if not configs:
            raise
        data = _json("POST", "/tool_router/session", {**body, "auth_configs": configs}, root)
    sess = _parse_session(data)
    integrations.save_session(pid, key, sess["session_id"], sess["mcp_url"], pin, root)
    return sess


def ensure_session(pid, ws, root=bots.ROOT):
    """The person's locked-down session for one workspace, remade when its picks change."""
    _key(root)
    if ws == integrations.LINK_WS:  # never a bot's: it is pinned to nothing
        raise ComposioError("not a workspace")
    accounts = integrations.accounts_for(ws, root)
    if not accounts:
        raise ComposioError("no app account is picked for this workspace")
    return _session(pid, ws, accounts, root)


def _link_session(pid, root):
    _key(root)
    return _session(pid, integrations.LINK_WS, None, root)


def reset_session(pid, ws, root=bots.ROOT):
    """Forget a gone session (401/404); connections live on the Composio user."""
    integrations.forget_session(pid, ws, root)


def _auth_kind(item):
    if item.get("composio_managed_auth_schemes"):
        return "managed"
    if "API_KEY" in [str(s).upper() for s in item.get("auth_schemes") or []]:
        return "api_key"
    return "own_app"


def _parse_catalog_page(data):
    items = [{"slug": i["slug"], "name": i.get("name") or i["slug"],
              "logo": (i.get("meta") or {}).get("logo"), "auth": _auth_kind(i)}
             for i in data.get("items") or [] if isinstance(i, dict) and i.get("slug")]
    return items, data.get("next_cursor")


def catalog(root=bots.ROOT):
    """Every toolkit, most used first, blocklist removed; cached an hour."""
    if _CATALOG and time.time() - _CATALOG["at"] < CATALOG_TTL:
        return _CATALOG["items"]
    items, cursor = [], None
    for _ in range(CATALOG_MAX_PAGES):
        q = {"limit": "500", "sort_by": "usage", **({"cursor": cursor} if cursor else {})}
        page, cursor = _parse_catalog_page(_json("GET", "/toolkits?" + urllib.parse.urlencode(q), None, root))
        items += page
        if not cursor:
            break
    items = [i for i in items if i["slug"] not in app_rules.BLOCKED_TOOLKITS]
    _CATALOG.update(at=time.time(), items=items)
    return items


def own_auth_config(toolkit, root=bots.ROOT):
    """The id of this project's own enabled auth config for an app, or None."""
    q = urllib.parse.urlencode({"toolkit_slug": toolkit, "is_composio_managed": "false", "limit": "50"})
    for i in _json("GET", "/auth_configs?" + q, None, root).get("items") or []:
        if isinstance(i, dict) and i.get("status") == "ENABLED" and not i.get("is_composio_managed") \
                and (i.get("toolkit") or {}).get("slug") == toolkit \
                and isinstance(i.get("id"), str) and _AUTH_CONFIG_RE.fullmatch(i["id"]):
            return i["id"]
    return None


def _link(sid, toolkit, root):
    return _json("POST", f"/tool_router/session/{urllib.parse.quote(sid)}/link", {"toolkit": toolkit}, root)


def connect_link(pid, toolkit, root=bots.ROOT):
    """{"url", "account_id"}: a link that adds one more account of this app. An app
    with no Composio-managed login uses the project's own auth config for it, or
    returns {"needs_own_app": True, ...} with what setting one up takes."""
    try:
        data = _link(_link_session(pid, root)["session_id"], toolkit, root)
    except ComposioError as e:
        if e.reason != NO_MANAGED_AUTH:
            raise
        config = own_auth_config(toolkit, root)
        if not config:
            return {"needs_own_app": True, "redirect_uri": OAUTH_REDIRECT, "auth_configs_page": AUTH_CONFIGS_PAGE}
        # One-off session: the saved link session stays free of any one app's config.
        user_id = integrations.person(pid, root)["composio_user_id"]
        body = {**_session_body(user_id), "auth_configs": {toolkit: config}}
        data = _link(_parse_session(_json("POST", "/tool_router/session", body, root))["session_id"], toolkit, root)
    url = data.get("redirect_url") or data.get("url")
    if not url:
        raise ComposioError("Composio returned no connect link")
    account = data.get("connected_account_id")
    return {"url": url, "account_id": account if integrations.is_account_id(account) else None}


def clean_label(value):
    return " ".join(_LABEL_DROP_RE.sub(" ", value).split())[:LABEL_MAX] if isinstance(value, str) else ""


def _label(item):
    val = (item.get("state") or {}).get("val") or {}
    for v in (item.get("alias"), val.get("displayName") if isinstance(val, dict) else None,
              item.get("word_id"), item.get("id")):
        if clean_label(v):
            return clean_label(v)
    return ""


def _parse_accounts_page(data):
    rows = []
    for i in data.get("items") or []:
        if not isinstance(i, dict) or i.get("status") != "ACTIVE" or i.get("is_disabled"):
            continue
        tk = (i.get("toolkit") or {}).get("slug")
        if integrations.is_account_id(i.get("id")) and isinstance(tk, str) and tk \
                and tk not in app_rules.BLOCKED_TOOLKITS:
            rows.append({"id": i["id"], "toolkit": tk, "label": _label(i)})
    return rows, data.get("next_cursor")


def accounts(pid, root=bots.ROOT):
    """Every ACTIVE account of the person, oldest first: [{"id", "toolkit", "label"}]."""
    user_id = integrations.person(pid, root)["composio_user_id"]
    rows, cursor = [], None
    for _ in range(ACCOUNTS_MAX_PAGES):
        q = {"user_ids": user_id, "statuses": "ACTIVE", "limit": "100",
             "order_by": "created_at", "order_direction": "asc", **({"cursor": cursor} if cursor else {})}
        page, cursor = _parse_accounts_page(_json("GET", "/connected_accounts?" + urllib.parse.urlencode(q), None, root))
        rows += page
        if not cursor:
            break
    return rows


def rename_account(account_id, alias, root=bots.ROOT):
    if not integrations.is_account_id(account_id):
        raise ComposioError("bad account")
    _json("PATCH", f"/connected_accounts/{urllib.parse.quote(account_id)}", {"alias": alias}, root)


def disconnect_account(account_id, root=bots.ROOT):
    """Delete the connection in Composio: the app's access for it ends there."""
    if not integrations.is_account_id(account_id):
        raise ComposioError("bad account")
    _json("DELETE", f"/connected_accounts/{urllib.parse.quote(account_id)}", None, root)


def execute(pid, ws, slug, arguments, account=None, root=bots.ROOT):
    """Run one approved tool call, exactly once, on one picked account."""
    sid = ensure_session(pid, ws, root)["session_id"]
    body = {"tool_slug": slug, "arguments": arguments or {}}
    # One account of an app is the session's pin. Composio accepts "account" only
    # when the app has several to choose from (multi-account on), and refuses it otherwise.
    if account and any(account in ids and len(ids) > 1 for ids in integrations.accounts_for(ws, root).values()):
        body["account"] = account
    return _json("POST", f"/tool_router/session/{urllib.parse.quote(sid)}/execute", body, root)


def _public_host(host):
    """Every address the name resolves to is on the public internet."""
    try:
        infos = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except (OSError, UnicodeError):
        return False
    return bool(infos) and all(ipaddress.ip_address(i[4][0].split("%")[0]).is_global for i in infos)


_logo_opener = urllib.request.build_opener(_NoRedirect)


def logo(slug, root=bots.ROOT):
    """(bytes, content type) of an app's logo, fetched without the key, or None."""
    if slug in _LOGOS:
        return _LOGOS[slug]
    url = next((a.get("logo") for a in catalog(root) if a["slug"] == slug), None)
    try:
        u = urllib.parse.urlsplit(url if isinstance(url, str) else "")
    except ValueError:
        u = urllib.parse.urlsplit("")
    got = None
    if u.scheme == "https" and u.hostname and _public_host(u.hostname):
        try:
            with _logo_opener.open(urllib.request.Request(url), timeout=10) as resp:
                ctype = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
                raw = resp.read(LOGO_MAX_BYTES + 1)
            if ctype in LOGO_TYPES and len(raw) <= LOGO_MAX_BYTES:
                got = (raw, ctype)
        except (urllib.error.URLError, OSError, ValueError, http.client.HTTPException):
            pass
    if len(_LOGOS) >= LOGO_CACHE_MAX:
        _LOGOS.clear()
    _LOGOS[slug] = got  # a miss is remembered too: the panel shows the letter instead
    return got


def _parse_rpc(raw, ctype):
    text = raw.decode(errors="replace").strip()
    if not text:
        return None
    if "text/event-stream" in (ctype or ""):
        datas = [ln[5:].strip() for ln in text.splitlines() if ln.startswith("data:")]
        text = datas[-1] if datas else ""
        if not text:
            return None
    try:
        return json.loads(text)
    except ValueError:
        raise ComposioError("Composio MCP returned invalid JSON") from None


def forward(mcp_url, frame, mcp_session_id, root=bots.ROOT):
    """POST one JSON-RPC frame to the session MCP URL; (reply or None, session header)."""
    if not trusted_mcp_url(mcp_url):
        raise ComposioError("untrusted MCP URL")
    headers = {"Accept": "application/json, text/event-stream"}
    if mcp_session_id:
        headers["mcp-session-id"] = mcp_session_id
    _, resp_headers, raw = _send("POST", mcp_url, frame, root, headers)
    return (_parse_rpc(raw, resp_headers.get("Content-Type")),
            resp_headers.get("mcp-session-id") or mcp_session_id)
