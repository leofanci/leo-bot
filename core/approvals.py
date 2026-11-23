"""approvals.py — proposals waiting on the CEO (Slack-style approvals inbox).

A bot that proposes a write and waits for the user's OK ends its reply with one
line `APPROVAL: <what it will save, where>`. The server strips that line,
records a pending approval for the thread, and the UI shows Approve / Edit / No
on the reply plus an inbox (HQ lists every workspace, a company only its own).
Any new user message in that thread resolves it.

A second kind, `app_action`, is a connected-app call a bot made that is not a
read (send, post, create...). The relay stores it instead of running it, with a
preview the server built from the real arguments; only an Approve click runs
it, exactly once (pending -> running -> done/failed). Chat rules never touch
these rows: a new message does not resolve them and a newer proposal does not
supersede them. Rows with no `kind` are chat approvals.

A fifth kind, `verdict`, is a scoring job's verdict on one experiment; Approve writes the
result and one evidence entry, exactly once.

A fourth kind, `contradiction`, says new evidence breaks a memo's assumption; Approve marks
the assumption contradicted, exactly once.

A third kind, `job`, is a scheduled-job change (create, change or delete) a bot made
(dashboard/job_cards.py). The server applies it when the bot's turn ends and keeps a
receipt row (status done, `undo`) that the thread shows with Undo; Undo runs once.
Older rows may still be pending cards, answered by Approve exactly once.

Browser send plans are app actions too (toolkit `browser`, slug `BROWSER_SEND`): a bot
plans its sends, the owner approves them (pending -> approved), and the browser relay
takes one when the bot types the same text on the same page (approved -> done). They are
never run from the card: only the bot's own click in its browser sends. Until it is used the
owner can withdraw it (approved -> rejected).

Stored in gitignored team/approvals.json (spans workspaces, so company bots
are denied Read on team/).
"""
import json
import re
import secrets
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import parse_qsl, unquote, urlsplit

from core import app_preview, app_rules, bots, browser, integrations

MARKER_RE = re.compile(r"^[ \t>*_-]*APPROVAL:[*_ \t]*(.+?)[ \t*_]*$", re.M)
SUMMARY_MAX = 200
KEEP = 200
AUTO_KEEP = 86400  # a bot's own app runs are kept at least this long (its daily limit counts them)
STATUSES = {"pending", "approved", "rejected", "answered", "superseded",
            "running", "done", "failed", "expired", "undone"}
RECEIPT_TTL = 7 * 86400  # a job receipt offers Undo this long
APP = "app_action"
JOB = "job"
CONTRA = "contradiction"
VERDICT = "verdict"
HELD = (APP, JOB, CONTRA, VERDICT)  # answered only through their own card
APP_TTL = 7 * 86400
PLAN_TTL = 7 * 86400  # an approved browser send can be used this long after its approval
BROWSER = "browser"
BROWSER_SEND = "BROWSER_SEND"
SENT = "Sent from the browser."
DAY = 86400
RUNNING_STALE = 600
STALE_NOTE = "May or may not have gone out. Check the app."
JOB_STALE_NOTE = "May or may not be saved. Check Team, Scheduled jobs."
# Every read-modify-write holds this: the relay thread writes here too.
LOCK = threading.RLock()


def extract(reply):
    """(reply without APPROVAL lines, last summary or None)."""
    found = MARKER_RE.findall(reply or "")
    if not found:
        return reply, None
    clean = MARKER_RE.sub("", reply).rstrip()
    return clean, found[-1].strip()[:SUMMARY_MAX] or None


def _path(root):
    return Path(root) / "team" / "approvals.json"


def load(root=bots.ROOT):
    try:
        data = json.loads(_path(root).read_text())
    except (OSError, ValueError):
        return []
    rows = data.get("approvals") if isinstance(data, dict) else None
    return [r for r in rows or [] if isinstance(r, dict) and r.get("id")]


def _is_held(r):
    return r.get("kind") in HELD


def _is_send(r):
    return r.get("kind") == APP and isinstance(r.get("action"), dict) and r["action"].get("toolkit") == BROWSER


def _is_app_send(r):
    """An app action (not a browser send) whose tool sends: it counts against its account's cap."""
    a = r.get("action") if r.get("kind") == APP else None
    return isinstance(a, dict) and a.get("toolkit") != BROWSER and isinstance(a.get("slug"), str) \
        and isinstance(a.get("toolkit"), str) and app_rules.is_send(a["slug"], a["toolkit"])


