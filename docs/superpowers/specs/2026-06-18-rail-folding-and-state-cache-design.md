# Rail folding + chat state cache — design

Date: 2026-06-18
Status: Approved (brainstorm)

## Problem

Two independent pain points in the dashboard:

1. **The left rail does not scale.** Every project card renders all five
   strategy sections (Overview, Problem & validation, Experiments, Positioning
   & pricing, Product) **plus** every profile and every channel, always fully
   expanded (`dashboard/app.html` `renderRail`, ~L319–343). With more than one
   or two brands the rail becomes a long, unscannable wall.

2. **The chat agent re-discovers the data model every turn.** `osctl` is
   write-only (no read/list subcommand), so each turn the guard-railed
   `claude` session forages with `Read`/`Grep`/`Glob` to learn the current
   structure before acting. This is the "let me look at the existing data
   model" narration and most of the `Bash` tool chips. Separately,
   `_handle_ask` (`dashboard/server.py:78`) already receives a `context` field
   from the frontend's `buildContext()` but **silently ignores it** — only
   `messages[-1]["content"]` is used.

Not in scope: the chat's verbose narration rendering, and the per-channel
guidelines model (working as designed — `guidelines.md` is per-platform and
feeds the voice cascade in `generate.py`).

## Part A — Rail folding (frontend only, `dashboard/app.html`)

Add disclosure (collapse/expand) behavior to the rail hierarchy.

### Behavior

- Each **project** card header gets a `▸`/`▾` toggle. Collapsed = only the
  header (name + kind tag) shows; expanded = sections + profiles + channels.
- Each **profile** row gets a `▸`/`▾` toggle that shows/hides its channel list.
- A project renders expanded if it is in the persisted open-set **or** it is
  the currently-active project (`STATE.project`). Same rule for a profile's
  channel list (active profile auto-expands).
- Newly created project / profile / channel auto-opens its ancestors so the
  user sees what they just made.

### State + persistence

- Module-level `OPEN = { projects: Set<slug>, profiles: Set<slug> }`.
- Persisted to `localStorage` (key `Leo Bot.rail.open`) as two string arrays, so
  folds survive the full `renderRail` that fires after every chat action and
  `refreshViews()`.
- Toggling a triangle mutates `OPEN`, persists to `localStorage`, and calls
  `renderRail()` to re-render. Triangle click calls `stopPropagation`
  so folding never triggers navigation; clicking the name still navigates as
  today (`selectSection` / `selectProfile` / `selectChannelGuidelines`).

### Implementation notes

- Keep the existing template-string rendering in `renderRail`. Gate the
  project body and the per-profile channel block on the open rule, and prepend
  a toggle element to the project header and profile row.
- Wire toggles in the same `querySelectorAll(...).onclick` block that already
  wires `[data-section]`, `[data-profile]`, etc. Use new data attributes
  `data-toggle-project` / `data-toggle-profile`.
- A tiny `isOpen(kind, slug)` helper encapsulates the "in OPEN set OR active"
  rule so the render and the auto-open-on-create paths stay consistent.
- `openNewProject` / `openNewProfile` / `openNewChannel` add the relevant
  slug(s) to `OPEN` before calling `renderRail()`.

## Part B — Chat state cache (backend, `dashboard/server.py` + `dashboard/chat_session.py`)

Give the agent the current structure up front so it stops foraging.

### `state_snapshot(tree)` — pure helper (server.py)

- Input: the list returned by `db.tree()` (the same data that renders the rail).
- Output: a compact plain-text outline. Shape:

  ```
  ## Current Leo Bot state
  acme (brand)
    profile demo "Demo Brand"
      channel demo-tiktok (tiktok)
      channel demo-instagram (instagram)
  ```

- Pure and deterministic → unit-testable without a server or DB. Empty tree
  yields a single `(no projects yet)` line under the header.

### `_handle_ask` wiring

- Build the snapshot from `db.tree()` (authoritative server-side source; do
  **not** trust the frontend `context` for structure).
- Prepend it to the turn text passed to `ChatSession.ask`:

  ```
  <snapshot>

  ## Request
  <user message>
  ```

- Snapshot is regenerated each turn, so it always reflects the result of the
  previous action.

### `RAIL` update (server.py)

Append one instruction:

> "The current Leo Bot state is provided to you at the start of each turn — do
> not explore with Read/Grep/Glob to discover existing structure; act
> directly with osctl."

`ChatSession` itself is unchanged — it already accepts arbitrary turn text and
streams events; only the text it receives changes.

## Testing

- **Part A:** manual — load with ≥2 projects, verify projects start collapsed
  except the active one, toggles fold/unfold without navigating, name clicks
  still navigate, folds survive a chat action / refresh, and creating an
  entity auto-opens it. (Rail rendering is untested DOM code today; no harness
  added.)
- **Part B:** unit tests for `state_snapshot` — typical tree, empty tree,
  project with no profiles, profile with no channels. Existing
  `tests/test_chat_session.py` continues to pass unchanged. Optionally assert
  `_handle_ask` prepends the snapshot header (light integration test if cheap).

## Files touched

- `dashboard/app.html` — rail folding (Part A).
- `dashboard/server.py` — `state_snapshot`, `_handle_ask` wiring, `RAIL` text
  (Part B).
- `tests/` — new tests for `state_snapshot`.
