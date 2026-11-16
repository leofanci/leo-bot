# Canvases and lists

Status: design approved, not built. Date: 2026-11-16.

## Intent

The canvas pane (right of the chat, `dashboard/app.js` "canvas pane") today only switches
between fixed views: Content, Files, Calendar, project sections. Nobody can make a page of
their own there.

Canvases turn it into a Slack-style canvas: pages the owner, invited coworkers and bots create,
read, edit and delete, pinned as tabs in rooms. Anything that serves the app's purpose (running
ventures, go-to-market, outreach, content) fits: a CRM, a lead tracker, a playbook, meeting
notes, a watch view of a bot's browser.

The platform rule holds: no feature per use case. Canvases are built from a few generic blocks.
A "CRM" is a canvas with a list block and a text block, set up by a person or by a bot that was
asked to set one up. Nothing in code knows what a CRM is.

## Goals

- Owner, coworkers and bots can create, read, edit and delete canvases and lists, both ways: a
  bot edits what a person made and a person edits what a bot made, within the rules below.
- Canvases belong to one project workspace; rooms pin them as tabs.
- Lists (typed rows and columns) are their own objects, embeddable in any canvas of the same
  workspace, edited row by row, so scheduled jobs and people can change one row without
  rewriting anything else.
- Every change records who made it and when, with undo.
- A browser block shows a live view of a bot's browser page with a Stop button.
- Bots work through osctl only, like every other write.

## Non-goals (round one)

- Taking over the bot's browser from the canvas (click, type, stream). Watch and Stop only.
- Real-time co-editing with cursors. Last write per block or row wins, with a version check.
- Formulas, charts, relations between lists, file uploads into lists.
- Canvases in HQ visible to anyone but the owner.
- Templates as code. A template is a saved block layout (data), see below.

## Anti-goals

- Any bot, job, coworker or canvas command changing Leo Bot's own code. Hard rule, see Part 0.
- A canvas or list kind hard-coded for one use case (CRM table in code, outreach-only fields).
- A coworker's text reaching the owner's bots as if the owner wrote it.
- Canvas edits that keep a whole workspace flagged as outside content forever (see Safety).

## Part 0: Leo Bot's code is never writable by a bot

Today: Leo Bot's code folder and its parents cannot be linked (`core/folders.py`, `own_msg`), and
browser turns wall it off in the shell sandbox (`dashboard/shell_rules.py`, `browser_walls`).
Gap: a shell turn on Full access before outside content and without a browser runs commands as
the owner, so it can write into `paths.CODE`. The Claude chat session also starts with
`repo_dir=paths.CODE` (`dashboard/server.py`, `ChatSession(repo_dir=...)`).

Change, built first, before any canvas work:

- Every bot shell command, at every level (Approve for me, Full access), before and after
  outside content, runs under a sandbox profile that denies writes to `shell_rules.REPO`
  (Leo Bot's code), the Python that runs osctl (the same `sys.prefix`, `sys.base_prefix`,
  `sys.executable` folder and site-packages paths the browser wall uses) and renames of every
  folder above them. Network rules stay as they are today for each level; only the write denies
  are added. Reuse the deny builder the browser wall already has rather than a second copy.
- A Mac without sandbox-exec, or another OS: a Full access command whose parsed target writes
  under those paths is refused by the wall-2 command check; the docs state the limit.
- The engine-level tools (Claude Code Edit/Write, Codex patch) get the code folder in their deny
  or outside their writable roots on every engine (`dashboard/engines/*`).
- The chat session's working folder moves off `paths.CODE` to a neutral per-turn folder (the
  data folder's run area), in both places it is set: `ChatSession(repo_dir=...)` and
  `ChatSession.restore(...)` in `dashboard/server.py`. osctl still runs as Leo Bot's own (`python3 -I` rewrite already in
  `shell_rules`).
- Linked repos stay editable for coding bots, exactly as today.
- Tests: a Full access turn with no browser cannot create, change or rename a file under the code
  folder or the osctl Python; a linked edit folder still accepts writes; each engine's write
  tool is denied in the code folder.

## Approaches considered

- **A. One JSON file per canvas, tables inline.** Simplest. Every row change rewrites the file,
  so a job logging replies clashes with a person editing the same canvas, and per-row authors
  are awkward. Rejected.
- **B. Canvases plus separate lists (chosen).** A canvas file holds ordered blocks; tables are
  list objects with their own rows, ids, authors and history, embedded by reference. Row-level
  edits, clean per-row labels, one list reusable in several canvases. Two kinds of objects.
- **C. Markdown pages with markdown tables.** Natural for bots, but no typed columns, filters or
  per-row history, and concurrent edits are hard to merge. Rejected.

## Data model

