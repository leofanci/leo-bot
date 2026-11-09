import os
import tempfile
import unittest
from pathlib import Path

from dashboard import deliverables


class DeliverablesWalls(unittest.TestCase):
    def test_symlink_and_bad_slug_skipped(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            proj = root / "projects" / "demo" / "strategy"
            proj.mkdir(parents=True)
            (proj / "intake.md").write_text("x")
            outside = root / "outside.md"
            outside.write_text("y")
            os.symlink(outside, proj / "technical-link.md")
            snap = deliverables.snapshot(root, "demo")
            self.assertIn("strategy/intake.md", snap)
            self.assertNotIn("strategy/technical-link.md", snap)
            for bad in ("..", "a/b", "", None):
                with self.assertRaises(ValueError):
                    deliverables.snapshot(root, bad)


if __name__ == "__main__":
    unittest.main()
