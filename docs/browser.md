# Bot browser

Bots drive a real browser through `agent-browser` (Vercel Labs, Apache-2.0, npm package), run as a pinned child process. Leo Bot never sends the engine's tools straight to a bot: a relay in front of it decides which tool calls pass. This page records what the engine really offers, measured by probing it, so the relay encodes real names.

## Engine facts

Probed on macOS (arm64), Node 22. Pinned version: **0.41.0** (npmjs.com release of 2026-10-29, the newest one at least two weeks old when probed; `core/browser_engine.VERSION`). Re-probe before changing the pin.

### MCP stdio mode exists

`agent-browser mcp` starts an MCP stdio server (protocol 2024-11-05 handshake works; `serverInfo` name `agent-browser`, version `0.41.0`). Tools load by profile: `--tools <profile>[,<profile>]`. With no flag the profile is `core`. Profiles: `core` (29 tools), `network`, `state`, `debug`, `tabs`, `react`, `mobile`, `webmcp`, `all` (156). Leo Bot starts it as `<binary> mcp --tools core` and never any other profile (`browser_engine.mcp_argv`).

The binary is a native executable, so `agent-browser mcp --help` does not print help: it starts the server and waits on stdin.

### Core tools (29)

Annotation: R = the engine marks it `readOnlyHint`. Every tool also takes the per-call fields listed under "Per-call fields".

| Tool | Meaning | Set |
|---|---|---|
| `agent_browser_tools_profiles` | List MCP profiles | refused |
| `agent_browser_open` | Launch the browser, optionally go to `url` (also `headed`, `webgpu`, `webmcp`) | NAV |
| `agent_browser_read` | Fetch a URL (or the active page) as agent-readable text (R) | READ |
| `agent_browser_snapshot` | Accessibility tree with reusable `@ref` handles (R) | READ |
| `agent_browser_click` | Click a `selector` (an `@ref` or CSS) | COMMIT |
| `agent_browser_fill` | Clear a field and fill it with `text` | TYPE |
| `agent_browser_type` | Type `text` into a field | TYPE |
| `agent_browser_press` | Press `key` at the focus | COMMIT |
| `agent_browser_check` | Check a checkbox or switch | COMMIT |
| `agent_browser_uncheck` | Uncheck a checkbox or switch | COMMIT |
| `agent_browser_select` | Select option `values` in a select | COMMIT |
| `agent_browser_scroll` | Scroll the page or an element | READ |
| `agent_browser_wait_ms` | Wait a fixed time (R) | READ |
| `agent_browser_wait_for_selector` | Wait for an element (R) | READ |
| `agent_browser_wait_for_text` | Wait for text (R) | READ |
| `agent_browser_wait_for_load` | Wait for a load state (R) | READ |
| `agent_browser_screenshot` | Capture a screenshot, returns the saved path | READ |
| `agent_browser_get_text` | Visible text of an element (R) | READ |
| `agent_browser_get_url` | Current page URL (R) | READ |
| `agent_browser_get_title` | Current page title (R) | READ |
| `agent_browser_eval` | Run arbitrary JavaScript in the page | refused |
| `agent_browser_close` | Close the browser session (`all` closes every session) | refused |
| `agent_browser_back` | History back | NAV |
| `agent_browser_forward` | History forward | NAV |
| `agent_browser_reload` | Reload | NAV |
| `agent_browser_tab_new` | New tab, optionally at a `url` | NAV |
| `agent_browser_tab_list` | List tabs (R) | NAV |
| `agent_browser_tab_switch` | Switch to a tab by id, label or target id | NAV |
| `agent_browser_tab_close` | Close a tab | NAV |

The other profiles add (not used): `network` (routing, HAR, headers, credentials), `state` (cookies, storage, auth vault, saved state, sessions, Chrome profiles), `debug` (console, trace, pdf, downloads, uploads, recording, clipboard, plugins, chat, install), `tabs`, `react`, `mobile`, `webmcp`. `all` also adds `hover`, `focus`, `dblclick`, `drag`, `upload`, `download`, `pdf`, `keydown`, `keyup`, `get_html`, `get_cdp_url`, `wait_for_function`, and more.

