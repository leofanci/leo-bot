"""chain.py: bot-to-bot chains in one message. Hop and cost limits (Settings, General) and pure helpers."""
import json
import tempfile
import unittest
from pathlib import Path

from core import chain, general, paths


class ChainSettings(unittest.TestCase):
    def setUp(self):
        self.saved = paths.SETTINGS.read_text() if paths.SETTINGS.exists() else None
        paths.SETTINGS.unlink(missing_ok=True)

    def tearDown(self):
        if self.saved is None:
            paths.SETTINGS.unlink(missing_ok=True)
        else:
            paths.SETTINGS.write_text(self.saved)

    def test_defaults_are_unlimited(self):
        v = general.view()
        self.assertEqual((v["chain_hops"], v["chain_cost"]), (0, 0.0))
        self.assertEqual(chain.limits(), (0, 0.0))

    def test_update_saves_and_validates(self):
        general.update({"chain_hops": 6, "chain_cost": 1.5})
        self.assertEqual(chain.limits(), (6, 1.5))
        for bad in ({"chain_hops": -1}, {"chain_hops": 2.5}, {"chain_hops": True}, {"chain_hops": 1001},
                    {"chain_cost": -0.1}, {"chain_cost": "x"}, {"chain_cost": float("inf")},
                    {"chain_cost": True}, {"chain_hops": 3, "chain_cost": -1}):
            with self.subTest(bad), self.assertRaises(ValueError):
                general.update(bad)
            self.assertEqual(chain.limits(), (6, 1.5))  # nothing saved
        for reset in (None, ""):
            general.update({"chain_hops": 6, "chain_cost": 1.5})
            general.update({"chain_hops": reset, "chain_cost": reset})
            self.assertEqual(chain.limits(), (0, 0.0))

    def test_loop_fields_validate(self):
        v = general.view()
        self.assertEqual((v["loop_mode"], v["loop_days"]), ("report", 30))
        general.update({"loop_mode": "auto", "loop_days": 0})
        self.assertEqual((general.view()["loop_mode"], general.view()["loop_days"]), ("auto", 0))
        for bad in ({"loop_mode": "free"}, {"loop_mode": None}, {"loop_days": -1}, {"loop_days": 366},
                    {"loop_days": 2.5}, {"loop_days": True}):
            with self.subTest(bad), self.assertRaises(ValueError):
                general.update(bad)
        general.update({"loop_days": ""})
        self.assertEqual(general.view()["loop_days"], 30)

    def test_workflow_cost_validates(self):
        from core import workflows
        self.assertEqual(general.view()["workflow_cost"], 0.0)
        general.update({"workflow_cost": 2.5})
        self.assertEqual(workflows.cost_cap(), 2.5)
        for bad in (-0.1, "x", float("inf"), True, general.CHAIN_COST_MAX + 1):
            with self.subTest(bad), self.assertRaises(ValueError):
                general.update({"workflow_cost": bad})
            self.assertEqual(workflows.cost_cap(), 2.5)
        general.update({"workflow_cost": ""})
        self.assertEqual(workflows.cost_cap(), 0.0)
        paths.SETTINGS.write_text(json.dumps({"workflow_cost": "lots"}))
        self.assertEqual(workflows.cost_cap(), 0.0)
        paths.SETTINGS.write_text("{not json")
        self.assertEqual(workflows.cost_cap(), 0.0)

    def test_hand_edited_file_never_breaks(self):
        paths.SETTINGS.write_text(json.dumps({"chain_hops": "lots", "chain_cost": [1]}))
        self.assertEqual(chain.limits(), (0, 0.0))
        paths.SETTINGS.write_text("{not json")
        self.assertEqual(chain.limits(), (0, 0.0))


