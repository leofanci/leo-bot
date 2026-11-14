"""The browser relay gate: levels, site modes, send plans, daily caps."""
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from core import approvals, browser
from dashboard import browser_relay, taint

CTX = {"ws": "acme", "bot": "b1", "chat": "c1", "room": "acme/general", "person": "ceo"}
P = "agent_browser_"


def u(rest):  # built from parts so no literal URL host sits in a tracked file
    return "https" + "://" + rest


class FakeUpstream(browser_relay.Upstream):
    def __init__(self, url=None, mode="bot", redirects=None, fields="", browser_key=None):
        self.url, self.mode, self.frames = url, mode, []
        self.browser_key = browser_key or "bot:fake-%d" % id(self)  # its own browser unless shared
        self.redirects = redirects or {}
        self.history = []
        self.fields = fields  # snapshot lines for the page's text fields (docs/browser.md format)
        self.snapshot_fails = False

    def forward(self, frame, sid):
        self.frames.append(frame)
        params = frame.get("params") or {}
        args = params.get("arguments") or {}
        if params.get("name") in (P + "open", P + "tab_new") and args.get("url"):
            self.history.append(self.url)
            self.url = self.redirects.get(args["url"], args["url"])
        elif params.get("name") == P + "back" and self.history:
            self.url = self.history.pop()
        if params.get("name") == P + "snapshot":
            if self.snapshot_fails:
                return {"jsonrpc": "2.0", "id": frame.get("id"), "result": {"content": [], "isError": True}}, "m-1"
            snap = (self.fields + "\n" if self.fields else "") + '- button "Send" [ref=e2]'
            return {"jsonrpc": "2.0", "id": frame.get("id"), "result": {
                "content": [{"type": "text", "text": snap}], "isError": False,
                "structuredContent": {"exitCode": 0, "response": {"data": {"snapshot": snap}}}}}, "m-1"
        if frame.get("method") == "tools/list":
            return {"jsonrpc": "2.0", "id": frame.get("id"), "result": {"tools": [
                {"name": P + "open", "inputSchema": {"type": "object", "required": ["session"],
                                                     "properties": {"url": {}, "session": {}, "restore": {}}}},
                {"name": P + "eval"}, {"name": P + "click"}]}}, "m-1"
        return {"jsonrpc": "2.0", "id": frame.get("id"), "result": {"content": [{"type": "text", "text": "ok"}]}}, "m-1"

    def current_url(self):
        return self.url

    def calls(self, snapshots=False):
        return [f["params"]["name"] for f in self.frames if f.get("method") == "tools/call"
                and (snapshots or f["params"]["name"] != P + "snapshot")]


def call(name, rid=7, **arguments):
    return {"jsonrpc": "2.0", "id": rid, "method": "tools/call", "params": {"name": name, "arguments": arguments}}


def text_of(reply):
    return "\n".join(c.get("text", "") for c in reply["result"]["content"])


