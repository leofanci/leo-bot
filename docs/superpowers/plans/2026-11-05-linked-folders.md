# Linked Folders Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a workspace link any number of folders, limit them per channel or per bot, and let bots on Claude Code and API-key engines edit them.

**Architecture:** A new `core/folders.py` owns storage, path checks, the layer rules and the never-write list. Each chat turn resolves to two tuples, `TurnPolicy.read_dirs` and `edit_dirs`, and each engine turns those into its own limits. `folders.json` replaces `local_folder` as the only source.

**Tech Stack:** Python 3 standard library, `unittest`, vanilla JS in `dashboard/app.js`, Claude Code CLI flags.

**Spec:** `docs/superpowers/specs/2026-11-05-linked-folders-design.md`

## Global Constraints

- Tracked files, fixtures and commit messages hold no real names. Fixtures use `demo`, `acme`, `profile-a`, `profile-b`.
- Run `python3 -m unittest tests.test_no_usage_data` before every commit.
- No `Co-Authored-By` trailer on commits. Branch `linked-folders`, PR, CI, then squash merge.
- No em dashes in tracked text.
- Never-write directories: `.git`, `.husky`, `.claude`, `.codex`, `.agents`, `.vscode`. Never-write files: `.mcp.json`, `.envrc`, plus everything in `SECRET_FILE_GLOBS`.
- Existing links migrate as `read`. New links default to `edit`.
- Shared threads, runner turns and invited people get no folders and no folder paths.
- Codex and ACP never receive edit folders.
- No free shell. The Claude Code Bash allow rule stays osctl-only.
- The bot process keeps starting in the Leo Bot folder.

## Review Focus

- The same folder linked to two workspaces, then unlinked from one: the other keeps access, and the first workspace's bots lose it. Pinned in Task 1.
- A folder path with a space, parentheses or glob characters: its secret-file deny rules must still match, or the link is refused. Pinned in Tasks 1 and 3.
- `edit_file` on a non-UTF-8 file, or with text that appears twice or not at all: refused, with the file unchanged. Pinned in Task 4.
- An HQ thread that used an HQ folder, then continued from a company workspace: never reused there. Pinned in Task 6.
- A hand-edited or damaged `folders.json`: bad rows dropped, unknown access treated as `read`, no crash, and the old `local_folder` is not migrated over it. Pinned in Task 1.

---

### Task 1: Folder store and rules

**Files:**
- Create: `core/folders.py`
- Modify: `dashboard/fileops.py:875-917` (`_inside_cloud_drive` and the checks of `normalize_local_folder` move out; the function stays as a wrapper)
- Test: `tests/test_folders.py`

**Interfaces:**
- Consumes: `core.bots.ws_dir(ws, root)`, `core.bots._atomic_write(path, text)`.
- Produces, all in `core/folders.py`:
  - `class FolderError(ValueError)`
  - `NEVER_WRITE_DIRS: tuple`, `NEVER_WRITE_FILES: tuple`, `READ`, `EDIT`
  - `normalize_path(value, root) -> str`: today's checks, plus refuses a path inside `root`, plus refuses `*?[]{}` anywhere in the path. Empty input returns `""`.
  - `load(ws, root) -> dict`: `{"folders": [...], "rooms": {...}, "bots": {...}}`, migrating `local_folder` first when no file exists.
  - `save(ws, data, root) -> None`
  - `add(ws, path, access=EDIT, root=...) -> dict`: returns the row; a path already linked returns its row unchanged.
  - `remove(ws, folder_id, root) -> None`, `set_access(ws, folder_id, access, root) -> None`
  - `set_limit(ws, kind, key, limit, root) -> None`: `kind` is `"rooms"` or `"bots"`; `limit` is `{folder_id: access}`, or `None` to clear.
  - `usable(row, root) -> bool`: `normalize_path` passes on the row's path.
  - `for_turn(ws, room_local, bot_id, root) -> list[dict]`: the layer rules. Rows carry the resolved `access`. Unusable folders are dropped. `room_local` is `None` for a DM.
  - `turn_denies(kept_paths, other_paths, own_removed_paths) -> tuple[str, ...]`: an other-workspace path is left out when it is the same as, inside or contains a kept path; an own removed path is left out when it is the same as or contains a kept path.
  - `all_paths(ws, root) -> list[str]`, `other_workspace_paths(ws, slugs, root) -> list[str]`: the second includes HQ's folders and returns `[]` when `ws` is HQ.
  - `may_write(path, base) -> bool`: false when any part of `path` below `base` is a never-write directory, or its name is a never-write file or matches `SECRET_FILE_GLOBS`.
  - `is_git_repo(path) -> bool`: `.git` exists in the folder or a parent. `first_indexed(rows) -> str | None`: the first path with `.tokensave/tokensave.db`.
