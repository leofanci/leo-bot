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
stream_event/content_block_start/tool_use, each model call's tokens as
stream_event/message_start (input) and message_delta (output), and the turn ends with
a top-level `result` line. A plain-text stdin turn (no positional prompt) is accepted.
"""
import ast
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

from core import folders, paths
from dashboard.engines import prices
from dashboard.engines.base import (TURN_ENV, Engine, EngineRefused, SessionLost, drain, gate_vars, stop_child,
                                    turn_vars)


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


_IN_KEYS = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")


def call_usage(obj, run, model=None):
    """Add one model call's tokens to `run` (this run's totals so far, with "cost": each call
    priced by the model it names, else `model`); True once a call's output is counted
    (message_delta), when the run's spend so far can be reported."""
    if obj.get("type") == "assistant" and obj.get("parent_tool_use_id"):
        # A sub-agent's call (the Task tool): it streams no usage events, only its messages, one
        # per content block with the same id. Verified live with 2.1.325. Counted once per id.
        msg = obj.get("message") or {}
        usage, mid = msg.get("usage") or {}, msg.get("id")
        seen = run.setdefault("sub_seen", set())
        if not usage or not mid or mid in seen:
            return False
        seen.add(mid)
        parts = {**prices.usage(*(usage.get(k) for k in _IN_KEYS), usage.get("output_tokens")),
                 "cache_write_1h": int((usage.get("cache_creation") or {}).get("ephemeral_1h_input_tokens") or 0)}
        for k in _IN_KEYS:
            run[k] = run.get(k, 0) + int(usage.get(k) or 0)
        run["output_tokens"] = run.get("output_tokens", 0) + int(usage.get("output_tokens") or 0)
        run["cost"] = run.get("cost", 0.0) + (prices.estimate(msg.get("model") or model, parts, unknown_ok=True) or 0.0)
        return True
    ev = (obj.get("event") or {}) if obj.get("type") == "stream_event" else {}
    if ev.get("type") == "message_start":
        msg = ev.get("message") or {}
        usage = msg.get("usage") or {}
        run["call"] = {"model": msg.get("model") or model, **{k: int(usage.get(k) or 0) for k in _IN_KEYS},
                       # Claude Code keeps the hour-long cache: those writes cost more.
                       "hour": int((usage.get("cache_creation") or {}).get("ephemeral_1h_input_tokens") or 0)}
        for k in _IN_KEYS:
            run[k] = run.get(k, 0) + run["call"][k]
    elif ev.get("type") == "message_delta":
        out = (ev.get("usage") or {}).get("output_tokens")
        if isinstance(out, int):
            run["output_tokens"] = run.get("output_tokens", 0) + out
            call = run.pop("call", None) or {"model": model}
            parts = {**prices.usage(*(call.get(k) for k in _IN_KEYS), out), "cache_write_1h": call.get("hour", 0)}
            cost = prices.estimate(call["model"], parts)
            run["cost"] = run.get("cost", 0.0) + (cost or 0.0)
            return True
    return False


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
# Sub-agents (parallel work) on every private turn. A sub-agent gets this turn's tools, rules and
# gate hook: verified live with 2.1.325 (its Read went through the PreToolUse hook, and a
# denied path stayed denied). Its spend is counted from its messages (call_usage).
AGENT_TOOLS = ["Task"]


def never_write_rules(folder):
    """Edit and Write denies for the never-write list inside one edit folder (core.folders):
    git, hook-manager and editor config that could run code later, a file named like one of
    those directories (a `.git` gitfile), every secret-looking file, and the repo's own
    core.hooksPath when it sits inside the folder."""
    names = [*folders.NEVER_WRITE_DIRS, *folders.NEVER_WRITE_FILES, *SECRET_FILE_GLOBS]
    return ([f"{t}(/{folder}/**/{d}/**)" for d in folders.NEVER_WRITE_DIRS for t in EDIT_TOOLS]
            + [f"{t}(/{folder}/**/{n})" for n in names for t in EDIT_TOOLS]
            + [f"{t}(/{h}/**)" for h in folders.hook_dirs(folder) for t in EDIT_TOOLS])