def _app_cap_row(r, now):
    """An app send that still counts toward its account's daily cap: running, done or failed (a
    failed call may have gone out), started in the last day."""
    return r.get("status") in ("done", "failed", "running") and _is_app_send(r) \
        and 0 <= now - (r.get("started") or r.get("resolved") or 0) < DAY


def count_app_sends(rows, account, now):
    """App sends from `account` in the 24 hours before `now`, by every bot in every workspace."""
    return sum(1 for r in rows if _app_cap_row(r, now) and (r.get("action") or {}).get("account") == account)


def _protected(r):
    # A bot's own runs of the last day stay too: they are its daily limit and the owner's record.
    # So do app sends of the last day (clicked ones too): an account's daily cap counts them.
    # So do approved plans and browser sends of the last week: the site's daily cap counts them,
    # and Approvals shows them as the "Sent from the browser" log for PLAN_TTL.
    now = time.time()
    return (_is_held(r) and r.get("status") in ("pending", "running")) or \
        (r.get("auto") and now - (r.get("resolved") or 0) < AUTO_KEEP) or _app_cap_row(r, now) or \
        (_is_send(r) and (r.get("status") == "approved"
                          or (r.get("status") in ("done", "failed")
                              and now - (r.get("resolved") or r.get("started") or 0) < PLAN_TTL)))


def _save(root, rows):
    """Keep every open app action plus the newest KEEP of the rest."""
    rest = {id(r) for r in [r for r in rows if not _protected(r)][-KEEP:]}
    rows = [r for r in rows if _protected(r) or id(r) in rest]
    bots._atomic_write(_path(root), json.dumps({"approvals": rows}, indent=2) + "\n")


def add(root=bots.ROOT, *, ws, room, chat, bot, summary):
    """Record a pending approval; an older pending one in the same thread is superseded."""
    with LOCK:
        rows = load(root)
        for r in rows:
            if r.get("chat") == chat and r.get("status") == "pending" and not _is_held(r):
                r["status"] = "superseded"
        rec = {"id": uuid.uuid4().hex[:12], "ws": ws, "room": room, "chat": chat, "bot": bot,
               "summary": summary, "created": time.time(), "status": "pending"}
        rows.append(rec)
        _save(root, rows)
        return rec


def asking_bot(chat, root=bots.ROOT):
    """The bot of the thread's open approval card (not a held app action or job), or None."""
    if not chat:
        return None
    with LOCK:
        rows = [r for r in load(root) if r.get("chat") == chat and r.get("status") == "pending" and not _is_held(r)]
    rows.sort(key=lambda r: r.get("created") or 0)
    return rows[-1].get("bot") if rows else None


def resolve_chat(chat, status, root=bots.ROOT):
    """Close the thread's pending approval (the user answered it)."""
    if status not in STATUSES or not chat:
        return 0
    with LOCK:
        rows = load(root)
        n = 0
        for r in rows:
            if r.get("chat") == chat and r.get("status") == "pending" and not _is_held(r):
                r["status"], r["resolved"] = status, time.time()
                n += 1
        if n:
            _save(root, rows)
        return n


def _cap_row(r, now):
    """A browser send that still counts toward a site's daily cap (done, failed or running in
    the last day)."""
    return _is_send(r) and r.get("status") in ("done", "failed", "running") \
        and 0 <= now - (r.get("started") or r.get("resolved") or 0) < DAY


def forget_workspace(ws, root=bots.ROOT):
    """A deleted workspace: its approvals (and held app actions) go, so a project made later
    with the same slug starts with an empty inbox. A running app action is left to finish, and
    its browser sends of the last day stay (marked ws_gone, never listed again): the site's
    daily cap counts them, so recreating the slug does not reset it."""
    if not isinstance(ws, str) or not ws or ws == bots.HQ:
        return 0
    now = time.time()
    with LOCK:
        rows = load(root)
        keep = []
        for r in rows:
            if r.get("ws") != ws:
                keep.append(r)
            elif r.get("status") == "running" or _cap_row(r, now):
                r["ws_gone"] = True
                keep.append(r)
        if len(keep) != len(rows) or any(r.get("ws_gone") for r in keep if r.get("ws") == ws):
            _save(root, keep)
        return len(rows) - len(keep)


