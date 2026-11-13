"""Stale memos: derived on read from contradicted assumptions, never stored."""
import json
import unittest
from unittest import mock

from core.ids import build_id_registry
from tests import test_fileops_crud as _crud
import dashboard.db as db
import dashboard.fileops as fileops
import dashboard.server as server


class Stale(unittest.TestCase):
    def setUp(self):
        _crud.CrudTest.setUp(self)
        fileops.create_memo("acme", "assessment", {"riskiest_assumption": "People pay."})
        fileops.extract_assumptions("acme", "assessment", 1, ["People pay", "Search works"])

    def tearDown(self):
        _crud.CrudTest.tearDown(self)

    def _set(self, ver, asm, status):
        path = self.root / f"projects/acme/strategy/memos/assessment-v{ver}.json"
        body = json.loads(path.read_text())
        for a in body["assumptions"]:
            if a["id"] == asm:
                a["status"] = status
        path.write_text(json.dumps(body))
        fileops.reindex()

    def _rows(self):
        data = db.project("acme")
        for m in data["memos"]:
            m["body"] = fileops.read_authored_json(m["file_path"])
        reg = build_id_registry(db.tree(), db.posts(), root=self.root)
        return fileops.enrich_project_memos(data["memos"], "acme", reg), reg

    def test_memo_with_a_contradicted_assumption_is_stale(self):
        self._set(1, "as1", "contradicted")
        rows, _ = self._rows()
        self.assertTrue(rows[0]["stale"])
        self.assertEqual(rows[0]["stale_why"][0]["text"], "People pay")
        self.assertRegex(rows[0]["stale_why"][0]["assumption"], r"\.mm\d+\.as1$")

    def test_memo_without_one_is_not_stale(self):
        rows, _ = self._rows()
        self.assertFalse(rows[0]["stale"])
        self.assertEqual(rows[0]["stale_why"], [])

    def test_why_names_the_newest_evidence_entry_for_that_assumption(self):
        aid = fileops.list_assumptions("acme")[0]["id"]
        fileops.add_evidence("acme", {"text": "first", "ref": aid})
        out = fileops.add_evidence("acme", {"text": "second", "ref": aid})
        self._set(1, "as1", "contradicted")
        rows, _ = self._rows()
        self.assertEqual(rows[0]["stale_why"][0]["evidence"], out["id"])

    def test_stale_is_per_version_and_a_newer_holding_version_is_not_stale(self):
        self._set(1, "as1", "contradicted")
        fileops.create_memo("acme", "assessment", {})
        self._set(2, "as1", "holding")  # owner's replacement version: assumption no longer contradicted
        rows, _ = self._rows()
        by_ver = {r["version"]: r["stale"] for r in rows}
        self.assertEqual(by_ver, {1: True, 2: False})

    def test_snapshot_line_only_when_stale_and_names_the_assumption(self):
        def snap():
            with mock.patch.object(server, "ROOT", self.root), mock.patch.object(server.db, "DB_PATH", db.DB_PATH), \
                    mock.patch.object(server.fileops, "ROOT", self.root):
                return server.state_snapshot(db.tree())
        self.assertNotIn("stale", snap())
        self._set(1, "as1", "contradicted")
        text = snap()
        line = next(ln for ln in text.splitlines() if ln.strip().startswith("stale memo"))
        self.assertIn("People pay", line)
        self.assertRegex(line, r"mm\d+")
        self.assertIn(", stale]", text)

    def test_nothing_is_stored(self):
        self._set(1, "as1", "contradicted")
        self._rows()
        body = json.loads((self.root / "projects/acme/strategy/memos/assessment-v1.json").read_text())
        self.assertNotIn("stale", body)


if __name__ == "__main__":
    unittest.main()
