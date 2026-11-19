"""The internal canvas endpoint: the actor comes from a per-run canvas token only."""
import io
import json
import os
import tempfile
import unittest
from email.message import Message
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import dashboard.server as server
from core import canvas_store as cs, canvases as cv, rooms
from dashboard import taint
from dashboard.engines import api_tools, claude_code, codex
from dashboard.engines.base import TURN_ENV, TurnPolicy, TurnTokens

PORT = 8765
PATH = "/api/internal/canvas"
CHAT = "11111111-1111-1111-1111-111111111111"
NO_ACCESS = "no canvas access for this run"


def request(body=None, *, token=None, host=f"127.0.0.1:{PORT}", headers=None, path=PATH):
    h = server.Handler.__new__(server.Handler)
    out = {}
    h.wfile = io.BytesIO()
    h.send_response = lambda code, *a: out.setdefault("code", code)
    h.send_header = lambda *a: None
    h.end_headers = lambda: None
    h.log_message = lambda *a: None
    raw = b"" if body is None else json.dumps(body).encode()
    msg = Message()
    msg["Host"] = host
    if raw:
        msg["Content-Type"] = "application/json"
    msg["Content-Length"] = str(len(raw))
    if token:
        msg["Authorization"] = f"Bearer {token}"
    for k, v in (headers or {}).items():
        msg[k] = v
    h.headers, h.rfile, h.path = msg, io.BytesIO(raw), path
    h.client_address = ("127.0.0.1", 50000)
    h.server = SimpleNamespace(server_address=("127.0.0.1", PORT))
    h.do_POST()
    payload = h.wfile.getvalue()
    return out["code"], (json.loads(payload) if payload else None)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "chats").mkdir()
        (self.root / "projects" / "demo").mkdir(parents=True)
        (self.root / "team").mkdir()
        (self.root / "team" / "people.json").write_text(json.dumps({"people": {
            "p1": {"name": "A", "ws": {"demo": "member"}}}}))
        for p in (mock.patch.object(server, "ROOT", self.root),
                  mock.patch.object(server.chat_store, "CHATS_DIR", self.root / "chats"),
                  mock.patch.object(server.db, "db_exists", return_value=True),
                  mock.patch.object(server.db, "tree", return_value=[])):
            p.start()
            self.addCleanup(p.stop)

    def mint(self, **over):
        turn = over.pop("turn", None) or taint.Turn()
        ctx = {"ws": "demo", "actor": "bot", "bot": "research", "job": None, "chat": "c1",
               "room": "demo/general", "shared": False, "people": (), "turn": turn, "gated": True}
        ctx.update(over)
        tok = server.CANVAS_TOKENS.mint(ctx)
        self.addCleanup(server.CANVAS_TOKENS.revoke, tok)
        return tok, turn

    def call(self, tok, cmd, **params):
        return request({"cmd": cmd, "params": params}, token=tok)


class Access(Base):
    def test_no_token_or_bad_token_403(self):
        for tok in (None, "nope", "a" * 64):
            code, body = request({"cmd": "list-canvases", "params": {}}, token=tok)
            self.assertEqual((code, body), (403, {"ok": False, "error": NO_ACCESS}), tok)
        gate = taint.GATES.mint(taint.Turn())  # another kind of token never opens it
        self.addCleanup(taint.GATES.revoke, gate)
        self.assertEqual(request({"cmd": "list-canvases"}, token=gate)[0], 403)

    def test_non_local_request_403(self):
        tok, _ = self.mint()
        for kw in ({"host": "leo.example.com"}, {"headers": {"X-Forwarded-For": "1.2.3.4"}},
                   {"headers": {"Origin": "https://evil.example"}}):
            code, body = request({"cmd": "list-canvases", "params": {}}, token=tok, **kw)
            self.assertEqual(code, 403, kw)
            self.assertFalse(body["ok"])

    def test_revoked_after_run(self):
        tok, _ = self.mint()
        self.assertEqual(self.call(tok, "list-canvases")[0], 200)
        server.CANVAS_TOKENS.revoke(tok)  # what run()'s finally does
        self.assertEqual(self.call(tok, "list-canvases"), (403, {"ok": False, "error": NO_ACCESS}))

    def test_bad_body_400(self):
        tok, _ = self.mint()
        for body in ({"params": {}}, {"cmd": 5}, {"cmd": "get-canvas", "params": "x"}):
            code, out = request(body, token=tok)
            self.assertEqual(code, 400, body)
            self.assertFalse(out["ok"])


    def test_invalid_input_stays_a_200_refusal(self):
        tok, _ = self.mint()
        self.assertEqual(self.call(tok, "create-canvas"), (200, {"ok": False, "error": "title: required"}))
        self.assertEqual(self.call(tok, "get-canvas", id="x"),
                         (200, {"ok": False, "error": "--id: not a canvas id"}))


