"""chain.py: bot-to-bot chains in one message. Hop and cost limits (Settings, General) and pure helpers."""
import json
import unittest

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

    def test_hand_edited_file_never_breaks(self):
        paths.SETTINGS.write_text(json.dumps({"chain_hops": "lots", "chain_cost": [1]}))
        self.assertEqual(chain.limits(), (0, 0.0))
        paths.SETTINGS.write_text("{not json")
        self.assertEqual(chain.limits(), (0, 0.0))


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