def move_room(old, new, root=bots.ROOT):
    """A renamed channel keeps its approvals: repoint every row at `new`."""
    with LOCK:
        rows = load(root)
        n = 0
        for r in rows:
            if r.get("room") == old:
                r["room"] = new
                n += 1
        if n:
            _save(root, rows)
        return n


def pending(root=bots.ROOT, ws=None):
    """Open approvals, newest first; `ws` other than HQ narrows to one workspace."""
    with LOCK:
        rows = load(root)
        if _housekeep(rows):
            _save(root, rows)
    rows = [r for r in rows if r.get("status") == "pending"
            and (not ws or ws == bots.HQ or r.get("ws") == ws)]
    return sorted(rows, key=lambda r: r.get("created") or 0, reverse=True)


def _housekeep(rows):
    """Expire week-old held rows; a run cut off by a restart is failed, never re-run."""
    now, changed = time.time(), False
    for r in rows:
        if not _is_held(r):
            continue
        if r.get("status") == "pending" and now - (r.get("created") or 0) > APP_TTL:
            r["status"], r["resolved"], changed = "expired", now, True
        elif r.get("status") == "approved" and _is_send(r) and now - (r.get("approved_at") or 0) > PLAN_TTL:
            r["status"], r["resolved"], changed = "expired", now, True
        elif r.get("status") == "running" and now - (r.get("started") or 0) > RUNNING_STALE:
            r.update(status="failed", result=JOB_STALE_NOTE if r.get("kind") in (JOB, CONTRA, VERDICT) else STALE_NOTE,
                     resolved=now)
            changed = True
    return changed


def _app_record(root, *, ws, room, chat, bot, person, toolkit, slug, arguments, account):
    label = (integrations.known(root).get(account) or {}).get("label") if account else None
    return {"id": uuid.uuid4().hex[:12], "kind": APP, "ws": ws, "room": room, "chat": chat,
            "bot": bot, "person": person, "status": "pending", "created": time.time(),
            "action": {"toolkit": toolkit, "slug": slug, "arguments": arguments,
                       "account": account, "account_label": label or account},
            "preview": app_preview.build(slug, arguments, toolkit, account=label or account)}


def add_app_action(root=bots.ROOT, *, ws, room, chat, bot, person, toolkit, slug, arguments, account=None,
                   batch=None):
    """Hold one app tool call for an Approve click, on the account it will run as. Calls held
    together share a `batch` id: one Approve all runs them (each still re-checked)."""
    rec = _app_record(root, ws=ws, room=room, chat=chat, bot=bot, person=person, toolkit=toolkit,
                      slug=slug, arguments=arguments, account=account)
    if batch:
        rec["batch"] = batch
    with LOCK:
        rows = load(root)
        rows.append(rec)
        _save(root, rows)
    return rec


def log_app_run(root=bots.ROOT, *, ws, room, chat, bot, person, toolkit, slug, arguments, account, status, result):
    """Record an app call a bot ran on its own (an "act" grant): never pending, so no card."""
    rec = _app_record(root, ws=ws, room=room, chat=chat, bot=bot, person=person, toolkit=toolkit,
                      slug=slug, arguments=arguments, account=account)
    now = time.time()
    rec.update(status=status, result=result, auto=True, resolved=now)
    with LOCK:
        rows = load(root)
        rows.append(rec)
        _save(root, rows)
    return rec


def start_app_run(root=bots.ROOT, *, ws, room, chat, bot, person, toolkit, slug, arguments, account, daily,
                  now=None):
    """Start an app call a bot runs on its own (an "act" grant), before it is forwarded: a
    `running` row with no card, or None when it sends and `account` already made `daily` sends
    in the last day. Counted and written under one lock: two turns cannot both take the last
    send. The caller forwards, then calls end_app_run."""
    now = time.time() if now is None else now
    rec = _app_record(root, ws=ws, room=room, chat=chat, bot=bot, person=person, toolkit=toolkit,
                      slug=slug, arguments=arguments, account=account)
    rec.update(status="running", started=now, auto=True)
    with LOCK:
        rows = load(root)
        _housekeep(rows)
        if daily and _is_app_send(rec) and count_app_sends(rows, account, now) >= daily:
            return None
        rows.append(rec)
        _save(root, rows)
    return dict(rec)