### Classification (`core/browser_engine.py`)

The four sets are disjoint. A tool in none of them is refused by the relay.

- `READ_TOOLS`: `read`, `snapshot`, `screenshot`, `scroll`, `get_text`, `get_url`, `get_title`, `wait_ms`, `wait_for_selector`, `wait_for_text`, `wait_for_load`. They look at the page or wait; none submits anything. `scroll` can trigger lazy loading but sends nothing the user typed. `screenshot` writes an image file under the engine's folder; the relay must return the image and not accept a caller `path` or `screenshotDir`. `read` can fetch a URL that is not the current page; the relay should treat its `url` like a navigation (same allow rules as `open`).
- `TYPE_TOOLS`: `fill`, `type`. They only put text into a field. Nothing leaves the page until a commit tool runs, so they stay cheap.
- `COMMIT_TOOLS`: `click`, `press`, `select`, `check`, `uncheck`. Any of them can submit a form or trigger a purchase, send or delete after text was typed (a click on Send, Enter in a field, a changed select, a toggled consent box). The relay gates these after a type tool ran.
- `NAV_TOOLS`: `open`, `back`, `forward`, `reload`, `tab_new`, `tab_list`, `tab_switch`, `tab_close`. They move between pages or tabs. `tab_list` only lists, but belongs with tab handling.
- Refused (in no set): `eval` (arbitrary script, can read cookies and tokens, bypasses every other gate; only the relay calls it, with one fixed script, to read field values), `close` (ends the shared session; Leo Bot owns the lifecycle), `tools_profiles` (engine admin). Also refused by absence, since the `core` profile does not load them: every file, upload, download, pdf, cookie, storage, header, network and state tool.

### Reading the current URL

Tool `agent_browser_get_url`: the reply text is the bare URL (`content[0].text`). Navigation replies (`open`) also carry the title in `content[0].text` and `url` plus `title` inside `structuredContent.response.data`. Tool results have `structuredContent: {exitCode, response: {data: {...}}}` and `isError`. CLI equivalent: `agent-browser --json get url`.

### Snapshot format

Probed on 0.41.0 against a local fixture page served on 127.0.0.1 only. The gate does not parse it for field values (a value can hold text that reads as more tree; see "Reading field values"). `agent_browser_snapshot` returns the tree as text in `content[0].text` and again in `structuredContent.response.data.snapshot`; `data.refs` maps each ref to `{name, role}` only, with no field values. One line per element:

```
- textbox "Name" [ref=e3]: Alice
- textbox "Empty" [ref=e4]
- searchbox "Find" [ref=e5]: query
- textbox "Msg" [ref=e6]: line one
line two
- generic "Editor" [ref=e1] editable [contenteditable]: Rich text
- textbox "Pass" [ref=e7]: ••••••
- combobox [expanded=false, ref=e4]: a
  - option "a" [selected, ref=e7]
- button "Send" [ref=e2]
```

- A field's current value follows `: ` after its attributes; an empty field has no `: `. A placeholder is shown as the name, never as the value.
- `textarea` and `input type=text|email` are `textbox`; `type=search` is `searchbox`; a contenteditable element keeps its role (often `generic`) and carries `editable [contenteditable]`. A password value is masked as bullets.
- A multi-line value continues on the next lines without a `- ` prefix. With `compact: true` only its first line is shown.
- With no interactive element the text is `(no interactive elements)`.

### Reading field values

Probed on 0.41.0 against a 127.0.0.1 fixture page (prefilled input, email, password, hidden, checkbox, empty search, multi-line textarea, nested contenteditable, an open shadow root with an input and a `plaintext-only` element, a same-origin iframe). `agent_browser_eval` takes `script` (required) and `timeoutMs`.

