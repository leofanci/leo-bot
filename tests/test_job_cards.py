"""Job proposals (dashboard/job_cards.py): checked from the thread's workspace, applied once."""
import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import index
from core import approvals, bots, jobs, memory
from dashboard import db, fileops, job_cards
from dashboard.engines import registry


class _Engine:
    def __init__(self, metered=True):
        self.id, self.label, self.kind = "api:x", "X API", "api"
        self._metered = metered


NEW = {"bot": "cmo", "skill": "positioning", "name": "weekly", "message": "review the week",
       "schedule": "mon 09:00", "cap": "0.5"}


class JobCardsBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for slug in ("acme", "demo"):
            (self.root / "projects" / slug).mkdir(parents=True)
            (self.root / "projects" / slug / "project.md").write_text(f"---\nname: {slug}\nkind: venture\n---\n")
        self.engine = _Engine()
        for p in [mock.patch.object(t, "ROOT", self.root) for t in (bots, memory, fileops)] + [
                mock.patch.object(db, "DB_PATH", self.root / "database" / "data" / "os.db"),
                mock.patch.object(registry, "pick", side_effect=lambda *a, **k: self.engine),
                mock.patch.object(registry, "metered", side_effect=lambda e: e._metered),
                mock.patch.dict(os.environ, {})]:
            p.start()
            self.addCleanup(p.stop)
        with contextlib.redirect_stdout(io.StringIO()):
            index.build(self.root)
        self.projects = db.tree()

    def propose(self, thread_ws="acme", op="create", job_id=None, **fields):
        return job_cards.proposal(self.root, thread_ws=thread_ws, op=op, job_id=job_id,
                                  fields=fields if fields or op != "create" else dict(NEW), projects=self.projects)

    def card(self, p, ws="acme"):
        return approvals.add_job(self.root, ws=ws, room=f"{ws}/dm-cmo", chat="c1", bot="cmo",
                                 job={k: p[k] for k in ("op", "job_id", "fields")},
                                 summary=p["summary"], preview=p["preview"])


class Proposal(JobCardsBase):
    def test_create_card_shows_what_approve_saves(self):
        p = self.propose()
        self.assertEqual(p["summary"], "New job: weekly")
        rows = dict(p["preview"]["fields"])
        self.assertEqual(rows["Bot"], "@cmo /positioning")
        self.assertEqual(rows["Workspace"], "acme")
        self.assertEqual(rows["Mode"], job_cards.MODE_TEXT["approval"])
        self.assertEqual(rows["Cap"], "$0.5 per 24 hours")
        self.assertIn("Next run", rows)
        self.assertEqual(p["fields"]["cap"], 0.5)
        self.assertEqual(jobs.load(self.root), [])  # a proposal saves nothing

    def test_refusals(self):
        bad = [dict(NEW, workspace="demo"),            # another workspace from a workspace thread
               dict(NEW, bot="nobody"), dict(NEW, bot="social"),  # unknown, switched off
               dict(NEW, skill="market-sizing"),     # not this bot's skill
               dict(NEW, mode="free"), dict(NEW, cap="-1"), {k: v for k, v in NEW.items() if k != "message"}]
        for fields in bad:
            with self.assertRaises(ValueError, msg=fields):
                self.propose(**fields)
        with self.assertRaises(ValueError):
            self.propose(op="drop")
        self.engine = _Engine(metered=False)
        with self.assertRaises(ValueError):
            self.propose()

    def test_cap_is_optional_on_a_card(self):
        for cap in (None, "", "0"):
            fields = {k: v for k, v in NEW.items() if k != "cap"} if cap is None else dict(NEW, cap=cap)
            p = self.propose(**fields)
            self.assertIsNone(p["fields"]["cap"])
            self.assertIn(["Cap", "no cap"], p["preview"]["fields"])
        job = jobs.create(self.root, ws="acme", bot="cmo", skill="positioning", name="other",
                          message="m", schedule="every 6h", cap=1)
        p = self.propose(op="update", job_id=job["id"], cap="0")
        self.assertEqual(p["fields"], {"cap": None})
        self.assertIn(["Cap", "$1 per 24 hours → no cap"], p["preview"]["fields"])

    def test_hq_thread_may_target_a_workspace(self):
        self.assertEqual(self.propose(thread_ws="hq", **dict(NEW, workspace="acme"))["fields"]["workspace"], "acme")

    def test_update_and_delete_in_scope_only(self):
        job = jobs.create(self.root, ws="demo", bot="cmo", skill="positioning", name="other",
                          message="m", schedule="every 6h", cap=1)
        with self.assertRaises(ValueError):
            self.propose(op="update", job_id=job["id"], cap="2")  # an acme thread, a demo job
        with self.assertRaises(ValueError):
            self.propose(op="delete", job_id=job["id"])
        with self.assertRaises(ValueError):
            self.propose(thread_ws="demo", op="update", job_id=job["id"])  # nothing to change
        with self.assertRaises(ValueError):
            self.propose(thread_ws="demo", op="update", job_id=job["id"], enabled="false")
        p = self.propose(thread_ws="demo", op="update", job_id=job["id"], cap="2", mode="auto")
        self.assertEqual(p["summary"], "Change job: other")
        self.assertEqual(dict(p["preview"]["fields"])["Mode"],
                         f"{job_cards.MODE_TEXT['approval']} → {job_cards.MODE_TEXT['auto']}")
        self.assertEqual(self.propose(thread_ws="demo", op="delete", job_id=job["id"])["summary"], "Delete job: other")
        with self.assertRaises(ValueError):
            self.propose(op="update", job_id="missing1", cap="2")

    def test_one_name_per_workspace(self):
        jobs.create(self.root, ws="acme", bot="cmo", skill="positioning", name="Weekly",
                    message="m", schedule="every 6h", cap=1)
        with self.assertRaises(ValueError):
            self.propose()  # "weekly" exists in acme, case aside
        other = jobs.create(self.root, ws="acme", bot="cmo", skill="positioning", name="other",
                            message="m", schedule="every 6h", cap=1)
        with self.assertRaises(ValueError):
            self.propose(op="update", job_id=other["id"], name="WEEKLY")
        self.assertEqual(self.propose(op="update", job_id=other["id"], name="other")["summary"], "Change job: other")


