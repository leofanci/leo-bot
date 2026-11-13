"""chat_session.py: one saved dashboard chat's state, across engines.

The process or HTTP work lives in the engine (dashboard/engines/). This class
keeps the thread state (session id, turn count, last skill, last bot) and turns
one ask() into a TurnPolicy plus an engine turn.
"""
import os  # noqa: F401
import shutil  # noqa: F401
import subprocess  # noqa: F401
import tempfile  # noqa: F401
import uuid

from dashboard.engines.base import EngineRefused, SessionLost, TurnPolicy, TurnTokens
from dashboard.engines.claude_code import (  # noqa: F401  (the server and osctl read these here)
    APP_ALLOWED_TOOLS, APP_TOOLS, APPS_MCP_PATH, APPS_TOKEN_ENV, APPS_USED_ENV, BASE_ALLOWED_TOOLS,
    BASE_TOOLS, CHAT_MODEL, ENGINE_ENV, ESCALATED_MODEL, OS_MCP_PATH, OS_TOKEN_ENV, SECRET_FILE_GLOBS,
    WEB_ALLOWED_TOOLS, WEB_TOOLS, ClaudeCodeEngine, apps_mcp_config, bot_tmpdir, os_mcp_config,
    forget_transcripts, private_tmpdir, sweep_stale_tmpdirs, tmp_name,
    parse_event, private_read_rules, remove_bot_tmpdir, user_web_domains, web_fetch_rules,
)


