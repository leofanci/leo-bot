"""Live browser frames belong to the relay's own, locked browser calls."""
import base64
import io
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from core import bots, browser
from dashboard import browser_relay as relay
from tests.test_browser_relay import FakeUpstream, call, text_of, u
from tests import test_browser_api as api

P = "agent_browser_"
PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=")
PAGE = u("acme.example/demo")
CTX = {"ws": "acme", "bot": "profile-a", "person": "ceo"}


class CaptureUpstream(FakeUpstream):
    def __init__(self, mode="bot"):
        super().__init__(PAGE, mode=mode)
        self.capture_locks = []
        # agent-browser 0.41.0 cli/src/mcp.rs:tool_result_from_run: text precedes image content.
        self.screenshot = {"content": [{"type": "text", "text": "/demo.png"},
                                       {"type": "image", "mimeType": "image/png",
                                        "data": base64.b64encode(PNG).decode()}],
                           "isError": False, "structuredContent": {"exitCode": 0,
                               "response": {"success": True, "data": {"path": "/demo.png"}}}}
        self.fail: str | None = None

    def tool_list(self):
        props = {"url": {}, "text": {}, "selector": {}, "key": {}}
        return [{"name": name, "inputSchema": {"type": "object", "properties": props}}
                for name in relay.KNOWN]

    def forward(self, frame, sid):
        name = (frame.get("params") or {}).get("name")
        if name in (P + "screenshot", P + "get_url", P + "get_title"):
            self.frames.append(frame)
            self.capture_locks.append(relay._browser_lock(self.lock_key).locked())
            if name == self.fail:
                raise RuntimeError("capture failed")
            if name == P + "screenshot":
                result = self.screenshot
            else:
                key = "url" if name == P + "get_url" else "title"
                value = self.url if key == "url" else "Demo"
                result = {"content": [{"type": "text", "text": value}], "isError": False,
                          "structuredContent": {"exitCode": 0,
                              "response": {"success": True, "data": {key: value}}}}
            return {"jsonrpc": "2.0", "id": frame["id"], "result": result}, "m-1"
        return super().forward(frame, sid)


