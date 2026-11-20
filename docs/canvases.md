# Canvases and lists

Canvases are workspace pages made of generic blocks. Pin one in a channel to show it as a tab in the canvas pane. Existing Content, Files, Calendar and project views stay available. The owner can find unpinned pages under More, All canvases. HQ canvases are owner-only.

## Blocks and editing

- **Text:** markdown with Edit and Preview. Save changes the block; Cancel keeps its stored text.
- **Checklist:** toggle items, add items and delete individual items.
- **List:** a typed table, embedded by reference. Click a cell or focus it and press Enter to edit. Enter saves; Escape cancels. Leaving a cell also saves it. Add rows and add, rename or retype columns. Supported types: text, number, date, select, checkbox, URL and person. Changing a type clears values that no longer fit; Undo restores them.
- **Files:** workspace file links. Known artifacts open their dashboard view; other paths open Files.
- **View:** an existing project section, shown read-only. Coworkers cannot open owner-only views.
- **Browser:** the owner's watch-only view of a bot's browser, described below.

Block menus move blocks up or down, or delete them. The `+` beside canvas tabs offers a blank canvas and templates. Shipped templates are ordinary layouts: tracker, pipeline, meeting notes and checklist. Saved templates keep columns but drop list rows.

A list is independent of the canvas that embeds it. Several blocks can show the same list with different saved filters and sorts. Deleting a list is refused while embedded unless `--force-unembed` also removes its embeds.

## Storage and versions

Files are truth; the database index is derived. Workspace data lives under:

- `projects/<ws>/canvases/cv<N>.json`: ordered blocks and canvas metadata.
- `projects/<ws>/lists/ls<N>.json`: columns and rows.
- `projects/<ws>/canvases/pins.json`: channel pins, in tab order.
- `projects/<ws>/canvases/history/` and `projects/<ws>/lists/history/`: append-only change logs, rotating at 2 MB.
- `projects/<ws>/canvases/templates/`: saved layouts.
- `projects/<ws>/canvases/.trash/` and `projects/<ws>/lists/.trash/`: deleted objects, retained for 30 days. The server removes older ones, with their history, at start and once a day. A removed canvas, list or template number is never given out again.

HQ uses `team/` instead of `projects/<ws>/`. Each block, checklist item and row records its original `created_by`, last writer `by`, timestamp `at`, item version `v` and outside-content label. Permissions use the creator, never the last writer.

Writes take a per-file lock. Dashboard writes send the document `version` they read and, for item edits, its `item_version`. Concurrent edits to different items can merge; a changed target returns HTTP 409. The UI reloads, says `Changed by someone else: reloaded`, and keeps text and cell drafts for an explicit retry. A draft retains the version editing started from, even if a background reload completes during editing.

History records actor, operation, target and before/after values. A job also records its job id. Use the History drawer's Undo button for an individual change. The owner can undo any change, including a canvas or list deletion. A deleted canvas leaves every channel it was pinned in; undoing the delete pins it back. Coworkers and private bots can undo their own changes only. History rotates at 2 MB, retaining one previous file.

Canonical references nest under the project: `pr1.cv1`, `pr1.cv1.bk2`, `pr1.ls1`, `pr1.ls1.rw4`. Use `get-id-catalog` and `resolve-id` to look them up.

## Commands

Bots read and write through `python3 -m dashboard.osctl`, never by editing data files. Canvas commands run only inside a bot turn: osctl posts to the local server using a per-turn canvas token. The server derives the actor, workspace and permissions from its own token record, not caller-supplied actor fields. The token is revoked when the run ends. A consulted teammate is read-only.

| Command | Purpose |
|---|---|
| `list-canvases --project <slug> [--room <room>]` | Visible canvases and pins |
| `get-canvas --id <cv>` | Blocks and embedded list summaries |
| `create-canvas --project <slug> --title "..." [--template <template>] [--pin <room>]` | Blank page or template |
| `update-canvas --id <cv> --version <n> [--title "..."] [--coworkers edit\|view]` | Canvas metadata; coworkers setting is owner-only |
| `add-block --id <cv> --type text --md "..."` | Add a block; other types use their own data flags |
| `update-block --id <cv> --block <bk> --version <n> --md "..."` | Replace text; other types use their own data flags |
| `move-block --id <cv> --block <bk> --version <n> --after <bk>` | Reorder; use `--first` to move to the start |
| `delete-block --id <cv> --block <bk> --version <n>` | Remove a block |
| `delete-canvas --id <cv> --version <n>` | Move a canvas to trash |
| `pin-canvas --id <cv> --room <room>` / `unpin-canvas --id <cv> --room <room>` | Change channel tabs |
| `create-list --project <slug> --title "..." --columns-json '[...]'` | Create a separate list |
| `get-list --id <ls> [--filter-json '{...}'] [--sort <key>] [--limit <n>] [--offset <n>]` | Filtered, sorted, paged rows; prefix sort key with `-` for descending |
| `update-list --id <ls> --version <n> [--title "..."] [--columns-json '[...]']` | Rename or change columns |
| `add-row --id <ls> --cells-json '{...}'` | Add one row |
| `add-rows --id <ls> --rows-json '[{...}]'` | Add a batch, up to 500 rows |
| `update-row --id <ls> --row <rw> --version <n> --cells-json '{...}'` | Edit supplied cells |
| `delete-row --id <ls> --row <rw> --version <n>` | Remove one row |
| `delete-list --id <ls> --version <n> [--force-unembed]` | Trash a list; optionally remove its embeds |
| `save-template --from <cv> --name "..."` / `list-templates --project <slug>` | Save or list reusable layouts |
| `undo --id <cv-or-ls> --change <change>` | Undo a change by its history id |