class Gate(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        browser.set_site("acme.test", None, 20, self.root)
        browser.set_site("demo.test", "my_chrome", None, self.root)
        self.page = u("acme.test/messages/42")
        self.up = FakeUpstream(self.page)
        self.turn = taint.Turn()

    def tearDown(self):
        self.tmp.cleanup()

    def level(self, level, ws="acme", bot="b1"):
        browser.set_bot(ws, bot, level, "bot", self.root)

    def handle(self, frame, up=None, **ctx):
        reply, _ = browser_relay.handle(frame, {**CTX, "taint": self.turn, **ctx}, "s-1", up or self.up, self.root)
        return reply

    def plan(self, text="Hello there", url=None, approve=True):
        self.level("write")
        reply = self.handle(call(browser_relay.PLAN_TOOL, sends=[{"url": url or self.page, "text": text}]))
        rows = [r for r in approvals.pending(self.root) if r["action"]["toolkit"] == approvals.BROWSER]
        if approve:
            for r in rows:
                approvals.approve_send(r["id"], self.root)
        return reply, rows

    def test_unknown_tool_refused(self):
        self.level("act")
        for name in (P + "eval", P + "close", "other", None):
            reply = self.handle(call(name))
            self.assertTrue(reply["result"]["isError"])
            self.assertIn("not available", text_of(reply))
        self.assertEqual(self.up.frames, [])

    def test_off_refuses_everything(self):
        reply = self.handle(call(P + "snapshot"))
        self.assertEqual(text_of(reply), browser_relay.OFF)
        self.assertEqual(self.up.frames, [])

    def test_read_level_refuses_typing(self):
        self.level("read")
        for name in (P + "fill", P + "click", browser_relay.PLAN_TOOL):
            self.assertIn("may only read", text_of(self.handle(call(name, text="x"))))
        self.assertEqual(self.up.frames, [])
        self.handle(call(P + "snapshot"))
        self.handle(call(P + "back"))
        self.assertEqual(self.up.calls(snapshots=True), [P + "snapshot", P + "back"])

    def test_click_without_typing_forwards(self):
        self.level("write")
        reply = self.handle(call(P + "click", selector="@e1"))
        self.assertEqual(text_of(reply), "ok")
        self.assertEqual(self.up.calls(), [P + "click"])

    def test_type_then_any_click_is_a_send(self):
        self.level("write")
        self.handle(call(P + "fill", selector="@e1", text="Hi"))
        reply = self.handle(call(P + "click", selector="@e9"))
        self.assertEqual(text_of(reply), browser_relay.NO_PLAN)
        self.assertEqual(self.up.calls(), [P + "fill"])
        # the text is still in the field: a second click is still a send
        self.assertEqual(text_of(self.handle(call(P + "press", key="Enter"))), browser_relay.NO_PLAN)
        self.assertEqual(self.up.calls(), [P + "fill"])

    def test_open_sets_typed_aside_and_back_brings_it_back(self):
        self.level("write")
        self.handle(call(P + "fill", selector="@e1", text="Hi"))
        self.handle(call(P + "open", url=u("acme.test/other")))
        self.assertEqual(text_of(self.handle(call(P + "click", selector="@e2"))), "ok")
        self.handle(call(P + "back"))
        self.assertEqual(text_of(self.handle(call(P + "click", selector="@e2"))), browser_relay.NO_PLAN)
        self.handle(call(P + "tab_new", url=u("acme.test/x")))
        self.assertEqual(text_of(self.handle(call(P + "click", selector="@e2"))), "ok")
        self.handle(call(P + "tab_switch", tab="1"))
        self.assertEqual(text_of(self.handle(call(P + "click", selector="@e2"))), browser_relay.NO_PLAN)

    def test_typed_belongs_to_the_browser_not_the_session(self):
        self.level("write")
        browser_relay.handle(call(P + "fill", text="Hi"), {**CTX}, "s-1", self.up, self.root)
        reply, _ = browser_relay.handle(call(P + "click"), {**CTX}, "s-2", self.up, self.root)
        self.assertEqual(text_of(reply), browser_relay.NO_PLAN)

    def test_bound_site_other_mode_refused_before_forward(self):
        self.level("act")
        bound = u("www.demo.test/inbox")
        for frame in (call(P + "open", url=bound), call(P + "tab_new", url=bound)):
            reply = self.handle(frame)
            self.assertEqual(text_of(reply), browser_relay.SITE_MODE.format(
                site="demo.test", mode_label="the owner's Chrome", other_label="the bot browser"))
        self.assertEqual(self.up.frames, [])
        # the same site opens in the owner's Chrome
        chrome = FakeUpstream(self.page, mode="my_chrome")
        self.assertEqual(text_of(self.handle(call(P + "open", url=bound), up=chrome)), "ok")

    def test_url_without_host_or_not_web_refused(self):
        self.level("act")
        for url in (u("[bad"), u("%zz.test/"), "javascript" + ":alert(1)", "file" + ":///etc/hosts",
                    "acme.test/x", 5):
            self.assertEqual(text_of(self.handle(call(P + "open", url=url))), browser_relay.BAD_URL, url)
        self.assertEqual(self.up.frames, [])
        self.assertEqual(text_of(self.handle(call(P + "open", url="about:blank"))), "ok")

    def test_redirect_to_bound_site_is_hidden_and_left(self):
        self.level("act")
        bound = u("demo.test/home")
        up = FakeUpstream(self.page, redirects={u("acme.test/go"): bound})
        reply = self.handle(call(P + "open", url=u("acme.test/go")), up=up)
        self.assertIn("demo.test is set to open only in", text_of(reply))
        self.assertEqual(up.frames[-1]["params"]["arguments"], {"url": "about:blank"})
        # a history move that lands there is hidden too
        up2 = FakeUpstream(bound)
        up2.history = [bound]
        up2.url = self.page
        self.assertIn("demo.test is set", text_of(self.handle(call(P + "back"), up=up2)))

    def test_blocked_page_allows_only_navigation(self):
        self.level("act")
        up = FakeUpstream(u("demo.test/home"))
        up.forward = lambda frame, sid, f=up.forward: (f(frame, sid) if (frame["params"]["arguments"].get("url")
                                                                         != "about:blank") else (None, sid))
        self.assertIn("demo.test is set", text_of(self.handle(call(P + "reload"), up=up)))
        self.assertIn("demo.test is set", text_of(self.handle(call(P + "get_title"), up=up)))
        self.assertEqual(text_of(self.handle(call(P + "open", url=u("acme.test/a")), up=up)), "ok")
        self.assertEqual(text_of(self.handle(call(P + "get_title"), up=up)), "ok")

    def test_engine_wide_args_stripped(self):
        self.level("act")
        self.handle(call(P + "screenshot", path="/tmp/x.png", screenshotDir="/tmp", session="other",
                         namespace="n", restore=True, restoreCheckFn="x()", allowedDomains=["*"],
                         extraArgs=["--x"], caCert="c", full=True))
        self.assertEqual(self.up.frames[0]["params"]["arguments"], {"full": True})

    def test_open_drops_headed_and_idle_timeout(self):
        self.level("act")
        self.handle(call(P + "open", url=self.page, headed=True, idleTimeout=0, timeoutMs=999999))
        self.assertEqual(self.up.frames[0]["params"]["arguments"], {"url": self.page, "timeoutMs": 120000})

    def test_undeclared_key_dropped_when_schema_known(self):
        self.level("act")
        self.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        self.handle(call(P + "open", url=self.page, surprise=1, timeoutMs=5))
        self.assertEqual(self.up.frames[-1]["params"]["arguments"], {"url": self.page})
        # unknown schema: only the always-dropped keys go
        self.handle(call(P + "snapshot", surprise=1, session="x"))
        self.assertEqual(self.up.frames[-1]["params"]["arguments"], {"surprise": 1})

    def test_tools_list_filters_and_adds_plan(self):
        reply = self.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        tools = reply["result"]["tools"]
        self.assertEqual([t["name"] for t in tools], [P + "open", P + "click", browser_relay.PLAN_TOOL])
        self.assertEqual(tools[0]["inputSchema"], {"type": "object", "required": [], "properties": {"url": {}}})

    def test_cap_counts_all_workspaces(self):
        for i in range(20):
            approvals.log_app_run(self.root, ws="demo", room=None, chat=None, bot="b%d" % i, person="ceo",
                                  toolkit=approvals.BROWSER, slug=approvals.BROWSER_SEND,
                                  arguments={"url": self.page, "text": "x", "site": "acme.test"},
                                  account=None, status="done", result=approvals.SENT)
        self.level("act")
        self.handle(call(P + "fill", text="Hi"))
        reply = self.handle(call(P + "click"))
        self.assertEqual(text_of(reply), browser_relay.CAPPED.format(site="acme.test", daily=20))
        self.assertEqual(self.up.calls(), [P + "fill"])
        # a day later the count is back to zero
        later = time.time() + approvals.DAY + 1
        with mock.patch("core.approvals.time.time", return_value=later):
            self.assertEqual(approvals.sends_today("acme.test", self.root), 0)

    def test_cap_applies_at_write(self):
        browser.set_site("acme.test", None, 1, self.root)
        self.plan("One")
        self.plan("Two")
        self.handle(call(P + "fill", text="One"))
        self.assertEqual(text_of(self.handle(call(P + "click"))), "ok")
        self.handle(call(P + "fill", text="Two"))
        self.assertEqual(text_of(self.handle(call(P + "click"))), browser_relay.CAPPED.format(site="acme.test", daily=1))

    def test_act_send_forwards_and_logs_site_of_current_page(self):
        self.level("act")
        self.handle(call(P + "fill", text="Hi"))
        self.assertEqual(text_of(self.handle(call(P + "click"))), "ok")
        self.assertEqual(self.up.calls(), [P + "fill", P + "click"])
        self.assertEqual(approvals.sends_today("acme.test", self.root), 1)
        row = approvals.load(self.root)[-1]
        self.assertEqual(row["action"]["arguments"], {"url": self.page, "text": "Hi", "site": "acme.test"})
        # typed reset after the send: the next click is a plain click
        self.assertEqual(text_of(self.handle(call(P + "click"))), "ok")

    def test_send_with_unknown_page_refused(self):
        for level in ("write", "act"):
            self.level(level)
            up = FakeUpstream(None)
            self.handle(call(P + "fill", text="Hi"), up=up)
            self.assertEqual(text_of(self.handle(call(P + "click"), up=up)), browser_relay.NO_PAGE)
            self.assertEqual(up.calls(), [P + "fill"])

    def test_plan_text_whitespace_matches_word_does_not(self):
        self.plan("Hello  there,\n friend")
        self.handle(call(P + "fill", text="Hello there, friend!"))
        self.assertEqual(text_of(self.handle(call(P + "click"))), browser_relay.NO_PLAN)
        up = FakeUpstream(self.page)
        self.handle(call(P + "open", url=u("acme.test/messages/42/#top")), up=up)  # sets the old text aside
        self.handle(call(P + "fill", text=" Hello there,  friend "), up=up)
        self.assertEqual(text_of(self.handle(call(P + "click"), up=up)), "ok")
        self.assertEqual(up.calls(), [P + "open", P + "fill", P + "click"])

    def test_plan_other_page_does_not_match(self):
        self.plan("Hi")
        up = FakeUpstream(u("acme.test/messages/43"))
        self.handle(call(P + "fill", text="Hi"), up=up)
        self.assertEqual(text_of(self.handle(call(P + "click"), up=up)), browser_relay.NO_PLAN)

    def test_plan_item_used_once(self):
        self.plan("Hi")
        self.handle(call(P + "fill", text="Hi"))
        self.assertEqual(text_of(self.handle(call(P + "click"))), "ok")
        self.handle(call(P + "fill", text="Hi"))
        self.assertEqual(text_of(self.handle(call(P + "click"))), browser_relay.NO_PLAN)
        done = [r for r in approvals.load(self.root) if r["status"] == "done"]
        self.assertEqual(len(done), 1)
        self.assertEqual(done[0]["result"], approvals.SENT)

    def test_plan_item_needs_approval(self):
        self.plan("Hi", approve=False)
        self.handle(call(P + "fill", text="Hi"))
        self.assertEqual(text_of(self.handle(call(P + "click"))), browser_relay.NO_PLAN)

    def test_plan_item_expires_after_7_days(self):
        self.plan("Hi")
        later = time.time() + approvals.PLAN_TTL + 1
        self.assertIsNone(approvals.take_send(self.root, ws="acme", bot="b1", url=self.page, text="Hi", now=later))
        self.assertIsNotNone(approvals.take_send(self.root, ws="acme", bot="b1", url=self.page, text="Hi"))

    def test_plan_other_bot_cannot_use(self):
        self.plan("Hi")
        self.level("write", bot="b2")
        self.handle(call(P + "fill", text="Hi"), bot="b2")
        self.assertEqual(text_of(self.handle(call(P + "click"), bot="b2")), browser_relay.NO_PLAN)
        self.assertIsNone(approvals.take_send(self.root, ws="demo", bot="b1", url=self.page, text="Hi"))

    def test_report_job_never_sends(self):
        self.level("act")
        self.handle(call(P + "fill", text="Hi"), report=True)
        self.assertEqual(text_of(self.handle(call(P + "click"), report=True)), browser_relay.REPORT_ONLY)
        self.assertEqual(text_of(self.handle(call(browser_relay.PLAN_TOOL, sends=[{"url": self.page, "text": "x"}]),
                                             report=True)), browser_relay.REPORT_ONLY)
        self.assertEqual(self.up.calls(), [P + "fill"])
        self.assertEqual(approvals.pending(self.root), [])

    def test_plan_tool_refused_when_off(self):
        reply = self.handle(call(browser_relay.PLAN_TOOL, sends=[{"url": self.page, "text": "x"}]))
        self.assertEqual(text_of(reply), browser_relay.OFF)

    def test_forward_marks_outside(self):
        self.level("read")
        self.assertFalse(self.turn.outside)
        self.handle(call(P + "eval"))
        self.assertFalse(self.turn.outside)
        self.handle(call(P + "snapshot"))
        self.assertTrue(self.turn.outside)

    def test_plan_tool_holds_one_batch(self):
        self.level("write")
        reply = self.handle(call(browser_relay.PLAN_TOOL, sends=[
            {"url": self.page, "text": "One"}, {"url": u("acme.test/messages/43"), "text": "Two"}]))
        self.assertEqual(text_of(reply), browser_relay.HELD_PLAN.format(n=2))
        self.assertFalse(reply["result"]["isError"])
        rows = approvals.pending(self.root)
        self.assertEqual(len(rows), 2)
        self.assertEqual(len({r["batch"] for r in rows}), 1)
        self.assertEqual({r["action"]["slug"] for r in rows}, {approvals.BROWSER_SEND})
        self.assertEqual(rows[0]["preview"]["title"], "Send on acme.test")
        self.assertEqual(self.up.frames, [])
        for bad in ([], [{"url": self.page}], [{"url": "acme.test", "text": "x"}], "x"):
            self.assertTrue(self.handle(call(browser_relay.PLAN_TOOL, sends=bad))["result"]["isError"])

    def test_broken_send_marks_plan_failed(self):
        self.plan("Hi")
        self.handle(call(P + "fill", text="Hi"))

        def boom(frame, sid):
            raise OSError("engine gone")
        self.up.forward = boom
        reply = self.handle(call(P + "click"))
        self.assertEqual(text_of(reply), browser_relay.SEND_BROKE)
        row = [r for r in approvals.load(self.root) if r["action"]["toolkit"] == approvals.BROWSER][0]
        self.assertEqual((row["status"], row["result"]), ("failed", approvals.STALE_NOTE))

    # Fix round 1 regressions

    def test_press_allows_only_keys_that_type_nothing(self):
        self.level("act")
        for key in ("H", "i", "a", " ", "Space", "Control+V", "Meta+v", "Alt+x", "Control+a", "Foo", "", None, 5):
            self.assertEqual(text_of(self.handle(call(P + "press", key=key))), browser_relay.BAD_KEY, key)
        self.assertEqual(self.up.frames, [])
        for key in ("Tab", "Shift+Tab", "ArrowDown", "Escape", "PageUp", "End"):
            self.assertEqual(text_of(self.handle(call(P + "press", key=key))), "ok", key)
        self.assertEqual(self.up.calls(snapshots=True), [P + "press"] * 6)

    def test_enter_is_a_send(self):
        self.level("write")
        self.assertEqual(text_of(self.handle(call(P + "press", key="H"))), browser_relay.BAD_KEY)
        self.handle(call(P + "type", text="Hi"))
        for key in ("Enter", "Shift+Enter", "Control+Enter", "NumpadEnter"):
            self.assertEqual(text_of(self.handle(call(P + "press", key=key))), browser_relay.NO_PLAN, key)
        self.assertEqual(self.up.calls(), [P + "type"])

    def test_type_text_must_be_one_line_string(self):
        self.level("act")
        for text in ("a\nb", "a\rb", "a\x00", "a\tb", "a\u2028b", "\x1b[A"):
            self.assertEqual(text_of(self.handle(call(P + "fill", text=text))), browser_relay.CONTROL_TEXT)
        for text in (None, 5, ["a"]):
            self.assertEqual(text_of(self.handle(call(P + "type", text=text))), browser_relay.BAD_TEXT)
        self.assertEqual(text_of(self.handle(call(P + "fill", selector="@e1"))), browser_relay.BAD_TEXT)
        self.assertEqual(self.up.frames, [])

    def test_failed_and_running_sends_count(self):
        def row(status):
            approvals.log_app_run(self.root, ws="demo", room=None, chat=None, bot="b9", person="ceo",
                                  toolkit=approvals.BROWSER, slug=approvals.BROWSER_SEND,
                                  arguments={"url": self.page, "text": "x", "site": "acme.test"},
                                  account=None, status=status, result="r")
        row("done")
        row("failed")
        status, _ = approvals.claim_send(self.root, ws="demo", bot="b9", url=self.page, text="y", planned=False)
        self.assertEqual(status, "ok")
        self.assertEqual(approvals.sends_today("acme.test", self.root), 3)

    def test_timeout_clamped_and_non_finite_dropped(self):
        self.level("act")
        for given, want in ((5, {"timeoutMs": 1000}), (50000, {"timeoutMs": 50000}), (10 ** 9, {"timeoutMs": 120000}),
                            (float("nan"), {}), (float("inf"), {}), ("30", {}), (True, {})):
            self.handle(call(P + "wait_ms", timeoutMs=given))
            self.assertEqual(self.up.frames[-1]["params"]["arguments"], want, given)

    def test_cap_counts_by_page_site_not_stored_site(self):
        for i in range(20):
            approvals.log_app_run(self.root, ws="demo", room=None, chat=None, bot="b%d" % i, person="ceo",
                                  toolkit=approvals.BROWSER, slug=approvals.BROWSER_SEND,
                                  arguments={"url": u("www.acme.test/m"), "text": "x", "site": "unlisted.test"},
                                  account=None, status="done", result=approvals.SENT)
        self.assertEqual(approvals.sends_today("acme.test", self.root), 20)
        self.plan("Hi", url=u("www.acme.test/messages/42"))
        status, _ = approvals.claim_send(self.root, ws="acme", bot="b1", url=u("www.acme.test/messages/42"),
                                         text="Hi", daily=20, planned=True)
        self.assertEqual(status, "capped")
        rec = approvals.take_send(self.root, ws="acme", bot="b1", url=u("www.acme.test/messages/42"), text="Hi")
        self.assertEqual(rec["action"]["arguments"]["site"], "acme.test")

    def test_cap_and_record_are_atomic(self):
        browser.set_site("acme.test", None, 1, self.root)
        self.level("act")
        other = FakeUpstream(self.page)
        seen = []
        real = self.up.forward

        def forward(frame, sid):  # while the first send is in flight, a second browser tries
            if frame["params"]["name"] == P + "click":
                seen.append(approvals.sends_today("acme.test", self.root))
                self.handle(call(P + "fill", text="Two"), up=other, bot="b2")
                seen.append(text_of(self.handle(call(P + "click"), up=other, bot="b2")))
            return real(frame, sid)
        self.up.forward = forward
        browser.set_bot("acme", "b2", "act", "bot", self.root)
        self.handle(call(P + "fill", text="One"))
        self.assertEqual(text_of(self.handle(call(P + "click"))), "ok")
        self.assertEqual(seen, [1, browser_relay.CAPPED.format(site="acme.test", daily=1)])
        rows = [r for r in approvals.load(self.root) if r["action"]["toolkit"] == approvals.BROWSER]
        self.assertEqual([r["status"] for r in rows], ["done"])

    def test_text_already_in_a_field_counts_as_typed_on_listed_site(self):
        self.level("write")
        up = FakeUpstream(self.page, fields='- textbox "Msg" [ref=e6]: Hello\nthere\n- searchbox "Find" [ref=e5]: q'
                                          '\n- textbox "Pass" [ref=e7]: \u2022\u2022\u2022')
        self.assertEqual(text_of(self.handle(call(P + "click", selector="@e2"), up=up)), browser_relay.NO_PLAN)
        self.assertEqual(up.calls(snapshots=True), [P + "snapshot"])
        self.plan("Hello there")
        self.assertEqual(text_of(self.handle(call(P + "click", selector="@e2"), up=up)), "ok")
        # contenteditable counts too
        up2 = FakeUpstream(self.page, fields='- generic "Editor" [ref=e1] editable [contenteditable]: Rich')
        self.assertEqual(text_of(self.handle(call(P + "check", selector="@e1"), up=up2)), browser_relay.NO_PLAN)
        # an unlisted site is not snapshotted
        up3 = FakeUpstream(u("other.test/x"), fields='- textbox "Msg" [ref=e6]: Hello')
        self.assertEqual(text_of(self.handle(call(P + "click"), up=up3)), "ok")
        self.assertEqual(up3.calls(snapshots=True), [P + "click"])

    def test_unreadable_snapshot_refuses_click(self):
        self.level("act")
        self.up.snapshot_fails = True
        self.assertEqual(text_of(self.handle(call(P + "click"))), browser_relay.NO_CHECK)
        self.assertEqual(self.up.calls(), [])

    def test_typed_survives_a_restart(self):
        self.level("write")
        up = FakeUpstream(self.page, browser_key="bot:acme--b1")
        self.handle(call(P + "fill", text="Hi"), up=up)
        self.assertEqual(browser_relay.typed_state("bot:acme--b1", self.root), {"typed": "Hi", "aside": ""})
        self.assertTrue((self.root / "run" / "browser_typed.json").is_file())
        fresh = FakeUpstream(self.page, browser_key="bot:acme--b1")  # a new engine child after a restart
        self.assertEqual(text_of(self.handle(call(P + "click"), up=fresh)), browser_relay.NO_PLAN)

    def test_read_with_url_refused(self):
        self.level("read")
        for url in (self.page, "about:blank", None):
            self.assertEqual(text_of(self.handle(call(P + "read", url=url))), browser_relay.READ_URL)
        self.assertEqual(self.up.frames, [])
        self.assertEqual(text_of(self.handle(call(P + "read"))), "ok")

    def test_unknown_page_after_navigation_fails_closed(self):
        self.level("act")
        target = u("acme.test/lost")
        up = FakeUpstream(self.page, redirects={target: None, "about:blank": None})
        self.assertEqual(text_of(self.handle(call(P + "open", url=target), up=up)), browser_relay.UNKNOWN_PAGE)
        self.assertEqual(up.frames[-1]["params"]["arguments"], {"url": "about:blank"})
        self.assertEqual(text_of(self.handle(call(P + "get_title"), up=up)), browser_relay.UNKNOWN_PAGE)
        self.assertEqual(text_of(self.handle(call(P + "open", url=u("acme.test/a")), up=up)), "ok")
        self.assertEqual(text_of(self.handle(call(P + "get_title"), up=up)), "ok")

    def test_plan_matches_query_not_fragment(self):
        self.plan("Hi", url=u("acme.test/m?to=1"))
        up = FakeUpstream(u("acme.test/m?to=2"))
        self.handle(call(P + "fill", text="Hi"), up=up)
        self.assertEqual(text_of(self.handle(call(P + "click"), up=up)), browser_relay.NO_PLAN)
        up2 = FakeUpstream(u("acme.test/m"))
        self.handle(call(P + "open", url=u("acme.test/m?to=1#end")), up=up2)
        self.handle(call(P + "fill", text="Hi"), up=up2)
        self.assertEqual(text_of(self.handle(call(P + "click"), up=up2)), "ok")
        row = [r for r in approvals.load(self.root) if r["status"] == "done"][0]
        self.assertEqual(row["preview"]["fields"][0], ["Page", u("acme.test/m?to=1")])

    def test_method_not_a_string_is_invalid(self):
        for method in (5, None, ["tools/call"]):
            reply = self.handle({"jsonrpc": "2.0", "id": 3, "method": method})
            self.assertEqual(reply["error"]["code"], -32600)
        self.assertEqual(self.up.frames, [])

    def test_nested_url_and_href_dropped(self):
        self.level("act")
        self.handle(call(P + "wait_for_selector", href="x", opts={"url": "y", "k": 1, "deep": [{"href": "z", "j": 2}]}))
        self.assertEqual(self.up.frames[-1]["params"]["arguments"], {"opts": {"k": 1, "deep": [{"j": 2}]}})

    # Fix round 2 regressions

    def snap(self, fields):
        up = FakeUpstream(self.page, fields=fields)
        return browser_relay._field_text(up.forward(call(P + "snapshot"), None)[0])

    def test_snapshot_reads_odd_fields_or_fails_closed(self):
        reads = {
            '- textbox "Reply to Jo "X" Doe" [ref=e3]: EVIL': "EVIL",
            '- textbox "Reply to\nJo" [ref=e3]: EVIL': "EVIL",
            '- generic "Ed" [ref=e1] editable [contenteditable=plaintext-only]: EVIL': "EVIL",
            '- combobox "Write a message" [ref=e4]: EVIL': "EVIL",
            '- textbox "Msg" [ref=e6]: line one\nline two': "line one line two",
            '- combobox [expanded=false, ref=e4]: a\n  - option "a" [selected, ref=e7]': "",
            '- searchbox "Find" [ref=e5]: q': "",
            '- button "Open the textbox "now"" [ref=e2]': "",
            '- text: use the combobox below': "",
            '- generic "Ed" contenteditable: EVIL': "EVIL",
        }
        for fields, want in reads.items():
            self.assertEqual(self.snap(fields), want, fields)
        for fields in ('- textbox "Odd" {ref=e3}: EVIL', '- textbox "a" [ref=e3] : EVIL',
                       'textbox somewhere: EVIL', '- button "Go" [ref=e2]\ntextbox x: EVIL'):
            self.assertIsNone(self.snap(fields), fields)
        self.level("write")
        up = FakeUpstream(self.page, fields='- textbox "Odd" {ref=e3}: EVIL')
        self.assertEqual(text_of(self.handle(call(P + "click"), up=up)), browser_relay.NO_CHECK)
        self.assertEqual(up.calls(), [])

    def test_shared_owner_chrome_keeps_one_typed_record(self):
        browser.set_bot("acme", "b1", "write", "my_chrome", self.root)
        browser.set_bot("acme", "b2", "act", "my_chrome", self.root)
        chrome = FakeUpstream(u("other.test/inbox"), mode="my_chrome",
                              browser_key=browser_relay.browser_key_for("my_chrome", "acme", "b1"))
        self.assertEqual(chrome.browser_key, browser_relay.browser_key_for("my_chrome", "demo", "b2"))
        held = []
        real = chrome.forward

        def forward(frame, sid):
            held.append(browser_relay._browser_lock("my_chrome").locked())
            return real(frame, sid)
        chrome.forward = forward
        self.handle(call(P + "fill", text="Hi"), up=chrome)
        self.assertEqual(text_of(self.handle(call(P + "click"), up=chrome)), browser_relay.NO_PLAN)
        self.assertEqual(text_of(self.handle(call(P + "click"), up=chrome, bot="b2")), "ok")
        self.assertEqual(approvals.sends_today("other.test", self.root), 1)
        self.assertTrue(held and all(held))
        # on a capped listed site b2's click is held to the cap like any send
        browser.set_site("other.test", None, 1, self.root)
        self.handle(call(P + "fill", text="Again"), up=chrome)
        self.assertEqual(text_of(self.handle(call(P + "click"), up=chrome, bot="b2")),
                         browser_relay.CAPPED.format(site="other.test", daily=1))

    def test_corrupt_typed_file_refuses_sends_then_heals(self):
        self.level("act")
        path = self.root / "run" / "browser_typed.json"
        path.parent.mkdir(parents=True)
        path.write_text("{not json")
        self.assertIsNone(browser_relay.typed_state(self.up.browser_key, self.root))
        self.assertEqual(text_of(self.handle(call(P + "click"))), browser_relay.NO_CHECK)
        self.assertEqual(self.up.calls(), [])
        self.handle(call(P + "fill", text="Hi"))
        self.assertEqual(browser_relay.typed_state(self.up.browser_key, self.root), {"typed": "Hi", "aside": ""})

    def test_end_send_after_housekeeping_keeps_failed(self):
        status, rec = approvals.claim_send(self.root, ws="acme", bot="b1", url=self.page, text="x", planned=False)
        later = time.time() + approvals.RUNNING_STALE + 1
        with mock.patch("core.approvals.time.time", return_value=later):
            approvals.pending(self.root)  # housekeeping fails the stuck run
        out = approvals.end_send(rec["id"], True, self.root)
        self.assertEqual((out["status"], out["result"]), ("failed", approvals.STALE_NOTE))

    def test_count_reads_sites_once(self):
        for i in range(5):
            approvals.log_app_run(self.root, ws="demo", room=None, chat=None, bot="b%d" % i, person="ceo",
                                  toolkit=approvals.BROWSER, slug=approvals.BROWSER_SEND,
                                  arguments={"url": self.page, "text": "x", "site": "acme.test"},
                                  account=None, status="done", result=approvals.SENT)
        with mock.patch("core.approvals.browser.sites", wraps=browser.sites) as spy:
            self.assertEqual(approvals.sends_today("acme.test", self.root), 5)
        self.assertEqual(spy.call_count, 1)


class Preview(unittest.TestCase):
    def test_send_preview_shows_page_and_full_text(self):
        from core import app_preview
        text = "line one\n" * 200
        p = app_preview.build("BROWSER_SEND", {"url": u("acme.test/m"), "text": text, "site": "acme.test"}, "browser")
        self.assertEqual(p, {"title": "Send on acme.test", "fields": [["Page", u("acme.test/m")], ["Text", text]]})


if __name__ == "__main__":
    unittest.main()
