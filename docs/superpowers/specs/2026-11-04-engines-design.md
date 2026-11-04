# Engines: run bots on Claude Code, Codex, other CLIs or an API key

## Intent

Every bot turn in myOS runs `claude -p` (Claude Code) on the owner's own Claude
login. That ties myOS to one vendor and to one person's subscription. Three
things need more than that:

- Someone who downloads myOS may use Codex, another agent CLI, or only have an
  API key.
- A team server (myOS on a VPS) should not depend on one member's personal
  login.
- A hosted myOS, if it ever exists, can only run on API keys.

An **engine** is what runs a bot's turn. This design adds an engine layer with
four drivers, so each workspace and each bot can pick its engine. Prior art:
OpenMausBot (CLI engines on the user's own login plus an OpenAI-compatible
endpoint driven by its own agent loop and its own tools), Hermes Agent (any
model through one provider config), Buzz (`buzz-acp` drives agent CLIs over
ACP). Behavior is borrowed, no code.

## Goals

- Drivers:
  - **claude-code**: today's code, moved behind the engine interface. No
    behavior change.
  - **codex**: `codex exec --json` on the person's own ChatGPT/Codex login.
  - **acp**: any CLI that speaks ACP (Agent Client Protocol: JSON-RPC over
    stdio). Covers Gemini CLI, Grok CLI, OpenCode and future ones without one
    adapter each.
  - **api**: myOS's own agent loop over HTTP. Two wire formats: Anthropic
    Messages and OpenAI-compatible Chat Completions. OpenAI-compatible covers
    OpenAI, OpenRouter, DeepSeek, Fireworks and local servers (Ollama, LM
    Studio).
- Per-workspace default engine, per-bot override.
- One tool policy per turn, computed by the server as today, enforced by each
  driver in its own way. A driver that cannot enforce a policy refuses the
  turn; it never runs with more than the policy allows.
- Shared threads (an invited person can see them) keep the PR #22 guarantee:
  no file, shell or app tools, only osctl bound to the workspace.
- An engines screen (HQ → Settings → Engines): detects installed CLIs and
  whether each is logged in, stores API keys, picks defaults.
- Optional, off by default: on a team server, each person connects their own
  engine account and their turns run on it.
- Runners: the workspace lives on a server, and a person's own Mac runs the
  turns of the bots assigned to them, on their own login (section 10).

## Non-goals

- No hosted myOS in this round. The design only keeps it possible (API
  engine, per-customer copy).
- No moving one engine's private transcript into another engine. Each
  engine keeps its own session per thread (section 6a); what crosses between
  engines is only the thread's visible messages.
- No shell tool in the API engine in this round.
- No Hermes-style chat-app gateway (Telegram, Slack). Separate project.
- No OS-level sandbox (separate macOS user, container) for full-disk engines
  in this round (section 5a).
- No change to osctl, skills routing, RAIL, or the invites model.

## Anti-goals

- An engine that silently runs with more tools than the turn's policy.
- A shared thread running on an engine whose lockdown has not been verified
  live.
- API keys or per-person engine tokens readable by any bot turn, listed in any
  API response, or written to a tracked file.
- One person's turn billed to another person's account without it being
  visible in the thread.

## Current state (what is hard-wired to Claude Code)

- `dashboard/chat_session.py`: `ChatSession._base_cmd` builds the `claude -p`
  flags (tools, allowedTools, deny rules, MCP config, `--resume`);
  `ask()` spawns it and maps stream-json lines with `parse_event` to
  `("delta", text)`, `("tool", name)`, `("done", {...})`, `("error", text)`.
  The server consumes only those tuples.
- Model tiers: `CHAT_MODEL = "haiku"`, `ESCALATED_MODEL = "sonnet"`, picked per
  turn by `server._pick_model`.
- Sessions resume through Claude Code's own transcript (`--resume <id>`);
  `chat_store` keeps only metadata.
- `generate.py run_claude`: one-shot `claude -p --output-format json` for plan
  and brief jobs.
- `server.py` terminal: `TerminalSession(cmd=["claude"])`, local requests only.
- Lockdown pieces: `SECRET_FILE_GLOBS`, `private_read_rules` (denies
  `~/.claude`), workspace `deny_dirs`/`deny_files`, the apps relay
  (`/api/internal/apps/mcp`), the osctl relay (`/api/internal/os/mcp`,
  `os_relay.py`), `MYOS_WORKSPACE`, `MYOS_READONLY`, `MYOS_SHARED`,
  `MYOS_APPS_USED`.

