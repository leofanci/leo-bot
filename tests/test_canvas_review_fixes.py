"""Regressions for the final whole-branch canvas review (core/canvases.py, core/canvas_store.py)."""
import unittest

from core import canvas_store as cs, canvases as cv
from core.canvases import Invalid, NeedsApproval, Refused, Stale
from tests.test_canvases import OWNER, P1, ROOM, Base, bot


def ver(t, fid):
    return cs.read(t.root, "demo", fid)["version"]


class TestBotVersions(Base):
    """I1: a bot's --version is checked, and a bot's rewrite or delete must carry one."""

    def test_stale_bot_writes_refused(self):
        c = self.canvas(OWNER)
        lid = self.lst(OWNER, c)
        r = self.d(OWNER, "add-row", id=lid, cells={"name": "a"})["row"]
        b = bot()
        seen = ver(self, lid)
        self.d(OWNER, "update-row", id=lid, row=r, cells={"notes": "owner"})
        for cmd, extra in (("update-row", {"row": r, "cells": {"notes": "bot"}}),
                           ("delete-row", {"row": r})):
            with self.assertRaises(Stale, msg=cmd):
                self.d(b, cmd, id=lid, version=seen, **extra)
        self.assertEqual(self.row(lid, r)["cells"]["notes"], "owner")
        seen = ver(self, lid)
        self.d(OWNER, "update-list", id=lid, title="Owner list")
        with self.assertRaises(Stale):
            self.d(b, "update-list", id=lid, version=seen, title="Renamed")
        self.assertEqual(cs.read(self.root, "demo", lid)["title"], "Owner list")

        nb = self.d(b, "add-block", id=c, blocks={"type": "text", "md": "mine"})["block"]
        seen = ver(self, c)
        self.d(OWNER, "update-block", id=c, block=nb, blocks={"md": "owner text"})
        self.d(OWNER, "update-canvas", id=c, title="Owner title")
        for cmd, extra in (("update-block", {"block": nb, "blocks": {"md": "bot"}}),
                           ("delete-block", {"block": nb}),
                           ("move-block", {"block": nb}),
                           ("update-canvas", {"title": "Bot title"})):
            with self.assertRaises(Stale, msg=cmd):
                self.d(b, cmd, id=c, version=seen, **extra)
        doc = cs.read(self.root, "demo", c)
        self.assertEqual(doc["title"], "Owner title")
        self.assertEqual(self.block(c, nb)["md"], "owner text")

    def test_fresh_bot_write_lands_and_other_rows_merge(self):
        lid = self.lst(OWNER, self.canvas(OWNER))
        r1 = self.d(OWNER, "add-row", id=lid, cells={"name": "a"})["row"]
        r2 = self.d(OWNER, "add-row", id=lid, cells={"name": "b"})["row"]
        seen = ver(self, lid)
        self.d(OWNER, "update-row", id=lid, row=r2, cells={"notes": "owner"})
        self.d(bot(), "update-row", id=lid, row=r1, cells={"notes": "bot"}, version=seen)
        self.assertEqual(self.row(lid, r1)["cells"]["notes"], "bot")

    def test_bot_rewrite_without_version_refused(self):
        c = self.canvas(OWNER)
        lid = self.lst(OWNER, c)
        r = self.d(OWNER, "add-row", id=lid, cells={"name": "a"})["row"]
        for cmd, fid, extra in (("update-row", lid, {"row": r, "cells": {"notes": "x"}}),
                                ("delete-row", lid, {"row": r}),
                                ("update-list", lid, {"title": "x"}),
                                ("update-block", c, {"block": "bk1", "blocks": {"md": "x"}}),
                                ("delete-block", c, {"block": "bk1"}),
                                ("move-block", c, {"block": "bk1"}),
                                ("update-canvas", c, {"title": "x"}),
                                ("delete-canvas", c, {}),
                                ("delete-list", lid, {"force_unembed": True})):
            with self.assertRaises(Invalid, msg=cmd) as e:
                self.d(bot(), cmd, id=fid, version=None, **extra)
            self.assertIn("version", str(e.exception))
        # adds merge with anything: no version needed
        self.d(bot(), "add-row", id=lid, cells={"name": "b"})
        self.d(bot(), "add-block", id=c, blocks={"type": "text", "md": "b"})

    def test_approved_hold_runs_without_a_version_check(self):
        c = self.canvas(OWNER)
        with self.assertRaises(NeedsApproval):
            self.d(bot(), "update-block", id=c, block="bk1", blocks={"md": "held"}, version=ver(self, c))
        self.d(OWNER, "add-block", id=c, blocks={"type": "text", "md": "later"})
        res = cv.run_holds(self.root, "c1")
        self.assertTrue(res[0]["ok"], res)
        self.assertEqual(self.block(c)["md"], "held")


