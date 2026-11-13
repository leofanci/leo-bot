"""Offline turn evals: each case is one chat message through Handler._handle_ask on a throwaway
data folder, with a scripted engine whose replies the case gives. They check what the server
decided (bot, skill, handoff, model tier and reason, web, approval card, consults, reactions,
the memory a bot card carries), never the wording of a reply. No model call, no tokens.

A case (cases/turns.json):
  id, category, msg            the user's message
  room                         "general" (default) or "dm-<bot>"
  thread                       {"bot", "skill", "explicit"}: a thread already under way
  shared                       true: a thread invited people can see
  apps                         {"gmail": "read"}: the lead bot's connected-app grant
  folder                       "edit" or "read": one linked folder in the workspace
  memory                       {"cmo": [file lines], "shared:cmo": [...]}: memory files
  context                      the thread's last reply had this context size (tokens): past
                               LEOBOT_COMPACT_TOKENS the turn compacts (needs `thread`)
  reply / teammate_reply / followup_reply   what the scripted engine says (default "ok"); a
                               HANDOFF target's run is the lead session's second run: followup_reply
  expect                       keys of the outcome to compare; *_has / *_lacks take substrings
  gap                          a known gap: the case states the wanted behavior and fails today.
                               It is reported, not failed; once it passes, drop `gap`.
"""
import io
import json
import shutil
import tempfile
from pathlib import Path
from unittest import mock

CASES = Path(__file__).parent / "cases" / "turns.json"
WS = "acme"
THREAD_ID = "e7a10000-0000-4000-8000-000000000000"  # chat_store takes UUID-shaped ids only
_EQUAL = ("bot", "skill", "handoff_from", "handoff", "tier", "reason", "web", "gate", "approval",
          "teammates", "followup", "reactions", "edit", "runs")
_TEXT = {"reply_has": "reply", "reply_lacks": "reply", "prompt_has": "prompt",
         "prompt_lacks": "prompt", "teammate_prompt_has": "teammate_prompt",
         "teammate_prompt_lacks": "teammate_prompt"}
GATE_TEXT = "tag who should do it"


_CASE_KEYS = {"id", "category", "msg", "room", "thread", "shared", "apps", "folder", "memory", "context",
              "reply", "teammate_reply", "followup_reply", "expect", "gap"}


def load_check(case):
    """Raise ValueError for a case a typo would make pass silently."""
    unknown = (set(case) - _CASE_KEYS) | {f"expect.{k}" for k in set(case.get("expect") or {})
                                          - set(_EQUAL) - set(_TEXT)}
    if unknown or not case.get("id") or not case.get("msg"):
        raise ValueError(f"{case.get('id')}: unknown keys {sorted(unknown)} or no id/msg")


def load(path=CASES):
    cases = json.loads(Path(path).read_text(encoding="utf-8"))
    seen = set()
    for c in cases:
        load_check(c)
        if c["id"] in seen:
            raise ValueError(f"duplicate case id {c['id']}")
        seen.add(c["id"])
    return cases


class _Engine:
    label = "Eval"
    shared_safe = True
    kind = "cli"
    full_disk = False

    def __init__(self, supports):
        self.id, self.supports = "claude-code", frozenset(supports)
        self.compacted = False

    def compact(self, session):
        self.compacted = True
        return True

    def resolve_model(self, model):
        return model

    def is_fresh(self, session):
        return True


def _data_root(tmp, ext, case):
    root = Path(tmp).resolve()
    proj = root / "projects" / WS
    shutil.rmtree(proj, ignore_errors=True)  # the live runner reuses one data folder: start clean
    (proj / "team").mkdir(parents=True)
    (proj / "project.md").write_text(f"---\nname: {WS}\nkind: venture\n---\n", encoding="utf-8")
    (root / "team").mkdir(exist_ok=True)
    if case.get("folder"):  # one linked folder, "edit" or "read": the CTO is active then
        from core import folders
        app = Path(ext).resolve() / "linked-app"  # outside the data folder, as a real link is
        app.mkdir()
        folders.add(WS, str(app), folders.EDIT if case["folder"] == "edit" else folders.READ, root)
    for key, lines in (case.get("memory") or {}).items():
        scope, _, bot = key.rpartition(":")  # "cmo" or "shared:cmo"
        path = proj / "team" / ("shared" if scope else "") / f"{bot}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(f"{ln}\n" for ln in lines), encoding="utf-8")
    return root


