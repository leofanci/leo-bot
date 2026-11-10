"""osctl workspace walls hold whatever the letter case, for records with no owner, and when
the index is behind. Each test is one way a bot in workspace `beta` (or an invited person's
shared thread there) could have reached workspace `acme` or HQ."""
import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import index
import dashboard.db as db
import dashboard.fileops as fileops
import dashboard.osctl as osctl
from tests.test_index_projects import write


def run(argv, ws=None, shared=False):
    env = {"LEOBOT_WORKSPACE": ws or ""}
    if shared:
        env["LEOBOT_SHARED"] = "1"
    buf = io.StringIO()
    with mock.patch.dict(os.environ, env), contextlib.redirect_stdout(buf):
        code = osctl.main(argv)
    return code, json.loads(buf.getvalue().strip().splitlines()[-1])


class Walls(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        for p in (mock.patch.object(fileops, "ROOT", self.root),
                  mock.patch.object(db, "DB_PATH", self.root / "database" / "data" / "os.db")):
            p.start()
            self.addCleanup(p.stop)
        write(self.root / "projects" / ".keep", "")
        index.build(self.root)
        for slug in ("acme", "beta"):
            self.assertEqual(run(["create-project", "--slug", slug, "--name", slug])[0], 0)

    def test_a_mixed_case_slug_in_another_workspace_is_refused(self):
        fileops.create_profile("acme", "BrandUp", {"name": "Brand"})
        fileops.reindex()
        for argv in (["get-project", "--slug", "acme"],
                     ["update-profile", "--slug", "BrandUp", "--name", "from-beta"],
                     ["get-brief-spec", "--profile", "BrandUp"]):
            with self.subTest(argv):
                code, out = run(argv, ws="beta", shared=True)
                self.assertEqual(code, 1, out)
                self.assertIn("another workspace", out.get("error", ""))
        self.assertNotIn("from-beta", (self.root / "projects/acme/profiles/BrandUp/profile.md").read_text())

    def test_an_activity_needs_a_real_entity_and_the_index_keeps_working(self):
        code, out = run(["create-activity", "--entity", "nosuch", "--title", "x", "--date", "2030-01-02"],
                        ws="beta", shared=True)
        self.assertEqual(code, 1, out)
        activities = self.root / "portfolio" / "activities.md"
        self.assertNotIn("nosuch", activities.read_text() if activities.exists() else "")
        self.assertEqual(run(["create-activity", "--entity", "beta", "--title", "ok", "--date", "2030-01-03"],
                             ws="beta")[0], 0)

    def test_an_hq_milestone_cannot_be_changed_from_a_workspace(self):
        code, out = run(["create-milestone", "--title", "hq goal", "--date", "2030-01-01"])
        self.assertEqual(code, 0, out)
        ms = json.loads((self.root / "portfolio" / "milestones.json").read_text())["milestones"][0]
        code, out = run(["update-milestone", "--id", ms["id"], "--entity", "beta", "--title", "moved"],
                        ws="beta", shared=True)
        self.assertEqual(code, 1, out)
        after = json.loads((self.root / "portfolio" / "milestones.json").read_text())["milestones"][0]
        self.assertEqual((after["title"], after.get("entity")), ("hq goal", ms.get("entity")))

    def test_hq_and_external_entities_are_refused_from_a_workspace(self):
        for ent in ("external", "hq", "HQ"):
            code, out = run(["create-activity", "--entity", ent, "--title", "x", "--date", "2030-01-02"],
                            ws="beta", shared=True)
            self.assertEqual(code, 1, (ent, out))

    def test_a_project_the_index_has_not_seen_is_still_walled(self):
        (self.root / "projects" / "gamma").mkdir()
        write(self.root / "projects" / "gamma" / "project.md", "---\nname: Gamma\n---\n")
        code, out = run(["create-experiment", "--project", "gamma", "--assumption", "x"], ws="beta", shared=True)
        self.assertEqual(code, 1, out)
        self.assertFalse((self.root / "projects/gamma/strategy").exists())

    def test_read_file_refuses_a_secret_in_any_letter_case(self):
        write(self.root / "dashboard" / ".auth-token", "DUMMY")
        write(self.root / "team" / "engines.json", "{}")
        write(self.root / "run" / "auth-token", "DUMMY")
        for ws, path in ((None, "team/ENGINES.JSON"), (None, "dashboard/.AUTH-TOKEN"), (None, "run/auth-token"), ("acme", "dashboard/.Auth-Token")):
            with self.subTest(path):
                code, out = run(["read-file", "--path", path], ws=ws)
                self.assertEqual(code, 1, out)
                self.assertNotIn("DUMMY", json.dumps(out))


if __name__ == "__main__":
    unittest.main()
