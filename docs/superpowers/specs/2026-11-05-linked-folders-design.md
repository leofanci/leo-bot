# Linked folders: several folders, per channel and per bot, with edit access

## Why

Today a workspace links one folder (`local_folder` in `project.md`), HQ links none, and bots may
only read it. Comparable agent tools let a bot work in any folder, set per channel or per bot, and
save there. myOS bots should be able to do real work in the owner's folders, without losing the
walls between workspaces or showing anything to invited people.

## Goals

- Link any number of folders to a workspace, HQ included.
- Each folder has an access level: `edit` or `read`.
- A channel or a bot can be limited to some folders, or to read access.
- Bots can search and read their folders on every private turn, not only on code questions.
- Bots can create and change files in their `edit` folders.
- Works on Claude Code and API-key engines.
- Works the same on a Mac, through a tunnel and on a server.

## Non-goals

- A free shell. Bots still cannot run tests, builds or git on Claude Code and API-key engines. It
  needs a sandbox and is separate work.
- Moving myOS's own data out of the repo checkout. Separate work.
- Edit access on Codex. Its sandbox has no per-path rule for the never-write list, and its shell
  stays off. It stays read-only, and the Engines screen says so.
- Edit access on ACP agents. They stay as they are until checked against a real agent.
- Any copying or syncing of folders between machines.
- A diff or review screen for bot edits.
- Stopping two bots editing the same file at once.

## Anti-goals: a failure even if the feature works

- An invited person, a shared thread or a runner turn reaches a linked folder or sees its path.
- A bot reads or writes another workspace's folder, unless the owner linked it to both.
- A bot writes anywhere in the myOS checkout other than through osctl.
- A bot changes which folders are linked or their access.
- A bot writes a file that makes code run on its own later, such as a git hook.
- An existing link gains write access without the owner choosing it.

## How it behaves

### Folder settings

Stored in the workspace's team folder, the one `core.bots.ws_dir` returns: `team/folders.json` for
HQ, `projects/<slug>/team/folders.json` for a project. Gitignored like the rest.

```json
{
  "folders": [
    {"id": "f1", "path": "/Users/you/Projects/acme-app", "name": "acme-app", "access": "edit"},
    {"id": "f2", "path": "/Users/you/Projects/acme-docs", "name": "acme-docs", "access": "read"}
  ],
  "rooms": {"t-launch": {"f2": "read"}},
  "bots": {"cmo": {"f1": "read", "f2": "read"}}
}
```

This file becomes the only source. `local_folder` leaves `project.md`:

- A one-time, repeat-safe migration moves each existing link to `folders.json` with `read` access.
- New links default to `edit`.
- `osctl --local-folder` keeps working from the owner's own terminal and adds a folder. It stays
  refused inside a bot turn, as today. No other osctl command can change folders.
- Whatever reads `local_folder` today moves to the new source: the project index, `default_active`,
  `_workspace_deny_dirs`, TokenSave, the workspace form.

### Which folders a turn gets

Start with all the workspace's folders at their own access. Each layer that is set can only narrow:

1. The channel's entry, if it has one. A DM has no channel layer.
2. The bot's entry, if it has one.

A folder missing from a set layer is gone for that turn. Access is the lower of the two. An empty
layer `{}` means no folders. Unlinking a folder removes it from every layer and never widens one.

Then, on the actual turn:

- **Shared thread or runner turn:** no folders at all, as today. TokenSave's outline with no source
  code stays as it is.
- **Consulted teammate, app turn, or a thread that has used an app:** everything drops to `read`.
- **Turn that is not tagged:** everything drops to `read`. A new `may_edit` value decides this: true
  for a `/skill`, any `@bot`, any DM message, or a continuation of a tagged skill thread. It is
  separate from `may_write`, which keeps controlling approvals. Untagged turns can read and search,
  and the bot asks to be tagged to make a change.
