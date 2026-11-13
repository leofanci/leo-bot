"""Memo assumptions: shape, ids stable across versions, composed ids resolve."""
import json
import unittest

from core import assumptions as A
from core.ids import build_id_registry, lk_assumption
from core.project_schemas import normalize_memo_body
from tests import test_fileops_crud as _crud
import dashboard.fileops as fileops
import dashboard.db as db


class Shape(unittest.TestCase):
    def test_normalize_table(self):
        raw = [{"id": "as1", "text": "  people  pay ", "status": "Holding", "refs": ["PR1.sec02.ev1"]},
               {"id": "as2", "text": "x", "status": "weird"},
               {"id": "as2", "text": "dup id"},
               {"id": "bad", "text": "bad id"},
               {"id": "as3", "text": ""},
               "junk"]
        out = A.normalize(raw)
        self.assertEqual([i["id"] for i in out], ["as1", "as2"])
        self.assertEqual(out[0], {"id": "as1", "text": "people pay", "status": "holding",
                                  "refs": ["pr1.sec02.ev1"]})
        self.assertEqual(out[1]["status"], "unknown")
        self.assertEqual(A.normalize("nope"), [])

    def test_next_id_unique_across_versions(self):
        self.assertEqual(A.next_id([[{"id": "as1"}], [{"id": "as4"}, {"id": "as2"}]]), "as5")
        self.assertEqual(A.next_id([]), "as1")

    def test_carry_keeps_ids_and_mints_for_changed_text(self):
        old = [{"id": "as1", "text": "A", "status": "contradicted", "refs": ["ev"]},
               {"id": "as2", "text": "B", "status": "holding", "refs": []}]
        out = A.carry(old, ["a ", "C", "D"], used=[[{"id": "as7"}]])
        self.assertEqual([(i["id"], i["status"]) for i in out],
                         [("as1", "contradicted"), ("as8", "unknown"), ("as9", "unknown")])
        self.assertEqual(out[0]["refs"], ["ev"])

    def test_stale_is_the_contradicted_ones(self):
        items = [{"id": "as1", "status": "holding"}, {"id": "as2", "status": "contradicted"}]
        self.assertEqual([i["id"] for i in A.stale(items)], ["as2"])

    def test_memo_body_without_assumptions_is_unchanged(self):
        self.assertNotIn("assumptions", normalize_memo_body("assessment", {"recommendation": "go"}))
        body = normalize_memo_body("assessment", {"assumptions": [{"id": "as1", "text": "t"}]})
        self.assertEqual(body["assumptions"][0]["status"], "unknown")

    def test_proposed_splits_the_prose_field(self):
        body = {"riskiest_assumption": "People pay. They find us by search;\nThe market is big."}
        self.assertEqual(A.proposed(body, "assessment"),
                         ["People pay.", "They find us by search;", "The market is big."])
        self.assertEqual(A.proposed({"som": {"what_changes_the_number": "price"}}, "market-sizing"), ["price"])
        self.assertEqual(A.proposed(body, "pricing"), [])


class Ids(unittest.TestCase):
    def setUp(self):
        _crud.CrudTest.setUp(self)

    def tearDown(self):
        _crud.CrudTest.tearDown(self)

    def _write(self, ver, items):
        d = self.root / "projects/acme/strategy/memos"
        d.mkdir(parents=True, exist_ok=True)
        (d / f"assessment-v{ver}.json").write_text(json.dumps(normalize_memo_body(
            "assessment", {"assumptions": items}, version=ver)))
        fileops.reindex()

    def _reg(self):
        return build_id_registry(db.tree(), db.posts(), root=self.root)

    def test_composed_id_resolves_and_lookup_names_the_newest_version(self):
        self._write(1, [{"id": "as1", "text": "A"}])
        self._write(2, [{"id": "as1", "text": "A", "status": "contradicted"}, {"id": "as2", "text": "B"}])
        reg = self._reg()
        memo2 = reg.get("memo:proj:acme:assessment-v2")
        cid = f"{memo2}.as1"
        ent = reg.resolve(cid)
        self.assertEqual(ent["kind"], "assumption")
        self.assertEqual(ent["ref"]["version"], 2)
        self.assertEqual(reg.get(lk_assumption("acme", "assessment", "as1")), cid)
        self.assertIsNotNone(reg.resolve(f"{reg.get('memo:proj:acme:assessment-v1')}.as1"))


if __name__ == "__main__":
    unittest.main()
