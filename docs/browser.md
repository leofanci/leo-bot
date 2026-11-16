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

- `READ_TOOLS`: `read`, `snapshot`, `screenshot`, `scroll`, `get_text`, `get_url`, `get_title`, `wait_ms`, `wait_for_selector`, `wait_for_text`, `wait_for_load`. They look at the page or wait; none submits anything. `scroll` can trigger lazy loading but sends nothing the user typed. `screenshot` writes an image file under the engine's folder; the relay must return the image and not accept a caller `path` or `screenshotDir`. `read` can fetch a URL that is not the current page, so the relay refuses `read` with a `url` (the bot opens the page, then reads it).
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
- `data.refs` keys are the ref without `@`: `{"e1": {"name": "Go next", "role": "link"}, "e2": {"name": "Send", "role": "button"}, ...}`. `click` takes `@e1` and also a bare `e1` as that ref. A ref still clicks after a navigation (no error on the next page). An element with `role="link"` (a `div`) is listed as `link`. Probed on 0.41.0 against a 127.0.0.1 page with two links, a button, a text box and a `div role=link`.

### Reading field values

Probed on 0.41.0 against a 127.0.0.1 fixture page (prefilled input, email, password, hidden, checkbox, empty search, multi-line textarea, nested contenteditable, an open shadow root with an input and a `plaintext-only` element, a same-origin iframe). `agent_browser_eval` takes `script` (required) and `timeoutMs`.

- Success: `isError: false`; the returned value, serialized by value (objects, arrays, strings, numbers; a Promise is awaited), is `structuredContent.response.data.result`, next to `data.origin` (the page URL). `content[0].text` holds the same value as pretty JSON.
- Script error: `isError: true`, `content[0].text` and `response.error` start with `Evaluation error:`, `response.success: false`, `data: {}`.
- The relay's `FIELDS_SCRIPT` returns `{fields: [...]}`. On the fixture it read `["pre filled", <the email value>, "line one\nline two", "Rich bold\ninner", "in shadow", "plain only", "in frame"]`: password, hidden, checkbox and empty fields left out, a nested contenteditable read once through its root, the shadow root and the iframe walked. A live run of the relay's own reader through the engine returned the same list, and after `type` into a prefilled textarea it read the combined value.

### Per-call fields (relay must control them)

Every core tool also accepts these inputs, so a bot could choose them unless the relay strips or overwrites them: `session`, `namespace`, `restore`, `restoreSave`, `restoreCheckUrl`, `restoreCheckText`, `restoreCheckFn`, `allowedDomains`, `extraArgs`, `caCert`, `clearCaCert`, `useSystemCa`, `idleTimeout`, `timeoutMs`. `extraArgs` is a raw argv list; the engine rejects the flags that change network, file, policy or host access (`--allowed-domains`, `--executable-path`, `--args`, `--config`), but the relay should drop it entirely. `open` also takes `headed`. `screenshot` takes `path` and `screenshotDir`. `restoreCheckFn` is script. The relay drops all of these per-call fields except `timeoutMs` (held to 1 to 120 seconds), and every key the tool's own engine schema does not declare. Session, namespace and restore come from the engine child's environment (`AGENT_BROWSER_SESSION`, `AGENT_BROWSER_NAMESPACE`, `AGENT_BROWSER_RESTORE`, see "The engine child"); `allowedDomains` is not used (see "Limits").

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

Probed again for moving a login (0.41.0, scratch HOME, scratch namespace, a 127.0.0.1 page that sets a cookie and localStorage):

