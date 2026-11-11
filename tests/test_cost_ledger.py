"""Harness upgrade phase 1: token parts, per-reply ledger fields, caps wiring, cost report."""
import io
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from dashboard import chat_session
from dashboard.engines import api, claude_code, codex, prices
import dashboard.server as server
from tests.test_teammates import VENTURE

JS = (Path(__file__).resolve().parents[1] / "dashboard" / "app.js").read_text()
USAGE = {"in": 100, "cache_write": 200, "cache_read": 1000, "out": 0}


def env(**kw):
    return mock.patch.dict(os.environ, kw)


class EngineUsage(unittest.TestCase):
    def test_claude_code_parts(self):
        _, p = claude_code.parse_event({"type": "result", "total_cost_usd": 0.01, "usage": {
            "input_tokens": 10, "cache_creation_input_tokens": 200,
            "cache_read_input_tokens": 1000, "output_tokens": 90}})
        self.assertEqual(p["usage"], {"in": 10, "cache_write": 200, "cache_read": 1000, "out": 90})
        self.assertEqual(p["tokens"], 1300)

    def test_codex_cached_split(self):
        (_, p), = codex.parse_event({"type": "turn.completed", "usage": {
            "input_tokens": 100, "cached_input_tokens": 60, "output_tokens": 5}})
        self.assertEqual(p["usage"], {"in": 40, "cache_write": 0, "cache_read": 60, "out": 5})
        self.assertEqual(p["tokens"], 105)

    def test_openai_compatible_cached_split(self):
        eng = api.ApiEngine.__new__(api.ApiEngine)
        _, p = eng._done(["x"], {"prompt_tokens": 50, "completion_tokens": 7, "cached_tokens": 20}, "some-model")
        self.assertEqual(p["usage"], {"in": 30, "cache_write": 0, "cache_read": 20, "out": 7})
        self.assertEqual(p["tokens"], 57)

    def test_anthropic_api_parts_and_estimate(self):
        eng = api.ApiEngine.__new__(api.ApiEngine)
        _, p = eng._done(["x"], {"input_tokens": 1_000_000, "output_tokens": 0}, "claude-haiku-5-5")
        self.assertEqual(p["usage"], {"in": 1_000_000, "cache_write": 0, "cache_read": 0, "out": 0})
        self.assertAlmostEqual(p["cost"], 0.10)

    def test_estimate(self):
        u = {"in": 1_000_000, "cache_write": 0, "cache_read": 0, "out": 0}
        self.assertAlmostEqual(prices.estimate("claude-haiku-5-5", u), 0.10)
        self.assertIsNone(prices.estimate("unknown-model", u))
        self.assertIsNone(prices.estimate("claude-haiku-5-5", None))
        self.assertIs(api.PRICES, prices.PRICES)



