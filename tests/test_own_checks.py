"""own_checks.py: a loop wakes on a change in Leo Bot's own data (cards, a list, memos, experiments)."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from core import approvals, bots, jobs, own_checks


class Digest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / "projects" / "acme" / "lists").mkdir(parents=True)
        (self.root / "projects" / "demo" / "lists").mkdir(parents=True)
        (self.root / "team").mkdir()

    def write_list(self, ws, lid, rows, version=1):
        (self.root / "projects" / ws / "lists" / f"{lid}.json").write_text(
            json.dumps({"id": lid, "version": version, "rows": rows}))

    def test_digest_changes_when_card_added(self):
        before = own_checks.digest(self.root, "acme", "approvals")
        approvals.add(self.root, ws="demo", room="demo/general", chat="c0", bot="cmo", summary="other workspace")
        self.assertEqual(own_checks.digest(self.root, "acme", "approvals"), before)
        approvals.add(self.root, ws="acme", room="acme/general", chat="c1", bot="cmo", summary="save the memo")
        self.assertNotEqual(own_checks.digest(self.root, "acme", "approvals"), before)

    def test_list_digest_changes_on_row_edit(self):
        self.write_list("acme", "ls1", [{"id": "rw1", "cells": {"a": "x"}}])
        before = own_checks.digest(self.root, "acme", "list", "ls1")
        self.assertEqual(own_checks.digest(self.root, "acme", "list", "ls1"), before)
        self.write_list("acme", "ls1", [{"id": "rw1", "cells": {"a": "y"}}], version=2)
        self.assertNotEqual(own_checks.digest(self.root, "acme", "list", "ls1"), before)

    def test_list_in_other_workspace_refused(self):
        self.write_list("demo", "ls1", [])
        with self.assertRaises(ValueError):
            own_checks.digest(self.root, "acme", "list", "ls1")
        for bad in (None, "", "cv1", "../ls1"):
            with self.subTest(bad), self.assertRaises(ValueError):
                own_checks.digest(self.root, "demo", "list", bad)

    def test_unknown_source_refused(self):
        for source in ("files", "", None):
            with self.subTest(source), self.assertRaises(ValueError):
                own_checks.digest(self.root, "acme", source)
        with self.assertRaises(ValueError):  # HQ has no project data
            own_checks.digest(self.root, bots.HQ, "memos")

    def test_memos_and_experiments_follow_the_project(self):
        rows = {"memos": [{"id": 1, "version": 1}], "experiments": []}
        with mock.patch("dashboard.db.project", side_effect=lambda slug: rows) as proj:
            before = own_checks.digest(self.root, "acme", "memos")
            proj.assert_called_with("acme")
            rows["memos"].append({"id": 2, "version": 1})
            self.assertNotEqual(own_checks.digest(self.root, "acme", "memos"), before)
            self.assertEqual(own_checks.digest(self.root, "acme", "experiments"),
                             own_checks.digest(self.root, "acme", "experiments"))
        with mock.patch("dashboard.db.project", return_value=None), self.assertRaises(ValueError):
            own_checks.digest(self.root, "acme", "memos")


class CleanCheck(unittest.TestCase):
    def test_clean_accepts_own_check(self):
        self.assertEqual(jobs.clean("check", {"own": "approvals"}), {"own": "approvals", "id": None})
        self.assertEqual(jobs.clean("check", '{"own": "list", "id": "ls3"}'), {"own": "list", "id": "ls3"})

    def test_clean_rejects_mixed_check(self):
        for bad in ({"own": "list"}, {"own": "files"}, {"own": "approvals", "app": "gmail"},
                    {"own": "list", "id": "cv1"}, {"own": "memos", "id": "ls1"}, {"own": "approvals", "args": {}}):
            with self.subTest(bad), self.assertRaises(ValueError):
                jobs.clean("check", bad)


if __name__ == "__main__":
    unittest.main()