- The daemon encrypts every save with its own launch key: `state save` from a CLI with another key, or none, still wrote `.json.enc` with the daemon's key. `state load` decrypts with the daemon's key too (another key: `Decryption failed: aead::Error`; a keyless daemon: `Encrypted state file requires AGENT_BROWSER_ENCRYPTION_KEY`). So the engine cannot hand out a keyed state in plain text.
- `cookies get` (and `--json cookies get`) lists the current page's cookies only: on `about:blank` it returned none for a session whose state held a cookie. `state show <name>` failed with `No such file or directory` for files `state list` showed, in the namespace and outside it.
- File layout, checked by decrypting real engine files with a standard AES-GCM and the tag check: the AES-256 key is the SHA-256 of the 64-hex key string (its ASCII text), then a 12-byte nonce, the ciphertext and the 16-byte tag (28 bytes over the plain JSON: 464 plain, 492 sealed). A file Leo Bot sealed the same way was read by `state load`, and a session restored from such a file showed the logged-in page.
- Plain state: `{"cookies": [{name, value, domain, path, expires, size, httpOnly, secure, session, sameSite?}], "origins": [{origin, localStorage: [{name, value}], sessionStorage: [...]}]}`.
- A save keeps localStorage only for the origins of the pages open then: a restored session that closed on `about:blank` saved its cookies but dropped the origin's localStorage (492 to 304 bytes). Cookies always stay.

### Proxy (probed on 0.41.0)

