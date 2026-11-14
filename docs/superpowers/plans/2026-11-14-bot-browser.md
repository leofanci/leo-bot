# Bot Browser Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Bots drive a real browser (the owner's Chrome or a per-bot background Chrome) through a gated relay, with send plans, daily caps per site, and the same access levels as connected apps.

**Architecture:** A new MCP endpoint `/api/internal/browser/mcp` mirrors `dashboard/apps_relay.py`: same turn tokens, a gate in front, and an `Upstream` behind it that talks to a pinned `agent-browser mcp` child process per bot. Every engine that already mounts the apps relay mounts this one the same way. Sends after typing are held to approved send plans (`write`) or capped (`act`).

**Tech Stack:** Python 3 stdlib (server, relay, stores), `agent-browser` (pinned npm package, run as a child process), existing `core/approvals.py`, `dashboard/taint.py`, `core/runners.py` (phase 2).

**Spec:** `docs/superpowers/specs/2026-11-14-bot-browser-design.md`

## Global Constraints

- `agent-browser` is a pinned dependency, run as a child process. No code copied from it or from any other project.
- Tests: `python3 -m unittest discover -t . -s tests`. All pass before each commit, including `tests/test_no_usage_data.py`.
- Fixtures use `demo`, `acme`, `profile-a`, `profile-b`, and `http://127.0.0.1` test pages. No real people, accounts or messages in tracked files.
- Default site caps: `linkedin.com` 20 per rolling 24 h, `instagram.com` 10 per rolling 24 h. Caps count all bots and workspaces together, per site.
- Approved send-plan items expire after 7 days (`PLAN_TTL = 7 * 86400`).
- Idle browser child closes after 10 minutes (`IDLE_SECONDS = 600`); saved logins stay.
- Shared threads never get browser tools. Report only jobs never send.
- `my_chrome` never falls back to `bot`.
- Copy in the UI and in refusals: plain words, no em dash.

## Review Focus

1. A bot types, then clicks something other than Send (emoji, a second field): the relay treats it as a send and refuses or holds it, with a line saying why. Test in Task 3.
2. Two bots, two modes, one site bound to `my_chrome`: the `bot`-mode bot opening that site is refused before any page loads. Test in Task 3.
3. The cap is reached by bot A in workspace `demo`; bot B in workspace `acme` tries a send on the same site: refused. Test in Task 3.
4. Approved text differs only in spacing or line breaks from what the bot typed: the send runs; differing by one word: refused. Test in Task 3.
5. `my_chrome` chosen, Chrome closed or remote debugging off: the first call returns one plain line ("Chrome is not open with remote debugging on...") and never starts a background Chrome. Test in Task 4.

---

### Task 1: Pin and probe the engine

Confirms the real tool names and flags of the pinned `agent-browser` before the relay encodes them. No guessing: every name later tasks use comes from this probe.

**Files:**
- Create: `core/browser_engine.py`
- Create: `docs/browser.md` (section "Engine facts", filled from the probe)
- Test: `tests/test_browser_engine.py`

**Interfaces:**
- Produces: `VERSION: str` (the pinned version), `resolve(env=os.environ) -> Path | None`, `status() -> dict` with `{"state": "ready"|"missing", "detail": str}`, `install() -> dict` (same shape), `mcp_argv(binary: Path) -> list[str]`.
- Produces: `READ_TOOLS`, `TYPE_TOOLS`, `COMMIT_TOOLS`, `NAV_TOOLS: frozenset[str]` in `core/browser_engine.py`, the exact MCP tool names from the probe. Every other tool name is refused by the relay.

- [ ] **Step 1: Probe.** In a scratch folder: `npm install agent-browser@<latest release at least 2 weeks old>`, `npx agent-browser install`, then start `npx agent-browser mcp --tools core` and send `initialize` + `tools/list`. Record in `docs/browser.md`: the version, every tool name with a one-line meaning, the env vars and flags for (a) a named saved session, headless, (b) a headed window for a first login, (c) attaching to a running Chrome by CDP URL, and where Chrome writes `DevToolsActivePort` on macOS and Linux. Also record how to read the current page URL (a tool or a reply field).
- [ ] **Step 2: Write failing tests** `test_resolve_prefers_env_then_data_then_path`, `test_status_missing_says_how_to_install`, `test_mcp_argv_core_tools_only` (argv contains `mcp` and `--tools core`, never a file or eval tool set), `test_tool_sets_disjoint` (the four sets share no name).
- [ ] **Step 3: Run** `python3 -m unittest tests.test_browser_engine -v`. Expected: FAIL (module missing).
- [ ] **Step 4: Implement** `core/browser_engine.py`: resolve order `LEOBOT_AGENT_BROWSER` env, then `paths.DATA / "tools" / "agent-browser" / VERSION / "node_modules/.bin/agent-browser"`, then `shutil.which("agent-browser")`. `install()` runs `npm install --prefix <that folder> agent-browser@VERSION` then `<binary> install`, with a 600 s timeout, and returns `status()`.
- [ ] **Step 5: Run** the tests. Expected: PASS. Then the full suite.
- [ ] **Step 6: Commit** `feat(browser): pin agent-browser and record its tool names`.

