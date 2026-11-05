"""Workspaces + rooms: per-workspace channels/DMs, routing, isolation, shared calendar."""
import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import index
from core import bots, rooms
import dashboard.db as db
import dashboard.fileops as fileops
import dashboard.osctl as osctl
import dashboard.server as server
from tests.test_index_projects import write

VENTURE = {"slug": "acme", "kind": "venture", "profiles": [{"slug": "profile-a"}],
           "local_folder": "/tmp/acme-app"}


class Rooms(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "projects" / "acme").mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def team(self, ws="acme"):
        return bots.team(self.root, ws, VENTURE if ws == "acme" else None)

    def ids(self, ws="acme"):
        return [r["id"] for r in rooms.list_rooms(self.team(ws), ws, self.root)]

    def test_rooms_follow_the_workspace_team(self):
        self.assertEqual(self.ids()[0], "acme/general")
        self.assertIn("acme/dm-cto", self.ids())
        self.assertEqual(self.ids("hq"), ["hq/general", "hq/dm-cos"])

    def test_topic_crud_is_per_workspace(self):
        rid = rooms.save_topic("acme", "launch", ["gtm", "social"], "gtm", self.root)
        self.assertEqual(rid, "acme/t-launch")
        self.assertNotIn("hq/t-launch", self.ids("hq"))
        room = rooms.get_room(rid, self.team(), self.root)
        self.assertEqual((room["lead"], room["members"]), ("gtm", ["gtm", "social"]))
        self.assertIsNone(rooms.get_room(rid, self.team("hq"), self.root))
        rooms.save_topic("acme", "go-live", ["cmo"], "cos", self.root, replace="launch")
        self.assertEqual(rooms.get_room("acme/t-go-live", self.team(), self.root)["members"], ["cos", "cmo"])
        rooms.delete_topic("acme", "go-live", self.root)
        self.assertNotIn("acme/t-go-live", self.ids())

    def test_topic_validation(self):
        for name, lead in (("Bad Name", "cos"), ("general", "cos"), ("x", "nobody")):
            with self.assertRaises(ValueError):
                rooms.save_topic("acme", name, [], lead, self.root)
        rooms.save_topic("acme", "x", [], "cos", self.root)
        with self.assertRaises(ValueError):
            rooms.save_topic("acme", "x", [], "cos", self.root)
        with self.assertRaises(ValueError):
            rooms.save_topic("missing", "x", [], "cos", self.root)

    def test_switched_off_lead_resolves_to_manager(self):
        rooms.save_topic("acme", "posts", ["social"], "social", self.root)
        bots.set_active("social", False, self.root, "acme", VENTURE)
        self.assertEqual(rooms.get_room("acme/t-posts", self.team(), self.root)["lead"], "cmo")
        self.assertNotIn("acme/dm-social", self.ids())

    def test_threads_find_a_room(self):
        ids, spaces = set(self.ids()), {"hq", "acme"}
        self.assertEqual(rooms.room_of_chat({"room": "acme/dm-cmo"}, ids, spaces), "acme/dm-cmo")
        self.assertEqual(rooms.room_of_chat({"room": "acme/t-gone"}, ids, spaces), "acme/general")
        self.assertEqual(rooms.room_of_chat({"room": "p-acme"}, ids, spaces), "acme/general")
        self.assertEqual(rooms.room_of_chat({"scope": ["acme", ""]}, ids, spaces), "acme/general")
        self.assertEqual(rooms.room_of_chat({"room": "hq"}, ids, spaces), "hq/general")