- `SECRET_FILE_GLOBS` moves from `dashboard/engines/claude_code.py` to `core/folders.py`. `claude_code` re-exports it, so existing imports keep working.
- `fileops.normalize_local_folder(value)` calls `normalize_path(value, ROOT)` and converts `FolderError` to `ActionError`.

- [ ] **Step 1: Write the failing tests** in `tests/test_folders.py`, using a temp root with `projects/acme/project.md`:

```python
def test_migrates_local_folder_as_read_and_removes_the_line(self):
    rows = folders.load("acme", self.root)["folders"]
    self.assertEqual([(r["path"], r["access"]) for r in rows], [(str(self.app), "read")])
    self.assertNotIn("local_folder", (self.root / "projects/acme/project.md").read_text())

def test_migration_runs_once(self):               # a second load keeps ids and access
def test_migration_keeps_a_stale_path(self):      # row kept, usable() False
def test_new_links_default_to_edit(self):
def test_add_same_path_twice_keeps_one_row(self):
def test_refuses_inside_leobot_and_glob_characters(self):
    for bad in (self.root / "projects", self.tmp / "app[1]", self.tmp / "a*"):
        with self.assertRaises(folders.FolderError): folders.normalize_path(str(bad), self.root)

def test_layers_only_narrow(self):
    folders.set_limit("acme", "rooms", "t-launch", {"f1": "read", "f2": "edit"}, self.root)
    folders.set_limit("acme", "bots", "cmo", {"f2": "read"}, self.root)
    rows = folders.for_turn("acme", "t-launch", "cmo", self.root)
    self.assertEqual([(r["id"], r["access"]) for r in rows], [("f2", "read")])

def test_empty_layer_means_no_folders(self):
def test_dm_skips_the_channel_layer(self):        # room_local None
def test_unlink_removes_from_layers_and_never_widens(self):
def test_same_folder_two_workspaces_then_unlink(self):
    # linked to acme and demo: turn_denies keeps it open for both; after remove from
    # demo, other_workspace_paths("demo", ...) holds it and turn_denies denies it
def test_turn_denies_nested(self):
    kept, inner = "/w/app", "/w/app/vendor"
    self.assertEqual(folders.turn_denies([kept], [inner], []), ())
    self.assertEqual(folders.turn_denies([kept], [], [inner]), (inner,))
    self.assertEqual(folders.turn_denies([inner], [], [kept]), ())

def test_may_write_refuses_the_list(self):
    for rel in (".git/hooks/pre-commit", "sub/.git/config", ".env", ".envrc", ".mcp.json",
                ".vscode/tasks.json", ".claude/settings.json", ".husky/pre-push", "a/id_rsa"):
        self.assertFalse(folders.may_write(self.base / rel, self.base), rel)
    self.assertTrue(folders.may_write(self.base / "CLAUDE.md", self.base))
    self.assertTrue(folders.may_write(self.base / ".github/workflows/ci.yml", self.base))

def test_damaged_file_is_safe(self):
    # not JSON, a list, a relative path, unknown access, a row with no id:
    # no crash; bad rows dropped; unknown access reads as "read"; project.md's
    # local_folder is not migrated over an existing file
```

