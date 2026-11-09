# Leo Bot AI Integration Engine — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the per-message `claude -p` consultant with a persistent, streamed, guard-railed Claude Code chat plus an embedded Cursor-style terminal, so AI reliably creates/edits OS entities and supports power work.

**Architecture:** A persistent `claude` headless session (stream-json) drives the right-side chat panel; the agent may mutate state ONLY through a new stdlib `osctl` CLI that wraps `fileops` + `reindex`. A separate full-trust `claude` runs in a PTY bridged to an xterm.js terminal over a hand-rolled stdlib WebSocket. Layout A (Cursor): left nav · center OS views · collapsible right chat · toggleable bottom terminal.

**Tech Stack:** Python 3 stdlib (`argparse`, `subprocess`, `pty`, `os`, `selectors`, `http.server`, `hashlib`, `base64`, `struct`), vanilla JS, vendored `xterm.js`. The `claude` CLI. `unittest` for tests.

## Global Constraints

- **No new Python dependencies.** Server + all new modules are stdlib-only. The only added asset is a vendored `xterm.js` JS file (no npm/build step).
- **Authored files are the source of truth**; `database/data/os.db` is a derived, READ-ONLY index rebuilt by `fileops.reindex()`. Never write `os.db` directly.
- **The dashboard mutates files via `fileops`, then reindexes.** The chat agent mutates ONLY via `python -m dashboard.osctl`; it is denied `Write`/`Edit`.
- **UI-first:** every AI surface lives inside the dashboard window; the user never hand-edits files.
- **Repo:** `/path/to/Leo Bot` (branch: main). Work on a feature branch.
- **Test runner:** `python -m unittest` from the repo root (CWD must be repo root). Full suite: `python -m unittest discover -s tests -v`.
- **Import convention for `osctl`:** insert repo root on `sys.path`, then `from dashboard import fileops` — so test overrides of `dashboard.fileops.ROOT` are seen by `osctl`.

## File Structure

- **Create `dashboard/osctl.py`** — stdlib argparse CLI; one subcommand per `fileops` mutation; prints one JSON line per call. Single mutation entry point for the agent.
- **Create `dashboard/chat_session.py`** — owns one persistent `claude` stream-json process; sends user turns; yields parsed events (`delta` / `tool` / `done`). Includes a pure `parse_event()` function.
- **Create `dashboard/ws.py`** — pure WebSocket helpers: `accept_key()`, `encode_frame()`, `decode_frame()` (RFC 6455). No I/O, fully unit-testable.
- **Create `dashboard/terminal_session.py`** — `os.openpty()` + spawn interactive `claude`; pump PTY↔callback; `resize()` via `TIOCSWINSZ` + `SIGWINCH`; `close()`.
- **Modify `dashboard/server.py`** — replace `_handle_ask` to drive `ChatSession` over SSE; add a `/ws/terminal` WebSocket upgrade handler bridging to `TerminalSession`; terminate children on shutdown.
- **Modify `dashboard/app.html`** — Layout A: left nav, center views, collapsible right chat dock (streamed deltas + tool chips + view refresh), toggleable bottom terminal (vendored xterm.js + WS).
- **Create `dashboard/vendor/xterm.js`, `dashboard/vendor/xterm.css`, `dashboard/vendor/xterm-addon-fit.js`** — vendored assets, served as static files.
- **Create tests:** `tests/test_osctl.py`, `tests/test_chat_session.py`, `tests/test_ws.py`, `tests/test_terminal_session.py`.

---

### Task 1: `osctl` skeleton + `create-project`

**Files:**
- Create: `dashboard/osctl.py`
- Test: `tests/test_osctl.py`

**Interfaces:**
- Produces: `osctl.main(argv: list[str]) -> int` (0 ok, 1 error). Prints exactly one JSON line: `{"ok": true, ...fileops_result}` or `{"ok": false, "error": "..."}`. Subcommand `create-project --slug S [--name --kind --priority --status --hours-per-week --voice]` → `fileops.create_project`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_osctl.py
import io, json, tempfile, unittest, contextlib
from pathlib import Path
import index
from tests.test_index_projects import write
import dashboard.fileops as fileops
import dashboard.db as db
import dashboard.osctl as osctl


def run(argv):
    """Invoke osctl.main, capture the single JSON line it prints."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = osctl.main(argv)
    line = buf.getvalue().strip().splitlines()[-1]
    return code, json.loads(line)


class T(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        fileops.ROOT = self.root
        db.DB_PATH = self.root / "database" / "data" / "os.db"
        # minimal indexable workspace
        write(self.root / "projects" / ".keep", "")
        index.build(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def test_create_project_ok(self):
        code, out = run(["create-project", "--slug", "acme", "--name", "Acme"])
        self.assertEqual(code, 0)
        self.assertTrue(out["ok"])
        self.assertEqual(out["slug"], "acme")
        self.assertTrue((self.root / "projects" / "acme" / "project.md").exists())

    def test_create_project_duplicate_errors(self):
        run(["create-project", "--slug", "dup"])
        code, out = run(["create-project", "--slug", "dup"])
        self.assertEqual(code, 1)
        self.assertFalse(out["ok"])
        self.assertIn("already exists", out["error"])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest tests.test_osctl -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'dashboard.osctl'`.

- [ ] **Step 3: Write minimal implementation**

```python
# dashboard/osctl.py
"""osctl.py — the single mutation entry point for the Leo Bot AI agent.

Wraps dashboard/fileops.py mutations as a stdlib argparse CLI. Each subcommand
validates input, calls fileops (which writes authored files and reindexes), and
prints exactly one JSON line. The chat agent is allowed to mutate state ONLY
through this CLI, so the authored-files-are-truth invariant cannot be bypassed.
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # repo root
from dashboard import fileops  # noqa: E402