class TestEmbedViewOnly(Base):
    """I2: embedding a view-only list into an editable canvas never makes it writable."""

    def _setup(self):
        view = self.canvas(OWNER, pin=ROOM)
        lid = self.lst(OWNER, view)
        r = self.d(OWNER, "add-row", id=lid, cells={"name": "a"})["row"]
        self.d(OWNER, "update-canvas", id=view, coworkers="view")
        return lid, r

    def test_person_cannot_embed_a_view_only_list(self):
        lid, r = self._setup()
        mine = self.d(P1, "create-canvas", title="Mine", pin=ROOM)["id"]
        with self.assertRaises(Refused):
            self.d(P1, "add-block", id=mine, blocks={"type": "list", "list": lid})
        with self.assertRaises(Refused):
            self.d(P1, "update-row", id=lid, row=r, cells={"notes": "x"})
        with self.assertRaises(Refused):
            self.d(P1, "create-canvas", title="Two", pin=ROOM, blocks=[{"type": "list", "list": lid}])

    def test_shared_bot_cannot_embed_a_view_only_list(self):
        lid, r = self._setup()
        s = self.shared
        mine = self.d(s, "create-canvas", title="Mine")["id"]
        with self.assertRaises(Refused):
            self.d(s, "add-block", id=mine, blocks={"type": "list", "list": lid})
        with self.assertRaises(Refused):
            self.d(s, "update-row", id=lid, row=r, cells={"notes": "x"}, version=ver(self, lid))

    def test_own_new_list_may_still_be_embedded(self):
        s = self.shared
        lid = self.d(s, "create-list", title="Mine", columns=[{"label": "Name"}])["id"]
        mine = self.d(s, "create-canvas", title="Mine")["id"]
        self.d(s, "add-block", id=mine, blocks={"type": "list", "list": lid})
        self.d(s, "add-row", id=lid, cells={"name": "x"})