Files are the source of truth; the SQLite index (`index.py`, `dashboard/db.py`) is rebuilt from
them for search and filters, as for every other artifact.

```
projects/<ws>/canvases/cv<N>.json
projects/<ws>/lists/ls<N>.json
projects/<ws>/canvases/history/<id>.jsonl     (append-only change log, canvases and lists)
projects/<ws>/canvases/templates/tp<N>.json   (saved block layouts)
```

Canvas:

```json
{"id": "cv1", "title": "Leads", "created_by": "owner", "created_at": "...",
 "coworkers": "edit", "version": 7,
 "blocks": [
   {"id": "bk1", "type": "text", "md": "...", "created_by": "bot:research", "by": "bot:research", "at": "...", "outside": false},
   {"id": "bk2", "type": "checklist", "items": [{"id": "i1", "text": "...", "done": false, "created_by": "...", "by": "...", "at": "...", "outside": false}]},
   {"id": "bk3", "type": "list", "list": "ls1", "view": {"filter": {"stage": "replied"}, "sort": "-last_contact"}},
   {"id": "bk4", "type": "files", "paths": ["projects/demo/docs/x.md"]},
   {"id": "bk5", "type": "view", "route": "#/project/demo/gtm"},
   {"id": "bk6", "type": "browser", "bot": "research"}
 ]}
```

- `coworkers`: `edit` (default) or `view`. Owner-only setting.
- Block types, round one: `text` (markdown), `checklist`, `list` (embed with an optional saved
  filter and sort), `files` (links to work files in the same workspace), `view` (an existing
  canvas route, rendered read-only inside the block), `browser` (see Browser block).
- `by`: `owner`, `person:<pid>`, or `bot:<id>`; a job run records `bot:<id>` plus `job:<id>`.

List:

```json
{"id": "ls1", "title": "Leads", "version": 31,
 "columns": [{"key": "name", "label": "Name", "type": "text"},
             {"key": "channel", "label": "Channel", "type": "select", "options": ["IG", "LinkedIn", "Email"]},
             {"key": "stage", "label": "Stage", "type": "select", "options": ["new", "sent", "replied", "won", "lost"]},
             {"key": "last_contact", "label": "Last contact", "type": "date"},
             {"key": "notes", "label": "Notes", "type": "text"}],
 "rows": [{"id": "rw1", "cells": {"name": "acme", "stage": "sent"}, "created_by": "bot:research", "by": "bot:research",
           "at": "...", "outside": false}]}
```

- Column types: `text`, `number`, `date`, `select`, `checkbox`, `url`, `person` (owner, a
  coworker of the workspace, or a bot).
- Every row, block and checklist item keeps `created_by` (never changes) plus `by`, `at`
  (last writer) and `outside`. A cell edit updates `by`/`at`; the history line records the cell.
  Permission rules below judge by `created_by`, never by the last writer.
- Writes are per row or per block under a per-file lock with the `version` check: a stale write
  (version moved) on the same row or block is refused and the writer re-reads; writes to other
  rows or blocks merge.

IDs (`core/ids.py`): canvases are a new entity under the project, `pr1.cv1`; blocks
`pr1.cv1.bk2`; lists `pr1.ls1`; rows `pr1.ls1.rw4`. Add `cv`, `bk`, `ls`, `rw` to the segment
prefixes and `IdRegistry.build`. A list embedded in several canvases keeps one id.

Deleting a list that a canvas embeds: refused while embedded, unless the call also removes the
embeds (`--force-unembed`); the canvas shows a "list deleted" placeholder otherwise.

## Rooms and the canvas pane

- Pins are stored per workspace in `projects/<ws>/canvases/pins.json`: `{room_id: [cv ids]}`
  in pin order. Only topic rooms have a stored record (`rooms.json`); `general` and DM rooms
  are built on the fly in `core/rooms.py` `list_rooms`, so pins live apart from room records and
  work for every room id. Deleting a topic room drops its pins. Pin, unpin, reorder: owner and
  coworkers who can see that room; bots via osctl.
- The canvas pane: the existing fixed views stay. When the open room has pinned canvases, the
  canvas bar (`renderCanvasNav`) shows them as tabs plus a `+` (new canvas, from blank or a
  template). The project's full canvas list is in the More menu for the owner.
- Route: `#/canvas/<cv-id>`. A bot that creates or changes a canvas returns it as a deliverable
  (`dashboard/deliverables.py`), so "Open in canvas" works as for other work.
- Editing in the UI: blocks are edited in place (text as markdown with preview, checklist
  toggles, list as a grid with add row, inline cell edit, column add/rename/type). Add, move,
  delete block from a block menu. Undo per change from the history.