- `--proxy <url>` / `AGENT_BROWSER_PROXY` (fallbacks `HTTP_PROXY`, `HTTPS_PROXY`, `ALL_PROXY`), with user and password in the URL. `--proxy-bypass <hosts>` / `AGENT_BROWSER_PROXY_BYPASS` (fallback `NO_PROXY`) sends listed hosts straight out. Config file key `proxy`.
- One proxy per browser: no per-host proxy option. A bypass list cannot say "only this host through the proxy".
- Live: with `AGENT_BROWSER_PROXY` set to Leo Bot's router on 127.0.0.1, with user `leobot` and a token in the URL, the engine answered the router's `407` with that user and token by itself. A page on a made-up host (`acme.test`) loaded through the router and an authenticated test proxy, over plain HTTP (absolute-form `GET`) and over HTTPS (`CONNECT`, with `--ignore-https-errors` for the test certificate). Another made-up host went straight out and failed to resolve, never reaching the site's proxy.

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
- Each workspace has a browser epoch: 6 random hex, stored in `team/browser.json` under `epochs.<ws>`. It is made under `browser.LOCK` while the workspace exists (`browser.with_epoch`): when a turn mints a browser token, when the owner opens a first login, and when a moved login is received (`import_login`). An incoming bot frame never makes one (a frame for a workspace with no epoch answers that the workspace was deleted and starts nothing). It is never equal to a live or retired one, and it is removed (retired) when the workspace is deleted. Every engine session name in that workspace carries it, so a recreated slug gets new session names, sockets and state files. It can never reach an old daemon or an old saved login, across restarts and repeated deletes.
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
- In the owner's Chrome the engine sees every tab, so for My Chrome bots the relay refuses `tab_list`, `tab_switch`, `tab_close` and `tab_new` (and leaves them out of `tools/list`): a bot stays in its own pinned tab and never sees the owner's tabs or another bot's. `click` declares `newTab` on 0.41.0, which opens the link in a new tab and moves the bot there (checked live), so in My Chrome a click with `newTab` set is refused and `newTab` is left out of click's inputs. The bot browser keeps all tab tools and `newTab`. The upstream's own `tab_new` after `tab_gone` is not a bot call and still runs.
- Opening a page or a new tab sets the typed text aside, except `open` of the same address with a `#fragment` (scheme, host, path and query alike): that is a move inside the same document, whose fields keep their text, so the typed text stays typed. An `open` of the same address without a fragment is a fresh page.
- On a listed site, and on any site while typed text is set aside, before every commit the relay reads the page's text field values with its own fixed script (`FIELDS_SCRIPT`, see "Reading field values"); the bot never gets `eval`. A read that fails, times out or comes back in another shape refuses the click. Two or more filled fields refuse it too (the bot clears the others with `fill`). Exactly one filled field is the send text (a page may keep text in a field after a move the gate saw as leaving), so text typed onto a prefilled field is checked whole; when the bot typed something, that field must hold exactly it (whitespace aside), and typed text with no filled field in sight is refused (it went where the read cannot see). A field longer than 10000 characters is refused, and a value of only invisible characters (zero-width, word joiner, BOM, soft hyphen) counts as empty. No filled field and nothing typed: a plain click. With a filled field and nothing typed, a refused send says so in its own line (a field on the page holds text; plan it, or clear the field with `fill` and an empty text). Unlisted sites with nothing set aside keep the typed rules only. At `act`, a click on a listed site with a filled field is a send and claims a cap slot, even when it only submits a prefilled form or follows a link: a known cost of failing closed.
- `read` with a `url` is refused: the bot opens the page, then reads it. After every navigation, commit or call with a url the engine's own URL is checked; an unknown one fails closed (reply withheld, tab sent to about:blank). Before every call but navigation the engine's URL is checked too, so a late redirect (a page that moves itself after a click) onto a site bound to the other mode, or a page the engine cannot name, is left before anything reads or types on it.
- Each tool call keeps only the keys its engine schema declares. A fresh upstream learns the schemas before its first call passes: from the engine's last `tools/list` (kept in memory and in `<data>/run/browser-tools.json`, or for a lent Chrome the runner's last list kept on the server), else one `tools/list` now. When none can be had, the call is refused.
- At Reads only a click passes only on a link by its snapshot ref (see "Levels"). The engine's `data.refs` keys are the ref without `@` (`e1`), and a bare `e1` selector also clicks that ref (probed on 0.41.0), so only the `@e1` form is accepted and looked up. A ref stays clickable after a navigation (the probe clicked `@e1` again on the next page), so the relay forgets the refs at every navigation, commit or call with a url, and when the engine's URL is no longer the one the snapshot was taken on (a script or a timer moved the page). An element can claim the role itself (`role="link"` on a `div` shows as `link`), so a page can make a script button look like a link: such a click runs within Reads only, with the field read before it.
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
- Chrome 136 and later ignore `--remote-debugging-port` on the default profile. Attaching to the owner's everyday Chrome needs Chrome's own remote debugging switch (`chrome://inspect`, "Remote debugging") or a separate user data dir started with the flag. Settings, Browser says both (the My Chrome check). Chrome is found only through `DevToolsActivePort` in the standard user data dirs under the home folder (`CHROME_DIRS`); there is no override. A Chrome started with the flag answers `/json/version` (checked live). Whether the built-in switch serves it is still untested (no real Chrome profile was used); when it does not, the connection check is the fallback, and that fallback was checked live against a port that does not speak HTTP.
- First login (Settings, Browser, "Log in", owner only, from this machine, never on a server): `POST /api/browser/login {ws, bot, url}` takes http(s) URLs only and is refused when the bot is set to My Chrome or the URL's site opens in My Chrome. The workspace's epoch is made under `browser.LOCK` only while the workspace exists (`browser.with_epoch`). The pool marks the bot's bot-mode session as the owner's (`Pool.begin_login`), kills and closes that bot's child, settles any daemon left of the session, then runs `<binary> --session <s> --namespace leobot open <url>` with the bot browser's own environment (`_engine_env`: Leo Bot's config file, restore key = session name, encryption key) with only `AGENT_BROWSER_HEADED=true` and `AGENT_BROWSER_IDLE_TIMEOUT_MS=1800000` (30 minutes, `LOGIN_SECONDS`) changed, so a person logging in, who sends the engine no commands, is not cut off by the 10-minute idle timeout. Its output goes to a file, not a pipe, since the daemon it starts may keep it. Until the session's daemon is gone (Done, `POST /api/browser/login/done`, which runs the engine `close` and saves the logins; the engine's own idle timeout; or 30 minutes, then closed here) every call of that bot in bot mode answers that the owner is logging in, and no child starts: the bot never sees the login page. Each login has its own token: its watcher and Done end only that login, and Done stops its watcher, so a new login of the same session is never ended or closed by an old one. A close that fails past 30 minutes is retried with a growing wait (up to a minute) and logged. Server start also closes (engine close, which saves the logins, then the confirmed-pid kill) any daemon of this data root's live epochs that no child or login owns, so a crash mid-login never leaves a headed window a bot could attach to; other roots' names are never touched. In Settings, adding a site that is already listed is refused (it would wipe its row); removing one, a default included, leaves it with no daily limit, and the confirm says so. Live check results: see "Live check" below. On macOS closing the login window does not end the daemon: Chrome keeps running with no window, so the login ends only with Done, the idle timeout or the 30 minutes. Settings, Browser tells the owner to press Done.
- While a login holds the session, `tools/list` still answers with the engine's last known tool list (kept in memory and in `<data>/run/browser-tools.json`, so also right after a restart), so the bot's first call gets the logging-in line and can say why, instead of seeing no browser.
- Live attach probe (round 2), Chrome for Testing started headless with `--remote-debugging-port=0` on a scratch profile, output to /dev/null, pages on 127.0.0.1 only: through the upstream, open, get_url, a second open and get_title all worked (three runs from a shell, one from a script). Quit: the next call answered that Chrome is off. Restart on a new port: the first call answered `tab_gone`, the upstream opened a new tab, the next open worked; close left that Chrome running. The earlier `Failed to read: Resource temporarily unavailable` and `page_wedged` came from the probe itself: that Chrome had been started with the scratch HOME the engine used. Started with the real home it never wedged.

## Moving a bot's login to a server (`export_login`, `import_login`)

- The owner logs a bot in on their computer (Log in, Done), then presses **Send to server** on that bot in Settings, Browser. `POST /api/browser/login/send {ws, bot}`: owner only, from that machine only, never on a server.
- Export (`browser_upstream.export_login`): the bot's bot-mode session is held (`Pool.hold`: every call of that bot in bot mode answers that its logins are being moved). It is refused while that bot is in a turn (its turn's browser token is live, so between calls too: "This bot is in a turn right now, so its browser logins are not moved..."), while a call is in flight, or while a login window is open; the bot's browser is then left as it is. Otherwise its child is closed (the daemon saves), any daemon left settled. The runner connection is checked first, so with no runner nothing is held or closed. Leo Bot then opens its own state file `<session>-<session>.json.enc` with its own key (`core/aes_gcm.py`, standard library only, tag checked). Only the engine's own cookie and origin fields are kept, and nothing of a site bound to My Chrome. The plain state stays in memory.
- Transport: through the runner (`dashboard/runner.py`). When a runner connects it saves the server address and its token file's path (not the token) in `~/.leobot-runner-settings.json`. `runner.send_login` posts the state to `POST /api/runner/browser-login` on the runner's token, with the runner's own client: https unless the server is local, no redirects. No runner ever connected from that computer: "This computer has no runner set up for a server yet. Start the runner once with --server and --token-file, then send again."
- Import (`browser_upstream.import_login`, on the server): only the owner's own runner token, the same workspace slug and bot id, a bot not set to My Chrome there, a state under 1 MB. Nothing of a site bound to My Chrome on the server is kept. The bot's session is held and closed the same way, with the same refusals (a turn of that bot on the server, a call in flight, an open login window). The state is sealed with the server's own key straight into the server bot's state file (0600, written to a temp file in the same folder, then renamed): no plain copy is written on either side. It replaces that bot's saved logins there; the old file stays until the new one is in place, and other files of that session are removed only after. A malformed or too large login answers 400, a refusal 409.
- Live check (2026-11-16, one machine, 127.0.0.1 only, scratch data and engine homes): an owner Leo Bot (local) and a server Leo Bot (`LEOBOT_DEPLOYMENT=server`), each with workspace `demo`, bot `research` at Reads only in Bot browser, and site `acme.test` with its own proxy (two copies of `tests/fixtures/auth_proxy.py`, each with its own password, both sending to a `python3 -m http.server` fixture page that sets a cookie and shows "logged in as demo"). A real runner (`--browser-only`) on the owner's side. Log in (headed window) and Done on the owner's side: the page loaded through the local router and proxy. Send to server: `{"cookies": 1, "skipped": []}`, and the server bot's state file appeared, sealed with the server's key. A Claude Code turn on the server ("open the acme.test login page and tell me the text of #who") ran open and get_text and answered "logged in as demo", with the server's proxy logging the requests. Neither password appeared in any log, the engine's or Chrome's environment, or an API answer; only in each side's `team/browser.json` (0600).
- What does not move: localStorage of an origin that was not open when the bot's browser last saved (see the probe above). A site whose login lives only there must be logged in again by a run that ends on that site.

## Proxy per site (`dashboard/browser_proxy.py`)

- Settings, Browser, a site's row: **Proxy**: an http address with host and port, and `user:password@` before the host when the proxy needs it (http proxies only, no SOCKS). Refused on a site bound to My Chrome: only the bot browser uses a proxy. After saving, the row shows it masked (any user and password replaced by `***`, since a user alone can be a token); the password is never sent back by the API. Remove proxy takes it off.
- Only the listed site's own host and its subdomains go through its proxy. Other hosts a site's pages load (a CDN or an API on another domain) go straight out; list each of them as its own site, with the same proxy, to send them through it.
- The engine has one proxy per browser, so while any site has a proxy every bot-mode engine (and the login window) gets Leo Bot's own router on 127.0.0.1 as its proxy, with a random user token per start. The router sends a listed site with a proxy through that proxy (CONNECT for https, the request itself for http, with the proxy's user and password) and everything else straight out. The real proxy address and password never reach the engine, Chrome or their environment. A proxy that does not answer fails the page ("The proxy for <site> did not answer."); it never falls back to going straight out.
- A plain-HTTP request is one request per browser connection: the router reads that request and its body (Content-Length; a chunked request body is refused; `Expect: 100-continue` is answered with `100 Continue` before the body is read, and not sent on), sends it on with `Connection: close`, relays exactly one answer with its head rewritten to `Connection: close`, and closes. A second request Chrome puts on the same connection (it pools proxy connections across hosts) is never read, so it cannot reach the first host's server or proxy.
- A CONNECT that fails after the router answered `200` closes the tunnel; it never writes a `502` inside it.
- The sites and proxies are checked for every connection and every engine start, and re-read from `team/browser.json` (in one read) only when that file changed: its inode, size, mtime or ctime moved, or this process wrote it (a counter in `core/browser.py`). A browser started before the first proxy was set, or after the last one was removed, is closed before its next call and started again.
- Chrome sends 127.0.0.1 and localhost straight out by itself. WebRTC is not routed through the proxy.

