"""Browser settings store: per-bot level and mode, sites with caps."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from core import browser


class Settings(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_defaults_off_and_bot(self):
        self.assertEqual(browser.bot_setting("demo", "b1", self.root), {"level": "off", "mode": "bot"})
        self.assertEqual(browser.sites(self.root)["linkedin.com"], {"mode": None, "daily": 20, "gap": 120})
        browser.set_bot("demo", "b1", "read", "my_chrome", self.root)
        self.assertEqual(browser.bot_setting("demo", "b1", self.root), {"level": "read", "mode": "my_chrome"})
        self.assertEqual(browser.bot_setting("demo", "b2", self.root), {"level": "off", "mode": "bot"})

    def test_unicode_site_name_matches_its_pages(self):
        def u(rest):
            return "https" + "://" + rest
        browser.set_site("b\u00fccher.test", None, 5, self.root)
        browser.set_site("\uff41\uff43\uff4d\uff45.test", None, 3, self.root)  # fullwidth letters
        self.assertIn("xn--bcher-kva.test", browser.sites(self.root))
        self.assertIn("acme.test", browser.sites(self.root))
        self.assertEqual(browser.site_of(u("www.b\u00fccher.test/x"), self.root), "xn--bcher-kva.test")
        self.assertEqual(browser.site_of(u("xn--bcher-kva.test/x"), self.root), "xn--bcher-kva.test")
        self.assertEqual(browser.site_of(u("acme.test/"), self.root), "acme.test")
        self.assertTrue(browser.remove_site("B\u00dcCHER.test", self.root))
        self.assertNotIn("xn--bcher-kva.test", browser.sites(self.root))

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

    def test_site_of_reads_like_a_browser(self):
        def u(rest):
            return "https://" + rest
        # a browser reads the backslash as a slash: the host is the listed site
        self.assertEqual(browser.site_of(u("linkedin.com\\@evil.example/"), self.root), "linkedin.com")
        # ...and here the host is the other one
        self.assertIsNone(browser.site_of(u("evil.example\\@linkedin.com/"), self.root))
        # tab and newline vanish inside the host; edges are trimmed
        self.assertEqual(browser.site_of(u("linked\nin.com/"), self.root), "linkedin.com")
        self.assertEqual(browser.site_of(u("linkedin.com\t.evil.example/"), self.root), None)
        self.assertEqual(browser.site_of("  \x01" + u("www.linkedin.com") + " \r\n", self.root), "linkedin.com")

    def test_site_of_matches_chromium_forms(self):
        def u(rest):
            return "https:" + rest
        hit = ("linkedin.com", "/linkedin.com", "//linkedin.com", "///linkedin.com", "////www.linkedin.com/in",
               "//%6Cinkedin.com/", "//linkedin%2Ecom/", "//linkedin\u3002com", "//linkedin\uff0ecom",
               "//linkedin\uff61com", "//\uff4c\uff49\uff4e\uff4b\uff45\uff44\uff49\uff4e.com",
               "//link\u00adedin.com", "//link\u200bedin.com", "//link\u200dedin.com",
               "//link\u2060edin.com", "//\ufefflinkedin.com", "//LINKEDIN.COM.")
        for rest in hit:
            self.assertEqual(browser.site_of(u(rest), self.root), "linkedin.com", repr(rest))
        self.assertEqual(browser.site_of("HTTP:\\\\linkedin.com", self.root), "linkedin.com")
        miss = ("//evil.example/linkedin.com", "//linkedin.com.evil.example", "//xn--80ak6aa92e.com",
                "//evil%2Elinkedin.example", "//linkedin\u3002com.evil.example")
        for rest in miss:
            self.assertIsNone(browser.site_of(u(rest), self.root), repr(rest))

    def test_host_of_and_is_web_url(self):
        self.assertIsNone(browser.host_of("https:" + "///"))
        self.assertIsNone(browser.host_of("https:" + "//"))
        self.assertIsNone(browser.host_of("ftp:" + "//a.test"))
        self.assertIsNone(browser.host_of(None))
        self.assertEqual(browser.host_of("https:" + "//Www.Acme.TEST:81/x"), "www.acme.test")
        self.assertEqual(browser.host_of("https:" + "//b\u00fccher.test"), "xn--bcher-kva.test")
        self.assertTrue(browser.is_web_url("HTTPS:" + "/a.test"))
        self.assertTrue(browser.is_web_url("https:" + "///"))
        self.assertFalse(browser.is_web_url("file:" + "///etc"))
        self.assertFalse(browser.is_web_url(5))

    def test_forbidden_host_code_points(self):
        for rest in ("//linkedin.com%00.evil.example", "//linkedin.com%20.evil.example", "//linkedin.com%09",
                     "//linkedin.com%0a.x", "//linkedin.com%7f", "//linkedin.com%2f.evil.example",
                     "//linkedin.com%3a8", "//linkedin.com%40evil.example", "//linkedin.com%25"):
            url = "https:" + rest
            self.assertIsNone(browser.host_of(url), rest)
            self.assertIsNone(browser.site_of(url, self.root), rest)
        self.assertIsNotNone(browser.host_of("https:" + "//[::1]/"))
        self.assertIsNone(browser.site_of("https:" + "//[::1]/", self.root))

    def test_wrong_shape_files_read_as_defaults(self):
        path = self.root / "team" / "browser.json"
        path.parent.mkdir(parents=True)
        for raw in ("not json {", "[1]", '{"bots":[1]}', '{"bots":{"demo":[1]}}',
                    '{"bots":{"demo":{"b1":5}}}', '{"sites":[1]}', '{"sites":{"x.test":7}}', '{"sites":{"x.test":[1]}}'):
            path.write_text(raw)
            self.assertEqual(browser.bot_setting("demo", "b1", self.root), {"level": "off", "mode": "bot"}, raw)
            self.assertIn("linkedin.com", browser.sites(self.root)) if "x.test" not in raw else browser.sites(self.root)
            self.assertIsNone(browser.site_of("http" + "://other.test", self.root))
            browser.forget_workspace("demo", self.root)
            path.write_text(raw)
            browser.set_bot("demo", "b1", "read", "bot", self.root)
            self.assertEqual(browser.bot_setting("demo", "b1", self.root)["level"], "read", raw)
            path.write_text(raw)
            browser.set_site("acme.test", "bot", 5, self.root)
            self.assertEqual(browser.sites(self.root)["acme.test"], {"mode": "bot", "daily": 5, "gap": None}, raw)

    def test_forget_workspace_skips_write_when_nothing_removed(self):
        browser.forget_workspace("demo", self.root)
        self.assertFalse((self.root / "team" / "browser.json").exists())

    def test_daily_bounds(self):
        for bad in (0, 501, -1, True, "5"):
            with self.assertRaises(ValueError):
                browser.set_site("acme.com", None, bad, self.root)
        browser.set_site("Acme.COM:8080", "bot", 500, self.root)
        self.assertEqual(browser.sites(self.root)["acme.com"], {"mode": "bot", "daily": 500, "gap": None})
        browser.set_site("acme.com", None, None, self.root)
        self.assertEqual(browser.sites(self.root)["acme.com"], {"mode": None, "daily": None, "gap": None})
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

class LoadCache(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_each_caller_gets_its_own_copy(self):
        browser.set_bot("demo", "b1", "read", "bot", self.root)
        browser.load(self.root)["bots"]["demo"]["b1"]["level"] = "act"
        self.assertEqual(browser.bot_setting("demo", "b1", self.root)["level"], "read")

    def test_a_write_from_outside_is_read(self):
        browser.set_bot("demo", "b1", "read", "bot", self.root)
        self.assertEqual(browser.bot_setting("demo", "b1", self.root)["level"], "read")
        path = self.root / "team" / "browser.json"
        path.write_text(json.dumps({"bots": {"demo": {"b1": {"level": "write", "mode": "bot"}}}}))
        self.assertEqual(browser.bot_setting("demo", "b1", self.root)["level"], "write")
        path.unlink()
        self.assertEqual(browser.bot_setting("demo", "b1", self.root)["level"], "off")

    def test_an_unchanged_file_is_parsed_once(self):
        browser.set_bot("demo", "b1", "read", "bot", self.root)
        browser.load(self.root)
        with mock.patch("json.loads", side_effect=AssertionError("parsed again")):
            self.assertEqual(browser.bot_setting("demo", "b1", self.root)["level"], "read")


if __name__ == "__main__":
    unittest.main()