- Server API: `GET/POST /api/canvases`, `/api/canvases/<id>`, `/api/lists/<id>`,
  `/api/lists/<id>/rows`, same auth layer as other project data (owner, or a person scoped by
  `rooms.can_see` to a room that pins the canvas).

## osctl commands

All new commands go through osctl; nothing writes the files directly.

**The server does the work, not the osctl process.** osctl gets no trustworthy bot id today
(its environment has `LEOBOT_WORKSPACE`, `LEOBOT_JOB`, `LEOBOT_CHAT`, `LEOBOT_SHARED`, and a Full
access shell can set its own environment). So every canvas command posts to a new server
endpoint, `POST /api/internal/canvas`, with the turn's token: the gate token
(`LEOBOT_GATE_TOKEN` / `LEOBOT_GATE_URL`, already passed to osctl by `os_relay.py` and read in
`osctl.py`) where the engine has one, else a per-turn canvas token the server mints into the
osctl environment for that turn only. The server maps the token to its turn and takes the actor
(owner turn, `bot:<id>`, `job:<id>`, `person:<pid>`), the workspace, the room, shared or not,
and the taint state from its own records. Nothing in the request names the actor. No valid token:
the command is refused. Permission checks, labels and history all live server-side, in one module
(`core/canvases.py`), used by both the dashboard API and this endpoint.

| Command | What |
|---|---|
| `list-canvases --project <slug> [--room <id>]` | ids, titles, pinned rooms |
| `get-canvas --id <cv>` | blocks; embedded lists summarized (columns, row count) |
| `create-canvas --project <slug> --title "..." [--template <tp>] [--blocks-json ...] [--pin <room>]` | new canvas |
| `update-canvas --id <cv> [--title] [--coworkers edit\|view]` | `--coworkers` owner only |
| `add-block` / `update-block` / `move-block` / `delete-block --id <cv> [--block <bk>] ...` | one block |
| `delete-canvas --id <cv>` | |
| `pin-canvas` / `unpin-canvas --id <cv> --room <id>` | |
| `create-list --project <slug> --title "..." --columns-json '[...]'` | |
| `get-list --id <ls> [--filter-json] [--sort] [--limit]` | rows, paged |
| `update-list --id <ls> [--title] [--columns-json]` | column add/rename/retype |
| `add-row` / `update-row --id <ls> [--row <rw>] --cells-json '{...}'` | one row; `add-rows` for a batch, one approval |
| `delete-row --id <ls> --row <rw>` / `delete-list --id <ls>` | |
| `save-template --from <cv> --name "..."` / `list-templates --project <slug>` | |

## Who may do what

Owner: everything.

Bots, owner's own threads (ownership is `created_by` of the item, see Data model):

- Items the bot created: free edit and delete.
- Items a person or another bot created: add rows, update cells, add blocks, check items freely.
  Deleting a row, block, list or canvas, and `update-block` on a text block whose `created_by`
  is someone else, end in an APPROVAL line (the existing approval path) and run only on
  Approve.
- Scheduled jobs: per job mode (`dashboard/osctl.py`). Every new command goes into exactly one
  of `_AUTO_ALLOWED` or `_AUTO_WAITS` (`tests/test_jobs.py` fails otherwise). `_AUTO_ALLOWED`:
  the reads, `create-canvas`, `create-list`, `add-block`, `add-row`, `add-rows`, `update-row`,
  `pin-canvas`, and `update-block` on a block the job's bot created (the server refuses it on
  anyone else's text block and the job ends with an APPROVAL line, in line with "text a job read
  must not wipe a doc"). `_AUTO_WAITS`: every `delete-*`, `move-block`, `update-list` (columns),
  `update-canvas`, `unpin-canvas`, `save-template`, `undo`. Approval and Report jobs follow their
  mode.

Coworkers (invited people, `docs/sharing.md`):

- See a canvas only when it is pinned in a room they can see (`rooms.can_see`), inside that
  project. Never the project's full list, never HQ, never other projects. A list is visible only
  through a visible canvas that embeds it.
- Edit by default; a canvas set to `coworkers: view` is read-only for them. They cannot change
  `coworkers`, delete a canvas the owner created, or pin into rooms they cannot see.

Bots in shared threads (relay only, `dashboard/os_relay.py`; the allowlist itself is
`_SHARED_ALLOWED` in `dashboard/osctl.py`): the canvas commands are added there, bound by the
server to the token's workspace and limited to canvases pinned in that thread's room (and their
lists). `delete-canvas`, `delete-list`, `update-canvas` and `undo` go into `_SHARED_BLOCKED`. A
shared-thread bot may delete a row or block only when that item's `created_by` is that bot or a
person in that thread; anything else is refused outright, with no APPROVAL line (no one in a
shared thread approves for the owner). Add the new commands to `tests/test_case_variants.py`
coverage.

Undo:

- Every change has a history entry with an id. Undo restores the item to its state before that
  entry.
- Owner: undo anything, including a deleted canvas or list. Deleted canvases and lists move to
  `projects/<ws>/canvases/trash/` with their history for 30 days, then are removed.
- Coworkers: undo their own changes only.
- Bots: `undo --change <id>` on their own changes only (in `_AUTO_WAITS` for jobs).

## Safety: outside content in canvases

Text a coworker writes, and text a tainted turn writes (one that read mail, pages, app data,
`dashboard/taint.py`), is outside content. It is labeled per item, not per workspace:

- Every block, checklist item and row carries `outside: true` when written by a coworker or by a
  tainted turn. A later clean write by the owner, or by a clean turn that replaces the whole item,
  clears it.
- A turn that reads a labeled item (`get-canvas`, `get-list`, a view that includes it) becomes
  tainted from that read on (`taint.Turn`), so the existing gates apply: shell sandbox after
  outside content, held sends, act-grant holds. The read goes through the server endpoint, so
  the server marks the turn itself.
- Engines the taint gate cannot watch mid-run (`dashboard/server.py` notes which: only this
  machine's Claude Code and API engines are gated): labeled items are withheld from their reads.
  The bot gets the item's id, column values that are not text (stage, date, select), and
  "outside content, hidden on this engine: the owner can trust it from the canvas". Unlabeled
  items read normally.
- Canvas and list files are left out of the workspace-wide ledger in `core/provenance.py`
  (otherwise one coworker note would keep the whole workspace flagged until the owner trusts the
  whole file). The per-item label replaces it for these files. The owner can trust items one by
  one, or a whole canvas, from the canvas (clears `outside`; logged in history).
- osctl output marks labeled items (for example a prefix line per item), so the bot's prompt
  shows which text is outside content.

## Browser block

- `{"type": "browser", "bot": "<id>"}` shows that bot's current browser page in this workspace:
  a screenshot refreshed every few seconds while the block is on screen and the bot has a live
  browser session, the page URL and title, and a Stop button. No click or type passthrough.
- Where screenshots come from: the relay (`dashboard/browser_relay.py`) itself takes one after
  each call of the bot's run that navigates, clicks or commits, inside that call's existing
  browser lock, and keeps the latest per bot (image held by the server, never a caller path).
  No server-side poll: it would compete with the bot's calls for the one-at-a-time lock, and in
  the owner's Chrome it could switch the owner's visible tab. In My Chrome mode the capture is
  of the bot's own pinned tab during the bot's own call only.
- Server: `GET /api/browser/live?ws=&bot=` returns that latest screenshot plus URL, title and
  its time; the block refreshes it every few seconds while on screen. Owner only in round one
  (coworkers see "owner only").
  Stop ends the bot's current run the same way the existing stop does and closes its browser
  child.
- My Chrome mode: the block shows only the bot's own pinned tab, never other tabs of the owner's
  Chrome.
- No session: the block shows "not browsing" and the last screenshot time.

## Templates

A template is a saved block layout with empty lists (columns kept, rows dropped). The app ships a
few generic ones as data (`templates/canvases/*.json`: tracker, CRM-style pipeline, meeting
notes, checklist). Bots may create one on request with `save-template`. No template is special in
code.

## Docs and rules

- `docs/canvases.md`: model, commands, permissions, safety labels.
- `CLAUDE.md` osctl table and `dashboard/ai_rules.py`: one short line each pointing bots at the
  canvas commands; keep the chat rail under its size test budget.
- Guard tests: `tests/test_no_usage_data.py` stays green (fixtures use `demo`, `acme`).

## Left to the builder

- Grid widget for lists: plain HTML table with inline edit, no new dependency.
- Exact refresh rate of the browser block (2 to 5 seconds) and its rate limit.
- History file rotation size.
- How osctl formats labeled items, as long as each one is visibly marked.
- Order of the tasks after Part 0.

## Calls made in the design (approved with the design)

- Canvases per project workspace; HQ canvases owner-only.
- Deletes and text rewrites on things a bot did not create need approval; adds and cell edits do
  not.
- Coworker text labeled per item, and canvas files left out of the workspace-wide provenance
  ledger.
- Browser block owner-only in round one; screenshots taken by the relay during the bot's own
  calls, no polling.
- No real-time co-editing; per-row and per-block version check.
- Canvas commands run server-side via the turn token; actor never taken from the request.
- Engines without the mid-run taint gate get labeled items withheld, not the whole turn tainted.
- Ownership judged by `created_by`; auto jobs may rewrite only text blocks their bot created.
- Shared-thread bots delete only items created by themselves or people in that thread; no
  approvals in shared threads.
- Undo: owner anything (30-day trash), coworkers and bots their own changes only.
