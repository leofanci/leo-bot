"""Evidence log: dated, numbered, append-only entries in the intake, with ids."""
import json
import unittest

from core import evidence
from core.ids import lk_evidence
from tests import test_fileops_crud as _crud
import dashboard.fileops as fileops

INTAKE = "# Venture intake\n\n## What it is\n\nA thing.\n\n## Evidence log\n"


class Log(unittest.TestCase):
    def test_append_adds_a_dated_numbered_bullet_under_evidence_log(self):
        text, n = evidence.append(INTAKE, "five signups", "pr1.sec03.ex1", "2026-11-13")
        self.assertEqual(n, 1)
        self.assertIn("- ev1 2026-11-13 [pr1.sec03.ex1] five signups", text)
        self.assertTrue(text.startswith(INTAKE.rstrip("\n")))
        self.assertEqual(evidence.entries(text)[0]["ref"], "pr1.sec03.ex1")

    def test_numbers_continue_and_entries_are_never_edited(self):
        one, _ = evidence.append(INTAKE, "first", None, "2026-11-13")
        two, n = evidence.append(one, "second", None, "2026-11-14")
        self.assertEqual(n, 2)
        self.assertIn("- ev1 2026-11-13 first\n", two)
        self.assertEqual([e["n"] for e in evidence.entries(two)], [1, 2])

    def test_other_sections_stay_byte_identical(self):
        base = "# Venture intake\n\n## Evidence log\n\n## Goals\n\nkeep this\n"
        text, _ = evidence.append(base, "x", None, "2026-11-13")
        self.assertTrue(text.endswith("## Goals\n\nkeep this\n"))

    def test_missing_evidence_section_is_created_last(self):
        text, n = evidence.append("# Venture intake\n\n## Goals\n\nx\n", "y", None, "2026-11-13")
        self.assertEqual(n, 1)
        self.assertLess(text.index("## Goals"), text.index("## Evidence log"))

    def test_text_is_single_line_and_capped(self):
        text, _ = evidence.append(INTAKE, "a\nb\n" + "z" * 600, None, "2026-11-13")
        entry = evidence.entries(text)[0]["text"]
        self.assertTrue(entry.startswith("a b zzz"))
        self.assertEqual(len(entry), 500)
        with self.assertRaises(ValueError):
            evidence.append(INTAKE, "  \n ", None, "2026-11-13")


class AddEvidence(unittest.TestCase):
    def setUp(self):
        _crud.CrudTest.setUp(self)
        out = fileops.create_experiment("acme", {"assumption": "people will pay", "stem": "will-pay"})
        self.exp = out["id"]

    def tearDown(self):
        _crud.CrudTest.tearDown(self)

    def _intake(self):
        return (self.root / "projects/acme/strategy/intake.md").read_text()

    def test_saves_entry_with_composed_id_and_resolves_it(self):
        out = fileops.add_evidence("acme", {"text": "five signups", "ref": self.exp, "date": "2026-11-13"})
        self.assertEqual(out["n"], 1)
        self.assertRegex(out["id"], r"^pr\d+\.sec\d+\.ev1$")
        self.assertIn(f"- ev1 2026-11-13 [{self.exp}] five signups", self._intake())
        from core.ids import build_id_registry
        import dashboard.db as db
        reg = build_id_registry(db.tree(), db.posts(), root=self.root)
        self.assertEqual(reg.resolve(out["id"])["kind"], "evidence")
        self.assertEqual(reg.get(lk_evidence("acme", 1)), out["id"])

    def test_second_entry_continues_numbering(self):
        fileops.add_evidence("acme", {"text": "one"})
        out = fileops.add_evidence("acme", {"text": "two"})
        self.assertEqual(out["n"], 2)
        self.assertIn("- ev1 ", self._intake())

    def test_ref_must_resolve_to_experiment_in_the_same_workspace(self):
        import dashboard.db as db
        from core.ids import build_id_registry
        for bad in ("pr9.sec03.ex9", "nonsense"):
            with self.assertRaises(fileops.ActionError):
                fileops.add_evidence("acme", {"text": "x", "ref": bad})
        reg = build_id_registry(db.tree(), db.posts(), root=self.root)
        proj = next(e["id"] for e in reg.entries if e["kind"] == "project")
        with self.assertRaises(fileops.ActionError):  # exists, but not an experiment
            fileops.add_evidence("acme", {"text": "x", "ref": proj})
        # an experiment of another workspace
        import index
        from tests.test_index_projects import write
        write(self.root / "projects/other/project.md", "---\nname: Other\nkind: venture\n---\n")
        other = fileops.create_experiment("other", {"assumption": "b", "stem": "b"})["id"]
        with self.assertRaises(fileops.ActionError):
            fileops.add_evidence("acme", {"text": "x", "ref": other})
        self.assertNotIn("ev1", self._intake() if (self.root / "projects/acme/strategy/intake.md").exists() else "")

    def test_text_required_and_date_checked(self):
        with self.assertRaises(fileops.ActionError):
            fileops.add_evidence("acme", {"text": "  "})
        with self.assertRaises(fileops.ActionError):
            fileops.add_evidence("acme", {"text": "x", "date": "yesterday"})


class OsctlWalls(unittest.TestCase):
    """add-evidence only appends: allowed for the owner, a shared thread and an Auto job; refused read-only."""
    ARGV = ["add-evidence", "--project", "acme", "--text", "five signups"]

    def setUp(self):
        from tests.test_metric import OsctlWalls as M
        M.setUp(self)

    run_cmd = None

    def _run(self, env):
        import contextlib, io, os
        from unittest import mock
        import dashboard.osctl as osctl
        out = io.StringIO()
        with mock.patch.dict(os.environ, env), mock.patch.object(fileops, "add_evidence") as saved, \
                contextlib.redirect_stdout(out):
            osctl.main(list(self.ARGV))
        return json.loads(out.getvalue().strip().splitlines()[-1]), saved

    def test_owner_shared_and_auto_reach_the_save(self):
        for env in ({"LEOBOT_WORKSPACE": "acme"},
                    {"LEOBOT_WORKSPACE": "acme", "LEOBOT_SHARED": "1"},
                    {"LEOBOT_WORKSPACE": "acme", "LEOBOT_JOB": "j1", "LEOBOT_JOB_MODE": "auto"}):
            out, saved = self._run(env)
            self.assertTrue(out["ok"], (env, out))
            saved.assert_called_once()

    def test_read_only_turn_refused(self):
        out, saved = self._run({"LEOBOT_WORKSPACE": "acme", "LEOBOT_READONLY": "1"})
        self.assertFalse(out["ok"])
        saved.assert_not_called()


if __name__ == "__main__":
    unittest.main()