def run(case, live_root=None):
    """The outcome of one case's turn, as a dict (see _EQUAL and _TEXT).

    `live_root`: run on the real engine registry.pick gives, with the data folder there (the
    live runner passes a throwaway home's data folder, so osctl in the turn writes there too).
    Otherwise the engine is scripted by the case and the data folder is a temp one."""
    from dashboard import server  # late: importing the server reads the data folder
    from dashboard.engines.claude_code import ClaudeCodeEngine

    live = live_root is not None
    replies = {"lead": case.get("reply", "ok"), "teammate": case.get("teammate_reply", "ok"),
               "followup": case.get("followup_reply", "ok")}
    thread = case.get("thread") or {}
    runs, recorded = [], {}
    real_ask = server.chat_session.ChatSession.ask

    def ask(self_, text, with_web=False, model=None, read_dirs=(), edit_dirs=(), web_domains=(), **kw):
        role = "teammate" if self_ is not sess else ("followup" if any(r["role"] == "lead" for r in runs)
                                                     else "lead")
        runs.append({"role": role, "prompt": text, "model": model, "web": with_web,
                     "edit_dirs": tuple(edit_dirs)})
        if live:
            yield from real_ask(self_, text, with_web=with_web, model=model, read_dirs=read_dirs,
                                edit_dirs=edit_dirs, web_domains=web_domains, **kw)
            return
        yield ("delta", replies[role])
        yield ("done", {"result": replies[role]})

    if live:
        sess = server.chat_session.ChatSession(repo_dir=str(server.paths.CODE), rail=server.rail())
    else:
        engine = _Engine(ClaudeCodeEngine.supports)

        class Sess:
            _started = bool(thread)
            engine_state = {}
            _turn_count = 1 if thread else 0
            _last_skill = thread.get("skill")
            _skill_explicit = bool(thread.get("explicit"))
            _last_bot = thread.get("bot")
            session_id = THREAD_ID

            def is_fresh(self):
                return not thread or engine.compacted

            def note_skill(self, skill, explicit=False):
                pass

        Sess.ask = ask
        sess = Sess()

    def record_turn(*a, **kw):
        recorded.update(kw, content=a[3] if len(a) > 3 else "")

    apps = case.get("apps") or {}
    with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as ext:
        root = _data_root(live_root if live else tmp, ext, case)
        if case.get("context") and not live:  # the thread's last reply, saved with its size
            from dashboard import chat_store
            with mock.patch.object(chat_store, "CHATS_DIR", root / "chats"):
                chat_store.save({"id": THREAD_ID, "title": "eval", "created": 0, "messages": [
                    {"role": "user", "content": "earlier question"},
                    {"role": "assistant", "bot": thread.get("bot"), "content": "earlier answer",
                     "engine": "claude-code", "context": case["context"]}],
                    "engines": {"claude-code": {"seen": 2}}})
        if live:  # osctl in the real turn reads the index
            import contextlib
            import index
            with contextlib.redirect_stdout(io.StringIO()):
                index.build(root)
        from core import folders
        tree = [{"slug": WS, "kind": "venture", "type": "project", "profiles": [],
                 "folders": folders.load(WS, root)["folders"]}]
        h = server.Handler.__new__(server.Handler)  # no socket: SSE goes to a buffer
        h.wfile = io.BytesIO()
        h.send_response = h.send_header = h.end_headers = lambda *a, **k: None
        h.log_message = lambda *a, **k: None
        h.server = mock.Mock(server_address=("127.0.0.1", 8765))
        patches = [
            mock.patch.object(server, "ROOT", root),
            mock.patch.object(server.fileops, "ROOT", root),
            mock.patch.object(server, "get_chat_session", return_value=sess),
            mock.patch.object(server.db, "tree", return_value=tree),
            mock.patch.object(server.db, "posts", return_value=[]),
            mock.patch.object(server.chat_session.ChatSession, "ask", ask),
        ]
        if not live:
            patches.append(mock.patch.object(server.engine_registry, "pick", return_value=engine))
        patches += [
            mock.patch.object(server.chat_store, "CHATS_DIR", root / "chats"),
            mock.patch.object(server.chat_store, "record_turn", record_turn),
            mock.patch.object(server.spend, "refusal", return_value=None),
            mock.patch.object(server.spend, "near_notice", return_value=None),
            mock.patch.object(server.memory_consolidate, "maybe_start"),
            mock.patch.object(server.runners.HUB, "online", return_value=False),
            mock.patch.object(server, "_shared_room", return_value=bool(case.get("shared"))),
            mock.patch.object(server.integrations, "api_key", return_value="k" if apps else None),
            mock.patch.object(server.integrations, "effective_grant", return_value=dict(apps)),
        ]
        for p in patches:
            p.start()
        try:
            h._handle_ask({"messages": [{"role": "user", "content": case["msg"]}],
                           "room_id": f"{WS}/{case.get('room') or 'general'}"})
        finally:
            for p in reversed(patches):
                p.stop()
        out = h.wfile.getvalue().decode()

    events = [json.loads(ln[6:]) for ln in out.splitlines() if ln.startswith("data: {")]
    bot_ev = next((e["bot"] for e in events if isinstance(e.get("bot"), dict)), {})
    starts = [e["msg_start"] for e in events if isinstance(e.get("msg_start"), dict)]
    lead = next((r for r in runs if r["role"] == "lead"), None)
    deltas = "".join(e["delta"] for e in events if isinstance(e.get("delta"), str))
    tiers = {server.chat_session.CHAT_MODEL: "fast", server.chat_session.ESCALATED_MODEL: "strong"}
    return {
        "bot": bot_ev.get("id"),
        "skill": bot_ev.get("skill"),
        "handoff_from": bot_ev.get("handoff_from"),
        "handoff": next((s.get("id") for s in starts if s.get("handoff_from")), None),
        "tier": tiers.get(lead["model"], lead["model"]) if lead else None,
        "reason": recorded.get("meta", {}).get("reason") if lead else None,
        "web": bool(lead and lead["web"]),
        "gate": not runs and GATE_TEXT in deltas,
        "approval": any("approval" in e for e in events),
        "teammates": [s.get("id") for s in starts if not s.get("followup") and not s.get("handoff_from")],
        "followup": any(s.get("followup") for s in starts),
        "reactions": [r.get("emoji") for e in events for r in e.get("reactions") or []],
        "edit": bool(lead and lead["edit_dirs"]),
        "runs": len(runs),
        "reply": recorded.get("content", deltas) if runs else deltas,
        "prompt": lead["prompt"] if lead else "",
        "teammate_prompt": "\n\n".join(r["prompt"] for r in runs if r["role"] == "teammate"),
        "errors": [e["error"] for e in events if "error" in e],
        # Live runs: what the turn cost, every reply of it (lead, teammates, follow-up).
        "cost": round(sum(m.get("cost") or 0 for m in [recorded.get("meta") or {}]
                          + list(recorded.get("extra") or [])), 6),
        "engine": (recorded.get("meta") or {}).get("engine"),
        "model": (recorded.get("meta") or {}).get("model"),
    }


