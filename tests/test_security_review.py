"""What a deep review of engines, runners and server mode found. Each test is one
way a hostile teammate, a poisoned page or a crash could have hurt."""
import http.server
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from core import bots, folders, runners
from dashboard import fileops, osctl, runner as runner_client, tokensave_bridge
from dashboard.engines import acp, api, api_tools, config, registry
from dashboard.engines.base import EngineRefused, TurnPolicy, TurnTokens, drain
from dashboard.engines.claude_code import ClaudeCodeEngine
from tests import test_server_people as sp


server = sp.server


class Tmp(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)


class WebFetchIsSafe(unittest.TestCase):
    def test_a_host_that_rebinds_to_this_machine_is_refused_at_connect(self):
        class Quiet(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"owner secret")

            def log_message(self, *a):
                pass
        srv = http.server.HTTPServer(("127.0.0.1", 0), Quiet)
        self.addCleanup(srv.server_close)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.shutdown)
        policy = TurnPolicy(web=True, web_domains=("rebind.example",))
        # The name check sees a public address; the real connection lands on loopback.
        real = api_tools.socket.create_connection
        with mock.patch.object(api_tools, "_public_host", return_value=True), \
                mock.patch.object(api_tools.socket, "create_connection",
                                  lambda a, *r, **k: real(("127.0.0.1", srv.server_port), *r, **k)):
            with self.assertRaises(api_tools.ToolError) as caught:
                api_tools.web_fetch(f"http://rebind.example:{srv.server_port}/", policy)
        self.assertNotIn("owner secret", str(caught.exception))

    def test_mapped_and_private_addresses_are_not_public(self):
        for ip in ("127.0.0.1", "10.0.0.1", "169.254.169.254", "::1", "::ffff:127.0.0.1", "fe80::1%en0", "224.0.0.1",
                   "2002:7f00:1::", "2002:a9fe:a9fe::", "64:ff9b::7f00:1", "64:ff9b::a9fe:a9fe",
                   "2001:0:4136:e378:8000:63bf:80ff:fffe"):
            self.assertFalse(api_tools._is_public_ip(ip), ip)
        self.assertTrue(api_tools._is_public_ip("93.184.216.34"))

    def test_a_hostile_page_cannot_freeze_the_tag_stripper(self):
        for page in (b"<script" * 280_000, b"<" * 2_000_000, b"<script>" + b"</scrip" * 280_000,
                     b"<style " * 280_000, b"<a " * 600_000):
            started = time.time()
            api_tools._page_text(page, "text/html")
            self.assertLess(time.time() - started, 2)

    def test_a_stray_less_than_keeps_the_rest_of_the_page(self):
        self.assertEqual(api_tools._page_text(b"<p>5</p> is < 6 and more", "text/html"), "5 is 6 and more")

    def test_the_stripper_still_reads_a_page(self):
        page = (b"<html><head><style>x{}</style><SCRIPT src=a>evil()</SCRIPT></head>"
                b"<body><h1>Hello &amp; welcome</h1><p>caf\xc3\xa9</p><scripted>kept</scripted>"
                b"<noscript>no</noscript>end</body></html>")
        self.assertEqual(api_tools._page_text(page, "text/html; charset=utf-8"), "Hello & welcome café kept end")
        self.assertEqual(api_tools._page_text(b"<b>raw</b>", "text/plain"), "<b>raw</b>")


class FileToolsAreSafe(Tmp):
    def policy(self, **kw):
        return TurnPolicy(read_dirs=(str(self.root),), **kw)

    def test_a_search_that_never_ends_is_killed_not_run_on_the_server(self):
        (self.root / "a.txt").write_text("text\n")
        started = time.time()
        with mock.patch.object(api_tools, "SEARCH_TIMEOUT", 1), \
                mock.patch.object(api_tools, "_GREP_WORKER", "import time\ntime.sleep(60)"):
            with self.assertRaises(api_tools.ToolError):
                api_tools.search_files(None, "text", self.root, self.policy())
        self.assertLess(time.time() - started, 5)

    def test_search_finds_and_skips_secrets(self):
        (self.root / "a.py").write_text("one\nneedle here\n")
        (self.root / ".env").write_text("needle secret\n")
        out = api_tools.search_files(None, "needle", self.root, self.policy())
        self.assertIn("a.py:2: needle here", out)
        self.assertNotIn(".env", out)

    def test_a_pipe_never_hangs_a_read_or_a_search(self):
        os.mkfifo(self.root / "pipe")
        with self.assertRaises(api_tools.ToolError):
            api_tools.read_file("pipe", self.root, self.policy())
        self.assertEqual(api_tools.search_files(None, "x", self.root, self.policy()), "(no matches)")


