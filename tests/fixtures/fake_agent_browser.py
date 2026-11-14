#!/usr/bin/env python3
"""A stand-in agent-browser for tests: `mcp ...` speaks MCP over stdio, `... close` exits.

It logs every start, call and close to $HOME/fake-log.jsonl (with its argv and AGENT_BROWSER_*
environment) and reads $HOME/fake.json for what to do: {"crash_on": tool} exits mid-call,
{"hang_on": tool} never answers, {"bad_list": true} answers tools/list with an error.
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


def main():
    env = {k: v for k, v in os.environ.items() if k.startswith("AGENT_BROWSER_")}
    if sys.argv[-1] == "close":
        log("close", argv=sys.argv[1:], env=env)
        return 0
    log("start", argv=sys.argv[1:], env=env, keys=sorted(os.environ))
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
            if ctl.get("hang_on") == name:
                time.sleep(3600)
            if name == "agent_browser_open" and args.get("url"):
                url = args["url"]
            text = url if name == "agent_browser_get_url" else "ok"
            result = {"content": [{"type": "text", "text": text}], "isError": False}
        else:
            print(json.dumps({"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": "no"}}), flush=True)
            continue
        print(json.dumps({"jsonrpc": "2.0", "id": rid, "result": result}), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