Every change or delete of an existing canvas, block, list or row passes `--version`: the `version` that `get-canvas` or `get-list` returned. The server refuses it with `changed since you read it: get it again` when that block, row or the canvas or list settings changed since, and refuses it outright when `--version` is missing. Adds need no version. `--item-version` also pins one block, row or checklist item to the `v` you read. Use a command's `--help` for its full flags. Text bodies accept `--md -` to read stdin. JSON arguments must be valid JSON and at most 1 MB. Limits include 200 blocks per canvas, 50 columns, 5,000 rows per list, 100,000 characters per text block, 10,000 characters per cell, and 100 select options of up to 60 characters per column.

## Who may do what

- **Owner:** all operations, including Trust and changing coworker access.
- **Coworker:** sees only canvases pinned in channels they can see in that workspace. Lists are visible only through those canvases. `coworkers: view` prevents editing. A coworker cannot delete an owner-created canvas, change coworker access, or pin in an invisible channel. Pinning into another channel is limited to their own canvases; DM pins are not available to coworkers.
- **Private bot:** edits its own items freely. It can add blocks and rows, update cells and toggle checklist items on others' work. Deletes and rewrites of others' text need the owner's approval. The server holds such a change; the bot ends its reply with an `APPROVAL:` line. Approval runs the held change once; rejection or a replacement request drops it. Holds expire after seven days.
- **Shared-thread bot:** stays within that channel's visible canvases and lists. It cannot delete canvases or lists, change canvas metadata, or undo. Row/block deletes are limited to its own items or items created by people in that thread. Other destructive edits are refused, not held for approval.
- **Scheduled job:** uses its job mode and the same creator rules. Auto mode permits reads, additions and cell edits; deletes, moves, metadata/schema changes, template saves and undo wait for approval. Rewriting someone else's text is also held. Approval and Report jobs only read; they say what to save and end with an APPROVAL: line. The server enforces these modes itself, and an Auto job that has read outside content saves nothing by itself.

## Outside content

Coworker writes and writes by a turn that has read outside material carry an `Outside content` tag per block, checklist item or row. Reading any labeled item marks a gated bot turn as outside-content-tainted, applying its existing shell and action gates. osctl labels the returned item explicitly.

An engine without a mid-turn gate receives hidden text for labeled items, not their original prose. Non-text values such as numbers, dates, short select values and checkboxes remain available; select options of a labeled list are hidden too. History shows coworkers and bots who changed what, never the values. The owner can clear labels with Trust on an item or the canvas; lists retain their separate labels. A clean full replacement can clear its item's label; a partial cell edit does not silently clear an existing label.

Canvas/list files are excluded from the workspace provenance ledger. Their per-item labels replace it. Direct bot file reads and shell reads of these folders are blocked; use `get-canvas` and `get-list`.

## Browser block

The browser block shows the latest screenshot, page URL, title and capture time for one bot in this workspace. It is owner-only. Stop ends that bot's current runs and closes its browser child; it does not close unrelated tabs in the owner's Chrome. The owner's plain chat Stop ends only the owner's own turns, never a coworker's. A stopped reply keeps what it wrote so far and is marked `Stopped`, also after a reload.

The relay captures images after the bot's own navigation or commit calls, while still holding the browser call lock. It never reads a screenshot from a caller-provided file path, and it removes the image file the engine saved for its own capture once the bytes are back. In My Chrome mode, captures use the bot's pinned tab only. A coworker's turn that uses their own Chrome is never captured, so the owner never sees a coworker's tabs.

The visible block fetches the cached image every three seconds; the endpoint is limited to one request per second per workspace, bot and viewer. No server-side timer polls the browser. Hidden blocks do not refresh. With no session, the block says `Not browsing` and shows the last capture time when available. This is a watch view, not remote control.

## Code wall

Bots cannot write Leo Bot's own code, the Python that runs it, or rename their parent folders. On macOS with sandbox-exec, every bot shell level gets these write denies, before and after outside content. Engine file-edit tools are denied the same paths. Linked Edit folders outside the wall stay editable.

Without sandbox-exec, the shell command check refuses parsed writes naming protected paths. This is a command check, not an OS sandbox: indirect writes from arbitrary programs cannot be fully contained. Bot turns start in a neutral data run folder rather than the checkout.
