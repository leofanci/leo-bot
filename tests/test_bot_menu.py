"""The bot card: a menu of the speaker's own skills, and who holds which app."""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from core import bots
import dashboard.server as server

HEADER = "Your skills (load one with `osctl get-skill --name <name>` when the task needs it):"


class SkillMenu(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for p in (mock.patch.object(server, "ROOT", self.root),
                  mock.patch.object(server.db, "tree", return_value=[])):
            p.start()
            self.addCleanup(p.stop)
        (self.root / "projects" / "acme").mkdir(parents=True)
        self.team = bots.set_active("cmo", True, self.root, "acme")

    def menu_lines(self, text):
        lines = text.splitlines()
        i = lines.index(HEADER)
        out = []
        for line in lines[i + 1:]:
            if not line.startswith("- "):
                break
            out.append(line)
        return out

    def test_fresh_card_lists_each_skill_once(self):
        text = server._bot_block(self.team, "cmo", None, "acme", fresh=True)
        self.assertIn(HEADER, text)
        got = [line[2:].split(":", 1)[0] for line in self.menu_lines(text)]
        self.assertEqual(got, self.team.skills_of("cmo"))

    def test_descriptions_are_one_short_line(self):
        text = server._bot_block(self.team, "cos", None, "acme", fresh=True)
        for line in self.menu_lines(text):
            name, _, desc = line[2:].partition(": ")
            self.assertLessEqual(len(desc), 100, line)
            self.assertNotIn("\n", desc)

    def test_cut_at_a_word_boundary(self):
        long = "word " * 40
        self.assertEqual(server._one_line(long, 100), ("word " * 19).strip() + "…")
        self.assertLessEqual(len(server._one_line(long, 100)), 100)
        self.assertEqual(server._one_line("short\nsecond", 100), "short second")

    def test_resumed_card_has_no_menu(self):
        session = mock.Mock(_last_bot="cmo")
        text = server._bot_block(self.team, "cmo", session, "acme")
        self.assertEqual(text, "(speaking as @cmo)")

    def test_bot_without_skills_gets_no_header(self):
        self.assertEqual(server._skill_menu(self.team, "nobody"), "")
        with mock.patch.object(self.team, "skills_of", return_value=[]):
            self.assertNotIn(HEADER, server._bot_block(self.team, "cmo", None, "acme", fresh=True))


if __name__ == "__main__":
    unittest.main()
