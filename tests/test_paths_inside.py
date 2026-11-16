"""paths.inside: one entry directly inside a folder, never anywhere else."""
import time
import unittest

from core import jobs, paths


class Inside(unittest.TestCase):
    def test_plain_names_pass(self):
        self.assertEqual(str(paths.inside("/base/dir", "acme")), "/base/dir/acme")
        self.assertEqual(paths.inside("/base", "ex-1.json").name, "ex-1.json")

    def test_escapes_are_refused(self):
        for name in ("../x", "a/b", "/etc", "..", ".", "", "a/../.."):
            with self.assertRaises(ValueError, msg=name):
                paths.inside("/base/dir", name)


class WeeklySchedule(unittest.TestCase):
    def test_day_lists_parse_and_a_long_bad_one_is_fast(self):
        for text in ("mon,thu 09:00", "mon, thu 09:00", "daily 09:00"):
            self.assertTrue(jobs._WEEKLY_RE.match(text), text)
        started = time.time()
        self.assertIsNone(jobs._WEEKLY_RE.match("a" + " " * 20000 + "x"))
        self.assertLess(time.time() - started, 0.5)


if __name__ == "__main__":
    unittest.main()
