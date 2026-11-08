"""Team of bots: ownership, roll-up, routing, memory isolation."""
import collections
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from core import bots
import dashboard.server as server

SKILLS = {d.name for d in (Path(bots.ROOT) / "skills").iterdir() if (d / "SKILL.md").exists()}
# A venture with profiles and a linked app: its smart default switches every bot on.
VENTURE = {"slug": "acme", "kind": "venture", "profiles": [{"slug": "profile-a"}],
           "folders": [{"id": "f1", "path": "/tmp/acme-app", "name": "app", "access": "read"}]}


def venture_root(tmp):
    root = Path(tmp)
    (root / "projects" / "acme").mkdir(parents=True, exist_ok=True)
    return root


class Paths(unittest.TestCase):
    def test_bot_id_must_match_whole(self):
        with self.assertRaises(ValueError):
            bots.memory_path("cos\n")  # "$" alone would admit it


class Defs(unittest.TestCase):
    def test_every_skill_has_exactly_one_defining_bot(self):
        owners = collections.Counter(s for b in bots.load_defs().values() for s in b.skills)
        self.assertEqual(set(owners), SKILLS)
        self.assertEqual([s for s, n in owners.items() if n > 1], [])

    def test_default_skill_is_own_and_hierarchy_resolves(self):
        defs = bots.load_defs()
        self.assertIn(bots.ROOT_BOT, defs)
        for b in defs.values():
            self.assertIn(b.default_skill, b.skills, b.id)
            self.assertIn(b.reports_to, set(defs) | {bots.CEO}, b.id)