def end_app_run(aid, status, result, root=bots.ROOT):
    """running -> done or failed for a bot's own app call. A row no longer running (housekept
    to failed while the call hung) keeps what it has."""
    if status not in ("done", "failed"):
        raise ValueError("bad status")

    def fn(r):
        if r.get("status") == "running":
            r.update(status=status, result=result, resolved=time.time())
    return _mutate_held(aid, root, fn, kinds=(APP,))


def _norm(text):
    return " ".join(text.split()) if isinstance(text, str) else ""


# Query keys a site adds on its own redirects to track the visit; they never pick the page or
# who a message goes to, so a plan matches with or without them.
_TRACKING_KEY = re.compile(r"utm_[a-z0-9_]*|ref|ref_src|refsrc|trk|trkinfo|fbclid|gclid|igsh|igshid|si", re.I)


def _place(url):
    """(host, path, query) of a page for matching a send to its plan. These do not count: a
    leading "www." on the host, a trailing slash, percent-encoding of the path, the order of
    the query's parameters, the site's own tracking parameters, and the fragment."""
    host = browser.host_of(url)
    if not host:
        return None
    try:
        parts = urlsplit(url.strip())
        query = parse_qsl(parts.query, keep_blank_values=True)
    except ValueError:
        return None
    host = host[4:] if host.startswith("www.") and len(host) > 4 else host
    path = unquote(parts.path).rstrip("/") or "/"
    return host, path, tuple(sorted(kv for kv in query if not _TRACKING_KEY.fullmatch(kv[0])))


def add_send_plan(root=bots.ROOT, *, ws, room, chat, bot, person, items):
    """Hold a bot's planned browser sends ({"url", "text", "site"} each) under one batch:
    one Approve all approves them. Approving does not send: the bot's own click does."""
    batch = secrets.token_hex(6)
    recs = []
    for it in items:
        rec = _app_record(root, ws=ws, room=room, chat=chat, bot=bot, person=person, toolkit=BROWSER,
                          slug=BROWSER_SEND, arguments={"url": it["url"], "text": it["text"], "site": it["site"]},
                          account=None)
        rec["batch"] = batch
        recs.append(rec)
    with LOCK:
        rows = load(root)
        rows.extend(recs)
        _save(root, rows)
    return recs


def withdraw_send(aid, root=bots.ROOT):
    """approved -> rejected for one planned browser send the bot has not used yet, so it can no
    longer be sent (the owner changed their mind within its PLAN_TTL)."""
    def fn(r):
        if not _is_send(r):
            raise ValueError("not a browser send")
        _need(r, "approved")
        r.update(status="rejected", resolved=time.time(), withdrawn=True)
    return _mutate_held(aid, root, fn, kinds=(APP,))


def approve_send(aid, root=bots.ROOT):
    """pending -> approved for one planned browser send, once."""
    def fn(r):
        if not _is_send(r):
            raise ValueError("not a browser send")
        _need(r, "pending")
        r.update(status="approved", approved_at=time.time())
    return _mutate_held(aid, root, fn, kinds=(APP,))


def _send_site(r, root, listed):
    """The listed site a send counts against, from its page address at count time."""
    url = (r["action"].get("arguments") or {}).get("url")
    return browser.site_of(url, root, listed) or browser.host_of(url)


def _count_sends(rows, site, root, now):
    # A failed or running send may have gone out: it counts against the cap too.
    # Counted from when it started: a row housekept to failed later still counts 24 h from its click.
    listed = browser.sites(root)
    return sum(1 for r in rows if _cap_row(r, now) and _send_site(r, root, listed) == site)