class Actor(Base):
    def test_actor_comes_from_token_not_body(self):
        tok, _ = self.mint()
        code, out = self.call(tok, "create-canvas", title="Plan", actor="owner", by="owner",
                              _actor="owner", source="api", blocks=[{"type": "text", "md": "hi"}])
        self.assertEqual(code, 200, out)
        self.assertTrue(out["ok"])
        doc = cs.read(self.root, "demo", out["id"])
        self.assertEqual(doc["created_by"], "bot:research")
        self.assertEqual((doc["blocks"][0]["created_by"], doc["blocks"][0]["by"]), ("bot:research", "bot:research"))

    def test_refusals_and_approvals(self):
        cid = cv.dispatch(self.root, "demo", cv.Actor("owner"), "create-canvas", {"title": "Plan"})["id"]
        tok, _ = self.mint()
        code, out = self.call(tok, "delete-canvas", id=cid, version=1)
        self.assertEqual(code, 200)
        self.assertEqual((out["ok"], out.get("needs_approval")), (False, True))
        self.assertIn("APPROVAL", out["error"])
        code, out = self.call(tok, "nope")
        self.assertEqual((code, out["ok"]), (200, False))
        self.assertNotIn("needs_approval", out)

    def test_hq_token_is_refused_for_a_bot(self):
        tok, _ = self.mint(ws="hq")
        code, out = self.call(tok, "list-canvases")
        self.assertEqual((code, out["ok"]), (200, False))

    def test_readonly_run_only_reads(self):
        cid = cv.dispatch(self.root, "demo", cv.Actor("owner"), "create-canvas", {"title": "Plan"})["id"]
        tok, _ = self.mint(readonly=True)
        for cmd, params in (("list-canvases", {}), ("get-canvas", {"id": cid}), ("list-templates", {}),
                            ("list-history", {"id": cid})):
            code, out = self.call(tok, cmd, **params)
            self.assertTrue(out["ok"], (cmd, out))
        for cmd in ("create-canvas", "add-block", "delete-canvas", "undo", "trust"):
            code, out = self.call(tok, cmd, id=cid, title="x")
            self.assertEqual((code, out["ok"]), (200, False), cmd)
            self.assertIn("read-only", out["error"])
        self.assertEqual(len(cs.list_ids(self.root, "demo", "cv")), 1)

    def test_shared_ctx_carries_room_people(self):
        rooms.save_topic("demo", "launch", ["research"], "research", self.root, private=True, people=["p1"])
        tok, _ = self.mint(shared=True, room="demo/t-launch", people=("p1",))
        code, out = self.call(tok, "create-canvas", title="Plan", pin="demo/t-launch")
        self.assertTrue(out["ok"], out)
        self.assertEqual(self.call(tok, "delete-canvas", id=out["id"])[1]["ok"], False)  # shared: blocked