def _emit(obj, ok=True):
    print(json.dumps({"ok": ok, **obj}, ensure_ascii=False))
    return 0 if ok else 1


def _fields(args, keys):
    """Collect provided (non-None) attrs into a fileops fields dict."""
    return {k: getattr(args, k) for k in keys if getattr(args, k) is not None}


def _build_parser():
    parser = argparse.ArgumentParser(prog="osctl", description="Leo Bot mutation CLI")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("create-project")
    p.add_argument("--slug", required=True)
    p.add_argument("--name")
    p.add_argument("--kind")
    p.add_argument("--priority")
    p.add_argument("--status")
    p.add_argument("--hours-per-week", dest="hours_per_week")
    p.add_argument("--voice")
    p.set_defaults(_run=lambda a: fileops.create_project(
        a.slug, _fields(a, ["name", "kind", "priority", "status", "hours_per_week", "voice"])))

    return parser


def main(argv=None):
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        result = args._run(args)
    except fileops.ActionError as exc:
        return _emit({"error": str(exc)}, ok=False)
    except Exception as exc:  # noqa: BLE001
        return _emit({"error": repr(exc)}, ok=False)
    return _emit(result)


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m unittest tests.test_osctl -v`
Expected: PASS (both tests).

- [ ] **Step 5: Commit**

```bash
git add dashboard/osctl.py tests/test_osctl.py
git commit -m "feat(osctl): mutation CLI skeleton + create-project"
```

---

### Task 2: `osctl` entity-creation subcommands (profile, channel, add-post)

**Files:**
- Modify: `dashboard/osctl.py` (add three subparsers in `_build_parser`)
- Test: `tests/test_osctl.py` (add cases)

**Interfaces:**
- Consumes: `osctl.main`, `_fields` (Task 1).
- Produces subcommands:
  - `create-profile --project P --slug S [--name --topic --voice]` → `fileops.create_profile(P, S, fields)`
  - `create-channel --profile P --slug S --platform PLAT [--handle H]` → `fileops.create_channel(P, S, PLAT, H)`
  - `add-post --profile P [--working-title --pillar --hook --channels ...]` → `fileops.add_post(P, fields)`

- [ ] **Step 1: Write the failing test** (append to `tests/test_osctl.py`)

```python
    def test_create_profile_and_channel_and_post(self):
        run(["create-project", "--slug", "acme", "--name", "Acme"])
        c, out = run(["create-profile", "--project", "acme",
                      "--slug", "demo", "--name", "Demo"])
        self.assertEqual(c, 0); self.assertTrue(out["ok"])
        self.assertTrue((self.root / "projects" / "acme" / "profiles"
                         / "demo" / "profile.md").exists())

        c, out = run(["create-channel", "--profile", "demo",
                      "--slug", "demo-tiktok", "--platform", "tiktok"])
        self.assertEqual(c, 0); self.assertEqual(out["platform"], "tiktok")

        c, out = run(["add-post", "--profile", "demo",
                      "--working-title", "Idea A", "--channels", "demo-tiktok"])
        self.assertEqual(c, 0); self.assertTrue(out["id"].startswith("m-"))
        self.assertEqual(len(db.profile_posts("demo")), 1)

    def test_create_profile_unknown_project_errors(self):
        c, out = run(["create-profile", "--project", "nope", "--slug", "x"])
        self.assertEqual(c, 1); self.assertIn("not found", out["error"])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest tests.test_osctl -v`
Expected: FAIL — `argparse` exits with "invalid choice: 'create-profile'" (SystemExit / error).

- [ ] **Step 3: Write minimal implementation** (insert into `_build_parser`, before `return parser`)

```python
    p = sub.add_parser("create-profile")
    p.add_argument("--project", required=True)
    p.add_argument("--slug", required=True)
    p.add_argument("--name")
    p.add_argument("--topic")
    p.add_argument("--voice")
    p.set_defaults(_run=lambda a: fileops.create_profile(
        a.project, a.slug, _fields(a, ["name", "topic", "voice"])))

    p = sub.add_parser("create-channel")
    p.add_argument("--profile", required=True)
    p.add_argument("--slug", required=True)
    p.add_argument("--platform", required=True)
    p.add_argument("--handle", default="")
    p.set_defaults(_run=lambda a: fileops.create_channel(
        a.profile, a.slug, a.platform, a.handle))

    p = sub.add_parser("add-post")
    p.add_argument("--profile", required=True)
    p.add_argument("--working-title", dest="working_title")
    p.add_argument("--pillar")
    p.add_argument("--hook")
    p.add_argument("--angle")
    p.add_argument("--channels")
    p.set_defaults(_run=lambda a: fileops.add_post(
        a.profile, _fields(a, ["working_title", "pillar", "hook", "angle", "channels"])))
```

> Note: `add-post` field keys must match `fileops._POST_FIELDS`. If a chosen key (e.g. `working_title`) is not in `_POST_FIELDS`, it is silently dropped by `add_post`; verify against `dashboard/fileops.py` and adjust the `--flag`/`dest` names to the real field set before finalizing.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m unittest tests.test_osctl -v`
Expected: PASS (all cases).

- [ ] **Step 5: Commit**

```bash
git add dashboard/osctl.py tests/test_osctl.py
git commit -m "feat(osctl): create-profile, create-channel, add-post"
```

---

### Task 3: `osctl` activity/milestone/status subcommands

**Files:**
- Modify: `dashboard/osctl.py`
- Test: `tests/test_osctl.py`