def claim_send(root=bots.ROOT, *, ws, room=None, chat=None, bot, person=None, url, text, daily=None,
               planned, now=None, gap=None):
    """Start one browser send, atomically with the daily cap: ("capped" | "no_plan" | "ok", record),
    or ("wait", seconds left) when the newest send on the page's site (every bot, every
    workspace) started less than `gap` seconds ago. Nothing is written for "wait".

    Under one lock it counts today's sends of the page's site (every bot, every workspace)
    against `daily`, then either moves this bot's matching approved plan item (same ws, page
    by host, path and query, text whitespace aside, approved at most PLAN_TTL ago) to
    `running` (`planned`), or writes a new `running` record (an act send). The caller sends,
    then calls end_send. A second browser checking the cap meanwhile sees the running row."""
    now = time.time() if now is None else now
    site = browser.site_of(url, root) or browser.host_of(url)
    place, want = _place(url), _norm(text)
    with LOCK:
        rows = load(root)
        _housekeep(rows)
        if daily and _count_sends(rows, site, root, now) >= daily:
            return "capped", None
        if gap:
            listed = browser.sites(root)
            last = max((r.get("started") or r.get("resolved") or 0 for r in rows
                        if _cap_row(r, now) and _send_site(r, root, listed) == site), default=None)
            if last is not None and now - last < gap:
                return "wait", gap - (now - last)
        if planned:
            hits = [] if place is None or not want else [
                r for r in rows if _is_send(r) and r.get("status") == "approved"
                and r.get("ws") == ws and r.get("bot") == bot
                and 0 <= now - (r.get("approved_at") or 0) <= PLAN_TTL
                and _place((r["action"].get("arguments") or {}).get("url")) == place
                and _norm((r["action"].get("arguments") or {}).get("text")) == want]
            if not hits:
                return "no_plan", None
            rec = min(hits, key=lambda r: r.get("approved_at") or 0)
            rec["action"]["arguments"] = {**(rec["action"].get("arguments") or {}), "site": site}
        else:
            rec = _app_record(root, ws=ws, room=room, chat=chat, bot=bot, person=person, toolkit=BROWSER,
                              slug=BROWSER_SEND, arguments={"url": url, "text": text, "site": site}, account=None)
            rec["auto"] = True
            rows.append(rec)
        rec.update(status="running", started=now)
        _save(root, rows)
        return "ok", dict(rec)


def end_send(aid, ok, root=bots.ROOT):
    """running -> done (SENT) or failed (it may or may not have gone out). A row no longer
    running (housekept to failed while the click hung) keeps what it has."""
    def fn(r):
        if r.get("status") != "running":
            return
        r.update(status="done" if ok else "failed", result=SENT if ok else STALE_NOTE, resolved=time.time())
    return _mutate_held(aid, root, fn, kinds=(APP,))


def take_send(root=bots.ROOT, *, ws, bot, url, text, now=None):
    """Use one approved send of this bot that matches the page and text (see claim_send):
    approved -> done, with its site rewritten from `url`. None when none matches."""
    status, rec = claim_send(root, ws=ws, bot=bot, url=url, text=text, planned=True, now=now)
    if status != "ok":
        return None
    return end_send(rec["id"], True, root)


def sends_today(site, root=bots.ROOT, now=None):
    """Browser sends of `site` in the last 24 hours, by every bot in every workspace: done,
    failed or running (a failed click may have gone out). The site comes from each send's
    page address now, so a site listed later counts its older sends."""
    if not site:
        return 0
    now = time.time() if now is None else now
    return _count_sends(load(root), site, root, now)


def batch_pending(batch, root=bots.ROOT):
    """The pending app actions held together under one batch id."""
    return [r for r in load(root) if r.get("kind") == APP and r.get("batch") == batch and r.get("status") == "pending"]


def _job_target(job):
    """What a job card acts on: the job for a change or deletion, the name for a new one."""
    job = job or {}
    if job.get("op") == "create":
        f = job.get("fields") or {}
        return ("create", f.get("workspace"), " ".join(str(f.get("name") or "").lower().split()))
    return (job.get("op"), job.get("job_id"))


def add_job(root=bots.ROOT, *, ws, room, chat, bot, job, summary, preview):
    """Hold one job proposal ({"op", "job_id", "fields"}) for an Approve click. A pending card
    in the same thread for the same target (that job, or a new job of that name) is
    superseded: a bot that proposes again never leaves two cards that would both apply."""
    rec = {"id": uuid.uuid4().hex[:12], "kind": JOB, "ws": ws, "room": room, "chat": chat,
           "bot": bot, "status": "pending", "created": time.time(), "job": job,
           "summary": str(summary)[:SUMMARY_MAX], "preview": preview}
    with LOCK:
        rows = load(root)
        for r in rows:
            if (r.get("kind") == JOB and r.get("status") == "pending" and r.get("chat") == chat
                    and _job_target(r.get("job")) == _job_target(job)):
                r.update(status="superseded", resolved=rec["created"])
        rows.append(rec)
        _save(root, rows)
    return rec