class Labels(Base):
    def labeled(self):
        tainted = cv.Actor("bot", "research", tainted=lambda: True, chat="c0")
        return cv.dispatch(self.root, "demo", tainted, "create-canvas",
                           {"title": "Plan", "blocks": [{"type": "text", "md": "from a page"}]})["id"]

    def test_read_of_labeled_item_taints_turn(self):
        cid = self.labeled()
        tok, turn = self.mint()
        self.assertFalse(turn.outside)
        code, out = self.call(tok, "get-canvas", id=cid)
        self.assertTrue(out["ok"], out)
        self.assertTrue(turn.outside)
        self.assertEqual(out["blocks"][0]["md"], "from a page")
        self.assertIn("outside_content", out["blocks"][0])

    def test_ungated_ctx_withholds_labeled_text(self):
        cid = self.labeled()
        tok, _ = self.mint(gated=False)
        out = self.call(tok, "get-canvas", id=cid)[1]
        self.assertEqual(out["blocks"][0]["md"], cv.HIDDEN)

    def test_tainted_turn_labels_its_writes(self):
        tok, turn = self.mint()
        turn.saw_outside()
        out = self.call(tok, "create-canvas", title="Plan", blocks=[{"type": "text", "md": "x"}])[1]
        self.assertTrue(cs.read(self.root, "demo", out["id"])["blocks"][0]["outside"])


class Env(unittest.TestCase):
    def test_env_carries_canvas_vars(self):
        self.assertIn("LEOBOT_CANVAS_TOKEN", TURN_ENV)
        self.assertIn("LEOBOT_CANVAS_URL", TURN_ENV)
        url = f"http://127.0.0.1:{PORT}{PATH}"
        toks = TurnTokens(canvas_token="ct1", canvas_port=PORT)
        leak = {"LEOBOT_CANVAS_TOKEN": "server-own", "LEOBOT_CANVAS_URL": "http://evil.example/x"}
        with mock.patch.dict(os.environ, leak):
            env = claude_code.ClaudeCodeEngine().turn_env(TurnPolicy(), toks)
            self.assertEqual((env["LEOBOT_CANVAS_TOKEN"], env["LEOBOT_CANVAS_URL"]), ("ct1", url))
            # Codex reaches osctl only through the os relay, which carries the vars: never its own env.
            env = codex.CodexEngine().turn_env(TurnPolicy(), toks)
            self.assertNotIn("LEOBOT_CANVAS_TOKEN", env)
            self.assertNotIn("LEOBOT_CANVAS_URL", env)
            for e in (claude_code.ClaudeCodeEngine().turn_env(TurnPolicy(), TurnTokens()),
                      codex.CodexEngine().turn_env(TurnPolicy(), TurnTokens()),
                      server.os_relay.env_for({"ws": "demo"})):
                self.assertNotIn("LEOBOT_CANVAS_TOKEN", e)
                self.assertNotIn("LEOBOT_CANVAS_URL", e)
            env = server.os_relay.env_for({"ws": "demo", "canvas_token": "ct1", "canvas_port": PORT})
            self.assertEqual((env["LEOBOT_CANVAS_TOKEN"], env["LEOBOT_CANVAS_URL"]), ("ct1", url))
        box = api_tools.ToolBox(TurnPolicy(), Path("."), toks, "api", {})
        self.assertEqual((box.osctl_ctx()["canvas_token"], box.osctl_ctx()["canvas_port"]), ("ct1", PORT))


