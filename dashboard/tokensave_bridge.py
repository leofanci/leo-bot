"""Small, read-only TokenSave adapter for the embedded myOS consultant.

The chat process may call only this module, never arbitrary TokenSave install,
sync, edit, or administration commands. A project slug is resolved through its
authored local_folder link, and graph output is hard-capped before it reaches the
model context.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

from dashboard import fileops


MAX_QUERY_CHARS = 1200
MAX_OUTPUT_CHARS = 10_000
SYNC_TIMEOUT_SECONDS = 45
ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
OTHER_PROJECTS_RE = re.compile(r"\n### Other initialized projects\n.*\Z", re.S)
PATH_EXCLUDES = [
    "node_modules", "vendor", ".venv", "dist", "build", ".next",
    "coverage", "__pycache__", ".tokensave", ".env", "secret",
    "credential", "private_key", ".pem", ".p12", ".key",
]


class BridgeError(RuntimeError):
    pass


def _linked_workspace(project_slug: str, active_project=None) -> str:
    active_slug = (
        str(active_project).strip()
        if active_project is not None
        else os.environ.get("WORKSPACE_ACTIVE_PROJECT", "").strip()
    )
    if not active_slug:
        raise BridgeError("TokenSave bridge requires an active myOS project")
    if project_slug != active_slug:
        raise BridgeError("requested project is not the active linked project")
    try:
        folder = fileops.project_local_folder(project_slug)
    except (fileops.ActionError, OSError) as exc:
        raise BridgeError(str(exc)) from exc
    path = Path(folder).resolve()
    if not (path / ".tokensave" / "tokensave.db").is_file():
        raise BridgeError(
            "linked app has no TokenSave index; run `tokensave init` in that app once"
        )
    # Require the graph at the exact linked root. Accepting an ancestor graph
    # can leak sibling symbol names through TokenSave's relationship summaries,
    # even when code results use path_include filters.
    return str(path)


def _binary() -> str:
    binary = shutil.which("tokensave")
    if not binary:
        raise BridgeError("TokenSave is not installed or is not on PATH")
    return binary


def _clean(text: str) -> str:
    cleaned = ANSI_RE.sub("", text or "").strip()
    # The CLI advertises sibling initialized repositories after context output.
    # That is irrelevant here and would both spend tokens and invite scope drift.
    return OTHER_PROJECTS_RE.sub("", cleaned).rstrip()


def _bounded(value: str, label: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise BridgeError(f"{label} is required")
    if len(text) > MAX_QUERY_CHARS:
        raise BridgeError(f"{label} is too long (max {MAX_QUERY_CHARS} characters)")
    return text


def _run_tool(project_slug: str, tool: str, args: dict, *, active_project=None) -> str:
    graph_root = _linked_workspace(project_slug, active_project)
    binary = _binary()
    # Refresh only TokenSave's derived local cache. This incremental operation
    # never modifies app source files and its output is not sent to the model.
    try:
        sync = subprocess.run(
            [binary, "sync", graph_root],
            capture_output=True,
            text=True,
            timeout=SYNC_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise BridgeError(
            f"TokenSave refresh timed out after {SYNC_TIMEOUT_SECONDS} seconds"
        ) from exc
    if sync.returncode != 0:
        detail = _clean(sync.stderr) or _clean(sync.stdout) or "unknown sync error"
        raise BridgeError(f"could not refresh TokenSave index: {detail[-1200:]}")
    try:
        result = subprocess.run(
            [binary, "tool", tool, "--project", graph_root,
             "--args", json.dumps(args)],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except subprocess.TimeoutExpired as exc:
        raise BridgeError("TokenSave context lookup timed out after 30 seconds") from exc
    output = _clean(result.stdout)
    if result.returncode != 0:
        detail = _clean(result.stderr) or output or "unknown TokenSave error"
        raise BridgeError(detail[-2000:])
    if not output:
        raise BridgeError("TokenSave returned no graph context")
    if len(output) > MAX_OUTPUT_CHARS:
        output = output[:MAX_OUTPUT_CHARS].rstrip() + "\n\n[TokenSave output capped]"
    return output


def context(project_slug: str, task: str, *, active_project=None, include_code=True) -> str:
    """`include_code=False`: names and structure only, for a thread invited people can see."""
    return _run_tool(project_slug, "context", {
        "task": _bounded(task, "task"),
        "mode": "explore",
        "include_code": bool(include_code),
        "max_nodes": 10,
        "max_per_file": 2,
        "max_code_blocks": 4,
        "max_code_lines": 80,
        "path_exclude": PATH_EXCLUDES,
    }, active_project=active_project)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Read-only TokenSave bridge for myOS")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("context")

    args = parser.parse_args(argv)
    try:
        output = context(
            os.environ.get("WORKSPACE_ACTIVE_PROJECT", ""),
            os.environ.get("WORKSPACE_TOKENSAVE_TASK", ""),
        )
    except BridgeError as exc:
        parser.exit(2, f"tokensave bridge: {exc}\n")
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
