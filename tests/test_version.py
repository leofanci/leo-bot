import unittest

from core import paths, version


class VersionTests(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(version.parse("v1.2.3"), (1, 2, 3))
        self.assertEqual(version.parse("1.2.3"), (1, 2, 3))
        self.assertIsNone(version.parse("v1.2"))
        self.assertIsNone(version.parse("v1.2.3-beta"))

    def test_latest_tag(self):
        h = "a" * 40
        out = "\n".join(f"{h}\trefs/tags/{t}" for t in
                         ["v0.1.0", "v0.10.0", "v0.9.9", "v0.10.0^{}", "nightly"])
        self.assertEqual(version.latest_tag(out), "v0.10.0")
        self.assertIsNone(version.latest_tag(""))

    def test_newer(self):
        self.assertTrue(version.newer("0.1.0", "v0.1.1"))
        self.assertFalse(version.newer("0.2.0", "v0.1.9"))
        self.assertFalse(version.newer("0.1.0", "v0.1.0"))

    def test_current_reads_version_file(self):
        expected = (paths.CODE / "VERSION").read_text(encoding="utf-8").strip()
        self.assertEqual(version.current(), expected)


if __name__ == "__main__":
    unittest.main()
