"""codex.py: the codex engine. Runs `codex exec --json` on the person's own login.

Verified against codex-cli 0.162: JSONL events are thread.started {thread_id},
item.started / item.completed {item: {type, text | command | server, tool}} and
turn.completed {usage}; failures are error {message} and turn.failed {error}.
`exec resume <id>` has no --sandbox flag, so the sandbox goes through -c everywhere.

Every turn: read-only sandbox, no user config, shell tool off, connectors and
sub-agents off, osctl through Leo Bot's own MCP relay. Codex still keeps tools of its
own that no flag lists (a live probe in the PR shows them), so this is a full-disk
engine: only the owner's private threads, only on the owner's own Mac, no connected
apps (registry.pick and spec section 5a). It is never shared_safe.
"""
import json
import os
import shutil
import subprocess

from dashboard.engines import prices
from dashboard.engines.base import Engine, SessionLost, drain, stop_child
from dashboard.engines.claude_code import (
    APPS_TOKEN_ENV, APPS_USED_ENV, ENGINE_ENV, OS_MCP_PATH, OS_TOKEN_ENV, RUNNER_TOKEN_ENV,
)

ENGINE_ID = "codex"
FULL_DISK_NOTE = "This engine can read any file on this computer."
_OFF = ("features.shell_tool=false", "features.apps=false", "features.multi_agent=false",
        "features.image_generation=false", "features.goals=false")


def _toml(value):
    return json.dumps(value, ensure_ascii=False)  # TOML basic strings share JSON's escapes


def parse_event(obj):
    """(kind, payload) events from one JSONL line, as a list."""
    t = obj.get("type")
    if t == "item.started":
        item = obj.get("item") or {}
        if item.get("type") == "mcp_tool_call" and item.get("server") != "codex":
            return [("tool", f"{item.get('server')}__{item.get('tool')}")]
        if item.get("type") == "command_execution":
            return [("tool", "shell")]
    elif t == "item.completed":
        item = obj.get("item") or {}
        if item.get("type") == "agent_message" and item.get("text"):
            return [("delta", item["text"] + "\n\n")]
    elif t == "turn.completed":
        usage = obj.get("usage") or {}
        tokens = sum(int(usage.get(k) or 0) for k in ("input_tokens", "output_tokens"))
        cached = int(usage.get("cached_input_tokens") or 0)
        parts = prices.usage(int(usage.get("input_tokens") or 0) - cached, 0, cached, usage.get("output_tokens"))
        # Codex reports the turn's input, not each call's: an upper bound on the context.
        return [("done", {"result": None, "subtype": "success", "tokens": tokens, "cost": None, "usage": parts,
                          "context": int(usage.get("input_tokens") or 0)})]
    elif t == "error":
        return [("error", str(obj.get("message") or "Codex failed"))]
    elif t == "turn.failed":
        return [("error", str((obj.get("error") or {}).get("message") or "Codex failed"))]
    return []


class CodexEngine(Engine):
    id = ENGINE_ID
    label = "Codex"
    kind = "cli"
    shared_safe = False
    full_disk = True
    supports = frozenset()

    def __init__(self, codex_bin="codex"):
        self.codex_bin = codex_bin

    def resolve_model(self, model):
        return None if model in ("haiku", "sonnet", None) else model  # the login's own default

    def is_fresh(self, session):
        return not (session.engine_state.get(self.id) or {}).get("session")

    def build_cmd(self, session, policy, model, tokens):
        state = session.engine_state.get(self.id) or {}
        cmd = [self.codex_bin, "exec"]
        if state.get("session"):
            cmd += ["resume", state["session"]]
        cmd += ["--json", "--ignore-user-config", "--ignore-rules", "--skip-git-repo-check",
                "-c", 'sandbox_mode="read-only"', "-c", 'approval_policy="never"', "-c", 'web_search="disabled"',
                "-c", f"developer_instructions={_toml(session.rail)}"]
        for off in _OFF:
            cmd += ["-c", off]
        if tokens.os_token and tokens.os_port:
            url = f"http://127.0.0.1:{tokens.os_port}{OS_MCP_PATH}"
            cmd += ["-c", f"mcp_servers.os.url={_toml(url)}",
                    "-c", f"mcp_servers.os.bearer_token_env_var={_toml(OS_TOKEN_ENV)}",
                    "-c", 'mcp_servers.os.default_tools_approval_mode="approve"']
        if model:
            cmd += ["-m", model]
        return cmd

    def turn_env(self, policy, tokens):
        drop = (APPS_TOKEN_ENV, APPS_USED_ENV, OS_TOKEN_ENV, ENGINE_ENV, RUNNER_TOKEN_ENV)
        return {**{k: v for k, v in os.environ.items() if k not in drop},
                "LEOBOT_WORKSPACE": policy.workspace or "hq",
                **({OS_TOKEN_ENV: tokens.os_token} if tokens.os_token else {})}

    def turn(self, session, text, policy, model, tokens):
        self.check(policy)
        had_session = bool((session.engine_state.get(self.id) or {}).get("session"))
        session.engine_state.setdefault(self.id, {})
        try:
            proc = subprocess.Popen(self.build_cmd(session, policy, self.resolve_model(model), tokens),
                                    cwd=session.repo_dir, env=self.turn_env(policy, tokens),
                                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    text=True, bufsize=1)
        except FileNotFoundError:
            yield ("error", "Codex is not installed on this machine.")
            return
        session._proc = proc
        stderr = drain(proc.stderr)
        try:
            proc.stdin.write(text + "\n")
            proc.stdin.close()
        except BrokenPipeError:  # it died at once: its own error, read below, is what to show
            pass
        errors = []
        try:
            for line in proc.stdout:
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                if obj.get("type") == "thread.started" and obj.get("thread_id"):
                    session.engine_state[self.id] = {"session": obj["thread_id"]}
                for ev in parse_event(obj):
                    if ev[0] == "error":
                        errors.append(ev[1])
                    else:
                        yield ev
            proc.wait()
            err = stderr()[:500]
            if proc.returncode not in (0, None) or errors:
                detail = errors[-1] if errors else err
                if had_session and "no rollout found" in f"{detail} {err}".lower():
                    session.engine_state[self.id] = {}
                    raise SessionLost(detail)
                yield ("error", detail or "Codex failed. Check `codex login status`.")
        finally:
            stop_child(proc)
            try:
                proc.stdout.close()
            except OSError:
                pass
            session._proc = None

    def status(self):
        path = shutil.which(self.codex_bin)
        row = {"id": self.id, "label": self.label, "kind": self.kind, "installed": bool(path), "ok": False,
               "full_disk": True, "detail": "not installed"}
        if not path:
            return row
        try:
            out = subprocess.run([path, "login", "status"], capture_output=True, text=True, timeout=15)
            row["ok"] = out.returncode == 0
        except (OSError, subprocess.SubprocessError):
            pass
        row["detail"] = (f"logged in. {FULL_DISK_NOTE} Used only on your private threads."
                         if row["ok"] else "run: codex login")
        return row
