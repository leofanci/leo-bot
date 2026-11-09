# Leo Bot — AI Integration Engine

**Date:** 2026-06-17
**Status:** Approved design, ready for implementation plan

## Problem

The dashboard's "Consultant" chat spawns a fresh `claude -p <prompt>` subprocess per
message (`dashboard/server.py:88`). It technically works agentically (it really created
`projects/acme/`), but it feels broken and is unsafe to rely on:

- **No streaming.** It reads stdout in 64-byte chunks, but `claude -p` without
  `--output-format stream-json` buffers the whole answer and returns it at the end, so the
  panel looks frozen for 10–40s.
- **Cold + amnesiac.** Every message is a brand-new session; conversation history is
  re-stuffed into the prompt each turn.
- **Unconstrained.** Nothing forces mutations through `fileops` + `reindex()`. The agent can
  free-form-write anywhere in the repo; it only happened to do the right thing.
- **No structure / no skills leverage.** A freeform prompt, not the controlled, repeatable
  behavior we want.

## Goal

Make AI a first-class engine inside the OS for creating tasks, posts, projects, milestones,
and for brainstorming — using Claude Code's capabilities (model + skills), with two surfaces:

1. **Chat panel** — everyday, guard-railed, streamed, *acts directly and shows the result*.
2. **Integrated terminal** — VSCode-style embedded terminal running full `claude` with all
   skills, for power/multi-step work.

## Decisions (locked)

| Decision | Choice |
| --- | --- |
| Engine | **A: Claude Code CLI**, persistent headless session (no API key, uses existing login, skills work, zero new Python deps) |
| Interaction | **Both**: chat for everyday + integrated terminal for power |
| Action model | **Act directly, show result** (chat mutates immediately, UI refreshes) |
| Safety rail | **`osctl`** stdlib CLI wrapping `fileops` + `reindex`; chat agent may mutate *only* via `osctl` |
| Terminal style | **Way 1: embedded xterm.js + stdlib WebSocket/PTY bridge** (no external binary; styled to match the app) |
| Layout | **A (Cursor Classic):** left nav · center OS views · **collapsible chat docked right** · **terminal toggles from bottom** |
| Scope | **Both built together** |

## UI / Layout (Cursor model)

The window mirrors Cursor/VSCode ergonomics, shaped to each surface's content:

```
┌──────────────────────────────────────────────────────────────┐
│  brand / menu                                                  │
├──────────┬───────────────────────────────────┬───────────────┤
│  nav     │                                    │  💬 Chat       │
│ Projects │     Calendar / Operations          │  (tall, right, │
│ Profiles │     (main OS views)                │  collapsible)  │
│ Channels │                                    │  acts directly │
│          │                                    │  shows result  │
│          ├────────────────────────────────────┤               │
│          │  ▸ Terminal  (toggles up, ⌃`)       │               │
│          │  full claude · all skills · xterm   │               │
└──────────┴────────────────────────────────────┴───────────────┘
```

Rationale: chat is vertical (conversation scrolls down) → tall right dock; terminal is
horizontal (wide recent output) → bottom panel. Matches Cursor muscle memory.

**Behaviors:**
- **Chat panel — collapsible.** A chevron / shortcut tucks the right panel away so the
  center OS views go full width (addresses calendar real-estate). State persists across
  reloads (localStorage).
- **Terminal — toggle.** A keystroke (Cursor-style ``⌃` ``) and a button slide the bottom
  terminal in/out. Collapsed by default; opening it lazily establishes the `/ws/terminal`
  connection (PTY spawns on first open, not on app load).
- The existing bottom "consultant drawer" in `app.html` is replaced by this arrangement:
  chat moves to the right dock; the bottom becomes the integrated terminal.

## Architecture