- **A folder that no longer validates:** skipped for that turn. Chat never breaks over a stale link.
- **Web turns:** keep edit access. A fetched page could carry instructions, and that risk is accepted:
  fetches only reach hosts the owner typed, the never-write list still applies, and git shows the
  change. A test records this behaviour.

The existing tag gate on untagged save words stays as it is.

**Model.** A turn with `may_edit` and at least one `edit` folder uses the stronger model when its
message matches `_WRITE_TURN_RE` or a new edit-word pattern (`fix`, `change`, `edit`, `refactor`,
`rename`, `delete`, `remove`, `implement`, `patch`). The new pattern applies only to this choice, not
to the tag gate.

**CTO.** Add a `folder-work` skill owned by CTO and make it CTO's default skill, because `tokensave`
runs on the cheap model and stops when a folder has no index. Its wording says read the folder's
`CLAUDE.md` or `AGENTS.md` first and make focused changes. Any bot can edit on a tagged turn;
`folder-work` is not required for that.

**Saved threads.** Session scope becomes the workspace slug, and old `(slug, path)` scopes on saved
chats are accepted as that slug, so no thread loses its context on upgrade. A thread records the
folder paths it was given. When one of them has been unlinked or changed, the next turn starts a
fresh session with a recap of the visible messages, keeping today's protection. Adding a folder, or
changing access or a layer, starts nothing fresh: bots in a thread share one transcript, so a layer
limits what a bot can open now, not what an earlier turn already said.

**Invited people and old threads.** No new rule is needed. `rooms.chat_visible` already hides any
thread with a turn that ran with full tools, and every turn with folders is one. A test pins this.

`TurnPolicy.workspace_dir` and `file_search` are replaced by `read_dirs` and `edit_dirs`, tuples of
absolute paths. `edit_dirs` is a subset of `read_dirs`.

### What bots are told

A short block on any turn with folders: each folder's name, path and access, plus a rule to read
its `CLAUDE.md` or `AGENTS.md` before working there and to name every file changed in the reply.
File tool schemas now load on every private turn in a workspace with folders. That costs more
tokens than today's only-on-code-questions rule, and it is the price of always being able to search.

The bot's process keeps starting in the myOS folder on Claude Code and API-key engines. The osctl
permission rule and `--resume` both depend on that starting folder. Folder files are not injected
into the prompt.

Text that says linked folders are read-only changes: `linked_workspace_prompt`, `bots/cto.md`, API
tool descriptions, the workspace form help, `docs/sharing.md`, `docs/server.md`, the README.
`skills/tokensave` stays read-only.

### Files bots can never write

In any folder, on Claude Code and API-key engines:

- Everything in `SECRET_FILE_GLOBS`. These stay unreadable too.
- `.git/`, `.husky/`, `.claude/`, `.codex/`, `.agents/`, `.vscode/`
- `.mcp.json`, `.envrc`

A bot may edit `CLAUDE.md`, `AGENTS.md` and CI workflow files. It cannot push, so the owner sees
those changes first in their own git.

### Engines

**Claude Code.** One `--add-dir` per folder in `read_dirs`. For each folder in `edit_dirs`, add
`Edit` and `Write` to `--tools`, allow `Edit(//path/**)` and `Write(//path/**)`, and deny the
never-write list inside it. Keep `--restricted` and `--permission-mode default`. Omit `Edit` and
`Write` when `edit_dirs` is empty.

A folder removed by a layer gets no `--add-dir`, so it is refused. When it sits inside a folder the
turn keeps, it also gets a `Read`, `Edit` and `Write` deny rule for that turn. So layers are real
limits on this engine, not just focus.

Checked on real Claude Code on 2026-11-05, with these flags and the osctl-only Bash rule:

- **Worked:** `Write` to two edit folders. `echo x > file` inside an edit folder also worked, so the
  edit rules cover shell redirects.
- **Refused by `Write`, nothing on disk:** `.env`, `.git/hooks/`, a folder outside, a symlink
  pointing outside, a `read` folder, the working folder.