- [ ] **Step 2: Run it.** `python3 -m unittest tests.test_folders -v`. Expected: `ModuleNotFoundError: core.folders`.

- [ ] **Step 3: Implement `core/folders.py`** to the signatures above. Ids are the next free `f<N>`, never reused while a layer names them. A folder's default `name` is the path's last part. Lower access wins: `read` beats `edit`.

- [ ] **Step 4: Run it.** `python3 -m unittest tests.test_folders tests.test_fileops_crud -v`. Expected: PASS.

- [ ] **Step 5: Commit.**

```bash
git add core/folders.py dashboard/fileops.py dashboard/engines/claude_code.py tests/test_folders.py docs/superpowers/specs/2026-11-05-linked-folders-design.md docs/superpowers/plans/2026-11-05-linked-folders.md
git commit -m "Folder store: several folders per workspace, layers and write rules"
```

### Task 2: Move every reader off `local_folder`

**Files:**
- Modify: `dashboard/fileops.py:919-931,1000-1009,1024-1032`, `index.py:147,172`, `dashboard/db.py:65,89,145,187`, `core/bots.py:109`, `dashboard/osctl.py:134-140`, `dashboard/server.py:255-271,936-946,1975,2833`, `dashboard/tokensave_bridge.py:36-58,124-137`
- Test: every file in `tests/` that names `local_folder`, plus `tests/test_folders.py`

**Interfaces:**
- Consumes: Task 1.
- Produces:
  - Each project dict from `db.tree()` has `"folders": folders.load(slug, ROOT)["folders"]`, and no `local_folder`.
  - `fileops.create_project` and `update_project` return `{"slug", "folders"}`. A non-empty `local_folder` field calls `folders.add` with the default access. An empty one does nothing. `project.md` is never written with that line again.
  - `server.resolve_linked_workspace(projects, slug)` returns the first usable folder's path. It is a temporary bridge that Task 5 removes.
  - `tokensave_bridge.context(project_slug, task, *, active_project=None, include_code=True, folder=None)`: `folder` must be among the project's linked paths, else `BridgeError`. With no `folder`, it uses `first_indexed` of the project's rows.
- The `entities.local_folder` column stays and `index.py` writes `None`. No SQL migration.

- [ ] **Step 1: Write the failing tests.**

```python
# tests/test_folders.py
def test_project_tree_lists_folders(self):              # db.tree()[...]["folders"], no "local_folder" key
def test_default_active_turns_cto_on_with_any_folder(self):
def test_update_project_adds_and_never_rewrites_the_line(self):
# tests/test_security_review.py, next to the existing _emit test
def test_shared_osctl_never_shows_folders(self):
    # osctl._emit with {"project": {"folders": [{"path": "/srv/app"}]}} under LEOBOT_SHARED
    self.assertNotIn("/srv/app", out.getvalue())
def test_osctl_in_a_bot_turn_cannot_add_a_folder(self):  # LEOBOT_WORKSPACE set, --local-folder refused
```

- [ ] **Step 2: Run them.** `python3 -m unittest tests.test_folders tests.test_security_review -v`. Expected: the new tests FAIL.

- [ ] **Step 3: Implement.** Rename `_no_local_folder` to `_no_folder_paths`; it strips the keys `local_folder` and `folders`. `_workspace_deny_dirs` builds its other-workspace dirs from `folders.other_workspace_paths`. Replace `project_local_folder` with `folders` lookups and delete it. Keep `/api/project/<slug>/folder/open` working on the first folder until Task 7 replaces it.

- [ ] **Step 4: Update the listed tests' fixtures to the new shape.** Fixtures that write `local_folder:` in `project.md` keep working through migration, so change only assertions on the `local_folder` key, the returned shape, and links now reading as `read`.

- [ ] **Step 5: Run everything.** `python3 -m unittest discover -s tests 2>&1 | tail -4`. Expected: OK.

