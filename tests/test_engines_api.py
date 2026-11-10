"""The api engine, against a fake provider on localhost: streaming, tools, errors, history."""
import json
import os
import stat
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest import mock

import dashboard.chat_session as cs
from dashboard.engines import api, api_tools, config, registry
from dashboard.engines.base import EngineRefused, TurnPolicy, TurnTokens
from dashboard.engines.claude_code import SECRET_FILE_GLOBS


def sse(events):
    return "".join(f"data: {json.dumps(e)}\n\n" for e in events).encode()


def anthropic_text(text, stop="end_turn"):
    return [{"type": "message_start", "message": {"usage": {"input_tokens": 10}}},
            {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": text}},
            {"type": "content_block_stop", "index": 0},
            {"type": "message_delta", "delta": {"stop_reason": stop}, "usage": {"output_tokens": 5}}]


def anthropic_tool(name, arguments, tool_id="tu_1"):
    return [{"type": "message_start", "message": {"usage": {"input_tokens": 10}}},
            {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": "", "signature": ""}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "signature_delta", "signature": "sig"}},
            {"type": "content_block_start", "index": 1, "content_block": {"type": "tool_use", "id": tool_id, "name": name, "input": {}}},
            {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": json.dumps(arguments)}},
            {"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 5}}]


def openai_text(text):
    return [{"choices": [{"delta": {"content": text}}]},
            {"choices": [{"delta": {}, "finish_reason": "stop"}]},
            {"choices": [], "usage": {"prompt_tokens": 7, "completion_tokens": 3}}]


def openai_tool(name, arguments):
    return [{"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "c1", "function": {"name": name, "arguments": ""}}]}}]},
            {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": json.dumps(arguments)}}]}}]},
            {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]}]


class Fake:
    """A provider: serves queued replies, records requests."""

    def __init__(self):
        fake = self
        self.replies, self.requests = [], []

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                fake.requests.append({"path": self.path, "body": body, "headers": {k.lower(): v for k, v in self.headers.items()}})
                reply = fake.replies.pop(0)
                if isinstance(reply, int):
                    self.send_response(reply)
                    self.send_header("Content-Length", "2")
                    self.end_headers()
                    self.wfile.write(b"{}")
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                self.wfile.write(sse(reply))
        self.srv = HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


class Base(unittest.TestCase):
    fmt = "anthropic"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.fake = Fake()
        self.addCleanup(self.fake.close)
        self.provider = {"id": "demo", "name": "Demo", "format": self.fmt, "base_url": self.fake.url, "key": "sk-secret-1234",
                         "models": {"fast": "m-fast", "strong": "m-strong"}}
        self.engine = api.ApiEngine(self.provider, self.root)
        self.osctl_calls = []
        self.backends = dict(api.BACKENDS)
        api.BACKENDS.clear()
        api.set_backends(osctl=self.run_osctl)
        self.addCleanup(lambda: (api.BACKENDS.clear(), api.BACKENDS.update(self.backends)))

    def run_osctl(self, args, stdin, ctx):
        self.osctl_calls.append((args, stdin, ctx))
        return "osctl says ok", False

    def session(self, **kw):
        sess = cs.ChatSession(repo_dir=str(self.root), rail="RAIL")
        sess.engine = self.engine
        sess.workspace = "acme"
        for k, v in kw.items():
            setattr(sess, k, v)
        return sess


class Anthropic(Base):
    def test_text_turn_maps_to_events_with_usage_and_auth(self):
        self.fake.replies = [anthropic_text("hello")]
        ev = list(self.session().ask("hi", model="sonnet"))
        self.assertEqual(ev[0], ("delta", "hello"))
        self.assertEqual(ev[-1][0], "done")
        self.assertEqual(ev[-1][1]["tokens"], 15)
        req = self.fake.requests[0]
        self.assertEqual(req["path"], "/v1/messages")
        self.assertEqual(req["body"]["model"], "m-strong")
        self.assertEqual(req["body"]["system"], "RAIL")
        self.assertEqual(req["headers"]["x-api-key"], "sk-secret-1234")

    def test_tool_loop_runs_osctl_bound_to_the_workspace_and_keeps_thinking_blocks(self):
        self.fake.replies = [anthropic_tool("osctl", {"args": ["get-project"], "stdin": "x"}), anthropic_text("done")]
        ev = list(self.session(shared=True).ask("hi", model="haiku"))
        self.assertIn(("tool", "osctl"), ev)
        self.assertEqual(self.osctl_calls[0][0], ["get-project"])
        ctx = self.osctl_calls[0][2]
        self.assertEqual((ctx["ws"], ctx["shared"], ctx["engine"]), ("acme", True, "api:demo"))
        second = self.fake.requests[1]["body"]["messages"]
        self.assertEqual(second[1]["content"][0]["signature"], "sig")  # thinking block unchanged
        self.assertNotIn("_json", second[1]["content"][1])
        self.assertEqual(second[2]["content"][0]["tool_use_id"], "tu_1")
        self.assertEqual(second[2]["content"][0]["content"], "osctl says ok")

    def test_shared_turn_is_offered_only_osctl(self):
        self.fake.replies = [anthropic_text("x")]
        list(self.session(shared=True, deny_dirs=("/x",)).ask("hi", read_dirs=("/tmp",), edit_dirs=("/tmp",)))
        self.assertEqual([t["name"] for t in self.fake.requests[0]["body"]["tools"]], ["osctl"])

    def test_shared_turn_refuses_read_file_even_if_the_model_asks(self):
        (self.root / "secret.txt").write_text("nope")
        self.fake.replies = [anthropic_tool("read_file", {"path": "secret.txt"}), anthropic_text("done")]
        list(self.session(shared=True).ask("hi"))
        result = self.fake.requests[1]["body"]["messages"][2]["content"][0]
        self.assertTrue(result["is_error"])
        self.assertNotIn("nope", result["content"])

    def test_owner_turn_gets_read_file_and_web_search_only_when_on(self):
        self.fake.replies = [anthropic_text("x"), anthropic_text("y")]
        s = self.session()
        list(s.ask("hi", model="m-sonnet"))
        names = [t["name"] for t in self.fake.requests[0]["body"]["tools"]]
        self.assertEqual(names, ["osctl", "read_file"])
        list(s.ask("hi", model="m-sonnet", with_web=True, web_domains=("docs.example",)))
        tools = self.fake.requests[1]["body"]["tools"]
        self.assertEqual([t["name"] for t in tools], ["osctl", "read_file", "web_fetch", "web_search"])
        self.assertEqual(tools[-1]["type"], "web_search_20260209")

    def test_history_persists_across_restore_and_delete_removes_it(self):
        self.fake.replies = [anthropic_text("first"), anthropic_text("second")]
        s = self.session()
        list(s.ask("one"))
        files = list((self.root / "chats").glob("*.jsonl"))
        self.assertEqual(len(files), 1)
        list(self.session(session_id=s.session_id).ask("two"))
        msgs = self.fake.requests[1]["body"]["messages"]
        self.assertEqual([m["role"] for m in msgs], ["user", "assistant", "user"])
        self.assertEqual(msgs[0]["content"], "one")
        from dashboard import chat_store
        with mock.patch.object(chat_store, "CHATS_DIR", self.root / "chats"):
            chat_store.delete(s.session_id)
        self.assertEqual(list((self.root / "chats").glob("*.jsonl")), [])

    def test_teammate_keeps_nothing_on_disk(self):
        self.fake.replies = [anthropic_text("x")]
        list(self.session(ephemeral=True).ask("hi"))
        self.assertFalse((self.root / "chats").exists())

    def test_bad_key_is_a_clear_error_and_no_retry(self):
        self.fake.replies = [401]
        ev = list(self.session().ask("hi"))
        self.assertEqual(ev[-1][0], "error")
        self.assertIn("refused the API key", ev[-1][1])
        self.assertNotIn("sk-secret", ev[-1][1])
        self.assertEqual(len(self.fake.requests), 1)

    def test_overload_is_retried(self):
        self.fake.replies = [503, anthropic_text("ok")]
        with mock.patch.object(api.time, "sleep"):
            ev = list(self.session().ask("hi"))
        self.assertEqual(ev[0], ("delta", "ok"))

    def test_refusal_ends_the_turn_with_a_note(self):
        self.fake.replies = [anthropic_text("", stop="refusal")]
        ev = list(self.session().ask("hi"))
        self.assertTrue(any(k == "delta" and "declined" in p for k, p in ev))
        self.assertEqual(ev[-1][0], "done")

    def test_tool_calls_are_capped(self):
        self.fake.replies = [anthropic_tool("osctl", {"args": ["x"]}) for _ in range(api.MAX_TOOL_CALLS + 2)]
        ev = list(self.session().ask("hi"))
        self.assertEqual(len(self.osctl_calls), api.MAX_TOOL_CALLS)
        self.assertEqual(ev[-1][0], "done")

    def test_trim_keeps_first_and_newest_and_never_splits_a_tool_result(self):
        big = "x" * (api.HISTORY_CHARS // 2 + 10)
        msgs = [{"role": "user", "content": "SNAPSHOT"}, {"role": "assistant", "content": big},
                {"role": "user", "content": big}, {"role": "assistant", "content": [{"type": "thinking", "thinking": "t"}, {"type": "tool_use"}]},
                {"role": "user", "content": [{"type": "tool_result"}]}, {"role": "user", "content": "now"}]
        out = self.engine._trim(msgs)
        self.assertEqual(out[0]["content"], "SNAPSHOT")
        self.assertEqual(out[-1]["content"], "now")
        self.assertEqual(out[2]["role"], "user")

    def test_complete_sends_no_tools(self):
        self.fake.replies = [anthropic_text('{"a":1}')]
        self.assertEqual(self.engine.complete("PROMPT", "INPUT", "sonnet"), '{"a":1}')
        body = self.fake.requests[0]["body"]
        self.assertNotIn("tools", body)
        self.assertEqual(body["system"], "PROMPT")
        self.assertEqual(body["messages"][0]["content"], "INPUT")


class OpenAI(Base):
    fmt = "openai"

    def test_tool_loop_and_usage(self):
        self.fake.replies = [openai_tool("osctl", {"args": ["get-project"]}), openai_text("done")]
        ev = list(self.session().ask("hi", model="haiku"))
        self.assertIn(("tool", "osctl"), ev)
        self.assertEqual(ev[-1][1]["tokens"], 10)
        req = self.fake.requests[1]
        self.assertEqual(req["path"], "/chat/completions")
        self.assertEqual(req["headers"]["authorization"], "Bearer sk-secret-1234")
        self.assertEqual(req["body"]["model"], "m-fast")
        msgs = req["body"]["messages"]
        self.assertEqual(msgs[0], {"role": "system", "content": "RAIL"})
        self.assertEqual(msgs[2]["tool_calls"][0]["function"]["name"], "osctl")
        self.assertEqual(msgs[3], {"role": "tool", "tool_call_id": "c1", "content": "osctl says ok"})

    def test_bad_tool_json_goes_back_to_the_model(self):
        bad = openai_tool("osctl", {})
        bad[1]["choices"][0]["delta"]["tool_calls"][0]["function"]["arguments"] = '{"args": ['
        self.fake.replies = [bad, openai_text("sorry")]
        list(self.session().ask("hi"))
        self.assertEqual(self.osctl_calls, [])
        self.assertIn("not valid JSON", self.fake.requests[1]["body"]["messages"][-1]["content"])

    def test_no_key_sends_no_auth_header(self):
        self.provider["key"] = ""
        self.fake.replies = [openai_text("x")]
        list(self.session().ask("hi"))
        self.assertNotIn("authorization", self.fake.requests[0]["headers"])


class FolderWrites(unittest.TestCase):
    """write_file and edit_file: only inside an edit folder, never the never-write list."""

    def setUp(self):
        root, ext = tempfile.TemporaryDirectory(), tempfile.TemporaryDirectory()
        self.addCleanup(root.cleanup)
        self.addCleanup(ext.cleanup)
        self.root = Path(root.name).resolve()
        self.ext = Path(ext.name).resolve()
        self.edit, self.ro, self.outside = (self.ext / n for n in ("edit", "ro", "outside"))
        for d in (self.edit, self.ro, self.outside):
            d.mkdir()
        (self.edit / "link").symlink_to(self.outside)
        (self.ro / "notes.md").write_text("read only")
        self.policy = TurnPolicy(workspace="acme", read_dirs=(str(self.edit), str(self.ro)),
                                 edit_dirs=(str(self.edit),))

    def write(self, path, content="n", policy=None):
        return api_tools.write_file(str(path), content, self.root, policy or self.policy)

    def test_write_inside_edit_folder(self):
        out = self.write(self.edit / "src" / "new.py", "print(1)\n")
        self.assertEqual((self.edit / "src" / "new.py").read_text(), "print(1)\n")
        self.assertIn(str(self.edit / "src" / "new.py"), out)
        self.write("rel.md", "relative")
        self.assertEqual((self.edit / "rel.md").read_text(), "relative")
        for rel in ("CLAUDE.md", "AGENTS.md", ".github/workflows/ci.yml"):
            self.write(self.edit / rel)
            self.assertEqual((self.edit / rel).read_text(), "n")

    def test_write_refusals(self):
        (self.edit / "evil").symlink_to(self.outside / "target")
        for path in (self.outside / "x", self.ro / "x", self.edit / ".env", self.edit / ".git/hooks/x",
                     self.edit / ".envrc", self.edit / "link/x", self.edit / "../x", self.root / "x",
                     self.edit / ".mcp.json", self.edit / ".vscode/tasks.json", self.edit / ".claude/x",
                     self.edit / ".husky/pre-push", self.edit / "sub/.GIT/config", self.edit / "id_rsa",
                     self.edit / "evil", self.edit, self.edit / "..", "../x", "/etc/hosts"):
            with self.assertRaises(api_tools.ToolError, msg=str(path)):
                self.write(path)
            path = Path(path) if Path(path).is_absolute() else self.edit / path
            self.assertFalse(path.resolve().is_file() and path.read_text() == "n", path)
        self.assertFalse((self.edit / ".git").exists())
        self.assertEqual(list(self.outside.iterdir()), [])

    def test_a_read_folder_inside_the_edit_folder_is_not_writable(self):
        docs = self.edit / "docs"
        docs.mkdir()
        p = TurnPolicy(workspace="acme", read_dirs=(str(self.edit), str(docs)), edit_dirs=(str(self.edit),))
        with self.assertRaises(api_tools.ToolError):
            api_tools.write_file(str(docs / "x.md"), "n", self.root, p)
        self.assertFalse((docs / "x.md").exists())
        api_tools.write_file(str(self.edit / "y.md"), "n", self.root, p)

    def test_core_hooks_path_is_not_writable(self):
        with mock.patch.object(api_tools.folders, "hook_dirs", return_value=[str(self.edit / "scripts" / "hooks")]):
            with self.assertRaises(api_tools.ToolError):
                self.write(self.edit / "scripts" / "hooks" / "pre-commit")
        self.assertFalse((self.edit / "scripts").exists())

    def test_write_refuses_a_pipe_and_oversized_content(self):
        os.mkfifo(self.edit / "pipe")
        with self.assertRaises(api_tools.ToolError):
            self.write(self.edit / "pipe")
        (self.edit / "dir").mkdir()
        with self.assertRaises(api_tools.ToolError):
            self.write(self.edit / "dir")
        with self.assertRaises(api_tools.ToolError):
            self.write(self.edit / "big.txt", "x" * (api_tools.MAX_FILE_BYTES + 1))
        self.assertFalse((self.edit / "big.txt").exists())
        with self.assertRaises(api_tools.ToolError):
            self.write(self.edit / "x.txt", 42)

    def test_write_is_atomic_and_keeps_mode(self):
        f = self.edit / "run.sh"
        f.write_text("old")
        f.chmod(0o750)
        self.write(f, "new")
        self.assertEqual(f.read_text(), "new")
        self.assertEqual(stat.S_IMODE(f.stat().st_mode), 0o750)
        self.assertEqual(sorted(p.name for p in self.edit.iterdir()), ["link", "run.sh"])
        with mock.patch.object(api_tools.os, "replace", side_effect=OSError("disk full")):
            with self.assertRaises(api_tools.ToolError):
                self.write(f, "broken")
        self.assertEqual(f.read_text(), "new")
        self.assertEqual(sorted(p.name for p in self.edit.iterdir()), ["link", "run.sh"])

    def test_edit_replaces_exactly_one_match(self):
        f = self.edit / "app.py"
        f.write_text("a = 1\nb = 2\n")
        out = api_tools.edit_file(str(f), "b = 2", "b = 3", self.root, self.policy)
        self.assertEqual(f.read_text(), "a = 1\nb = 3\n")
        self.assertIn(str(f), out)

    def test_edit_refuses_missing_or_repeated_text_and_leaves_the_file(self):
        f = self.edit / "app.py"
        f.write_text("x = 1\nx = 1\n")
        for old in ("x = 1", "nope", ""):
            with self.assertRaises(api_tools.ToolError, msg=old):
                api_tools.edit_file(str(f), old, "y", self.root, self.policy)
        self.assertEqual(f.read_text(), "x = 1\nx = 1\n")
        with self.assertRaises(api_tools.ToolError):
            api_tools.edit_file(str(self.edit / "missing.py"), "a", "b", self.root, self.policy)
        with self.assertRaises(api_tools.ToolError):
            api_tools.edit_file(str(self.ro / "notes.md"), "read", "write", self.root, self.policy)
        self.assertEqual((self.ro / "notes.md").read_text(), "read only")

    def test_edit_refuses_non_utf8_and_leaves_the_file(self):
        f = self.edit / "blob.bin"
        f.write_bytes(b"\xff\xfeabc")
        with self.assertRaises(api_tools.ToolError):
            api_tools.edit_file(str(f), "abc", "def", self.root, self.policy)
        self.assertEqual(f.read_bytes(), b"\xff\xfeabc")

    def test_tools_not_offered_without_edit_folders(self):
        names = lambda p: [s["name"] for s in api_tools.ToolBox(p, self.root, TurnTokens(), "api", {}).specs]
        self.assertIn("write_file", names(self.policy))
        self.assertIn("edit_file", names(self.policy))
        read_only = TurnPolicy(workspace="acme", read_dirs=(str(self.edit),))
        self.assertNotIn("write_file", names(read_only))
        self.assertNotIn("edit_file", names(read_only))
        box = api_tools.ToolBox(read_only, self.root, TurnTokens(), "api", {})
        text, failed = box.run("write_file", {"path": str(self.edit / "x"), "content": "n"})
        self.assertTrue(failed)
        self.assertFalse((self.edit / "x").exists())
        with self.assertRaises(api_tools.ToolError):
            self.write(self.edit / "x", policy=read_only)

    def test_tools_not_offered_on_a_shared_turn(self):
        shared = TurnPolicy(workspace="acme", shared=True, read_dirs=(str(self.edit),), edit_dirs=(str(self.edit),))
        box = api_tools.ToolBox(shared, self.root, TurnTokens(), "api", {})
        self.assertEqual([s["name"] for s in box.specs], ["osctl"])
        text, failed = box.run("write_file", {"path": str(self.edit / "x"), "content": "n"})
        self.assertTrue(failed)
        with self.assertRaises(api_tools.ToolError):
            self.write(self.edit / "x", policy=shared)
        self.assertFalse((self.edit / "x").exists())

    def test_toolbox_runs_write_and_edit(self):
        box = api_tools.ToolBox(self.policy, self.root, TurnTokens(), "api", {})
        text, failed = box.run("write_file", {"path": str(self.edit / "a.txt"), "content": "one"})
        self.assertFalse(failed, text)
        text, failed = box.run("edit_file", {"path": str(self.edit / "a.txt"), "old_text": "one", "new_text": "two"})
        self.assertFalse(failed, text)
        self.assertEqual((self.edit / "a.txt").read_text(), "two")

    def test_deny_dirs_block_writes(self):
        vendor = self.edit / "vendor"
        vendor.mkdir()
        p = TurnPolicy(workspace="acme", read_dirs=(str(self.edit),), edit_dirs=(str(self.edit),),
                       deny_dirs=(str(vendor),))
        with self.assertRaises(api_tools.ToolError):
            self.write(vendor / "x", policy=p)
        self.assertFalse((vendor / "x").exists())

    def test_read_reaches_every_read_dir_and_nothing_else(self):
        (self.edit / "app.py").write_text("needle")
        (self.ro / "doc.md").write_text("needle")
        (self.outside / "o.md").write_text("needle")
        self.assertEqual(api_tools.read_file(str(self.ro / "doc.md"), self.root, self.policy), "needle")
        self.assertEqual(api_tools.read_file(str(self.edit / "app.py"), self.root, self.policy), "needle")
        with self.assertRaises(api_tools.ToolError):
            api_tools.read_file(str(self.outside / "o.md"), self.root, self.policy)
        out = api_tools.search_files(None, "needle", self.root, self.policy)
        self.assertIn(str(self.edit / "app.py"), out)
        self.assertIn(str(self.ro / "doc.md"), out)
        self.assertNotIn("o.md", out)
        only = api_tools.search_files("*.md", None, self.root, self.policy, folder=str(self.ro))
        self.assertIn(str(self.ro / "doc.md"), only)
        self.assertNotIn("app.py", only)
        with self.assertRaises(api_tools.ToolError):
            api_tools.search_files(None, "needle", self.root, self.policy, folder=str(self.outside))


class FileRules(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        for rel in ("docs/a.md", "team/engines.json", "team/x.md", "chats/c.json", "projects/acme/a.md",
                    "projects/demo/b.md", ".env", "dashboard/.auth-token", "run/auth-token"):
            f = self.root / rel
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text("data")
        self.policy = TurnPolicy(workspace="acme")

    def read(self, path, policy=None):
        return api_tools.read_file(path, self.root, policy or self.policy)

    def test_allowed(self):
        self.assertEqual(self.read("docs/a.md"), "data")
        self.assertEqual(self.read("projects/acme/a.md"), "data")

    def test_refused(self):
        for path in ("team/engines.json", "team/x.md", "chats/c.json", "projects/demo/b.md", ".env",
                     "dashboard/.auth-token", "run/auth-token", "../outside", "/etc/hosts", "docs/../team/x.md"):
            with self.assertRaises(api_tools.ToolError, msg=path):
                self.read(path)

    def test_symlink_out_is_refused(self):
        (self.root / "docs" / "link").symlink_to(self.root / "team" / "x.md")
        with self.assertRaises(api_tools.ToolError):
            self.read("docs/link")

    def test_linked_folder_only_when_set_and_secrets_stay_out(self):
        linked = Path(tempfile.mkdtemp(dir=self.root.parent)).resolve()
        self.addCleanup(lambda: __import__("shutil").rmtree(linked, ignore_errors=True))
        (linked / "app.py").write_text("code")
        (linked / ".env").write_text("secret")
        with self.assertRaises(api_tools.ToolError):
            self.read(str(linked / "app.py"))
        on = TurnPolicy(workspace="acme", read_dirs=(str(linked),))
        self.assertEqual(self.read(str(linked / "app.py"), on), "code")
        with self.assertRaises(api_tools.ToolError):
            self.read(str(linked / ".env"), on)
        self.assertIn("app.py", api_tools.search_files(None, "code", self.root, on))
        self.assertNotIn(".env", api_tools.search_files("*", None, self.root, on))

    def test_engines_json_is_a_secret_glob(self):
        self.assertIn("engines.json", SECRET_FILE_GLOBS)


class WebFetch(unittest.TestCase):
    policy = TurnPolicy(web=True, web_domains=("docs.example",))

    def test_host_not_typed_is_refused(self):
        with self.assertRaisesRegex(api_tools.ToolError, "not named"):
            api_tools.web_fetch("https://other.example/x", self.policy)

    def test_host_on_a_private_address_is_refused(self):
        for addr in ("169.254.169.254", "127.0.0.1", "10.0.0.5"):
            with mock.patch.object(api_tools.socket, "getaddrinfo", return_value=[(2, 1, 6, "", (addr, 0))]), \
                    self.assertRaisesRegex(api_tools.ToolError, "not a public"):
                api_tools.web_fetch("https://docs.example/x", self.policy)

    def test_redirect_to_another_host_is_refused(self):
        import io
        import urllib.error

        class Opener:
            def open(self, req, timeout=None):
                raise urllib.error.HTTPError(req.full_url, 302, "moved", {"Location": "http://other.example/"}, io.BytesIO())
        with mock.patch.object(api_tools.socket, "getaddrinfo", return_value=[(2, 1, 6, "", ("93.184.216.34", 0))]), \
                self.assertRaisesRegex(api_tools.ToolError, "not named"):
            api_tools.web_fetch("https://docs.example/x", self.policy, opener=Opener())

    def test_html_becomes_text(self):
        self.assertEqual(api_tools._page_text(b"<p>a &amp; b</p><script>x()</script>", "text/html"), "a & b")


class Config(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.key = "sk-secret-abcd"

    def add(self):
        return config.upsert_provider({"id": "anthropic", "key": self.key}, self.root)

    def test_file_is_owner_only_and_public_view_hides_the_key(self):
        self.add()
        path = self.root / "team" / "engines.json"
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        view = config.public(self.root)
        self.assertNotIn(self.key, json.dumps(view))
        self.assertEqual(view["providers"][0]["key_hint"], "abcd")

    def test_edit_without_a_key_keeps_it(self):
        self.add()
        config.upsert_provider({"id": "anthropic", "models": {"fast": "m"}}, self.root)
        self.assertEqual(config.provider("anthropic", self.root)["key"], self.key)
        self.assertEqual(config.provider("anthropic", self.root)["models"]["fast"], "m")

    def test_key_required_except_local(self):
        with self.assertRaises(ValueError):
            config.upsert_provider({"id": "anthropic"}, self.root)
        config.upsert_provider({"id": "ollama", "models": {"fast": "m"}}, self.root)
        self.assertEqual(config.provider("ollama", self.root)["models"], {"fast": "m", "strong": "m"})

    def test_bad_values_refused(self):
        for bad in ({"id": "../x", "key": "k", "format": "openai", "base_url": "http://x.example"},
                    {"id": "x", "key": "k", "format": "nope", "base_url": "http://x.example"},
                    {"id": "x", "key": "k", "format": "openai", "base_url": "ftp://x.example"}):
            with self.assertRaises(ValueError):
                config.upsert_provider(bad, self.root)

    def test_removing_a_provider_clears_every_choice(self):
        self.add()
        for name, scope in (("default", None), ("people", None), ("workspaces", "acme"), ("bots", "acme/cmo")):
            config.set_choice(name, "api:anthropic", scope, self.root)
        config.remove_provider("anthropic", self.root)
        view = config.public(self.root)
        self.assertEqual((view["default"], view["people"], view["workspaces"], view["bots"]), ("", "", {}, {}))


class Pick(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def api(self, pid="anthropic"):
        config.upsert_provider({"id": pid, "key": "k"}, self.root)

    def test_invited_person_never_runs_on_the_owner_login(self):
        with self.assertRaises(EngineRefused):
            registry.pick(self.root, "acme", "cmo", invited=True)
        self.api()
        config.set_choice("people", "api:anthropic", None, self.root)
        self.assertEqual(registry.pick(self.root, "acme", "cmo", invited=True).id, "api:anthropic")
        data = config.load(self.root)  # a hand-edited file
        data["people"] = "claude-code"
        config.save(data, self.root)
        with self.assertRaises(EngineRefused):
            registry.pick(self.root, "acme", "cmo", invited=True)

    def test_owner_order_bot_then_workspace_then_default(self):
        self.api("anthropic")
        self.api("openai")
        self.assertEqual(registry.pick(self.root, "acme", "cmo").id, "claude-code")
        config.set_choice("default", "api:anthropic", None, self.root)
        self.assertEqual(registry.pick(self.root, "acme", "cmo").id, "api:anthropic")
        config.set_choice("workspaces", "claude-code", "acme", self.root)
        self.assertEqual(registry.pick(self.root, "acme", "cmo").id, "claude-code")
        config.set_choice("bots", "api:openai", "acme/cmo", self.root)
        self.assertEqual(registry.pick(self.root, "acme", "cmo").id, "api:openai")

    def test_no_claude_installed_falls_to_first_provider(self):
        self.api()
        with mock.patch.object(registry.shutil, "which", return_value=None):
            self.assertEqual(registry.pick(self.root, "acme", "cmo").id, "api:anthropic")

    def test_job_follows_its_turn(self):
        self.api()
        with mock.patch.dict(os.environ, {"LEOBOT_ENGINE": "api:anthropic"}):
            self.assertEqual(registry.for_job(self.root).id, "api:anthropic")
        with mock.patch.dict(os.environ, {"LEOBOT_ENGINE": "api:gone"}), self.assertRaises(EngineRefused):
            registry.for_job(self.root)

    def test_terminal_defaults_to_the_shell(self):
        with mock.patch.dict(os.environ, {"SHELL": "/bin/zsh"}):
            self.assertEqual(registry.terminal_cmd(self.root), ["/bin/zsh", "-l"])
        config.set_choice("terminal", "claude", None, self.root)
        with mock.patch.object(registry.shutil, "which", return_value="/x/claude"):
            self.assertEqual(registry.terminal_cmd(self.root), ["claude"])


class Recap(unittest.TestCase):
    def setUp(self):
        from dashboard import chat_store
        self.store = chat_store
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.object(chat_store, "CHATS_DIR", Path(self.tmp.name))
        patcher.start()
        self.addCleanup(patcher.stop)

    def sess(self, engine_id, sid="11111111-1111-1111-1111-111111111111"):
        s = cs.ChatSession(repo_dir=".", rail="R", session_id=sid)
        s.engine = type("E", (), {"id": engine_id, "is_fresh": lambda self, x: False})()
        return s

    def test_engine_sees_only_what_it_missed_never_tool_output(self):
        a, b = self.sess("api:a"), self.sess("api:b")
        self.store.record_turn(a, None, "one", "reply one", bot="cmo")
        rec = self.store.load(a.session_id)
        self.assertEqual(self.store.recap(rec, "api:a"), "")
        missed = self.store.recap(rec, "api:b")
        self.assertIn("You: one", missed)
        self.assertIn("@cmo: reply one", missed)
        self.store.record_turn(b, None, "two", "reply two", bot="cmo", by="p1", by_name="Profile A")
        rec = self.store.load(a.session_id)
        missed = self.store.recap(rec, "api:a")
        self.assertNotIn("one", missed)
        self.assertIn("Profile A: two", missed)

    def test_older_record_counts_as_seen_by_claude_code_only(self):
        rec = {"id": "x", "started": True, "messages": [{"role": "user", "content": "old"}]}
        self.assertEqual(self.store.recap(rec, "claude-code"), "")
        self.assertIn("old", self.store.recap(rec, "api:a"))

    def test_budget_keeps_the_newest(self):
        rec = {"engines": {}, "messages": [{"role": "user", "content": "OLD " * 5000},
                                           {"role": "user", "content": "new"}]}
        out = self.store.recap(rec, "api:a")
        self.assertIn("new", out)
        self.assertNotIn("OLD", out)

    def test_lost_session_gets_everything(self):
        rec = {"engines": {"claude-code": {"seen": 1}}, "messages": [{"role": "user", "content": "a"}]}
        self.assertIn("a", self.store.recap(rec))


if __name__ == "__main__":
    unittest.main()
