"""Deliverables: what a teammate saved becomes a card; the Files list replaces tabs."""
import io
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import dashboard.deliverables as deliverables
import dashboard.server as server


def touch(path, text="x"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    later = time.time() + 5
    os.utime(path, (later, later))


class Describe(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.p = self.root / "projects" / "acme"
        self.p.mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def test_kinds_and_links(self):
        touch(self.p / "strategy" / "memos" / "positioning-v2.json", json.dumps({"status": "proposed"}))
        touch(self.p / "strategy" / "experiments" / "landing-test.json", json.dumps({"assumption": "SMBs pay"}))
        touch(self.p / "strategy" / "intake.md")
        touch(self.p / "products" / "acme-app" / "roadmap.md")
        touch(self.p / "profiles" / "profile-a" / "content" / "plan-2026-11.json", json.dumps({"posts": [{}, {}]}))
        touch(self.p / "team" / "cmo.md")  # bot memory: not a deliverable
        items = {i["key"]: i for i in deliverables.list_files(self.root, "acme", [], [])}
        self.assertEqual(items["memo:positioning:2"]["link"], "#/project/acme/pricing")
        self.assertEqual(items["memo:positioning:2"]["label"], "Positioning memo v2")
        self.assertEqual(items["experiment:landing-test"]["label"], "Experiment: SMBs pay")
        self.assertEqual(items["doc:intake"]["link"], "#/project/acme/validation")
        self.assertIn("roadmap:acme-app", items)
        self.assertEqual(items["content:profile-a"]["label"], "Content plan: Profile a (2 posts)")
        self.assertEqual(items["content:profile-a"]["link"], "#/content")
        self.assertEqual(items["content:profile-a"]["profile"], "profile-a")
        self.assertFalse(any(k.startswith("team") for k in items))

    def test_changed_only_reports_this_turn_and_collapses_briefs(self):
        touch(self.p / "strategy" / "intake.md")
        before = deliverables.snapshot(self.root, "acme")
        touch(self.p / "strategy" / "memos" / "pricing-v1.json", "{}")
        for pid in ("p1", "p2", "p3"):
            touch(self.p / "profiles" / "profile-a" / "content" / "briefs" / f"{pid}.json", "{}")
        got = deliverables.changed(self.root, "acme", before, [], [])
        labels = sorted(i["label"] for i in got)
        self.assertEqual(labels, ["3 post briefs: Profile a", "Pricing memo v1"])
        card = next(i for i in got if i["kind"] == "brief")
        self.assertEqual(card["link"], "#/content")
        self.assertEqual(card["profile"], "profile-a")

    def test_single_post_brief_opens_its_row_in_the_content_view(self):
        before = deliverables.snapshot(self.root, "acme")
        touch(self.p / "profiles" / "profile-a" / "content" / "briefs" / "p1.json", "{}")
        (card,) = deliverables.changed(self.root, "acme", before, [], [])
        self.assertEqual(card["link"], "#/content")
        self.assertEqual(card["post"], "p1")
        self.assertEqual(card["profile"], "profile-a")

    def test_a_changed_canvas_is_a_canvas_card(self):
        touch(self.p / "canvases" / "cv2.json", json.dumps({"title": "Other", "blocks": []}))
        before = deliverables.snapshot(self.root, "acme")
        touch(self.p / "canvases" / "cv1.json", json.dumps({"title": "Launch plan", "blocks": []}))
        (card,) = deliverables.changed(self.root, "acme", before, [], [])
        self.assertEqual((card["kind"], card["key"], card["label"], card["link"]),
                         ("canvas", "canvas:cv1", "Launch plan", "#/canvas/cv1"))

    def test_a_changed_list_is_the_card_of_the_first_canvas_embedding_it(self):
        touch(self.p / "canvases" / "cv1.json", json.dumps({"title": "Notes", "blocks": [{"type": "text"}]}))
        touch(self.p / "canvases" / "cv2.json", json.dumps({"title": "Pipeline", "blocks": [
            {"id": "bk1", "type": "list", "list": "ls1"}]}))
        touch(self.p / "canvases" / "cv3.json", json.dumps({"title": "Again", "blocks": [
            {"id": "bk1", "type": "list", "list": "ls1"}]}))
        before = deliverables.snapshot(self.root, "acme")
        touch(self.p / "lists" / "ls1.json", json.dumps({"title": "Leads", "rows": []}))
        (card,) = deliverables.changed(self.root, "acme", before, [], [])
        self.assertEqual((card["key"], card["label"], card["link"]), ("canvas:cv2", "Pipeline", "#/canvas/cv2"))

    def test_shared_thread_sees_only_its_rooms_canvases(self):  # M3
        touch(self.p / "canvases" / "cv1.json", json.dumps({"title": "Hidden", "blocks": [
            {"id": "bk1", "type": "list", "list": "ls1"}]}))
        touch(self.p / "canvases" / "cv2.json", json.dumps({"title": "Shown", "blocks": [
            {"id": "bk1", "type": "list", "list": "ls1"}]}))
        before = deliverables.snapshot(self.root, "acme")
        touch(self.p / "canvases" / "cv1.json", json.dumps({"title": "Hidden 2", "blocks": []}))
        touch(self.p / "canvases" / "cv3.json", json.dumps({"title": "Also hidden", "blocks": []}))
        touch(self.p / "lists" / "ls1.json", json.dumps({"title": "Leads", "rows": []}))
        got = deliverables.changed(self.root, "acme", before, [], [], canvases_seen={"cv2"})
        self.assertEqual([(c["key"], c["label"]) for c in got], [("canvas:cv2", "Shown")])
        self.assertEqual(deliverables.changed(self.root, "acme", before, [], [], canvases_seen=set()), [])
        self.assertEqual(len(deliverables.changed(self.root, "acme", before, [], [])), 3)  # the owner: all

    def test_canvas_side_files_and_orphan_lists_are_no_cards(self):
        before = deliverables.snapshot(self.root, "acme")
        touch(self.p / "lists" / "ls9.json", json.dumps({"title": "Alone", "rows": []}))
        touch(self.p / "canvases" / "history" / "cv1.jsonl", "{}")
        touch(self.p / "canvases" / ".trash" / "cv4.json", "{}")
        touch(self.p / "canvases" / "templates" / "tp1.json", "{}")
        touch(self.p / "canvases" / "pins.json", "{}")
        touch(self.p / "canvases" / "cv1.json.lock", "")
        self.assertEqual(deliverables.changed(self.root, "acme", before, [], []), [])
        self.assertEqual(deliverables.list_files(self.root, "acme", [], []), [])


class TurnCards(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "projects" / "acme").mkdir(parents=True)
        self.tree = [{"slug": "acme", "name": "Acme", "kind": "venture", "type": "project",
                      "folders": [], "profiles": [], "products": []}]
        self._chats = server.chat_store.CHATS_DIR
        server.chat_store.CHATS_DIR = self.root / "chats"

    def tearDown(self):
        server.chat_store.CHATS_DIR = self._chats
        self.tmp.cleanup()

    def test_saved_file_becomes_a_card_on_the_reply(self):
        root = self.root

        class S:
            _started = True
            _turn_count = 1
            _last_skill = None
            _skill_explicit = False
            _last_bot = None
            session_id = "22222222-0000-0000-0000-000000000000"

            def is_fresh(self):
                return False

            def note_skill(self, *a, **k):
                pass

            def note_bot(self, *a):
                pass

            def ask(self, text, **kw):
                touch(root / "projects" / "acme" / "strategy" / "memos" / "positioning-v1.json",
                      json.dumps({"status": "proposed"}))
                yield ("delta", "Positioning memo v1 saved, 2 options inside.")
                yield ("done", {})

        sink = io.BytesIO()
        h = server.Handler.__new__(server.Handler)
        h.wfile = sink
        h.send_response = h.send_header = h.end_headers = lambda *a, **k: None
        with mock.patch.object(server, "ROOT", self.root), \
                mock.patch.object(server.db, "tree", return_value=self.tree), \
                mock.patch.object(server.db, "posts", return_value=[]), \
                mock.patch.object(server, "get_chat_session", return_value=S()):
            h._handle_ask({"messages": [{"role": "user", "content": "@cmo positioning, yes save"}],
                           "room_id": "acme/general", "chat_id": S.session_id})
        out = sink.getvalue().decode()
        self.assertIn('"deliverables"', out)
        msg = server.chat_store.load(S.session_id)["messages"][-1]
        self.assertEqual(msg["deliverables"][0]["label"], "Positioning memo v1")


if __name__ == "__main__":
    unittest.main()