- Success: `isError: false`; the returned value, serialized by value (objects, arrays, strings, numbers; a Promise is awaited), is `structuredContent.response.data.result`, next to `data.origin` (the page URL). `content[0].text` holds the same value as pretty JSON.
- Script error: `isError: true`, `content[0].text` and `response.error` start with `Evaluation error:`, `response.success: false`, `data: {}`.
- The relay's `FIELDS_SCRIPT` returns `{fields: [...]}`. On the fixture it read `["pre filled", <the email value>, "line one\nline two", "Rich bold\ninner", "in shadow", "plain only", "in frame"]`: password, hidden, checkbox and empty fields left out, a nested contenteditable read once through its root, the shadow root and the iframe walked. A live run of the relay's own reader through the engine returned the same list, and after `type` into a prefilled textarea it read the combined value.

### Per-call fields (relay must control them)

Every core tool also accepts these inputs, so a bot could choose them unless the relay strips or overwrites them: `session`, `namespace`, `restore`, `restoreSave`, `restoreCheckUrl`, `restoreCheckText`, `restoreCheckFn`, `allowedDomains`, `extraArgs`, `caCert`, `clearCaCert`, `useSystemCa`, `idleTimeout`, `timeoutMs`. `extraArgs` is a raw argv list; the engine rejects the flags that change network, file, policy or host access (`--allowed-domains`, `--executable-path`, `--args`, `--config`), but the relay should drop it entirely. `open` also takes `headed`. `screenshot` takes `path` and `screenshotDir`. `restoreCheckFn` is script. The relay must set `session`, `namespace`, `restore` and `allowedDomains` itself and reject everything else it did not whitelist, with `additionalProperties: false` in the engine's schemas as the reference.

### (a) Named saved session, headless

Headless is the default. A session is named with `--session <name>` (env `AGENT_BROWSER_SESSION`; MCP field `session`). Cookies and localStorage are saved and restored by `--restore [name]` (env `AGENT_BROWSER_RESTORE`; MCP field `restore`: `true` uses the session name, a string is an explicit key). `--restore-save auto|always|never` (env `AGENT_BROWSER_RESTORE_SAVE`) sets when it saves; `--namespace <name>` (env `AGENT_BROWSER_NAMESPACE`) isolates daemon sockets and restore folders. Verified: a `--restore` session wrote a state file when the session closed.

A daemon per session stays alive after a call (idle timeout 1h, `--idle-timeout` / `AGENT_BROWSER_IDLE_TIMEOUT_MS`, `0` disables). Close with `agent-browser --session <s> --namespace <n> close`.

### (b) Headed window for a first login

`--headed` (env `AGENT_BROWSER_HEADED`, MCP `open` field `headed`) shows the Chrome window. Use the same `--session` and `--restore` as the headless runs, log in by hand, close: the saved state is then reused headless. A custom persistent Chrome profile folder is also possible with `--profile <path>` (env `AGENT_BROWSER_PROFILE`); `--allowed-domains` refuses profiles and restore replay.

### (c) Attach to a running Chrome

`--cdp <port|url>` (env `AGENT_BROWSER_CDP`) connects to a CDP port or WebSocket URL; `connect <port|url>` attaches the session until `close`. `--auto-connect` (env `AGENT_BROWSER_AUTO_CONNECT`) finds a running Chrome by itself. Add `--pin-tab` (env `AGENT_BROWSER_PIN_TAB`) so a session keeps its own tab. Chrome must be started with `--remote-debugging-port=<n>`; that port gives any local process full control, so it is for trusted machines only. A not-yet-open port is waited on for 2 seconds. Attached browsers are not owned: `close` leaves Chrome running. `--allowed-domains` refuses CDP and auto-connect.

`DevToolsActivePort` (first line the port, second the WebSocket path) is written by Chrome inside its user data dir. The engine's auto-connect looks there. Standard locations:

- macOS: `~/Library/Application Support/Google/Chrome/DevToolsActivePort` (Chrome Canary: `Google/Chrome Canary`, Brave: `BraveSoftware/Brave-Browser`, Chromium: `Chromium`).
- Linux: `~/.config/google-chrome/DevToolsActivePort` (Chromium: `~/.config/chromium/DevToolsActivePort`).

The macOS and Linux paths come from the engine binary's built-in search list and Chrome's own layout; they were not exercised against a live attached Chrome in this probe.

### Session and state export/import, encryption (for Task 9)

Yes, both exist and encryption is built in.

