"""Connected apps: which Composio tool calls read, wait for approval, or are refused."""
import unittest

from core import app_rules


def _YES(slug):  # Composio tags the tool readOnlyHint
    return True

CAT = ["gmail", "googlecalendar", "googledrive", "google_search_console", "google_maps",
       "supabase", "apollo", "firecrawl", "instagram"]


class Classify(unittest.TestCase):
    def test_read_table(self):
        cases = {
            ("GMAIL_FETCH_EMAILS", "gmail"): True,
            ("GMAIL_LIST_THREADS", "gmail"): True,
            ("GMAIL_SEND_EMAIL", "gmail"): False,
            ("GMAIL_CREATE_EMAIL_DRAFT", "gmail"): False,
            ("GMAIL_GET_AND_DELETE_X", "gmail"): False,          # write verb inside
            ("GOOGLEDOCS_FINDANDREPLACE", "googledocs"): False,  # whole-word only
            ("GOOGLECALENDAR_EVENTS_LIST", "googlecalendar"): True,  # known read
            ("GOOGLECALENDAR_QUICK_ADD", "googlecalendar"): False,
            ("SUPABASE_EXECUTE_PROJECT_DATABASE_QUERY", "supabase"): False,
            ("GOOGLE_MAPS_CREATE_TILES_SESSION", "google_maps"): False,
            ("GOOGLE_MAPS_TILES_CREATE_SESSION", "google_maps"): False,
            # Noun-first reads (names as Composio lists them now).
            ("GOOGLECALENDAR_FREE_BUSY_QUERY", "googlecalendar"): True,
            ("GOOGLECALENDAR_EVENTS_LIST_ALL_CALENDARS", "googlecalendar"): True,
            ("GOOGLE_MAPS_GEOCODE_LOCATION", "google_maps"): True,
            ("GOOGLE_MAPS_GET2D_TILE", "google_maps"): True,
            ("GOOGLE_MAPS_MAPS_EMBED_API", "google_maps"): True,
            ("GOOGLE_MAPS_GEOLOCATE", "google_maps"): True,
            # Noun-first names that change something stay writes.
            ("GOOGLECALENDAR_EVENTS_WATCH", "googlecalendar"): False,
            ("GOOGLECALENDAR_EVENTS_IMPORT", "googlecalendar"): False,
            ("GOOGLECALENDAR_EVENTS_MOVE", "googlecalendar"): False,
            ("GOOGLECALENDAR_CALENDAR_LIST_INSERT", "googlecalendar"): False,
            ("GOOGLECALENDAR_CALENDAR_LIST_DELETE", "googlecalendar"): False,
        }
        for (slug, tk), want in cases.items():
            self.assertIs(app_rules.is_read(slug, tk), want, slug)

    def test_toolkit_longest_prefix(self):
        self.assertEqual(app_rules.toolkit_of("GOOGLE_SEARCH_CONSOLE_LIST_SITES", CAT), "google_search_console")
        self.assertEqual(app_rules.toolkit_of("GMAIL_SEND_EMAIL", CAT), "gmail")
        self.assertIsNone(app_rules.toolkit_of("NOPE_DO_IT", CAT))

    def test_decide(self):
        g = {"gmail": "write", "googledrive": "read", "apollo": "write", "firecrawl": "write"}
        self.assertEqual(app_rules.decide("GMAIL_FETCH_EMAILS", g, CAT, read_only=_YES), ("read", "gmail"))
        self.assertEqual(app_rules.decide("GMAIL_SEND_EMAIL", g, CAT, read_only=_YES), ("hold", "gmail"))
        self.assertEqual(app_rules.decide("GOOGLEDRIVE_UPLOAD_FILE", g, CAT, read_only=_YES), ("refuse", "googledrive"))
        self.assertEqual(app_rules.decide("GOOGLEDRIVE_FIND_FILE", g, CAT, read_only=_YES), ("read", "googledrive"))
        self.assertEqual(app_rules.decide("APOLLO_SEARCH_PEOPLE", g, CAT, read_only=_YES), ("hold", "apollo"))  # paid read
        self.assertEqual(app_rules.decide("FIRECRAWL_SCRAPE", g, CAT, read_only=_YES)[0], "refuse")            # blocked
        self.assertEqual(app_rules.decide("INSTAGRAM_GET_USER_INFO", g, CAT, read_only=_YES)[0], "refuse")     # not granted
        self.assertEqual(app_rules.decide("NOPE_X", g, CAT, read_only=_YES), ("refuse", None))

    def test_a_read_needs_composios_read_only_tag_too(self):
        g = {"gmail": "write", "slack": "read"}
        cat = CAT + ["slack"]
        untagged, unknown = (lambda s: False), (lambda s: None)
        self.assertEqual(app_rules.decide("GMAIL_FETCH_EMAILS", g, cat, read_only=untagged), ("hold", "gmail"))
        self.assertEqual(app_rules.decide("GMAIL_FETCH_EMAILS", g, cat, read_only=unknown), ("hold", "gmail"))
        self.assertEqual(app_rules.decide("GMAIL_FETCH_EMAILS", g, cat), ("hold", "gmail"))  # nobody asked
        # Composio tags this write read-only: the name still makes it a write.
        self.assertEqual(app_rules.decide("SLACK_SET_USER_ACTIVE", g, cat, read_only=_YES), ("refuse", "slack"))
        asked = []
        app_rules.decide("GMAIL_SEND_EMAIL", g, cat, read_only=asked.append)
        self.assertEqual(asked, [])  # a write by name never costs a lookup

    def test_read_grant_refuses_paid_read(self):
        self.assertEqual(app_rules.decide("APOLLO_SEARCH_PEOPLE", {"apollo": "read"}, CAT, read_only=_YES), ("refuse", "apollo"))

    def test_calls_in(self):
        args = {"tools": [{"tool_slug": "GMAIL_FETCH_EMAILS", "arguments": {"q": "x"}}],
                "sync_response_to_workbench": True, "thought": "t"}
        self.assertEqual(app_rules.calls_in("COMPOSIO_MULTI_EXECUTE_TOOL", args),
                         [{"tool_slug": "GMAIL_FETCH_EMAILS", "arguments": {"q": "x"}}])

    def test_calls_in_rejects_malformed(self):
        for bad in ({}, {"tools": "x"}, {"tools": []}, {"tools": [{"arguments": {}}]},
                    {"tools": [{"tool_slug": ""}]}, {"tools": [1]}, None,
                    {"tools": [{"tool_slug": "A_B"}] * 51}):
            self.assertIsNone(app_rules.calls_in("COMPOSIO_MULTI_EXECUTE_TOOL", bad), bad)
        self.assertIsNone(app_rules.calls_in("COMPOSIO_REMOTE_WORKBENCH", {"code": "x"}))


if __name__ == "__main__":
    unittest.main()
