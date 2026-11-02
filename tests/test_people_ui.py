"""Dashboard pieces for invited people: trimmed rail, Team People, private channels."""
import unittest
from pathlib import Path

DASH = Path(__file__).resolve().parent.parent / "dashboard"
JS = (DASH / "app.js").read_text(encoding="utf-8")
HTML = (DASH / "app.html").read_text(encoding="utf-8")


class PersonView(unittest.TestCase):
    def test_boot_asks_who_is_signed_in_first(self):
        self.assertIn("async function boot(){ await loadMe();", JS)
        self.assertIn('api("/api/me")', JS)

    def test_person_routes_are_allowlisted(self):
        self.assertIn("const PERSON_ROUTE = /^\\/(team|room\\/new|room\\/[a-z0-9-]+\\/edit)$/;", JS)
        self.assertIn('if (isPerson() && !PERSON_ROUTE.test(path)) path = "/team";', JS)

    def test_rail_skips_owner_reads_for_a_person(self):
        self.assertIn('person ? [] : api("/api/tree")', JS)
        self.assertIn('person ? [] : api("/api/approvals" + q)', JS)

    def test_terminal_never_opens_for_a_person(self):
        self.assertIn("if (isPerson()) return;   // the terminal is the owner's", JS)

    def test_open_thread_polls_and_names_authors(self):
        self.assertIn("setInterval(pollThread, 5000);", JS)
        self.assertIn("function senderOf(by, byName)", JS)

    def test_team_has_people_invites_and_sharing(self):
        for needle in ('jpost("/api/invites"', 'jpost("/api/invites/revoke"', 'jpost("/api/people/remove"',
                       'jpost("/api/sharing"', "function peopleSection(pv, link)"):
            self.assertIn(needle, JS)

    def test_private_channels(self):
        self.assertIn('id="room-private"', JS)
        self.assertIn('icon(r.private ? "lock" : "hash")', JS)
        self.assertIn('<symbol id="i-lock"', HTML)


if __name__ == "__main__":
    unittest.main()
