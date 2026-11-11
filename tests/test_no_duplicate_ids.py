"""Guard: no two things ever share one composed id.

A composed id (pr1.pf1.sec00.po3, ...) is meant to be the one permanent
reference to a single thing. Posts are the one kind minted from a bare
counter with content baked straight into plan files (see mint_post_ids),
so they're the kind that can drift and collide without anyone noticing:
this is what actually happened on 2026-07-06 (two posts sharing po3, one
resolving via the SQLite index, the other via file lookup). Every other
kind is allocated from a natural key, so IdRegistry.build() raising
ValueError is itself the guard for those.

The owner's real data lives in the home folder, which tests never read, so
`leobot doctor` runs this check on it ("no duplicate ids"). Here the check
itself is tested on a throwaway workspace.
"""
import json
import tempfile
import unittest
from pathlib import Path

from core.ids import find_duplicate_ids, find_duplicate_post_ids


def _plan(root: Path, profile: str, name: str, ids) -> Path:
    content = root / "projects" / "demo" / "profiles" / profile / "content"
    content.mkdir(parents=True, exist_ok=True)
    f = content / f"plan-{name}.json"
    f.write_text(json.dumps({"posts": [{"id": i} for i in ids]}), encoding="utf-8")
    return f


class TestNoDuplicateIds(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def test_clean_workspace_has_no_duplicates(self):
        _plan(self.root, "profile-a", "2026-07", ["pr1.pf1.sec00.po1", "pr1.pf1.sec00.po2"])
        _plan(self.root, "profile-a", "2026-08", ["pr1.pf1.sec00.po3"])
        self.assertEqual(find_duplicate_post_ids(self.root), {})

    def test_post_id_in_two_plan_files_is_found(self):
        _plan(self.root, "profile-a", "2026-07", ["pr1.pf1.sec00.po1", "pr1.pf1.sec00.po3"])
        _plan(self.root, "profile-a", "2026-08", ["pr1.pf1.sec00.po3"])
        dupes = find_duplicate_post_ids(self.root)
        self.assertEqual(list(dupes), ["pr1.pf1.sec00.po3"])
        self.assertEqual(len(dupes["pr1.pf1.sec00.po3"]), 2)

    def test_unreadable_plan_is_skipped(self):
        _plan(self.root, "profile-a", "2026-07", ["pr1.pf1.sec00.po1"])
        bad = _plan(self.root, "profile-a", "2026-08", [])
        bad.write_text("{not json", encoding="utf-8")
        self.assertEqual(find_duplicate_post_ids(self.root), {})

    def test_every_kind_check_reports_post_duplicates(self):
        _plan(self.root, "profile-a", "2026-07", ["pr1.pf1.sec00.po3"])
        _plan(self.root, "profile-b", "2026-07", ["pr1.pf1.sec00.po3"])
        problems = find_duplicate_ids(self.root, [], [])
        self.assertEqual(len(problems), 1)
        self.assertIn("duplicate post id 'pr1.pf1.sec00.po3'", problems[0])


if __name__ == "__main__":
    unittest.main()