## Setup guide

### Install the engine

Settings, Browser, "Install" runs `npm install --prefix <data>/tools/agent-browser/0.41.0 agent-browser@0.41.0`, then `agent-browser install` (Chrome for Testing into `~/.agent-browser/browsers`, about 190 MB). It needs Node and npm. The same screen shows the exact command when the install cannot run. `LEOBOT_AGENT_BROWSER` can point at a binary instead.

### Levels (Team, a bot's Details, Browser)

- **Off** (the default): no browser tools.
- **Reads only**: open, read, snapshot, screenshot, scroll, wait, history, tabs (bot browser only), and a click on a link. A click passes only with an `@ref` selector that the last snapshot's `data.refs` gives the role `link`; no snapshot yet, an unknown ref, another role or a CSS selector is refused. Every navigation or click makes the refs stale, so the bot takes a new snapshot first. Typing, `press`, `select`, `check` and `uncheck` are refused and left out of `tools/list`. On a listed site the field read still runs, and a link click on a page with a filled field is refused.
- **Read + write**: may type. A send (a click, select, check, uncheck or Enter after typing) runs only when it matches an approved send plan item.
- **Acts on its own**: sends run at once, within the daily limit of listed sites (other sites have none), and each is logged in Approvals.

A Report only job never sends. Shared threads (an invited person can see them) never get the browser.