class WorkspaceLimits(unittest.TestCase):
    """Each workspace may set its own hops and cost (team/chain.json); a value left empty inherits Settings."""

    def setUp(self):
        self.saved = paths.SETTINGS.read_text() if paths.SETTINGS.exists() else None
        paths.SETTINGS.unlink(missing_ok=True)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def tearDown(self):
        if self.saved is None:
            paths.SETTINGS.unlink(missing_ok=True)
        else:
            paths.SETTINGS.write_text(self.saved)

    def test_workspace_override_wins(self):
        general.update({"chain_hops": 5, "chain_cost": 2.0})
        chain.set_override(self.root, "acme", 2, None)
        self.assertEqual(chain.limits("acme", self.root), (2, 2.0))
        self.assertEqual(chain.limits("hq", self.root), (5, 2.0))
        self.assertEqual(chain.limits(None, self.root), (5, 2.0))
        chain.set_override(self.root, "acme", None, 0)
        self.assertEqual(chain.limits("acme", self.root), (5, 0.0))  # 0 here: no cap in this workspace
        self.assertEqual(chain.overrides(self.root), {"acme": {"hops": None, "cost": 0.0}})

    def test_cleared_override_inherits(self):
        general.update({"chain_hops": 5})
        chain.set_override(self.root, "acme", 2, 1.0)
        chain.set_override(self.root, "acme", "", None)
        self.assertEqual(chain.limits("acme", self.root), (5, 0.0))
        self.assertEqual(chain.overrides(self.root), {})

    def test_bad_override_refused(self):
        chain.set_override(self.root, "acme", 3, 1.0)
        for hops, cost in ((-1, None), (2.5, None), (True, None), (1001, None), (None, "x"),
                           (None, -1), (None, float("inf")), (None, True)):
            with self.subTest(hops=hops, cost=cost), self.assertRaises(ValueError):
                chain.set_override(self.root, "acme", hops, cost)
        self.assertEqual(chain.limits("acme", self.root), (3, 1.0))

    def test_broken_file_reads_as_none(self):
        general.update({"chain_hops": 4})
        f = self.root / "team" / "chain.json"
        f.parent.mkdir(parents=True)
        for text in ("{", "[]", json.dumps({"acme": {"hops": "x", "cost": -3}}), json.dumps({"acme": 7})):
            f.write_text(text)
            with self.subTest(text):
                self.assertEqual(chain.limits("acme", self.root), (4, 0.0))


class ChainView(unittest.TestCase):
    def test_view_names_own_and_settings(self):
        from unittest import mock
        import dashboard.server as server
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(server, "ROOT", Path(tmp)), \
                mock.patch.object(server.general, "view", return_value={"chain_hops": 4, "chain_cost": 1.0}):
            self.assertEqual(server._chain_view("acme"), {"ws": "acme", "hops": None, "cost": None,
                                                          "settings": {"hops": 4, "cost": 1.0}})
            chain.set_override(Path(tmp), "acme", 2, None)
            self.assertEqual(server._chain_view("acme")["hops"], 2)


class ChainHelpers(unittest.TestCase):
    def test_within_hops(self):
        self.assertTrue(chain.within_hops(0, 0))
        self.assertTrue(chain.within_hops(99, 0))
        self.assertTrue(chain.within_hops(2, 3))
        self.assertFalse(chain.within_hops(3, 3))

    def test_pingpong(self):
        self.assertFalse(chain.pingpong(["cos", "research", "cos"]))
        self.assertTrue(chain.pingpong(["cos", "research", "cos", "research"]))
        self.assertFalse(chain.pingpong(["cos", "research", "cmo", "research"]))
        self.assertTrue(chain.pingpong(["x", "cos", "research", "cos", "research"]))
        self.assertFalse(chain.pingpong(["cos", "cos", "cos", "cos"]))
        self.assertFalse(chain.pingpong([]))

    def test_missed(self):
        self.assertEqual(chain.missed([("a", 0)], [True]), {"a": 0})
        self.assertEqual(chain.missed([("a", 0), ("b", 1)], [True, True]), {"a": 1, "b": 0})
        self.assertEqual(chain.missed([("a", 0), ("b", 1), ("a", 2), ("c", 3)], [True] * 4),
                         {"a": 1, "b": 2, "c": 0})
        # a dropped (empty) reply is not counted; one after the last run (a consult) is
        self.assertEqual(chain.missed([("a", 0), ("b", 1), ("b", 3)], [True, False, True, True, True]),
                         {"a": 3, "b": 1})

if __name__ == "__main__":
    unittest.main()