class ThreadsNeverGetStuck(unittest.TestCase):
    repair = staticmethod(api.ApiEngine._repair)

    def test_anthropic_tool_call_with_no_result_goes(self):
        msgs = [{"role": "user", "content": "go"},
                {"role": "assistant", "content": [{"type": "text", "text": "ok"},
                                                  {"type": "tool_use", "id": "t1", "name": "osctl", "input": {}}]},
                {"role": "user", "content": "next"}]
        self.assertEqual(self.repair(msgs)[1]["content"], [{"type": "text", "text": "ok"}])

    def test_answered_tool_calls_and_finished_searches_stay(self):
        msgs = [{"role": "user", "content": "go"},
                {"role": "assistant", "content": [{"type": "server_tool_use", "id": "s1", "name": "web_search"},
                                                  {"type": "web_search_tool_result", "tool_use_id": "s1", "content": []},
                                                  {"type": "tool_use", "id": "t1", "name": "osctl", "input": {}}]},
                {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "x"}]}]
        self.assertEqual(self.repair(msgs), msgs)

    def test_empty_replies_go(self):
        msgs = [{"role": "user", "content": "a"}, {"role": "assistant", "content": []},
                {"role": "user", "content": "b"}, {"role": "assistant", "content": [{"type": "text", "text": ""}]},
                {"role": "user", "content": "c"}, {"role": "assistant", "content": None},
                {"role": "user", "content": "d"}]
        self.assertEqual([m["content"] for m in self.repair(msgs)], ["a", "b", "c", "d"])

    def test_openai_calls_without_answers_go_with_their_orphans(self):
        call = {"id": "c1", "type": "function", "function": {"name": "osctl", "arguments": "{}"}}
        msgs = [{"role": "user", "content": "a"},
                {"role": "assistant", "content": "partly", "tool_calls": [call, {**call, "id": "c2"}]},
                {"role": "tool", "tool_call_id": "c1", "content": "x"},
                {"role": "user", "content": "b"}]
        self.assertEqual(self.repair(msgs), [msgs[0], {"role": "assistant", "content": "partly"}, msgs[3]])

    def test_openai_answered_calls_stay(self):
        call = {"id": "c1", "type": "function", "function": {"name": "osctl", "arguments": "{}"}}
        msgs = [{"role": "user", "content": "a"}, {"role": "assistant", "content": None, "tool_calls": [call]},
                {"role": "tool", "tool_call_id": "c1", "content": "x"}, {"role": "assistant", "content": "done"}]
        self.assertEqual(self.repair(msgs), msgs)


class AgentPermissions(unittest.TestCase):
    def test_a_shell_command_naming_the_tool_is_not_the_tool(self):
        for call in ({"title": "echo mcp__os__; rm -rf ~"}, {"title": "cat x # os__osctl", "kind": "other"},
                     {"title": "mcp__os__osctl", "kind": "execute"}, {"title": "os/osctl", "kind": "delete"},
                     {"title": "x", "toolCallId": "y-os__osctl"}, {"title": "", "kind": ""}, None):
            self.assertFalse(acp.is_own_tool(call), call)

    def test_the_real_tool_is(self):
        for call in ({"title": "mcp__os__osctl"}, {"title": "os__osctl", "kind": "other"},
                     {"title": "", "toolCallId": "os_osctl-12"}):
            self.assertTrue(acp.is_own_tool(call), call)


class Keys(Tmp):
    def row(self, **kw):
        return {"id": "acme", "format": "openai", "base_url": "https://one.example/v1",
                "models": {"fast": "m", "strong": "m"}, **kw}

    def test_a_saved_key_never_follows_a_new_address(self):
        config.upsert_provider(self.row(key="sk-secret"), self.root)
        config.upsert_provider(self.row(name="same address"), self.root)  # kept
        self.assertEqual(config.provider("acme", self.root)["key"], "sk-secret")
        with self.assertRaises(ValueError):
            config.upsert_provider(self.row(base_url="https://attacker.example/v1"), self.root)
        self.assertEqual(config.provider("acme", self.root)["base_url"], "https://one.example/v1")
        config.upsert_provider(self.row(base_url="https://two.example/v1", key="sk-new"), self.root)
        self.assertEqual(config.provider("acme", self.root)["key"], "sk-new")


