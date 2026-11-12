"""claude_code.py: the claude-code engine. Runs `claude -p` in stream-json mode.

Bash is restricted to the osctl CLI, so the agent can ONLY mutate GTM state via
`python -m dashboard.osctl`; Read is always available for Leo Bot. Glob/Grep and
linked-folder access are loaded only when a turn explicitly needs file-search
fallback. TokenSave retrieval happens once in the server before the model
starts, so the agent cannot skip or repeat it. No MCP, no default system prompt:
see build_cmd for the flags. Parses stream-json events into (kind, payload)
tuples for the SSE layer.

The event field paths below were confirmed against claude 2.1.179
(`--output-format stream-json --include-partial-messages --verbose`): text
deltas arrive as stream_event/content_block_delta/text_delta, tool starts as
stream_event/content_block_start/tool_use, and the turn ends with a top-level
`result` line. A plain-text stdin turn (no positional prompt) is accepted.
"""
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import uuid
from pathlib import Path

from core import folders, paths
from dashboard.engines import prices
from dashboard.engines.base import Engine, SessionLost, drain, stop_child


def parse_event(obj):
    """Map one decoded stream-json line to (kind, payload). See module docstring."""
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
        elif et == "message_start":  # one model call: its whole prompt, for compaction
            usage = (ev.get("message") or {}).get("usage") or {}
            return ("context", sum(int(usage.get(k) or 0) for k in (
                "input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")))
    elif t == "result":
        usage = obj.get("usage") or {}
        tokens = sum(int(usage.get(k) or 0) for k in (
            "input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens"))
        parts = prices.usage(usage.get("input_tokens"), usage.get("cache_creation_input_tokens"),
                             usage.get("cache_read_input_tokens"), usage.get("output_tokens"))
        return ("done", {"result": obj.get("result"), "subtype": obj.get("subtype"),
                         "tokens": tokens, "cost": obj.get("total_cost_usd"), "usage": parts})
    return (None, None)


# Hostnames typed by the user. Bare names need a real-looking TLD so file names
# like notes.md / app.py are not mistaken for hosts.
_URL_HOST_RE = re.compile(r"https?://([a-z0-9.-]+\.[a-z]{2,})", re.I)
_HOST_RUN_RE = re.compile(r"[a-z0-9.-]+", re.I)  # linear scan; each run is then checked whole
_BARE_HOST_RE = re.compile(r"(?:[a-z0-9-]+\.)+[a-z]{2,}", re.I)
_HOST_MAX = 253
_FILE_EXTS = frozenset({
    "md", "py", "js", "ts", "json", "txt", "csv", "html", "css", "sh", "yml",
    "yaml", "toml", "lock", "log", "sql", "png", "jpg", "jpeg", "gif", "pdf",
})


def user_web_domains(text):
    """Hosts the user explicitly named in their message (lowercased, www. stripped)."""
    text = (text or "")[:20000]  # bodies may be huge; hosts are typed near the top
    hosts = {h.lower() for h in _URL_HOST_RE.findall(text)}
    for m in _HOST_RUN_RE.finditer(text):
        h = m.group()
        before, after = text[m.start() - 1:m.start()], text[m.end():m.end() + 1]
        if (len(h) > _HOST_MAX or not _BARE_HOST_RE.fullmatch(h)
                or before in ("@", "/", "_") or after == "_"):
            continue
        if h.rsplit(".", 1)[-1].lower() not in _FILE_EXTS:
            hosts.add(h.lower())
    return sorted({h[4:] if h.startswith("www.") else h for h in hosts})


def web_fetch_rules(domains):
    return [r for d in domains for r in (f"WebFetch(domain:{d})", f"WebFetch(domain:www.{d})")]


# Leo Bot data changes go through osctl ONLY: Write/Edit load only for edit folders and
# are allowed only inside them, so the authored-files-are-truth invariant holds.
# Permissions are assembled per turn: linked-folder permissions exist only on turns with folders.
# Passed as separate --allowedTools args — do not join with spaces.
BASE_ALLOWED_TOOLS = [
    "Bash(python3 -m dashboard.osctl:*)",
    "Bash(python -m dashboard.osctl:*)",
    "Read",
]
APP_ALLOWED_TOOLS = [
    "Glob",
    "Grep",
]
# WebFetch is never granted broadly: a fetched page (or a linked app file) can
# carry injected instructions telling the agent to fetch attacker.com/?d=<data>.
# Only hosts the user typed in the message are fetchable (web_fetch_rules);
# WebSearch stays open. In -p mode any other fetch is denied, not prompted.
WEB_ALLOWED_TOOLS = [
    "WebSearch",
]
# Secret-looking files stay unreadable even inside the repo or a linked folder.
# The list lives in core.folders (also the never-write list); re-exported here.
SECRET_FILE_GLOBS = folders.SECRET_FILE_GLOBS


