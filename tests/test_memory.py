"""Bot memory: typed entries, the 400 cap, recall over a derived index, the card block."""
import contextlib
import io
import json
import os
import sqlite3
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

import index
from core import bots, memory
from dashboard import db, fileops, osctl
import dashboard.server as server


def make_root(tmp):
    root = Path(tmp)
    for slug in ("acme", "demo"):
        (root / "projects" / slug).mkdir(parents=True, exist_ok=True)
    return root


class TempRoot(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = make_root(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()


class TypedEntries(TempRoot):
    def test_type_is_written_and_defaults_to_fact(self):
        memory.remember("cmo", "B2B only", "acme", self.root, today=date(2026, 11, 11), type="decision")
        memory.remember("cmo", "Launch in March", "acme", self.root, today=date(2026, 11, 11))
        lines = memory.read_memory("cmo", "acme", self.root, tail=0).splitlines()
        self.assertEqual(lines, ["- 2026-11-11 [decision] B2B only", "- 2026-11-11 [fact] Launch in March"])
        with self.assertRaises(ValueError):
            memory.remember("cmo", "x", "acme", self.root, type="todo")

    def test_untyped_lines_read_as_facts(self):
        e = memory.parse("- 2026-10-01 premium tier only")
        self.assertEqual((e.date, e.type, e.text), ("2026-10-01", "fact", "premium tier only"))
        self.assertIsNone(memory.parse("   "))

    def test_odd_lines_read_as_facts(self):
        path = memory.memory_path("cmo", "acme", self.root)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes("- no date here\r\n\r\n- 2026-10-01 [todo] tagged\r\n- 2026-10-02 café ☕\r\n".encode())
        got = [(e.type, e.text) for e in memory.entries("cmo", "acme", self.root)]
        self.assertEqual(got, [("fact", "no date here"), ("fact", "[todo] tagged"), ("fact", "café ☕")])
        memory.remember("cmo", "new", "acme", self.root)
        text = path.read_text(encoding="utf-8")
        self.assertIn("- 2026-10-02 café ☕\n", text)
        self.assertIn("- 2026-10-01 [todo] tagged\n", text)
        self.assertIn("- no date here\n", text)

    def test_cap_drops_oldest_fact_never_pinned(self):
        memory.remember("cos", "keep me", None, self.root, type="decision")
        for i in range(memory.MAX_ENTRIES + 5):
            memory.remember("cos", f"fact {i}", None, self.root)
        got = memory.entries("cos", None, self.root)
        self.assertEqual(len(got), memory.MAX_ENTRIES)
        self.assertEqual(got[0].text, "keep me")
        self.assertEqual(got[-1].text, f"fact {memory.MAX_ENTRIES + 4}")
        self.assertNotIn("fact 0", [e.text for e in got])

    def test_full_of_pinned_refuses(self):
        for i in range(memory.MAX_ENTRIES):
            memory.remember("cos", f"rule {i}", None, self.root, type="preference")
        with self.assertRaises(ValueError) as ctx:
            memory.remember("cos", "one more", None, self.root)
        self.assertIn("forget", str(ctx.exception))
        self.assertEqual(len(memory.entries("cos", None, self.root)), memory.MAX_ENTRIES)

    def test_bots_names_still_work(self):
        self.assertIs(bots.remember, memory.remember)
        self.assertIs(bots.forget, memory.forget)
        self.assertEqual(bots.MEMORY_MAX_LINES, memory.MAX_ENTRIES)

    def test_forget_removes_a_typed_line(self):
        memory.remember("cmo", "B2B only", "acme", self.root, today=date(2026, 11, 11), type="decision")
        self.assertTrue(memory.forget("cmo", "- 2026-11-11 [decision] B2B only", "acme", self.root))
        self.assertEqual(memory.entries("cmo", "acme", self.root), [])


class Recall(TempRoot):
    def test_old_fact_beyond_the_tail_is_found(self):
        memory.remember("cmo", "Pricing anchor is 49 euros per seat", "acme", self.root)
        for i in range(60):
            memory.remember("cmo", f"weekly note {i}", "acme", self.root)
        self.assertNotIn("49 euros", memory.read_memory("cmo", "acme", self.root))  # outside the old 20-line tail
        hits = memory.recall("cmo", "what is our pricing anchor", "acme", self.root)
        self.assertIn("49 euros", hits[0])

    def test_scopes_and_workspaces_never_mix(self):
        memory.remember("cmo", "PRIVATE pricing", "acme", self.root)
        memory.remember("cmo", "TEAM pricing", "acme", self.root, shared=True)
        memory.remember("cmo", "OTHER pricing", "demo", self.root)
        memory.remember("cmo", "HQ pricing", None, self.root)
        team = memory.recall("cmo", "pricing", "acme", self.root, shared=True)
        self.assertEqual(len(team), 1)
        self.assertIn("TEAM pricing", team[0])
        private = " ".join(memory.recall("cmo", "pricing", "acme", self.root))
        self.assertIn("PRIVATE", private)
        for other in ("TEAM", "OTHER", "HQ"):
            self.assertNotIn(other, private)
        self.assertEqual(len(memory.recall("cmo", "pricing", None, self.root)), 1)

    def test_output_capped_at_800_chars(self):
        for i in range(20):
            memory.remember("cmo", f"pricing detail {i} " + "x" * 200, "acme", self.root)
        hits = memory.recall("cmo", "pricing", "acme", self.root, limit=20)
        self.assertLessEqual(sum(len(h) for h in hits), memory.RECALL_CHARS)
        self.assertTrue(hits)

    def test_limit_is_clamped(self):
        for i in range(30):
            memory.remember("cmo", f"pricing {i}", "acme", self.root)
        self.assertEqual(len(memory.recall("cmo", "pricing", "acme", self.root, limit=0)), 1)
        self.assertEqual(len(memory.recall("cmo", "pricing", "acme", self.root, limit=99)), 20)

    def test_query_syntax_is_inert(self):
        memory.remember("cmo", "pricing anchor", "acme", self.root)
        for q in ('"', 'pric*', '-pricing', 'text:pricing', 'NEAR(a b)', 'AND OR', '(', '', '   ', None):
            self.assertIsInstance(memory.recall("cmo", q, "acme", self.root), list, q)
        self.assertTrue(memory.recall("cmo", 'text:"pricing"', "acme", self.root))

    def test_unknown_scope_is_empty(self):
        self.assertEqual(memory.recall("cmo", "pricing", "missing", self.root), [])
        self.assertEqual(memory.recall("../x", "pricing", "acme", self.root), [])

    def test_hand_edit_is_seen_without_reindex(self):
        memory.remember("cmo", "first", "acme", self.root)
        path = memory.memory_path("cmo", "acme", self.root)
        path.write_text(path.read_text() + "- 2026-11-01 [fact] hand added zebra\n")
        self.assertTrue(memory.recall("cmo", "zebra", "acme", self.root))

    def test_forget_leaves_the_index(self):
        memory.remember("cmo", "pricing anchor", "acme", self.root, today=date(2026, 11, 11))
        memory.forget("cmo", "- 2026-11-11 [fact] pricing anchor", "acme", self.root)
        self.assertEqual(memory.recall("cmo", "pricing", "acme", self.root), [])

    def test_corrupt_index_is_rebuilt(self):
        memory.remember("cmo", "pricing anchor", "acme", self.root)
        (self.root / memory.INDEX_RELPATH).write_bytes(b"not a database")
        self.assertTrue(memory.recall("cmo", "pricing", "acme", self.root))

    def test_deleted_index_is_rebuilt(self):
        memory.remember("cmo", "pricing anchor", "acme", self.root)
        (self.root / memory.INDEX_RELPATH).unlink()
        self.assertTrue(memory.recall("cmo", "pricing", "acme", self.root))

    def test_scan_fallback_without_fts5(self):
        memory.remember("cmo", "pricing anchor", "acme", self.root)
        memory.remember("cmo", "unrelated", "acme", self.root)
        with mock.patch.object(memory, "HAS_FTS5", False):
            self.assertEqual(len(memory.recall("cmo", "Pricing?", "acme", self.root)), 1)
            self.assertIn("pricing anchor", memory.recall("cmo", "Pricing?", "acme", self.root)[0])

    def test_busy_index_is_never_deleted(self):
        memory.remember("cmo", "pricing anchor", "acme", self.root)
        db_file = self.root / memory.INDEX_RELPATH
        with mock.patch.object(memory, "_connect", side_effect=sqlite3.OperationalError("database is locked")):
            memory.sync_file(memory.memory_path("cmo", "acme", self.root), self.root, force=True)
            self.assertIn("pricing anchor", memory.recall("cmo", "pricing", "acme", self.root)[0])  # scan
        self.assertTrue(db_file.exists())
        self.assertTrue(memory.recall("cmo", "pricing", "acme", self.root))

    def test_index_py_survives_a_memory_index_error(self):
        with mock.patch.object(memory, "rebuild_index", side_effect=sqlite3.OperationalError("database is locked")), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            index.build(self.root)
        self.assertTrue((self.root / "database" / "data" / "os.db").exists())
        self.assertIn("memory entries", out.getvalue())

    def test_index_py_rebuilds_memory(self):
        memory.remember("cmo", "pricing anchor", "acme", self.root)
        memory.remember("cmo", "team note", "acme", self.root, shared=True)
        memory.remember("cos", "hq note", None, self.root)
        (self.root / memory.INDEX_RELPATH).unlink()
        self.assertEqual(memory.rebuild_index(self.root), 3)


class Card(TempRoot):
    def fixture(self):
        for i in range(12):
            memory.remember("cmo", f"rule {i}", "acme", self.root, type="preference")
        memory.remember("cmo", "Pricing anchor is 49 euros", "acme", self.root)
        for i in range(50):
            memory.remember("cmo", f"weekly note {i} about the newsletter and the agency", "acme", self.root)

    def test_pinned_newest_first_then_hits(self):
        self.fixture()
        lines = memory.card_block("cmo", "acme", self.root, query="what is the pricing anchor?").splitlines()
        self.assertEqual(lines[0].split("] ", 1)[1], "rule 11")
        self.assertEqual(sum("[preference]" in ln for ln in lines), memory.PINNED_IN_CARD)
        self.assertIn("49 euros", "\n".join(lines))
        self.assertLessEqual(len(lines), memory.PINNED_IN_CARD + memory.HITS_IN_CARD)

    def test_hits_never_repeat_a_pinned_line(self):
        memory.remember("cmo", "pricing is B2B only", "acme", self.root, type="decision")
        lines = memory.card_block("cmo", "acme", self.root, query="pricing").splitlines()
        self.assertEqual(len(lines), 1)

    def test_empty_when_nothing_pinned_or_matching(self):
        memory.remember("cmo", "weekly note", "acme", self.root)
        self.assertEqual(memory.card_block("cmo", "acme", self.root, query="zebra"), "")
        self.assertEqual(memory.card_block("cmo", "missing", self.root, query="zebra"), "")

    def test_block_is_smaller_than_the_old_tail(self):
        self.fixture()
        old = memory.read_memory("cmo", "acme", self.root, tail=20)
        new = memory.card_block("cmo", "acme", self.root, query="what is the pricing anchor?")
        self.assertLess(len(new), len(old))

    def test_bot_card_uses_the_message_and_names_recall(self):
        self.fixture()
        team = bots.team(self.root, "acme", {"slug": "acme"})
        with mock.patch.object(server, "ROOT", self.root):
            text = server._bot_block(team, "cmo", None, "acme", fresh=True, query="pricing anchor?")
        self.assertIn("49 euros", text)
        self.assertNotIn("weekly note 49", text)
        self.assertIn("recall --bot cmo", text)
        self.assertIn("--type decision|preference|fact", text)

    def test_shared_card_reads_team_memory_only(self):
        self.fixture()
        memory.remember("cmo", "team pricing rule", "acme", self.root, shared=True, type="decision")
        team = bots.team(self.root, "acme", {"slug": "acme"})
        with mock.patch.object(server, "ROOT", self.root):
            text = server._bot_block(team, "cmo", None, "acme", fresh=True, shared=True, query="pricing")
        self.assertIn("team pricing rule", text)
        self.assertNotIn("49 euros", text)
        self.assertNotIn("remember --bot", text)

    def test_empty_card_still_names_recall(self):
        team = bots.team(self.root, "acme", {"slug": "acme"})
        with mock.patch.object(server, "ROOT", self.root):
            text = server._bot_block(team, "cmo", None, "acme", fresh=True, query="pricing")
        self.assertIn("nothing pinned or matching", text)
        self.assertIn("recall --bot cmo", text)


def run_osctl(*argv):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        osctl.main(list(argv))
    return json.loads(out.getvalue().strip().splitlines()[-1])


class OsctlType(TempRoot):
    def setUp(self):
        super().setUp()
        for target in (bots, memory):
            p = mock.patch.object(target, "ROOT", self.root)
            p.start()
            self.addCleanup(p.stop)
        env = mock.patch.dict(os.environ, {})
        env.start()
        self.addCleanup(env.stop)
        for k in ("LEOBOT_WORKSPACE", "LEOBOT_SHARED", "LEOBOT_READONLY"):
            os.environ.pop(k, None)

    def test_type_flag_is_written(self):
        r = run_osctl("remember", "--bot", "cmo", "--project", "acme", "--text", "x", "--type", "preference")
        self.assertTrue(r["ok"], r)
        self.assertIn("[preference] x", memory.memory_path("cmo", "acme", self.root).read_text())

    def test_unknown_type_is_rejected(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            osctl.main(["remember", "--bot", "cmo", "--project", "acme", "--text", "x", "--type", "todo"])


class OsctlRecall(TempRoot):
    def setUp(self):
        super().setUp()
        for p in [mock.patch.object(t, "ROOT", self.root) for t in (bots, memory, fileops)] + [
                mock.patch.object(db, "DB_PATH", self.root / "database" / "data" / "os.db")]:
            p.start()
            self.addCleanup(p.stop)
        with contextlib.redirect_stdout(io.StringIO()):
            index.build(self.root)  # the workspace guard reads the index
        memory.remember("cmo", "PRIVATE pricing", "acme", self.root)
        memory.remember("cmo", "TEAM pricing", "acme", self.root, shared=True)
        memory.remember("cmo", "OTHER pricing", "demo", self.root)
        memory.remember("cmo", "HQ pricing", None, self.root)

    def run_recall(self, env, *argv):
        out = io.StringIO()
        with mock.patch.dict(os.environ, env, clear=False), contextlib.redirect_stdout(out):
            for k in ("LEOBOT_WORKSPACE", "LEOBOT_SHARED", "LEOBOT_READONLY"):
                if k not in env:
                    os.environ.pop(k, None)
            osctl.main(["recall", "--bot", "cmo", *argv])
        return json.loads(out.getvalue().strip().splitlines()[-1])

    def texts(self, r):
        return [e.split("] ", 1)[1] for e in r["entries"]]

    def test_shared_thread_reaches_only_team_memory(self):
        r = self.run_recall({"LEOBOT_WORKSPACE": "acme", "LEOBOT_SHARED": "1"}, "--query", "pricing")
        self.assertEqual(self.texts(r), ["TEAM pricing"])

    def test_private_thread_reaches_only_private_memory(self):
        r = self.run_recall({"LEOBOT_WORKSPACE": "acme"}, "--query", "pricing")
        self.assertEqual(self.texts(r), ["PRIVATE pricing"])

    def test_hq_reaches_only_hq_memory(self):
        r = self.run_recall({"LEOBOT_WORKSPACE": "hq"}, "--query", "pricing")
        self.assertEqual(self.texts(r), ["HQ pricing"])

    def test_workspace_never_reaches_another(self):
        r = self.run_recall({"LEOBOT_WORKSPACE": "demo"}, "--query", "pricing")
        self.assertEqual(self.texts(r), ["OTHER pricing"])
        self.assertNotIn("PRIVATE", json.dumps(r))
        self.assertNotIn("HQ", json.dumps(r))

    def test_no_project_argument(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            self.run_recall({"LEOBOT_WORKSPACE": "demo"}, "--query", "pricing", "--project", "acme")

    def test_unknown_bot_is_an_error(self):
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"LEOBOT_WORKSPACE": "acme"}), contextlib.redirect_stdout(out):
            osctl.main(["recall", "--bot", "nobody", "--query", "pricing"])
        r = json.loads(out.getvalue().strip().splitlines()[-1])
        self.assertFalse(r["ok"])
        self.assertIn("unknown bot", r["error"])

    def test_allowed_when_consulted(self):
        r = self.run_recall({"LEOBOT_WORKSPACE": "acme", "LEOBOT_READONLY": "1"}, "--query", "pricing")
        self.assertTrue(r["ok"], r)
        self.assertEqual(self.texts(r), ["PRIVATE pricing"])


if __name__ == "__main__":
    unittest.main()