- [ ] **Step 6: Commit.** `git commit -am "folders.json replaces local_folder as the only source"`

### Task 3: Claude Code limits

**Files:**
- Modify: `dashboard/engines/base.py:23-34,89`, `dashboard/engines/claude_code.py:221,246-285`, `dashboard/chat_session.py:79-104`, `dashboard/server.py:2292-2293` (bridge), `dashboard/engines/api.py:108`, `dashboard/engines/api_tools.py:131,147-149,332,367` (bridge)
- Test: `tests/test_engine_folders.py` (create)

**Interfaces:**
- Consumes: `NEVER_WRITE_*`, `SECRET_FILE_GLOBS` from Task 1.
- Produces:
  - `TurnPolicy.read_dirs: tuple = ()` and `edit_dirs: tuple = ()`; `workspace_dir` and `file_search` removed.
  - `ChatSession.ask(text, with_web=False, model=None, read_dirs=(), edit_dirs=(), web_domains=(), apps_token=None, apps_port=None, os_token=None, os_port=None)`, and `_policy` and `_base_cmd` to match.
  - `"folder_edit"` added to `supports` on `ClaudeCodeEngine` and `ApiEngine`. `"file_search"` removed everywhere.
  - In this task `api_tools` reads `policy.read_dirs` in place of `workspace_dir` and offers `search_files` when `read_dirs` is not empty. Task 4 extends it.
  - The handler passes `read_dirs=(workspace_dir,)` on the turns that used file search, `edit_dirs=()`. Task 5 replaces this.

- [ ] **Step 1: Write the failing tests.**

```python
def test_read_folder_gets_add_dir_and_no_edit_tools(self):
    cmd = self.cmd(read_dirs=("/w/a",))
    self.assertEqual(self.flag_values(cmd, "--add-dir"), ["/w/a"])
    self.assertNotIn("Edit", self.flag_values(cmd, "--tools"))
    self.assertIn("Glob", self.flag_values(cmd, "--tools"))

def test_edit_folder_gets_scoped_rules(self):
    cmd = self.cmd(read_dirs=("/w/a", "/w/b"), edit_dirs=("/w/a",))
    allowed, denied = self.flag_values(cmd, "--allowedTools"), self.flag_values(cmd, "--disallowedTools")
    self.assertIn("Edit(//w/a/**)", allowed); self.assertIn("Write(//w/a/**)", allowed)
    self.assertNotIn("Edit(//w/b/**)", allowed)
    for rule in ("Write(//w/a/**/.git/**)", "Edit(//w/a/**/.envrc)", "Write(//w/a/**/.env)",
                 "Read(//w/b/**/.env)"):
        self.assertIn(rule, denied)
    self.assertIn("--restricted", cmd)

def test_shared_turn_drops_every_folder(self):   # shared=True: no --add-dir, no Edit, Write, Glob or Grep
def test_edit_rules_keep_bash_osctl_only(self):
    self.assertNotIn("Bash", self.flag_values(self.cmd(edit_dirs=("/w/a",), read_dirs=("/w/a",)), "--allowedTools"))
def test_deny_dirs_also_deny_edit_and_write(self):

@unittest.skipUnless(shutil.which("claude"), "Claude Code not installed")
class LiveClaudeFolders(unittest.TestCase):
    def test_real_limits(self):
        # temp folders: "edit one" and "My App (old)" (edit), "ro" (read), "outside", plus a symlink
        # to outside. One haiku turn runs each write with build_cmd's real argv.
        # Assert on disk: written in both edit folders; nothing at .env, .git/hooks/x, .envrc,
        # outside, via the symlink, in ro, or in the working folder.
```

- [ ] **Step 2: Run them.** `python3 -m unittest tests.test_engine_folders -v`. Expected: FAIL on the `read_dirs` keyword.

