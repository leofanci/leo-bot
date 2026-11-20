"""turn_hook.py — Claude Code PreToolUse hook: asks the server's gate before a web tool or a
Full access shell command runs.

Engines run its source inline, `python3 -I -c <source>`, read once when the server starts
(claude_code.HOOK_COMMAND): a shell command that rewrites this file changes nothing a later
command asks. Standard library only, nothing from the bot's folders. Claude Code and Codex wait
for it, so the answer holds at the moment the tool would run. Any failure (no token, server
unreachable, a bad reply) denies: the gate fails closed. An allow prints nothing, so the
engine's own permission rules still apply (fetch stays limited to typed hosts, Approve for me
still asks the engine's reviewer). On Full access a shell command comes back from the gate
rewritten to run in the OS sandbox (dashboard/shell_rules.py: from the first command for the code
wall, and after outside content also offline and write-limited): the hook passes that one on and
allows it. On Approve for me the gate rewrites nothing and the hook stays silent, so Claude Code's
own reviewer judges the command as written (a hook "allow" skips it and an "ask" is refused with no
prompt, both verified live with 2.1.325); the sandbox comes from the review shell every command
runs in (shell_rules.review_shell). Leo Bot's own osctl comes back as a one-time ticket that shell
redeems, so it runs as on Full access. On that level the same hook also runs after each Bash command
(PostToolUse, PostToolUseFailure) and tells the gate it ran: a command the review shell never asked
for means Claude Code no longer runs commands in it, and the gate refuses every later one.
"""
import json
import os
import sys
import urllib.request

TOKEN_ENV = "LEOBOT_GATE_TOKEN"
URL_ENV = "LEOBOT_GATE_URL"


def _deny(reason):
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                             "permissionDecisionReason": reason}}))


def _report_run(url, token, event):
    """Tell the gate a Bash command ran, so it can check the review shell ran it (taint.Gates.ran).
    Prints nothing: after the run there is no decision to make. A lost report skips that one
    command's check only."""
    try:
        body = {"tool": "Bash-ran", "id": event.get("tool_use_id") if isinstance(event.get("tool_use_id"), str) else None}
        req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                     headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10):  # noqa: S310  (127.0.0.1 only, checked by main)
            pass
    except Exception:  # noqa: BLE001  nothing to deny after the run
        pass
    return None


def main():
    try:
        event = json.load(sys.stdin)
        tool = event.get("tool_name") if isinstance(event, dict) else None
        token, url = os.environ.get(TOKEN_ENV), os.environ.get(URL_ENV)
        ok = isinstance(tool, str) and token and url and url.startswith("http://127.0.0.1:")
        if isinstance(event, dict) and event.get("hook_event_name") in ("PostToolUse", "PostToolUseFailure"):
            return _report_run(url, token, event) if ok else None  # Approve for me: a Bash command ran
        if not ok:
            return _deny("This tool is off: Leo Bot could not check it.")
        body = {"tool": tool}
        if isinstance(event.get("tool_use_id"), str):
            body["id"] = event["tool_use_id"]
        if tool == "Bash":  # the gate reads the command itself (dashboard/shell_rules.py)
            body["command"] = str((event.get("tool_input") or {}).get("command") or "")
            if isinstance(event.get("cwd"), str) and event["cwd"].startswith("/"):
                body["cwd"] = event["cwd"]  # the engine's shell keeps a `cd` across commands
        elif tool in ("Edit", "Write", "MultiEdit", "NotebookEdit"):  # Approve for me: edit folders only
            ti = event.get("tool_input") or {}
            body["path"] = str(ti.get("file_path") or ti.get("notebook_path") or "")
        req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                     headers={"Authorization": f"Bearer {token}",
                                              "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310  (127.0.0.1 only, above)
            answer = json.loads(resp.read() or b"{}")
        if answer.get("allow") is True:
            run = answer.get("command")
            if tool == "Bash" and isinstance(run, str) and run:  # sandboxed after outside content
                print(json.dumps({"hookSpecificOutput": {
                    "hookEventName": "PreToolUse", "permissionDecision": "allow",
                    "permissionDecisionReason": "Leo Bot runs it in its sandbox (dashboard/shell_rules.py).",
                    "updatedInput": {**(event.get("tool_input") or {}), "command": run}}}))
            return None
        return _deny(str(answer.get("reason") or "This tool is off in this conversation."))
    except Exception:  # noqa: BLE001  any failure denies
        return _deny("This tool is off: Leo Bot could not check it.")


if __name__ == "__main__":
    main()