**Interfaces:**
- Produces:
  - `create-activity --entity E --title T [--date --date-end --type --priority]` → `fileops.create_activity(fields)`
  - `create-milestone --title T --date D [--entity --type --entity-type --date-end --notes --priority]` → `fileops.create_milestone(fields)`
  - `mark-done --title T --entity E` → `fileops.mark_activity_done(T, E)`
  - `update-post --id ID [--working-title --pillar --hook --angle --channels]` → `fileops.update_post(ID, fields)`
  - `set-status --id ID --status S` → `fileops.set_status(ID, S)`

- [ ] **Step 1: Write the failing test** (append)

```python
    def test_activity_and_milestone(self):
        run(["create-project", "--slug", "acme"])
        c, out = run(["create-activity", "--entity", "acme",
                      "--title", "Draft hook", "--type", "task"])
        self.assertEqual(c, 0); self.assertEqual(out["title"], "Draft hook")
        c, out = run(["mark-done", "--entity", "acme", "--title", "Draft hook"])
        self.assertEqual(c, 0); self.assertTrue(out["done"])

        c, out = run(["create-milestone", "--title", "Launch", "--date", "2026-07-01",
                      "--entity", "acme"])
        self.assertEqual(c, 0); self.assertTrue(out["id"].startswith("ms-"))

    def test_create_activity_requires_title(self):
        c, out = run(["create-activity", "--entity", "acme"])
        self.assertEqual(c, 1); self.assertIn("title is required", out["error"])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest tests.test_osctl -v`
Expected: FAIL — invalid choice: 'create-activity'.

- [ ] **Step 3: Write minimal implementation** (insert into `_build_parser`)

```python
    p = sub.add_parser("create-activity")
    p.add_argument("--entity", required=True)
    p.add_argument("--title", required=True)
    p.add_argument("--date")
    p.add_argument("--date-end", dest="date_end")
    p.add_argument("--type")
    p.add_argument("--priority")
    p.set_defaults(_run=lambda a: fileops.create_activity(
        _fields(a, ["entity", "title", "date", "date_end", "type", "priority"])))

    p = sub.add_parser("create-milestone")
    p.add_argument("--title", required=True)
    p.add_argument("--date", required=True)
    p.add_argument("--entity")
    p.add_argument("--type")
    p.add_argument("--entity-type", dest="entity_type")
    p.add_argument("--date-end", dest="date_end")
    p.add_argument("--notes")
    p.add_argument("--priority")
    p.set_defaults(_run=lambda a: fileops.create_milestone(
        _fields(a, ["title", "date", "entity", "type", "entity_type",
                    "date_end", "notes", "priority"])))

    p = sub.add_parser("mark-done")
    p.add_argument("--title", required=True)
    p.add_argument("--entity", required=True)
    p.set_defaults(_run=lambda a: fileops.mark_activity_done(a.title, a.entity))

    p = sub.add_parser("update-post")
    p.add_argument("--id", required=True, dest="id")
    p.add_argument("--working-title", dest="working_title")
    p.add_argument("--pillar")
    p.add_argument("--hook")
    p.add_argument("--angle")
    p.add_argument("--channels")
    p.set_defaults(_run=lambda a: fileops.update_post(
        a.id, _fields(a, ["working_title", "pillar", "hook", "angle", "channels"])))

    p = sub.add_parser("set-status")
    p.add_argument("--id", required=True, dest="id")
    p.add_argument("--status", required=True)
    p.set_defaults(_run=lambda a: fileops.set_status(a.id, a.status))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m unittest tests.test_osctl -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add dashboard/osctl.py tests/test_osctl.py
git commit -m "feat(osctl): activity, milestone, mark-done, update-post, set-status"
```

---

### Task 4: `ChatSession` — persistent claude stream-json process

**Files:**
- Create: `dashboard/chat_session.py`
- Test: `tests/test_chat_session.py`

**Interfaces:**
- Produces:
  - `parse_event(obj: dict) -> tuple[str|None, object]`: maps a decoded stream-json line to `("delta", text)`, `("tool", tool_name)`, `("done", result_dict)`, or `(None, None)`.
  - `class ChatSession(repo_dir: str, rail: str, claude_bin: str = "claude", session_id: str|None = None)`.
    - `ask(text: str) -> Iterator[tuple[str, object]]`: send one user turn; yield `(kind, payload)` events until the turn's `done`.
- Consumes: nothing from earlier tasks.

- [ ] **Step 0 (discovery, no code): capture the real event schema**

Run: `claude -p --output-format stream-json --include-partial-messages --verbose 'say hi' | head -40`
Read the JSON lines. Confirm the field paths used in `parse_event` below (text deltas, tool_use start, final `result`). If paths differ, update `parse_event` AND its test together to match the real schema. Do not skip this step.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_chat_session.py
import os, stat, tempfile, textwrap, unittest
from pathlib import Path
import dashboard.chat_session as cs


class ParseEvent(unittest.TestCase):
    def test_text_delta(self):
        ev = {"type": "stream_event",
              "event": {"type": "content_block_delta",
                        "delta": {"type": "text_delta", "text": "Hello"}}}
        self.assertEqual(cs.parse_event(ev), ("delta", "Hello"))

    def test_tool_use_start(self):
        ev = {"type": "stream_event",
              "event": {"type": "content_block_start",
                        "content_block": {"type": "tool_use", "name": "Bash"}}}
        self.assertEqual(cs.parse_event(ev), ("tool", "Bash"))

    def test_result(self):
        ev = {"type": "result", "subtype": "success", "result": "done"}
        kind, payload = cs.parse_event(ev)
        self.assertEqual(kind, "done")

    def test_ignored(self):
        self.assertEqual(cs.parse_event({"type": "system"}), (None, None))


