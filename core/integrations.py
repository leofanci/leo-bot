"""integrations.py — connected apps state (Composio), in gitignored team/integrations.json.

  composio.api_key     the user's own Composio project key; write-only (public() never returns it)
  people.<pid>         Composio user id (random, never personal) + one session per workspace
                       (sessions.<ws>), each pinned to that workspace's accounts
  known.<account id>   {"toolkit", "label"}: the ACTIVE accounts Composio listed last time
  accounts.<ws>.<tk>   account ids this workspace may use; nothing is shared automatically
  grants.<ws>.<bot>    toolkit slug -> "read" | "write" ("write" still waits on approval)

An account is connected once (HQ) and each workspace picks which accounts it
uses; a workspace session only ever sees its own picks, pinned by Composio.

A missing HQ Chief of Staff grant falls back to Gmail + Google Calendar; an
explicit {} means the user switched them off. Bots are denied Read on this file
(chat_session.SECRET_FILE_GLOBS).
"""
import json
import re
import secrets
import threading
import time
from pathlib import Path

from core import app_rules, bots

LOCK = threading.Lock()
SCOPES = ("read", "write")
DEFAULT_GRANTS = {bots.HQ: {bots.ROOT_BOT: {"gmail": "write", "googlecalendar": "write"}}}
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_ACCOUNT_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")
LINK_WS = "_link"  # the session that only makes connect links; never a bot's
MAX_ACCOUNTS = 10  # Composio's multi-account cap per toolkit and session


def _path(root):
    return Path(root) / "team" / "integrations.json"