class TestUndoAddRows(Base):
    """I3: undo of an add-rows batch never destroys a later edit, and is itself undoable."""

    def test_undo_refused_when_a_row_changed_since(self):
        lid = self.lst(OWNER, self.canvas(OWNER))
        out = self.d(bot(), "add-rows", id=lid, rows=[{"name": "a"}, {"name": "b"}])
        r1 = out["rows"][0]
        self.d(OWNER, "update-row", id=lid, row=r1, cells={"notes": "owner edit"})
        with self.assertRaises(Refused) as e:
            self.d(bot(), "undo", id=lid, change=out["change"])
        self.assertIn("changed after that change", str(e.exception))
        self.assertEqual(self.row(lid, r1)["cells"]["notes"], "owner edit")
        self.assertEqual(len(cs.read(self.root, "demo", lid)["rows"]), 2)

    def test_undo_refused_when_a_row_was_deleted_and_readded(self):
        lid = self.lst(OWNER, self.canvas(OWNER))
        out = self.d(OWNER, "add-rows", id=lid, rows=[{"name": "a"}, {"name": "b"}])
        self.d(OWNER, "delete-row", id=lid, row=out["rows"][1])
        self.d(OWNER, "add-row", id=lid, cells={"name": "new b"})  # may reuse the id
        with self.assertRaises(Refused):
            self.d(OWNER, "undo", id=lid, change=out["change"])

    def test_undo_then_redo_restores_rows(self):
        lid = self.lst(OWNER, self.canvas(OWNER))
        self.d(OWNER, "add-row", id=lid, cells={"name": "first"})
        out = self.d(OWNER, "add-rows", id=lid, rows=[{"name": "a"}, {"name": "b"}])
        undo = self.d(OWNER, "undo", id=lid, change=out["change"])
        self.assertEqual([r["cells"]["name"] for r in cs.read(self.root, "demo", lid)["rows"]], ["first"])
        self.d(OWNER, "undo", id=lid, change=undo["change"])
        rows = cs.read(self.root, "demo", lid)["rows"]
        self.assertEqual([r["cells"]["name"] for r in rows], ["first", "a", "b"])
        self.assertEqual([r["id"] for r in rows[1:]], out["rows"])

    def test_redo_refused_when_an_id_is_taken(self):
        lid = self.lst(OWNER, self.canvas(OWNER))
        out = self.d(OWNER, "add-rows", id=lid, rows=[{"name": "a"}, {"name": "b"}])
        undo = self.d(OWNER, "undo", id=lid, change=out["change"])
        self.d(OWNER, "add-row", id=lid, cells={"name": "reused"})
        with self.assertRaises(Refused):
            self.d(OWNER, "undo", id=lid, change=undo["change"])
        self.assertEqual([r["cells"]["name"] for r in cs.read(self.root, "demo", lid)["rows"]], ["reused"])

    def test_redo_keeps_labels_and_maker(self):
        lid = self.lst(OWNER, self.canvas(OWNER, pin=ROOM))
        out = self.d(P1, "add-rows", id=lid, rows=[{"name": "a"}, {"name": "b"}])
        undo = self.d(OWNER, "undo", id=lid, change=out["change"])
        self.d(OWNER, "undo", id=lid, change=undo["change"])
        rows = cs.read(self.root, "demo", lid)["rows"]
        self.assertTrue(all(r["outside"] and r["created_by"] == "person:p1" for r in rows))
        redo = cs.history(self.root, "demo", lid)[-1]
        self.d(OWNER, "undo", id=lid, change=redo["id"])  # the redo is a batch add again
        self.assertEqual(cs.read(self.root, "demo", lid)["rows"], [])


class TestTrashPins(Base):
    """I6, M8: a deleted canvas leaves its rooms (undo puts it back), purged ids never come
    back, and an embed of a purged list never shows a newer list."""

    def test_delete_unpins_and_undo_repins(self):
        c = self.canvas(OWNER, pin=ROOM)
        self.d(OWNER, "pin-canvas", id=c, room="demo/general")
        ch = self.d(OWNER, "delete-canvas", id=c)["change"]
        self.assertEqual(cs.pins(self.root, "demo"), {})
        self.d(OWNER, "undo", id=c, change=ch)
        self.assertEqual(cs.pins(self.root, "demo"), {ROOM: [c], "demo/general": [c]})
        self.assertIn(c, cv.visible_canvas_ids(self.root, "demo", P1, self.team))

    def test_purge_then_create_is_a_new_unpinned_canvas(self):
        import os
        c = self.canvas(OWNER, pin=ROOM)
        self.d(OWNER, "delete-canvas", id=c)
        os.utime(cs.base(self.root, "demo") / "canvases" / ".trash" / f"{c}.json", (0, 0))
        self.assertEqual(cs.purge_all(self.root), {"demo": [c]})
        new = self.canvas(OWNER)
        self.assertNotEqual(new, c)
        self.assertEqual(cs.pins(self.root, "demo"), {})
        self.assertEqual(cv.visible_canvas_ids(self.root, "demo", P1, self.team), [])

    def test_embed_of_a_purged_list_stays_deleted(self):
        import os
        c = self.canvas(OWNER, pin=ROOM)
        lid = self.lst(OWNER, c)
        with cs.locked(self.root, "demo", lid):  # trashed outside the rules (an older trash)
            cs.to_trash(self.root, "demo", lid, held=True)
        os.utime(cs.base(self.root, "demo") / "lists" / ".trash" / f"{lid}.json", (0, 0))
        cs.purge_all(self.root)
        other = self.d(OWNER, "create-list", title="Private", columns=[{"label": "Secret"}])["id"]
        self.assertNotEqual(other, lid)
        out = self.d(P1, "get-canvas", id=c)
        lb = next(b for b in out["blocks"] if b["type"] == "list")
        self.assertEqual(lb["summary"], {"deleted": True})
        self.assertNotIn("Private", str(out))

    def test_missing_ids_take_no_lock_file(self):
        with self.assertRaises(Refused):
            self.d(OWNER, "update-row", id="ls77", row="rw1", cells={})
        with self.assertRaises(Refused):
            self.d(OWNER, "delete-canvas", id="cv77")
        with self.assertRaises(Refused):
            self.d(OWNER, "delete-list", id="ls78")
        self.assertEqual(list((self.root / "projects").rglob("*77*")), [])
        self.assertEqual(list((self.root / "projects").rglob("*78*")), [])