class AskWithStub(unittest.TestCase):
    """Drive ChatSession against a fake `claude` that emits stream-json."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        stub = Path(self.tmp.name) / "fakeclaude"
        stub.write_text(textwrap.dedent('''\
            #!/usr/bin/env python3
            import sys, json
            sys.stdin.readline()  # consume the user turn
            for t in ["He", "llo"]:
                print(json.dumps({"type":"stream_event","event":{
                    "type":"content_block_delta",
                    "delta":{"type":"text_delta","text":t}}}), flush=True)
            print(json.dumps({"type":"result","subtype":"success","result":"Hello"}), flush=True)
        '''))
        stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
        self.stub = str(stub)

    def tearDown(self):
        self.tmp.cleanup()

    def test_ask_streams_then_done(self):
        sess = cs.ChatSession(repo_dir=self.tmp.name, rail="RAIL", claude_bin=self.stub)
        events = list(sess.ask("hi"))
        deltas = "".join(p for k, p in events if k == "delta")
        self.assertEqual(deltas, "Hello")
        self.assertTrue(any(k == "done" for k, _ in events))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest tests.test_chat_session -v`
Expected: FAIL — `No module named 'dashboard.chat_session'`.

- [ ] **Step 3: Write minimal implementation**

```python
# dashboard/chat_session.py
"""chat_session.py — one persistent Claude Code session for the dashboard run.

Runs `claude` in stream-json mode with a permission allowlist limited to the
osctl CLI + read tools, so the agent can ONLY mutate state via
`python -m dashboard.osctl`. Parses stream-json events into simple
(kind, payload) tuples for the SSE layer.
"""
import json
import subprocess
import uuid


def parse_event(obj):
    """Map one decoded stream-json line to (kind, payload). See Task 4 Step 0."""
    t = obj.get("type")
    if t == "stream_event":
        ev = obj.get("event", {})
        et = ev.get("type")
        if et == "content_block_delta":
            delta = ev.get("delta", {})
            if delta.get("type") == "text_delta":
                return ("delta", delta.get("text", ""))
        elif et == "content_block_start":
            block = ev.get("content_block", {})
            if block.get("type") == "tool_use":
                return ("tool", block.get("name", ""))
    elif t == "result":
        return ("done", {"result": obj.get("result"), "subtype": obj.get("subtype")})
    return (None, None)


# Allowlist: the agent may run osctl + read-only tools, nothing else.
ALLOWED_TOOLS = "Bash(python -m dashboard.osctl:*) Read Grep Glob"
DISALLOWED_TOOLS = "Write Edit"


class ChatSession:
    def __init__(self, repo_dir, rail, claude_bin="claude", session_id=None):
        self.repo_dir = repo_dir
        self.rail = rail
        self.claude_bin = claude_bin
        self.session_id = session_id or str(uuid.uuid4())
        self._started = False

    def _base_cmd(self):
        cmd = [self.claude_bin, "-p",
               "--output-format", "stream-json",
               "--include-partial-messages",
               "--verbose",
               "--add-dir", self.repo_dir,
               "--append-system-prompt", self.rail,
               "--allowedTools", ALLOWED_TOOLS,
               "--disallowedTools", DISALLOWED_TOOLS,
               "--permission-mode", "default"]
        if self._started:
            cmd += ["--resume", self.session_id]
        else:
            cmd += ["--session-id", self.session_id]
        return cmd

    def ask(self, text):
        """Run one turn; yield (kind, payload) events. Each turn is its own
        `claude -p` invocation, resumed by session id so context persists."""
        proc = subprocess.Popen(
            self._base_cmd(), cwd=self.repo_dir,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1)
        self._started = True
        proc.stdin.write(text + "\n")
        proc.stdin.close()
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            kind, payload = parse_event(obj)
            if kind:
                yield (kind, payload)
        proc.wait()
        if proc.returncode not in (0, None):
            yield ("error", (proc.stderr.read() or "")[:500])
```

> Note: `-p` with a piped stdin turn is the simplest robust shape and gives true session continuity via `--resume`. If Step 0 shows the installed `claude` needs `--input-format stream-json` for stdin turns, pass the turn as the final positional arg instead (`cmd + [text]`) and drop the stdin write — adjust `_base_cmd`/`ask` and the stub together.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m unittest tests.test_chat_session -v`
Expected: PASS (parse + stub-driven ask).

- [ ] **Step 5: Commit**

```bash
git add dashboard/chat_session.py tests/test_chat_session.py
git commit -m "feat(chat): persistent stream-json ChatSession + event parser"
```

---

### Task 5: Wire `/api/ask` to `ChatSession` over SSE

**Files:**
- Modify: `dashboard/server.py` (replace `_handle_ask`, ~lines 57–111; add module-level session singleton + RAIL; import `chat_session`)
- Test: `tests/test_server_ask.py` (create)

**Interfaces:**
- Consumes: `chat_session.ChatSession`, `chat_session.parse_event` (Task 4).
- Produces: `Handler._handle_ask(self, body)` streams SSE lines: `data: {"delta": "..."}`, `data: {"tool": "Bash"}`, `data: {"error": "..."}`, terminating with `data: [DONE]`. A module-level `get_chat_session()` returns the singleton (lazily built), overridable in tests.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_server_ask.py
import io, unittest
from unittest import mock
import dashboard.server as server


class FakeSession:
    def ask(self, text):
        yield ("delta", "Hi ")
        yield ("delta", "there")
        yield ("done", {"result": "Hi there"})


class SSE(unittest.TestCase):
    def test_handle_ask_streams_sse(self):
        h = server.Handler.__new__(server.Handler)  # bypass __init__/socket
        sink = io.BytesIO()
        h.wfile = sink
        h.send_response = lambda *a, **k: None
        h.send_header = lambda *a, **k: None
        h.end_headers = lambda *a, **k: None
        with mock.patch.object(server, "get_chat_session", return_value=FakeSession()):
            h._handle_ask({"messages": [{"role": "user", "content": "hi"}]})
        out = sink.getvalue().decode()
        self.assertIn('data: {"delta": "Hi "}', out)
        self.assertIn('"tool"', out) if False else None
        self.assertIn('data: [DONE]', out)
        self.assertLess(out.index('"delta": "Hi "'), out.index("[DONE]"))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest tests.test_server_ask -v`
Expected: FAIL — `AttributeError: module 'dashboard.server' has no attribute 'get_chat_session'`.

- [ ] **Step 3: Write minimal implementation**

In `dashboard/server.py`, after the existing `import fileops` line, add:

```python
import chat_session  # noqa: E402

RAIL = (
    "You operate a Leo Bot whose source of truth is authored files. "
    "Mutate state ONLY by running `python -m dashboard.osctl <command>` "
    "(e.g. create-project, create-profile, create-channel, add-post, "
    "create-activity, create-milestone, mark-done, update-post, set-status). "
    "Never write or edit files directly. After acting, confirm briefly what you changed."
)

_CHAT = None


def get_chat_session():
    global _CHAT
    if _CHAT is None:
        _CHAT = chat_session.ChatSession(repo_dir=str(ROOT), rail=RAIL)
    return _CHAT
```

Replace the body of `_handle_ask` (keep the method signature) with:

```python
    def _handle_ask(self, body):
        messages = body.get("messages", [])
        if not messages or messages[-1].get("role") != "user":
            return self._send(400, {"error": "no user message"})
        text = messages[-1]["content"]

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        def emit(obj):
            self.wfile.write(f"data: {json.dumps(obj)}\n\n".encode())
            self.wfile.flush()

        try:
            for kind, payload in get_chat_session().ask(text):
                if kind == "delta":
                    emit({"delta": payload})
                elif kind == "tool":
                    emit({"tool": payload})
                elif kind == "error":
                    emit({"error": payload})
                # "done" → fall through to [DONE]
        except Exception as exc:  # noqa: BLE001
            emit({"error": repr(exc)})
        finally:
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
```

Delete the now-unused prompt-building code and the old `subprocess.Popen(["claude", "-p", prompt] ...)` block.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m unittest tests.test_server_ask -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add dashboard/server.py tests/test_server_ask.py
git commit -m "feat(server): stream ChatSession over SSE via /api/ask"
```

---

### Task 6: WebSocket helpers (`accept_key`, `encode_frame`, `decode_frame`)

**Files:**
- Create: `dashboard/ws.py`
- Test: `tests/test_ws.py`

**Interfaces:**
- Produces:
  - `accept_key(client_key: str) -> str` (RFC 6455 Sec-WebSocket-Accept).
  - `encode_frame(payload: bytes, opcode: int = 0x1) -> bytes` (server→client, unmasked).
  - `decode_frame(buf: bytes) -> tuple[int|None, bytes, int]`: returns `(opcode, payload, consumed)` for one complete frame, or `(None, b"", 0)` if `buf` lacks a full frame. Unmasks client frames.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_ws.py
import unittest
import dashboard.ws as ws


class WS(unittest.TestCase):
    def test_accept_key_rfc_example(self):
        # RFC 6455 §1.3 worked example
        self.assertEqual(ws.accept_key("dGhlIHNhbXBsZSBub25jZQ=="),
                         "s3pPLMBiTxaQ9kYGzzhZRbK+xOo=")

    def test_encode_small_text(self):
        frame = ws.encode_frame(b"hi", opcode=0x1)
        self.assertEqual(frame[0], 0x81)        # FIN + text
        self.assertEqual(frame[1], 0x02)        # unmasked, len 2
        self.assertEqual(frame[2:], b"hi")

    def test_decode_masked_client_frame_roundtrip(self):
        # Build a masked client text frame for "hi"
        import os
        mask = bytes([1, 2, 3, 4])
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(b"hi"))
        buf = bytes([0x81, 0x82]) + mask + masked
        opcode, payload, consumed = ws.decode_frame(buf)
        self.assertEqual(opcode, 0x1)
        self.assertEqual(payload, b"hi")
        self.assertEqual(consumed, len(buf))

    def test_decode_incomplete_returns_none(self):
        self.assertEqual(ws.decode_frame(b"\x81"), (None, b"", 0))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest tests.test_ws -v`
Expected: FAIL — `No module named 'dashboard.ws'`.

- [ ] **Step 3: Write minimal implementation**

```python
# dashboard/ws.py
"""ws.py — minimal RFC 6455 WebSocket framing for the stdlib server.