```
                         ┌──────────────────────── app.html ────────────────────────┐
                         │  Chat panel (SSE)          Integrated terminal (xterm.js)  │
                         └─────────┬───────────────────────────┬─────────────────────┘
                                   │ POST /api/ask (SSE)        │ WS /ws/terminal
                                   ▼                            ▼
                         ┌───────────────────┐        ┌───────────────────────┐
                         │ ChatSession        │        │ TerminalSession (PTY)  │
                         │ persistent claude  │        │ os.openpty + claude    │
                         │ --resume, stream-  │        │ interactive, full tools│
                         │ json, osctl-only   │        │ all skills             │
                         └─────────┬──────────┘        └───────────┬───────────┘
                                   │ Bash(python -m dashboard.osctl …)         │ (free,
                                   ▼                                            │  full trust)
                         ┌───────────────────┐                                 │
                         │ dashboard/osctl.py │ ── validate → fileops → reindex │
                         └─────────┬──────────┘                                 │
                                   ▼                                            ▼
                         authored files (source of truth) ── index.py ──► os.db (read-only)
```

Both surfaces operate on the same repo and the same authored-files-are-truth invariant.
The chat is permission-constrained; the terminal is full-trust. They are separate processes
(different permission profiles) but MAY share context by resuming the same session id.

## Components

### 1. `dashboard/osctl.py` (new) — the safety rail

Stdlib `argparse` CLI. One subcommand per existing `fileops` mutation; each validates input,
calls the `fileops` function, runs `fileops.reindex()`, and prints a single JSON result line
to stdout (`{"ok": true, ...}` or `{"ok": false, "error": "..."}` with non-zero exit).

Subcommands (mapping to existing `fileops` functions):

| Subcommand | fileops call |
| --- | --- |
| `create-project` | `create_project(slug, fields)` |
| `create-profile` | `create_profile(project_slug, slug, fields)` |
| `create-channel` | `create_channel(profile_slug, slug, platform, handle)` |
| `add-post` | `add_post(profile_slug, fields)` |
| `create-activity` | `create_activity(fields)` |
| `create-milestone` | `create_milestone(fields)` |
| `mark-done` | `mark_activity_done(title, entity_slug)` |
| `update-post` | `update_post(post_id, fields)` |
| `set-status` | `set_status(post_id, status)` |

`osctl` is the single mutation entry point shared by the chat agent and (optionally) the
human. It is independently unit-testable.

### 2. `dashboard/chat_session.py` (new) — persistent chat agent

Owns one long-lived `claude` process for the dashboard run:

- First turn: `claude -p --input-format stream-json --output-format stream-json
  --session-id <uuid> --add-dir <repo> --append-system-prompt <RAIL>
  --allowedTools "Bash(python -m dashboard.osctl:*) Read Grep Glob"
  --disallowedTools "Write Edit" --permission-mode default`, `cwd` = repo root.
- Subsequent turns: same flags with `--resume <uuid>`.
- The allowlist covers everything the agent needs, so there are **no permission prompts**
  (no hangs). `Write`/`Edit` disallowed → it physically cannot free-form the file tree.

**RAIL system prompt (essence):** "You operate a Leo Bot whose source of truth is authored
files. Mutate state ONLY by running `python -m dashboard.osctl <cmd>`. Never write or edit
files directly. After acting, confirm briefly what you changed."

Exposes a generator that yields parsed events: assistant text deltas and `tool_use` events
(for rendering activity chips). On process death, transparently restart and re-apply the rail.

### 3. `dashboard/terminal_session.py` (new) — PTY + WebSocket bridge

- `os.openpty()` → spawn `claude` (interactive, full tools, all skills) with `cwd` = repo,
  optionally `--resume <chat session id>` for shared context.
- A reader thread pumps PTY output → WebSocket frames; inbound WebSocket frames → PTY stdin.
- Handle `{"type":"resize","cols":..,"rows":..}` control frames → `TIOCSWINSZ` ioctl +
  `SIGWINCH`. Clean up PTY/child on disconnect.

### 4. `dashboard/server.py` (modify)