- [ ] **Step 3: Implement in `build_cmd`.** One `--add-dir` per `read_dirs` entry. Secret `Read` denies for each. `APP_TOOLS` when `read_dirs` is not empty. `Edit` and `Write` in `--tools` plus the scoped rules when `edit_dirs` is not empty. Rules use the `//<path>` form, with `**/<dir>/**` for never-write directories and `**/<name>` for files. Each `deny_dirs` entry also gets `Edit` and `Write` denies.

- [ ] **Step 4: Run them, including live.** Expected: PASS. If the `My App (old)` deny rule does not hold, add `(` and `)` to the refused characters in `normalize_path`, add that to `test_refuses_inside_leobot_and_glob_characters`, and rename the live fixture.

- [ ] **Step 5: Run the whole suite.** `python3 -m unittest discover -s tests 2>&1 | tail -4`. Expected: OK, after moving the `workspace_dir` and `file_search_mode` keywords in existing tests to the new names.

- [ ] **Step 6: Commit.** `git commit -am "Claude Code: several folders, scoped edit rules, never-write denies"`

### Task 4: API-key engine tools

**Files:**
- Modify: `dashboard/engines/api_tools.py`
- Test: `tests/test_engines_api.py` (extend `FileRules`, line 263)

**Interfaces:**
- Consumes: `TurnPolicy.read_dirs`, `edit_dirs`; `folders.may_write`.
- Produces: `write_file(raw_path, content, root, policy) -> str`, `edit_file(raw_path, old, new, root, policy) -> str`. Specs `WRITE_SPEC` and `EDIT_SPEC`, offered only when `edit_dirs` is not empty and the turn is not shared. `search_files` gains an optional `folder` argument and searches all `read_dirs` when it is absent, returning absolute paths.

- [ ] **Step 1: Write the failing tests.**

```python
def test_write_inside_edit_folder(self):            # file written; returns the path
def test_write_refusals(self):
    for path in (self.outside / "x", self.ro / "x", self.edit / ".env", self.edit / ".git/hooks/x",
                 self.edit / ".envrc", self.edit / "link/x", self.edit / "../x", self.root / "x"):
        with self.assertRaises(ToolError): write_file(str(path), "n", self.root, self.policy)
        self.assertFalse(path.resolve().exists() and path.read_text() == "n")
def test_write_refuses_a_pipe_and_oversized_content(self):
def test_write_is_atomic_and_keeps_mode(self):
def test_edit_refuses_missing_or_repeated_text_and_leaves_the_file(self):
def test_edit_refuses_non_utf8_and_leaves_the_file(self):
def test_tools_not_offered_without_edit_folders(self):
def test_tools_not_offered_on_a_shared_turn(self):
def test_deny_dirs_block_writes(self):
def test_read_reaches_every_read_dir_and_nothing_else(self):
```

- [ ] **Step 2: Run them.** `python3 -m unittest tests.test_engines_api -v`. Expected: the new tests FAIL.

- [ ] **Step 3: Implement.** Refuse unless all of these hold:
  - the path is absolute, or relative to the first edit folder
  - its resolved parent is inside one `edit_dirs` entry
  - `folders.may_write` is true
  - `is_private_path` is false
  - the target is missing or a regular file
  - content is within `MAX_FILE_BYTES`

  Write through `tempfile.mkstemp` in the same folder, then `os.replace`, keeping an existing file's mode. `edit_file` decodes strictly as UTF-8 and needs exactly one match. Parent directories are created inside the edit folder, after the checks pass.

- [ ] **Step 4: Run them.** Expected: PASS.

- [ ] **Step 5: Commit.** `git commit -am "API-key engines: write_file and edit_file with the same limits"`

### Task 5: Turn rules in the chat handler

**Files:**
- Modify: `dashboard/server.py:255-271,283-325,605-631,665-687,936-946,1990-1994,2050-2136,2235-2262,2291-2309`
- Create: `skills/folder-work/SKILL.md`
- Modify: `bots/cto.md`, `skills/tokensave/SKILL.md` (only "active project's linked app" wording)
- Test: `tests/test_server_ask.py`, `tests/test_teammates.py`, `tests/test_bots.py`