class TestMinors(Base):
    def test_ungated_labeled_list_hides_options_and_long_select_values(self):  # M1
        long = "x" * 60
        cols = [{"key": "name", "label": "Name", "type": "text"},
                {"key": "stage", "label": "Stage", "type": "select", "options": ["new", long]}]
        c = self.canvas(OWNER, pin=ROOM)
        lid = self.d(P1, "create-list", title="Theirs", columns=cols)["id"]
        self.d(P1, "add-block", id=c, blocks={"type": "list", "list": lid})
        a = self.d(P1, "add-row", id=lid, cells={"name": "a", "stage": "new"})["row"]
        b = self.d(P1, "add-row", id=lid, cells={"name": "b", "stage": long})["row"]
        out = self.d(bot(gated=False), "get-list", id=lid)
        stage = next(x for x in out["columns"] if x["key"] == "stage")
        self.assertEqual(stage["options"], cv.HIDDEN)
        rows = {r["id"]: r["cells"] for r in out["rows"]}
        self.assertEqual(rows[a]["stage"], "new")
        self.assertEqual(rows[b]["stage"], cv.HIDDEN)
        self.assertNotIn(long, str(out))
        gated = self.d(bot(), "get-list", id=lid)  # a gated engine reads the labeled text
        self.assertIn(long, str(gated))

    def test_select_options_are_bounded_on_write(self):  # M1
        with self.assertRaises(Invalid):
            self.d(OWNER, "create-list", title="L", columns=[
                {"label": "S", "type": "select", "options": [f"o{i}" for i in range(cv.OPTIONS_MAX + 1)]}])
        with self.assertRaises(Invalid):
            self.d(OWNER, "create-list", title="L", columns=[
                {"label": "S", "type": "select", "options": ["x" * (cv.OPTION_MAX + 1)]}])

    def test_list_history_never_hands_non_owners_cell_values(self):  # M2
        c = self.canvas(OWNER, pin=ROOM)
        lid = self.lst(OWNER, c)
        self.d(OWNER, "add-row", id=lid, cells={"name": "a", "notes": "secret words"})
        self.d(OWNER, "update-list", id=lid, columns=[
            {"key": "name", "label": "Name", "type": "text"},
            {"key": "notes", "label": "Notes", "type": "number"}])
        self.assertIn("cleared", cs.history(self.root, "demo", lid)[-1])
        for actor in (P1, bot()):
            out = self.d(actor, "list-history", id=lid)
            self.assertNotIn("secret words", str(out))
            for e in out["entries"]:
                self.assertLessEqual(set(e), {"op", "kind", "target", "by", "job", "at", "id", "undid", "pos"})
        self.assertIn("secret words", str(self.d(OWNER, "list-history", id=lid)))


if __name__ == "__main__":
    unittest.main()
