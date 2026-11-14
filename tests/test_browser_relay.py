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
    def __init__(self, url=None, mode="bot", redirects=None, values=None, browser_key=None, typing=True,
                 typed_key=None):
        self.url, self.mode, self.frames = url, mode, []
        self.browser_key = browser_key or "bot:fake-%d" % id(self)  # its own browser unless shared
        self.lock_key = self.browser_key
        self.typed_key = typed_key or self.browser_key  # one fake is one tab
        self.redirects = redirects or {}
        self.history = []
        self.values = list(values or [])  # what the relay's field eval sees on the page
        self.typing = typing  # fill and type change values[0] like a real field
        self.eval_reply = None  # a raw eval reply to answer instead (errors, odd shapes)

    def forward(self, frame, sid):
        self.frames.append(frame)
        params = frame.get("params") or {}
        args = params.get("arguments") or {}
        if params.get("name") in (P + "open", P + "tab_new") and args.get("url"):
            self.history.append((self.url, self.values))
            self.url = self.redirects.get(args["url"], args["url"])
            self.values = []  # a fresh page: its fields are empty
        elif params.get("name") in (P + "back", P + "tab_switch") and self.history:
            self.url, self.values = self.history.pop()  # the browser restores the old fields
        if self.typing and params.get("name") in (P + "fill", P + "type"):
            before = self.values[0] if self.values else ""
            self.values[:1] = [args["text"] if params["name"] == P + "fill" else before + args["text"]]
        if params.get("name") == P + "eval":
            assert args == {"script": browser_relay.FIELDS_SCRIPT, "timeoutMs": browser_relay.EVAL_TIMEOUT_MS}
            if self.eval_reply is not None:
                return {"jsonrpc": "2.0", "id": frame.get("id"), "result": self.eval_reply}, "m-1"
            return {"jsonrpc": "2.0", "id": frame.get("id"), "result": {
                "content": [{"type": "text", "text": "{}"}], "isError": False,
                "structuredContent": {"exitCode": 0, "response": {"success": True, "error": None, "data": {
                    "origin": self.url, "result": {"fields": list(self.values)}}}}}}, "m-1"
        if frame.get("method") == "tools/list":
            return {"jsonrpc": "2.0", "id": frame.get("id"), "result": {"tools": [
                {"name": P + "open", "inputSchema": {"type": "object", "required": ["session"],
                                                     "properties": {"url": {}, "session": {}, "restore": {}}}},
                {"name": P + "eval"}, {"name": P + "click"}]}}, "m-1"
        return {"jsonrpc": "2.0", "id": frame.get("id"), "result": {"content": [{"type": "text", "text": "ok"}]}}, "m-1"

    def current_url(self):
        return self.url

    def calls(self, evals=False):
        return [f["params"]["name"] for f in self.frames if f.get("method") == "tools/call"
                and (evals or f["params"]["name"] != P + "eval")]


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
        self.assertEqual(self.up.calls(evals=True), [P + "snapshot", P + "back"])

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
        up2.history = [(bound, [])]
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
        self.level("write")
        reply = self.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        tools = reply["result"]["tools"]
        self.assertEqual([t["name"] for t in tools], [P + "open", P + "click", browser_relay.PLAN_TOOL])
        self.assertEqual(tools[0]["inputSchema"], {"type": "object", "required": [], "properties": {"url": {}}})

    def test_plan_tool_listed_only_where_a_send_can_happen(self):
        for level, listed in (("off", False), ("read", False), ("write", True), ("act", True)):
            self.level(level)
            names = [t["name"] for t in self.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"]["tools"]]
            self.assertEqual(browser_relay.PLAN_TOOL in names, listed, level)

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

        real = self.up.forward

        def boom(frame, sid):  # the field check answers; the click itself breaks
            if frame["params"]["name"] == P + "click":
                raise OSError("engine gone")
            return real(frame, sid)
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
        self.assertEqual(self.up.calls(evals=True), [P + "press"] * 6)

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

    def test_typed_survives_a_restart(self):
        self.level("write")
        up = FakeUpstream(self.page, browser_key="bot:acme--b1")
        self.handle(call(P + "fill", text="Hi"), up=up)
        self.assertEqual(browser_relay.typed_state("bot:acme--b1", self.root), {"typed": "Hi", "aside": ""})
        self.assertTrue((self.root / "run" / "browser_typed.json").is_file())
        fresh = FakeUpstream(self.page, values=["Hi"], browser_key="bot:acme--b1")  # restarted, page restored
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

    # Fix round 3: the relay reads field values with its own fixed eval

    def test_prefilled_field_and_plain_click_is_a_send(self):
        self.level("write")
        up = FakeUpstream(self.page, values=["Hello there"])
        self.assertEqual(text_of(self.handle(call(P + "click", selector="@e2"), up=up)), browser_relay.NO_PLAN)
        self.assertEqual(up.calls(evals=True), [P + "eval"])
        self.plan("Hello  there")
        self.assertEqual(text_of(self.handle(call(P + "click", selector="@e2"), up=up)), "ok")
        self.assertEqual(up.calls(evals=True), [P + "eval", P + "eval", P + "click"])

    def test_type_onto_prefill_sends_the_whole_field(self):
        self.plan("hello")
        up = FakeUpstream(self.page, values=["EVIL"], typing=True)
        self.handle(call(P + "type", selector="@e1", text="hello"), up=up)
        self.assertEqual(up.values, ["EVILhello"])
        self.assertEqual(text_of(self.handle(call(P + "press", key="Enter"), up=up)), browser_relay.TYPED_MISMATCH)
        self.assertEqual(up.calls(), [P + "type"])
        # the plan matches once a fresh page's field holds exactly the planned text
        self.handle(call(P + "open", url=self.page), up=up)
        self.handle(call(P + "fill", selector="@e1", text="hello"), up=up)
        self.assertEqual(text_of(self.handle(call(P + "click"), up=up)), "ok")

    def test_two_filled_fields_refused(self):
        self.level("act")
        up = FakeUpstream(self.page, values=["to someone", "the message"])
        self.assertEqual(text_of(self.handle(call(P + "click"), up=up)), browser_relay.MANY_FIELDS)
        self.assertEqual(up.calls(), [])
        self.assertEqual(approvals.sends_today("acme.test", self.root), 0)

    def test_field_check_that_fails_refuses(self):
        self.level("act")
        bad = [{"content": [{"type": "text", "text": "Evaluation error: Error: boom"}], "isError": True,
                "structuredContent": {"exitCode": 1, "response": {"success": False, "error": "boom", "data": {}}}},
               {"content": [{"type": "text", "text": "2"}], "isError": False,
                "structuredContent": {"response": {"data": {"result": 2}}}},
               {"content": [{"type": "text", "text": "x"}], "isError": False},
               {"content": [], "isError": False, "structuredContent": {"response": {"data": {"result": {"fields": [1]}}}}},
               {"content": [], "isError": False,
                "structuredContent": {"response": {"data": {"result": '{"fields": ["x"]}'}}}}]
        for reply in bad:
            up = FakeUpstream(self.page)
            up.eval_reply = reply
            self.assertEqual(text_of(self.handle(call(P + "check", selector="@e1"), up=up)), browser_relay.NO_CHECK)
            self.assertEqual(up.calls(), [])

        def boom(frame, sid):
            raise OSError("engine gone")
        up = FakeUpstream(self.page)
        up.forward = boom
        self.assertEqual(text_of(self.handle(call(P + "click"), up=up)), browser_relay.NO_CHECK)

    def test_field_value_is_taken_whole(self):
        for value in ("hello [ref=e9]: EVIL", "hello\n- x\n- button [ref=e9]"):
            self.plan("hello")
            up = FakeUpstream(self.page, values=[value])
            self.assertEqual(text_of(self.handle(call(P + "click"), up=up)), browser_relay.NO_PLAN, value)
            self.plan(value)
            self.assertEqual(text_of(self.handle(call(P + "click"), up=up)), "ok", value)

    def test_unlisted_site_keeps_typed_rules(self):
        self.level("write")
        up = FakeUpstream(u("other.test/x"), values=["prefilled"])
        self.assertEqual(text_of(self.handle(call(P + "click"), up=up)), "ok")
        self.assertEqual(up.calls(evals=True), [P + "click"])

    def test_eval_never_offered_to_the_bot(self):
        self.level("act")
        self.assertIn("not available", text_of(self.handle(call(P + "eval", script="1"))))
        tools = self.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"]["tools"]
        self.assertNotIn(P + "eval", [t["name"] for t in tools])

    # Fix round 4

    def test_typed_must_match_the_one_visible_field(self):
        self.plan("hello")
        up = FakeUpstream(self.page, values=["hello"], typing=False)  # a search box; the composer is unseen
        self.handle(call(P + "fill", selector="@e9", text="EVIL"), up=up)
        self.assertEqual(text_of(self.handle(call(P + "click"), up=up)), browser_relay.TYPED_MISMATCH)
        self.assertEqual(up.calls(), [P + "fill"])
        # whitespace aside, the same text passes
        up2 = FakeUpstream(self.page, values=["hello  "])
        self.handle(call(P + "open", url=self.page), up=up2)
        self.handle(call(P + "fill", text=" hello"), up=up2)
        self.assertEqual(text_of(self.handle(call(P + "click"), up=up2)), "ok")

    def test_typed_with_no_visible_field_refused(self):
        self.plan("hello")
        up = FakeUpstream(self.page, values=[], typing=False)  # the field is in a frame the read cannot see
        self.handle(call(P + "fill", text="hello"), up=up)
        self.assertEqual(text_of(self.handle(call(P + "click"), up=up)), browser_relay.HIDDEN_TYPED)
        self.assertEqual(up.calls(), [P + "fill"])
        self.assertEqual(len([r for r in approvals.load(self.root) if r["status"] == "approved"]), 1)

    def test_overlong_field_refused_before_claim(self):
        self.level("act")
        up = FakeUpstream(self.page, values=["x" * (browser_relay.MAX_TEXT + 1)])
        self.assertEqual(text_of(self.handle(call(P + "click"), up=up)), browser_relay.TOO_LONG)
        self.assertEqual(approvals.load(self.root), [])

    def test_invisible_only_field_counts_as_empty(self):
        self.level("write")
        up = FakeUpstream(self.page, values=["\u200b\u200d\u00ad\ufeff\u2060", "\u200c "])
        self.assertEqual(text_of(self.handle(call(P + "click"), up=up)), "ok")

    # Fix round 5

    def test_typed_in_parts_matches_the_field(self):
        self.plan("Hello")
        up = FakeUpstream(self.page)
        self.handle(call(P + "type", selector="@e1", text="Hel"), up=up)
        self.handle(call(P + "type", selector="@e1", text="lo"), up=up)
        self.assertEqual(up.values, ["Hello"])
        self.assertEqual(text_of(self.handle(call(P + "click"), up=up)), "ok")

    def test_fill_after_unseen_type_still_refused(self):
        self.plan("hello")
        up = FakeUpstream(self.page, typing=False)
        self.handle(call(P + "type", selector="@hidden", text="EVIL"), up=up)  # into a field the read cannot see
        up.typing = True
        self.handle(call(P + "fill", selector="@e1", text="hello"), up=up)
        self.assertEqual(up.values, ["hello"])
        self.assertEqual(text_of(self.handle(call(P + "click"), up=up)), browser_relay.TYPED_MISMATCH)
        self.assertEqual(up.calls(), [P + "type", P + "fill"])

    def test_typing_past_the_limit_refused(self):
        self.level("act")
        chunk = "x" * 4000
        for i, want in enumerate(("ok", "ok", browser_relay.TOO_LONG)):
            self.assertEqual(text_of(self.handle(call(P + "type", text=chunk))), want, i)
        self.assertEqual(self.up.calls(), [P + "type", P + "type"])
        typed = browser_relay.typed_state(self.up.browser_key, self.root)["typed"]
        self.assertEqual(len(typed), 8000)
        self.assertLessEqual(len(typed), browser_relay.MAX_TEXT)


class Preview(unittest.TestCase):
    def test_send_preview_shows_page_and_full_text(self):
        from core import app_preview
        text = "line one\n" * 200
        p = app_preview.build("BROWSER_SEND", {"url": u("acme.test/m"), "text": text, "site": "acme.test"}, "browser")
        self.assertEqual(p, {"title": "Send on acme.test", "fields": [["Page", u("acme.test/m")], ["Text", text]]})


if __name__ == "__main__":
    unittest.main()