def _hooks_expressible(folder):
    """False when the folder's core.hooksPath holds glob characters: its deny rule could not
    match it, so the folder gets no edit access at all (it stays readable)."""
    return not any(folders.has_glob_chars(h) for h in folders.hook_dirs(folder))
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


TMP_PREFIXES = ("leobot-bots-", "leobot-uploads-", "leobot-runner-")
_PID_FILE = ".leobot-pid"


def private_tmpdir(prefix):
    """A new owner-only temp folder that names the process that made it, so a later start
    can sweep it once that process is gone (a crash or SIGKILL skips the cleanup)."""
    path = tempfile.mkdtemp(prefix=prefix)
    try:
        with open(os.path.join(path, _PID_FILE), "w") as f:
            f.write(str(os.getpid()))
    except OSError:
        pass
    return path


def _alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:  # it exists, owned by someone else
        return True
    return True


def sweep_stale_tmpdirs(base=None, now=None):
    """Remove leftover Leo Bot temp folders of processes that are gone: pasted images and bot
    task output must not outlive a crash. A folder with no pid note is swept after a day."""
    base = base or tempfile.gettempdir()
    now = now or time.time()
    removed = []
    try:
        entries = list(os.scandir(base))
    except OSError:
        return removed
    for e in entries:
        if not e.name.startswith(TMP_PREFIXES):
            continue
        try:
            if not e.is_dir(follow_symlinks=False) or e.stat(follow_symlinks=False).st_uid != os.getuid():
                continue
            try:
                with open(os.path.join(e.path, _PID_FILE)) as f:
                    stale = not _alive(int(f.read().strip() or 0))
            except (OSError, ValueError):
                stale = now - e.stat(follow_symlinks=False).st_mtime > 86400
        except OSError:
            continue
        if stale:
            shutil.rmtree(e.path, ignore_errors=True)
            removed.append(e.path)
    return removed


_SESSION_ID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def forget_transcripts(rec):
    """A deleted thread's Claude Code transcripts go too (each session id the thread used,
    in Claude's own config folder): a delete leaves no copy of the conversation behind."""
    engines = (rec or {}).get("engines") if isinstance(rec, dict) else None
    sids = {str(st.get("session")) for st in (engines or {}).values() if isinstance(st, dict) and st.get("session")}
    sids.add(str((rec or {}).get("id") or ""))  # older threads used the thread id as the session id
    sids = {sid for sid in sids if _SESSION_ID_RE.match(sid)}
    if not sids:
        return 0
    cfg = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude").expanduser()
    n = 0
    for sid in sids:
        for f in (cfg / "projects").glob(f"*/{sid}.jsonl"):
            try:
                f.unlink()
                n += 1
            except OSError:
                pass
    return n


def tmp_name(ws):
    """A workspace's subfolder name under a per-run temp root (uploads, bot task output)."""
    return re.sub(r"[^a-z0-9-]", "_", str(ws or "hq").lower()) or "hq"


def bot_tmpdir(ws=None):
    """The per-run root, or `ws`'s own subfolder of it: each workspace's turns keep their task
    output apart, and a company turn is denied every other workspace's subfolder."""
    global _BOT_TMPDIR
    with _BOT_TMPDIR_LOCK:
        if _BOT_TMPDIR is None:
            _BOT_TMPDIR = private_tmpdir("leobot-bots-")
        root = _BOT_TMPDIR
    if ws is None:
        return root
    sub = os.path.join(root, tmp_name(ws))
    os.makedirs(sub, mode=0o700, exist_ok=True)
    return sub


def remove_bot_tmpdir():
    global _BOT_TMPDIR
    with _BOT_TMPDIR_LOCK:
        if _BOT_TMPDIR:
            shutil.rmtree(_BOT_TMPDIR, ignore_errors=True)
            _BOT_TMPDIR = None