class Turn(Base):
    """Through _handle_ask: run() mints a canvas token per run and revokes it after."""

    def run_turn(self, body=None, during=None, replies=(), engine=None):
        seen = []
        replies = list(replies)
        self.out = ""

        class S:
            _started = False
            _turn_count = 0
            _last_skill = None
            _skill_explicit = False
            session_id = CHAT
            apps_used = False

            def __init__(self, *a, **k):
                pass

            def is_fresh(self):
                return not self._started

            def note_skill(self, *a, **k):
                pass

            def note_bot(self, *a):
                pass

            def close(self):
                pass

            def ask(self, prompt, **kw):
                seen.append({**kw, "_prompt": prompt, "_readonly": getattr(self, "readonly", False)})
                if during:
                    during(kw)
                yield ("delta", replies.pop(0) if replies else "ok")
                yield ("done", {})
        h = server.Handler.__new__(server.Handler)
        h.wfile = io.BytesIO()
        h.send_response = h.send_header = h.end_headers = lambda *a, **k: None
        h.log_message = lambda *a: None
        h.server = SimpleNamespace(server_address=("127.0.0.1", PORT))
        patches = [mock.patch.object(server, "get_chat_session", return_value=S()),
                   mock.patch.object(server.chat_session, "ChatSession", S)]
        if engine is not None:
            patches.append(mock.patch.object(server.engine_registry, "pick", return_value=engine))
        for p in patches:
            p.start()
        try:
            h._handle_ask({"messages": [{"role": "user", "content": "hello"}], **(body or {})})
        finally:
            for p in reversed(patches):
                p.stop()
        self.out = h.wfile.getvalue().decode()
        return seen

    def test_os_relay_ctx_gets_the_runs_canvas_token(self):
        live = []

        def during(kw):
            live.append((kw.get("canvas_token"), server.os_relay.TOKENS.lookup(kw["os_token"])))
        seen = self.run_turn(during=during, engine=codex.CodexEngine())  # full disk: osctl over the relay
        tok, os_ctx = live[0]
        self.assertTrue(seen[0].get("os_token"))
        self.assertEqual((os_ctx["canvas_token"], os_ctx["canvas_port"]), (tok, PORT))
        self.assertEqual(request({"cmd": "list-canvases", "params": {}}, token=os_ctx["canvas_token"])[0], 403)

    def test_consulted_teammate_ctx_is_readonly(self):
        ctxs = []
        server.bots.set_active("research", True, self.root, "hq")
        seen = self.run_turn(replies=["CONSULT @research: numbers?", "42", "done"],
                             during=lambda kw: ctxs.append(server.CANVAS_TOKENS.lookup(kw["canvas_token"])))
        self.assertGreaterEqual(len(seen), 2, self.out)
        self.assertTrue(seen[1]["_readonly"])
        self.assertEqual((ctxs[1]["bot"], ctxs[1]["readonly"]), ("research", True))
        self.assertFalse(ctxs[0]["readonly"])
        self.assertIsNone(server.CANVAS_TOKENS.lookup(seen[1]["canvas_token"]))  # revoked after its run
        cid = cv.dispatch(self.root, "demo", cv.Actor("owner"), "create-canvas", {"title": "Plan"})["id"]
        tok, _ = self.mint(**{k: v for k, v in ctxs[1].items() if k != "ws"})  # the same ctx, live
        self.assertIn("read-only", self.call(tok, "delete-canvas", id=cid)[1]["error"])

    def test_run_mints_then_revokes(self):
        live = []
        seen = self.run_turn(during=lambda kw: live.append(server.CANVAS_TOKENS.lookup(kw["canvas_token"])))
        kw = seen[0]
        self.assertEqual(kw["canvas_port"], PORT)
        ctx = live[0]
        self.assertEqual((ctx["actor"], ctx["bot"], ctx["chat"], ctx["shared"]), ("bot", "cos", CHAT, False))
        self.assertIsInstance(ctx["turn"], taint.Turn)
        self.assertIsNone(server.CANVAS_TOKENS.lookup(kw["canvas_token"]))

    def hold(self):
        cid = cv.dispatch(self.root, "demo", cv.Actor("owner"), "create-canvas", {"title": "Plan"})["id"]
        with self.assertRaises(cv.NeedsApproval):
            cv.dispatch(self.root, "demo", cv.Actor("bot", "research", chat=CHAT), "delete-canvas",
                        {"id": cid, "version": 1})
        self.assertEqual(len(cv.pending_holds(self.root, CHAT)), 1)
        return cid

    def test_approve_runs_holds_once_reject_drops(self):
        cid = self.hold()
        real = cv.run_holds
        with mock.patch.object(server.canvases, "run_holds", side_effect=real) as ran:
            self.run_turn({"chat_id": CHAT, "approval": "approved"})
            self.run_turn({"chat_id": CHAT, "approval": "approved"})
        self.assertEqual(ran.call_count, 2)  # once per approval; the second finds none
        self.assertNotIn(cid, cs.list_ids(self.root, "demo", "cv"))  # the held delete ran
        cid = self.hold()
        self.run_turn({"chat_id": CHAT, "approval": "rejected"})
        self.assertEqual(cv.pending_holds(self.root, CHAT), [])
        self.assertIn(cid, cs.list_ids(self.root, "demo", "cv"))
        self.hold()
        self.run_turn({"chat_id": CHAT})  # a new message answered: dropped
        self.assertEqual(cv.pending_holds(self.root, CHAT), [])
        self.hold()
        code, _ = request_owner("/api/approvals/dismiss", {"chat": CHAT})
        self.assertEqual(code, 200)
        self.assertEqual(cv.pending_holds(self.root, CHAT), [])
        self.assertIn(cid, cs.list_ids(self.root, "demo", "cv"))

    def test_approve_tells_the_bot_and_the_owner_what_ran(self):
        cid = self.hold()  # will run: delete-canvas cid
        gone = cv.dispatch(self.root, "demo", cv.Actor("owner"), "create-canvas", {"title": "Old"})["id"]
        with self.assertRaises(cv.NeedsApproval):
            cv.dispatch(self.root, "demo", cv.Actor("bot", "research", chat=CHAT), "delete-canvas",
                        {"id": gone, "version": 1})
        cv.dispatch(self.root, "demo", cv.Actor("owner"), "delete-canvas", {"id": gone})  # its hold will fail
        seen = self.run_turn({"chat_id": CHAT, "approval": "approved"})
        prompt = seen[0]["_prompt"]
        self.assertTrue(prompt.startswith("Approved canvas actions already ran: "), prompt[:200])
        self.assertIn(f"delete-canvas {cid}: done", prompt)
        self.assertIn(f"delete-canvas {gone}: failed (", prompt)
        self.assertIn("Do not repeat them.", prompt)
        notices = [json.loads(line[6:])["notice"] for line in self.out.splitlines()
                   if line.startswith("data: {") and '"notice"' in line]
        self.assertIn(f"Approved canvas action delete-canvas {cid}: done", notices)
        self.assertTrue(any(n.startswith(f"Approved canvas action delete-canvas {gone}: failed (") for n in notices),
                        notices)
        self.assertNotIn(cid, cs.list_ids(self.root, "demo", "cv"))
        rec = server.chat_store.load(CHAT)
        saved = " ".join(n for m in (rec or {}).get("messages", []) for n in m.get("notices") or [])
        self.assertIn(f"delete-canvas {cid}: done", saved)

    def test_no_summary_without_held_actions(self):
        seen = self.run_turn({"chat_id": CHAT, "approval": "approved"})
        self.assertNotIn("Approved canvas actions", seen[0]["_prompt"])

    def test_malformed_holds_file_never_stops_the_turn(self):
        import time
        (self.root / "team" / "canvas_holds.json").write_text(json.dumps(
            {CHAT: [{"id": "hdx", "actor": "bad", "created_at": time.time()}]}))
        with mock.patch("traceback.print_exc"):
            for answer in ("approved", "rejected", None):
                seen = self.run_turn({"chat_id": CHAT, **({"approval": answer} if answer else {})})
                self.assertEqual(len(seen), 1, answer)

    def test_a_person_never_runs_holds(self):
        self.hold()
        with mock.patch.object(server.canvases, "run_holds") as ran:
            server._canvas_holds_answered(CHAT, "approved", {"kind": "person", "id": "p1"})
        ran.assert_not_called()
        self.assertEqual(cv.pending_holds(self.root, CHAT), [])