**Interfaces:**
- Consumes: `folders.for_turn`, `turn_denies`, `first_indexed`, `other_workspace_paths`; `ask(read_dirs, edit_dirs)`; `bridge.context(folder=)`.
- Produces in `server.py`:
  - `_turn_folders(ws, room, bot_id, *, locked, may_edit, drop_edit, engine) -> tuple[list[dict], tuple, tuple]`: rows for the block, `read_dirs`, `edit_dirs`. `locked` returns empty tuples. `edit_dirs` is empty unless `may_edit`, not `drop_edit`, and `"folder_edit" in engine.supports`.
  - `_may_edit(explicit, room, user_msg, team, sess) -> bool`: true for a tagged skill, any `team.mentioned(user_msg)`, a DM room, or `_continuation_allowed`.
  - `linked_workspace_prompt(project_slug, rows, *, graph_context="", graph_error=None)`: the block lists name, path and access. It adds the instruction to read the folder's `CLAUDE.md` or `AGENTS.md` before working there and to name every file changed in the reply. "Never edit the linked app" goes; read-only folders say read-only. It also says that a change needs a tag, when rows have `edit` access but this turn does not.
  - `_EDIT_WORD_RE`: `\b(fix|change|edit|refactor|rename|delete|remove|implement|patch)\b`, used only in `_pick_model`.
  - `_pick_model(skill, web, user_msg="", session=None, *, explicit=False, editing=False)`: `editing` returns `ESCALATED_MODEL` when `_WRITE_TURN_RE` or `_EDIT_WORD_RE` matches. It is checked before the `tokensave` branch.
- `drop_edit` is `bool(app_turn or apps_used)`. Web turns do not drop edit.
- `_needs_workspace` no longer decides file access; it still routes the `/tokensave` skill.
- A consulted teammate gets `_turn_folders` for `b2` with `may_edit=False`, and its prompt carries the block.
- The follow-up `run` at line 2309 passes the same `read_dirs` and `edit_dirs` as the lead.
- `sess.deny_dirs` becomes `folders.turn_denies(kept, other_workspace_paths(...), own_removed)` plus the existing fixed dirs from `_workspace_deny_dirs`, set after the turn's folders are known. HQ keeps an empty other-workspace list.
- TokenSave uses `first_indexed` of the layered rows before the lock drop, so the shared outline with no source stays.
- `bots/cto.md`: `skills: tokensave, folder-work`, `default_skill: folder-work`, and "Reads the code; changes it when tagged. Never pushes." New route for `folder-work` in `_SKILL_ROUTES` on `fix`, `refactor`, `implement`, `edit the code`.
- `skills/folder-work/SKILL.md`: read the folder's `CLAUDE.md` or `AGENTS.md` first; make focused changes; name every changed file; it cannot run tests or git, so say what the owner should run.

- [ ] **Step 1: Write the failing tests**, in the style of the existing fake-engine turns in `tests/test_server_ask.py`.

```python
def test_untagged_turn_reads_all_folders_and_edits_none(self):
    self.assertEqual(policy.read_dirs, (a, b)); self.assertEqual(policy.edit_dirs, ())
def test_tagged_dm_skill_and_continuation_can_edit(self):      # one subtest each, edit_dirs == (a,)
def test_untagged_save_word_still_hits_the_tag_gate(self):
def test_read_folder_never_in_edit_dirs(self):
def test_app_turn_and_apps_used_thread_drop_edit(self):
def test_web_turn_keeps_edit(self):
def test_shared_and_runner_turns_get_no_folders(self):
def test_codex_and_acp_never_get_edit_dirs(self):
def test_consulted_teammate_reads_only(self):
def test_followup_keeps_the_leads_folders(self):
def test_other_workspace_folder_in_deny_dirs_unless_also_linked(self):
def test_layer_removed_nested_folder_denied(self):
def test_stale_folder_skipped_and_chat_still_answers(self):
def test_block_lists_folders_and_is_absent_with_none(self):
def test_tokensave_stops_only_for_tokensave_without_index(self):
def test_cto_edit_turn_runs_with_no_index_on_the_strong_model(self):
def test_edit_word_picks_strong_model_only_when_editing(self):
def test_may_write_and_approvals_unchanged(self):
```