# Keys and CLI tokens in the home folder: a bare Read must not reach them.
HOME_PRIVATE = (".codex", ".ssh", ".aws", ".gnupg", ".kube", ".docker", ".config")


def private_read_rules(repo_dir):
    """Read denies for every turn: Claude's config keeps each bot session's
    transcript (all workspaces), and server.log lists their ids."""
    home = Path.home()
    private = [home / ".claude", *(home / n for n in HOME_PRIVATE)]
    cfg = os.environ.get("CLAUDE_CONFIG_DIR")
    if cfg:
        private.append(Path(cfg).expanduser())
    # Every home folder entry but app and data (token and pid, logs, backups, settings, data set
    # aside by a restore); the old in-checkout log name stays denied for clones that write there.
    home_dirs, home_files = paths.home_private()
    spellings = lambda d: dict.fromkeys((str(d), str(Path(d).resolve())))  # noqa: E731  (macOS /var)
    return [*(f"Read(/{d}/**)" for d in private),
            *(f"Read(/{s}/**)" for d in home_dirs for s in spellings(d)),
            *(f"Read(/{s})" for f in home_files for s in spellings(f)),
            f"Read(/{Path(repo_dir).resolve() / 'dashboard' / 'server.log'})"]
# Tools loaded EVERY turn. Skills are NOT loaded via the Skill tool — that costs
# ~4k tok of discovery (35 descriptions, incl. useless built-ins) just to let the
# model pick one. Instead the server routes to the ONE relevant skill and injects
# its SKILL.md into the prompt (see server._route_skill) — no discovery overhead.
BASE_TOOLS = ["Bash", "Read"]
# Search tools load on any turn with linked folders. Neither modifies files.
APP_TOOLS = ["Glob", "Grep"]
# File changes load only on a turn with edit folders, and are allowed only inside them.
EDIT_TOOLS = ["Edit", "Write"]


def never_write_rules(folder):
    """Edit and Write denies for the never-write list inside one edit folder (core.folders):
    git, hook-manager and editor config that could run code later, a file named like one of
    those directories (a `.git` gitfile), every secret-looking file, and the repo's own
    core.hooksPath when it sits inside the folder."""
    names = [*folders.NEVER_WRITE_DIRS, *folders.NEVER_WRITE_FILES, *SECRET_FILE_GLOBS]
    return ([f"{t}(/{folder}/**/{d}/**)" for d in folders.NEVER_WRITE_DIRS for t in EDIT_TOOLS]
            + [f"{t}(/{folder}/**/{n})" for n in names for t in EDIT_TOOLS]
            + [f"{t}(/{h}/**)" for h in folders.hook_dirs(folder) if not folders.has_glob_chars(h)
               for t in EDIT_TOOLS])
# WebSearch/WebFetch are added only on turns whose request needs live research
# (server._needs_web) — their schemas are small but off by default per the
# token-lean policy.
WEB_TOOLS = ["WebSearch", "WebFetch"]
# Connected apps: on an app turn only, one HTTP MCP server pointing back at the
# dashboard relay (server.APPS_RELAY_PATH). The turn token rides in the child's
# env, never in argv (`ps` shows argv to every local user).
APPS_MCP_PATH = "/api/internal/apps/mcp"
APPS_TOKEN_ENV = "LEOBOT_APPS_TOKEN"
# A shared thread (an invited person can see it): no Bash/Read/Glob/Grep at all,
# because Claude Code runs read-only tools and shell reads (cat, `< file`) on any
# path without asking. Its one Leo Bot tool is the osctl relay (os_relay.py).
OS_MCP_PATH = "/api/internal/os/mcp"
OS_TOKEN_ENV = "LEOBOT_OS_TOKEN"
RUNNER_TOKEN_ENV = "LEOBOT_RUNNER_TOKEN"
# Set on every turn of a thread that has read an app: osctl then refuses the
# commands that send text off the machine (research-signal), like web-off does.
APPS_USED_ENV = "LEOBOT_APPS_USED"
# Which engine this turn runs on: `generate-*` jobs started inside it follow it.
ENGINE_ENV = "LEOBOT_ENGINE"