Pure functions only (no sockets), so they are fully unit-testable. The server's
/ws/terminal handler uses these to talk to the browser's xterm.js client.
"""
import base64
import hashlib
import struct

_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

OP_TEXT = 0x1
OP_BIN = 0x2
OP_CLOSE = 0x8
OP_PING = 0x9
OP_PONG = 0xA


def accept_key(client_key):
    digest = hashlib.sha1((client_key + _GUID).encode()).digest()
    return base64.b64encode(digest).decode()


def encode_frame(payload, opcode=OP_TEXT):
    b0 = 0x80 | (opcode & 0x0F)  # FIN=1
    n = len(payload)
    if n < 126:
        header = struct.pack("!BB", b0, n)
    elif n < (1 << 16):
        header = struct.pack("!BBH", b0, 126, n)
    else:
        header = struct.pack("!BBQ", b0, 127, n)
    return header + payload


def decode_frame(buf):
    """Decode one frame from the front of buf. Returns (opcode, payload, consumed)
    or (None, b"", 0) if a full frame is not yet present."""
    if len(buf) < 2:
        return (None, b"", 0)
    b0, b1 = buf[0], buf[1]
    opcode = b0 & 0x0F
    masked = b1 & 0x80
    length = b1 & 0x7F
    idx = 2
    if length == 126:
        if len(buf) < idx + 2:
            return (None, b"", 0)
        length = struct.unpack("!H", buf[idx:idx + 2])[0]
        idx += 2
    elif length == 127:
        if len(buf) < idx + 8:
            return (None, b"", 0)
        length = struct.unpack("!Q", buf[idx:idx + 8])[0]
        idx += 8
    if masked:
        if len(buf) < idx + 4:
            return (None, b"", 0)
        mask = buf[idx:idx + 4]
        idx += 4
    if len(buf) < idx + length:
        return (None, b"", 0)
    data = buf[idx:idx + length]
    if masked:
        data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
    return (opcode, data, idx + length)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m unittest tests.test_ws -v`
Expected: PASS (all four).

- [ ] **Step 5: Commit**

```bash
git add dashboard/ws.py tests/test_ws.py
git commit -m "feat(ws): stdlib RFC6455 frame codec + accept key"
```

---

### Task 7: `TerminalSession` — PTY-backed claude process

**Files:**
- Create: `dashboard/terminal_session.py`
- Test: `tests/test_terminal_session.py`

**Interfaces:**
- Produces:
  - `class TerminalSession(cmd: list[str], cwd: str)`.
    - `start(on_output: Callable[[bytes], None]) -> None`: spawn `cmd` on a PTY; a reader thread calls `on_output(chunk)` until EOF.
    - `write(data: bytes) -> None`: forward bytes to the PTY (keystrokes).
    - `resize(cols: int, rows: int) -> None`: `TIOCSWINSZ` + `SIGWINCH`.
    - `close() -> None`: terminate child, close fds, join thread.
- Consumes: nothing.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_terminal_session.py
import threading, time, unittest
import dashboard.terminal_session as ts


class Term(unittest.TestCase):
    def test_echo_roundtrip(self):
        got = bytearray()
        done = threading.Event()

        def on_output(chunk):
            got.extend(chunk)
            if b"PONG" in got:
                done.set()

        # `cat` echoes stdin back to its PTY stdout
        sess = ts.TerminalSession(cmd=["cat"], cwd=".")
        sess.start(on_output)
        sess.write(b"PONG\n")
        done.wait(timeout=5)
        sess.close()
        self.assertIn(b"PONG", bytes(got))

    def test_resize_no_crash(self):
        sess = ts.TerminalSession(cmd=["cat"], cwd=".")
        sess.start(lambda c: None)
        sess.resize(100, 40)   # must not raise
        sess.close()


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest tests.test_terminal_session -v`
Expected: FAIL — `No module named 'dashboard.terminal_session'`.

- [ ] **Step 3: Write minimal implementation**

```python
# dashboard/terminal_session.py
"""terminal_session.py — a claude process attached to a PTY.