class WorkspaceTurns(unittest.TestCase):
    """A chat turn inside a company is bound to it and walled off from the rest."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        for slug in ("acme", "demo"):
            (self.root / "projects" / slug).mkdir(parents=True)
        self.tree = [dict(VENTURE, type="project", name="Acme", products=[]),
                     {"slug": "demo", "name": "Demo", "kind": "brand", "type": "project",
                      "local_folder": None, "profiles": [], "products": []}]

    def tearDown(self):
        self.tmp.cleanup()

    def ask(self, room_id, msg="what next?", last_bot=None):
        sink, seen = io.BytesIO(), {}
        h = server.Handler.__new__(server.Handler)
        h.wfile = sink
        h.send_response = h.send_header = h.end_headers = lambda *a, **k: None

        class S:
            _started = False
            _turn_count = 0
            _last_skill = None
            _skill_explicit = False
            _last_bot = None
            session_id = None

            def is_fresh(self):
                return True

            def note_skill(self, *a, **k):
                pass

            def note_bot(self, bot_id):
                seen["bot"] = bot_id

            def ask(self, text, **kw):
                seen["text"] = text
                yield ("done", {})

        sess = S()
        sess._last_bot = last_bot

        def fake_session(scope, *a, **k):
            seen["scope"] = scope
            seen["sess"] = sess
            return sess

        with mock.patch.object(server, "ROOT", self.root), \
                mock.patch.object(server.db, "tree", return_value=self.tree), \
                mock.patch.object(server, "get_chat_session", side_effect=fake_session), \
                mock.patch.object(server, "state_snapshot",
                                  side_effect=lambda projects, only=None: f"SNAPSHOT only={only}"):
            h._handle_ask({"messages": [{"role": "user", "content": msg}], "room_id": room_id})
        return seen

    def test_company_turn_is_bound_and_isolated(self):
        seen = self.ask("acme/general")
        self.assertEqual(seen["scope"][0], "acme")
        self.assertEqual(seen["sess"].workspace, "acme")
        denied = seen["sess"].deny_dirs
        self.assertIn(str(self.root / "projects" / "demo"), denied)
        self.assertNotIn(str(self.root / "projects" / "acme"), denied)
        self.assertIn(str(self.root / "chats"), denied)
        self.assertIn("SNAPSHOT only=acme", seen["text"])

    def test_other_companies_linked_folders_are_denied(self):
        self.tree[1]["local_folder"] = "/tmp/demo-app"
        denied = self.ask("acme/general")["sess"].deny_dirs
        self.assertIn("/tmp/demo-app", denied)

    def test_hq_sees_every_company(self):
        seen = self.ask("hq/general")
        self.assertIsNone(seen["scope"])
        self.assertEqual(seen["sess"].deny_dirs, ())
        self.assertIn("SNAPSHOT only=None", seen["text"])

    def test_brand_workspace_has_brand_team(self):
        seen = self.ask("demo/general", "@product what is on the roadmap?")
        self.assertEqual(seen["bot"], "cos")  # Head of Product is off in a brand: CoS covers

    def test_topic_lead_and_member_roster(self):
        rooms.save_topic("acme", "launch", ["gtm", "social"], "gtm", self.root)
        seen = self.ask("acme/t-launch")
        self.assertEqual(seen["bot"], "gtm")
        self.assertIn("@social", seen["text"])
        self.assertNotIn("@cmo", seen["text"])

    def test_dm_always_answers_as_its_bot(self):
        self.assertEqual(self.ask("acme/dm-cmo", "and pricing?", last_bot="research")["bot"], "cmo")


class SharedCalendar(unittest.TestCase):
    """One calendar for the CEO: timed activities never overlap across workspaces."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        for slug, kind in (("acme", "venture"), ("demo", "brand")):
            write(self.root / "projects" / slug / "project.md", f"---\nname: {slug}\nkind: {kind}\n---\n")
        write(self.root / "projects" / "demo" / "profiles" / "profile-b" / "profile.md",
              "---\nname: Profile B\nproject: demo\n---\n")
        self._saved = (fileops.ROOT, db.DB_PATH)
        fileops.ROOT = self.root
        db.DB_PATH = self.root / "database" / "data" / "os.db"
        index.build(self.root)

    def tearDown(self):
        fileops.ROOT, db.DB_PATH = self._saved
        self.tmp.cleanup()

    def osctl(self, ws, *argv):
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"MYOS_WORKSPACE": ws}), contextlib.redirect_stdout(out):
            osctl.main(list(argv))
        return json.loads(out.getvalue())

    def test_bots_cannot_double_book_across_workspaces(self):
        r = self.osctl("demo", "create-activity", "--entity", "demo", "--title", "Shoot",
                       "--date", "2026-11-03", "--time", "10:00", "--time-end", "11:30")
        self.assertTrue(r["ok"], r)
        r = self.osctl("acme", "create-activity", "--entity", "acme", "--title", "Call",
                       "--date", "2026-11-03", "--time", "11:00")
        self.assertFalse(r["ok"])
        self.assertIn("overlaps", r["error"])
        self.assertNotIn("Shoot", r["error"])  # other company's work stays private
        self.assertNotIn("demo", r["error"])
        r = self.osctl("acme", "create-activity", "--entity", "acme", "--title", "Call",
                       "--date", "2026-11-03", "--time", "11:30")
        self.assertTrue(r["ok"], r)

    def test_user_booking_overlap_is_allowed_but_reported(self):
        fileops.create_activity({"entity": "demo", "title": "Shoot", "date": "2026-11-03",
                                 "time": "10:00", "time_end": "11:00"})
        out = fileops.create_activity({"entity": "acme", "title": "Call", "date": "2026-11-03",
                                       "time": "10:30"}, allow_overlap=True)
        self.assertEqual(out["overlaps"][0]["entity"], "demo")
        row = [r for r in db.timeline() if r["title"] == "Call"][0]
        self.assertEqual((row["time"], row["time_end"]), ("10:30", "11:30"))

    def test_time_validation(self):
        for f in ({"time": "25:00"}, {"time": "10:00", "time_end": "09:00"},
                  {"time": "10:00", "date_end": "2026-11-04"}, {"time_end": "10:00"}):
            with self.assertRaises(fileops.ActionError):
                fileops.create_activity({"entity": "acme", "title": "x", "date": "2026-11-03", **f})

    def test_get_busy_hides_other_workspaces(self):
        self.osctl("demo", "create-activity", "--entity", "demo", "--title", "Shoot",
                   "--date", "2026-11-03", "--time", "10:00")
        busy = self.osctl("acme", "get-busy", "--from", "2026-11-01", "--to", "2026-11-30")["busy"]
        self.assertEqual(busy[0]["title"], "busy (another workspace)")
        self.assertIsNone(busy[0]["entity"])
        self.assertEqual(self.osctl("hq", "get-busy", "--from", "2026-11-03")["busy"][0]["title"], "Shoot")

    def test_snapshot_lists_only_the_workspace(self):
        # server imports db as a top-level module: point that copy at the test index
        # too, or it reads the real os.db.
        with mock.patch.object(server, "ROOT", self.root), \
                mock.patch.object(server.db, "DB_PATH", db.DB_PATH):
            snap = server.state_snapshot(db.tree(), only="acme")
        self.assertIn("### acme", snap)
        self.assertNotIn("### demo", snap)
        self.assertNotIn("profile-b", snap)

    def test_osctl_refuses_other_workspaces(self):
        for argv in (("get-project", "--slug", "demo"),
                     ("get-posts", "--profile", "profile-b"),
                     ("create-activity", "--entity", "demo", "--title", "x"),
                     ("read-file", "--path", "projects/demo/project.md"),
                     ("read-file", "--path", "portfolio/activities.md"),
                     ("read-file", "--path", "private/notes.md"),
                     ("create-project", "--slug", "new")):
            r = self.osctl("acme", *argv)
            self.assertFalse(r["ok"], argv)
        self.assertTrue(self.osctl("acme", "get-project", "--slug", "acme")["ok"])
        self.assertNotIn("demo", self.osctl("acme", "tree")["tree"])
        self.assertIn("demo", self.osctl("hq", "tree")["tree"])

    def test_read_file_guard_holds_for_absolute_and_odd_case_paths(self):
        for path in (str(self.root / "projects" / "demo" / "project.md"),
                     str(self.root / "portfolio" / "activities.md"),
                     "Projects/demo/project.md",
                     "projects/acme/../demo/project.md"):
            r = self.osctl("acme", "read-file", "--path", path)
            self.assertFalse(r["ok"], path)
            self.assertIn("workspace", r["error"], path)
        r = self.osctl("acme", "read-file", "--path", str(self.root / "projects" / "acme" / "project.md"))
        self.assertTrue(r["ok"], r)

    def test_get_busy_shows_bookings_on_your_own_profiles(self):
        self.osctl("demo", "create-activity", "--entity", "profile-b", "--title", "Shoot",
                   "--date", "2026-11-03", "--time", "10:00")
        busy = self.osctl("demo", "get-busy", "--from", "2026-11-03")["busy"]
        self.assertEqual((busy[0]["title"], busy[0]["entity"]), ("Shoot", "profile-b"))

    def test_no_bot_links_a_local_folder_not_even_hq(self):
        # A linked folder becomes --add-dir on that company's later turns: any bot
        # (an HQ one steered by a web page too) could point it at ~/.config.
        app = self.root / "apps" / "acme-app"
        app.mkdir(parents=True)
        for ws in ("acme", "hq"):
            r = self.osctl(ws, "update-project", "--slug", "acme", "--local-folder", str(app))
            self.assertFalse(r["ok"], ws)
            self.assertIn("dashboard", r["error"])
        r = self.osctl("hq", "create-project", "--slug", "newco", "--local-folder", str(app))
        self.assertFalse(r["ok"])
        self.assertTrue(self.osctl("acme", "update-project", "--slug", "acme", "--status", "active")["ok"])

    def test_research_signal_is_off_once_a_thread_read_an_app(self):
        # Its query goes to public search sites: the "read mail, leak it via a
        # search" path that web-off closes for WebSearch.
        with mock.patch.dict(os.environ, {"MYOS_APPS_USED": "1"}), \
                mock.patch.object(fileops, "research_signal") as research:
            r = self.osctl("hq", "research-signal", "--query", "x")
        self.assertFalse(r["ok"])
        self.assertIn("connected app", r["error"])
        research.assert_not_called()

    def test_owner_map_is_built_once_per_call(self):
        with mock.patch.object(osctl, "build_id_registry", wraps=osctl.build_id_registry) as reg:
            self.osctl("demo", "get-posts", "--profile", "profile-b")
        self.assertEqual(reg.call_count, 1)


if __name__ == "__main__":
    unittest.main()
