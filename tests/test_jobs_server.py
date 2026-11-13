"""A scheduled job's turn through the server's ask path (server.run_job), on a fake engine."""
import io
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from core import approvals, bots, folders, jobs
import dashboard.server as server

WS = "acme"


class _Engine:
    id, label, kind = "api:test", "Test API", "api"
    shared_safe, full_disk = True, False
    supports = frozenset({"web", "apps", "folder_edit", "complete"})

    def resolve_model(self, model):
        return model

    def is_fresh(self, session):
        return True


class JobTurn(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        (self.root / "projects" / WS / "team").mkdir(parents=True)
        (self.root / "projects" / WS / "project.md").write_text(f"---\nname: {WS}\nkind: venture\n---\n")
        (self.root / "team").mkdir()
        self.reply = "Weekly numbers.\nAPPROVAL: save the review memo to sec04"
        self.events = None
        self.asks = []
        self.metered = True
        test = self

        def ask(sess, text, with_web=False, model=None, read_dirs=(), edit_dirs=(), **kw):
            test.asks.append({"sess": sess, "text": text, "readonly": sess.readonly,
                              "edit_dirs": tuple(edit_dirs), "with_web": with_web, "kw": kw})
            for ev in test.events or [("delta", test.reply), ("done", {"cost": 0.01})]:
                yield ev

        tree = lambda: [{"slug": WS, "kind": "venture", "type": "project", "profiles": [],  # noqa: E731
                         "folders": folders.load(WS, self.root)["folders"]}]
        self.pick = mock.Mock(return_value=_Engine())
        for p in [
            mock.patch.object(server, "ROOT", self.root),
            mock.patch.object(server.fileops, "ROOT", self.root),
            mock.patch.object(server.db, "tree", side_effect=tree),
            mock.patch.object(server.db, "posts", return_value=[]),
            mock.patch.object(server.chat_session.ChatSession, "ask", ask),
            mock.patch.object(server.engine_registry, "pick", self.pick),
            mock.patch.object(server.engine_registry, "metered", side_effect=lambda e: self.metered),
            mock.patch.object(server.chat_store, "CHATS_DIR", self.root / "chats"),
            mock.patch.object(server.memory_consolidate, "maybe_start"),
            mock.patch.object(server, "_shared_room", return_value=False),
            mock.patch.object(server.integrations, "api_key", return_value=None),
            mock.patch.dict("os.environ", {"LEOBOT_OWNER_DAILY_CAP": "", "LEOBOT_WORKSPACE_DAILY_CAP": ""}),
        ]:
            p.start()
            self.addCleanup(p.stop)
        self.srv = mock.Mock(server_address=("127.0.0.1", 8765))

    def job(self, **kw):
        return {"id": "j1", "name": "weekly", "workspace": WS, "bot": "cmo", "skill": "positioning",
                "message": "review the week", "schedule": "mon 09:00", "cap": 0.5, "enabled": True, **kw}

    def run_job(self, **kw):
        return server.run_job(self.job(**kw), self.srv)

    def test_running_job_spend_is_live_for_its_cap(self):
        test, seen = self, []

        def ask(sess, text, **kw):
            seen.append(jobs.live_spent(test.root, "j1"))  # before any call ends
            yield ("spent", {"usage": None, "cost": 0.3})  # a call ended: a generate run would see it
            seen.append(jobs.live_spent(test.root, "j1"))
            # No cost from the engine: priced from its tokens by the model the turn asked for.
            yield ("spent", {"usage": {"in": 100_000, "cache_write": 0, "cache_read": 0, "out": 0}, "cost": None})
            seen.append(jobs.live_spent(test.root, "j1") > 0)
            yield ("delta", "Updated the draft list.")
            yield ("done", {"cost": 0.4})
        with mock.patch.object(server.chat_session.ChatSession, "ask", ask):
            out = self.run_job(mode="auto")
        self.assertEqual(seen, [0.0, 0.3, True])
        self.assertAlmostEqual(out["cost"], 0.4)
        self.assertEqual(jobs.live_spent(self.root, "j1"), 0.0)  # saved with the turn, no longer live
        self.assertFalse((self.root / jobs.LIVE_RELPATH).exists())

    def test_job_turn_is_read_only_and_proposes(self):
        out = self.run_job()
        self.assertEqual(out["status"], "waiting", out)
        self.assertEqual(len(self.asks), 1)
        a = self.asks[0]
        self.assertTrue(a["readonly"])
        self.assertEqual(a["edit_dirs"], ())
        self.assertIn("## Scheduled job", a["text"])
        self.assertIn('"weekly"', a["text"])
        rec = server.chat_store.load(out["chat"])
        self.assertEqual((rec["title"], rec["job"], rec["room"]), ("Job: weekly", "j1", f"{WS}/dm-cmo"))
        self.assertEqual(rec["spend"][-1]["job"], "j1")
        self.assertEqual(rec["messages"][0]["content"], "@cmo /positioning review the week")
        self.assertEqual(rec["messages"][-1]["job"], "j1")
        pend = approvals.pending(self.root, WS)
        self.assertEqual([(p["chat"], p["summary"]) for p in pend], [(out["chat"], "save the review memo to sec04")])
        self.assertAlmostEqual(out["cost"], 0.01)

    def test_auto_job_saves_by_itself(self):
        self.reply = "Updated the draft list."
        out = self.run_job(mode="auto")
        self.assertEqual(out["status"], "done")
        a = self.asks[0]
        self.assertFalse(a["readonly"])
        self.assertEqual((a["sess"].job_id, a["sess"].job_mode), ("j1", "auto"))
        self.assertEqual(a["edit_dirs"], ())
        self.assertIn("Auto", a["text"])
        self.assertIn("APPROVAL", a["text"])  # deletes and memory still go through one

    def test_report_job_drops_the_approval_line(self):
        out = self.run_job(mode="report")
        self.assertEqual(out["status"], "done")
        a = self.asks[0]
        self.assertTrue(a["readonly"])
        self.assertEqual(a["sess"].job_mode, "report")
        self.assertIn("Mode: Report only", a["text"])
        self.assertEqual(approvals.pending(self.root), [])
        rec = server.chat_store.load(out["chat"])
        self.assertNotIn("APPROVAL", rec["messages"][-1]["content"])
        self.assertIn("Weekly numbers.", rec["messages"][-1]["content"])

    def test_unknown_mode_runs_as_approval(self):
        out = self.run_job(mode="whatever")
        self.assertEqual(out["status"], "waiting")
        self.assertTrue(self.asks[0]["readonly"])
        self.assertEqual(self.asks[0]["sess"].job_mode, "approval")

    def test_report_job_app_token_refuses_writes(self):
        minted = []
        real = server.apps_relay.TOKENS.mint
        with mock.patch.object(server.apps_relay.TOKENS, "mint", side_effect=lambda ctx: minted.append(ctx) or real(ctx)), \
                mock.patch.object(server.integrations, "api_key", return_value="k"), \
                mock.patch.object(server.integrations, "effective_grant", return_value={"gmail": True}), \
                mock.patch.object(server, "_needs_apps", return_value=True):
            self.run_job(mode="report", message="draft the weekly email")
            self.run_job(mode="auto", message="draft the weekly email")
        self.assertEqual([bool(c.get("report")) for c in minted], [True, False])

    def test_job_turn_never_hands_off(self):
        bots.set_active("research", True, self.root, WS, {"slug": WS, "kind": "venture"})
        self.reply = "Research should size it.\nHANDOFF @research: size the market"
        out = self.run_job(mode="auto")
        self.assertEqual(len(self.asks), 1)
        rec = server.chat_store.load(out["chat"])
        self.assertNotIn("HANDOFF", rec["messages"][-1]["content"])
        self.assertTrue(any("does not hand off" in n for n in rec["messages"][-1].get("notices", [])))

    def test_job_turn_searches_the_web(self):
        # A research job runs unattended end to end: web search on, fetch hosts none (below).
        self.run_job(mode="auto")
        self.assertTrue(self.asks[0]["with_web"])

    def test_plain_reply_is_done(self):
        self.reply = "Nothing changed this week."
        out = self.run_job()
        self.assertEqual(out["status"], "done")
        self.assertEqual(approvals.pending(self.root), [])

    def test_owner_thread_untouched(self):
        mine = server.chat_session.ChatSession(repo_dir=".", rail="")
        with mock.patch.object(server, "_CHAT", mine):
            self.run_job()
            self.assertIs(server._CHAT, mine)
        self.assertIsNot(self.asks[0]["sess"], mine)

    def test_job_cap_stops_before_engine(self):
        cid = "0" * 8 + "-0000-0000-0000-" + "0" * 12
        server.chat_store.save({"id": cid, "spend": [{"ts": time.time(), "by": "owner", "cost": 1.0,
                                                      "ws": WS, "job": "j1"}]})
        out = self.run_job()
        self.assertEqual(out["status"], "capped")
        self.assertIn("this job reached its cap", out["detail"])
        self.assertEqual(self.asks, [])
        self.assertEqual(self.run_job(id="j2")["status"], "waiting")  # another job has its own cap

    def test_unmetered_engine_refused(self):
        self.metered = False
        out = self.run_job()
        self.assertEqual(out["status"], "refused")
        self.assertIn("does not report cost", out["detail"])
        self.assertEqual(self.asks, [])

    def test_stale_job_refused_without_engine(self):
        bots.set_active("cmo", False, self.root, WS, {"slug": WS, "kind": "venture"})
        self.assertEqual(self.run_job()["status"], "refused")
        bots.set_active("cmo", True, self.root, WS, {"slug": WS, "kind": "venture"})
        self.assertEqual(self.run_job(skill="market-sizing")["status"], "refused")
        out = self.run_job(workspace="gone")
        self.assertEqual(out["status"], "refused")
        self.assertIn("gone", out["detail"])
        self.pick.assert_not_called()
        self.assertEqual(self.asks, [])

    def test_failed_engine_turn(self):
        self.events = [("delta", "half"), ("error", "boom")]
        out = self.run_job()
        self.assertEqual(out["status"], "failed")
        self.assertEqual(out["detail"], "boom")

    def test_stop_job_closes_the_turn_in_flight(self):
        closed = []
        test = self

        def ask(sess, text, **kw):
            sess.close = lambda: closed.append(sess)
            server.stop_job()  # the timer fires mid-turn
            yield ("delta", "partial")
            yield ("done", {"cost": 0.01})
        with mock.patch.object(server.chat_session.ChatSession, "ask", ask):
            test.run_job()
        self.assertEqual(len(closed), 1)
        self.assertIsNone(server._JOB_RUN["live"])

    def test_stopped_job_starts_no_teammate_or_followup(self):
        runs = []

        def ask(sess, text, **kw):
            runs.append(text)
            if len(runs) == 1:
                server.stop_job()  # the timeout fires during the lead's run
            yield ("delta", "Lead.\nCONSULT @research: rivals?" if len(runs) == 1 else "more")
            yield ("done", {"cost": 0.01})
        with mock.patch.object(server.chat_session.ChatSession, "ask", ask):
            self.run_job()
        self.assertEqual(len(runs), 1)

    def test_teammate_on_unmetered_engine_is_not_consulted(self):
        mate = _Engine()
        mate.id = "api:mate"
        self.pick.side_effect = lambda root, ws, bot, **kw: mate if bot == "research" else _Engine()
        self.metered = None
        runs = []

        def ask(sess, text, **kw):
            runs.append(sess.engine.id)
            yield ("delta", "Lead.\nCONSULT @research: rivals?")
            yield ("done", {"cost": 0.01})
        with mock.patch.object(server.engine_registry, "metered", side_effect=lambda e: e.id != "api:mate"), \
                mock.patch.object(server.chat_session.ChatSession, "ask", ask):
            out = self.run_job()
        self.assertEqual(runs, ["api:test"])
        self.assertEqual(out["status"], "failed")
        self.assertIn("does not report cost", out["detail"])

    def test_job_message_grants_no_web_host(self):
        # A job's message was written by a bot (osctl propose-job), not typed by the owner:
        # a host in it must not become a fetch grant.
        out = self.run_job(message="search the web and check https://example.com/pricing")
        self.assertEqual(out["status"], "waiting")
        self.assertEqual(tuple(self.asks[0]["kw"].get("web_domains") or ()), ())

    def test_held_app_action_is_waiting(self):
        self.reply = "Drafted the email."
        test = self

        def ask(sess, text, **kw):
            approvals.add_app_action(test.root, ws=WS, room=f"{WS}/dm-cmo", chat=sess.session_id, bot="cmo",
                                     person=bots.CEO, toolkit="gmail", slug="GMAIL_SEND_EMAIL",
                                     arguments={"to": "a@example.com"})
            yield ("delta", test.reply)
            yield ("done", {"cost": 0.01})
        with mock.patch.object(server.integrations, "api_key", return_value="k"), \
                mock.patch.object(server.integrations, "effective_grant", return_value={"gmail": True}), \
                mock.patch.object(server, "_needs_apps", return_value=True), \
                mock.patch.object(server.chat_session.ChatSession, "ask", ask):
            out = self.run_job(message="draft the weekly email")
        self.assertEqual(out["status"], "waiting")


    # -- proposals: a bot's propose-job becomes a card; only Approve writes the job ------- #
    def owner_ask(self, msg="@cmo /positioning set up a weekly review job", room=f"{WS}/dm-cmo", propose=None,
                  fail=False):
        """An owner turn whose bot ran `osctl propose-job` (simulated: an outbox row for its thread)."""
        test = self

        def ask(sess, text, **kw):
            for prop in propose or ():
                jobs.add_proposal(test.root, {"chat": sess.session_id, "ws": WS, **prop})
            if fail:  # the engine errors after the proposal
                yield ("error", "engine failed")
            yield ("delta", "Proposed a weekly job.")
            yield ("done", {"cost": 0.01})
        h = server.Handler.__new__(server.Handler)
        h.wfile = io.BytesIO()
        h.server = self.srv
        h.send_response = h.send_header = h.end_headers = lambda *a, **k: None
        made = []

        def session(*a, **k):
            made.append(server.chat_session.ChatSession(repo_dir=".", rail=""))
            return made[-1]
        with mock.patch.object(server.chat_session.ChatSession, "ask", ask), \
                mock.patch.object(server, "get_chat_session", side_effect=session):
            h._handle_ask({"messages": [{"role": "user", "content": msg}], "room_id": room})
        return made[-1].session_id, h.wfile.getvalue().decode()

    NEW = {"op": "create", "fields": {"bot": "cmo", "skill": "positioning", "name": "weekly",
                                      "message": "review the week", "schedule": "mon 09:00", "cap": 0.5,
                                      "mode": "auto"}}

    def post(self, path, body):
        h = server.Handler.__new__(server.Handler)
        sent = []
        h._send = lambda code, obj: sent.append((code, obj))
        h._jobs_post(path, body)
        return sent[-1]

    def test_proposal_becomes_a_card_and_approve_writes_once(self):
        chat, out = self.owner_ask(propose=[self.NEW])
        cards = [r for r in approvals.pending(self.root, WS) if r.get("kind") == approvals.JOB]
        self.assertEqual(len(cards), 1)
        card = cards[0]
        self.assertEqual((card["chat"], card["bot"], card["room"], card["summary"]),
                         (chat, "cmo", f"{WS}/dm-cmo", "New job: weekly"))
        self.assertIn('"app_actions"', out)
        self.assertEqual(jobs.load(self.root), [])  # nothing saved before the click
        self.assertEqual(jobs.take_proposals(self.root, chat), [])  # the outbox was emptied
        done = server._approve_job_card(card["id"])
        self.assertEqual(done["status"], "done", done)
        self.assertTrue(done["result"].startswith("Job created: weekly"))
        got = jobs.load(self.root)
        self.assertEqual([(j["name"], j["mode"], j["workspace"]) for j in got], [("weekly", "auto", WS)])
        with self.assertRaises(server._AppRefused):  # a second click (another tab)
            server._approve_job_card(card["id"])
        self.assertEqual(len(jobs.load(self.root)), 1)
        note = server.chat_store.load(chat)["messages"][-1]
        self.assertIn("Job created", note["content"])

    def test_failed_turn_still_shows_its_card(self):
        chat, out = self.owner_ask(propose=[self.NEW], fail=True)
        self.assertEqual([r["summary"] for r in approvals.pending(self.root, WS)], ["New job: weekly"])
        self.assertIn('"app_actions"', out)
        self.assertEqual(jobs.take_proposals(self.root, chat), [])

    def test_edited_card_saves_the_edit(self):
        self.owner_ask(propose=[self.NEW])
        card = approvals.pending(self.root, WS)[0]
        fields = dict(card["job"]["fields"], cap=2, mode="report")
        server._approve_job_card(card["id"], fields)
        self.assertEqual((jobs.load(self.root)[0]["cap"], jobs.load(self.root)[0]["mode"]), (2.0, "report"))

    def test_bad_edit_and_stale_card_stay_pending(self):
        self.owner_ask(propose=[self.NEW])
        card = approvals.pending(self.root, WS)[0]
        with self.assertRaises(server._AppRefused):
            server._approve_job_card(card["id"], dict(card["job"]["fields"], schedule="whenever"))
        bots.set_active("cmo", False, self.root, WS, {"slug": WS, "kind": "venture"})
        with self.assertRaises(server._AppRefused):
            server._approve_job_card(card["id"])
        self.assertEqual(approvals.get(card["id"], self.root)["status"], "pending")
        self.assertEqual(jobs.load(self.root), [])

    def test_invalid_proposal_is_dropped_with_a_notice(self):
        chat, out = self.owner_ask(propose=[{"op": "create", "fields": dict(self.NEW["fields"], workspace="demo")}])
        self.assertEqual(approvals.pending(self.root), [])
        self.assertIn("Job proposal dropped", out)

    def test_shared_thread_proposals_are_discarded(self):
        with mock.patch.object(server, "_shared_room", return_value=True):
            chat, _ = self.owner_ask(msg="@cmo hello", room=f"{WS}/general", propose=[self.NEW])
        self.assertEqual(approvals.pending(self.root), [])
        self.assertEqual(jobs.take_proposals(self.root, chat), [])

    def test_job_turn_proposals_are_discarded(self):
        test = self

        def ask(sess, text, **kw):
            jobs.add_proposal(test.root, {"chat": sess.session_id, "ws": WS, **test.NEW})
            yield ("delta", "done")
            yield ("done", {"cost": 0.01})
        with mock.patch.object(server.chat_session.ChatSession, "ask", ask):
            out = self.run_job(mode="auto")
        self.assertEqual(approvals.pending(self.root), [])
        self.assertEqual(jobs.take_proposals(self.root, out["chat"]), [])

    def test_team_endpoints(self):
        mine = jobs.create(self.root, ws=WS, bot="cmo", skill="positioning", name="weekly",
                           message="m", schedule="every 6h", cap=1)
        other = jobs.create(self.root, ws="hq", bot="cos", skill="weekly-review", name="hq",
                            message="m", schedule="every 6h", cap=1)
        view = server._jobs_view(WS)
        self.assertEqual([j["id"] for j in view["jobs"]], [mine["id"]])
        self.assertEqual((view["jobs"][0]["mode"], view["jobs"][0]["room"]), ("approval", f"{WS}/dm-cmo"))
        self.assertEqual(len(server._jobs_view("hq")["jobs"]), 2)
        code, _ = self.post(f"/api/jobs/{other['id']}/mode", {"ws": WS, "mode": "auto"})
        self.assertEqual(code, 404)  # another workspace's job
        code, body = self.post(f"/api/jobs/{mine['id']}/mode", {"ws": WS, "mode": "report"})
        self.assertEqual((code, body["jobs"][0]["mode"]), (200, "report"))
        for bad in ({"ws": WS}, {"ws": WS, "mode": None}, {"ws": WS, "mode": "free"}):
            self.assertEqual(self.post(f"/api/jobs/{mine['id']}/mode", bad)[0], 400, bad)
        self.assertEqual(self.post(f"/api/jobs/{mine['id']}/enabled", {"ws": WS, "enabled": "no"})[0], 400)
        # The owner sets or clears the cap here: empty or 0 is no cap.
        code, body = self.post(f"/api/jobs/{mine['id']}/cap", {"ws": WS, "cap": "2.5"})
        self.assertEqual((code, body["jobs"][0]["cap"]), (200, 2.5))
        for none in ("", 0, None):
            self.assertEqual(self.post(f"/api/jobs/{mine['id']}/cap", {"ws": WS, "cap": none})[0], 200)
            self.assertIsNone(jobs.get(self.root, mine["id"])["cap"])
        self.assertEqual(self.post(f"/api/jobs/{mine['id']}/cap", {"ws": WS, "cap": "-1"})[0], 400)
        self.assertEqual(self.post(f"/api/jobs/{other['id']}/cap", {"ws": WS, "cap": "1"})[0], 404)
        jobs.update(self.root, mine["id"], cap=1)
        self.assertEqual(jobs.get(self.root, mine["id"])["mode"], "report")
        self.assertTrue(jobs.get(self.root, mine["id"])["enabled"])
        self.post(f"/api/jobs/{mine['id']}/enabled", {"ws": WS, "enabled": False})
        self.assertFalse(jobs.get(self.root, mine["id"])["enabled"])
        bots.set_active("cmo", False, self.root, WS, {"slug": WS, "kind": "venture"})
        code, body = self.post(f"/api/jobs/{mine['id']}/enabled", {"ws": WS, "enabled": True})
        self.assertEqual(code, 409)  # it could not run
        self.assertFalse(jobs.get(self.root, mine["id"])["enabled"])
        self.post(f"/api/jobs/{other['id']}/delete", {"ws": "hq"})
        self.assertIsNone(jobs.get(self.root, other["id"]))

    def test_invited_person_has_no_job_routes(self):
        h = server.Handler.__new__(server.Handler)
        sent = []
        h._send = lambda code, obj: sent.append(code)
        h._read_json = lambda: {"ws": WS, "mode": "auto"}
        h._person_post("/api/jobs/x/mode", {"id": "p1", "name": "P", "ws": [WS]})
        h.path = f"/api/jobs?ws={WS}"
        h._person_get("/api/jobs", {"id": "p1", "name": "P", "ws": [WS]})
        self.assertEqual(sent, [403, 403])


if __name__ == "__main__":
    unittest.main()
