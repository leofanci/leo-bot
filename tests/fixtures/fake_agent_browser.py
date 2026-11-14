#!/usr/bin/env python3
"""A stand-in agent-browser for tests: `mcp ...` speaks MCP over stdio, `... close` exits.

It logs every start, call and close to $HOME/fake-log.jsonl (with its argv and AGENT_BROWSER_*
environment) and reads $HOME/fake.json for what to do: {"crash_on": tool} exits mid-call,
{"hang_on": tool} never answers, {"bad_list": true} answers tools/list with an error, {"tab_gone_on": tool} answers
"tab_gone:" until agent_browser_tab_new runs. "settings" in a start record is
what the real engine would use from its config files and environment.
"""
import json
import os
import sys
import time

HOME = os.environ.get("HOME", ".")


def log(event, **more):
    with open(os.path.join(HOME, "fake-log.jsonl"), "a") as f:
        f.write(json.dumps({"event": event, "pid": os.getpid(), **more}) + "\n")


def control():
    try:
        with open(os.path.join(HOME, "fake.json")) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


TOOLS = [
    {"name": "agent_browser_open", "inputSchema": {"type": "object", "properties": {
        "url": {"type": "string"}, "session": {"type": "string"}, "timeoutMs": {"type": "number"}}}},
    {"name": "agent_browser_get_url", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "agent_browser_click", "inputSchema": {"type": "object", "properties": {
        "selector": {"type": "string"}}}},
]


def _load(path):
    try:
        with open(path) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def effective():
    """The settings the real engine would use: AGENT_BROWSER_CONFIG replaces the user and
    project files; env overrides; AGENT_BROWSER_AUTO_CONNECT turns auto-connect on when set at
    all, even to "false" (as probed on the real engine)."""
    given = os.environ.get("AGENT_BROWSER_CONFIG")
    if given:
        cfg = _load(given)
    else:
        cfg = {**_load(os.path.join(HOME, ".agent-browser", "config.json")), **_load("agent-browser.json")}
    if "AGENT_BROWSER_AUTO_CONNECT" in os.environ:
        cfg["autoConnect"] = True
    if "AGENT_BROWSER_HEADED" in os.environ:
        cfg["headed"] = os.environ["AGENT_BROWSER_HEADED"] not in ("false", "0", "")
    if "AGENT_BROWSER_CDP" in os.environ:
        cfg["cdp"] = os.environ["AGENT_BROWSER_CDP"]
    return {k: cfg.get(k) for k in ("autoConnect", "headed", "cdp", "proxy", "plugins")}


def main():
    env = {k: v for k, v in os.environ.items() if k.startswith("AGENT_BROWSER_")}
    if sys.argv[-1] == "close":
        log("close", argv=sys.argv[1:], env=env)
        return 0
    log("start", argv=sys.argv[1:], env=env, keys=sorted(os.environ), cwd=os.getcwd(), settings=effective())
    url = "about:blank"
    for raw in sys.stdin:
        msg = json.loads(raw)
        method, rid = msg.get("method"), msg.get("id")
        if rid is None:
            continue
        ctl = control()
        if method == "initialize":
            result = {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}},
                      "serverInfo": {"name": "agent-browser", "version": "0"}}
        elif method == "tools/list":
            if ctl.get("bad_list"):
                print(json.dumps({"jsonrpc": "2.0", "id": rid, "error": {"code": -1, "message": "no"}}), flush=True)
                continue
            result = {"tools": TOOLS}
        elif method == "tools/call":
            name = msg["params"]["name"]
            args = msg["params"].get("arguments") or {}
            log("call", name=name, args=args)
            if ctl.get("crash_on") == name:
                os._exit(3)
            if ctl.get("tab_gone_on") == name and not os.path.exists(os.path.join(HOME, "tab-new-done")):
                result = {"content": [{"type": "text", "text": "tab_gone: bound tab is gone"}], "isError": True}
                print(json.dumps({"jsonrpc": "2.0", "id": rid, "result": result}), flush=True)
                continue
            if name == "agent_browser_tab_new":
                open(os.path.join(HOME, "tab-new-done"), "w").close()
            if ctl.get("hang_on") == name:
                time.sleep(3600)
            if name == "agent_browser_open" and args.get("url"):
                url = args["url"]
            text = url if name == "agent_browser_get_url" else "ok"
            result = {"content": [{"type": "text", "text": text}], "isError": False}
            if name == "agent_browser_eval":  # the relay's field read: no field holds text
                result["structuredContent"] = {"exitCode": 0, "response": {"success": True, "data": {
                    "origin": url, "result": {"fields": []}}}}
        else:
            print(json.dumps({"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": "no"}}), flush=True)
            continue
        print(json.dumps({"jsonrpc": "2.0", "id": rid, "result": result}), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