- Export: `agent-browser state save <path>` (cookies, localStorage, sessionStorage). Import: `agent-browser state load <path>` or the `--state <path>` flag / `AGENT_BROWSER_STATE` env at launch. Also `state list|show|rename|clear|clean`. These commands are in the `state` MCP profile, which Leo Bot does not load, so export and import run as CLI calls by Leo Bot itself, never as a bot tool.
- Encryption: set `AGENT_BROWSER_ENCRYPTION_KEY` to a 64-char hex key (AES-256-GCM). Verified: with the key set, `state save st.json` wrote `st.json.enc`, and the `--restore` auto-save wrote `<namespace>/state/sessions/<namespace>-<session>.json.enc`. Without the key the state file is plaintext session tokens.
- Location of auto-saved state: `~/.agent-browser/namespaces/<namespace>/state/sessions/` (no namespace: `~/.agent-browser/sessions/`). The engine home is the user home; no override variable was found for it (only `AGENT_BROWSER_SOCKET_DIR` for sockets). `AGENT_BROWSER_STATE_EXPIRE_DAYS` (default 30) deletes older states.
- A key lost means the saved state is unreadable. The key must be created once and kept by Leo Bot (for example in the bot's secret store), not generated per run.

### Other facts the relay may use

- `AGENT_BROWSER_ALLOWED_DOMAINS` / `--allowed-domains` / MCP `allowedDomains`: blocks navigation and page network traffic outside the list (also WebSocket, EventSource, beacons, and WebRTC). Needs a fresh launched browser: no profile, restore, state replay, CDP or auto-connect. So a domain allowlist and a saved login do not combine.
- `--action-policy <file>` / `AGENT_BROWSER_ACTION_POLICY`, `--confirm-actions <categories>` / `AGENT_BROWSER_CONFIRM_ACTIONS`, and the `confirm <id>` / `deny <id>` commands: an engine-side approval mechanism. Not used; the relay gates calls itself.
- `AGENT_BROWSER_CONTENT_BOUNDARIES` / `--content-boundaries` wraps page output in markers, and `AGENT_BROWSER_MAX_OUTPUT` / `--max-output` truncates it. Useful to mark page text as untrusted.
- `AGENT_BROWSER_DEFAULT_TIMEOUT` (default 25000 ms), `AGENT_BROWSER_DOWNLOAD_PATH`, `AGENT_BROWSER_SCREENSHOT_DIR`.
- `agent-browser install` downloads Chrome for Testing into `~/.agent-browser/browsers/chrome-<version>` (about 190 MB). `agent-browser doctor [--fix]` diagnoses an install.
- Other `agent-browser` daemons on the machine (any tool using the same engine) share `~/.agent-browser`; use a Leo Bot specific `--namespace` so sockets and states do not collide.

### Session daemons (probed on 0.41.0)

Probed in a scratch namespace on a 127.0.0.1 page, every process cleaned up after.
- A live session has its files in `~/.agent-browser/namespaces/<ns>/run/`: `<session>.sock` (a Unix socket that accepts while the daemon lives), `<session>.pid` (the daemon's pid), plus `.config`, `.engine`, `.stream`, `.target`, `.version`.
- The daemon is the engine binary with parent pid 1, the leader of its own process group. Its environment holds `AGENT_BROWSER_DAEMON=1`, `AGENT_BROWSER_SESSION=<session>` and `AGENT_BROWSER_NAMESPACE=<ns>`, readable with `ps -E` for the same user, so a pid can be checked as that session's daemon before anything is signalled.
- The headless Chrome is a child of the daemon and the leader of its own process group. Its renderers and helpers are in that group, and its crash handlers have their own groups and exit with it. Chrome's argv names a temporary `--user-data-dir`, not the session.
- SIGTERM to the daemon ends Chrome and the daemon, removes `.pid` and `.sock`, and saves the state (`<restore>-<session>.json.enc`).
- SIGKILL to the daemon leaves Chrome running with parent pid 1 and the `.pid` and `.sock` files behind; the stale socket then refuses connections. Signalling Chrome's process group ends Chrome and its crash handlers.
- Each workspace has a browser epoch: 6 random hex, stored in `team/browser.json` under `epochs.<ws>`. It is made when a turn mints a browser token, never by an incoming frame (a frame for a workspace with no epoch answers that the workspace was deleted and starts nothing). It is never equal to a live or retired one, and it is removed (retired) when the workspace is deleted. Every engine session name in that workspace carries it, so a recreated slug gets new session names, sockets and state files. It can never reach an old daemon or an old saved login, across restarts and repeated deletes.
- Workspace delete revokes the workspace's browser tokens first, then reads the old epoch, then removes the settings and the epoch. It then tidies the old epoch's sessions only (`Pool.tidy_workspace`), best effort:
  1. Close that epoch's children in parallel within one bound.
  2. Forget their saved state by exact name.
  3. For each daemon still alive, retry the engine close, then kill it (`kill_daemon`). The SIGTERM, the SIGKILL, and each Chrome group signal all go only to a pid checked again just before it as that session's. Chrome inherits the daemon's `AGENT_BROWSER_SESSION`, which is what that check reads (probed).
  4. Forget again, and remove the dead sessions' run files and their typed rows (`bot:<s>`, `my_chrome:<s>`, exact keys).

  The request kills the children and retires the epoch, then returns. Engine closes and the rest of the tidy run in a background thread.

  The sessions are settled in parallel within one bound. A daemon that lives on is retried in the background for 15 minutes; nothing waits for it, since no new name can meet it.
- Server start (`sweep_orphans`): every session in namespace `leobot`, from state files or run files, whose epoch this data root retired (a deleted workspace's, kept under `retired` in `team/browser.json`) is handled: confirmed daemons are settled, then state and run files are removed. A retired epoch with nothing left on disk is cleared, and so is one the delete tidy finished. A live epoch's name is never touched. Neither is an epoch this data root never made, since another Leo Bot data root on the same machine shares the namespace, nor a name from before epochs.
- Live check, scratch namespace, 127.0.0.1 page:
  1. A child was killed without its engine close, leaving its daemon. The workspace was forgotten, and the recreated slug's bot opened a page: a different session name and a different daemon.
  2. The tidy closed the old daemon and left the new one running.
  3. The start sweep with the workspace live touched nothing. Once its epoch was gone, the sweep closed the new daemon too.
  4. `pgrep` for engine and Chrome came back empty after.

### Install in Leo Bot

`core/browser_engine.py`: `resolve()` checks `LEOBOT_AGENT_BROWSER`, then `<data>/tools/agent-browser/0.41.0/node_modules/.bin/agent-browser`, then `PATH`. `install()` runs `npm install --prefix <that folder> agent-browser@0.41.0`, then `<binary> install`, with a 600 s timeout, and returns `status()`.

## The gate (`dashboard/browser_relay.py`)

- A send is a commit (click, select, check, uncheck, or `press` with Enter in the key) while text is typed. `press` passes otherwise only for Tab, Shift+Tab, arrows, Escape, PageUp, PageDown, Home and End: a printable key, a Control, Meta or Alt combo (paste included) or an unknown key name is refused. `fill` and `type` refuse text that is not a string or holds a line break or other control character.
- Typed text is kept per tab it went into (`Upstream.typed_key`: `bot:<session>` for a bot's own browser, so a new epoch never sees old text, `my_chrome:<session>` for one bot's pinned tab in the owner's Chrome) in `<data>/run/browser_typed.json`, so a restart does not clear it and another bot's open, new tab or tab reset never moves it. Calls on one real browser run one at a time (`Upstream.lock_key`, alias `browser_key`: `my_chrome` is one lock for every bot in the owner's Chrome). A corrupt file refuses every commit until the next typing writes a clean one.
- In the owner's Chrome the engine sees every tab, so for My Chrome bots the relay refuses `tab_list`, `tab_switch`, `tab_close` and `tab_new` (and leaves them out of `tools/list`): a bot stays in its own pinned tab and never sees the owner's tabs or another bot's. The bot browser keeps all tab tools. The upstream's own `tab_new` after `tab_gone` is not a bot call and still runs.
- On a listed site, before every commit the relay reads the page's text field values with its own fixed script (`FIELDS_SCRIPT`, see "Reading field values"); the bot never gets `eval`. A read that fails, times out or comes back in another shape refuses the click. Two or more filled fields refuse it too (the bot clears the others with `fill`). Exactly one filled field is the send text, so text typed onto a prefilled field is checked whole; when the bot typed something, that field must hold exactly it (whitespace aside), and typed text with no filled field in sight is refused (it went where the read cannot see). A field longer than 10000 characters is refused, and a value of only invisible characters (zero-width, word joiner, BOM, soft hyphen) counts as empty. No filled field and nothing typed: a plain click. Unlisted sites keep the typed rules only. At `act`, a click on a listed site with a filled field is a send and claims a cap slot, even when it only submits a prefilled form: a known cost of failing closed.
- `read` with a `url` is refused: the bot opens the page, then reads it. After every navigation, commit or call with a url the engine's own URL is checked; an unknown one fails closed (reply withheld, tab sent to about:blank).
- The daily cap counts done, failed and running sends of the page's site across every bot and workspace, and the count and the new running record are written under one lock. That lock (`approvals.LOCK`) is in-process: the gate runs only inside the dashboard server process; runners forward to it and never run their own gate.
- Residual risk: a send that needs no typing is not detected. A one-click canned reply, a "Like", "Connect" or "Accept" button, or a quick-reply chip goes out as a plain click, within the bot's level but without a plan or cap. Text in cross-origin frames and closed shadow roots is not seen by the field read. The script runs in the page, so a page that rewrites built-in DOM methods could hide its fields. An unseen composer plus a visible field prefilled from the URL can still be made to match the typed text; it needs both on one page and falls under the cross-origin frame and closed shadow root case. `type=hidden` inputs are not read on purpose (they hold CSRF tokens and the like), so a hidden form prefilled from the URL and sent by a plain click is not detected.

## The engine child (`dashboard/browser_upstream.py`)

- One engine child per (workspace, bot), kept in `POOL` and started on the first tool call: `<binary> mcp --tools core`, line-delimited JSON-RPC over stdin and stdout, stderr drained. At start it sends `initialize`, `notifications/initialized` and `tools/list`, so the relay knows every tool's inputs before a bot calls one. The bot's own `initialize` and `ping` are answered by the upstream; its notifications are not passed on.
- Engine config files. The engine reads `~/.agent-browser/config.json` and `./agent-browser.json` (working folder) by itself, and the environment only overrides keys that are set, so a file with `autoConnect`, `cdp`, `headed`, `proxy` or `plugins` would reach a bot. Leo Bot writes its own `<data>/run/agent-browser.json` (`{"headed": false, "autoConnect": false}`, mode 0600) before every start and points `AGENT_BROWSER_CONFIG` at it, which replaces both files; the child runs in the empty `<data>/run/engine-cwd` (a stray `agent-browser.json` there is deleted). Probed on 0.41.0 against a 127.0.0.1 page with a scratch HOME whose `config.json` held `autoConnect: true`, `headed: true` and a `cdp` port Leo Bot listened on: without `AGENT_BROWSER_CONFIG` the engine used them (`Cannot use --auto-connect and --cdp together`); with it, Chrome launched headless and nothing touched the `cdp` port.
- `AGENT_BROWSER_AUTO_CONNECT` is never set: on 0.41.0 setting it at all, even to `false` or `0`, turns auto-connect on (probed: `Auto-launch failed: No running Chrome instance found`). `AGENT_BROWSER_HEADED=false` is parsed as off (probed: Chrome ran with `--headless`; `true` showed a window).
- The child's environment is an allowlist of the server's variables (PATH, HOME, user, shell, temp and locale) plus Leo Bot's own: `AGENT_BROWSER_CONFIG`, `AGENT_BROWSER_HEADED=false`, `AGENT_BROWSER_SESSION`, `AGENT_BROWSER_NAMESPACE=leobot`, `AGENT_BROWSER_ENCRYPTION_KEY`, `AGENT_BROWSER_IDLE_TIMEOUT_MS=600000`, `AGENT_BROWSER_STATE_EXPIRE_DAYS=365`. No other `AGENT_BROWSER_*` setting of the server reaches it.
- Bot browser: session `<ws>-<epoch>--<bot>` with `AGENT_BROWSER_RESTORE` set to that session name (the session name as the restore key). The engine saves state as `<restore key>-<session>.json.enc` under `~/.agent-browser/namespaces/leobot/state/sessions/`. Probed with two bots on a 127.0.0.1 page: localStorage set in one was not visible in the other, and came back in the first after both were closed and started again. Closing runs `<binary> --session <s> --namespace leobot close`, which writes the saved state.
- Session names: a workspace or bot id that is not lowercase letters, digits and single dashes (or that looks like a hashed part) becomes `h` + 8 hex of its sha256, so `<ws>-<epoch>--<bot>` names one pair only. A session name over 24 characters becomes `x` + the epoch + 10 hex of its sha256 (`c` appended for My Chrome), so its epoch stays readable; the name never depends on the home folder. The engine's socket path (`<home>/.agent-browser/namespaces/<namespace>/<session>` plus 37 bytes, home taken as its real path without a trailing slash) must fit 103 bytes; when it does not, the browser refuses to start and says the home folder path is too long. With the longest name (24) and the `leobot` namespace the home path may be 36 characters.
- My Chrome: session `<ws>-<epoch>--<bot>--chrome` (never the bot browser's daemon), `AGENT_BROWSER_CDP` set to the WebSocket URL from Chrome's `DevToolsActivePort`, `AGENT_BROWSER_PIN_TAB=1` so each bot keeps its own tab. The file is trusted only when its port accepts a connection on 127.0.0.1 (Chrome leaves the file behind when it quits). When `/json/version` answers 200 with a `webSocketDebuggerUrl`, its path must be the file's (another Chrome run reuses the port); any other answer (another status, a reset, not HTTP) falls back to the connection check, since Chrome's built-in remote debugging switch may not serve it. It is checked on every call: Chrome gone ends the session (child stopped, engine `close`, which leaves an attached Chrome running) and answers that Chrome is not open with remote debugging on; a new port or browser id ends the session and starts a new one. The engine keeps a session's pinned tab across restarts of the session, so after Chrome restarts (or the owner closes that tab) a call answers `tab_gone`; the upstream then opens a new empty tab with `tab_new`, performs nothing else, and tells the bot to open the page again.
- A call gets 120 seconds. A timeout or a crash stops the child (an exit counts even when a process it started keeps its output open) and is answered once as an error; the next call starts a fresh child. Stopping a child never waits on its stderr (measured on 0.41.0: the daemon does not keep the pipe, stop took 0.01 s).
- The pool lock only guards its table. An idle child (10 minutes) is closed by a sweeper thread every minute, under that child's own lock, and a call in flight keeps it open. A mode change closes the old browser in the caller's turn; the new one waits for that close before it starts. Server stop refuses new browsers, kills every child, then runs the engine closes in parallel within 15 seconds.
- The key: `<data>/run/browser-engine-key`, 64 hex characters, created once with mode 0600. **Back up `<data>/run` with the data**: without this key every saved login is unreadable and each site must be logged in again. A key that cannot be read stops the browser from starting; it is never replaced.
- Risk, not tested live: Chrome 136 and later ignore `--remote-debugging-port` on the default profile. Attaching to the owner's everyday Chrome needs Chrome's own remote debugging switch (`chrome://inspect`, "Remote debugging") or a separate user data dir started with the flag. Settings, Browser must say which. Whether the built-in switch serves `/json/version` (the check above) is untested.
- Live attach probe (round 2), Chrome for Testing started headless with `--remote-debugging-port=0` on a scratch profile, output to /dev/null, pages on 127.0.0.1 only: through the upstream, open, get_url, a second open and get_title all worked (three runs from a shell, one from a script). Quit: the next call answered that Chrome is off. Restart on a new port: the first call answered `tab_gone`, the upstream opened a new tab, the next open worked; close left that Chrome running. The earlier `Failed to read: Resource temporarily unavailable` and `page_wedged` came from the probe itself: that Chrome had been started with the scratch HOME the engine used. Started with the real home it never wedged.