### Task 2: Browser settings store

**Files:**
- Create: `core/browser.py`
- Test: `tests/test_browser_settings.py`

**Interfaces:**
- Consumes: nothing.
- Produces (data in `<root>/team/browser.json`, written atomically like `core/integrations.py`):
  - `LEVELS = ("off", "read", "write", "act")`, `MODES = ("bot", "my_chrome")`
  - `DEFAULT_SITES = {"linkedin.com": {"mode": None, "daily": 20}, "instagram.com": {"mode": None, "daily": 10}}`
  - `bot_setting(ws, bot, root) -> {"level": str, "mode": str}` (default `{"level": "off", "mode": "bot"}`)
  - `set_bot(ws, bot, level, mode, root) -> dict` (raises `ValueError` on an unknown level or mode)
  - `sites(root) -> dict[str, {"mode": str|None, "daily": int|None}]`
  - `set_site(host, mode, daily, root) -> dict` (`daily` None or 1..500; `mode` None or one of `MODES`)
  - `site_of(url, root) -> str | None`: the listed site a URL belongs to (`www.linkedin.com`, `m.linkedin.com` -> `linkedin.com`; `evil-linkedin.com` -> None)
  - `forget_workspace(ws, root)`, called where `integrations.forget_workspace` is called.

- [ ] **Step 1: Write failing tests** `test_defaults_off_and_bot`, `test_rejects_unknown_level`, `test_site_of_matches_subdomains_only` (cases above plus `https://linkedin.com.evil.example` -> None), `test_daily_bounds`, `test_forget_workspace_drops_bots`.
- [ ] **Step 2: Run** `python3 -m unittest tests.test_browser_settings -v`. Expected: FAIL.
- [ ] **Step 3: Implement** `core/browser.py` as specified; host matching is `host == site or host.endswith("." + site)` on the lowercased, port-stripped host.
- [ ] **Step 4: Run** the tests, then the full suite. Expected: PASS.
- [ ] **Step 5: Commit** `feat(browser): per-bot level and mode, sites with caps`.

### Task 3: The gate

**Files:**
- Create: `dashboard/browser_relay.py`
- Modify: `core/approvals.py` (send-plan helpers)
- Modify: `core/app_preview.py` (`build` case for `BROWSER_SEND`)
- Test: `tests/test_browser_relay.py`

**Interfaces:**
- Consumes: `core.browser` (Task 2), tool sets from `core.browser_engine` (Task 1), `apps_relay.TOKENS` (turn ctx: `ws, bot, chat, room, person, report, taint`).
- Produces:
  - `class Upstream: forward(frame, sid) -> (reply, sid); current_url() -> str | None; mode -> str`
  - `handle(frame, ctx, sid, upstream, root) -> (reply, sid)`, the only entry point.
  - `PLAN_TOOL = "leobot_plan_sends"`, added to `tools/list` replies; `tools/list` keeps only names in the Task 1 sets plus `PLAN_TOOL`.
  - In `core/approvals.py`: `BROWSER = "browser"` toolkit name; `add_send_plan(root, *, ws, room, chat, bot, person, items) -> list[dict]` (one batch, slug `BROWSER_SEND`, arguments `{"url", "text", "site"}`); `approve_send(aid, root) -> dict` (status `approved`, `approved_at`); `take_send(root, *, ws, bot, url, text, now=None) -> dict | None` (finds an `approved` item of that bot, same host and path, normalized text equal, not older than `PLAN_TTL`; marks it `done`, result `Sent from the browser.`); `sends_today(site, root, now=None) -> int` (done browser sends of that site in the last 24 h, all workspaces, including `act` runs logged with `log_app_run`).
  - Per-session state kept in the relay, keyed by the MCP session id: `typed: str` (text typed since the last commit or navigation).