- [ ] **Step 2: Run them.** `python3 -m unittest tests.test_server_ask tests.test_teammates -v`. Expected: the new tests FAIL.

- [ ] **Step 3: Implement to the interfaces above.** Remove `resolve_linked_workspace` and `file_search_mode`. Set `may_edit` next to `may_write`, and leave `may_write` and its approval use as they are.

- [ ] **Step 4: Run the whole suite.** `python3 -m unittest discover -s tests 2>&1 | tail -4`. Expected: OK.

- [ ] **Step 5: Commit.** `git commit -am "Chat turns: folders on every private turn, edits on tagged turns"`

### Task 6: Saved threads

**Files:**
- Modify: `dashboard/server.py:853,1069-1110,1137-1156,1207-1210,1974-1983,2359`, `dashboard/chat_session.py:40-66`, `dashboard/chat_store.py:163-170`
- Test: `tests/test_server_ask.py:464-466` and neighbours, `tests/test_rooms_private.py`

**Interfaces:**
- Consumes: `folders.all_paths`, `chat_store.recap`.
- Produces:
  - `project_scope` becomes `(slug,)`; saved `scope` is `[slug]`.
  - `_scope_slug(scope) -> str | None`: reads old `[slug, path]` and new shapes. All scope comparisons in `get_chat_session` and `person_chat_session` go through it.
  - `ChatSession.folder_paths: set`, restored from `rec["folder_paths"]`, saved by `record_turn`, and added to when a turn gets `read_dirs`.
  - `get_chat_session(project_scope=None, chat_id=None, new=False, linked=None)`: when a reopened or active session has a `folder_paths` entry missing from `linked`, it starts a new `ChatSession` and sets `sess.pending_recap = chat_store.recap(rec)`, as a scope change does today. The old chat stays saved.
  - The handler puts `pending_recap` before the prompt once, then clears it.
  - The adopting branches of `get_chat_session` (the `rec_scope in (scope, None)` restore and `_CHAT_SCOPE is None`) refuse a session with non-empty `folder_paths`.
  - `_for_person` also strips `folder_paths`.

- [ ] **Step 1: Write the failing tests.**

```python
def test_old_scope_keeps_its_context(self):
    # a saved chat with scope ["acme", "/app-a"] reopens under ("acme",) with started True
def test_adding_a_folder_keeps_the_session(self):
def test_changing_access_or_a_layer_keeps_the_session(self):
def test_unlinking_a_used_folder_starts_fresh_with_a_recap(self):
    self.assertIsNot(second, first); self.assertIn("earlier message", second.pending_recap)
def test_hq_thread_with_folders_never_moves_to_a_company(self):
def test_thread_with_a_folder_turn_hidden_from_a_person_who_joins_later(self):
    self.assertFalse(rooms.chat_visible(rec, room, person))
def test_person_never_gets_folder_paths(self):       # _for_person output, list_meta
```

- [ ] **Step 2: Run them.** Expected: the new tests FAIL. Update line 464-466 to assert that `("acme",)` keeps the session.

- [ ] **Step 3: Implement to the interfaces above.**

- [ ] **Step 4: Run the whole suite.** `python3 -m unittest discover -s tests 2>&1 | tail -4`. Expected: OK.

- [ ] **Step 5: Commit.** `git commit -am "Saved threads: scope by workspace, fresh start when a used folder is unlinked"`

### Task 7: Folders screen, routes and guides