class ProviderEdits(Tmp):
    def test_editing_a_custom_provider_keeps_its_format_address_and_key(self):
        config.upsert_provider({"id": "custom-one", "format": "openai", "base_url": "https://one.example/v1",
                                "key": "sk-1", "models": {"fast": "a", "strong": "b"}}, self.root)
        config.upsert_provider({"id": "custom-one", "models": {"fast": "c", "strong": "d"}}, self.root)  # what the form sends
        row = config.provider("custom-one", self.root)
        self.assertEqual((row["format"], row["base_url"], row["key"], row["models"]["fast"]),
                         ("openai", "https://one.example/v1", "sk-1", "c"))


class ApiKeyStaysHome(unittest.TestCase):
    def test_a_redirect_does_not_resend_the_key(self):
        seen = []

        class Redirect(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                seen.append((self.path, self.headers.get("Authorization")))
                self.send_response(302)
                self.send_header("Location", f"http://127.0.0.1:{srv.server_port}/stolen")
                self.send_header("Content-Length", "0")
                self.end_headers()

            do_GET = do_POST

            def log_message(self, *a):
                pass
        srv = http.server.HTTPServer(("127.0.0.1", 0), Redirect)
        self.addCleanup(srv.server_close)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.shutdown)
        engine = api.ApiEngine({"id": "x", "format": "openai", "key": "sk-1", "models": {"fast": "m"},
                                "base_url": f"http://127.0.0.1:{srv.server_port}"}, "/tmp")
        with self.assertRaises(api.ProviderError):
            engine.complete("p", "s", None)
        self.assertEqual([p for p, _ in seen], ["/chat/completions"])


class IndexSwapsWhole(Tmp):
    def build(self):
        return subprocess.run([sys.executable, str(Path(__file__).resolve().parent.parent / "index.py"), str(self.root)],
                              capture_output=True, text=True)

    def test_a_failed_build_leaves_the_last_good_index(self):
        (self.root / "projects/demo").mkdir(parents=True)
        project = self.root / "projects/demo/project.md"
        project.write_text("---\nname: Demo\npriority: primary\n---\n")
        self.assertEqual(self.build().returncode, 0)
        db_file = self.root / "database/data/os.db"
        good = db_file.read_bytes()
        project.write_text("---\nname: Demo\npriority: urgent\n---\n")  # the schema refuses this one mid-build
        failed = self.build()
        self.assertNotEqual(failed.returncode, 0)
        self.assertIn("INDEX FAILED", failed.stderr)
        self.assertEqual(db_file.read_bytes(), good)
        self.assertEqual([p.name for p in db_file.parent.iterdir() if ".tmp" in p.name], [])


class Jobs(Tmp):
    def job_engine(self, env):
        with mock.patch.dict(os.environ, {"LEOBOT_ENGINE": env}):
            return registry.for_job(self.root)

    def test_a_runner_job_never_lands_on_a_login(self):
        with self.assertRaises(EngineRefused):
            self.job_engine("runner:p-1")
        data = config.load(self.root)
        data["people"] = "claude-code"  # a hand-edited file
        config.save(data, self.root)
        with self.assertRaises(EngineRefused):
            self.job_engine("runner:p-1")

    def test_a_runner_job_uses_the_people_key(self):
        config.upsert_provider({"id": "ollama", "models": {"fast": "m"}}, self.root)
        config.set_choice("people", "api:ollama", None, self.root)
        self.assertEqual(self.job_engine("runner:p-1").id, "api:ollama")


