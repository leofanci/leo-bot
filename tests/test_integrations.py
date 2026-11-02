"""Connected apps store: write-only Composio key, per-person sessions, per-bot grants."""
import json
import tempfile
import unittest
from pathlib import Path

from core import integrations


class Store(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_key_write_only(self):
        integrations.set_key("ak_testkey", self.root)
        pub = integrations.public(self.root)
        self.assertTrue(pub["configured"])
        self.assertIsInstance(pub["saved"], float)
        self.assertNotIn("ak_testkey", json.dumps(pub))
        self.assertEqual(integrations.api_key(self.root), "ak_testkey")

    def test_unconfigured(self):
        self.assertEqual(integrations.public(self.root), {"configured": False, "saved": None})
        self.assertIsNone(integrations.api_key(self.root))

    def test_empty_key_rejected(self):
        for bad in ("", "   ", None):
            with self.assertRaises(ValueError):
                integrations.set_key(bad, self.root)

    def test_file_mode_0600(self):
        integrations.set_key("ak_testkey", self.root)
        path = self.root / "team" / "integrations.json"
        self.assertEqual(oct(path.stat().st_mode & 0o777), "0o600")

    def test_person_id_random_and_stable(self):
        a = integrations.person("ceo", self.root)["composio_user_id"]
        self.assertTrue(a.startswith("myos-"))
        self.assertNotIn("ceo", a)
        self.assertEqual(integrations.person("ceo", self.root)["composio_user_id"], a)

    def test_sessions_are_per_workspace(self):
        integrations.save_session("ceo", "hq", "sess1", "https://mcp.acme.example/x", "{}", self.root)
        integrations.save_session("ceo", "acme", "sess2", "https://mcp.acme.example/y", "{}", self.root)
        self.assertEqual(integrations.session("ceo", "hq", self.root)["session_id"], "sess1")
        self.assertEqual(integrations.session("ceo", "acme", self.root)["session_id"], "sess2")
        integrations.forget_session("ceo", "hq", self.root)
        self.assertEqual(integrations.session("ceo", "hq", self.root), {})
        self.assertEqual(integrations.session("ceo", "acme", self.root)["session_id"], "sess2")

    def test_new_accounts_join_hq_only_once(self):
        rows = [{"id": "ca_1", "toolkit": "gmail", "label": "work"}]
        integrations.note_known(rows, self.root)
        self.assertEqual(integrations.accounts_for("hq", self.root), {"gmail": ["ca_1"]})
        self.assertEqual(integrations.accounts_for("acme", self.root), {})  # never automatic
        integrations.set_account("hq", "ca_1", False, self.root)
        integrations.note_known(rows, self.root)  # seen before: HQ's choice stands
        self.assertEqual(integrations.accounts_for("hq", self.root), {})

    def test_pick_accounts_per_workspace(self):
        integrations.note_known([{"id": "ca_1", "toolkit": "gmail", "label": "a"},
                                 {"id": "ca_2", "toolkit": "gmail", "label": "b"},
                                 {"id": "ca_3", "toolkit": "gmail", "label": "c"}], self.root)
        integrations.set_account("acme", "ca_1", True, self.root)
        integrations.set_account("demo", "ca_2", True, self.root)
        integrations.set_account("demo", "ca_3", True, self.root)
        integrations.set_account("demo", "ca_3", True, self.root)  # twice is once
        self.assertEqual(integrations.accounts_for("acme", self.root), {"gmail": ["ca_1"]})
        self.assertEqual(integrations.accounts_for("demo", self.root), {"gmail": ["ca_2", "ca_3"]})
        for bad in ("ca_9", "../x", None):
            with self.assertRaises(ValueError):
                integrations.set_account("acme", bad, True, self.root)

    def test_gone_account_is_not_used(self):
        integrations.note_known([{"id": "ca_1", "toolkit": "gmail", "label": "a"}], self.root)
        integrations.set_account("acme", "ca_1", True, self.root)
        integrations.note_known([], self.root)  # revoked in Composio
        self.assertEqual(integrations.accounts_for("acme", self.root), {})
        self.assertEqual(integrations.assigned(self.root)["acme"], {"gmail": ["ca_1"]})

    def test_account_cap(self):
        integrations.note_known([{"id": f"ca_{i}", "toolkit": "gmail", "label": str(i)}
                                 for i in range(integrations.MAX_ACCOUNTS + 1)], self.root)
        with self.assertRaises(ValueError):
            integrations.set_account("hq", f"ca_{integrations.MAX_ACCOUNTS}", True, self.root)

    def test_effective_grant_needs_an_account(self):
        integrations.set_grant("acme", "cos", {"gmail": "write", "slack": "read"}, self.root)
        self.assertEqual(integrations.effective_grant("acme", "cos", self.root), {})
        integrations.note_known([{"id": "ca_1", "toolkit": "gmail", "label": "a"}], self.root)
        integrations.set_account("acme", "ca_1", True, self.root)
        self.assertEqual(integrations.effective_grant("acme", "cos", self.root), {"gmail": "write"})

    def test_default_and_explicit_empty_grant(self):
        self.assertEqual(integrations.grant("hq", "cos", self.root),
                         {"gmail": "write", "googlecalendar": "write"})
        self.assertEqual(integrations.grant("acme", "cos", self.root), {})
        self.assertEqual(integrations.grant("hq", "cmo", self.root), {})
        integrations.set_grant("hq", "cos", {}, self.root)
        self.assertEqual(integrations.grant("hq", "cos", self.root), {})

    def test_set_grant_round_trip(self):
        integrations.set_grant("acme", "social", {"instagram": "write", "googledrive": "read"}, self.root)
        self.assertEqual(integrations.grant("acme", "social", self.root),
                         {"instagram": "write", "googledrive": "read"})

    def test_set_grant_validates(self):
        for bad in ({"firecrawl": "write"}, {"gmail": "all"}, {"Bad Slug": "read"}, ["gmail"]):
            with self.assertRaises(ValueError):
                integrations.set_grant("hq", "cos", bad, self.root)

    def test_remove_key_drops_sessions_keeps_grants(self):
        integrations.set_key("ak_testkey", self.root)
        integrations.save_session("ceo", "hq", "sess1", "https://mcp.acme.example/x", "{}", self.root)
        integrations.set_grant("acme", "social", {"instagram": "read"}, self.root)
        integrations.remove_key(self.root)
        self.assertIsNone(integrations.api_key(self.root))
        self.assertNotIn("sessions", integrations.person("ceo", self.root))
        self.assertEqual(integrations.grant("acme", "social", self.root), {"instagram": "read"})


if __name__ == "__main__":
    unittest.main()