### Send plans (Read + write)

1. The bot opens the page and calls `leobot_plan_sends` with `[{url, text}]` (up to 20 items). Nothing is typed or sent by that call.
2. The plan waits in Approvals as one batch (Approve all or No). The text cannot be edited there; say No and ask for new text.
3. After Approve, the bot's next turn types the exact text on that page and clicks Send. The send matches when the page has the same host, path and query, and the text is the same after whitespace is collapsed.
4. An approved item can be used for 7 days. It is used up by one send.
5. Until then Approvals lists it under "Approved, not sent yet", with **Withdraw** (`POST /api/approvals/<id>/withdraw`, owner only): approved becomes rejected under the approvals lock, so the bot can no longer send it.

### Sites (Settings, Browser)

- A site is a host. It matches itself and its subdomains (`www.example.com` belongs to `example.com`). A host needs a dot, so `localhost` cannot be listed; an IP address such as `127.0.0.1` can.
- Defaults: `linkedin.com` 20 a day, `instagram.com` 10 a day, no mode bound.
- A daily limit counts every send of that site in the last 24 hours from when it started: planned and on-its-own sends, done, failed and running, by every bot in every workspace. Deleting a workspace keeps its sends of the last day (never listed again), so recreating the slug does not reset a limit.
- A site name is stored the way the gate reads a page's host (NFKC, then IDNA ascii), so a site typed with Unicode letters matches its pages.
- A site bound to a mode opens only in that mode. A bot in the other mode is refused, so one account is never signed in from two places.
- On a listed site every commit first reads the page's text fields (see "The gate"). Unlisted sites keep the typed-text rules, plus the field read while typed text is set aside.