class HostileRunner(unittest.TestCase):
    def setUp(self):
        self.hub = runners.Hub()
        self.turn = self.hub.submit("p1", {"chat": "c"})
        self.assertIsNotNone(self.hub.next("rn-1", "p1", wait=0))

    def drained(self):
        out = []
        while not self.turn.events.empty():
            out.append(self.turn.events.get())
        return out

    def test_bad_events_are_dropped_and_numbers_never_count(self):
        self.hub.events("rn-1", self.turn.id, [
            ["delta", "hi"], ["delta", {"x": 1}], ["tool", "t" * 999], ["evil", "x"], "junk", ["done"],
            ["done", {"tokens": 10**12, "cost": "free", "result": "fake"}], ["done", "not a dict"]])
        got = self.drained()
        self.assertEqual(got[0], ("delta", "hi"))
        self.assertEqual(len(got[1][1]), 200)
        for kind, payload in got[2:]:
            self.assertEqual((kind, payload["tokens"], payload["cost"]), ("done", None, None))

    def test_a_trickle_cannot_outlast_the_time_limit(self):
        self.turn.created -= runners.MAX_TURN_SECONDS + 1
        self.hub.events("rn-1", self.turn.id, [["delta", "x"]])
        events = list(self.hub.wait_events(self.turn, poll=0.01))
        self.assertEqual(events[0][0], "error")

    def test_a_flood_is_cut_off(self):
        stop = self.hub.events("rn-1", self.turn.id, [["delta", "x" * (runners.MAX_TURN_CHARS + 1)]])
        self.assertTrue(stop)
        self.assertEqual(self.drained()[0][0], "error")
        self.assertTrue(self.hub.events("rn-1", self.turn.id, [["delta", "more"]]))

    def test_a_runner_that_never_ends_cannot_hold_a_thread(self):
        self.turn.created -= runners.MAX_TURN_SECONDS + 1
        with mock.patch.object(self.hub, "seen_recently", return_value=True):
            events = list(self.hub.wait_events(self.turn, poll=0.01))
        self.assertEqual(events[-1][0], "error")

    def test_stop_ends_the_wait_even_if_the_runner_stays_silent(self):
        self.hub.cancel(self.turn)
        with mock.patch.object(self.hub, "seen_recently", return_value=True):
            self.assertEqual(list(self.hub.wait_events(self.turn, poll=0.01)), [])

    def test_token_count_per_person_is_capped(self):
        with tempfile.TemporaryDirectory() as tmp:
            for _ in range(runners.MAX_PER_PERSON):
                runners.create(tmp, "p1")
            with self.assertRaises(ValueError):
                runners.create(tmp, "p1")
            runners.create(tmp, "p2")


class RunnerClient(unittest.TestCase):
    def test_token_never_travels_in_the_clear(self):
        self.assertEqual(runner_client.server_url("https://leobot.example/"), "https://leobot.example")
        self.assertEqual(runner_client.server_url("http://127.0.0.1:8766"), "http://127.0.0.1:8766")
        for bad in ("http://leobot.example", "ftp://x", "leobot.example", "http://127.0.0.1.evil.example"):
            with self.assertRaises(ValueError):
                runner_client.server_url(bad)

    def test_a_redirect_does_not_carry_the_token(self):
        seen = []

        class Redirect(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                seen.append((self.path, self.headers.get("Authorization")))
                self.send_response(302)
                self.send_header("Location", f"http://127.0.0.1:{srv.server_port}/stolen")
                self.send_header("Content-Length", "0")
                self.end_headers()

            do_GET = do_POST

            def log_message(self, *a):
                pass
        srv = http.server.HTTPServer(("127.0.0.1", 0), Redirect)
        self.addCleanup(srv.server_close)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.shutdown)
        client = runner_client.Client(f"http://127.0.0.1:{srv.server_port}", "myr_x")
        with self.assertRaises(Exception):
            client.post("/api/runner/next", {})
        self.assertEqual([p for p, _ in seen], ["/api/runner/next"])

    def test_the_job_cannot_reach_the_command_line(self):
        good = {"chat": "0" * 8 + "-" + "0" * 27, "turn": "a" * 32, "rail": "r", "text": "t", "model": "sonnet"}
        runner_client.check_job(good)
        for bad in ({"model": "--dangerously-skip-permissions"}, {"chat": "--resume"}, {"turn": "../x"},
                    {"rail": None}, {"model": 7}):
            with self.assertRaises(ValueError):
                runner_client.check_job({**good, **bad})

    def test_web_domains_cannot_smuggle_a_permission_rule(self):
        policy = runner_client.job_policy({"policy": {"web": True, "web_domains": [
            "ok.example", "a.example) Bash(*", "x y", 7]}})
        self.assertEqual(policy.web_domains, ("ok.example",))
        self.assertTrue(policy.shared)


