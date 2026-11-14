"""turn_hook.py — Claude Code PreToolUse hook: asks the server's gate before a web tool or a
Full access shell command runs.

Run as `python3 -I turn_hook.py` (standard library only, nothing from the bot's folders).
Claude Code waits for it, so the answer holds at the moment the tool would run. Any failure
(no token, server unreachable, a bad reply) denies: the gate fails closed. An allow prints
nothing, so Claude Code's own permission rules still apply (fetch stays limited to typed hosts).
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


def main():
    try:
        event = json.load(sys.stdin)
        tool = event.get("tool_name") if isinstance(event, dict) else None
        token, url = os.environ.get(TOKEN_ENV), os.environ.get(URL_ENV)
        if not (isinstance(tool, str) and token and url and url.startswith("http://127.0.0.1:")):
            return _deny("This tool is off: Leo Bot could not check it.")
        body = {"tool": tool}
        if tool == "Bash":  # the gate reads the command itself (dashboard/shell_rules.py)
            body["command"] = str((event.get("tool_input") or {}).get("command") or "")
        req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                     headers={"Authorization": f"Bearer {token}",
                                              "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310  (127.0.0.1 only, above)
            answer = json.loads(resp.read() or b"{}")
        if answer.get("allow") is True:
            return None
        return _deny(str(answer.get("reason") or "This tool is off in this conversation."))
    except Exception:  # noqa: BLE001  any failure denies
        return _deny("This tool is off: Leo Bot could not check it.")


if __name__ == "__main__":
    main()
