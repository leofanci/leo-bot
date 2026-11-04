"""os_relay.py — the one myOS tool of a shared thread's bot.

A thread an invited person can see (core.people) runs with no shell and no
file tools: Claude Code lets read-only tools and shell commands (Read, cat,
grep, `< file`) touch any path on the machine without asking, so a person's
message could otherwise read the owner's files. Instead the turn's `claude -p`
gets one HTTP MCP server pointing back at the dashboard
(POST /api/internal/os/mcp) with a per-request turn token, and one tool:

  osctl   runs `python -m dashboard.osctl <args>` on the server, with the
          turn's workspace bound from the token (MYOS_WORKSPACE, never the
          bot's) and MYOS_SHARED set, so osctl applies its shared rules.

Arguments go to the process as a list (no shell); a body travels as `stdin`.
"""
import os
import subprocess
import sys
from pathlib import Path

import apps_relay
import chat_session
from core import bots

TOKENS = apps_relay.Tokens()  # own store: an apps token never opens this relay
TOOL = "osctl"
MAX_ARGS = 64
MAX_ARG_CHARS = 100_000
MAX_STDIN_CHARS = 400_000
MAX_OUT_CHARS = 120_000
TIMEOUT = 600  # generate-* commands run a model turn
PROTOCOL = "2025-06-18"
SHARED_ENV = "MYOS_SHARED"
CODE_DIR = Path(__file__).resolve().parent.parent  # where `-m dashboard.osctl` imports from

TOOL_SPEC = {
    "name": TOOL,
    "description": ("Run one myOS command (the osctl CLI) in this workspace. `args` = the words "
                    "after `python3 -m dashboard.osctl`, one per item, e.g. "
                    "[\"get-project\", \"--slug\", \"acme\"]. Long bodies go in `stdin` instead of --text. "
                    "Returns the command's JSON output."),
    "inputSchema": {
        "type": "object",
        "properties": {
            "args": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": MAX_ARGS},
            "stdin": {"type": "string"},
        },
        "required": ["args"],
        "additionalProperties": False,
    },
}


def _error(rid, code, message):
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}


def _text(rid, text, is_error=False):
    return {"jsonrpc": "2.0", "id": rid,
            "result": {"content": [{"type": "text", "text": text}], "isError": is_error}}


def clean_args(arguments):
    """(argv list, stdin) from a tools/call, or raise ValueError."""
    if not isinstance(arguments, dict):
        raise ValueError("arguments must be an object")
    args, stdin = arguments.get("args"), arguments.get("stdin")
    if not isinstance(args, list) or not 1 <= len(args) <= MAX_ARGS:
        raise ValueError(f"args: a list of 1-{MAX_ARGS} strings")
    if not all(isinstance(a, str) and len(a) <= MAX_ARG_CHARS and "\x00" not in a for a in args):
        raise ValueError("args: strings only, no NUL")
    if stdin is not None and (not isinstance(stdin, str) or len(stdin) > MAX_STDIN_CHARS or "\x00" in stdin):
        raise ValueError("stdin: a string")
    return args, stdin or ""


def env_for(ctx):
    """The child's environment: the token's workspace, never a caller's."""
    drop = {chat_session.APPS_TOKEN_ENV, chat_session.APPS_USED_ENV, chat_session.OS_TOKEN_ENV,
            chat_session.ENGINE_ENV, "MYOS_WORKSPACE", "MYOS_READONLY", SHARED_ENV}
    env = {k: v for k, v in os.environ.items() if k not in drop}
    env["MYOS_WORKSPACE"] = ctx["ws"]
    if ctx.get("shared", True):  # an owner's private turn on the api engine sets it False
        env[SHARED_ENV] = "1"
    if ctx.get("engine"):  # a generate-* job runs on the engine its turn runs on
        env[chat_session.ENGINE_ENV] = ctx["engine"]
    if ctx.get("readonly"):
        env["MYOS_READONLY"] = "1"
    if ctx.get("apps_used"):
        env[chat_session.APPS_USED_ENV] = "1"
    return env


def run_osctl(args, stdin, ctx, root=bots.ROOT):
    """osctl as a child process; it finds the data itself (fileops.ROOT)."""
    try:
        out = subprocess.run([sys.executable, "-m", "dashboard.osctl", *args], input=stdin,
                             capture_output=True, text=True, cwd=str(CODE_DIR), env=env_for(ctx),
                             timeout=TIMEOUT)
    except subprocess.TimeoutExpired:
        return "the command took too long and was stopped", True
    text = (out.stdout or "").strip() or (out.stderr or "").strip()[-2000:] or "(no output)"
    return text[:MAX_OUT_CHARS], out.returncode != 0


def handle(frame, ctx, root=bots.ROOT, runner=run_osctl):
    """One JSON-RPC frame from a bot -> reply, or None for a notification."""
    if not isinstance(frame, dict):
        return _error(None, -32600, "invalid request")
    method, rid = frame.get("method") or "", frame.get("id")
    if method.startswith("notifications/"):
        return None
    if method == "initialize":
        params = frame.get("params") if isinstance(frame.get("params"), dict) else {}
        version = params.get("protocolVersion")
        return {"jsonrpc": "2.0", "id": rid, "result": {
            "protocolVersion": version if isinstance(version, str) and len(version) < 32 else PROTOCOL,
            "capabilities": {"tools": {}}, "serverInfo": {"name": "myos", "version": "1"}}}
    if method == "ping":
        return {"jsonrpc": "2.0", "id": rid, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": rid, "result": {"tools": [TOOL_SPEC]}}
    if method != "tools/call":
        return _error(rid, -32601, "method not available")
    params = frame.get("params") if isinstance(frame.get("params"), dict) else {}
    if params.get("name") != TOOL:
        return _text(rid, f'"{params.get("name")}" is not available here. Only osctl is.', True)
    try:
        args, stdin = clean_args(params.get("arguments"))
    except ValueError as e:
        return _text(rid, str(e), True)
    text, failed = runner(args, stdin, ctx, root)
    return _text(rid, text, failed)