### Bot browser mode

Each bot gets its own headless Chrome with its own saved logins (encrypted with the key in `<data>/run/browser-engine-key`). To save a login: Settings, Browser, "Log in" on that bot, with the address of the site's login page. A visible window opens on this Mac. Log in there, then press **Done** in Settings. Closing the window is not enough on a Mac. While the window is open, that bot's browser answers that the owner is logging in.

### My Chrome mode (owner steps)

The bot uses its own pinned tab in the owner's everyday Chrome, with the owner's logins. Leo Bot and Chrome must run on the same Mac.

1. In Chrome, open `chrome://inspect/#remote-debugging` and turn on remote debugging. (Chrome 136 and later ignore `--remote-debugging-port` on the default profile. A Chrome started with that flag and a separate `--user-data-dir` is found only when that dir is one of the standard ones under the home folder.)
2. Settings, Browser shows "Chrome: found" when Leo Bot can reach it.
3. Set the bot to My Chrome in Team. Each bot keeps to its own tab. Tab tools and `newTab` clicks are refused, so other tabs stay private.
4. When Chrome is closed or debugging is off, every call answers: "Chrome is not open with remote debugging on, so this bot cannot use your Chrome. Open Chrome and turn on remote debugging (Settings, Browser shows how), then ask again."

Remote debugging gives any local program full control of that Chrome. Turn it on only on a machine you trust.

### My Chrome on a server: the runner lends Chrome

On a server Leo Bot (`LEOBOT_DEPLOYMENT=server`), a My Chrome bot uses its own tab of the owner's Chrome on the owner's computer, through that person's runner (`dashboard/runner.py`). The server never looks for a Chrome of its own.

- **Lend my Chrome** is a setting of the runner, saved on that computer only (`~/.leobot-runner-settings.json`). It is off until the person at that computer runs `python3 -m dashboard.runner --lend-chrome on` (`off` turns it back off; the runner reads it again for every call). The server cannot turn it on.
- `--browser-only` lends Chrome and takes no turns. A runner that takes turns makes the owner's chats run on that computer, and runner turns never get the browser. So to use My Chrome from chats on a server, run the runner with `--browser-only`. Scheduled jobs run on the server's engine either way.
- The gate stays on the server: levels, send plans, daily caps, site modes and the field read. Each call the gate passes becomes one `browser` job in the runner hub (`core/runners.py`) for the turn's own person. A frame for one person's turn never reaches another person's runner. The answer comes back on `POST /api/runner/reply/<job>`, and only the runner holding that job may send it. An answer over `MAX_TURN_CHARS` is dropped.
- The runner checks each call again before its Chrome sees it (`browser_lent.runner_check`). It passes only the engine tools of the four sets, never a tab tool or a `newTab` click. It drops the engine-wide inputs (and undeclared ones), opens only http(s) addresses or `about:blank`, and allows `eval` only as the relay's fixed field read (`FIELDS_SCRIPT`). A server that is not to be trusted still reaches no eval, file or cookie tool through the lend.
- No runner of that person online: "Your computer is offline, so this bot cannot use your Chrome. Open Leo Bot on your computer, then ask again." Nothing else is tried: not the bot browser, not a Chrome on the server.
- Runner online with Lend my Chrome off: "Lend my Chrome is off on your computer, so this bot cannot use your Chrome. Run the runner there with --lend-chrome on, then ask again."
- A thread invited people can see has no browser: "This thread is shared with invited people, so it has no browser. This call was not performed."
- Settings, Browser on a server shows whether a computer lends its Chrome now, in place of the local Chrome check.
- One person with several lending computers: a bot sticks to one. When another one takes over, the typed text is set aside, because that Chrome has nothing typed.
- The lent tabs' engine sessions close when the runner stops (Ctrl-C or a plain kill), after 10 idle minutes, and at once when their workspace is deleted on the server: the server sends each lending runner of that person, and on a server every lending runner of the owner (also after a restart, when it keeps no lent tab for them), a `close` note for the workspace and its epoch, which needs no answer and is dropped if no runner takes it within 5 minutes. Chrome keeps running.
- A call the runner cannot read gets an error reply right away ("Your computer could not read this browser call, so it did not run."), so the server never waits out the 300 seconds for it.
- A runner with Lend my Chrome on that also takes turns says at start that chats sent there run on that computer without the browser, and to use `--browser-only` for My Chrome in chats.

