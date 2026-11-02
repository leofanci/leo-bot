"""People invited into workspaces: invites, joining, roles, removal, sharing URL."""
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from core import people


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        for ws in ("acme", "demo"):
            (self.root / "projects" / ws).mkdir(parents=True)
        self.addCleanup(self.tmp.cleanup)

    def invite(self, ws="acme", role="member", expiry="7d"):
        return people.create_invite(self.root, ws=ws, role=role, by="ceo", expiry=expiry)


class Invites(Base):
    def test_code_is_returned_once_and_only_its_hash_is_stored(self):
        rec, code = self.invite()
        raw = (self.root / "team" / "invites.json").read_text()
        self.assertNotIn(code, raw)
        self.assertNotIn("code_hash", rec)
        self.assertEqual(rec["role"], "member")
        self.assertEqual(rec["ws"], "acme")

    def test_expiry_choices(self):
        now = time.time()
        rec, _ = self.invite(expiry="1d")
        self.assertAlmostEqual(rec["expires"] - now, 86400, delta=5)
        with self.assertRaises(ValueError):
            self.invite(expiry="1y")

    def test_hq_and_unknown_workspaces_cannot_be_invited_into(self):
        for ws in ("hq", "", "nope", "../acme"):
            with self.assertRaises(ValueError):
                self.invite(ws=ws)

    def test_role_must_be_admin_or_member(self):
        with self.assertRaises(ValueError):
            self.invite(role="owner")

    def test_pending_lists_only_live_invites_of_the_workspace(self):
        a, _ = self.invite()
        self.invite(ws="demo")
        used, code = self.invite()
        people.join(self.root, code, "Pat")
        ids = [i["id"] for i in people.pending_invites(self.root, "acme")]
        self.assertEqual(ids, [a["id"]])
        with mock.patch.object(people.time, "time", return_value=time.time() + 8 * 86400):
            self.assertEqual(people.pending_invites(self.root, "acme"), [])

    def test_revoke(self):
        rec, code = self.invite()
        people.revoke_invite(self.root, rec["id"], ws="acme")
        with self.assertRaises(ValueError):
            people.join(self.root, code, "Pat")

    def test_revoke_is_bound_to_its_workspace(self):
        rec, _ = self.invite()
        with self.assertRaises(LookupError):
            people.revoke_invite(self.root, rec["id"], ws="demo")

    def test_invite_preview(self):
        rec, code = self.invite(role="admin")
        self.assertEqual(people.invite_for(self.root, code)["role"], "admin")
        self.assertIsNone(people.invite_for(self.root, "x" * 32))
        self.assertIsNone(people.invite_for(self.root, None))


class Join(Base):
    def test_join_creates_person_with_role(self):
        _, code = self.invite(role="admin")
        pid, token = people.join(self.root, code, "  Pat  ")
        p = people.person_by_token(self.root, token)
        self.assertEqual(p["id"], pid)
        self.assertEqual(p["name"], "Pat")
        self.assertEqual(p["ws"], {"acme": "admin"})
        self.assertNotIn(token, (self.root / "team" / "people.json").read_text())

    def test_link_works_once(self):
        _, code = self.invite()
        people.join(self.root, code, "Pat")
        with self.assertRaises(ValueError):
            people.join(self.root, code, "Sam")

    def test_expired_link_refused(self):
        _, code = self.invite(expiry="1d")
        with mock.patch.object(people.time, "time", return_value=time.time() + 2 * 86400):
            with self.assertRaises(ValueError):
                people.join(self.root, code, "Pat")

    def test_existing_device_adds_workspace_to_same_person(self):
        _, c1 = self.invite()
        pid, token = people.join(self.root, c1, "Pat")
        _, c2 = self.invite(ws="demo", role="admin")
        pid2, token2 = people.join(self.root, c2, "ignored", token=token)
        self.assertEqual((pid2, token2), (pid, token))
        self.assertEqual(people.person_by_token(self.root, token)["ws"], {"acme": "member", "demo": "admin"})

    def test_rejoin_keeps_the_higher_role(self):
        _, c1 = self.invite(role="admin")
        _, token = people.join(self.root, c1, "Pat")
        _, c2 = self.invite(role="member")
        people.join(self.root, c2, "Pat", token=token)
        self.assertEqual(people.person_by_token(self.root, token)["ws"]["acme"], "admin")

    def test_unknown_device_token_starts_a_new_person(self):
        _, code = self.invite()
        pid, token = people.join(self.root, code, "Pat", token="stale")
        self.assertNotEqual(token, "stale")
        self.assertIsNotNone(people.person_by_token(self.root, token))

    def test_name_rules(self):
        for bad in ("", "   ", "x" * 41, "a\x00b", "<b>", None, 7):
            _, code = self.invite()
            with self.assertRaises(ValueError):
                people.join(self.root, code, bad)

    def test_bad_tokens(self):
        for t in (None, "", 5, "nope"):
            self.assertIsNone(people.person_by_token(self.root, t))