The bottom integrated terminal runs full (unrestricted) `claude` here; bytes are
bridged to the browser's xterm.js over the /ws/terminal WebSocket. The chat
agent's osctl guard rail does NOT apply to this surface — it is the full-trust
power terminal.
"""
import fcntl
import os
import pty
import signal
import struct
import termios
import threading


class TerminalSession:
    def __init__(self, cmd, cwd):
        self.cmd = cmd
        self.cwd = cwd
        self.pid = None
        self.fd = None
        self._reader = None
        self._alive = False

    def start(self, on_output):
        self.pid, self.fd = pty.fork()
        if self.pid == 0:  # child
            try:
                os.chdir(self.cwd)
                os.execvp(self.cmd[0], self.cmd)
            except Exception:
                os._exit(1)
        self._alive = True

        def pump():
            while self._alive:
                try:
                    data = os.read(self.fd, 4096)
                except OSError:
                    break
                if not data:
                    break
                on_output(data)
            self._alive = False

        self._reader = threading.Thread(target=pump, daemon=True)
        self._reader.start()

    def write(self, data):
        if self.fd is not None:
            try:
                os.write(self.fd, data)
            except OSError:
                pass

    def resize(self, cols, rows):
        if self.fd is None:
            return
        winsize = struct.pack("HHHH", rows, cols, 0, 0)
        try:
            fcntl.ioctl(self.fd, termios.TIOCSWINSZ, winsize)
            if self.pid:
                os.kill(self.pid, signal.SIGWINCH)
        except OSError:
            pass

    def close(self):
        self._alive = False
        if self.pid:
            try:
                os.kill(self.pid, signal.SIGTERM)
            except OSError:
                pass
        if self.fd is not None:
            try:
                os.close(self.fd)
            except OSError:
                pass
        self.fd = None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m unittest tests.test_terminal_session -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add dashboard/terminal_session.py tests/test_terminal_session.py
git commit -m "feat(terminal): PTY-backed TerminalSession with resize"
```

---

### Task 8: `/ws/terminal` WebSocket handler in `server.py`

**Files:**
- Modify: `dashboard/server.py` (import `ws`, `terminal_session`; add upgrade handling in `do_GET`; add `_handle_terminal_ws`)
- Manual verification (no unit test — exercises a live socket; covered by Task 6/7 unit tests + manual smoke).

**Interfaces:**
- Consumes: `ws.accept_key/encode_frame/decode_frame` (Task 6), `terminal_session.TerminalSession` (Task 7).
- Produces: `GET /ws/terminal` upgrades to WebSocket and bridges a `claude` PTY. Text frames `{"type":"resize","cols":..,"rows":..}` call `resize`; other inbound frames are written to the PTY; PTY output is sent as binary frames.

- [ ] **Step 1: Implement (add imports near the others)**

```python
import ws                # noqa: E402
import terminal_session  # noqa: E402
```

- [ ] **Step 2: Add upgrade detection at the top of `do_GET`** (before the `/` route)

```python
        if path == "/ws/terminal" and self.headers.get("Upgrade", "").lower() == "websocket":
            return self._handle_terminal_ws()
```

- [ ] **Step 3: Add the handler method**

```python
    def _handle_terminal_ws(self):
        key = self.headers.get("Sec-WebSocket-Key")
        if not key:
            return self._send(400, {"error": "missing Sec-WebSocket-Key"})
        self.send_response(101, "Switching Protocols")
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", ws.accept_key(key))
        self.end_headers()

        sock = self.connection
        lock = threading.Lock()

        def on_output(chunk):
            with lock:
                try:
                    sock.sendall(ws.encode_frame(chunk, ws.OP_BIN))
                except OSError:
                    pass

        term = terminal_session.TerminalSession(cmd=["claude"], cwd=str(ROOT))
        term.start(on_output)

        buf = b""
        try:
            while True:
                data = sock.recv(4096)
                if not data:
                    break
                buf += data
                while True:
                    opcode, payload, consumed = ws.decode_frame(buf)
                    if opcode is None:
                        break
                    buf = buf[consumed:]
                    if opcode == ws.OP_CLOSE:
                        raise ConnectionError
                    if opcode == ws.OP_PING:
                        with lock:
                            sock.sendall(ws.encode_frame(payload, ws.OP_PONG))
                        continue
                    # Control message (resize) vs keystrokes
                    handled = False
                    if opcode == ws.OP_TEXT:
                        try:
                            msg = json.loads(payload.decode())
                            if isinstance(msg, dict) and msg.get("type") == "resize":
                                term.resize(int(msg["cols"]), int(msg["rows"]))
                                handled = True
                        except (ValueError, KeyError):
                            handled = False
                    if not handled:
                        term.write(payload)
        except (OSError, ConnectionError):
            pass
        finally:
            term.close()
```

- [ ] **Step 4: Manual smoke test**

Run: `python3 dashboard/server.py --port 8765` (in repo root), then in a browser console:
```js
const s = new WebSocket("ws://127.0.0.1:8765/ws/terminal");
s.binaryType = "arraybuffer";
s.onmessage = e => console.log(new TextDecoder().decode(e.data));
s.onopen = () => s.send(JSON.stringify({type:"resize",cols:80,rows:24}));
```
Expected: console prints the `claude` startup output. Then `s.send("help\n")` echoes into the session. Ctrl-C the server when done.

- [ ] **Step 5: Run the full suite to confirm nothing regressed**

Run: `python -m unittest discover -s tests -v`
Expected: all existing + new Python tests PASS.

- [ ] **Step 6: Commit**

```bash
git add dashboard/server.py
git commit -m "feat(server): /ws/terminal WebSocket bridge to claude PTY"
```

---

### Task 9: Vendor xterm.js + Layout A in `app.html`

**Files:**
- Create: `dashboard/vendor/xterm.js`, `dashboard/vendor/xterm.css`, `dashboard/vendor/xterm-addon-fit.js`
- Modify: `dashboard/server.py` (serve `/vendor/*` static files)
- Modify: `dashboard/app.html` (Layout A; right chat dock streaming SSE; bottom terminal)
- Manual verification (frontend; no JS unit harness in repo).

**Interfaces:**
- Consumes: `/api/ask` SSE (Task 5), `/ws/terminal` (Task 8).
- Produces: the running app UI in Layout A.

- [ ] **Step 1: Vendor the assets**

Download pinned files into `dashboard/vendor/` (no npm):
```bash
mkdir -p dashboard/vendor
curl -L https://cdn.jsdelivr.net/npm/xterm@5.3.0/lib/xterm.js          -o dashboard/vendor/xterm.js
curl -L https://cdn.jsdelivr.net/npm/xterm@5.3.0/css/xterm.css         -o dashboard/vendor/xterm.css
curl -L https://cdn.jsdelivr.net/npm/xterm-addon-fit@0.8.0/lib/xterm-addon-fit.js -o dashboard/vendor/xterm-addon-fit.js
```
Verify each file is non-empty and starts with JS/CSS (not an HTML error page): `head -c 80 dashboard/vendor/xterm.js`.

- [ ] **Step 2: Serve `/vendor/*` in `server.py`** (add in `do_GET`, after the `/` route, before the `/api/` guard)

```python
        if path.startswith("/vendor/"):
            f = (HERE / path.lstrip("/")).resolve()
            if f.is_file() and str(f).startswith(str(HERE / "vendor")):
                ctype = "text/css" if f.suffix == ".css" else "application/javascript"
                return self._send(200, f.read_bytes(), ctype)
            return self._send(404, {"error": "not found"})
```

- [ ] **Step 3: Restructure `app.html` to Layout A**

Replace the current bottom consultant drawer markup (around the `#chat-input` / "consultant terminal" section, `app.html:128`, `:227`, `:659`+) with:
- A CSS grid shell: `[ nav | center | chat ]` columns; the bottom terminal is a fixed-height panel under `center` that is hidden when collapsed.
- **Right chat dock** (`#chat-dock`): the existing message list + input move here; add a collapse chevron `#chat-collapse`. Persist collapsed state: `localStorage.setItem("chatCollapsed", ...)`; on load, apply it. When collapsed, the grid drops the chat column so center goes full width.
- **Bottom terminal** (`#term-panel`, hidden by default): a container `#term` for xterm + a header with a close button. Add `<link rel="stylesheet" href="/vendor/xterm.css">` and `<script src="/vendor/xterm.js"></script>`, `<script src="/vendor/xterm-addon-fit.js"></script>` in `<head>`.

Keep all existing center views (calendar/operations/tree) unchanged in the center column.

- [ ] **Step 4: Update the chat send handler to consume SSE incrementally**

Replace the existing `fetch("/api/ask")` handling (around `app.html:772`) with streaming reads:

```js
async function sendChat(text) {
  const bubble = addMsg("assistant", "");      // empty bubble to fill
  const chips = [];
  const resp = await fetch("/api/ask", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ messages: chatHistory })  // existing history array
  });
  const reader = resp.body.getReader();
  const dec = new TextDecoder();
  let buf = "";
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += dec.decode(value, { stream: true });
    let nl;
    while ((nl = buf.indexOf("\n\n")) !== -1) {
      const line = buf.slice(0, nl).trim();
      buf = buf.slice(nl + 2);
      if (!line.startsWith("data: ")) continue;
      const payload = line.slice(6);
      if (payload === "[DONE]") { await refreshViews(); return; }
      const obj = JSON.parse(payload);
      if (obj.delta) { bubble.textContent += obj.delta; scrollChat(); }
      else if (obj.tool) { addChip(bubble, "⚙ " + obj.tool); }
      else if (obj.error) { bubble.textContent += "\n[error] " + obj.error; }
    }
  }
  await refreshViews();
}
```

Where `refreshViews()` re-fetches `/api/tree` and `/api/timeline` and re-renders (reuse the existing load functions), so entities the agent created via osctl appear immediately ("act directly, show result"). `addChip` appends a small styled span to the bubble.

- [ ] **Step 5: Wire the bottom terminal (lazy connect on first open)**

```js
let term, termSock, termFit;
function toggleTerminal() {
  const panel = document.getElementById("term-panel");
  const opening = panel.classList.toggle("open");
  if (opening && !term) initTerminal();
  if (opening && termFit) termFit.fit();
}
function initTerminal() {
  term = new Terminal({ fontFamily: "ui-monospace, monospace", fontSize: 12,
                        theme: { background: "#1e1e28" } });
  termFit = new FitAddon.FitAddon();
  term.loadAddon(termFit);
  term.open(document.getElementById("term"));
  termFit.fit();
  termSock = new WebSocket(`ws://${location.host}/ws/terminal`);
  termSock.binaryType = "arraybuffer";
  termSock.onopen = () => sendResize();
  termSock.onmessage = e => term.write(new Uint8Array(e.data));
  term.onData(d => termSock.readyState === 1 && termSock.send(d));
  window.addEventListener("resize", () => { termFit.fit(); sendResize(); });
}
function sendResize() {
  if (termSock && termSock.readyState === 1)
    termSock.send(JSON.stringify({ type: "resize", cols: term.cols, rows: term.rows }));
}
// Cursor-style toggle: Ctrl+`
document.addEventListener("keydown", e => {
  if (e.ctrlKey && e.key === "`") { e.preventDefault(); toggleTerminal(); }
});
```

Bind the terminal header button and `#chat-collapse` chevron to `toggleTerminal()` / a `toggleChat()` that flips the grid + persists to localStorage.

- [ ] **Step 6: Manual verification**

Run: `python3 dashboard/server.py --port 8765`, open `http://127.0.0.1:8765`.
Verify:
1. Chat is docked right; type "add a project called Demo" → assistant text streams token-by-token, a "⚙ Bash" chip appears, and after `[DONE]` the new project shows in the nav/timeline without reload.
2. The collapse chevron hides the chat and the center view goes full width; reload preserves the collapsed state.
3. ``Ctrl+` `` opens the bottom terminal; `claude` starts in it; typing works; resizing the window keeps it fitted; the close button hides it.

- [ ] **Step 7: Commit**

```bash
git add dashboard/app.html dashboard/server.py dashboard/vendor/
git commit -m "feat(ui): Layout A — collapsible chat dock + embedded xterm terminal"
```

---

### Task 10: Session lifecycle cleanup + full-suite regression

**Files:**
- Modify: `dashboard/server.py` (close chat + terminal children on shutdown)

- [ ] **Step 1: Ensure clean shutdown**

In the server's shutdown path (the `/quit` handler / `KeyboardInterrupt` in `main`), add: if `_CHAT` exists and its process is alive, terminate it. (Per-connection `TerminalSession`s already `close()` in the `finally` of `_handle_terminal_ws`.) Keep it minimal — a `try/except` around process termination so quitting never hangs.

- [ ] **Step 2: Run the full test suite**

Run: `python -m unittest discover -s tests -v`
Expected: ALL tests pass (existing 12 + `test_osctl`, `test_chat_session`, `test_server_ask`, `test_ws`, `test_terminal_session`).

- [ ] **Step 3: End-to-end manual smoke**

Launch the `.app` (or `python3 dashboard/server.py`); confirm: chat creates a real entity via osctl (check the file appears under `projects/`), terminal runs `claude` with skills available, quitting the app leaves no orphaned `claude` processes (`pgrep -fl claude`).

- [ ] **Step 4: Commit**

```bash
git add dashboard/server.py
git commit -m "chore(server): terminate AI child processes on shutdown"
```

---

## Self-Review Notes

- **Spec coverage:** Engine (T4–T5), osctl rail incl. all 9 subcommands (T1–T3), act-directly + view refresh (T5, T9 Step 4), embedded xterm + WS/PTY (T6–T9), Layout A collapsible chat + toggle terminal (T9), tests for osctl/WS/chat-SSE (T1–T6), shutdown (T10). Covered.
- **Schema risk flagged:** stream-json field paths (T4 Step 0) and the `claude` stdin-turn shape (T4 note) require a one-time confirmation against the installed CLI; the plan instructs updating parser+stub together.
- **No new deps:** all Python is stdlib; only `xterm.js` assets are vendored (allowed, no build step).
- **Type consistency:** `parse_event` kinds (`delta`/`tool`/`done`/`error`) are produced in T4 and consumed identically in T5; `ws` opcode constants and `(opcode,payload,consumed)` decode contract are produced in T6 and consumed in T8; `TerminalSession.start/write/resize/close` produced in T7, consumed in T8.