class AskHarness(unittest.TestCase):
    """One chat turn through the real ask path with fake engine sessions.
    `replies`: one string, or a list with one reply per engine run in order
    (lead, teammate, follow-up). `engine_calls` counts runs that started."""

    SID = "55555555-0000-0000-0000-000000000000"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "projects" / "acme").mkdir(parents=True)
        self._chats = server.chat_store.CHATS_DIR
        server.chat_store.CHATS_DIR = self.root / "chats"
        self.engine_calls = 0

    def tearDown(self):
        server.chat_store.CHATS_DIR = self._chats
        self.tmp.cleanup()

    def seed_spend(self, owner, ws):
        server.chat_store.save({"id": "66666666-0000-0000-0000-000000000000", "title": "earlier",
                                "created": time.time(), "messages": [],
                                "spend": [{"ts": time.time(), "by": "owner", "cost": owner, "ws": ws}]})

    def ask(self, msg, replies, *, cost=0.02, usage=USAGE, done=None):
        queue = [replies] if isinstance(replies, str) else list(replies)
        payload = done if done is not None else {"tokens": 1300, "cost": cost, "usage": usage}
        harness = self

        def stream():
            harness.engine_calls += 1
            text = queue.pop(0) if queue else ""
            yield ("delta", text)
            yield ("done", dict(payload))

        class Lead:
            _started = True
            _turn_count = 1
            _last_skill = None
            _skill_explicit = False
            _last_bot = None
            session_id = self.SID

            def is_fresh(self):
                return False

            def note_skill(self, *a, **k):
                pass

            def note_bot(self, *a):
                pass

            def ask(self, text, **kw):
                yield from stream()

        class Mate:
            def __init__(self, repo_dir, rail):
                self.readonly = False

            def ask(self, text, **kw):
                yield from stream()

        sink = io.BytesIO()
        h = server.Handler.__new__(server.Handler)
        h.wfile = sink
        h.send_response = h.send_header = h.end_headers = lambda *a, **k: None
        h.server = mock.Mock(server_address=("127.0.0.1", 8765))
        with mock.patch.object(server, "ROOT", self.root), \
                mock.patch.object(server.db, "tree", return_value=[VENTURE]), \
                mock.patch.object(server.db, "posts", return_value=[]), \
                mock.patch.object(server, "state_snapshot", side_effect=lambda p, only=None: "SNAP"), \
                mock.patch.object(server, "get_chat_session", return_value=Lead()), \
                mock.patch.object(server.chat_session, "ChatSession", Mate):
            h._handle_ask({"messages": [{"role": "user", "content": msg}],
                           "room_id": "acme/general", "chat_id": self.SID})
        return sink.getvalue().decode(), server.chat_store.load(self.SID)


class LedgerFields(AskHarness):
    def test_reason_codes(self):
        self.assertEqual(server._pick_model_reason(None, False, "what is our plan?"),
                         (chat_session.CHAT_MODEL, "default"))
        self.assertEqual(server._pick_model_reason(None, True, "x")[1], "web")
        self.assertEqual(server._pick_model_reason("strategy", False, "draft it", explicit=True)[1],
                         "explicit-skill")
        self.assertEqual(server._pick_model_reason("tokensave", False, "x")[1], "tokensave")
        self.assertEqual(server._pick_model("strategy", False, "draft it", explicit=True),
                         chat_session.ESCALATED_MODEL)

    def test_reply_records_usage_skill_reason(self):
        _, rec = self.ask("@cmo positioning for acme", "ok")
        last = rec["messages"][-1]
        self.assertEqual(last["usage"]["cache_read"], 1000)
        self.assertEqual(last["tokens"], 1300)
        self.assertTrue(last["metered"])
        self.assertIn(last["reason"], {"explicit-skill", "explicit-read", "default"})
        self.assertIn("skill", last)
        self.assertEqual(rec["spend"][-1]["ws"], "acme")

    def test_cost_estimated_when_engine_reports_none(self):
        with mock.patch.object(claude_code.ClaudeCodeEngine, "resolve_model", return_value="claude-haiku-5-5"):
            _, rec = self.ask("@cmo positioning for acme", "ok", done={
                "tokens": 1_000_000, "cost": None,
                "usage": {"in": 1_000_000, "cache_write": 0, "cache_read": 0, "out": 0}})
        last = rec["messages"][-1]
        self.assertAlmostEqual(last["cost"], 0.10)
        self.assertTrue(last["metered"])

    def test_unpriced_reply_is_unmetered(self):
        _, rec = self.ask("@cmo positioning for acme", "ok", done={"tokens": 50, "cost": None, "usage": None})
        self.assertFalse(rec["messages"][-1]["metered"])
        self.assertNotIn("spend", rec)

    def test_teammate_reply_carries_its_own_reason(self):
        _, rec = self.ask("@cmo plan for acme", ["Lead.\nCONSULT @research: rivals?", "Mate.", "Done."])
        mate = [m for m in rec["messages"] if m.get("bot") == "research"][0]
        self.assertIn(mate["reason"], {"teammate", "teammate-skill"})
        self.assertEqual(rec["messages"][-1]["reason"], rec["messages"][1]["reason"])



def runner_online(owner):
    return mock.patch.object(server.runners.HUB, "online", side_effect=lambda who: who == owner)