class ChattyChild(unittest.TestCase):
    def test_a_child_flooding_stderr_cannot_freeze_a_turn(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake = Path(tmp) / "claude"
            fake.write_text("#!/bin/sh\ncat >/dev/null\nhead -c 1000000 /dev/zero >&2\n"
                            "echo '{\"type\":\"result\",\"result\":\"ok\",\"subtype\":\"success\"}'\n")
            fake.chmod(0o755)
            session = mock.Mock(repo_dir=tmp, rail="r", session_id="x", _started=False, model="haiku")
            events = []

            def run():
                events.extend(ClaudeCodeEngine(str(fake)).turn(session, "hi", TurnPolicy(), "haiku", TurnTokens()))
            worker = threading.Thread(target=run, daemon=True)
            worker.start()
            worker.join(10)
            self.assertFalse(worker.is_alive(), "the turn froze")
            self.assertEqual(events[0][0], "done")

    def test_drain_returns_what_was_said(self):
        proc = subprocess.Popen([sys.executable, "-c", "import sys; sys.stderr.write('oops')"],
                                stderr=subprocess.PIPE, text=True)
        text = drain(proc.stderr)
        proc.wait()
        self.assertEqual(text(), "oops")


class SharedThreadOsctl(Tmp):
    """osctl as a hostile teammate's runner calls it: any arguments it likes."""

    def setUp(self):
        super().setUp()
        for target in (fileops, ):
            patch = mock.patch.object(target, "ROOT", self.root)
            patch.start()
            self.addCleanup(patch.stop)
        patch = mock.patch.object(fileops, "reindex", lambda: None)
        patch.start()
        self.addCleanup(patch.stop)
        for ws in ("demo", "acme"):
            d = self.root / "projects" / ws
            (d / "profiles" / f"{ws}-p" / "channels" / f"{ws}-c").mkdir(parents=True)
            (d / "products" / f"{ws}-prod").mkdir(parents=True)
            (d / "project.md").write_text(f"---\nname: {ws}\nkind: venture\nstatus: idea\n---\nbody\n")

    def test_a_line_break_cannot_link_a_folder(self):
        evil = "x\nlocal_folder: /etc\nstatus: hacked"
        for field in ("name", "kind", "status", "hours_per_week"):
            with self.assertRaises(fileops.ActionError):
                fileops.update_project("demo", {field: evil})
        self.assertNotIn("local_folder", (self.root / "projects/demo/project.md").read_text())

    def test_every_line_ending_python_splits_on_is_refused(self):
        for bad in ("a\nb", "a\rb", "a\x0bb", "a\x0cb", "a\x1cb", "a\x85b", "a b", "a b", "a---b"):
            with self.assertRaises(fileops.ActionError, msg=repr(bad)):
                fileops.update_project("demo", {"name": bad})

    def test_other_header_writers_refuse_too(self):
        evil = "x\nproject: acme"
        calls = (lambda: fileops.create_profile("demo", "new-p", {"name": evil}),
                 lambda: fileops.create_product("demo", "new-prod", {"name": evil}),
                 lambda: fileops.create_channel("demo-p", "new-c", evil),
                 lambda: fileops.update_channel("demo-c", {"name": evil}))
        for call in calls:
            with self.assertRaises(fileops.ActionError):
                call()

    def test_a_pasted_line_break_in_a_box_stays_on_one_line(self):
        fileops.update_channel("demo-c", {"bio": "one\nlocal_folder: /etc\u2028two"})
        text = (self.root / "projects/demo/profiles/demo-p/channels/demo-c/channel.md").read_text()
        self.assertIn("bio: one local_folder: /etc two\n", text)
        self.assertNotIn("\nlocal_folder", text)

    def test_a_bad_priority_cannot_stop_the_index(self):
        with self.assertRaises(fileops.ActionError):
            fileops.update_project("demo", {"priority": "urgent"})
        fileops.update_project("demo", {"priority": "secondary"})

    def test_a_duplicate_slug_cannot_stop_the_index(self):
        for call in (lambda: fileops.create_project("demo-p", {}),
                     lambda: fileops.create_channel("demo-p", "demo-prod", "instagram"),
                     lambda: fileops.create_product("demo", "demo-c", {}),
                     lambda: fileops.create_profile("demo", "demo", {})):
            with self.assertRaises(fileops.ActionError):
                call()

    def test_an_unknown_entity_cannot_stop_the_index(self):
        with self.assertRaises(fileops.ActionError):
            fileops.create_milestone({"title": "t", "date": "2030-01-01", "entity": "nobody"})
        self.assertFalse((self.root / "portfolio" / "milestones.json").exists())
        self.assertTrue(fileops.create_milestone({"title": "t", "date": "2030-01-01", "entity": "demo"})["id"])

    def activities(self, text):
        path = self.root / "portfolio" / "activities.md"
        path.parent.mkdir(exist_ok=True)
        path.write_text(text)
        return path

    def test_mark_done_stays_in_its_workspace(self):
        path = self.activities("## Activities\n- [ ] Launch — id: a1 — entity: acme\n"
                               "- [ ] Launch — id: a2 — entity: demo\n- [ ] Launch party — id: a3 — entity: demo\n")
        fileops.mark_activity_done("Launch", "demo")
        self.assertEqual(path.read_text(), "## Activities\n- [ ] Launch — id: a1 — entity: acme\n"
                                           "- [x] Launch — id: a2 — entity: demo\n- [ ] Launch party — id: a3 — entity: demo\n")

    def test_mark_done_cannot_tick_everything_or_another_workspace(self):
        text = "## Activities\n- [ ] Launch — id: a1 — entity: acme\n"
        path = self.activities(text)
        for title, entity in (("", "acme"), ("", "demo"), ("Launch", "demo"), ("Lau", "acme")):
            with self.assertRaises(fileops.ActionError):
                fileops.mark_activity_done(title, entity)
        self.assertEqual(path.read_text(), text)

    def test_the_dashboard_can_still_finish_a_hand_written_row(self):
        path = self.activities("## Activities\n- [ ] Old task — id: a1\n- [ ] Old task — id: a2 — entity: acme\n")
        fileops.mark_activity_done("Old task", "")
        self.assertEqual(path.read_text(), "## Activities\n- [x] Old task — id: a1\n- [ ] Old task — id: a2 — entity: acme\n")

    def test_a_shared_thread_never_sees_the_linked_folder_path(self):
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"LEOBOT_SHARED": "1"}), mock.patch("sys.stdout", out):
            osctl._emit({"project": {"entity": {"slug": "demo", "local_folder": "/srv/app"}}, "rows": [{"local_folder": "/x"}]})
        self.assertNotIn("local_folder", out.getvalue())

    def test_shared_osctl_never_shows_folders(self):
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"LEOBOT_SHARED": "1"}), mock.patch("sys.stdout", out):
            osctl._emit({"project": {"folders": [{"path": "/srv/app"}]}, "rows": [{"folders": ["/srv/x"]}]})
        self.assertNotIn("/srv/app", out.getvalue())
        self.assertNotIn("/srv/x", out.getvalue())

    def test_osctl_in_a_bot_turn_cannot_add_a_folder(self):
        app = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, app, True)
        for argv in (["update-project", "--slug", "demo", "--local-folder", str(app)],
                     ["create-project", "--slug", "newco", "--local-folder", str(app)]):
            with mock.patch.dict(os.environ, {"LEOBOT_WORKSPACE": "demo"}), \
                    mock.patch("sys.stdout", io.StringIO()) as out:
                self.assertEqual(osctl.main(argv), 1, argv)
            self.assertIn("only you can link", out.getvalue())
        self.assertEqual(folders.all_paths("demo", self.root), [])

    def test_an_activity_cannot_write_a_row_for_another_workspace(self):
        with self.assertRaises(fileops.ActionError):
            fileops.create_activity({"entity": "demo", "title": "x\n- [ ] y — id: a9 — entity: acme"})
        out = fileops.create_activity({"entity": "demo", "title": "x — entity: acme"})
        self.assertEqual((out["entity"], out["title"]), ("demo", "x - entity: acme"))

    def test_a_milestone_from_a_workspace_stays_in_it(self):
        with mock.patch.dict(os.environ, {"LEOBOT_WORKSPACE": "demo", "LEOBOT_SHARED": "1"}), \
                mock.patch.object(osctl, "_owners", _NoCache(lambda: {"demo": "demo", "acme": "acme"})), \
                mock.patch("sys.stdout", io.StringIO()):
            self.assertEqual(osctl.main(["create-milestone", "--title", "t", "--date", "2030-01-01"]), 0)
        rows = json.loads((self.root / "portfolio" / "milestones.json").read_text())["milestones"]
        self.assertEqual(rows[0]["entity"], "demo")