class Captures(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        browser.epoch("acme", self.root)
        browser.set_bot("acme", "profile-a", "act", "bot", self.root)
        self.up = CaptureUpstream()
        live = mock.patch.dict(relay.LIVE, clear=True)
        live.start()
        self.addCleanup(live.stop)

    def handle(self, name, **args):
        return relay.handle(call(P + name, **args), CTX, "s-1", self.up, self.root)[0]

    def test_open_captures_image_and_metadata_inside_its_lock(self):
        reply = self.handle("open", url=PAGE)
        self.assertEqual(text_of(reply), "ok")
        self.assertEqual(self.up.calls(), [P + n for n in ("open", "screenshot", "get_url", "get_title")])
        self.assertEqual(self.up.capture_locks, [True, True, True])
        frame = relay.LIVE[("acme", "profile-a")]
        self.assertEqual((frame["png"], frame["url"], frame["title"]), (PNG, PAGE, "Demo"))
        self.assertGreater(frame["at"], 0)

    def test_capture_follows_navigation_plain_commits_and_actual_sends(self):
        for name, args in (("reload", {}), ("click", {"selector": "@e1"}), ("press", {"key": "Tab"})):
            with self.subTest(name=name):
                self.up.frames.clear()
                self.assertFalse(self.handle(name, **args)["result"].get("isError"))
                self.assertEqual(self.up.calls(), [P + n for n in (name, "screenshot", "get_url", "get_title")])
        self.handle("fill", selector="@e1", text="demo")
        self.up.frames.clear()
        self.assertEqual(text_of(self.handle("press", key="Enter")), "ok")
        self.assertEqual(self.up.calls(), [P + n for n in ("press", "screenshot", "get_url", "get_title")])
        self.assertTrue(all(self.up.capture_locks))

    def test_no_capture_for_reads_typing_or_refused_calls(self):
        for name, args in (("snapshot", {}), ("scroll", {}), ("fill", {"selector": "@e1", "text": "demo"})):
            self.handle(name, **args)
        browser.set_bot("acme", "profile-a", "write", "bot", self.root)
        self.assertTrue(self.handle("click", selector="@e1")["result"]["isError"])
        self.assertTrue(self.handle("open", url="file:///demo")["result"]["isError"])
        self.assertNotIn(P + "screenshot", self.up.calls())
        self.assertEqual(relay.LIVE, {})

    def test_capture_failures_never_replace_last_frame_or_bot_result(self):
        self.handle("open", url=PAGE)
        before = dict(relay.LIVE)
        for name in ("screenshot", "get_url", "get_title"):
            self.up.fail = P + name
            self.assertEqual(text_of(self.handle("reload")), "ok")
            self.assertEqual(relay.LIVE, before)

    def test_image_bytes_only_never_paths_or_malformed_images(self):
        for content in ([{"type": "text", "text": str(self.root / "demo.png")}],
                        [{"type": "image", "mimeType": "image/png", "data": "not base64"}],
                        [{"type": "image", "mimeType": "image/jpeg", "data": base64.b64encode(PNG).decode()}],
                        [{"type": "image", "mimeType": "image/png", "data": base64.b64encode(b"demo").decode()}]):
            self.up.screenshot = {"content": content, "structuredContent": {
                "response": {"data": {"path": str(self.root / "demo.png")}}}}
            with mock.patch.object(Path, "read_bytes", side_effect=AssertionError("no file reads")):
                self.assertEqual(text_of(self.handle("open", url=PAGE, path="/demo.png")), "ok")
            self.assertEqual(relay.LIVE, {})

    def test_my_chrome_capture_uses_only_the_existing_pinned_upstream(self):
        browser.set_bot("acme", "profile-a", "act", "my_chrome", self.root)
        self.up = CaptureUpstream(mode="my_chrome")
        self.up.lock_key = "my_chrome"
        self.handle("open", url=PAGE)
        self.assertEqual(self.up.calls(), [P + n for n in ("open", "screenshot", "get_url", "get_title")])
        for f in self.up.frames:
            args = (f.get("params") or {}).get("arguments", {})
            self.assertFalse({"tab", "tabId", "targetId", "path", "screenshotDir", "session"} & set(args))
        self.assertTrue(all(self.up.capture_locks))

    def test_a_coworkers_own_chrome_is_never_captured_for_the_owner(self):
        browser.set_bot("acme", "profile-a", "act", "my_chrome", self.root)
        for owner, ctx in (("pm-1", CTX), (None, {**CTX, "runner_owner": "pm-1"})):
            with self.subTest(owner=owner, ctx=ctx):
                self.up = CaptureUpstream(mode="my_chrome")
                self.up.lock_key = "my_chrome"
                if owner:
                    self.up.owner = owner
                reply = relay.handle(call(P + "open", url=PAGE), ctx, "s-1", self.up, self.root)[0]
                self.assertEqual(text_of(reply), "ok")
                self.assertNotIn(P + "screenshot", self.up.calls())
                self.assertEqual(relay.LIVE, {})
        self.up = CaptureUpstream(mode="my_chrome")
        self.up.lock_key, self.up.owner = "my_chrome", "owner"
        relay.handle(call(P + "open", url=PAGE), {**CTX, "runner_owner": "owner"}, "s-1", self.up, self.root)
        self.assertIn(P + "screenshot", self.up.calls())
        self.assertIn(("acme", "profile-a"), relay.LIVE)

    def test_capture_sends_only_the_screenshot_keys_the_engine_declares(self):
        def shot_args():
            return [(f.get("params") or {}).get("arguments") for f in self.up.frames
                    if (f.get("params") or {}).get("name") == P + "screenshot"]
        self.handle("open", url=PAGE)
        self.assertEqual(shot_args(), [{}])
        self.assertIn(("acme", "profile-a"), relay.LIVE)
        relay.LIVE.clear()
        self.up = CaptureUpstream()
        listed = self.up.tool_list()
        for tool in listed:
            if tool["name"] == P + "screenshot":
                tool["inputSchema"] = {"type": "object", "properties": {"format": {}, "path": {}}}
        self.up.tool_list = lambda: listed
        self.handle("open", url=PAGE)
        self.assertEqual(shot_args(), [{"format": "png"}])

    def test_capture_removes_the_engine_file_it_read(self):
        home = self.root / "home"
        folder = home / ".agent-browser" / "tmp" / "screenshots"
        folder.mkdir(parents=True)
        elsewhere = self.root / "screenshot-2.png"
        elsewhere.write_bytes(PNG)
        with mock.patch.dict(os.environ, {"HOME": str(home)}):
            for remote, name in ((False, "screenshot-1.png"), (True, "screenshot-3.png"),
                                 (False, "demo.png"), (False, None)):
                with self.subTest(remote=remote, name=name):
                    path = folder / name if name else elsewhere
                    path.write_bytes(PNG)
                    self.up = CaptureUpstream()
                    self.up.remote = remote
                    self.up.screenshot["structuredContent"]["response"]["data"]["path"] = str(path)
                    self.handle("open", url=PAGE)
                    self.assertIn(("acme", "profile-a"), relay.LIVE)
                    self.assertEqual(path.exists(), remote or name != "screenshot-1.png")

    def test_prune_needs_image_bytes_in_the_reply(self):
        home = self.root / "home"
        folder = home / ".agent-browser" / "tmp" / "screenshots"
        folder.mkdir(parents=True)
        path = folder / "screenshot-4.png"
        path.write_bytes(PNG)
        result = {"content": [{"type": "text", "text": str(path)}],
                  "structuredContent": {"response": {"data": {"path": str(path)}}}}
        self.assertFalse(relay.prune_screenshot(result, home=str(home)))
        self.assertTrue(path.exists())
        result["content"].append({"type": "image", "mimeType": "image/png", "data": ""})
        self.assertTrue(relay.prune_screenshot(result, home=str(home)))
        self.assertFalse(path.exists())

    def test_failed_actions_and_refused_redirects_never_capture(self):
        with mock.patch.object(self.up, "forward", return_value=({"result": {"isError": True}}, None)):
            self.handle("open", url=PAGE)
        self.assertEqual(relay.LIVE, {})
        browser.set_site("demo.example", "my_chrome", None, self.root)
        self.up.redirects[PAGE] = u("demo.example/demo")
        self.assertTrue(self.handle("open", url=PAGE)["result"]["isError"])
        self.assertNotIn(P + "screenshot", self.up.calls())

    def test_late_capture_from_retired_epoch_is_discarded(self):
        self.up.epoch = browser.epochs(self.root)["acme"]
        browser.forget_workspace("acme", self.root)
        browser.epoch("acme", self.root)
        browser.set_bot("acme", "profile-a", "act", "bot", self.root)
        self.handle("open", url=PAGE)
        self.assertEqual(relay.LIVE, {})


class LiveAPI(api.Base):
    def setUp(self):
        super().setUp()
        for p in (mock.patch.dict(relay.LIVE, clear=True),
                  mock.patch.object(api.server.bots, "load_defs", return_value={"profile-a": None, "profile-b": None})):
            p.start()
            self.addCleanup(p.stop)
        browser.epoch("acme", self.root)
        browser.set_bot("acme", "profile-a", "act", "bot", self.root)
        self.up = CaptureUpstream()
        relay.handle(call(P + "open", url=PAGE), CTX, None, self.up, self.root)

    def get(self, bot="profile-a", **kwargs):
        return api.request("GET", "/api/browser/live?ws=acme&bot=" + bot, **kwargs)

    def test_owner_gets_cached_png_without_starting_or_calling_browser(self):
        with mock.patch.object(self.pool, "get", side_effect=AssertionError("no upstream creation")), \
                mock.patch.object(self.up, "forward", side_effect=AssertionError("no browser polling")):
            code, data = self.get()
        self.assertEqual(code, 200, data)
        self.assertEqual(data, {"url": PAGE, "title": "Demo", "at": relay.LIVE[("acme", "profile-a")]["at"],
                                "live": False, "image": "data:image/png;base64," + base64.b64encode(PNG).decode()})

    def test_person_and_unauthenticated_get_owner_only(self):
        _, invite = api.people.create_invite(self.root, ws="acme", role="admin", by="ceo")
        _, member = api.people.join(self.root, invite, "Demo")
        for kwargs in ({"owner": False}, {"owner": False, "member": member}):
            self.assertEqual(self.get(**kwargs), (403, {"error": "owner only"}))

    def test_rate_limit_is_per_bot_and_expires_after_one_second(self):
        with mock.patch.object(api.server.time, "monotonic", return_value=100) as clock:
            self.assertEqual(self.get()[0], 200)
            self.assertEqual(self.get()[0], 429)
            self.assertEqual(self.get("profile-b")[0], 200)
            clock.return_value = 101
            self.assertEqual(self.get()[0], 200)

    def test_live_uses_existing_child_state_not_allocated_upstream(self):
        up = self.pool.get("acme", "profile-a", "bot", self.root)
        self.assertFalse(self.get()[1]["live"])
        up.forward(call(P + "open", url=PAGE), None)
        with mock.patch.object(up, "forward", side_effect=AssertionError("no browser call")), \
                mock.patch.object(up, "current_url", side_effect=AssertionError("no browser call")), \
                mock.patch.object(api.server.time, "monotonic", return_value=time.monotonic() + 2):
            self.assertTrue(self.get()[1]["live"])
        up.kill()
        with mock.patch.object(api.server.time, "monotonic", return_value=time.monotonic() + 4):
            data = self.get()[1]
        self.assertFalse(data["live"])
        self.assertTrue(data["image"])

    def test_no_frame_leaks_across_workspace_epoch_or_browser_mode(self):
        browser.forget_workspace("acme", self.root)
        browser.epoch("acme", self.root)
        browser.set_bot("acme", "profile-a", "act", "bot", self.root)
        self.assertIsNone(self.get()[1]["image"])
        relay.handle(call(P + "open", url=PAGE), CTX, None, self.up, self.root)
        browser.set_bot("acme", "profile-a", "act", "my_chrome", self.root)
        with mock.patch.object(api.server.time, "monotonic", return_value=time.monotonic() + 2):
            self.assertIsNone(self.get()[1]["image"])

    def test_no_calls_no_background_capture_and_invalid_target_rejected(self):
        frames, latest = list(self.up.frames), dict(relay.LIVE)
        time.sleep(0.02)
        self.assertEqual((self.up.frames, relay.LIVE), (frames, latest))
        self.assertEqual(api.request("GET", "/api/browser/live?ws=demo&bot=profile-a")[0], 400)
        self.assertEqual(self.get("../profile-a")[0], 400)

    def test_mode_round_trip_discards_old_frame_even_without_viewer_reads(self):
        with mock.patch.object(api.server, "_browser_view", return_value={}):
            for mode in ("my_chrome", "bot"):
                code, data = api.request("POST", "/api/browser/settings", {
                    "ws": "acme", "bot": "profile-a", "level": "act", "mode": mode})
                self.assertEqual(code, 200, data)
        self.assertIsNone(self.get()[1]["image"])

    def test_stop_closes_only_target_browser_child_keeps_other_runs(self):
        browser.set_bot("acme", "profile-b", "act", "bot", self.root)
        target = self.pool.get("acme", "profile-a", "bot", self.root)
        target.forward(call(P + "open", url=PAGE), None)
        child = target._child.proc
        other = self.pool.get("acme", "profile-b", "bot", self.root)
        other.forward(call(P + "open", url=PAGE), None)
        unrelated = mock.Mock()
        with mock.patch.object(api.server, "_CHAT", unrelated):
            code, data = api.request("POST", "/api/chat-stop", {"ws": "acme", "bot": "profile-a"})
        self.assertEqual((code, data), (200, {"ok": True}))
        self.assertTrue(target.closed)
        self.assertIsNotNone(child.poll())
        self.assertFalse(other.closed)
        unrelated.close.assert_not_called()
        self.assertFalse(self.get()[1]["live"])
        self.assertTrue(self.get("profile-b")[1]["live"])

    def test_stop_closes_remote_runner_child_not_sibling_or_other_scopes(self):
        from core import runners
        from dashboard import runner as runner_client
        from tests.test_browser_lent import FakeRunner, LEND
        from tests.test_browser_upstream import CDP

        hub = runners.Hub()
        settings = self.root / "runner-settings.json"
        runner_client.set_lend(settings, True)
        lender = runner_client.Lender(self.root / "lend", settings)
        self.addCleanup(lender.close_all)
        runner = FakeRunner(hub, "profile-a", "owner", lender.answer)
        with mock.patch.dict(os.environ, {"LEOBOT_DEPLOYMENT": "server"}), \
                mock.patch.object(runners, "HUB", hub), \
                mock.patch.object(api.bu, "find_chrome_debug_url", return_value=CDP):
            browser.set_bot("acme", "profile-a", "act", "my_chrome", self.root)
            epoch = browser.epochs(self.root)["acme"]
            runner.start()
            self.addCleanup(runner.stop)
            deadline = time.monotonic() + 2
            while not hub.reachable("owner") and time.monotonic() < deadline:
                time.sleep(0.01)
            remote = self.pool.get("acme", "profile-a", "my_chrome", self.root,
                                   ctx={"runner_owner": "owner"})
            self.assertEqual(remote.current_url(), "about:blank")
            target = lender._ups[("acme", "profile-a", epoch)]
            child = target._child.proc
            keys = [("acme", "profile-b", epoch), ("demo", "profile-a", epoch),
                    ("acme", "profile-a", "dddddd" if epoch != "dddddd" else "eeeeee")]
            for ws, bot, e in keys:
                lender.answer({"kind": "browser", "turn": "a" * 32, "op": "url",
                               "ws": ws, "bot": bot, "epoch": e})
            siblings = [lender._ups[key] for key in keys]
            hub.next("profile-b", "profile-b", wait=0, caps=LEND)
            _, invite = api.people.create_invite(self.root, ws="acme", role="admin", by="ceo")
            _, member = api.people.join(self.root, invite, "Demo")
            body = {"ws": "acme", "bot": "profile-a", "runner_owner": "profile-b", "epoch": "ffffff"}
            self.assertEqual(api.request("POST", "/api/chat-stop", body, owner=False, member=member)[0], 403)
            self.assertEqual(api.request("POST", "/api/chat-stop", body, origin="")[0], 403)
            self.assertIsNone(child.poll())
            code, data = api.request("POST", "/api/chat-stop", body)
            self.assertEqual((code, data), (200, {"ok": True}))
            deadline = time.monotonic() + 3
            while child.poll() is None and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertIsNotNone(child.poll(), "Stop must close the remote engine child, not only its proxy")
            self.assertTrue(target.closed)
            self.assertTrue(all(not up.closed and up._child.proc.poll() is None for up in siblings))
            self.assertIsNone(hub.next("profile-b", "profile-b", wait=0, caps=LEND))
            notes = [j for j in runner.jobs if j["op"] == "close"]
            self.assertEqual([(j["ws"], j["bot"], j["epoch"], j["runner"]) for j in notes],
                             [("acme", "profile-a", epoch, "profile-a")])
            self.assertTrue(remote.closed)
            self.assertFalse(self.pool.is_live("acme", "profile-a", "my_chrome", self.root))
            runner.stop()
            lender.close_all()

    def test_runner_bot_close_interrupts_a_browser_call_in_flight(self):
        from dashboard import runner as runner_client
        from tests.test_browser_upstream import CDP

        settings = self.root / "runner-settings.json"
        runner_client.set_lend(settings, True)
        lender = runner_client.Lender(self.root / "lend", settings)
        self.addCleanup(lender.close_all)
        epoch = browser.epochs(self.root)["acme"]
        job = {"kind": "browser", "turn": "a" * 32, "op": "url",
               "ws": "acme", "bot": "profile-a", "epoch": epoch}
        with mock.patch.object(api.bu, "find_chrome_debug_url", return_value=CDP):
            lender.answer(job)
            up = lender._ups[("acme", "profile-a", epoch)]
            child = up._child.proc
            (self.home / "fake.json").write_text(json.dumps({"hang_on": P + "get_title"}))
            busy = threading.Thread(target=lender.answer, args=({
                **job, "op": "call", "frame": call(P + "get_title")},))
            closing = threading.Thread(target=lender.answer, args=({**job, "op": "close"},))
            try:
                busy.start()
                deadline = time.monotonic() + 2
                while not any(row["name"] == P + "get_title" for row in self.log("call")):
                    self.assertLess(time.monotonic(), deadline, "fixture call did not start")
                    time.sleep(0.01)
                closing.start()
                closing.join(1)
                self.assertFalse(closing.is_alive(), "Stop must interrupt the call, not wait for its timeout")
                self.assertIsNotNone(child.poll())
                self.assertTrue(up.closed)
            finally:
                up.kill()
                busy.join(3)
                if closing.ident is not None:
                    closing.join(3)

    def test_targeted_stop_requires_owner_origin_and_valid_target(self):
        _, invite = api.people.create_invite(self.root, ws="acme", role="admin", by="ceo")
        _, member = api.people.join(self.root, invite, "Demo")
        body = {"ws": "acme", "bot": "profile-a"}
        self.assertEqual(api.request("POST", "/api/chat-stop", body, owner=False, member=member)[0], 403)
        self.assertEqual(api.request("POST", "/api/chat-stop", body, origin="")[0], 403)
        self.assertEqual(api.request("POST", "/api/chat-stop", {**body, "bot": "../profile-a"})[0], 400)


class UntargetedStop(api.Base):
    def test_owner_stop_leaves_a_coworkers_run_token_and_child_alone(self):
        browser.epoch("acme", self.root)
        for bot in ("profile-a", "profile-b"):
            browser.set_bot("acme", bot, "act", "bot", self.root)
        owner_up = self.pool.get("acme", "profile-a", "bot", self.root)
        owner_up.forward(call(P + "open", url=PAGE), None)
        coworker_up = self.pool.get("acme", "profile-b", "bot", self.root)
        coworker_up.forward(call(P + "open", url=PAGE), None)
        runs, sessions = {}, {}
        for bot, sender in (("profile-a", "owner"), ("profile-b", "pm-1")):
            token = api.server.BROWSER_TOKENS.mint({"ws": "acme", "bot": bot, "runner_owner": sender})
            sessions[sender] = mock.Mock()
            runs[token] = {"ws": "acme", "bot": bot, "session": sessions[sender], "live": {},
                           "job": False, "token": token, "sender": sender}
        tokens = {r["sender"]: t for t, r in runs.items()}
        with mock.patch.dict(api.server._BROWSER_RUNS, runs, clear=True), \
                mock.patch.object(api.server, "_CHAT", None):
            self.assertEqual(api.request("POST", "/api/chat-stop", {}), (200, {"ok": True}))
            coworker_run = api.server._BROWSER_RUNS[tokens["pm-1"]]
        sessions["owner"].close.assert_called_once()
        sessions["pm-1"].close.assert_not_called()
        self.assertFalse(coworker_run["live"].get("stopped"))
        self.assertIsNone(api.server.BROWSER_TOKENS.lookup(tokens["owner"]))
        self.assertIsNotNone(api.server.BROWSER_TOKENS.lookup(tokens["pm-1"]))
        self.assertTrue(owner_up.closed)
        self.assertFalse(coworker_up.closed)
        self.assertTrue(self.pool.is_live("acme", "profile-b", "bot", self.root))


class UntargetedStopSameBot(api.Base):
    def test_owner_stop_keeps_the_bot_browser_a_coworker_run_still_uses(self):
        browser.epoch("acme", self.root)
        browser.set_bot("acme", "profile-a", "act", "bot", self.root)
        shared = self.pool.get("acme", "profile-a", "bot", self.root)
        shared.forward(call(P + "open", url=PAGE), None)
        runs, sessions, tokens = {}, {}, {}
        for sender in ("owner", "pm-1"):
            token = api.server.BROWSER_TOKENS.mint({"ws": "acme", "bot": "profile-a", "runner_owner": sender})
            sessions[sender], tokens[sender] = mock.Mock(), token
            runs[token] = {"ws": "acme", "bot": "profile-a", "session": sessions[sender], "live": {},
                           "job": False, "token": token, "sender": sender}
        with mock.patch.dict(api.server._BROWSER_RUNS, runs, clear=True), \
                mock.patch.object(api.server, "_CHAT", None):
            self.assertEqual(api.request("POST", "/api/chat-stop", {}), (200, {"ok": True}))
            coworker_run = api.server._BROWSER_RUNS[tokens["pm-1"]]
        sessions["owner"].close.assert_called_once()
        sessions["pm-1"].close.assert_not_called()
        self.assertFalse(coworker_run["live"].get("stopped"))
        self.assertIsNone(api.server.BROWSER_TOKENS.lookup(tokens["owner"]))
        self.assertIsNotNone(api.server.BROWSER_TOKENS.lookup(tokens["pm-1"]))
        self.assertFalse(shared.closed)
        self.assertTrue(self.pool.is_live("acme", "profile-a", "bot", self.root))


class StopRun(api.Base):
    def test_stop_ends_actual_registered_bot_run_and_revokes_token(self):
        self.exercise_stop({"ws": "acme", "bot": "profile-a"})

    def test_existing_untargeted_stop_also_closes_active_browser_child(self):
        self.exercise_stop({})

    def exercise_stop(self, body):
        defs = {name: bots.Bot(name, name, name, "", "ceo", (), "") for name in ("profile-a", "profile-b")}
        browser.epoch("acme", self.root)
        browser.set_bot("acme", "profile-a", "act", "bot", self.root)
        sess = api.server.new_chat_session()
        sess.close = mock.Mock(wraps=sess.close)
        seen = []

        def ask(prompt, **kw):
            token = kw["browser_token"]
            seen.append(token)
            self.assertEqual(api.server._BROWSER_RUNS[token]["sender"], "owner")
            up = self.pool.get("acme", "profile-a", "bot", self.root)
            up.forward(call(P + "open", url=PAGE), None)
            self.assertFalse(up.closed)
            code, data = api.request("POST", "/api/chat-stop", body)
            self.assertEqual((code, data), (200, {"ok": True}))
            sess.close.assert_called_once()
            self.assertTrue(up.closed)
            self.assertIsNone(api.server.BROWSER_TOKENS.lookup(token))
            yield "delta", "HANDOFF @profile-b: demo"
            yield "done", {}

        sess.ask = ask
        h = api.server.Handler.__new__(api.server.Handler)
        h.wfile = io.BytesIO()
        h.send_response = h.send_header = h.end_headers = lambda *a, **k: None
        h.log_message = lambda *a: None
        h.server = SimpleNamespace(server_address=("127.0.0.1", api.PORT))
        with mock.patch.object(bots, "load_defs", return_value=defs), \
                mock.patch.object(bots, "ROOT_BOT", "profile-a"), \
                mock.patch.object(api.server, "get_chat_session", return_value=sess), \
                mock.patch.object(api.server.db, "posts", return_value=[]), \
                mock.patch.object(api.server.engine_registry, "pick", return_value=sess.engine):
            h._handle_ask({"messages": [{"role": "user", "content": "@profile-a demo"}], "project_slug": "acme"})
        self.assertEqual(len(seen), 1, h.wfile.getvalue().decode())
        sess.close.assert_called_once()
        self.assertFalse(api.server._BROWSER_RUNS)
        # The cut-off reply says it was stopped, on screen and (saved notice) after a reload.
        notices = [json.loads(line[len("data: "):]).get("notice") for line in h.wfile.getvalue().decode().splitlines()
                   if line.startswith("data: {")]
        self.assertEqual(notices.count("Stopped"), 1)