Rules, in order, for a `tools/call`:
1. Unknown tool name: refused.
2. Level `off`: refused. Level `read`: only `READ_TOOLS` and `NAV_TOOLS`.
3. A `NAV_TOOLS` call to a URL whose `site_of` is bound to the other mode: refused before forwarding (`SITE_MODE`).
4. `TYPE_TOOLS` at `write`/`act`: forwarded; the typed value is appended to `typed`.
5. `COMMIT_TOOLS` with `typed` empty: forwarded (navigation click).
6. `COMMIT_TOOLS` with `typed` set (a send): Report only job: refused. `write`: `take_send(...)` on `upstream.current_url()` and `typed`; none: refused (`NO_PLAN`). `act`: `sends_today(site) >= daily`: refused (`CAPPED`); else forwarded and logged with `approvals.log_app_run(..., toolkit=BROWSER, slug="BROWSER_SEND", status="done")`. A `write` send on a capped site also checks the cap. `typed` resets after any send attempt.
7. Every forwarded call marks `ctx["taint"].saw_outside()` when a taint is present.

Refusal lines (exact copy):
- `SITE_MODE = "{site} is set to open only in {mode_label}. This bot uses {other_label}, so it did not open it."`
- `NO_PLAN = "This would send what you typed, and no approved send plan matches it. Call leobot_plan_sends with the page address and the exact text, then stop: the owner approves it in Approvals."`
- `CAPPED = "{site} reached today's limit of {daily} sends. Nothing was sent. Try again tomorrow or ask the owner to raise the limit in Settings, Browser."`
- `REPORT_ONLY = "This scheduled job only reports, so it does not send. Nothing was sent."`
- `HELD_PLAN = "{n} sends wait for one approval (Approve all in Approvals). None has gone out. Tell the user."`

- [ ] **Step 1: Write failing tests** with a `FakeUpstream(url, mode)` recording forwarded frames:
  - `test_unknown_tool_refused`, `test_read_level_refuses_typing`
  - `test_click_without_typing_forwards`
  - `test_type_then_any_click_is_a_send` (Review Focus 1: type, then click a different element: at `write` with no plan -> `NO_PLAN`, nothing forwarded)
  - `test_bound_site_other_mode_refused_before_forward` (Review Focus 2)
  - `test_cap_counts_all_workspaces` (Review Focus 3: 20 done sends logged from `demo`, bot in `acme` at `act` -> `CAPPED`)
  - `test_plan_text_whitespace_matches_word_does_not` (Review Focus 4)
  - `test_plan_item_used_once`, `test_plan_item_expires_after_7_days`, `test_plan_other_bot_cannot_use`
  - `test_report_job_never_sends`, `test_forward_marks_outside`
  - `test_plan_tool_holds_one_batch` (`HELD_PLAN`, items in `approvals.pending` with one batch id)
- [ ] **Step 2: Run** `python3 -m unittest tests.test_browser_relay -v`. Expected: FAIL.
- [ ] **Step 3: Implement** the approvals helpers, the `BROWSER_SEND` preview (title `Send on {site}`, body: the URL and the full text), then `dashboard/browser_relay.py` with the rules above. Normalize text with `" ".join(text.split())`.
- [ ] **Step 4: Run** the tests, then the full suite. Expected: PASS.
- [ ] **Step 5: Commit** `feat(browser): gate with send plans, caps and site modes`.

### Task 4: The upstream (child process per bot)

**Files:**
- Create: `dashboard/browser_upstream.py`
- Test: `tests/test_browser_upstream.py` (uses a fake `agent-browser` script that speaks MCP over stdio, in `tests/fixtures/fake_agent_browser.py`)

