"""acp.py: any agent CLI that speaks ACP (Agent Client Protocol: JSON-RPC over stdio).

Covers Gemini CLI, OpenCode and future CLIs without one adapter each. myOS is the
client: it answers permission requests (denying all but its own MCP tools) and
offers no file or terminal access of its own. Agents may still run read-only tools
without asking, so every ACP agent is a full-disk engine: only the owner's private
threads, only on the owner's own Mac, no connected apps (spec section 5a).
"""
import json
import os
import re
import shutil
import subprocess
import threading

from dashboard.engines.base import Engine, SessionLost
from dashboard.engines.claude_code import OS_MCP_PATH
from dashboard.engines.codex import FULL_DISK_NOTE

PROTOCOL_VERSION = 1
START_TIMEOUT = 60
# A clean environment: never myOS's tokens, never a key from the server's own env.
_ENV_KEEP = ("PATH", "HOME", "LANG", "LC_ALL", "TERM", "USER", "TMPDIR", "XDG_CONFIG_HOME", "XDG_DATA_HOME")
OS_TOOL_NAMES = ("os__osctl", "os_osctl", "os/osctl")
_OWN_TOOL_KINDS = ("", "other")  # an MCP tool. execute, edit, delete and the rest are the agent's own


def is_own_tool(tool_call):
    """True when a permission request is for myOS's own MCP tool. A title can hold the
    model's own text (`echo mcp__os__; rm -rf ~`), so the name must lead it, and the
    kind must not be one of the agent's own tools."""
    tc = tool_call or {}
    if str(tc.get("kind") or "").lower() not in _OWN_TOOL_KINDS:
        return False
    return any(_names_os_tool(str(tc.get(k) or "")) for k in ("title", "toolCallId"))


def _names_os_tool(text):
    """The text opens with one of myOS's tool names, whole: a longer name (`os__osctl_evil`)
    or a command that merely starts with one does not count."""
    low = text.strip().lower()
    if low.startswith("mcp__os__"):
        return True
    return any(re.match(re.escape(n) + r"(?![a-z0-9_])", low) for n in OS_TOOL_NAMES)


def permission_reply(params):
    """Allow only myOS's own tools. Any other request is rejected."""
    options = params.get("options") or []
    wanted = ("allow_once", "allow_always") if is_own_tool(params.get("toolCall")) else ("reject_once", "reject_always")
    for kind in wanted:
        for opt in options:
            if opt.get("kind") == kind:
                return {"outcome": {"outcome": "selected", "optionId": opt.get("optionId")}}
    return {"outcome": {"outcome": "cancelled"}}


def parse_update(update):
    """(kind, payload) for one session/update, or None."""
    kind = (update or {}).get("sessionUpdate")
    if kind == "agent_message_chunk":
        content = update.get("content") or {}
        if content.get("type") == "text" and content.get("text"):
            return ("delta", content["text"])
    elif kind == "tool_call":
        return ("tool", str(update.get("title") or update.get("kind") or "tool")[:60])
    return None


