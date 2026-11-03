"""chat_session.py — one Claude Code session per saved dashboard chat.

Runs `claude` in stream-json mode. Bash is restricted to the osctl CLI, so the
agent can ONLY mutate GTM state via `python -m dashboard.osctl`; Read is always
available for myOS. Glob/Grep and linked-folder access are loaded only when a
turn explicitly needs file-search fallback. TokenSave retrieval happens once in
the server before the model starts, so the agent cannot skip or repeat it. No
MCP, no default system prompt — see _base_cmd for the flags. Parses stream-json
events into simple (kind, payload) tuples for the SSE layer.

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
    elif t == "result":
        usage = obj.get("usage") or {}
        tokens = sum(int(usage.get(k) or 0) for k in (
            "input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens"))
        return ("done", {"result": obj.get("result"), "subtype": obj.get("subtype"),
                         "tokens": tokens, "cost": obj.get("total_cost_usd")})
    return (None, None)


# Hostnames typed by the user. Bare names need a real-looking TLD so file names
# like notes.md / app.py are not mistaken for hosts.
_URL_HOST_RE = re.compile(r"https?://([a-z0-9.-]+\.[a-z]{2,})", re.I)
_BARE_HOST_RE = re.compile(r"(?<![\w@/.-])((?:[a-z0-9-]+\.)+[a-z]{2,})(?![\w.-])", re.I)
_FILE_EXTS = frozenset({
    "md", "py", "js", "ts", "json", "txt", "csv", "html", "css", "sh", "yml",
    "yaml", "toml", "lock", "log", "sql", "png", "jpg", "jpeg", "gif", "pdf",
})


def user_web_domains(text):
    """Hosts the user explicitly named in their message (lowercased, www. stripped)."""
    text = (text or "")[:20000]  # bodies may be huge; hosts are typed near the top
    hosts = {h.lower() for h in _URL_HOST_RE.findall(text)}
    for h in _BARE_HOST_RE.findall(text):
        if h.rsplit(".", 1)[-1].lower() not in _FILE_EXTS:
            hosts.add(h.lower())
    return sorted({h[4:] if h.startswith("www.") else h for h in hosts})


def web_fetch_rules(domains):
    return [r for d in domains for r in (f"WebFetch(domain:{d})", f"WebFetch(domain:www.{d})")]


# Mutations go through osctl ONLY (Write/Edit are never loaded, so the
# authored-files-are-truth invariant holds). Permissions are assembled per turn:
# linked-folder and TokenSave permissions do not exist on ordinary chat turns.
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
# Secret-looking files stay unreadable even inside the repo or a linked app
# folder (Grep/Glob honor Read deny rules too). Verified against claude 2.x:
# "**/x" covers the repo; a linked --add-dir needs its own "//abs/**/x" rule.
SECRET_FILE_GLOBS = [
    ".env", ".env.*", "*.pem", "*.key", ".npmrc", ".netrc",
    "credentials*.json", "secrets.*", "id_rsa*", "id_ed25519*",
    ".auth-token",  # dashboard login token (server.TOKEN_FILE)
    "integrations.json",  # Composio key + sessions (core.integrations)
    "people.json", "invites.json", "access.json",  # invited people (core.people)
]


def private_read_rules(repo_dir):
    """Read denies for every turn: Claude's config keeps each bot session's
    transcript (all workspaces), and server.log lists their ids."""
    return [f"Read(/{Path.home() / '.claude'}/**)",
            f"Read(/{Path(repo_dir).resolve() / 'dashboard' / 'server.log'})"]
# Tools loaded EVERY turn. Skills are NOT loaded via the Skill tool — that costs
# ~4k tok of discovery (35 descriptions, incl. useless built-ins) just to let the
# model pick one. Instead the server routes to the ONE relevant skill and injects
# its SKILL.md into the prompt (see server._route_skill) — no discovery overhead.
BASE_TOOLS = ["Bash", "Read"]
# App-code discovery is progressive: these schemas are present only for a code
# question whose active GTM project has a linked folder. Neither tool modifies files.
APP_TOOLS = ["Glob", "Grep"]
# WebSearch/WebFetch are added only on turns whose request needs live research
# (server._needs_web) — their schemas are small but off by default per the
# token-lean policy.
WEB_TOOLS = ["WebSearch", "WebFetch"]
# Connected apps: on an app turn only, one HTTP MCP server pointing back at the
# dashboard relay (server.APPS_RELAY_PATH). The turn token rides in the child's
# env, never in argv (`ps` shows argv to every local user).
APPS_MCP_PATH = "/api/internal/apps/mcp"
APPS_TOKEN_ENV = "MYOS_APPS_TOKEN"
# A shared thread (an invited person can see it): no Bash/Read/Glob/Grep at all,
# because Claude Code runs read-only tools and shell reads (cat, `< file`) on any
# path without asking. Its one myOS tool is the osctl relay (os_relay.py).
OS_MCP_PATH = "/api/internal/os/mcp"
OS_TOKEN_ENV = "MYOS_OS_TOKEN"
# Set on every turn of a thread that has read an app: osctl then refuses the
# commands that send text off the machine (research-signal), like web-off does.
APPS_USED_ENV = "MYOS_APPS_USED"

# Claude lets a turn Read its project's temp folder. The default one is shared
# with every dev session in this repo (scratch files, task output), so bots get
# their own owner-only root per server run (CLAUDE_CODE_TMPDIR), removed on shutdown.
_BOT_TMPDIR = None
_BOT_TMPDIR_LOCK = threading.Lock()  # chat requests run on server threads


def bot_tmpdir():
    global _BOT_TMPDIR
    with _BOT_TMPDIR_LOCK:
        if _BOT_TMPDIR is None:
            _BOT_TMPDIR = tempfile.mkdtemp(prefix="myos-bots-")
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


def os_mcp_config(port):
    return json.dumps({"mcpServers": {"os": _mcp_server(port, OS_MCP_PATH, OS_TOKEN_ENV)}})

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


class ChatSession:
    def __init__(self, repo_dir, rail, claude_bin="claude", session_id=None, model=CHAT_MODEL):
        self.repo_dir = repo_dir
        self.rail = rail
        self.claude_bin = claude_bin
        self.model = model
        self.session_id = session_id or str(uuid.uuid4())
        self._started = False
        self._proc = None  # the in-flight `claude -p` turn, if any
        self._turn_count = 0
        self._last_skill = None  # last routed skill (server); skip re-injecting body
        self._skill_explicit = False  # True when user tagged @/skill (Sonnet gate)
        self._pending_skill = None
        self._pending_skill_explicit = False
        self._last_bot = None  # bot that spoke last (server routes untagged turns to it)
        self._pending_bot = None
        # Workspace isolation, set by the server before each turn: osctl reads
        # MYOS_WORKSPACE; deny_dirs are other workspaces' folders (no Read/Grep).
        self.workspace = None
        self.deny_dirs = ()
        self.deny_files = ()  # single files, same rule (stores with every workspace)
        self.readonly = False  # a consulted teammate: osctl refuses every write
        # Once a thread has read an app, its context holds that data: web tools
        # stay off for good there (no "read mail, leak it via a search" path).
        self.apps_used = False
        # Set by the server per turn: an invited person can see this thread, so
        # the turn gets only the osctl relay (+ web as usual). See OS_MCP_PATH.
        self.shared = False

    @classmethod
    def restore(cls, repo_dir, rail, rec, **kw):
        """Rebuild a saved chat (chat_store record) so the next turn --resumes it."""
        sess = cls(repo_dir=repo_dir, rail=rail, session_id=rec["id"], **kw)
        sess._started = bool(rec.get("started"))
        sess._turn_count = int(rec.get("turn_count") or 0)
        sess._last_skill = rec.get("last_skill")
        sess._skill_explicit = bool(rec.get("skill_explicit"))
        sess._last_bot = rec.get("last_bot")
        sess.apps_used = bool(rec.get("apps_used"))
        return sess

    def is_fresh(self):
        """True before the first turn of this session id (no --resume yet)."""
        return not self._started

    def session_meta(self):
        return {
            "session_id": self.session_id,
            "turn_count": self._turn_count,
            "fresh": self.is_fresh(),
        }

    def _base_cmd(self, with_web=False, model=None, workspace_dir=None,
                  file_search_mode=False, web_domains=(), apps_port=None, os_port=None):
        # Lean per-turn flags. Skills are NEVER discovered here (the server injects
        # the routed skill's body into the prompt instead), so we always pass
        # --disable-slash-commands + --setting-sources "" — zero skill-discovery
        # tokens, no leaked built-in commands.
        #   --system-prompt        REPLACES Claude Code's default prompt with RAIL.
        #   --tools Bash Read [Glob Grep] [WebSearch WebFetch]  app discovery is
        #                          loaded only for a linked workspace; mutations
        #                          stay osctl-only. Web tools are likewise opt-in.
        #   --add-dir             grants file-tool access to that one linked app
        #                          folder for the current turn; no eager read.
        #   --strict-mcp-config    no --mcp-config given ⇒ zero MCP servers/tools.
        #   --model                per-turn (server tiers haiku→sonnet).
        # Changing model/tools per turn is safe across --resume: the session is
        # restored from disk by id and context is preserved (verified). Do NOT use
        # --bare — it changes auth behavior.
        if self.shared:  # no file, shell or app tools: the osctl relay only
            workspace_dir = file_search_mode = apps_port = None
        tools = ((WEB_TOOLS if with_web else []) if self.shared else
                 (list(BASE_TOOLS)
                  + (APP_TOOLS if workspace_dir and file_search_mode else [])
                  + (WEB_TOOLS if with_web else [])))
        allowed_tools = (([] if self.shared else list(BASE_ALLOWED_TOOLS))
                         + (APP_ALLOWED_TOOLS
                            if workspace_dir and file_search_mode else [])
                         + (WEB_ALLOWED_TOOLS if with_web else [])
                         + (web_fetch_rules(web_domains) if with_web else [])
                         + (["mcp__apps"] if apps_port else [])
                         + (["mcp__os"] if self.shared and os_port else []))
        mcp = (apps_mcp_config(apps_port) if apps_port
               else os_mcp_config(os_port) if self.shared and os_port else None)
        denied = [f"Read(**/{g})" for g in SECRET_FILE_GLOBS]
        denied += private_read_rules(self.repo_dir)
        if workspace_dir and file_search_mode:
            denied += [f"Read(/{workspace_dir}/**/{g})" for g in SECRET_FILE_GLOBS]
        denied += [f"Read(/{d}/**)" for d in self.deny_dirs]
        denied += [f"Read(/{f})" for f in self.deny_files]
        cmd = [self.claude_bin, "-p",
               "--output-format", "stream-json",
               "--include-partial-messages",
               "--verbose",
               "--model", model or self.model,
               "--system-prompt", self.rail,
               "--tools", *(tools or [""]),
               "--allowedTools", *(allowed_tools or [""]),
               "--disallowedTools", *denied,
               "--strict-mcp-config",
               *(["--mcp-config", mcp] if mcp else []),
               "--disable-slash-commands",
               "--setting-sources", "",
               "--restricted",
               "--permission-mode", "default"]
        if workspace_dir and file_search_mode:
            cmd += ["--add-dir", str(workspace_dir)]
        if self._started:
            cmd += ["--resume", self.session_id]
        else:
            cmd += ["--session-id", self.session_id]
        return cmd

    def ask(self, text, with_web=False, model=None, workspace_dir=None,
            file_search_mode=False, web_domains=(), apps_token=None, apps_port=None,
            os_token=None, os_port=None):
        """Run one turn; yield (kind, payload) events. Each turn is its own
        `claude -p` invocation, resumed by session id so context persists.
        `with_web` adds the WebSearch/WebFetch tools for this turn;
        `workspace_dir` plus `file_search_mode` grants read-only access to one
        linked app and adds Glob/Grep only when fallback discovery is warranted;
        `model` overrides the default for this turn (the server tiers per turn —
        see server._needs_web / _pick_model). Any routed skill is already
        injected into `text` by the server. Server injects full snapshot + skill
        body on fresh session start only (no turn cap — session never auto-resets)."""
        proc = subprocess.Popen(
            self._base_cmd(with_web, model, workspace_dir, file_search_mode,
                           web_domains, apps_port=apps_port if apps_token else None,
                           os_port=os_port if os_token else None),
            cwd=self.repo_dir,
            env={**{k: v for k, v in os.environ.items()
                    if k not in (APPS_TOKEN_ENV, APPS_USED_ENV, OS_TOKEN_ENV)},
                 "MYOS_WORKSPACE": self.workspace or "hq",
                 "CLAUDE_CODE_TMPDIR": bot_tmpdir(),
                 **({"MYOS_READONLY": "1"} if self.readonly else {}),
                 **({APPS_USED_ENV: "1"} if self.apps_used else {}),
                 **({APPS_TOKEN_ENV: apps_token} if apps_token else {}),
                 **({OS_TOKEN_ENV: os_token} if os_token else {})},
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1)
        self._started = True
        self._proc = proc
        proc.stdin.write(text + "\n")
        proc.stdin.close()
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
                if kind:
                    yield (kind, payload)
            proc.wait()
            if proc.returncode not in (0, None):
                yield ("error", (proc.stderr.read() or "")[:500])
            else:
                self._turn_count += 1
                if self._pending_skill:
                    self._last_skill = self._pending_skill
                    self._skill_explicit = self._pending_skill_explicit
                self._pending_skill = None
                self._pending_skill_explicit = False
                if self._pending_bot:
                    self._last_bot = self._pending_bot
                self._pending_bot = None
        finally:
            for stream in (proc.stdout, proc.stderr):
                try:
                    stream.close()
                except OSError:
                    pass
            self._proc = None

    def note_skill(self, skill, explicit=False):
        """Record routed skill for this turn (applied after successful completion)."""
        self._pending_skill = skill
        self._pending_skill_explicit = bool(explicit and skill)

    def note_bot(self, bot_id):
        """Record the speaking bot for this turn (applied after successful completion)."""
        self._pending_bot = bot_id

    def close(self):
        """Terminate any in-flight turn. Safe to call when idle (no-op)."""
        proc = self._proc
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
            except OSError:
                pass
        self._proc = None