class Membership(Base):
    def join(self, ws="acme", role="member", name="Pat"):
        _, code = self.invite(ws=ws, role=role)
        return people.join(self.root, code, name)

    def test_people_in_workspace(self):
        a, _ = self.join(role="admin", name="Ada")
        m, _ = self.join(name="Mo")
        self.join(ws="demo", name="Dee")
        rows = people.people_in(self.root, "acme")
        self.assertEqual([(r["id"], r["role"]) for r in rows], [(a, "admin"), (m, "member")])
        self.assertNotIn("token_hash", json.dumps(rows))

    def test_remove_last_workspace_deletes_person(self):
        pid, token = self.join()
        people.remove(self.root, "acme", pid)
        self.assertIsNone(people.person_by_token(self.root, token))

    def test_remove_one_of_two_keeps_person(self):
        pid, token = self.join()
        _, c2 = self.invite(ws="demo")
        people.join(self.root, c2, "Pat", token=token)
        people.remove(self.root, "acme", pid)
        self.assertEqual(people.person_by_token(self.root, token)["ws"], {"demo": "member"})

    def test_remove_unknown(self):
        with self.assertRaises(LookupError):
            people.remove(self.root, "acme", "p-nope")

    def test_permission_rules(self):
        self.assertTrue(people.can_invite("owner", "admin"))
        self.assertTrue(people.can_invite("owner", "member"))
        self.assertFalse(people.can_invite("admin", "admin"))
        self.assertTrue(people.can_invite("admin", "member"))
        self.assertFalse(people.can_invite("member", "member"))
        self.assertTrue(people.can_remove("owner", "admin"))
        self.assertTrue(people.can_remove("admin", "member"))
        self.assertFalse(people.can_remove("admin", "admin"))
        self.assertFalse(people.can_remove("member", "member"))
        self.assertFalse(people.can_remove(None, "member"))

    def test_project_delete_leaves_no_ghost_rows(self):
        self.join()
        (self.root / "projects" / "acme").rmdir()
        self.assertEqual(people.people_in(self.root, "acme"), [])


class SharingUrl(Base):
    def test_default_none(self):
        self.assertIsNone(people.public_url(self.root))

    def test_set_and_normalize(self):
        self.assertEqual(people.set_public_url(self.root, " https://Mac.tail1.example/ "),
                         "https://mac.tail1.example")
        self.assertEqual(people.public_url(self.root), "https://mac.tail1.example")
        self.assertEqual(people.set_public_url(self.root, "https://a.example:8443"),
                         "https://a.example:8443")
        self.assertIsNone(people.set_public_url(self.root, ""))
        self.assertIsNone(people.public_url(self.root))

    def test_rejects_unsafe_urls(self):
        for bad in ("http://a.example", "https://", "https://a.example/path",
                    "https://" + "u:p@" + "a.example", "https://a.example?x=1",
                    "https://localhost", "https://127.0.0.1", "javascript:alert(1)",
                    "https://" + "a b.example", "https://a.example:99999"):
            with self.assertRaises(ValueError, msg=bad):
                people.set_public_url(self.root, bad)

    def test_host(self):
        people.set_public_url(self.root, "https://a.example:8443")
        self.assertEqual(people.public_host(self.root), "a.example:8443")


if __name__ == "__main__":
    unittest.main()