def events(out):
    return [json.loads(l[6:]) for l in out.splitlines() if l.startswith("data: {")]


class CapWiring(AskHarness):
    def test_capped_turn_never_starts_engine(self):
        self.seed_spend(owner=6.0, ws="acme")
        with env(LEOBOT_OWNER_DAILY_CAP="5"):
            out, _ = self.ask("@cmo positioning for acme", "should not run")
        self.assertIn("daily cap", out)
        self.assertEqual(self.engine_calls, 0)

    def test_workspace_cap_refuses_too(self):
        self.seed_spend(owner=6.0, ws="acme")
        with env(LEOBOT_WORKSPACE_DAILY_CAP="5"):
            out, _ = self.ask("@cmo positioning for acme", "should not run")
        self.assertIn("this workspace reached", out)
        self.assertEqual(self.engine_calls, 0)

    def test_teammate_refused_keeps_lead_reply(self):
        # the lead's own cost (not yet saved) pushes the owner past the cap before the consult
        with env(LEOBOT_OWNER_DAILY_CAP="0.01"):
            out, rec = self.ask("@cmo plan for acme", ["Lead text.\nCONSULT @research: rivals?", "never"], cost=0.02)
        evs = events(out)
        self.assertTrue(any("notice" in e for e in evs))
        self.assertFalse(any("error" in e for e in evs))
        self.assertFalse(any("msg_start" in e for e in evs))  # no empty teammate bubble
        self.assertEqual(self.engine_calls, 1)
        last = rec["messages"][-1]
        self.assertEqual(last["content"], "Lead text.")
        self.assertTrue(any("daily cap" in n for n in last["notices"]))  # saved: survives the reload

    def test_followup_refused_after_teammate(self):
        with env(LEOBOT_OWNER_DAILY_CAP="0.03"):
            out, rec = self.ask("@cmo plan for acme",
                                ["Lead.\nCONSULT @research: rivals?", "Mate.", "never"], cost=0.02)
        self.assertEqual(self.engine_calls, 2)
        self.assertEqual([m.get("bot") for m in rec["messages"][1:]], ["cmo", "research"])
        self.assertFalse(any("error" in e for e in events(out)))

    def test_runner_turn_ignores_caps(self):
        self.seed_spend(owner=6.0, ws="acme")
        with env(LEOBOT_OWNER_DAILY_CAP="5", LEOBOT_WORKSPACE_DAILY_CAP="5"), runner_online("owner"):
            _, rec = self.ask("@cmo positioning for acme", "ran", done={"tokens": 10, "cost": None, "usage": None})
        self.assertEqual(self.engine_calls, 1)
        last = rec["messages"][-1]
        self.assertNotIn("metered", last)
        self.assertNotIn("notices", last)

    def test_no_cap_set_changes_nothing(self):
        self.seed_spend(owner=600.0, ws="acme")
        out, _ = self.ask("@cmo positioning for acme", "ran")
        self.assertEqual(self.engine_calls, 1)
        self.assertNotIn('"notice"', out)

    def test_near_cap_notice_once(self):
        self.seed_spend(owner=4.0, ws="acme")
        with env(LEOBOT_OWNER_DAILY_CAP="5"):
            out, rec = self.ask("@cmo positioning for acme", "ran", cost=0.5)
            self.assertIn("Heads up: 90%", out)
            self.assertIn("Heads up: 90%", rec["messages"][-1]["notices"][0])
            out2, _ = self.ask("@cmo positioning for acme", "ran", cost=0.01)
        self.assertNotIn('"notice"', out2)

    def test_invited_person_cap_holds_mid_message(self):
        with env(LEOBOT_PERSON_DAILY_CAP="0.01"):
            refused = server.spend.refusal("acme", "p1", pending=0.02, records=server.chat_store.records)
        self.assertIn("allowance", refused)

    def test_zero_model_reply_not_refused(self):
        self.seed_spend(owner=6.0, ws="acme")
        with env(LEOBOT_OWNER_DAILY_CAP="5"):
            out, _ = self.ask("save a positioning memo for acme", "should not run")
        self.assertNotIn("daily cap", out)  # the tag-gate hint costs nothing
        self.assertEqual(self.engine_calls, 0)

    def test_ui_handles_notice(self):
        self.assertIn("if (obj.notice)", JS)
        self.assertIn("m.notices", JS)