class AcpEngine(Engine):
    kind = "cli"
    shared_safe = False
    full_disk = True
    supports = frozenset()

    def __init__(self, agent):
        self.agent = agent
        self.id = f"acp:{agent['id']}"
        self.label = str(agent.get("name") or agent["id"])

    def resolve_model(self, model):
        return None  # the agent's own configured model

    def is_fresh(self, session):
        return not (session.engine_state.get(self.id) or {}).get("session")

    def _spawn(self, session):
        env = {k: os.environ[k] for k in _ENV_KEEP if k in os.environ}
        return subprocess.Popen([self.agent["command"], *self.agent.get("args", [])], cwd=session.repo_dir, env=env,
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                text=True, bufsize=1)

    def turn(self, session, text, policy, model, tokens):
        self.check(policy)
        state = session.engine_state.get(self.id) or {}
        session.engine_state.setdefault(self.id, {})
        try:
            proc = self._spawn(session)
        except OSError as e:
            yield ("error", f"{self.label} could not start: {e}")
            return
        session._proc = proc
        watchdog = threading.Timer(START_TIMEOUT, proc.kill)  # replaced by the turn's own wait below
        next_id = [0]
        lock = threading.Lock()

        def send(obj):
            with lock:
                proc.stdin.write(json.dumps({"jsonrpc": "2.0", **obj}) + "\n")
                proc.stdin.flush()

        def request(method, params):
            next_id[0] += 1
            send({"id": next_id[0], "method": method, "params": params})
            return next_id[0]

        def pump(want):
            """Yield events until the response to `want` arrives; then yield ("_reply", msg)."""
            for line in proc.stdout:
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(msg, dict):
                    continue
                method = msg.get("method")
                if method == "session/update":
                    ev = parse_update((msg.get("params") or {}).get("update"))
                    if ev:
                        yield ev
                elif method == "session/request_permission" and "id" in msg:
                    send({"id": msg["id"], "result": permission_reply(msg.get("params") or {})})
                elif method and "id" in msg:  # fs/*, terminal/*: not offered
                    send({"id": msg["id"], "error": {"code": -32601, "message": "not available"}})
                elif msg.get("id") == want:
                    yield ("_reply", msg)
                    return
            yield ("_reply", {"error": {"message": f"{self.label} stopped unexpectedly"}})

        def call(method, params):
            reply = None
            for ev in pump(request(method, params)):
                if ev[0] == "_reply":
                    reply = ev[1]
                else:
                    yield ev
            yield ("_result", reply)

        try:
            watchdog.start()
            init = None
            for ev in call("initialize", {"protocolVersion": PROTOCOL_VERSION,
                                          "clientCapabilities": {"fs": {"readTextFile": False, "writeTextFile": False},
                                                                 "terminal": False}}):
                init = ev[1] if ev[0] == "_result" else init
            if not init or init.get("error"):
                yield ("error", f"{self.label} did not start. Its installed version may be too old to speak ACP: update it.")
                return
            mcp = []
            if tokens.os_token and tokens.os_port:
                mcp = [{"type": "http", "name": "os", "url": f"http://127.0.0.1:{tokens.os_port}{OS_MCP_PATH}",
                        "headers": [{"name": "Authorization", "value": f"Bearer {tokens.os_token}"}]}]
            cwd = os.path.abspath(session.repo_dir)
            caps = (init.get("result") or {}).get("agentCapabilities") or {}
            sid, first = state.get("session"), True
            if sid and caps.get("loadSession"):
                loaded = None
                for ev in call("session/load", {"sessionId": sid, "cwd": cwd, "mcpServers": mcp}):
                    loaded = ev[1] if ev[0] == "_result" else loaded  # replayed history is dropped
                if not loaded or loaded.get("error"):
                    session.engine_state[self.id] = {}
                    raise SessionLost("session is gone")
                first = False
            else:
                if sid:  # this agent cannot reload a session
                    session.engine_state[self.id] = {}
                    raise SessionLost("session cannot be reloaded")
                made = None
                for ev in call("session/new", {"cwd": cwd, "mcpServers": mcp}):
                    made = ev[1] if ev[0] == "_result" else made
                if not made or made.get("error"):
                    yield ("error", f"{self.label}: {((made or {}).get('error') or {}).get('message', 'no session')}")
                    return
                sid = (made.get("result") or {}).get("sessionId")
                session.engine_state[self.id] = {"session": sid}
            watchdog.cancel()
            prompt = f"{session.rail}\n\n{text}" if first else text  # ACP has no system prompt
            done = None
            for ev in call("session/prompt", {"sessionId": sid, "prompt": [{"type": "text", "text": prompt}]}):
                if ev[0] == "_result":
                    done = ev[1]
                else:
                    yield ev
            if not done or done.get("error"):
                yield ("error", f"{self.label}: {((done or {}).get('error') or {}).get('message', 'no reply')}")
                return
            yield ("done", {"result": None, "subtype": "success", "tokens": 0, "cost": None})
        except (BrokenPipeError, ValueError):
            yield ("error", f"{self.label} stopped unexpectedly")
        finally:
            watchdog.cancel()
            try:
                proc.kill()
                proc.wait(timeout=5)
            except (OSError, subprocess.SubprocessError):
                pass
            for stream in (proc.stdin, proc.stdout):
                try:
                    stream.close()
                except (OSError, ValueError):
                    pass
            session._proc = None

    def probe(self, timeout=15):
        """The Test button: does it really speak ACP? An error string, or None."""
        try:
            proc = subprocess.Popen([self.agent["command"], *self.agent.get("args", [])],
                                    env={k: os.environ[k] for k in _ENV_KEEP if k in os.environ},
                                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        except OSError as e:
            return f"{self.label} could not start: {e}"
        timer = threading.Timer(timeout, proc.kill)
        timer.start()
        try:
            proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                         "params": {"protocolVersion": PROTOCOL_VERSION, "clientCapabilities": {}}}) + "\n")
            proc.stdin.flush()
            for line in proc.stdout:
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if isinstance(msg, dict) and msg.get("id") == 1:
                    return None if "result" in msg else f"{self.label} refused the connection."
        except (OSError, ValueError):
            pass
        finally:
            timer.cancel()
            proc.kill()
            proc.wait()
        return f"{self.label} did not answer. Its installed version may be too old to speak ACP: update it."

    def status(self):
        path = shutil.which(self.agent["command"])
        return {"id": self.id, "label": self.label, "kind": self.kind, "installed": bool(path), "ok": bool(path),
                "full_disk": True, "acp": True,
                "detail": f"{FULL_DISK_NOTE} Your private threads only." if path else "not installed"}