- Replace `_handle_ask` to drive `ChatSession` and stream parsed deltas + tool events as SSE.
- Add a minimal **WebSocket** upgrade handler at `/ws/terminal` (RFC 6455 handshake +
  frame encode/decode, ~120–150 lines; text/binary + close + ping/pong). Bridges to
  `TerminalSession`. This is the one genuinely new low-level piece — built test-first.
- On shutdown, terminate child sessions.

### 5. `dashboard/app.html` (modify)

Restructure to Layout A (see UI / Layout above): left nav, center OS views, **collapsible
right chat dock**, **toggleable bottom terminal**. The current bottom consultant drawer is
removed; chat moves right, terminal takes the bottom.

- **Chat (right dock):** consume the streamed SSE deltas into the bubble token-by-token;
  render `tool_use` events as live activity chips ("⚙ Creating post…"); on turn end, refresh
  the affected view (`/api/tree`, `/api/timeline`) so new entities appear — "act directly,
  show result." Collapsible via chevron + shortcut; collapse state persisted to
  localStorage; collapsing gives the center views full width.
- **Terminal (bottom, toggle):** vendor `xterm.js` (+ `fit` addon) as static assets (script
  tag, no build step). Hidden by default; ``⌃` `` / button slides it in. The `/ws/terminal`
  connection and PTY spawn happen lazily on first open. Connect to `/ws/terminal`; wire
  resize via the fit addon (emit `resize` control frames); theme to match the app.

## Data flow

**Chat:** `app.html` POST `/api/ask {messages|turn}` → `ChatSession` feeds the user turn to
the live process → server parses stream-json events → SSE `{"delta": "..."}` /
`{"tool":"osctl create-post", ...}` / `[DONE]` → UI streams text, shows chips, then refreshes
views. Mutations land via `osctl` → `fileops` → `reindex()` → `os.db`.

**Terminal:** browser opens WS `/ws/terminal` → server PTY-spawns `claude` → bidirectional
byte bridge → full interactive Claude Code in-pane.

## Error handling

- **Chat process dies:** auto-restart, re-apply rail, emit a one-line "reconnected" notice.
- **`osctl` bad input:** non-zero exit + JSON error; the agent reads it and self-corrects.
- **Terminal disconnect/child exit:** tear down PTY, show "session ended — reconnect" with a
  reconnect affordance.
- **WebSocket handshake/frame errors:** close with a proper status code; never crash the
  stdlib server thread.

## Testing

- **`osctl` unit tests:** each subcommand → asserts correct `fileops` call, that `reindex()`
  ran, and JSON/exit-code contract on success and on validation failure.
- **WebSocket bridge tests:** RFC 6455 handshake; frame encode/decode round-trip
  (text, ping/pong, close, masked client frames); resize control frame → ioctl path.
- **Chat SSE smoke test:** `/api/ask` emits incremental `delta` events and terminates with
  `[DONE]` (claude invocation mocked).
- Preserve existing dashboard test discipline (currently 12/12).

## Constraints honored

- **No new Python deps** — server stays stdlib-only; `osctl`, chat session, PTY, and the
  WebSocket bridge are all stdlib. The only added asset is a vendored `xterm.js` JS file
  (no npm/build pipeline), which is the unavoidable minimum for an in-app terminal.
- **Authored files = source of truth; `os.db` read-only**, rebuilt by `reindex()`.
- **UI-first:** both AI surfaces live inside the dashboard window; the user never hand-edits
  files. (The chat is the primary surface; the terminal is power mode, in-app.)

## Out of scope (this iteration)

- Migrating to the Claude Agent SDK / in-process custom tools (clean future swap; the
  `osctl` boundary makes it low-risk).
- Multi-user / remote hosting.
- Streaming the terminal session's transcript back into chat context automatically.

## Open implementation risks

- Hand-rolled WebSocket in `http.server` is the highest-complexity piece — isolate it in
  `terminal_session.py` + a focused server handler and build it test-first.
- `stream-json` event schema parsing for the chat — pin to the fields we read (assistant
  text deltas, tool_use) and tolerate unknown event types.
