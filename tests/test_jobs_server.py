"""A scheduled job's turn through the server's ask path (server.run_job), on a fake engine."""
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from core import approvals, bots, folders
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
                              "edit_dirs": tuple(edit_dirs), "kw": kw})
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


if __name__ == "__main__":
    unittest.main()