class JobRules(Base):
    """I4: an Auto job's waits and the no-save-after-outside-content rule hold at the endpoint
    itself (a shell that skips osctl, or unsets LEOBOT_JOB, still reaches only this)."""

    def setUp(self):
        super().setUp()
        self.cid = cv.dispatch(self.root, "demo", cv.Actor("owner"), "create-canvas",
                               {"title": "Plan", "pin": "demo/general",
                                "blocks": [{"type": "text", "md": "a"}, {"type": "text", "md": "b"}]})["id"]
        self.lid = cv.dispatch(self.root, "demo", cv.Actor("owner"), "create-list",
                               {"title": "Leads", "columns": [{"label": "Name"}]})["id"]

    def job(self, mode="auto", **over):
        return self.mint(job="j1", job_mode=mode, chat=CHAT, **over)

    def test_auto_job_waits_on_the_waits_set(self):
        tok, _ = self.job()
        v = cs.read(self.root, "demo", self.cid)["version"]
        for cmd, params in (("unpin-canvas", {"id": self.cid, "room": "demo/general"}),
                            ("move-block", {"id": self.cid, "block": "bk2", "version": v}),
                            ("update-canvas", {"id": self.cid, "title": "x", "version": v}),
                            ("save-template", {"from": self.cid, "name": "x"}),
                            ("update-list", {"id": self.lid, "title": "x", "version": 1}),
                            ("undo", {"id": self.cid, "change": "ch" + "0" * 12}),
                            ("delete-block", {"id": self.cid, "block": "bk2", "version": v})):
            code, out = self.call(tok, cmd, **params)
            self.assertEqual((code, out["ok"]), (200, False), cmd)
            self.assertIn("APPROVAL:", out["error"], cmd)
        self.assertEqual(cs.read(self.root, "demo", self.cid)["version"], v)
        self.assertEqual(cs.pins(self.root, "demo"), {"demo/general": [self.cid]})
        self.assertEqual(cs.list_ids(self.root, "demo", "tp"), [])
        # what an Auto job does by itself still runs
        self.assertTrue(self.call(tok, "add-row", id=self.lid, cells={"name": "x"})[1]["ok"])
        self.assertTrue(self.call(tok, "get-canvas", id=self.cid)[1]["ok"])

    def test_auto_job_saves_nothing_after_outside_content(self):
        tok, turn = self.job()
        turn.saw_outside()
        code, out = self.call(tok, "add-row", id=self.lid, cells={"name": "x"})
        self.assertEqual((code, out["ok"]), (200, False))
        self.assertEqual(out["error"], taint.SAVE_WAITS)
        self.assertEqual(cs.read(self.root, "demo", self.lid)["rows"], [])
        self.assertTrue(self.call(tok, "get-list", id=self.lid)[1]["ok"])  # reads stay open

    def test_approval_and_report_jobs_only_read(self):
        for mode in ("approval", "report", None):
            tok, _ = self.job(mode)  # readonly not set on the token: the mode alone decides
            code, out = self.call(tok, "add-row", id=self.lid, cells={"name": "x"})
            self.assertEqual((code, out["ok"]), (200, False), mode)
            self.assertTrue(self.call(tok, "list-canvases")[1]["ok"], mode)
        self.assertEqual(cs.read(self.root, "demo", self.lid)["rows"], [])

    def test_job_mode_is_read_live(self):
        state = {"mode": "auto"}
        tok, _ = self.job(job_mode_now=lambda: state["mode"])
        self.assertTrue(self.call(tok, "add-row", id=self.lid, cells={"name": "a"})[1]["ok"])
        state["mode"] = "approval"  # the run was switched to approval mid-run
        self.assertFalse(self.call(tok, "add-row", id=self.lid, cells={"name": "b"})[1]["ok"])
        self.assertEqual(len(cs.read(self.root, "demo", self.lid)["rows"]), 1)

    def test_auto_job_past_its_skill_saves_nothing(self):
        with mock.patch.object(server.jobs, "get", return_value={"id": "j1", "skill": "own"}), \
                mock.patch.object(server.skill_loads, "loaded", return_value=["other"]):
            tok, _ = self.job()
            out = self.call(tok, "add-row", id=self.lid, cells={"name": "x"})[1]
        self.assertFalse(out["ok"])
        self.assertEqual(cs.read(self.root, "demo", self.lid)["rows"], [])

    def test_private_thread_is_not_a_job(self):
        tok, turn = self.mint()
        turn.saw_outside()  # a private thread labels its writes instead
        self.assertTrue(self.call(tok, "add-row", id=self.lid, cells={"name": "x"})[1]["ok"])

    def test_waits_set_is_shared_with_osctl(self):
        from dashboard import osctl
        self.assertIs(osctl._CANVAS_AUTO_WAITS, cv.AUTO_WAITS)
        self.assertEqual(server.CANVAS_READS, cv.READS)

    def test_run_mints_a_live_job_mode(self):
        class S:
            job_mode = "auto"
        ctx = server._canvas_token_ctx(ws="demo", bot="b", job={"id": "j1"}, s_=S(), sess=S(), room=None,
                                       shared=False, turn=taint.Turn(), gated=True, sender=None)
        self.assertEqual(ctx["job_mode_now"](), "auto")
        S.job_mode = "approval"
        self.assertEqual(ctx["job_mode_now"](), "approval")