def thread(ws, msgs):
    return {"id": f"t-{ws}", "room": f"{ws}/general", "messages": msgs}


LABELS = {"claude-code": "Claude Code", "codex": "Codex"}


def reply(bot, *, engine, model, skill, cost, tokens, usage, metered=True, ts=None):
    return {"role": "assistant", "bot": bot, "ts": ts or time.time(), "engine": engine,
            "engine_label": LABELS.get(engine), "model": model,
            "skill": skill, "cost": cost, "tokens": tokens, "usage": usage, "metered": metered}


class Report(unittest.TestCase):
    def test_groupings_and_cache_share(self):
        from dashboard import costs
        recs = [thread("acme", [reply("cmo", engine="claude-code", model="haiku", skill="strategy",
                                      cost=0.02, tokens=1300, usage=USAGE),
                                reply("cfo", engine="codex", model="gpt", skill=None, cost=None,
                                      tokens=50, usage=None, metered=False)]),
                thread("demo", [reply("cmo", engine="claude-code", model="haiku", skill="strategy",
                                      cost=5.0, tokens=9, usage=None)])]
        out = costs.summarize("acme", recs, {"acme", "demo"})
        self.assertAlmostEqual(out["by_model"]["haiku"]["cost"], 0.02)  # demo's reply is not counted
        self.assertEqual(out["by_skill"]["-"]["turns"], 1)
        self.assertEqual(out["by_engine"]["codex"]["tokens"], 50)
        self.assertAlmostEqual(out["cache_read_share"], 1000 / 1300)
        self.assertEqual(out["unmetered"], 1)
        self.assertEqual(list(out["by_day"]), [time.strftime("%Y-%m-%d")])
        self.assertEqual(out["total"]["turns"], 2)
        self.assertEqual(out["bots"]["cmo"]["turns"], 1)
        self.assertEqual(out["by_engine"]["claude-code"]["label"], "Claude Code")
        self.assertEqual(out["by_skill"]["-"]["label"], "no skill")
        hq = costs.summarize("hq", recs, {"acme", "demo"})
        self.assertEqual(hq["total"]["turns"], 3)

    def test_old_reply_without_new_fields(self):
        from dashboard import costs
        recs = [thread("acme", [{"role": "assistant", "bot": "cmo", "ts": time.time(), "tokens": 1500, "cost": 0.02}])]
        out = costs.summarize("acme", recs, {"acme"})
        self.assertEqual(out["total"]["tokens"], 1500)
        self.assertEqual(out["by_engine"]["-"]["turns"], 1)
        self.assertEqual(out["by_engine"]["-"]["label"], "not recorded")
        self.assertIsNone(out["cache_read_share"])
        self.assertEqual(out["unmetered"], 0)

    def test_old_and_bad_rows_never_crash(self):
        from dashboard import costs
        recs = [thread("acme", [{"role": "assistant", "ts": time.time() - 40 * 86_400, "cost": 9},
                                {"role": "assistant", "ts": time.time(), "tokens": "x", "cost": "y",
                                 "usage": {"in": "z"}, "skill": ["odd"]}])]
        out = costs.summarize("acme", recs, {"acme"})
        self.assertEqual(out["total"]["turns"], 1)  # the 40-day-old one is outside the window

    def test_server_costs_wraps_summarize(self):
        with mock.patch.object(server.chat_store, "records", return_value=[]):
            self.assertIn("by_model", server._costs("hq", []))

    def test_ui_shows_breakdown(self):
        for s in ("by_model", "by_skill", "cache_read_share", "unmetered", "team-spend"):
            self.assertIn(s, JS)


if __name__ == "__main__":
    unittest.main()