# Claude lets a turn Read its project's temp folder. The default one is shared
# with every dev session in this repo (scratch files, task output), so bots get
# their own owner-only root per server run (CLAUDE_CODE_TMPDIR), removed on shutdown.
_BOT_TMPDIR = None
_BOT_TMPDIR_LOCK = threading.Lock()  # chat requests run on server threads


def bot_tmpdir():
    global _BOT_TMPDIR
    with _BOT_TMPDIR_LOCK:
        if _BOT_TMPDIR is None:
            _BOT_TMPDIR = tempfile.mkdtemp(prefix="leobot-bots-")
        return _BOT_TMPDIR


def remove_bot_tmpdir():
    global _BOT_TMPDIR
    with _BOT_TMPDIR_LOCK:
        if _BOT_TMPDIR:
            shutil.rmtree(_BOT_TMPDIR, ignore_errors=True)
            _BOT_TMPDIR = None


def _mcp_server(port, path, env):
    return {"type": "http", "url": f"http://127.0.0.1:{port}{path}",
            "headers": {"Authorization": "Bearer ${" + env + "}"}}


def apps_mcp_config(port):
    return json.dumps({"mcpServers": {"apps": _mcp_server(port, APPS_MCP_PATH, APPS_TOKEN_ENV)}})


def os_mcp_config(port, url=None):
    server = _mcp_server(port, OS_MCP_PATH, OS_TOKEN_ENV)
    if url:  # a runner: the relay is on the server, and it also asks who this runner is
        server["url"] = url
        server["headers"]["X-Runner-Token"] = "${" + RUNNER_TOKEN_ENV + "}"
    return json.dumps({"mcpServers": {"os": server}})

# No turn cap: a chat persists via --resume until the user deletes it.
# Full state snapshot + skill body inject on session start only (server); resume
# turns rely on Claude session history + osctl on demand.

# Default model for ordinary turns. The server escalates to a stronger model
# (ESCALATED_MODEL) on skill/web/strategic turns; mechanical turns (reads,
# mutations, chit-chat) stay on the cheap one — this is what actually keeps
# rate-limit-window consumption down, since a cheaper model costs less quota
# per token than caching can reliably claw back. See server._pick_model.
CHAT_MODEL = "haiku"
ESCALATED_MODEL = "sonnet"


ENGINE_ID = "claude-code"
_LOST_RE = re.compile(r"no conversation found", re.I)
_UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def _failure_text(text):
    """What a failed `claude -p` said, or what to do when it said nothing useful."""
    text = str(text or "").strip()[:500]
    if not text or re.search(r"not logged in|please run /login|invalid api key", text, re.I):
        return ("Claude Code is not logged in on this machine. Run `claude auth login` here, "
                "or add an API key in Settings, Engines and pick it in Settings, Bots.")
    return text


