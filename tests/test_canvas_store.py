import contextlib
import io
import json
import os
import re
import tempfile
import threading
import time
from unittest import mock
import unittest
from pathlib import Path

from core import bots, canvas_store as cs


def _doc(i, **kw):
    return {"id": i, "title": "T", "blocks": [], **kw}


def _add_rows_worker(root, lid, n, tag):
    """One process: n add-row dispatches through the rules engine (its own locks, its own cache)."""
    from core import canvases
    for k in range(n):
        canvases.dispatch(root, "demo", canvases.Actor("owner"), "add-row",
                          {"id": lid, "cells": {"name": f"{tag}-{k}"}})


def _create_worker(root, n):
    from core import canvases
    for k in range(n):
        canvases.dispatch(root, "demo", canvases.Actor("owner"), "create-canvas", {"title": f"C{k}"})


class TestCanvasStore(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _make(self, ws, i):
        with cs.locked(self.root, ws, i):
            cs.write(self.root, ws, _doc(i))

    def test_paths_per_workspace(self):
        self.assertEqual(cs.base(self.root, "demo"), self.root / "projects" / "demo")
        self.assertEqual(cs.base(self.root, bots.HQ), self.root / "team")

    def test_ids_refuse_traversal(self):
        self.assertTrue(cs.valid("canvas", "cv1"))
        for bad in ("cv1/../x", "../cv1", "cv", "cv1234567", "CV1"):
            self.assertFalse(cs.valid("canvas", bad), bad)
        self.assertTrue(cs.valid("list", "ls12"))
        self.assertTrue(cs.valid("template", "tp3"))
        self.assertTrue(cs.valid("block", "bk1"))
        self.assertTrue(cs.valid("row", "rw9"))
        self.assertTrue(cs.valid("item", "i4"))
        self.assertTrue(cs.valid("change", "ch0123456789ab"))
        self.assertFalse(cs.valid("change", "ch0123"))
        self.assertFalse(cs.valid("nope", "cv1"))
        with self.assertRaises(ValueError):
            cs.read(self.root, "demo", "../x")
        with self.assertRaises(LookupError):
            cs.read(self.root, "demo", "cv9")

    def test_next_id_counts_trash(self):
        self.assertEqual(cs.next_id(self.root, "demo", "cv"), "cv1")
        self._make("demo", "cv1")
        self._make("demo", "cv2")
        cs.to_trash(self.root, "demo", "cv2")
        self.assertEqual(cs.next_id(self.root, "demo", "cv"), "cv3")
        self.assertEqual(cs.list_ids(self.root, "demo", "cv"), ["cv1"])
        self.assertEqual(cs.next_child_id([{"id": "bk2"}, {"id": "bk7"}], "bk"), "bk8")
        self.assertEqual(cs.next_child_id([], "rw"), "rw1")

    def test_write_bumps_version_atomic(self):
        d = _doc("cv1", version=0)
        with cs.locked(self.root, "demo", "cv1"):
            self.assertEqual(cs.write(self.root, "demo", d)["version"], 1)
            self.assertEqual(cs.write(self.root, "demo", d)["version"], 2)
        self.assertEqual(cs.read(self.root, "demo", "cv1")["version"], 2)
        left = [p for p in (self.root / "projects" / "demo").rglob("*.tmp")]
        self.assertEqual(left, [])

    def test_history_append_and_rotate(self):
        self._make("demo", "cv1")
        cid = cs.history_append(self.root, "demo", "cv1", {"by": "x", "text": "a"})
        self.assertRegex(cid, r"^ch[0-9a-f]{12}$")
        h = cs.history(self.root, "demo", "cv1")
        self.assertEqual(h[0]["id"], cid)
        self.assertIn("at", h[0])
        blob = "z" * 100_000
        last = None
        for _ in range(25):
            last = cs.history_append(self.root, "demo", "cv1", {"blob": blob})
        hdir = cs.base(self.root, "demo") / "canvases" / "history"
        self.assertTrue((hdir / "cv1.1.jsonl").exists())
        self.assertEqual(cs.history(self.root, "demo", "cv1")[-1]["id"], last)

    def test_purge_trash_after_30_days(self):
        self._make("demo", "cv1")
        self._make("demo", "cv2")
        cs.history_append(self.root, "demo", "cv1", {"a": 1})
        cs.to_trash(self.root, "demo", "cv1")
        cs.to_trash(self.root, "demo", "cv2")
        trash = cs.base(self.root, "demo") / "canvases" / ".trash"
        now = time.time()
        os.utime(trash / "cv1.json", (now - 31 * 86400,) * 2)
        os.utime(trash / "cv2.json", (now - 29 * 86400,) * 2)
        self.assertEqual(cs.purge_trash(self.root, "demo", now=now), ["cv1"])
        self.assertFalse((trash / "cv1.json").exists())
        self.assertFalse(list(trash.glob("cv1*")))
        self.assertTrue((trash / "cv2.json").exists())
        cs.from_trash(self.root, "demo", "cv2")
        self.assertEqual(cs.read(self.root, "demo", "cv2")["id"], "cv2")

    def test_pins_round_trip_and_drop_room(self):
        self.assertEqual(cs.pins(self.root, "demo"), {})
        cs.set_pins(self.root, "demo", {"room1": ["cv1"], "room2": ["ls1"]})
        self.assertEqual(cs.pins(self.root, "demo")["room1"], ["cv1"])
        cs.drop_room_pins(self.root, "demo", "room1")
        self.assertEqual(cs.pins(self.root, "demo"), {"room2": ["ls1"]})

    def test_drop_room_pins_waits_for_pins_lock(self):
        cs.set_pins(self.root, "demo", {"room1": ["cv1"]})
        done = threading.Event()
        with cs.pins_lock(self.root, "demo"):
            t = threading.Thread(target=lambda: (cs.drop_room_pins(self.root, "demo", "room1"),
                                                 done.set()))
            t.start()
            self.assertFalse(done.wait(0.3))
        t.join(5)
        self.assertTrue(done.is_set())
        self.assertEqual(cs.pins(self.root, "demo"), {})

    def test_to_trash_held_by_caller(self):
        self._make("demo", "cv1")
        with cs.locked(self.root, "demo", "cv1"):
            cs.to_trash(self.root, "demo", "cv1", held=True)
        with self.assertRaises(LookupError):
            cs.read(self.root, "demo", "cv1")
        with self.assertRaises(LookupError):
            cs.to_trash(self.root, "demo", "cv9")

    def test_shipped_templates_load(self):
        ts = cs.templates(self.root, "demo")
        self.assertEqual({t["id"] for t in ts},
                         {"tracker", "pipeline", "meeting-notes", "checklist"})
        for t in ts:
            self.assertTrue(t["blocks"])
            for b in t["blocks"]:
                if b["type"] == "list":
                    self.assertTrue(b["columns"])
                    self.assertNotIn("rows", b)
        with cs.locked(self.root, "demo", "tp1"):
            cs.write(self.root, "demo", _doc("tp1"))
        self.assertIn("tp1", {t["id"] for t in cs.templates(self.root, "demo")})
        self.assertEqual(cs.next_id(self.root, "demo", "tp"), "tp2")

    def test_locked_no_lost_update(self):
        self._make("demo", "cv1")

        def bump():
            for _ in range(20):
                with cs.locked(self.root, "demo", "cv1"):
                    d = cs.read(self.root, "demo", "cv1")
                    d["n"] = d.get("n", 0) + 1
                    cs.write(self.root, "demo", d)

        ts = [threading.Thread(target=bump) for _ in range(2)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        d = cs.read(self.root, "demo", "cv1")
        self.assertEqual(d["n"], 40)
        self.assertEqual(d["version"], 41)

    def test_write_failure_leaves_original(self):
        self._make("demo", "cv1")
        d = cs.read(self.root, "demo", "cv1")
        d["title"] = "changed"
        with mock.patch("core.canvas_store.os.replace", side_effect=OSError("boom")):
            with self.assertRaises(OSError):
                cs.write(self.root, "demo", d)
        self.assertEqual(cs.read(self.root, "demo", "cv1")["title"], "T")
        self.assertEqual(list((self.root / "projects" / "demo").rglob("*.tmp")), [])

    def test_hq_round_trip(self):
        self._make(bots.HQ, "cv1")
        self.assertEqual(cs.read(self.root, bots.HQ, "cv1")["id"], "cv1")
        self.assertEqual(cs.list_ids(self.root, bots.HQ, "cv"), ["cv1"])
        self.assertTrue((self.root / "team" / "canvases" / "cv1.json").exists())

    def test_create_concurrent_distinct_ids(self):
        ids = []
        lk = threading.Lock()

        def mk():
            for _ in range(10):
                d = cs.create(self.root, "demo", "cv", {"title": "x", "blocks": []})
                with lk:
                    ids.append(d["id"])

        ts = [threading.Thread(target=mk) for _ in range(4)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(len(set(ids)), 40)

    def test_write_new_existing_file_refused(self):
        self._make("demo", "cv1")
        with self.assertRaises(FileExistsError):
            cs.write(self.root, "demo", _doc("cv1"))
        self.assertEqual(cs.read(self.root, "demo", "cv1")["version"], 1)

    def test_write_stale_doc_refused(self):
        self._make("demo", "cv1")
        with cs.locked(self.root, "demo", "cv1"):
            d = cs.read(self.root, "demo", "cv1")
            cs.write(self.root, "demo", dict(d))
            with self.assertRaises(ValueError):
                cs.write(self.root, "demo", d)

    def test_lock_files_are_never_unlinked(self):
        """A waiter on an unlinked lock file and a newcomer on its replacement would both enter:
        lock files stay (empty) through trash, restore and purge."""
        self._make("demo", "cv1")
        cdir = self.root / "projects" / "demo" / "canvases"
        lock = cdir / "cv1.json.lock"
        ino = lock.stat().st_ino
        cs.to_trash(self.root, "demo", "cv1")
        cs.from_trash(self.root, "demo", "cv1")
        cs.to_trash(self.root, "demo", "cv1")
        os.utime(cdir / ".trash" / "cv1.json", (0, 0))
        self.assertEqual(cs.purge_trash(self.root, "demo"), ["cv1"])
        self.assertEqual(lock.stat().st_ino, ino)
        self.assertEqual(lock.stat().st_size, 0)
        self.assertFalse(hasattr(cs, "_drop_lock"))

    def test_no_lock_file_for_a_missing_id(self):
        with self.assertRaises(LookupError):
            cs.to_trash(self.root, "demo", "cv9")
        with self.assertRaises(LookupError):
            cs.from_trash(self.root, "demo", "cv9")
        self.assertEqual(list((self.root / "projects").rglob("*.lock")), [])

    def test_purged_ids_are_never_reused(self):
        self._make("demo", "cv1")
        made = cs.create(self.root, "demo", "cv", _doc(""))
        self.assertEqual(made["id"], "cv2")
        cs.to_trash(self.root, "demo", "cv2")
        os.utime(cs.base(self.root, "demo") / "canvases" / ".trash" / "cv2.json", (0, 0))
        cs.purge_trash(self.root, "demo")
        self.assertEqual(cs.create(self.root, "demo", "cv", _doc(""))["id"], "cv3")
        lid = cs.create(self.root, "demo", "ls", {"title": "L", "rows": []})["id"]
        cs.to_trash(self.root, "demo", lid)
        os.utime(cs.base(self.root, "demo") / "lists" / ".trash" / f"{lid}.json", (0, 0))
        cs.purge_trash(self.root, "demo")
        self.assertEqual(cs.create(self.root, "demo", "ls", {"title": "L", "rows": []})["id"], "ls2")

    def test_purge_drops_the_purged_canvas_pins(self):
        self._make("demo", "cv1")
        self._make("demo", "cv2")
        cs.set_pins(self.root, "demo", {"demo/general": ["cv1", "cv2"], "demo/t-x": ["cv1"]})
        cs.to_trash(self.root, "demo", "cv1")
        os.utime(cs.base(self.root, "demo") / "canvases" / ".trash" / "cv1.json", (0, 0))
        cs.purge_trash(self.root, "demo")
        self.assertEqual(cs.pins(self.root, "demo"), {"demo/general": ["cv2"]})

    def test_from_trash_refuses_a_taken_live_path_and_a_race(self):
        self._make("demo", "cv1")
        cs.to_trash(self.root, "demo", "cv1")
        self._make("demo", "cv1")  # the live path is taken again
        with self.assertRaises(FileExistsError):
            cs.from_trash(self.root, "demo", "cv1")
        self.assertTrue((cs.base(self.root, "demo") / "canvases" / ".trash" / "cv1.json").exists())
        # two restores at once: one lands, the other is a plain LookupError
        self._make("demo", "cv5")
        cs.to_trash(self.root, "demo", "cv5")
        errs = []

        def restore():
            try:
                cs.from_trash(self.root, "demo", "cv5")
            except LookupError as e:
                errs.append(e)
        with cs.locked(self.root, "demo", "cv5"):
            ts = [threading.Thread(target=restore) for _ in range(2)]
            for t in ts:
                t.start()
            time.sleep(0.2)
        for t in ts:
            t.join(5)
        self.assertEqual(len(errs), 1)
        self.assertEqual(cs.read(self.root, "demo", "cv5")["id"], "cv5")

    def test_purge_all_workspaces_and_hq(self):
        for ws in ("demo", "acme", bots.HQ):
            (self.root / "projects" / "acme").mkdir(parents=True, exist_ok=True)
            (self.root / "projects" / "demo").mkdir(parents=True, exist_ok=True)
            self._make(ws, "cv1")
            cs.to_trash(self.root, ws, "cv1")
            os.utime(cs.base(self.root, ws) / "canvases" / ".trash" / "cv1.json", (0, 0))
        (self.root / "projects" / "Bad Name").mkdir()
        gone = cs.purge_all(self.root)
        self.assertEqual(gone, {"acme": ["cv1"], "demo": ["cv1"], bots.HQ: ["cv1"]})

    def test_purge_all_one_bad_workspace_does_not_stop_the_rest(self):
        for ws in ("acme", "demo"):
            (self.root / "projects" / ws).mkdir(parents=True, exist_ok=True)
            self._make(ws, "cv1")
            cs.to_trash(self.root, ws, "cv1")
            os.utime(cs.base(self.root, ws) / "canvases" / ".trash" / "cv1.json", (0, 0))
        real = cs.purge_trash

        def purge(root, ws, *a):
            if ws == "acme":
                raise OSError("disk")
            return real(root, ws, *a)
        with mock.patch.object(cs, "purge_trash", side_effect=purge), \
                contextlib.redirect_stderr(io.StringIO()) as err:
            gone = cs.purge_all(self.root)
        self.assertEqual(gone, {"demo": ["cv1"]})
        self.assertIn("acme", err.getvalue())

    def test_saved_templates_not_shipped(self):
        with cs.locked(self.root, "demo", "tp1"):
            cs.write(self.root, "demo", _doc("tp1"))
        by = {t["id"]: t for t in cs.templates(self.root, "demo")}
        self.assertFalse(by["tp1"]["shipped"])
        self.assertTrue(by["tracker"]["shipped"])


class TestCrossProcess(unittest.TestCase):
    """Writers in separate processes (a bot's osctl and the server, two runs): flock serializes
    them; no lost update, no duplicate id, one history line per change."""

    def test_processes_never_lose_or_duplicate_a_write(self):
        import multiprocessing
        from core import canvases
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        (root / "projects" / "demo").mkdir(parents=True)
        lid = canvases.dispatch(root, "demo", canvases.Actor("owner"), "create-list",
                                {"title": "L", "columns": [{"label": "Name"}]})["id"]
        ctx = multiprocessing.get_context("spawn")
        procs = [ctx.Process(target=_add_rows_worker, args=(str(root), lid, 50, f"w{i}")) for i in range(3)]
        procs.append(ctx.Process(target=_create_worker, args=(str(root), 20)))
        for pr in procs:
            pr.start()
        for pr in procs:
            pr.join(120)
            self.assertEqual(pr.exitcode, 0)
        doc = cs.read(root, "demo", lid)
        rows = doc["rows"]
        self.assertEqual(len(rows), 150)
        self.assertEqual(len({r["id"] for r in rows}), 150)
        self.assertEqual(doc["version"], 151)  # created at 1, one bump per write
        self.assertEqual(sorted(r["v"] for r in rows), list(range(2, 152)))  # each write saw the last
        hist = cs.history(root, "demo", lid)
        self.assertEqual(len(hist), 151)
        self.assertEqual(len({h["id"] for h in hist}), 151)
        self.assertEqual(sorted(cs.list_ids(root, "demo", "cv"), key=lambda x: int(x[2:])),
                         [f"cv{i}" for i in range(1, 21)])
        for cid in cs.list_ids(root, "demo", "cv"):
            self.assertEqual(len(cs.history(root, "demo", cid)), 1)


if __name__ == "__main__":
    unittest.main()