class _NoCache:
    def __init__(self, fn):
        self.fn = fn

    def __call__(self):
        return self.fn()

    def cache_clear(self):
        pass


class PrivateMemoryStaysPrivate(Tmp):
    """Like a DM: what the owner tells a bot privately never reaches a thread people can see."""

    def setUp(self):
        super().setUp()
        (self.root / "projects" / "demo").mkdir(parents=True)
        bots.remember("cmo", "PRIVATE-NOTE fire the agency", "demo", self.root)
        bots.remember("cmo", "TEAM-NOTE launch is in March", "demo", self.root, shared=True)

    def test_the_two_memories_never_mix(self):
        self.assertIn("PRIVATE-NOTE", bots.read_memory("cmo", "demo", self.root))
        self.assertNotIn("TEAM-NOTE", bots.read_memory("cmo", "demo", self.root))
        self.assertIn("TEAM-NOTE", bots.read_memory("cmo", "demo", self.root, shared=True))
        self.assertNotIn("PRIVATE-NOTE", bots.read_memory("cmo", "demo", self.root, shared=True))

    def test_forgetting_touches_only_its_own_memory(self):
        line = bots.read_memory("cmo", "demo", self.root, tail=0, shared=True)
        self.assertFalse(bots.forget("cmo", line, "demo", self.root))
        self.assertTrue(bots.forget("cmo", line, "demo", self.root, shared=True))
        self.assertIn("PRIVATE-NOTE", bots.read_memory("cmo", "demo", self.root))

    def test_hq_has_no_team_memory(self):
        with self.assertRaises(ValueError):
            bots.memory_path("cmo", None, self.root, shared=True)
        self.assertEqual(bots.read_memory("cmo", None, self.root, shared=True), "")

    def prompt(self, shared, readonly=False):
        team = bots.team(self.root, "demo", {"slug": "demo"})
        with mock.patch.object(server, "ROOT", self.root):
            return server._bot_block(team, "cmo", None, "demo", fresh=True, shared=shared, readonly=readonly)

    def test_a_shared_prompt_carries_only_the_team_memory(self):
        text = self.prompt(shared=True)
        self.assertIn("TEAM-NOTE", text)
        self.assertNotIn("PRIVATE-NOTE", text)
        self.assertNotIn("remember --bot", text)

    def test_a_private_prompt_carries_only_the_private_memory(self):
        text = self.prompt(shared=False)
        self.assertIn("PRIVATE-NOTE", text)
        self.assertNotIn("TEAM-NOTE", text)

    def test_a_consulted_teammate_in_a_shared_thread_too(self):
        team = bots.team(self.root, "demo", {"slug": "demo"})
        with mock.patch.object(server, "ROOT", self.root):
            text = server._teammate_prompt(team, "cmo", None, "q", "said", [], "demo", None, shared=True)
        self.assertIn("TEAM-NOTE", text)
        self.assertNotIn("PRIVATE-NOTE", text)


