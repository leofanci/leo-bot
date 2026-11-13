"""Verdict cards: Approve writes the result and one evidence entry, once; Reject writes nothing."""
import json
import unittest
from unittest import mock

from core import approvals
from core.ids import build_id_registry
from tests.test_score_run import Base
import dashboard.db as db
import dashboard.fileops as fileops
import dashboard.score_run as score_run
import dashboard.server as server


class Cards(Base):
    def setUp(self):
        super().setUp()
        for p in (mock.patch.object(server, "ROOT", self.root), mock.patch.object(server.db, "DB_PATH", db.DB_PATH),
                  mock.patch.object(server.fileops, "ROOT", self.root),
                  mock.patch.object(server.chat_store, "append_note")):
            p.start()
            self.addCleanup(p.stop)

    def card(self, n=150, verdict="met"):
        self.run_job(lambda a, b, c: {"data": {"n": n}}, judge=lambda p: (f"{verdict}\nwhy", 0.0))
        return [r for r in self.cards() if r["status"] == "pending"][0]

    def exp(self):
        return json.loads((self.root / "projects/acme/strategy/experiments/will-pay.json").read_text())

    def evidence(self):
        text = (self.root / "projects/acme/strategy/intake.md")
        return text.read_text() if text.exists() else ""

    def test_approve_writes_result_and_exactly_one_evidence_entry(self):
        card = self.card()
        out = server._approve_verdict_card(card["id"])
        self.assertEqual(out["status"], "done")
        body = self.exp()
        self.assertEqual(body["status"], "done")
        self.assertIn("met: 150 over 7d", body["result"])
        self.assertEqual(self.evidence().count("- ev1 "), 1)
        self.assertIn(card["verdict"]["exp"], self.evidence())

    def test_status_by_verdict(self):
        for verdict, want in (("on track", "running"), ("inconclusive", "planned"), ("killed", "done")):
            fileops.update_experiment("acme", "will-pay", {"status": "planned"})
            for r in approvals.load(self.root):
                if r["status"] == "pending":
                    approvals.reject(r["id"], self.root)
            server._approve_verdict_card(self.card(n=hash(verdict) % 1000, verdict=verdict)["id"])
            self.assertEqual(self.exp()["status"], want, verdict)

    def test_second_approve_refuses_and_writes_nothing_more(self):
        card = self.card()
        server._approve_verdict_card(card["id"])
        before = self.evidence()
        with self.assertRaises(server._AppRefused) as cm:
            server._approve_verdict_card(card["id"])
        self.assertIn("already done", str(cm.exception))
        self.assertEqual(self.evidence(), before)

    def test_reject_writes_nothing_and_marks_rejected(self):
        card = self.card()
        before = self.snapshot()
        out = approvals.reject(card["id"], self.root)
        self.assertEqual(out["status"], "rejected")
        self.assertEqual(self.snapshot(), before)
        with self.assertRaises(server._AppRefused):
            server._approve_verdict_card(card["id"])

    def test_approve_after_the_metric_changed_fails_and_writes_nothing(self):
        card = self.card()
        fileops.set_metric("acme", "will-pay", {**{"app": "instagram", "action": "INSTAGRAM_GET_X",
                                                     "window_days": 7}, "path": "data.other"},
                           catalog=["instagram"], read_only=lambda s: True)
        before = self.snapshot()
        out = server._approve_verdict_card(card["id"])
        self.assertEqual(out["status"], "failed")
        self.assertIn("Not saved", out["result"])
        self.assertEqual(self.snapshot(), before)

    def test_approve_after_the_experiment_is_deleted_fails(self):
        card = self.card()
        fileops.delete_experiment("acme", "will-pay")
        out = server._approve_verdict_card(card["id"])
        self.assertEqual(out["status"], "failed")

    def test_unknown_or_other_kind_card_is_not_a_verdict(self):
        with self.assertRaises(LookupError):
            server._approve_verdict_card("nope")

    def test_verdicts_are_not_reachable_from_osctl(self):
        import dashboard.osctl as o
        src = open(o.__file__).read()
        self.assertNotIn("add_verdict", src)
        self.assertNotIn("verdict", " ".join(o._LOCAL_CMDS))

    def test_experiment_row_carries_metric_and_newest_result(self):
        server._approve_verdict_card(self.card()["id"])
        data = db.project("acme")
        for x in data["experiments"]:
            x["body"] = fileops.read_authored_json(x["file_path"])
        reg = build_id_registry(db.tree(), db.posts(), root=self.root)
        rows = fileops.enrich_project_experiments(data["experiments"], "acme", reg)
        by = {r["stem"]: r for r in rows}
        self.assertEqual(by["will-pay"]["metric"]["path"], "data.n")
        self.assertIn("met: 150", by["will-pay"]["metric"]["last"]["text"])
        self.assertRegex(by["will-pay"]["metric"]["last"]["evidence"], r"\.ev1$")
        self.assertNotIn("metric", by["no-metric"])


class Dispatch(Base):
    def setUp(self):
        super().setUp()
        p = mock.patch.object(server.db, "DB_PATH", db.DB_PATH)
        p.start()
        self.addCleanup(p.stop)

    def test_job_with_this_skill_runs_the_score_run_not_a_bot_turn(self):
        job = {**JOB_LIKE, "skill": "score-experiments"}
        with mock.patch.object(server, "_job_problem", return_value=None), \
                mock.patch.object(server.score_run, "run", return_value={"status": "ok", "cards": 0, "chat": None,
                                                                         "cost": 0.0, "detail": ""}) as run, \
                mock.patch.object(server.Handler, "_handle_ask") as ask:
            out = server.run_job(job, None)
        run.assert_called_once()
        ask.assert_not_called()
        self.assertEqual(out["status"], "done")

    def test_hq_job_is_refused(self):
        out = server._score_job({**JOB_LIKE, "workspace": server.bots.HQ})
        self.assertEqual(out["status"], "refused")


JOB_LIKE = {"id": "j1", "workspace": "acme", "bot": "product", "mode": "approval", "cap": 5.0}

if __name__ == "__main__":
    unittest.main()
