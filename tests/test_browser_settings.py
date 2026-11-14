"""Browser settings store: per-bot level and mode, sites with caps."""
import tempfile
import unittest
from pathlib import Path

from core import browser


class Settings(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_defaults_off_and_bot(self):
        self.assertEqual(browser.bot_setting("demo", "b1", self.root), {"level": "off", "mode": "bot"})
        self.assertEqual(browser.sites(self.root)["linkedin.com"], {"mode": None, "daily": 20})
        browser.set_bot("demo", "b1", "read", "my_chrome", self.root)
        self.assertEqual(browser.bot_setting("demo", "b1", self.root), {"level": "read", "mode": "my_chrome"})
        self.assertEqual(browser.bot_setting("demo", "b2", self.root), {"level": "off", "mode": "bot"})

    def test_rejects_unknown_level(self):
        with self.assertRaises(ValueError):
            browser.set_bot("demo", "b1", "root", "bot", self.root)
        with self.assertRaises(ValueError):
            browser.set_bot("demo", "b1", "read", "tor", self.root)

    def test_site_of_matches_subdomains_only(self):
        # Built from parts so no literal URL host sits in a tracked file.
        def u(rest, scheme="https"):
            return scheme + "://" + rest
        for url in (u("linkedin.com/in/x"), u("www.linkedin.com/feed"), u("m.linkedin.com"),
                    u("WWW.LinkedIn.com:8443/x", "HTTPS")):
            self.assertEqual(browser.site_of(url, self.root), "linkedin.com", url)
        for url in (u("evil-linkedin.com"), u("linkedin.com.evil.example"), u("other.test"),
                    "not a url", u("linkedin.com", "ftp"), "", None, u("[bad", "http")):
            self.assertIsNone(browser.site_of(url, self.root), url)

    def test_daily_bounds(self):
        for bad in (0, 501, -1, True, "5"):
            with self.assertRaises(ValueError):
                browser.set_site("acme.com", None, bad, self.root)
        browser.set_site("Acme.COM:8080", "bot", 500, self.root)
        self.assertEqual(browser.sites(self.root)["acme.com"], {"mode": "bot", "daily": 500})
        browser.set_site("acme.com", None, None, self.root)
        self.assertEqual(browser.sites(self.root)["acme.com"], {"mode": None, "daily": None})
        self.assertIn("linkedin.com", browser.sites(self.root))
        with self.assertRaises(ValueError):
            browser.set_site("acme.com", "tor", 5, self.root)
        self.assertEqual(browser.site_of("https:" + "//shop.acme.com", self.root), "acme.com")

    def test_forget_workspace_drops_bots(self):
        browser.set_bot("demo", "b1", "act", "bot", self.root)
        browser.set_bot("acme", "b1", "read", "bot", self.root)
        browser.set_bot("hq", "b1", "read", "bot", self.root)
        browser.forget_workspace("demo", self.root)
        browser.forget_workspace("hq", self.root)
        self.assertEqual(browser.bot_setting("demo", "b1", self.root)["level"], "off")
        self.assertEqual(browser.bot_setting("acme", "b1", self.root)["level"], "read")
        self.assertEqual(browser.bot_setting("hq", "b1", self.root)["level"], "read")


if __name__ == "__main__":
    unittest.main()