TRAVERSAL = ("../x", "../../x", "../../../x", "../../../../x", "..", "../..", "/etc/passwd", "a/b", "a\\b", ".",
             "a\x00b", "a\nb", "..%2fx", "x y", "demo/../../x", "\u2025/x", "\uff0e\uff0e/x")


class PathsStayInside(Tmp):
    """The code scanner flags these paths and cannot see their validators, so its alerts are
    dismissed: these tests are what watches them instead."""

    def setUp(self):
        super().setUp()
        self.data = self.root / "data"
        (self.data / "projects" / "demo").mkdir(parents=True)
        (self.data / "projects" / "demo" / "project.md").write_text("---\nname: d\n---\n")
        (self.root / "x.md").write_text("CANARY")
        (self.data / "x.md").write_text("CANARY")
        (self.data / "projects" / "x.md").write_text("CANARY")
        self.before = self.tree()
        for target, name, value in ((fileops, "ROOT", self.data), (fileops, "reindex", lambda: None)):
            patch = mock.patch.object(target, name, value)
            patch.start()
            self.addCleanup(patch.stop)

    def tree(self):
        return sorted((str(p), p.read_text() if p.is_file() else "") for p in self.root.rglob("*"))

    def test_memory_refuses_every_traversal(self):
        for bad in TRAVERSAL:
            for shared in (False, True):
                for args in ((bad, "demo"), ("cos", bad)):
                    with self.assertRaises(ValueError, msg=repr(args)):
                        bots.memory_path(args[0], args[1], self.data, shared)
                    self.assertEqual(bots.read_memory(args[0], args[1], self.data, tail=0, shared=shared), "", repr(args))
                    with self.assertRaises(ValueError, msg=repr(args)):
                        bots.remember(args[0], "x", args[1], self.data, shared=shared)
        self.assertEqual(self.tree(), self.before)

    def test_project_writers_refuse_every_traversal(self):
        calls = (lambda v: fileops.create_project(v, {}), lambda v: fileops.update_project(v, {"name": "n"}),
                 lambda v: fileops.create_profile("demo", v, {}), lambda v: fileops.create_profile(v, "ok-slug", {}),
                 lambda v: fileops.create_product("demo", v, {}), lambda v: fileops.create_product(v, "ok-slug", {}))
        for bad in TRAVERSAL + ("x" * 300,):
            for call in calls:
                with self.assertRaises(fileops.ActionError, msg=repr(bad)):
                    call(bad)
        self.assertEqual(self.tree(), self.before)

    def test_project_writers_never_follow_a_symlink_out(self):
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "project.md").write_text("CANARY")
        (self.data / "projects" / "dangling").symlink_to(self.root / "nowhere")
        (self.data / "projects" / "linked").symlink_to(outside)
        before = self.tree()
        for call in (lambda: fileops.create_project("dangling", {}),
                     lambda: fileops.create_project("linked", {}),
                     lambda: fileops.update_project("linked", {"name": "n"})):
            with self.assertRaises(fileops.ActionError):
                call()
        self.assertFalse((self.root / "nowhere").exists())
        self.assertEqual(self.tree(), before)