class Team(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = venture_root(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def team(self):
        return bots.team(self.root, "acme", VENTURE)

    def off(self, bot_id):
        bots.set_active(bot_id, False, self.root, "acme", VENTURE)

    def test_smart_defaults_by_project_type(self):
        on = lambda t: sorted(b.id for b in t.bots.values() if b.active)  # noqa: E731
        self.assertEqual(on(self.team()), sorted(bots.load_defs()))
        self.assertEqual(on(bots.team(self.root)), ["cos"])  # HQ: CEO office
        brand = {"slug": "acme", "kind": "brand", "profiles": [], "folders": []}
        self.assertEqual(on(bots.team(self.root, "acme", brand)), ["cmo", "cos", "social"])
        plain = {"slug": "acme", "kind": "venture", "profiles": [], "folders": []}
        self.assertEqual(on(bots.team(self.root, "acme", plain)),
                         ["cmo", "cos", "gtm", "product", "research"])

    def test_teams_are_per_workspace(self):
        (self.root / "projects" / "demo").mkdir()
        self.off("cmo")
        self.assertFalse(self.team().get("cmo").active)
        self.assertTrue(bots.team(self.root, "demo", dict(VENTURE, slug="demo")).get("cmo").active)
        with self.assertRaises(ValueError):
            bots.set_active("cmo", False, self.root, "missing", None)

    def test_switched_off_bot_rolls_up_to_manager(self):
        self.off("social")
        t = self.team()
        self.assertEqual(t.owner("content-plan"), "cmo")
        self.assertIn("social", t.get("cmo").covering)
        self.off("cmo")
        t = self.team()
        self.assertEqual(t.owner("content-plan"), "cos")
        self.assertEqual(t.owner("positioning"), "cos")

    def test_root_bot_cannot_be_switched_off(self):
        with self.assertRaises(ValueError):
            bots.set_active("cos", False, self.root)

    def test_mention_ignores_inactive_and_unknown(self):
        self.off("gtm")
        t = self.team()
        self.assertEqual(t.mentioned("hey @gtm then @cmo"), "cmo")
        self.assertIsNone(t.mentioned("ping x@cmo or @acme"))


class Memory(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        for slug in ("acme", "demo"):
            (self.root / "projects" / slug).mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def test_memory_is_per_project(self):
        bots.remember("cmo", "premium tier only", "acme", self.root)
        self.assertIn("premium tier only", bots.read_memory("cmo", "acme", self.root))
        self.assertEqual(bots.read_memory("cmo", "demo", self.root), "")
        self.assertEqual(bots.read_memory("cmo", None, self.root), "")

    def test_memory_rejects_bad_input(self):
        for bot, project in (("nobody", "acme"), ("cmo", "../x"), ("cmo", "missing")):
            with self.assertRaises(ValueError):
                bots.remember(bot, "x", project, self.root)
        with self.assertRaises(ValueError):
            bots.remember("cmo", "x" * 400, "acme", self.root)

    def test_memory_is_capped(self):
        for i in range(bots.MEMORY_MAX_LINES + 5):
            bots.remember("cos", f"fact {i}", None, self.root)
        lines = bots.read_memory("cos", None, self.root, tail=0).splitlines()
        self.assertEqual(len(lines), bots.MEMORY_MAX_LINES)
        self.assertTrue(lines[-1].endswith(f"fact {bots.MEMORY_MAX_LINES + 4}"))


class Routing(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.team = bots.team(venture_root(self.tmp.name), "acme", VENTURE)

    def tearDown(self):
        self.tmp.cleanup()

    def _sess(self, last_bot=None, last_skill=None, explicit=False):
        class S:
            pass
        s = S()
        s._last_bot, s._last_skill, s._skill_explicit = last_bot, last_skill, explicit
        return s

    def route(self, msg, **kw):
        return server._route_bot(self.team, msg, self._sess(**kw))

    def test_untagged_goes_to_cos_without_skill(self):
        self.assertEqual(self.route("what should I do today?"), ("cos", None, False, None))

    def test_untagged_follow_up_stays_with_last_bot(self):
        self.assertEqual(self.route("and the premium tier?", last_bot="cmo")[0], "cmo")

    def test_bot_tag_matches_own_skill(self):
        self.assertEqual(self.route("@cmo help with pricing"),
                         ("cmo", "pricing-strategy", True, None))

    def test_bot_tag_hands_off_to_owner(self):
        self.assertEqual(self.route("@cmo who are our competitors?"),
                         ("research", "competitor-scan", True, "cmo"))

    def test_bot_tag_write_without_match_uses_default_skill(self):
        self.assertEqual(self.route("@social draft three posts"),
                         ("social", "content-brief", True, None))

    def test_bot_tag_chat_stays_unskilled(self):
        self.assertEqual(self.route("@cmo thoughts on our name?"), ("cmo", None, False, None))

    def test_single_skill_bot_always_uses_it(self):
        self.assertEqual(self.route("@social draft three posts")[1], "content-brief")
        # CTO owns two skills: a change routes to folder-work, a plain question to none.
        self.assertEqual(self.route("@cto fix the login redirect")[1], "folder-work")
        self.assertIsNone(self.route("@cto how does login work")[1])

    def test_slash_skill_speaks_as_owner(self):
        self.assertEqual(self.route("/positioning for acme"),
                         ("cmo", "positioning", True, None))

    def test_continuation_speaks_as_skill_owner(self):
        r = self.route("yes save it", last_bot="cos", last_skill="launch-plan", explicit=True)
        self.assertEqual(r, ("gtm", None, False, None))

    def dm(self, msg, bot="social", **kw):
        return server._route_bot(self.team, msg, self._sess(**kw), default_bot=bot, dm=bot)

    def test_dm_foreign_slash_skill_stays_with_dm_bot(self):
        self.assertEqual(self.dm("/workspace should we add the word drama?"),
                         ("social", "workspace", True, None))

    def test_dm_untagged_write_is_addressed_to_dm_bot(self):
        self.assertEqual(self.dm("should we add the word drama?"),
                         ("social", "content-brief", True, None))

    def test_dm_untagged_chat_stays_unskilled(self):
        self.assertEqual(self.dm("thoughts on the hook?"), ("social", None, False, None))

    def test_dm_ignores_other_bots_skill_keywords(self):
        self.assertEqual(self.dm("who are our competitors?"), ("social", None, False, None))

    def test_dm_continuation_stays_with_dm_bot(self):
        r = self.dm("yes save it", last_bot="cos", last_skill="workspace", explicit=True)
        self.assertEqual(r[0], "social")

    def test_dm_other_bot_tag_does_not_take_over(self):
        self.assertEqual(self.dm("@cmo thoughts on the hook?")[0], "social")

    def test_handoff_emits_bot_event(self):
        sink = io.BytesIO()
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
            seen = {}

            def is_fresh(self):
                return True

            def note_skill(self, skill, explicit=False):
                pass

            def note_bot(self, bot_id):
                S.seen["bot"] = bot_id

            def ask(self, text, **kw):
                S.seen["text"] = text
                yield ("done", {})

        with mock.patch.object(server, "get_chat_session", return_value=S()), \
                mock.patch.object(server.bots, "team", return_value=self.team), \
                mock.patch.object(server.db, "tree", return_value=[]), \
                mock.patch.object(server, "state_snapshot", return_value=""):
            h._handle_ask({"messages": [{"role": "user", "content": "@cmo map competitors"}]})
        out = sink.getvalue().decode()
        self.assertIn('"handoff_from": "cmo"', out)
        self.assertEqual(S.seen["bot"], "research")
        self.assertIn("## Speaking as @research", S.seen["text"])
        self.assertIn("## Active skill: competitor-scan", S.seen["text"])


if __name__ == "__main__":
    unittest.main()