class Apply(JobCardsBase):
    def test_create_then_update_then_delete(self):
        line, undo_create, jid = job_cards.apply(self.root, self.card(self.propose(**dict(NEW, mode="report"))),
                                                 self.projects)
        self.assertTrue(line.startswith("Job created: weekly"), line)
        job = jobs.load(self.root)[0]
        self.assertEqual(jid, job["id"])
        self.assertEqual((job["workspace"], job["mode"], job["cap"]), ("acme", "report", 0.5))
        upd = self.card(self.propose(op="update", job_id=job["id"], schedule="daily 08:00"))
        line, undo_upd, _ = job_cards.apply(self.root, upd, self.projects)
        self.assertEqual(line, "Job changed: weekly.")
        self.assertEqual(jobs.get(self.root, job["id"])["schedule"], "daily 08:00")
        job_cards.undo(self.root, undo_upd)  # back as it was
        self.assertEqual(jobs.get(self.root, job["id"])["schedule"], job["schedule"])
        gone = self.card(self.propose(op="delete", job_id=job["id"]))
        line, undo_del, _ = job_cards.apply(self.root, gone, self.projects)
        self.assertEqual(line, "Job deleted: weekly.")
        self.assertEqual(jobs.load(self.root), [])
        job_cards.undo(self.root, undo_del)
        self.assertEqual(jobs.load(self.root), [job])
        job_cards.undo(self.root, undo_create)
        self.assertEqual(jobs.load(self.root), [])
        with self.assertRaises(ValueError):  # already gone
            job_cards.undo(self.root, undo_create)
        upd = self.card(self.propose(**NEW))
        upd = self.card(self.propose(op="update", job_id=job_cards.apply(self.root, upd, self.projects)[2],
                                     schedule="daily 08:00"))
        jobs.delete(self.root, upd["job"]["job_id"])
        with self.assertRaises(ValueError):  # the job went between proposal and Approve
            job_cards.apply(self.root, upd, self.projects)

    def test_rechecked_at_approve(self):
        rec = self.card(self.propose())
        bots.set_active("cmo", False, self.root, "acme", next(p for p in self.projects if p["slug"] == "acme"))
        with self.assertRaises(ValueError):
            job_cards.apply(self.root, rec, self.projects)
        self.assertEqual(jobs.load(self.root), [])


if __name__ == "__main__":
    unittest.main()