class TrashPurge(Base):
    """I6: the server purges old canvas trash at start and daily, in every workspace and HQ."""

    def test_loop_purges_each_round_and_survives_errors(self):
        waits = []
        with mock.patch.object(server.canvas_store, "purge_all", side_effect=[OSError("disk"), {}]) as purge, \
                mock.patch("traceback.print_exc"):
            server._canvas_trash_loop(sleep=waits.append, rounds=2)
        self.assertEqual([c.args for c in purge.call_args_list], [(self.root,), (self.root,)])
        self.assertEqual(waits, [server.CANVAS_TRASH_EVERY])

    def test_loop_removes_old_trash(self):
        cid = cv.dispatch(self.root, "demo", cv.Actor("owner"), "create-canvas", {"title": "Old"})["id"]
        cv.dispatch(self.root, "demo", cv.Actor("owner"), "delete-canvas", {"id": cid})
        os.utime(cs.base(self.root, "demo") / "canvases" / ".trash" / f"{cid}.json", (0, 0))
        server._canvas_trash_loop(sleep=lambda s: None, rounds=1)
        self.assertEqual(list((cs.base(self.root, "demo") / "canvases" / ".trash").glob("cv*")), [])

    def test_main_starts_the_loop(self):
        import inspect
        self.assertIn("target=_canvas_trash_loop", inspect.getsource(server.main))


def request_owner(path, body):
    h = server.Handler.__new__(server.Handler)
    out = {}
    h.wfile = io.BytesIO()
    h.send_response = lambda code, *a: out.setdefault("code", code)
    h.send_header = lambda *a: None
    h.end_headers = lambda: None
    h.log_message = lambda *a: None
    raw = json.dumps(body).encode()
    msg = Message()
    msg["Host"] = f"127.0.0.1:{PORT}"
    msg["Content-Type"] = "application/json"
    msg["Content-Length"] = str(len(raw))
    msg["Cookie"] = f"{server.AUTH_COOKIE}={server.AUTH_TOKEN}"
    msg["Origin"] = f"http://127.0.0.1:{PORT}"
    h.headers, h.rfile, h.path = msg, io.BytesIO(raw), path
    h.client_address = ("127.0.0.1", 50000)
    h.server = SimpleNamespace(server_address=("127.0.0.1", PORT))
    h.do_POST()
    payload = h.wfile.getvalue()
    return out["code"], (json.loads(payload) if payload else None)


if __name__ == "__main__":
    unittest.main()