def load(root=bots.ROOT):
    try:
        data = json.loads(_path(root).read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save(root, data):
    # mkstemp creates the temp file 0600 and os.replace keeps that mode.
    bots._atomic_write(_path(root), json.dumps(data, indent=2) + "\n")


def _update(root, fn):
    with LOCK:
        data = load(root)
        out = fn(data)
        _save(root, data)
        return out


def public(root=bots.ROOT):
    c = load(root).get("composio") or {}
    return {"configured": bool(c.get("api_key")), "saved": c.get("saved")}


def api_key(root=bots.ROOT):
    return (load(root).get("composio") or {}).get("api_key") or None


def set_key(key, root=bots.ROOT):
    if not isinstance(key, str) or not key.strip():
        raise ValueError("api_key required")
    _update(root, lambda d: d.__setitem__("composio", {"api_key": key.strip(), "saved": time.time()}))


def remove_key(root=bots.ROOT):
    """Forget the key and every session made with it; grants and picks stay."""
    def fn(d):
        d.pop("composio", None)
        for row in (d.get("people") or {}).values():
            for k in ("session_id", "mcp_url", "sessions"):
                row.pop(k, None)
    _update(root, fn)


def person(pid, root=bots.ROOT):
    def fn(d):
        row = d.setdefault("people", {}).setdefault(pid, {})
        row.setdefault("composio_user_id", "myos-" + secrets.token_hex(8))
        return json.loads(json.dumps(row))
    return _update(root, fn)


def session(pid, ws, root=bots.ROOT):
    """{"session_id", "mcp_url", "pin"} saved for this person and workspace, or {}."""
    return dict(((person(pid, root).get("sessions") or {}).get(ws)) or {})


def save_session(pid, ws, session_id, mcp_url, pin, root=bots.ROOT):
    person(pid, root)

    def fn(d):
        row = d["people"][pid]
        row.pop("session_id", None)  # one shared session from before workspaces had their own
        row.pop("mcp_url", None)
        row.setdefault("sessions", {})[ws] = {"session_id": session_id, "mcp_url": mcp_url, "pin": pin}
    _update(root, fn)


def forget_session(pid, ws, root=bots.ROOT):
    _update(root, lambda d: (((d.get("people") or {}).get(pid) or {}).get("sessions") or {}).pop(ws, None))


def is_account_id(value):
    return isinstance(value, str) and bool(_ACCOUNT_RE.fullmatch(value))


def note_known(rows, root=bots.ROOT):
    """Replace the known ACTIVE accounts with Composio's current list. An account
    seen for the first time joins HQ, where it was connected; a project only
    ever gets one by being picked there."""
    known = {r["id"]: {"toolkit": r["toolkit"], "label": r["label"]}
             for r in rows if is_account_id(r.get("id")) and _SLUG_RE.match(r.get("toolkit") or "")}

    def fn(d):
        d["known"] = known
        seen = set(d.get("seen") or [])
        hq = d.setdefault("accounts", {}).setdefault(bots.HQ, {})
        for aid, row in known.items():
            if aid in seen:
                continue
            ids = hq.setdefault(row["toolkit"], [])
            if aid not in ids and len(ids) < MAX_ACCOUNTS:
                ids.append(aid)
            seen.add(aid)
        d["seen"] = sorted(seen)
    _update(root, fn)
    return known


def set_label(account_id, label, root=bots.ROOT):
    def fn(d):
        row = (d.get("known") or {}).get(account_id)
        if row:
            row["label"] = label
    _update(root, fn)


def known(root=bots.ROOT):
    return dict(load(root).get("known") or {})


def assigned(root=bots.ROOT):
    """Every workspace's picks as stored (may name accounts Composio no longer lists)."""
    return json.loads(json.dumps(load(root).get("accounts") or {}))


def accounts_for(ws, root=bots.ROOT):
    """{toolkit: [account id, ...]} this workspace uses: its picks that are still ACTIVE."""
    data = load(root)
    live = data.get("known") or {}
    out = {}
    for tk, ids in ((data.get("accounts") or {}).get(ws) or {}).items():
        keep = [i for i in ids if (live.get(i) or {}).get("toolkit") == tk]
        if keep and tk not in app_rules.BLOCKED_TOOLKITS:
            out[tk] = keep
    return out


def set_account(ws, account_id, on, root=bots.ROOT):
    """Pick (or drop) one known account for a workspace; returns its picks."""
    if not is_account_id(account_id):
        raise ValueError("bad account")

    def fn(d):
        row = (d.get("known") or {}).get(account_id)
        if not row:
            raise ValueError("unknown account: reload Integrations")
        tk = row["toolkit"]
        picks = d.setdefault("accounts", {}).setdefault(ws, {})
        ids = [i for i in picks.get(tk, []) if i != account_id]
        if on:
            if len(ids) >= MAX_ACCOUNTS:
                raise ValueError(f"at most {MAX_ACCOUNTS} {tk} accounts per workspace")
            ids.append(account_id)
        if ids:
            picks[tk] = ids
        else:
            picks.pop(tk, None)
        return json.loads(json.dumps(picks))
    return _update(root, fn)


def effective_grant(ws, bot, root=bots.ROOT):
    """The bot's grant limited to apps this workspace has an account for."""
    have = accounts_for(ws, root)
    return {tk: scope for tk, scope in grant(ws, bot, root).items() if tk in have}


def grant(ws, bot, root=bots.ROOT):
    stored = ((load(root).get("grants") or {}).get(ws) or {}).get(bot)
    if stored is None:
        stored = (DEFAULT_GRANTS.get(ws) or {}).get(bot) or {}
    return dict(stored)


def all_grants(root=bots.ROOT):
    return load(root).get("grants") or {}


def set_grant(ws, bot, apps, root=bots.ROOT):
    if not isinstance(apps, dict):
        raise ValueError("apps must be an object")
    clean = {}
    for slug, scope in apps.items():
        if not isinstance(slug, str) or not _SLUG_RE.match(slug):
            raise ValueError(f"bad app: {slug!r}")
        if slug in app_rules.BLOCKED_TOOLKITS:
            raise ValueError(f"{slug} cannot be granted")
        if scope not in SCOPES:
            raise ValueError(f"bad scope for {slug}: {scope!r}")
        clean[slug] = scope
    _update(root, lambda d: d.setdefault("grants", {}).setdefault(ws, {}).__setitem__(bot, clean))
    return dict(clean)