class LinkedAppCode(unittest.TestCase):
    def test_shared_threads_ask_the_index_for_no_source_code(self):
        with mock.patch.object(tokensave_bridge, "_run_tool", return_value="x") as tool:
            tokensave_bridge.context("demo", "what does it do", include_code=False)
            self.assertIs(tool.call_args.args[2]["include_code"], False)
            tokensave_bridge.context("demo", "what does it do")
            self.assertIs(tool.call_args.args[2]["include_code"], True)


class Files(Tmp):
    def test_a_token_file_others_can_read_is_refused(self):
        token = self.root / "token"
        token.write_text("myr_x")
        token.chmod(0o644)
        with self.assertRaises(SystemExit) as caught:
            runner_client.main(["--server", "https://leobot.example", "--token-file", str(token)])
        self.assertIn("chmod 600", str(caught.exception))
        self.assertEqual(stat.S_IMODE(token.stat().st_mode), 0o644)



class NoBotLinksAFolder(unittest.TestCase):
    """CodeQL #124's source is the owner-typed folder path. A bot reaches osctl only through the
    relay or the api engine's osctl backend; both bind LEOBOT_WORKSPACE, so --local-folder is refused
    before anything is read or written."""

    def test_relay_osctl_refuses_local_folder_for_every_kind_of_turn(self):
        from dashboard import os_relay
        for ctx in ({"ws": "hq", "shared": False, "engine": "api:x"},
                    {"ws": "demo", "shared": True},
                    {"ws": "demo", "shared": False, "readonly": True}):
            for cmd in ("update-project", "create-project"):
                text, failed = os_relay.run_osctl(
                    [cmd, "--slug", "no-such-slug-zz", "--local-folder", tempfile.gettempdir()], "", ctx)
                self.assertTrue(failed, (cmd, ctx))
                self.assertRegex(text, "only you can link|read-only: you were consulted", (cmd, ctx))


class OddTokensAreRefusedNotCrashed(sp.Base):
    """A cookie or bearer token that decodes to non-ASCII text is just a wrong token:
    str compare_digest raises on it, which used to crash the request."""

    def test_non_ascii_owner_cookie_is_forbidden(self):
        for cookie in ('leobot_auth="\\351\\351"', "leobot_auth=\xe9"):
            with self.subTest(cookie):
                code, _, _ = sp.request("GET", "/api/me", headers={"Cookie": cookie})
                self.assertEqual(code, 403)

    def test_non_ascii_bearer_is_unauthorized_on_both_relays(self):
        live = server.os_relay.TOKENS.mint({"ws": "acme", "readonly": True})
        self.addCleanup(server.os_relay.TOKENS.revoke, live)
        live_apps = server.apps_relay.TOKENS.mint({"ws": "acme"})
        self.addCleanup(server.apps_relay.TOKENS.revoke, live_apps)
        for path in ("/api/internal/os/mcp", "/api/internal/apps/mcp"):
            with self.subTest(path):
                code, _, _ = sp.request("POST", path, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                                        headers={"Authorization": "Bearer \xe9\xe9"})
                self.assertIn(code, (401, 403))


if __name__ == "__main__":
    unittest.main()