- **Refused by the shell, nothing on disk:** redirects and `cp` into `.git/`, redirects into `.env`,
  `.envrc`, a `read` folder and the working folder; `rm`, `mv`, `sed -i`, `mkdir`, `chmod`, `tee`,
  `python3`, `git`, `curl`.
- **Refused reads:** `Read` and `cat` on a folder with no `--add-dir`, `Read`, `cat`, `cat <` and
  `Grep` on a denied folder, `Read` and `cat` on `.env`.

**API-key engines.** `read_file` and `search_files` take all of `read_dirs`. New `write_file` and
`edit_file` tools, offered only when `edit_dirs` is not empty:

- The path must resolve, symlinks followed, inside an `edit_dirs` folder.
- It must not hit the never-write list or `is_private_path`.
- Writes are atomic and capped at `MAX_FILE_BYTES`.
- Only regular files can be replaced.

**Codex and ACP.** Unchanged: no edit access. Their turn block lists every folder as `read`.

### Workspace walls

`_workspace_deny_dirs` denies every other workspace's folders. A folder is left out of that list
when it is the same as, inside, or contains a folder this turn has. A deny rule would otherwise
block the owner's own link. HQ keeps today's behaviour of no deny list, and only gets edit access to
HQ's own folders.

`normalize_local_folder` keeps its checks and adds one: a folder inside the myOS checkout is
refused. Links like that from before this change count as stale after the migration.

### Screen

A Folders section on the workspace settings page, and on an equivalent page for HQ:

- A row per folder: name, path, Read/Edit switch, Open, Unlink.
- A row shows a warning when the folder is missing on this machine, when it is not in a git repo
  ("edits can't be undone easily"), and when myOS can't write to it.
- Add folder: the existing chooser on a Mac, a typed path on a server.
- Channels and bots: each shows "All folders" until limited, then per folder None, Read or Edit.

Layout is left to the builder, following existing form patterns.

All of it is owner-only. Reads and writes reject invited people. Folder paths and `folders.json`
content never reach them: extend `_no_local_folder` in osctl and check every project payload a
person can load. Over a tunnel, the Mac chooser opens on the Mac, as it does today.

### On a server

Nothing is special beyond the typed path. Folders must already be on the server. Paths synced from a
Mac show as missing until they are replaced. `docs/server.md` says to clone repos on the server and
link them there.

### TokenSave

`/tokensave` and the automatic graph context use the first folder of the turn that has an index.
Other folders are reached with file tools. `tokensave_bridge` takes the folder path resolved for the
turn instead of looking it up by slug.

## Tests that must exist

- Layer rules: narrowing, lower access wins, empty layer, unlink, DM skips the channel layer.
- Migration: read access, repeat-safe, a stale path, `local_folder` gone from `project.md`.
- `build_cmd` output for read-only, edit, mixed, shared, runner, consulted, app-turn and untagged.
- API tools: every refusal in the never-write list, symlink out, `..`, a pipe, oversized content,
  writing with no edit folders, a read folder.
- Walls: another workspace's folder refused on every engine that enforces it, same folder linked to
  two workspaces works, nested links work, a folder inside myOS refused.
- Invited people: no route or payload reveals a path, and settings routes refuse them.
- `osctl` inside a bot turn cannot change folders.
- Tag gate and `may_edit`: an untagged save word still stops; `/skill`, `@bot`, DM and continuation
  get edit access; an untagged turn does not; web turns keep it.
- Model choice for edit words, with `may_write` and approvals unchanged.
- Saved threads: old scopes keep their context, unlinking starts fresh, adding a folder does not.
- A thread with a folder turn stays hidden from an invited person who joins later.
- Codex and ACP never receive edit folders.
- A live probe on real Claude Code, skipped when it is not installed, like the existing live tests.

## Left to the builder

- The module layout. A small `core/folders.py` is the natural home for load, save, layers, migration.
- Route names and screen layout.
- Folder id generation and how a default name comes from the path.
- How the project index exposes folders.
