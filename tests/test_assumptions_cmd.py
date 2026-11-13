"""list-assumptions and extract-assumptions: propose writes nothing; save replaces in place."""
import contextlib
import io
import json
import os
import unittest
from unittest import mock

from tests import test_fileops_crud as _crud
import dashboard.fileops as fileops
import dashboard.osctl as osctl

PROSE = "People pay for it. They find us through search."


class Cmd(unittest.TestCase):
    def setUp(self):
        _crud.CrudTest.setUp(self)
        fileops.create_memo("acme", "assessment", {"riskiest_assumption": PROSE})
        self.path = self.root / "projects/acme/strategy/memos/assessment-v1.json"

    def tearDown(self):
        _crud.CrudTest.tearDown(self)

    def _body(self, ver=1):
        return json.loads((self.root / f"projects/acme/strategy/memos/assessment-v{ver}.json").read_text())

    def test_proposing_writes_nothing(self):
        before = self.path.read_bytes()
        out = fileops.extract_assumptions("acme", "assessment", 1, None)
        self.assertEqual(out["proposed"], ["People pay for it.", "They find us through search."])
        self.assertEqual(self.path.read_bytes(), before)

    def test_saving_writes_assumptions_and_nothing_else_changes(self):
        before = self._body()
        fileops.extract_assumptions("acme", "assessment", 1, ["People pay for it.", "Search works"])
        after = self._body()
        saved = after.pop("assumptions")
        self.assertEqual([(a["id"], a["status"]) for a in saved], [("as1", "unknown"), ("as2", "unknown")])
        self.assertEqual(after, before)

    def test_saving_twice_with_same_items_does_not_duplicate(self):
        for _ in range(2):
            fileops.extract_assumptions("acme", "assessment", 1, ["A", "B"])
        self.assertEqual([a["id"] for a in self._body()["assumptions"]], ["as1", "as2"])

    def test_unknown_version_and_bad_items_raise(self):
        with self.assertRaises(fileops.ActionError):
            fileops.extract_assumptions("acme", "assessment", 9, ["A"])
        with self.assertRaises(fileops.ActionError):
            fileops.extract_assumptions("acme", "assessment", 1, "A")
        with self.assertRaises(fileops.ActionError):
            fileops.extract_assumptions("acme", "nope", 1, None)

    def test_new_memo_version_carries_ids_and_status(self):
        fileops.extract_assumptions("acme", "assessment", 1, ["A", "B"])
        body = self._body()
        body["assumptions"][0]["status"] = "contradicted"  # as set by the (later) approval
        self.path.write_text(json.dumps(body))
        fileops.create_memo("acme", "assessment", {"recommendation": "go"})
        v2 = self._body(2)["assumptions"]
        self.assertEqual([(a["id"], a["status"]) for a in v2], [("as1", "contradicted"), ("as2", "unknown")])
        # re-extracting with one text changed: kept id for the same text, fresh id for the new one
        fileops.extract_assumptions("acme", "assessment", 2, ["A", "C"])
        v2 = self._body(2)["assumptions"]
        self.assertEqual([(a["id"], a["status"]) for a in v2], [("as1", "contradicted"), ("as3", "unknown")])

    def test_list_adds_memo_id_and_version(self):
        fileops.extract_assumptions("acme", "assessment", 1, ["A"])
        rows = fileops.list_assumptions("acme")
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["version"], rows[0]["asm"]), (1, "as1"))
        self.assertRegex(rows[0]["id"], r"^pr\d+\.sec\d+\.mm\d+\.as1$")
        self.assertEqual(fileops.list_assumptions("acme", "pricing"), [])

    def test_evidence_ref_accepts_an_assumption(self):
        fileops.extract_assumptions("acme", "assessment", 1, ["A"])
        cid = fileops.list_assumptions("acme")[0]["id"]
        out = fileops.add_evidence("acme", {"text": "saw otherwise", "ref": cid})
        self.assertEqual(out["n"], 1)
        with self.assertRaises(fileops.ActionError):
            fileops.add_evidence("acme", {"text": "x", "ref": cid.rsplit(".", 1)[0]})  # the memo itself


class Walls(unittest.TestCase):
    def setUp(self):
        from tests.test_metric import OsctlWalls as M
        M.setUp(self)

    def _run(self, argv, env, name):
        out = io.StringIO()
        with mock.patch.dict(os.environ, env), mock.patch.object(fileops, name, return_value=[] if name == "list_assumptions" else {}) as fn, \
                contextlib.redirect_stdout(out):
            osctl.main(list(argv))
        return json.loads(out.getvalue().strip().splitlines()[-1]), fn

    LIST = ["list-assumptions", "--project", "acme"]
    EXTRACT = ["extract-assumptions", "--project", "acme", "--type", "assessment", "--version", "1",
               "--items-json", '["A"]']

    def test_list_is_read_only_safe_for_every_context(self):
        for env in ({"LEOBOT_WORKSPACE": "acme", "LEOBOT_READONLY": "1"},
                    {"LEOBOT_WORKSPACE": "acme", "LEOBOT_SHARED": "1"},
                    {"LEOBOT_WORKSPACE": "acme", "LEOBOT_JOB": "j1", "LEOBOT_JOB_MODE": "auto"}):
            out, fn = self._run(self.LIST, env, "list_assumptions")
            self.assertTrue(out["ok"], (env, out))

    def test_extract_replaces_memo_content_so_it_is_owner_only(self):
        out, fn = self._run(self.EXTRACT, {"LEOBOT_WORKSPACE": "acme"}, "extract_assumptions")
        self.assertTrue(out["ok"], out)
        fn.assert_called_once_with("acme", "assessment", 1, ["A"])
        for env in ({"LEOBOT_WORKSPACE": "acme", "LEOBOT_SHARED": "1"},
                    {"LEOBOT_WORKSPACE": "acme", "LEOBOT_READONLY": "1"},
                    {"LEOBOT_WORKSPACE": "acme", "LEOBOT_JOB": "j1", "LEOBOT_JOB_MODE": "auto"}):
            out, fn = self._run(self.EXTRACT, env, "extract_assumptions")
            self.assertFalse(out["ok"], env)
            fn.assert_not_called()


if __name__ == "__main__":
    unittest.main()