HOOK_PATH = Path(__file__).resolve().parent.parent / "turn_hook.py"
# The hook's source, read once when the server starts and run inline (`-I -c`): a bot's shell
# command that rewrites turn_hook.py changes nothing a later command asks.
def _hook_source():
    tree = ast.parse(HOOK_PATH.read_text(encoding="utf-8"))
    if tree.body and isinstance(tree.body[0], ast.Expr) and isinstance(getattr(tree.body[0], "value", None), ast.Constant):
        tree.body = tree.body[1:]  # the docstring: the command stays short
    return ast.unparse(tree)


HOOK_COMMAND = f"{shlex.quote(sys.executable)} -I -c {shlex.quote(_hook_source())}"


def gate_settings(shell=False, review=False):
    """Claude Code settings holding one PreToolUse hook on the web tools (and Bash on a Full
    access turn): it asks the server's gate (dashboard/turn_hook.py) and denies when it cannot.
    Verified live (2.1.325): a hook passed with --settings runs under --setting-sources "", and
    its deny is final."""
    # Approve for me (auto mode): file tools ask too, so a write outside the edit folders is refused
    # as on every other level instead of going to Claude Code's reviewer.
    matcher = "WebSearch|WebFetch" + ("|Bash" if shell else "") + ("|Edit|Write|MultiEdit|NotebookEdit" if review else "")
    return json.dumps({"hooks": {"PreToolUse": [{"matcher": matcher,
                                                 "hooks": [{"type": "command", "command": HOOK_COMMAND}]}]}})


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
    supports = frozenset({"web", "apps", "folder_edit", "complete", "shell", "review"})
    reports_tokens = True

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

    @staticmethod
    def shell_dir(policy, gate):
        """The edit folder a shell-level turn works in (its cwd, so a command never lands in the
        Leo Bot checkout), or None: no shell level, no gate, no edit folder it may change."""
        if not (getattr(policy, "shell", False) and gate and not policy.shared):
            return None
        read = [d for d in policy.read_dirs if not folders.has_glob_chars(d)]
        dirs = [d for d in policy.edit_dirs if d in read and _hooks_expressible(d)]
        return dirs[0] if dirs else None

    def build_cmd(self, session, policy, model, apps_port=None, os_port=None, os_url=None, gate=False):
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
        edit_dirs = tuple(d for d in policy.edit_dirs if d in read_dirs and _hooks_expressible(d))
        walls = [*policy.deny_dirs, *policy.deny_files]
        if any(folders.has_glob_chars(w) for w in walls):  # a deny rule could not match it: never run
            raise EngineRefused("Leo Bot's data folder path holds * ? [ or ] characters, so bots "
                                "could read past their walls. Move the folder to a plain path.")
        if shared:  # no file, shell or app tools: the osctl relay only
            read_dirs = edit_dirs = ()
            apps_port = None
        if with_web and apps_port and not gate:  # app data and web with nothing between them: never
            with_web = False
        # A shell level: commands checked by the gate (dashboard/shell_rules.py) as they run. Full
        # access allows every command; Approve for me leaves each one to Claude Code's own reviewer
        # (permission mode auto, verified live with 2.1.325). No gate, or no edit folder to work
        # in: the osctl-only shell of every other turn.
        shell = bool(self.shell_dir(policy, gate))
        review = shell and getattr(policy, "level", "") == "approve"
        tools = ((WEB_TOOLS if with_web else []) if shared else
                 (list(BASE_TOOLS) + AGENT_TOOLS
                  + (APP_TOOLS if read_dirs else [])
                  + (EDIT_TOOLS if edit_dirs else [])
                  + (WEB_TOOLS if with_web else [])))
        allowed_tools = (([] if shared else list(BASE_ALLOWED_TOOLS) + AGENT_TOOLS)
                         + (APP_ALLOWED_TOOLS if read_dirs else [])
                         + [f"{t}(/{d}/**)" for d in edit_dirs for t in EDIT_TOOLS]
                         + (WEB_ALLOWED_TOOLS if with_web else [])
                         + (web_fetch_rules(web_domains) if with_web else [])
                         + (["mcp__apps"] if apps_port else [])
                         + (["mcp__os"] if shared and (os_port or os_url) else [])
                         + (["Bash"] if shell and not review else []))
        mcp = (apps_mcp_config(apps_port) if apps_port
               else os_mcp_config(os_port, os_url) if shared and (os_port or os_url) else None)
        # Claude Code matches these across letter case, dot-dot and NFC/NFD spellings of the path
        # (probed live with the real binary, 2.1.322): one spelling per rule is enough.
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
        denied += [f"{t}(/{f})" for f in policy.deny_files for t in ("Read", *EDIT_TOOLS)]
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
               # The taint gate (dashboard/taint.py): a web tool asks it first. A run without one
               # (a runner on another Mac) has no apps; with apps and no gate, web is off above.
               *(["--settings", gate_settings(shell, review)] if (with_web or shell) and gate else []),
               "--restricted",
               "--permission-mode", "auto" if review else "default",
               # Nobody answers a prompt in a bot's run: whatever would ask is refused.
               *(["--permission-prompts", "none"] if review else [])]
        for d in read_dirs:
            cmd += ["--add-dir", str(d)]
        sid = self.session_of(session)
        cmd += ["--resume", sid] if session._started else ["--session-id", sid]
        return cmd

    def turn_env(self, policy, tokens, repo_dir=None):
        """`repo_dir`: a turn working in another folder still runs `python3 -m dashboard.osctl`."""
        drop = (APPS_TOKEN_ENV, APPS_USED_ENV, OS_TOKEN_ENV, ENGINE_ENV, RUNNER_TOKEN_ENV, *TURN_ENV)
        return {**{k: v for k, v in os.environ.items() if k not in drop},
                **({"PYTHONPATH": os.pathsep.join(x for x in (str(repo_dir), os.environ.get("PYTHONPATH")) if x)}
                   if repo_dir else {}),
                "LEOBOT_WORKSPACE": policy.workspace or "hq",
                "CLAUDE_CODE_TMPDIR": bot_tmpdir(policy.workspace or "hq"),
                ENGINE_ENV: self.id,
                **turn_vars(policy),
                **({APPS_USED_ENV: "1"} if policy.apps_used else {}),
                **({APPS_TOKEN_ENV: tokens.apps_token} if tokens.apps_token else {}),
                **({OS_TOKEN_ENV: tokens.os_token} if tokens.os_token else {}),
                **({RUNNER_TOKEN_ENV: tokens.runner_token} if tokens.runner_token else {}),
                **gate_vars(tokens)}

    def turn(self, session, text, policy, model, tokens):
        """One `claude -p` invocation, resumed by session id so context persists."""
        was_started = session._started
        gated = bool(gate_vars(tokens))
        try:
            proc = subprocess.Popen(
                self.build_cmd(session, policy, model,
                               apps_port=tokens.apps_port if tokens.apps_token else None,
                               os_port=tokens.os_port if tokens.os_token else None,
                               os_url=tokens.os_url if tokens.os_token else None,
                               gate=gated),
                # A shell level works in its edit folder. Claude Code keys a session to its folder:
                # a thread that moves in or out of one continues with its recap (SessionLost).
                cwd=self.shell_dir(policy, gated) or session.repo_dir,
                env=self.turn_env(policy, tokens, repo_dir=session.repo_dir if self.shell_dir(policy, gated) else None),
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, bufsize=1)
        except FileNotFoundError:
            yield ("error", "Claude Code is not installed on this machine. Install it, or add an API key in Settings, Engines and pick it in Settings, Bots.")
            return
        session._started = True
        session._proc = proc
        said, context, run_usage = "", None, {}
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
                if call_usage(obj, run_usage, model):  # a model call ended: its tools run next
                    yield ("spent", {"usage": prices.usage(*(run_usage.get(k) for k in (
                        *_IN_KEYS, "output_tokens"))), "cost": run_usage["cost"]})
                kind, payload = parse_event(obj)
                if kind == "context":  # the last call's size rides on the done event
                    context = payload
                    continue
                if kind == "done":
                    said = payload.get("result") or said
                    payload["context"] = context
                    if run_usage.get("sub_seen"):  # sub-agents ran: never report less than counted
                        payload["cost"] = max(float(payload.get("cost") or 0), run_usage.get("cost", 0.0))
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
