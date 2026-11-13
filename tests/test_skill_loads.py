"""Skills a bot loads on demand: the per-chat load record and `osctl get-skill`."""
import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from core import bots, skill_loads
from dashboard import osctl


def run_osctl(*argv):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        osctl.main(list(argv))
    return json.loads(out.getvalue().strip().splitlines()[-1])


class SkillLoads(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        p = mock.patch.object(bots, "ROOT", self.root)
        p.start()
        self.addCleanup(p.stop)
        env = mock.patch.dict(os.environ, {})
        env.start()
        self.addCleanup(env.stop)
        for k in ("LEOBOT_WORKSPACE", "LEOBOT_SHARED", "LEOBOT_READONLY", "LEOBOT_CHAT",
                  "LEOBOT_JOB", "LEOBOT_JOB_MODE"):
            os.environ.pop(k, None)

    def test_record_and_loaded_unique_in_order(self):
        skill_loads.record(self.root, "c1", "pricing-strategy")
        skill_loads.record(self.root, "c1", "positioning")
        skill_loads.record(self.root, "c1", "pricing-strategy")
        self.assertEqual(skill_loads.loaded(self.root, "c1"), ["pricing-strategy", "positioning"])

    def test_clear_removes_the_record(self):
        skill_loads.record(self.root, "c1", "positioning")
        skill_loads.clear(self.root, "c1")
        self.assertEqual(skill_loads.loaded(self.root, "c1"), [])
        skill_loads.clear(self.root, "c1")  # twice is fine

    def test_bad_chat_id_is_ignored(self):
        skill_loads.record(self.root, "../x", "positioning")
        self.assertEqual(skill_loads.loaded(self.root, "../x"), [])
        self.assertFalse((self.root / "x.json").exists())
        skill_loads.record(self.root, "", "positioning")
        self.assertEqual(skill_loads.loaded(self.root, ""), [])

    def test_corrupt_record_reads_empty(self):
        f = self.root / "team" / "skill_loads" / "c1.json"
        f.parent.mkdir(parents=True)
        f.write_text("{not json")
        self.assertEqual(skill_loads.loaded(self.root, "c1"), [])

    def test_get_skill_prints_body_and_records(self):
        os.environ["LEOBOT_CHAT"] = "c1"
        r = run_osctl("get-skill", "--name", "positioning")
        self.assertTrue(r["ok"], r)
        self.assertIn("positioning", r["body"].lower())
        self.assertEqual(skill_loads.loaded(self.root, "c1"), ["positioning"])

    def test_get_skill_without_chat_records_nothing(self):
        r = run_osctl("get-skill", "--name", "positioning")
        self.assertTrue(r["ok"], r)
        self.assertFalse((self.root / "team" / "skill_loads").exists())

    def test_get_skill_rail_stub(self):
        r = run_osctl("get-skill", "--name", "workspace")
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["body"], skill_loads.RAIL_STUBS["workspace"])

    def test_get_skill_unknown_lists_names(self):
        os.environ["LEOBOT_CHAT"] = "c1"
        r = run_osctl("get-skill", "--name", "nope")
        self.assertFalse(r["ok"])
        self.assertIn("unknown skill", r["error"])
        self.assertIn("positioning", r["error"])
        self.assertEqual(skill_loads.loaded(self.root, "c1"), [])

    def test_get_skill_name_cannot_escape_skills_dir(self):
        r = run_osctl("get-skill", "--name", "../docs")
        self.assertFalse(r["ok"])
        self.assertIn("unknown skill", r["error"])

    def test_get_skill_allowed_everywhere(self):
        for s in (osctl._READ_ONLY_CMDS, osctl._LOCAL_CMDS, osctl._SHARED_ALLOWED, osctl._AUTO_ALLOWED):
            self.assertIn("get-skill", s)

    def test_server_loader_uses_the_shared_body(self):
        import dashboard.server as server
        self.assertEqual(server._load_skill_body("positioning"), skill_loads.body("positioning"))
        self.assertEqual(server._load_skill_body("workspace"), skill_loads.RAIL_STUBS["workspace"])
        self.assertEqual(skill_loads.body("nope"), "")


if __name__ == "__main__":
    unittest.main()