def add_job_receipt(root=bots.ROOT, *, ws, room, chat, bot, job_id, summary, result, undo, preview=None):
    """Record a job change a bot made and the server already applied: no card to answer, only
    an Undo (`undo`, see job_cards.undo) the owner can run once."""
    now = time.time()
    rec = {"id": uuid.uuid4().hex[:12], "kind": JOB, "ws": ws, "room": room, "chat": chat,
           "bot": bot, "status": "done", "created": now, "resolved": now, "receipt": True,
           "job_id": job_id, "undo": undo, "summary": str(summary)[:SUMMARY_MAX],
           "result": str(result)[:SUMMARY_MAX], "preview": preview}
    with LOCK:
        rows = load(root)
        rows.append(rec)
        _save(root, rows)
    return rec


def receipts(root=bots.ROOT, ws=None, now=None):
    """Job receipts that still offer Undo, newest first; `ws` other than HQ narrows."""
    now = time.time() if now is None else now
    rows = [r for r in load(root) if r.get("kind") == JOB and r.get("receipt") and r.get("status") == "done"
            and now - (r.get("created") or 0) <= RECEIPT_TTL and (not ws or ws == bots.HQ or r.get("ws") == ws)]
    return sorted(rows, key=lambda r: r.get("created") or 0, reverse=True)


SENT_LIMIT = 50


def browser_sent(root=bots.ROOT, ws=None, now=None):
    """Browser sends that ran in the last PLAN_TTL (done, failed or still running), newest first;
    `ws` other than HQ narrows. Approvals lists them, so every send is logged there."""
    now = time.time() if now is None else now
    rows = [r for r in load(root) if _is_send(r) and r.get("status") in ("done", "failed", "running")
            and 0 <= now - (r.get("started") or r.get("created") or 0) <= PLAN_TTL
            and not r.get("ws_gone") and (not ws or ws == bots.HQ or r.get("ws") == ws)]
    rows.sort(key=lambda r: r.get("started") or r.get("created") or 0, reverse=True)
    return rows[:SENT_LIMIT]


def browser_approved(root=bots.ROOT, ws=None, now=None):
    """Planned browser sends the owner approved that the bot has not sent yet (still usable,
    within PLAN_TTL of their approval), newest first; `ws` other than HQ narrows. Approvals lists
    them under "Approved, not sent yet", each with Withdraw."""
    now = time.time() if now is None else now
    rows = [r for r in load(root) if _is_send(r) and r.get("status") == "approved"
            and 0 <= now - (r.get("approved_at") or 0) <= PLAN_TTL
            and (not ws or ws == bots.HQ or r.get("ws") == ws)]
    rows.sort(key=lambda r: r.get("approved_at") or 0, reverse=True)
    return rows[:SENT_LIMIT]


def undo_receipt(aid, apply, root=bots.ROOT, now=None):
    """Run a receipt's Undo once: `apply(undo)` returns the result line (ValueError: nothing
    changes and the receipt keeps its Undo)."""
    now = time.time() if now is None else now

    def fn(r):
        if not r.get("receipt"):
            raise ValueError("no such receipt")
        _need(r, "done")
        if now - (r.get("created") or 0) > RECEIPT_TTL:
            raise ValueError("too old to undo: change it on the Team screen")
        line = apply(r.get("undo") or {})
        r.update(status="undone", result=str(line)[:SUMMARY_MAX], undone=now)
    return _mutate_held(aid, root, fn, kinds=(JOB,))


def add_contradiction(root=bots.ROOT, *, ws, room, chat, bot, contra, summary, preview):
    """Hold one finding ({"assumption", "evidence", "reason"}) for an Approve click. A pending
    card for the same (assumption, evidence) is not repeated."""
    key = (contra.get("assumption"), contra.get("evidence"))
    with LOCK:
        rows = load(root)
        for r in rows:
            c = r.get("contra") or {}
            if r.get("kind") == CONTRA and r.get("status") == "pending" \
                    and (c.get("assumption"), c.get("evidence")) == key:
                return r
        rec = {"id": uuid.uuid4().hex[:12], "kind": CONTRA, "ws": ws, "room": room, "chat": chat,
               "bot": bot, "status": "pending", "created": time.time(), "contra": contra,
               "summary": str(summary)[:SUMMARY_MAX], "preview": preview}
        rows.append(rec)
        _save(root, rows)
    return rec