def check(case, outcome):
    """Failure lines for one case; [] when the outcome matches every expectation."""
    fails = [f"server error: {e}" for e in outcome.get("errors") or []]
    for key, want in (case.get("expect") or {}).items():
        if key in _EQUAL:
            if outcome.get(key) != want:
                fails.append(f"{key}: want {want!r}, got {outcome.get(key)!r}")
            continue
        text = outcome.get(_TEXT[key]) or ""
        for s in [want] if isinstance(want, str) else want:
            found = s.lower() in text.lower()
            if found != key.endswith("_has"):
                fails.append(f"{key}: {s!r} {'missing' if key.endswith('_has') else 'present'}")
    return fails


def run_all(cases=None):
    """[(case, failures)] for every case."""
    return [(c, check(c, run(c))) for c in (cases if cases is not None else load())]


def scorecard(results):
    """Pass counts per category, the failures, and the known gaps (open or closed)."""
    lines, by_cat = [], {}
    for case, fails in results:
        cat = by_cat.setdefault(case.get("category", "other"), [0, 0])
        if not case.get("gap"):
            cat[1] += 1
            cat[0] += not fails
    for cat, (ok, total) in sorted(by_cat.items()):
        lines.append(f"{cat:<10} {ok}/{total}")
    for case, fails in results:
        if fails and not case.get("gap"):
            lines.append(f"FAIL {case['id']}: " + "; ".join(fails))
    for case, fails in results:
        if case.get("gap"):
            state = "open" if fails else "CLOSED, drop `gap` from the case"
            lines.append(f"gap  {case['id']} ({state}): {case['gap']}")
    return "\n".join(lines)