class ChatSession:
    def __init__(self, repo_dir, rail, claude_bin="claude", session_id=None, model=CHAT_MODEL):
        self.repo_dir = repo_dir
        self.rail = rail
        self.claude_bin = claude_bin
        self.engine = ClaudeCodeEngine(claude_bin)  # the server sets it per run
        self.engine_state = {}  # engine id -> that engine's state for this thread
        self.recap = None       # the server sets it per turn: what a lost session gets back
        self.ephemeral = False  # a consulted teammate: nothing kept after this turn
        self.model = model
        self.session_id = session_id or str(uuid.uuid4())
        self._started = False
        self._proc = None  # the in-flight `claude -p` turn, if any
        self._turn_count = 0
        self._last_skill = None  # last routed skill (server); skip re-injecting body
        self._skill_explicit = False  # True when user tagged @/skill (Sonnet gate)
        # True when the bot loaded _last_skill itself (osctl get-skill): the stronger model on the
        # next turn, never the rights of a tag (continuation, edits).
        self._skill_loaded = False
        self._pending_skill = None
        self._pending_skill_explicit = False
        self._last_bot = None  # bot that spoke last (server routes untagged turns to it)
        self._pending_bot = None
        # Workspace isolation, set by the server before each turn: osctl reads
        # LEOBOT_WORKSPACE; deny_dirs are other workspaces' folders (no Read/Grep).
        self.workspace = None
        self.deny_dirs = ()
        self.deny_files = ()  # single files, same rule (stores with every workspace)
        self.readonly = False  # a consulted teammate: osctl refuses every write
        # Set by the server on a scheduled job's turn: osctl runs that job's mode.
        self.job_id = None
        self.job_mode = None
        self.code_folder = None  # the server sets it per turn: the folder osctl code-context reads
        # Once a thread has read an app, its context holds that data: web tools
        # stay off for good there (no "read mail, leak it via a search" path).
        self.apps_used = False
        # Set by the server per turn: an invited person can see this thread, so
        # the turn gets only the osctl relay (+ web as usual). See OS_MCP_PATH.
        self.shared = False
        # Every linked folder a turn of this thread was given. Its session holds what those
        # turns read, so when one is unlinked the server starts a fresh session (with a recap).
        self.folder_paths = set()
        self.pending_recap = None  # set by the server on such a fresh start; used once

    @classmethod
    def restore(cls, repo_dir, rail, rec, **kw):
        """Rebuild a saved chat (chat_store record) so the next turn --resumes it."""
        sess = cls(repo_dir=repo_dir, rail=rail, session_id=rec["id"], **kw)
        sess._started = bool(rec.get("started"))
        sess.engine_state = dict(rec.get("engines") or {})
        sess._turn_count = int(rec.get("turn_count") or 0)
        sess._last_skill = rec.get("last_skill")
        sess._skill_explicit = bool(rec.get("skill_explicit"))
        sess._skill_loaded = bool(rec.get("skill_loaded"))
        sess._last_bot = rec.get("last_bot")
        sess.apps_used = bool(rec.get("apps_used"))
        sess.folder_paths = {p for p in rec.get("folder_paths") or () if isinstance(p, str)}
        return sess

    def is_fresh(self):
        """True before the first turn of this session id (no --resume yet)."""
        return self.engine.is_fresh(self)

    def session_meta(self):
        return {
            "session_id": self.session_id,
            "turn_count": self._turn_count,
            "fresh": self.is_fresh(),
        }

    def _policy(self, with_web=False, read_dirs=(), edit_dirs=(), web_domains=(), apps=False):
        read_dirs = tuple(str(d) for d in read_dirs or ())
        edit_dirs = tuple(str(d) for d in edit_dirs or () if str(d) in read_dirs)
        return TurnPolicy(shared=self.shared, readonly=self.readonly, web=bool(with_web),
                          web_domains=tuple(web_domains or ()), apps=apps,
                          workspace=self.workspace or "hq", read_dirs=read_dirs, edit_dirs=edit_dirs,
                          deny_dirs=tuple(self.deny_dirs),
                          deny_files=tuple(self.deny_files), apps_used=bool(self.apps_used),
                          chat=self.session_id, job=self.job_id, job_mode=self.job_mode,
                          code_folder=getattr(self, "code_folder", None))

    def _base_cmd(self, with_web=False, model=None, read_dirs=(), edit_dirs=(),
                  web_domains=(), apps_port=None, os_port=None):
        """The `claude -p` argv for one turn (ClaudeCodeEngine.build_cmd)."""
        policy = self._policy(with_web, read_dirs, edit_dirs, web_domains)
        return ClaudeCodeEngine(self.claude_bin).build_cmd(self, policy, model, apps_port, os_port)

    def ask(self, text, with_web=False, model=None, read_dirs=(), edit_dirs=(),
            web_domains=(), apps_token=None, apps_port=None, os_token=None, os_port=None):
        """Run one turn on self.engine; yield (kind, payload) events.
        `with_web` adds web tools for this turn; `read_dirs` are the linked folders
        it may read and search, `edit_dirs` (a subset) the ones it may change; `model` overrides
        the default for this turn (the server tiers per turn: see
        server._needs_web / _pick_model). Any routed skill is already injected
        into `text` by the server. Server injects full snapshot + skill body on
        fresh session start only (no turn cap: a session never auto-resets)."""
        policy = self._policy(with_web, read_dirs, edit_dirs, web_domains, apps=bool(apps_token))
        if not policy.shared:
            self.folder_paths |= set(policy.read_dirs)
        tokens = TurnTokens(apps_token, apps_port, os_token, os_port)
        failed = False
        try:
            for ev in self.engine.turn(self, text, policy, model, tokens):
                failed = failed or ev[0] == "error"
                yield ev
        except SessionLost:  # the chat moved machines: same turn, fresh session, recap in front
            prompt = f"{self.recap}\n\n{text}" if self.recap else text
            for ev in self.engine.turn(self, prompt, policy, model, tokens):
                failed = failed or ev[0] == "error"
                yield ev
        except EngineRefused as e:
            failed = True
            yield ("error", str(e))
        if failed:
            return
        self._turn_count += 1
        if self._pending_skill:
            self._last_skill = self._pending_skill
            self._skill_explicit = self._pending_skill_explicit
            self._skill_loaded = False
        self._pending_skill = None
        self._pending_skill_explicit = False
        if self._pending_bot:
            self._last_bot = self._pending_bot
        self._pending_bot = None

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