## Design

### 1. Engine interface

New module `dashboard/engines/` with `base.py` and one module per driver.

```python
@dataclass
class TurnPolicy:            # computed by the server, same inputs as today
    shared: bool             # invited person can see the thread
    readonly: bool           # consulted teammate
    web: bool                # WebSearch allowed this turn
    web_domains: tuple       # hosts the user typed (WebFetch only to these)
    apps: bool               # connected-apps relay this turn
    workspace: str           # MYOS_WORKSPACE
    workspace_dir: str|None  # linked app folder, read-only
    file_search: bool        # Glob/Grep fallback on the linked folder
    deny_dirs: tuple
    deny_files: tuple
    apps_used: bool

class Engine:
    id: str                       # "claude-code", "codex", "acp:<name>", "api:<name>"
    shared_safe: bool             # lockdown verified live for shared threads
    supports: set                 # {"web", "apps", "file_search", "resume"}
    def tiers(self) -> dict       # {"fast": model, "strong": model}
    def turn(self, session, text, policy, model, tokens) -> Iterator[tuple]
    def complete(self, prompt, stdin, model) -> str   # one-shot, for generate.py
    def status(self) -> dict      # installed? logged in? version, error text
```

- `turn()` yields exactly today's tuples. The server code that consumes them
  does not change.
- `tokens` carries the per-turn apps/os relay tokens (minted by the server, as
  today).
- `ChatSession` keeps the thread state (session id, started, turn count, last
  skill, last bot, apps_used, shared) and gains `engine_id`. Driver-specific
  process code moves into the driver.
- Lockdown flags are hard: `shared` on an engine with `shared_safe = False`,
  or `apps` on an engine without `"apps"` in `supports`, raises
  `EngineRefused(reason)`. The server shows the reason and falls back as in
  section 6.
- Capability flags that only add tools (`web`, `file_search`) are soft: an
  engine without them runs the turn without the tool and the thread shows a
  short note ("web search is not available on this engine"). `_needs_web` is a
  guess from the text, so refusing would break ordinary turns. (Design call.)

### 2. claude-code driver

Today's `_base_cmd` and `ask` moved as they are. `shared_safe = True` (verified
in PR #22). Tests for the current flags move with it unchanged. This step
lands alone and must not change any behavior.

### 3. api driver

myOS runs the loop: send messages plus tool schemas, stream the reply, run the
tools the model calls, send results, repeat. Stdlib `urllib` with streaming
reads (the repo has no third-party Python dependencies; keep it that way).

- **Providers:** a provider is `{name, format: "anthropic"|"openai", base_url,
  key_ref, models: {fast, strong}}`. Presets: Anthropic, OpenAI, OpenRouter,
  Ollama (local, no key). Custom entries allowed.
- **History:** no CLI transcript exists, so myOS stores each API thread's
  messages itself: `chats/<id>.messages.jsonl` beside the chat record, same
  privacy as chat records. A long thread is trimmed oldest-first to a token
  budget, keeping the first (snapshot) message.
- **Tools myOS defines.** Nothing else exists, so the policy is enforced by
  construction:

  | Tool | When | How |
  |---|---|---|
  | `osctl` | always | `os_relay.run_osctl` in-process with the turn's ctx (workspace, readonly, apps_used; `MYOS_SHARED` when shared) |
  | `read_file` | not shared | myOS checks the path itself: repo files minus `SECRET_FILE_GLOBS`, minus `deny_dirs`/`deny_files`, minus `~/.claude` and every per-person account dir; the linked folder only when `workspace_dir` is set |
  | `search_files` | not shared, `file_search` | Glob/Grep equivalent, same path check |
  | `web_search` | `web` | Anthropic format: the provider's server-side web search tool. OpenAI format: off unless the provider offers one (builder checks OpenRouter's) |
  | `web_fetch` | `web` | myOS fetches; only hosts in `web_domains` (same rule as `web_fetch_rules`) |
  | connected apps | `apps`, not shared | call the apps relay handler in-process, same token checks and held-for-approval sends |

- `shared_safe = True`: in a shared turn only `osctl` (plus web when
  allowed) is offered.