- Lending Chrome gives the server read and act access, at the bot's level, to every site the owner is logged into in that Chrome, within that bot's own tab. The gate on the server and the runner's own check still apply to every call.

Live check (2026-11-15, one machine, scratch dirs, 127.0.0.1 only): a server-mode Leo Bot (scratch `LEOBOT_HOME`, `demo` workspace, `research` bot at Reads only in My Chrome) and a real runner (`--browser-only`, scratch settings and browser folder). A throwaway Chrome for Testing (`--headless=new --remote-debugging-port=0`, scratch `--user-data-dir`) stood in for the owner's Chrome. A test wrapper pointed the runner's Chrome lookup at that folder.

1. With Lend my Chrome on, Settings, Browser showed a lending computer. Asked for the fixture page's title and button text, the bot ran open, get_title and snapshot and answered: "Browser form fixture", "Send". The engine child ran under the runner process. The server started none, and the page was open in the throwaway Chrome.
2. After `--lend-chrome off` the bot quoted the Lend my Chrome line, and Settings showed no lending computer.
3. With the runner stopped, the bot quoted the offline line. This held both right after the stop (the job was not taken within its lease) and after the runner's 45 seconds online window had passed. The stop closed the runner's engine session.

### Limits and what is not supported

- A server Leo Bot reaches My Chrome only through the owner's runner (see "My Chrome on a server"). Logging a bot browser in is refused on a server: log in on your computer, then Send to server.
- A send that needs no typing (a one-click canned reply, Like, Connect, Accept, a quick-reply chip) is not detected. It runs as a plain click within the bot's level.
- Text in cross-origin frames and closed shadow roots is not seen by the field read.
- No file upload, download, PDF, cookie, storage, network or `eval` tool reaches a bot.
- Links with `target=_blank` and `window.open` in My Chrome open new tabs in the owner's Chrome. The bot stays on its own tab, and those tabs stay open until the owner closes them.
- A domain allowlist (`allowedDomains`) is not used: the engine cannot combine it with saved logins or an attached Chrome.
- A shell beside the browser (`dashboard/shell_rules.py`). The relay is the only way a bot reaches the browser, so a run with a browser (level above Off) runs every shell command in the macOS sandbox from its first command, outside content or not. The sandbox denies every localhost port from the first command (a Chrome debug port, the bot browser's own CDP port, any local service; `ssh localhost` too), the engine folder `~/.agent-browser` (read, write and its session sockets), `<data>/run` (the engine key, typed text, run state) and writes to `<data>/team` (approvals, browser settings, the sandbox profiles). Checked live by tests: 127.0.0.1 and ::1 ports, a socket in a walled folder, the key file, `team/approvals.json` and the profile folder were refused; a write in the edit folder was not.
  - Every sandbox profile Leo Bot writes to a file also denies writes to that file's folder (`<data>/team/sandbox`), so a command cannot swap the profile of the next one.
  - The one command that runs outside the sandbox, a plain `python3 -m dashboard.osctl ...`, always runs as Leo Bot's own osctl: this server's Python (`sys.executable`) with `-I` (no current folder, `PYTHONPATH` or user site on `sys.path`), the repo put on `sys.path` explicitly, from the repo folder. A `dashboard/osctl.py` a bot writes in its edit folder is never loaded (checked by a test). `-P` would need Python 3.11; Leo Bot supports 3.10. Any other form (another `python3`, flags, a prefix) is not exempt.
  - Where Leo Bot's sandbox cannot wrap the shell (Codex keeps its own sandbox; a system without `sandbox-exec`), a browser that can send (Read + write, Acts on its own) and a shell level (Full access, Approve for me) do not run together: the turn answers with a line naming both settings. At Reads only the pair runs, and such a shell is not walled off from the browser. The relay is told about such a shell, so if the owner raises the level mid-turn, every send of that turn is refused with the same line.

## Live check (2026-11-15)

Run against a fixture page only (`tests/fixtures/browser_form.html`: a Message box, a Send button that shows `sent: <text>`, a `target=_blank` link and a `window.open` button), served by `python3 -m http.server` on 127.0.0.1. The dashboard ran on its own port with a throwaway `LEOBOT_HOME` and a `demo` workspace. Turns went through `POST /api/ask`; approvals went through `POST /api/approvals/<id>/approve`, the call behind the Approve button. No real site, account or Chrome profile was used. Every Chrome, daemon and server started was stopped after.

Versions: macOS 27.0.1 (arm64), Node 22.23.2, agent-browser 0.41.0, Chrome for Testing 157.0.8103.78, Claude Code 2.1.325, Codex CLI 0.167.2.

1. **Claude Code, Read + write, Bot browser.** Asked to send "hello from demo": one plan card (open, snapshot, `leobot_plan_sends`; nothing typed). After Approve, the next turn ran fill, click and read: the page showed `sent: hello from demo`, and the record ended `done` with "Sent from the browser." The Approvals screen now lists such sends under "Sent from the browser".
2. **Acts on its own, site `127.0.0.1` at daily 1.** The first send went out (`sent: act one`). The second was refused: "127.0.0.1 reached today's limit of 1 sends. Nothing was sent. Try again tomorrow or ask the owner to raise the limit in Settings, Browser." In a data root where step 1 had already sent on that site, the first send was refused the same way: planned sends count toward the limit.
3. **My Chrome.** A separate Chrome (scratch `--user-data-dir`, `--remote-debugging-port=0`) stood in for the owner's Chrome. A test wrapper pointed Leo Bot's Chrome lookup at that folder's `DevToolsActivePort` (`CHROME_DIRS` holds only the standard dirs). The bot opened and read the fixture in its pinned tab. With that Chrome closed, the next call answered the CHROME_OFF line quoted under "My Chrome mode".
4. **Codex, Read + write, Bot browser.** Same as step 1: one plan card, and after Approve, `sent: hello from demo`. The send turn ran as `codex exec resume <id>` with `mcp_servers.browser.url` on its command line, so a resumed Codex turn keeps the browser.

Settled risks:

- `/json/version`: answered with `webSocketDebuggerUrl` by a flag-started Chrome. Chrome's built-in remote debugging switch is still untested. The connection fallback returned the URL for a port that does not speak HTTP, and None once the port closed.
- Pinned tab: a `target=_blank` link and `window.open` opened new tabs, but the bot's page stayed the same. `click` with `newTab: true` moved the bot to the new tab. It is now refused in My Chrome (`CHROME_NEW_TAB`), and that was checked live. On 0.41.0 the core tools that declare tab fields are `tab_switch` and `tab_close` (`tab`), plus `click` (`newTab`). None declares `targetId`.
- Headed login: `POST /api/browser/login` returned in about 4 seconds with a visible window and the daemon kept (no `--headless`, idle timeout 30 minutes). Closing the window left Chrome and the daemon running. Done closed both and saved the encrypted state. Bot calls during the login answered the logging-in line.
- Codex resume: carries the browser MCP config (step 4).
- `127.0.0.1` as a site: accepted by `set_site`. No code change was needed.