**Interfaces:**
- Consumes: `core.browser_engine.resolve`, `mcp_argv` (Task 1); `browser_relay.Upstream` (Task 3).
- Produces:
  - `class BrowserUpstream(Upstream)`: `__init__(ws, bot, mode, root)`, `forward`, `current_url`, `close()`.
  - `POOL.get(ws, bot, mode, root) -> BrowserUpstream` (one child per `(ws, bot)`; a mode change closes the old one), `POOL.sweep(now=None)` (closes children idle `IDLE_SECONDS`), `POOL.close_all()` (server shutdown).
  - `key_file(root) -> Path`: `<root>/run/browser-engine-key`, created once, mode 0600.
  - `find_chrome_debug_url(home=Path.home(), platform=sys.platform) -> str | None` from `DevToolsActivePort` (paths from Task 1's probe).
  - `CHROME_OFF = "Chrome is not open with remote debugging on, so this bot cannot use your Chrome. Open Chrome and turn on remote debugging (Settings, Browser shows how), then ask again."`
- `bot` mode env: session `<ws>--<bot>`, saved-login restore key, the encryption key, headless on. `my_chrome` mode: attach to `find_chrome_debug_url()`; when it is None, `forward` returns `CHROME_OFF` as an MCP error result and starts nothing.

- [ ] **Step 1: Write failing tests** `test_one_child_per_bot`, `test_mode_change_restarts_child`, `test_idle_sweep_closes`, `test_key_file_0600_and_stable`, `test_my_chrome_off_says_how_and_starts_nothing` (Review Focus 5: no child spawned), `test_child_crash_reported_once` (fake exits mid-call: one MCP error, next call starts a fresh child).
- [ ] **Step 2: Run** `python3 -m unittest tests.test_browser_upstream -v`. Expected: FAIL.
- [ ] **Step 3: Implement.** Line-delimited JSON-RPC over the child's stdin/stdout, one lock per child, 120 s per call; stderr drained with `engines.base.drain`. Start `POOL.sweep` on the server's existing minute timer and `close_all` in its shutdown path.
- [ ] **Step 4: Run** the tests, then the full suite. Expected: PASS.
- [ ] **Step 5: Commit** `feat(browser): one engine child per bot, both modes`.

### Task 5: Mount it on every engine that mounts apps

**Files:**
- Modify: `dashboard/engines/base.py` (`TurnPolicy.browser: bool = False`; `TurnTokens.browser_token: str = None`)
- Modify: `dashboard/engines/claude_code.py` (`BROWSER_MCP_PATH = "/api/internal/browser/mcp"`, `BROWSER_TOKEN_ENV = "LEOBOT_BROWSER_TOKEN"`; `build_cmd` adds server `browser` and `mcp__browser` to allowed tools when `policy.browser` and not shared)
- Modify: `dashboard/engines/codex.py` (`mcp_servers.browser.*` like `apps`; env carries the token)
- Modify: `dashboard/server.py` (POST `/api/internal/browser/mcp` like the apps endpoint at `server.py:2478`; `_browser_rpc` for the api engine like `_apps_rpc`; mint the token where the apps token is minted, when `browser.bot_setting(...)["level"] != "off"` and the thread is not shared; revoke it with the apps token; a `## Browser` note in the rail naming `leobot_plan_sends` and the send rule)
- Modify: `dashboard/engines/api_tools.py` (browser tools listed and called through `_browser_rpc`)
- Test: `tests/test_browser_engines.py`

**Interfaces:**
- Consumes: `browser_relay.handle` (Task 3), `browser_upstream.POOL` (Task 4), `apps_relay.TOKENS`.
- Produces: `browser_mcp_config(port) -> str` in `claude_code.py`; engines' `supports` gain `"browser"` for Claude Code, Codex and the api engine only (not ACP, not runner).

- [ ] **Step 1: Write failing tests** `test_claude_cmd_has_browser_server_only_when_on`, `test_shared_thread_never_gets_browser`, `test_codex_cmd_has_browser_server`, `test_token_not_in_argv`, `test_endpoint_refuses_without_token`, `test_api_engine_lists_browser_tools`, `test_acp_and_runner_never_get_browser`.
- [ ] **Step 2: Run** `python3 -m unittest tests.test_browser_engines -v`. Expected: FAIL.
- [ ] **Step 3: Implement** as listed, following the apps path line for line.
- [ ] **Step 4: Run** the tests, then the full suite. Expected: PASS.
- [ ] **Step 5: Commit** `feat(browser): browser tools on Claude Code, Codex and API key bots`.

### Task 6: Approvals and settings screens

**Files:**
- Modify: `dashboard/server.py` (`_approve_app_action`: toolkit `browser` calls `approvals.approve_send` and appends the note `Approved. The bot sends it on its next run.`, never Composio; `_answer_app_batch` works unchanged. New endpoints: `GET/POST /api/browser/settings`, `POST /api/browser/install`, `POST /api/browser/login` `{ws, bot, url}` which opens a headed window for that bot's `bot`-mode session on this machine)
- Modify: `dashboard/app.js`, `dashboard/app.html`, `dashboard/app.css` (Team: per bot "Browser" level Off / Reads only / Read + write / Acts on its own, and mode Bot browser / My Chrome. Settings, Browser: engine status with Install, the sites table (site, opens in, daily limit), the My Chrome check with how to turn on remote debugging, and "Log in" per bot)
- Test: `tests/test_browser_api.py`

**Interfaces:**
- Consumes: `core.browser` (Task 2), `approvals.approve_send` (Task 3), `browser_engine.install/status` (Task 1).

- [ ] **Step 1: Write failing tests** `test_approve_browser_send_never_calls_composio`, `test_batch_approve_marks_all_approved`, `test_settings_owner_only`, `test_settings_rejects_bad_level`, `test_login_refused_for_my_chrome_mode`.
- [ ] **Step 2: Run** `python3 -m unittest tests.test_browser_api -v`. Expected: FAIL.
- [ ] **Step 3: Implement** the server parts, then the screens.
- [ ] **Step 4: Run** the tests, then the full suite. Expected: PASS. Open the dashboard, set a bot to Read + write, Bot browser, and see the settings save and reload.
- [ ] **Step 5: Commit** `feat(browser): approvals and settings screens`.

### Task 7: Live check and docs (phase 1 done)

**Files:**
- Modify: `docs/browser.md` (setup, levels, send plans, sites, both modes, limits, what is not supported)
- Create: `tests/fixtures/browser_form.html` (a local page with a text box and a Send button that shows "sent: <text>")

- [ ] **Step 1:** Serve the fixture on `http://127.0.0.1:<port>`. With a Claude Code bot at Read + write, Bot browser: ask it to send "hello from demo" on that page. Expected: one plan card; after Approve, on the next turn the page shows `sent: hello from demo`, and Approvals shows "Sent from the browser".
- [ ] **Step 2:** Same with Acts on its own and the site added with daily 1: the first send runs, the second is refused with `CAPPED`.
- [ ] **Step 3:** My Chrome mode with Chrome open and remote debugging on: the bot reads the fixture page in the owner's Chrome. With Chrome closed: `CHROME_OFF`.
- [ ] **Step 4:** Repeat Step 1 on Codex. Record results and engine versions in `docs/browser.md`.
- [ ] **Step 5: Commit** `docs(browser): setup guide and live check`. Open the PR, wait for CI, resolve review comments, merge.

### Task 8 (phase 2): A server borrows the owner's Chrome through the runner

**Files:**
- Modify: `core/runners.py` (a `browser` job kind: one MCP frame in, one reply out, same lease and owner checks as turns)
- Modify: `dashboard/runner.py` (the runner keeps a local `BrowserUpstream(mode="my_chrome")` and answers `browser` jobs with it)
- Create: `dashboard/browser_lent.py` (`class RunnerChromeUpstream(Upstream)`: forwards through `runners.HUB` to the turn owner's runner)
- Modify: `dashboard/browser_upstream.py` (`POOL.get` returns `RunnerChromeUpstream` for `my_chrome` when `deployment` is server mode)
- Test: `tests/test_browser_lent.py`

**Interfaces:**
- Consumes: `runners.HUB`, `BrowserUpstream` (Task 4).
- Produces: `RUNNER_OFF = "Your computer is offline, so this bot cannot use your Chrome. Open Leo Bot on your computer, then ask again."`

- [ ] **Step 1: Write failing tests** `test_frames_go_to_owner_runner_only` (a ctx for person B never reaches person A's runner), `test_runner_offline_says_so_no_fallback`, `test_runner_reply_size_capped` (reuse `MAX_TURN_CHARS`), `test_shared_thread_refused`.
- [ ] **Step 2: Run** `python3 -m unittest tests.test_browser_lent -v`. Expected: FAIL.
- [ ] **Step 3: Implement.** The runner only answers while its owner turned on "Lend my Chrome" in the runner's own settings (default off).
- [ ] **Step 4: Run** the tests, then the full suite. Expected: PASS. Live: a server Leo Bot reads the fixture page in the owner's Chrome through a real runner.
- [ ] **Step 5: Commit** `feat(browser): server bots use the owner's Chrome through the runner`.

### Task 9 (phase 2): Bot browser logins and proxy on a server

**Files:**
- Modify: `dashboard/browser_upstream.py` (`export_login(ws, bot) -> bytes`, `import_login(ws, bot, blob)`, both through the engine's own encrypted state export/import named in Task 1's probe)
- Modify: `core/browser.py` (`set_site(..., proxy=None)`: an optional proxy URL per site, used only by `bot` mode)
- Modify: `dashboard/server.py` (`POST /api/browser/login/send` on the owner's computer pushes the exported login to the server through the runner; owner only)
- Test: `tests/test_browser_login_transfer.py`

- [ ] **Step 1: Write failing tests** `test_export_import_round_trip_with_fake_engine`, `test_login_transfer_owner_only`, `test_proxy_only_in_bot_mode`, `test_proxy_url_never_logged` (a proxy with a password never appears in logs or Approvals).
- [ ] **Step 2: Run** `python3 -m unittest tests.test_browser_login_transfer -v`. Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the tests, then the full suite. Expected: PASS. Live: log in to the fixture site locally, send the login to the server, and have a server bot read the logged-in page.
- [ ] **Step 5: Commit** `feat(browser): move bot browser logins to a server, proxy per site`. Open the PR, wait for CI, resolve review comments, merge.
