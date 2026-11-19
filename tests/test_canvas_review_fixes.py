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


if __name__ == "__main__":
    unittest.main()