def _verdict_key(v):
    return (v.get("exp"), v.get("number"), v.get("verdict"))


def has_pending_verdict(root, verdict):
    """A pending card already holds this experiment, number and verdict."""
    return any(r.get("kind") == VERDICT and r.get("status") == "pending"
               and _verdict_key(r.get("verdict") or {}) == _verdict_key(verdict) for r in load(root))


def add_verdict(root=bots.ROOT, *, ws, room, chat, bot, verdict, summary, preview):
    """Hold one verdict ({"exp", "project", "stem", "metric", "number", "window_days", "date",
    "verdict", "reason", "raw"}) for an Approve click. The same (experiment, number, verdict)
    pending is not repeated; the same experiment with other numbers supersedes the old card."""
    with LOCK:
        rows = load(root)
        for r in rows:
            v = r.get("verdict") or {}
            if r.get("kind") == VERDICT and r.get("status") == "pending" and v.get("exp") == verdict.get("exp"):
                if _verdict_key(v) == _verdict_key(verdict):
                    return r
                r.update(status="superseded", resolved=time.time())
        rec = {"id": uuid.uuid4().hex[:12], "kind": VERDICT, "ws": ws, "room": room, "chat": chat,
               "bot": bot, "status": "pending", "created": time.time(), "verdict": verdict,
               "summary": str(summary)[:SUMMARY_MAX], "preview": preview}
        rows.append(rec)
        _save(root, rows)
    return rec


def get(aid, root=bots.ROOT):
    return next((r for r in load(root) if r.get("id") == aid), None)


def _mutate_held(aid, root, fn, kinds=HELD):
    with LOCK:
        rows = load(root)
        _housekeep(rows)
        rec = next((r for r in rows if r.get("id") == aid and r.get("kind") in kinds), None)
        if rec is None:
            raise ValueError("no such app action" if kinds == (APP,) else "no such card")
        fn(rec)
        _save(root, rows)
        return dict(rec)


def _need(rec, status):
    if rec.get("status") != status:
        raise ValueError(f"not {status}")


def claim(aid, root=bots.ROOT, daily=None, account=None):
    """pending -> running, once: a double click or a second tab cannot run it twice. With
    `daily`, a send whose account (`account`, the one it runs as, else the row's) already made
    that many sends today raises ValueError("capped") and stays pending. `account` is written on
    the row, so the send counts against the account it ran as."""
    with LOCK:
        rows = load(root) if daily else []

        def fn(r):
            _need(r, "pending")
            now = time.time()
            action = r.get("action") if isinstance(r.get("action"), dict) else None
            acct = account or (action or {}).get("account")
            if daily and _is_app_send(r) and count_app_sends(rows, acct, now) >= daily:
                raise ValueError("capped")
            if account and action is not None:
                action["account"] = account
            r.update(status="running", started=now)
        return _mutate_held(aid, root, fn)


def finish(aid, status, result, root=bots.ROOT):
    if status not in ("done", "failed"):
        raise ValueError("bad status")
    def fn(r):
        _need(r, "running")
        r.update(status=status, result=result, resolved=time.time())
    return _mutate_held(aid, root, fn)


def reject(aid, root=bots.ROOT):
    def fn(r):
        _need(r, "pending")
        r.update(status="rejected", resolved=time.time())
    return _mutate_held(aid, root, fn)


def edit_arguments(aid, arguments, root=bots.ROOT):
    if not isinstance(arguments, dict):
        raise ValueError("arguments must be an object")
    def fn(r):
        _need(r, "pending")
        r["action"]["arguments"] = arguments
        r["preview"] = app_preview.build(r["action"]["slug"], arguments, r["action"].get("toolkit"),
                                         account=r["action"].get("account_label"))
    return _mutate_held(aid, root, fn, kinds=(APP,))


def edit_job(aid, job, summary, preview, root=bots.ROOT):
    """Replace a pending job card's proposal (already validated by the caller)."""
    def fn(r):
        _need(r, "pending")
        r.update(job=job, summary=str(summary)[:SUMMARY_MAX], preview=preview)
    return _mutate_held(aid, root, fn, kinds=(JOB,))