- Limits: max 25 tool calls per turn, per-call output cap like
  `os_relay.MAX_OUT_CHARS`, overall timeout like today's turn timeout.
- Cost: `done` carries tokens and, when the provider returns it, cost.
- Tool events map to `("tool", name)` so the thread shows the same tool chips.

### 4. codex driver

`codex exec --json` (verified present in codex-cli 0.162: `--json`,
`exec resume <id>`, `--sandbox read-only|workspace-write|danger-full-access`,
`--ignore-user-config`, `-c key=value`, `--ephemeral`, `--add-dir`, `-m`).

- Always `--sandbox read-only` and `--ignore-user-config` so the user's own
  Codex config (their MCP servers, profiles) never loads into a bot turn.
- myOS's MCP relays through `-c mcp_servers.<name>.url=...` and
  `bearer_token_env_var` (Codex supports streamable HTTP MCP with a bearer env
  var, per `codex mcp add --help`).
- Event mapping from Codex JSONL to the tuples; the builder records the
  verified field paths in the module docstring, as `chat_session.py` does.
- `shared_safe = False` until a live probe shows that a shared turn can be
  given no shell and no file reads (read-only sandbox still reads any path,
  the same hole PR #22 closed for Claude Code). Until then Codex is a
  **full-disk engine** (section 5a).
- Osctl in owner turns: the shell must be limited to osctl. If Codex cannot
  restrict commands to a prefix, owner turns also use the osctl MCP relay
  instead of shell, and shell stays off.

### 5. acp driver

One driver for any ACP CLI. Config per agent: `{name, command, args}`
(e.g. Gemini CLI with its ACP flag, an OpenCode or Grok ACP mode, Zed's
Claude Code ACP adapter).

- myOS is the ACP client: `initialize`, `session/new` with myOS's MCP relays in
  `mcpServers`, `session/prompt`, stream `session/update`, answer
  `session/request_permission`.
- myOS denies every permission request except calls to its own MCP tools.
  This is not a lockdown by itself: agents may run read-only tools without
  asking. So `shared_safe = False` for every ACP agent, always, unless a
  specific agent gets its own verified flags (then it becomes its own driver).
- File writes the agent asks for are denied (myOS writes go through osctl).
- Every ACP agent is a **full-disk engine** (section 5a).

### 5a. Full-disk engines (Codex, ACP agents)

Their shell and read tools can read any file the OS user can, and no flag
fences that. No OS-level sandbox is built in this round. Instead they run only
where that equals the owner running the CLI in their own terminal:

- **Only the machine owner's own private threads:** the owner's DMs and
  private channels with no people listed. Never a thread an invited person
  can see, never `#general` once anyone is invited.
- **Only on the owner's own machine:** a server setting `deployment: "mac" |
  "server"` (default `mac`). With `server`, full-disk engines are hidden from
  the engines screen and refused. On a runner (section 10) they run only for
  the runner owner's own private threads.
- **No connected apps** in their turns (no apps relay, no apps token), so mail
  or calendar content can never reach a tool that reads the disk. Their own
  built-in web tools are switched off where the CLI allows it; where it does
  not, the engines screen says so.
- The engines screen and every thread on one show: "This engine can read any
  file on this computer."
- The checklist below applies to them except the file-visibility line, which
  this label replaces.

### 6. Engine choice and fallback

- Workspace setting `engine` (default: `claude-code` when the `claude` CLI is
  logged in, else the first configured API provider). Bot setting `engine`
  overrides it.
- Stored in the workspace's existing team settings.

### 6a. Engine per run, sessions per engine (design call)

The engine belongs to each **run** (one bot answering once), not to the
thread. One thread can mix bots on different engines, and a room can turn
shared later, so a thread-wide engine cannot hold.

- The chat record keeps `engines: {engine_id: session_state}`: the claude-code
  session id, the codex session id, the api driver's `messages.jsonl` path.
  A run on an engine already in the map continues that session.
- A run on an engine that is new to the thread (a tagged bot on another
  engine, a consulted teammate, the shared-engine fallback) starts a fresh
  session whose first prompt carries a recap: the thread's visible messages
  from `chat_store` (user and assistant text, never tool output), trimmed to a
  budget, newest kept. It never receives another engine's transcript.
- The shared-engine fallback is per turn: if the room becomes private again,
  the next owner turn goes back to the bot's engine and continues its session,
  with the recap of the messages it missed.
- Each assistant message records `engine_id` and `account`; the thread shows
  them as a small label.
- `chat_visible` rules are unchanged: `shared` stays false for good once any
  turn ran unshared.
- Shared thread on an engine with `shared_safe = False`: the turn runs on the
  workspace's **shared engine**, a second setting that only lists
  `shared_safe` engines (claude-code, api providers). If none is set, the bot
  replies that this channel needs a shared-safe engine and names the setting.
- Engine unavailable (CLI missing, logged out, key rejected): `("error", ...)`
  with the reason from `status()`. No silent switch to another engine; the
  thread names the problem.
- Model tiers: `_pick_model` asks for `fast` or `strong`; each engine maps them
  (claude-code: haiku/sonnet as today; codex and api: from settings, with
  presets). Bots with an explicit model keep it when the engine supports it.

### 7. Keys and the engines screen

- HQ → Settings → Engines, owner only. One row per driver: installed version,
  logged in yes/no, "test" button (one tiny turn), default tiers.
- API keys stored in `engines.json` at the data root, mode 0600, added to
  `.gitignore` (it is not ignored today),
  added to `SECRET_FILE_GLOBS`, to `_workspace_deny_files`, and to the osctl
  private suffix/prefix rules. Never returned by any API: the screen shows
  only the last 4 characters.
- Keys reach the api driver in memory only, never via env of a child process
  that also runs model-written commands.

### 8. Engine accounts per person (team servers, off by default)

- Setting `person_accounts: false`. When on, an invited person can connect
  their own engine account on their profile:
  - claude-code: the person runs `claude setup-token` on their own computer
    and pastes the token. myOS stores it in
    `team/accounts/<person-id>/claude/` and runs their turns with
    `CLAUDE_CONFIG_DIR` pointing there.
  - codex: per-person `CODEX_HOME` under `team/accounts/<person-id>/codex/`.
    Login method on a headless server: builder verifies the device-code flow.
  - api: their own key, stored per person like the owner's keys.
- `team/accounts/` is 0700, and every account dir is denied to every turn
  (Read deny rules for claude-code, path check for api, osctl private prefix).
- Scope: claude-code and api accounts only. Every turn an invited person
  sends is shared, so a person's account only ever runs on a `shared_safe`
  engine. Codex/ACP person accounts come only if one of them becomes
  shared-safe. (Design call.)
- **Who pays:** a turn runs on the account of the person who sent the message.
  No account connected: the workspace engine. Owner turns: the owner's.
- Indirect runs follow the sender: a consulted teammate and every
  `generate-*` job started inside a turn run on the sender's account (on the
  teammate bot's engine when it is shared-safe, else the shared engine). The
  server puts an account **reference** in the turn's osctl ctx
  (`MYOS_ACCOUNT=<person-id|owner|workspace>`, set by `os_relay.env_for`,
  never by the model); `generate.py` resolves it to the account dir or key
  itself. No secret value travels through osctl args or a model-visible env.
  Their messages carry the same account label.
- Every bot message records `engine_id` and `account` (owner / person id /
  workspace). The thread shows a small label, e.g. "via profile-a's Codex".
- Before connecting, the person sees: "Your token is stored on this server.
  Its admin can access it." One-click disconnect deletes the dir.
- Personal subscription tokens are for the person's own use: the screen links
  the provider's terms. A hosted myOS would turn this setting off and offer
  API keys only.

### 9. generate.py and the terminal

- `generate.py run_claude` becomes `engine.complete(prompt, stdin, model)` on
  the engine and account from section 8 (default: the workspace engine).
  Brief and voice input is untrusted, so `complete()` exists only on engines
  that can run with zero tools: claude-code (today's flags) and api (no tools
  sent). Codex and ACP keep a shell, so they never run `complete()`; a
  workspace on them uses its shared engine for jobs.
- Terminal command: setting, default the user's `$SHELL`; presets `claude`,
  `codex`, any installed CLI. Still local requests only. On a server, the
  terminal stays off unless a later design adds an owner-login rule for it.

### 10. Runners: the workspace on a server, the engine on a person's Mac

A team server (VPS) holds the workspace: channels, people, files, osctl. A
**runner** is myOS on a person's own Mac, connected out to that server, that
runs turns for the bots assigned to that person on that person's own engine
login. No tunnel, and each subscription only ever serves its owner. Prior art:
Buzz (`buzz-acp` connects agent CLIs to the relay from anywhere).

- **Connecting:** on their profile, a person (owner or invited) creates a
  runner token, shown once, stored hashed in `team/runners.json` (gitignored).
  On their Mac: `python3 -m dashboard.runner --server https://<host>
  --token-file <path>`. The runner uses the same engine drivers as the server.
- **Assignment:** bot setting `runner: <person-id>`. A turn for that bot goes
  to that person's runner when it is online.
- **Who may see what:** the server leases a turn to person X's runner only if
  X can see the thread (`chat_visible` with X as viewer). The runner never
  receives anything X could not read in the dashboard.
- **Protocol:** outbound HTTPS only. The runner long-polls `POST
  /api/runner/next` (runner token) and gets a job: thread id, bot, engine,
  model tier, the full prompt the server built (RAIL, snapshot, routed skill,
  recap), the policy, and a per-turn relay token. It streams the turn's event
  tuples back to `POST /api/runner/events/<turn>`; the server forwards them to
  the thread like local events. Heartbeat every 20 s; a missed heartbeat
  marks the runner offline.
- **Tools:** a runner turn always uses the shared policy: its only myOS tool is
  osctl through `/api/runner/os/mcp`, a new path that accepts the runner token
  **and** the turn token of a turn leased to that runner (the local-only
  `/api/internal/os/mcp` stays local-only). The runner's own files are not the
  workspace, so no file or shell tools. Web per policy. Connected apps stay on
  the server: a turn that needs apps runs on the server's engine.
- **Full-disk engines on a runner:** only for threads that are the runner
  owner's own private threads (section 5a), labeled.
- **Sessions:** the engine session (claude-code session id, codex session id)
  lives on the runner's Mac; the chat record's `engines` map (6a) stores
  `{engine_id, runner: person-id, session}`. Another runner or the server
  picking the thread up starts fresh with the recap.
- **Offline:** if the assigned runner is offline, the turn runs on the
  workspace's shared engine, or, when none is set, the bot replies
  "<name>'s runner is offline".
- **Who pays:** the runner owner's login. Messages carry the label "via
  <name>'s Mac".
- **Revoking:** deleting the token on the profile ends the runner's lease at
  once; leased turns in flight are cancelled.

## Order of work

Each step is its own PR, tests green, merged before the next.

1. Engine interface + claude-code driver (pure refactor, no behavior change).
2. api driver: Anthropic format first, then OpenAI-compatible. Engines screen
   with keys. Workspace/bot engine settings, shared engine setting.
3. generate.py and terminal through the engine layer.
4. codex driver, with the live lockdown probe recorded in the PR.
5. acp driver.
6. Engine accounts per person.
7. Runners (needs a myOS deployable on a server; the Docker/VPS packaging is
   its own design and may land before this step).

## Security checklist (every driver)

- Shared turn: only osctl (+ web per policy). Verified live with a probe like
  PR #22's (ask the bot to read `/etc/hosts` and a file in another workspace;
  both must fail).
- Bot turns never see `engines.json`, `team/accounts/`, `~/.claude`,
  `~/.codex`, or another workspace's folder.
- Relay tokens in env only, never argv.
- No user config of any CLI loads into a bot turn (`--setting-sources ""`,
  `--ignore-user-config`, ACP agents started with a clean env).
- Apps sends still held for approval; apps_used threads still lose web.

## Testing

- Unit: each driver's command or request builder per policy (shared, readonly,
  web, apps, file_search), event mapping from recorded fixture streams,
  `EngineRefused` paths, api path checks for `read_file`, key redaction in API
  responses.
- Fake HTTP server for the api driver (streaming, tool calls, errors, 401).
- Function tests: a shared turn on each `shared_safe` engine offers only
  osctl; a shared turn on a non-safe engine falls back or refuses.
- `tests/test_no_usage_data.py` before every commit. Fixtures use `demo`,
  `acme`, `profile-a`, `profile-b`.

## Left to the builder

- Exact module layout under `dashboard/engines/`, names of settings keys.
- Codex and ACP event field paths (verify against installed versions, record
  them in docstrings).
- History trimming budget for the api driver.
- Preset model names per tier for codex and api providers.
- Engines screen layout, following the existing settings screens.