**Files:**
- Modify: `dashboard/server.py` (owner POST and GET sections near `/api/engines`, lines 1665 and 2571), `dashboard/app.js:205,558,938-970,3219-3250`, `dashboard/app.css`, `dashboard/engines/registry.py` or the Engines screen text in `dashboard/app.js:1375`
- Modify: `docs/sharing.md`, `docs/server.md`, `docs/engines.md`, `README.md`
- Test: `tests/test_folder_routes.py` (create)

**Interfaces:**
- Consumes: Task 1 functions; `pick_local_folder`, `open_local_folder`.
- Produces, all owner-only and same-origin like `/api/engines`:
  - `GET /api/folders?ws=<ws>`: `{"folders": [rows + {"missing","not_git","cant_write"}], "rooms": ..., "bots": ..., "channels": [...], "team": [...], "chooser": bool}`
  - `POST /api/folders/add {ws, path, access?}`, `/remove {ws, id}`, `/access {ws, id, access}`, `/limit {ws, kind, key, limit}`, `/open {ws, id}`
- Remove `/api/project/<slug>/folder/open`.
- Screen: `renderFolders(ws)` at route `/folders`, linked from the workspace settings page for a project and from the HQ rail next to Engines. `localFolderField` on new and edit project forms is replaced by a link to it. The new-project form keeps one optional folder field, which calls `folders.add` after creation.
- Row warnings, exact text: "Missing on this machine", "Not a git repo: edits can't be undone easily", "Leo Bot can't write here".
- Engines screen, on Codex and ACP rows, exact text: "Read-only in linked folders".

- [ ] **Step 1: Write the failing tests.**

```python
def test_owner_adds_limits_and_removes(self):
def test_invited_person_gets_403_on_every_folder_route(self):   # GET and each POST, subTest per route
def test_person_payloads_hold_no_paths(self):
    # walk every GET a person can load (_person_get): the temp folder path never appears
def test_row_flags(self):                 # missing, not_git, cant_write (chmod 500 folder)
def test_bad_access_or_unknown_id_is_400(self):
def test_add_refuses_a_folder_inside_leobot(self):
def test_server_mode_has_no_chooser(self):
def test_bot_turn_token_cannot_reach_folder_routes(self):
```

- [ ] **Step 2: Run them.** `python3 -m unittest tests.test_folder_routes -v`. Expected: FAIL with 404.

- [ ] **Step 3: Implement the routes and the screen,** following `renderEngines` patterns. Channels and bots each show "All folders" until limited, then per folder None, Read or Edit.

- [ ] **Step 4: Update the guides.**
  - `docs/sharing.md`: threads where a bot used folders stay hidden from invited people.
  - `docs/server.md:61`: clone repos on the server and link them there; synced paths show as missing.
  - `docs/engines.md`: Codex and ACP are read-only in linked folders.
  - `README.md`: one short section on folders, access, and that bots cannot run tests or git.

- [ ] **Step 5: Run the tests.** `python3 -m unittest tests.test_folder_routes tests.test_no_usage_data -v`. Expected: PASS.

- [ ] **Step 6: Check it in the real app** on a throwaway copy with `demo` data, in Mac and server modes. Link two folders, set one to Read, limit a bot and a channel, unlink one, and take screenshots. Then run one tagged turn on real Claude Code that creates a file in the Edit folder, and one untagged turn that does not.

- [ ] **Step 7: Commit.** `git commit -am "Folders screen, owner routes and guides"`

### Task 8: Finish

- [ ] **Step 1: Run everything.** `python3 -m unittest discover -s tests 2>&1 | tail -4`. Expected: OK.
- [ ] **Step 2: Whole-branch review** against the spec's anti-goals, one by one.
- [ ] **Step 3: Push and open the PR.** The body ends with the Claude Code line. Wait for CI and CodeQL, resolve Copilot comments, squash merge, pull `main`, prune.
- [ ] **Step 4: Update memory** `engines-layer.md`: folders shipped, what was checked live, shell and moving data still open.