class ClaudeCodeEngine(Engine):
    id = ENGINE_ID
    label = "Claude Code"
    kind = "cli"
    shared_safe = True  # verified live: see tests/test_chat_session.py and PR #22
    supports = frozenset({"web", "apps", "folder_edit", "complete"})

    def __init__(self, claude_bin="claude"):
        self.claude_bin = claude_bin

    def is_fresh(self, session):
        return not session._started

    def session_of(self, session):
        """Claude Code's own session id for this thread: the chat id, until a compaction gave
        it a new one. Only a UUID ever reaches the command line."""
        sid = (session.engine_state.get(self.id) or {}).get("session")
        return sid if isinstance(sid, str) and _UUID_RE.fullmatch(sid) else session.session_id

    def compact(self, session):
        session._started = False
        session.engine_state[self.id] = {"session": str(uuid.uuid4())}
        return True

    def build_cmd(self, session, policy, model, apps_port=None, os_port=None, os_url=None):
        # Lean per-turn flags. Skills are NEVER discovered here (the server injects
        # the routed skill's body into the prompt instead), so we always pass
        # --disable-slash-commands + --setting-sources "": zero skill-discovery
        # tokens, no leaked built-in commands.
        #   --system-prompt        REPLACES Claude Code's default prompt with RAIL.
        #   --tools Bash Read [Glob Grep] [Edit Write] [WebSearch WebFetch]  search
        #                          tools load only with linked folders, Edit/Write
        #                          only with edit folders; Leo Bot data stays osctl-only.
        #   --add-dir             one per linked folder this turn may read.
        #   Edit(//dir/**)        allowed per edit folder; the never-write list inside
        #                          it, and every deny dir, are denied (deny wins).
        #   --strict-mcp-config    no --mcp-config given => zero MCP servers/tools.
        #   --model                per-turn (server tiers haiku->sonnet).
        # Changing model/tools per turn is safe across --resume: the session is
        # restored from disk by id and context is preserved (verified). Do NOT use
        # --bare: it changes auth behavior.
        shared, with_web = policy.shared, policy.web
        web_domains = policy.web_domains
        # A rule is a glob: a path holding glob characters could not be matched by its own
        # deny rules, so it never reaches one (core.folders refuses to link it, too).
        read_dirs = tuple(d for d in policy.read_dirs if not folders.has_glob_chars(d))
        edit_dirs = tuple(d for d in policy.edit_dirs if d in read_dirs)
        if shared:  # no file, shell or app tools: the osctl relay only
            read_dirs = edit_dirs = ()
            apps_port = None
        tools = ((WEB_TOOLS if with_web else []) if shared else
                 (list(BASE_TOOLS)
                  + (APP_TOOLS if read_dirs else [])
                  + (EDIT_TOOLS if edit_dirs else [])
                  + (WEB_TOOLS if with_web else [])))
        allowed_tools = (([] if shared else list(BASE_ALLOWED_TOOLS))
                         + (APP_ALLOWED_TOOLS if read_dirs else [])
                         + [f"{t}(/{d}/**)" for d in edit_dirs for t in EDIT_TOOLS]
                         + (WEB_ALLOWED_TOOLS if with_web else [])
                         + (web_fetch_rules(web_domains) if with_web else [])
                         + (["mcp__apps"] if apps_port else [])
                         + (["mcp__os"] if shared and (os_port or os_url) else []))
        mcp = (apps_mcp_config(apps_port) if apps_port
               else os_mcp_config(os_port, os_url) if shared and (os_port or os_url) else None)
        denied = [f"Read(**/{g})" for g in SECRET_FILE_GLOBS]
        denied += private_read_rules(session.repo_dir)
        for d in read_dirs:
            denied += [f"Read(/{d}/**/{g})" for g in SECRET_FILE_GLOBS]
        for d in edit_dirs:
            denied += never_write_rules(d)
        # A Read folder nested inside an Edit folder stays read-only.
        for d in read_dirs:
            if d not in edit_dirs and any(Path(e) in Path(d).parents for e in edit_dirs):
                denied += [f"{t}(/{d}/**)" for t in EDIT_TOOLS]
        denied += [f"{t}(/{d}/**)" for d in policy.deny_dirs for t in ("Read", *EDIT_TOOLS)]
        denied += [f"Read(/{f})" for f in policy.deny_files]
        cmd = [self.claude_bin, "-p",
               "--output-format", "stream-json",
               "--include-partial-messages",
               "--verbose",
               "--model", model or session.model,
               "--system-prompt", session.rail,
               "--tools", *(tools or [""]),
               "--allowedTools", *(allowed_tools or [""]),
               "--disallowedTools", *denied,
               "--strict-mcp-config",
               *(["--mcp-config", mcp] if mcp else []),
               "--disable-slash-commands",
               "--setting-sources", "",
               "--restricted",
               "--permission-mode", "default"]
        for d in read_dirs:
            cmd += ["--add-dir", str(d)]
        sid = self.session_of(session)
        cmd += ["--resume", sid] if session._started else ["--session-id", sid]
        return cmd

    def turn_env(self, policy, tokens):
        drop = (APPS_TOKEN_ENV, APPS_USED_ENV, OS_TOKEN_ENV, ENGINE_ENV, RUNNER_TOKEN_ENV)
        return {**{k: v for k, v in os.environ.items() if k not in drop},
                "LEOBOT_WORKSPACE": policy.workspace or "hq",
                "CLAUDE_CODE_TMPDIR": bot_tmpdir(),
                ENGINE_ENV: self.id,
                **({"LEOBOT_READONLY": "1"} if policy.readonly else {}),
                **({APPS_USED_ENV: "1"} if policy.apps_used else {}),
                **({APPS_TOKEN_ENV: tokens.apps_token} if tokens.apps_token else {}),
                **({OS_TOKEN_ENV: tokens.os_token} if tokens.os_token else {}),
                **({RUNNER_TOKEN_ENV: tokens.runner_token} if tokens.runner_token else {})}

    def turn(self, session, text, policy, model, tokens):
        """One `claude -p` invocation, resumed by session id so context persists."""
        was_started = session._started
        try:
            proc = subprocess.Popen(
                self.build_cmd(session, policy, model,
                               apps_port=tokens.apps_port if tokens.apps_token else None,
                               os_port=tokens.os_port if tokens.os_token else None,
                               os_url=tokens.os_url if tokens.os_token else None),
                cwd=session.repo_dir,
                env=self.turn_env(policy, tokens),
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, bufsize=1)
        except FileNotFoundError:
            yield ("error", "Claude Code is not installed on this machine. Install it, or add an API key in Settings, Engines and pick it in Settings, Bots.")
            return
        session._started = True
        session._proc = proc
        said, context = "", None
        stderr = drain(proc.stderr)
        try:
            proc.stdin.write(text + "\n")
            proc.stdin.close()
        except BrokenPipeError:  # it died at once: its own error, read below, is what to show
            pass
        try:
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                kind, payload = parse_event(obj)
                if kind == "context":  # the last call's size rides on the done event
                    context = payload
                    continue
                if kind == "done":
                    said = payload.get("result") or said
                    payload["context"] = context
                if kind:
                    yield (kind, payload)
            proc.wait()
            if proc.returncode not in (0, None):
                err = stderr()[:500]
                if was_started and _LOST_RE.search(err):
                    session._started = False
                    raise SessionLost(err)
                yield ("error", _failure_text(err or said))
        finally:
            stop_child(proc)
            try:
                proc.stdout.close()
            except OSError:
                pass
            session._proc = None

    def complete(self, prompt, stdin_text, model):
        """Pure text generation: no tools, no MCP servers, no user/project settings
        or skills. Brief/voice text is untrusted input, so it must not be able to
        steer the model into running commands, reading files or fetching URLs."""
        cmd = [self.claude_bin, "-p", prompt, "--output-format", "json",
               "--tools", "", "--strict-mcp-config", "--setting-sources", "",
               "--disable-slash-commands", "--no-session-persistence"]
        if model:
            cmd += ["--model", model]
        proc = subprocess.run(cmd, input=stdin_text, capture_output=True, text=True)
        if proc.returncode != 0:
            raise RuntimeError(f"claude exited {proc.returncode}: {proc.stderr.strip()[:500]}")
        return proc.stdout

    def complete_metered(self, prompt, stdin_text, model):
        """`complete`, read from its JSON envelope: text, model, usage and cost."""
        raw = self.complete(prompt, stdin_text, model)
        try:
            obj = json.loads(raw)
        except ValueError:
            raise RuntimeError("claude returned no JSON result") from None
        kind, done = parse_event(obj) if isinstance(obj, dict) else (None, None)
        if kind != "done":
            raise RuntimeError("claude returned no JSON result")
        used = [m for m in (obj.get("modelUsage") or {}) if isinstance(m, str)]
        return {"text": done["result"] or "", "model": used[0] if len(used) == 1 else model,
                "usage": done["usage"], "cost": done["cost"],
                "truncated": obj.get("stop_reason") == "max_tokens"}

    def status(self):
        path = shutil.which(self.claude_bin)
        row = {"id": self.id, "label": self.label, "kind": self.kind, "installed": bool(path),
               "ok": False, "detail": "not installed"}
        if not path:
            return row
        try:
            out = subprocess.run([path, "auth", "status", "--json"], capture_output=True,
                                 text=True, timeout=15)
            info = json.loads(out.stdout or "{}")
        except (OSError, subprocess.SubprocessError, ValueError):
            info = {}
        row["ok"] = bool(info.get("loggedIn"))
        row["detail"] = "logged in" if row["ok"] else "run: claude auth login"
        return row
