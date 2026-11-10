# Install and Updates Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** One-command install on macOS and Debian/Ubuntu, code and data split under `~/.leobot`, versioned releases, and owner-triggered updates with backup, migration, health check and rollback.

**Architecture:** A new `core/paths.py` owns every location (`CODE`, `HOME`, `DATA`, `RUN`, `LOGS`, `BACKUPS`). Existing modules already take a data `root` argument whose default is a module `ROOT`; those defaults become `paths.DATA`, while tracked files (`bots/`, `skills/`, `generate.py`, `index.py`) stay under `paths.CODE`. A stdlib-only `leobot.py` CLI plus `core/datadir.py`, `core/version.py`, `core/service.py` and `core/updater.py` do the install-time and update-time work; `install.sh` bootstraps it.

**Tech Stack:** Python 3.10+ stdlib only, `unittest`, POSIX shell (`bash`), git, launchd / systemd user units, Swift (existing `LeoBot.app`).

**Spec:** `docs/superpowers/specs/2026-11-10-install-and-updates-design.md`

## Global Constraints

- Python stdlib only. No new dependencies, no `pip install`.
- Python floor: 3.10.
- `HOME` = `$LEOBOT_HOME` or `~/.leobot`; `HOME` and `DATA` mode 700; token files mode 600.
- No `sudo` anywhere on macOS.
- Stable channel only ever checks out `vX.Y.Z` tags; main channel checks out `origin/main`.
- Keep the last 5 `pre-*` update backups.
- Health check timeout: 60 s.
- First release: `0.1.0`.
- Tracked files never contain the repository's hosting account name (the privacy guard derives it from `git remote` and fails on it). `install.sh` carries the placeholder `__LEOBOT_REPO_URL__`; `scripts/release.sh` fills it from `git remote get-url origin` into a release asset. Docs link with the relative `../../releases/latest`.
- Any `https://` host written into a tracked file must be in `PUBLIC_NETWORK_HOSTS` in `tests/test_no_usage_data.py` (`github.com` and `claude.ai` already are). Name other sites in plain text (for example "python.org") or add the host there with a comment saying where it is used.
- No release is published in this work: no tags pushed, no GitHub Release created, `scripts/release.sh` is written and tested but not run.
- Public repo: fixtures use generic slugs only (`demo`, `acme`, `profile-a`). No names of other software projects in tracked files, commits or PRs. Run `python3 -m unittest tests.test_no_usage_data` before every commit (it scans committed files, so also run it after each commit).
- Bot working directory stays `CODE` (so `python3 dashboard/osctl.py` keeps working and existing Claude sessions keyed by cwd still resume); bots reach data only through osctl, as today.
- Update controls are owner-only: never for invited people, shared threads, bots, runners or the relay.
- Copy style in user-facing strings: no em dashes.
- Test command for the whole suite: `python3 -m unittest discover -t . -s tests`.

## Review Focus

1. **A test or a dev run touching the real `~/.leobot`.** Expected: impossible; `tests/__init__.py` pins `LEOBOT_HOME` to a temp dir before any import, and a guard test proves it. Pinned in Task 1.
2. **Server started twice against one `DATA` (dev clone plus installed copy, or app plus `leobot start`).** Expected: second start exits with a message naming the running pid and port. Pinned in Task 3 (`test_second_start_refused`).
3. **Update interrupted mid-way (network drop during fetch, health check never passes).** Expected: previous code running, data as before, both backup paths printed. Pinned in Task 8 (`test_fetch_failure_leaves_old_code`, `test_failed_health_rolls_back_and_restores`).
4. **Disk path with spaces or non-ASCII (home folder named like `Ana María`, with a space and an accent).** Expected: install, move, backup, service files all work. Pinned in Task 4 (`test_move_with_space_in_home`) and Task 10 (dry run with a spaced `HOME`).
5. **Legacy data present in the clone while `DATA` already holds data (owner ran the installer first, then the clone).** Expected: refuse to start, never merge or overwrite, explain choices. Pinned in Task 4 (`test_both_sides_have_data_refuses`).

---

### Task 1: `core/paths.py` and test isolation

**Files:**
- Create: `core/paths.py`
- Modify: `tests/__init__.py`
- Test: `tests/test_paths.py`

**Interfaces:**
- Produces:
  - `paths.CODE: Path` (repo/app checkout: `Path(__file__).resolve().parent.parent`)
  - `paths.HOME: Path` (`Path(os.environ["LEOBOT_HOME"]).expanduser()` if set and non-empty, else `Path.home() / ".leobot"`), resolved once at import
  - `paths.DATA = HOME / "data"`, `paths.RUN = HOME / "run"`, `paths.LOGS = HOME / "logs"`, `paths.BACKUPS = HOME / "backups"`, `paths.SETTINGS = HOME / "leobot.json"`
  - `paths.TOKEN_FILE = RUN / "auth-token"`, `paths.PID_FILE = RUN / "server.pid"`, `paths.SERVER_LOG = LOGS / "server.log"`, `paths.UPDATE_LOG = LOGS / "update.log"`
  - `paths.home_for(env: Mapping[str, str]) -> Path` (pure, used by tests)
  - `paths.is_installed() -> bool`: `CODE.resolve() == (HOME / "app").resolve()`
  - `paths.ensure_dirs() -> None`: creates `HOME`, `DATA`, `RUN`, `LOGS`, `BACKUPS` with mode 700 (chmod existing ones too)

- [ ] **Step 1: Write failing tests** in `tests/test_paths.py`:
  - `test_default_home`: `paths.home_for({})` equals `Path.home() / ".leobot"`.
  - `test_env_override`: `paths.home_for({"LEOBOT_HOME": "~/x"})` equals `Path.home() / "x"`.
  - `test_empty_env_ignored`: `paths.home_for({"LEOBOT_HOME": ""})` equals default.
  - `test_ensure_dirs_modes`: after `ensure_dirs()` on a temp `HOME` (reload module with env), each dir exists and `stat().st_mode & 0o777 == 0o700`.
  - `test_tests_never_use_real_home`: `paths.HOME != Path.home() / ".leobot"` and `str(paths.HOME).startswith(tempfile.gettempdir())` or resolved equivalent.
  - `test_is_installed_false_in_clone`: `paths.is_installed()` is `False`.
- [ ] **Step 2:** Run `python3 -m unittest tests.test_paths -v`. Expected: FAIL, `No module named 'core.paths'`.
- [ ] **Step 3:** Implement `core/paths.py`. In `tests/__init__.py`, before anything else: create `tempfile.mkdtemp(prefix="leobot-test-home-")`, set `os.environ["LEOBOT_HOME"]` to it, register `atexit` removal.
- [ ] **Step 4:** Run `python3 -m unittest tests.test_paths -v`. Expected: PASS.
- [ ] **Step 5:** Commit: `git add core/paths.py tests/__init__.py tests/test_paths.py && git commit -m "Add core.paths: one place for code, home and data folders"`

### Task 2: Data roots move to `paths.DATA`, code roots to `paths.CODE`

**Files:**
- Modify: `core/bots.py:25,64`, `core/ws_logos.py:12`, `dashboard/db.py:15-16`, `dashboard/engines/config.py:21`, `dashboard/fileops.py:100,125,392,433,598`, `dashboard/osctl.py:148,796`, `dashboard/runner.py:22`, `hooks/reindex.py:18,40`, `generate.py` (default `--workspace`), `index.py` (default root arg)
- Test: `tests/test_paths_wiring.py`; update the 19 `fileops.ROOT = …` test sites only if they break (they keep working: `fileops.ROOT` stays the data-root attribute name)

**Interfaces:**
- Consumes: `paths.CODE`, `paths.DATA` (Task 1).
- Produces: unchanged public names. `bots.ROOT`, `fileops.ROOT`, `engines.config.ROOT`, `ws_logos.ROOT`, `db.ROOT` now equal `paths.DATA`. `db.DB_PATH = paths.DATA / "database" / "data" / "os.db"`. `bots.load_defs(root=paths.CODE)` (definitions ship with code). `fileops` code scripts use `paths.CODE / "generate.py"` and `paths.CODE / "index.py"` and pass `--workspace str(ROOT)`.

- [ ] **Step 1: Write failing tests** in `tests/test_paths_wiring.py`:
  - `test_data_roots_point_at_data`: for each of `bots.ROOT`, `fileops.ROOT`, `engine_config.ROOT`, `ws_logos.ROOT`, `db.ROOT`: `== paths.DATA`.
  - `test_db_path_under_data`: `db.DB_PATH == paths.DATA / "database" / "data" / "os.db"`.
  - `test_bot_defs_from_code_with_empty_data`: with `paths.DATA` empty, `bots.load_defs()` returns the shipped ids (`{"cos", "cmo", "cto", "gtm", "product", "research", "social"}` ⊆ keys).
  - `test_generate_script_from_code`: patch `subprocess.run`/`Popen` in fileops, trigger one `generate.py` call path (`generate_brief` helper used at `fileops.py:392`), assert argv[1] `== str(paths.CODE / "generate.py")` and `--workspace` value `== str(fileops.ROOT)`.
  - `test_no_module_defines_its_own_root`: scan `core/**/*.py`, `dashboard/**/*.py`, `hooks/*.py`, `generate.py`, `index.py` for the regex `Path\(__file__\)\.resolve\(\)\.parent\.parent(\.parent)?\s*$` assigned to a name other than inside `core/paths.py`; allowed only for `sys.path` setup lines that also reference `paths` or are in `dashboard/server.py`/`dashboard/runner.py` before `core` is importable (list them explicitly in the test).
- [ ] **Step 2:** Run `python3 -m unittest tests.test_paths_wiring -v`. Expected: FAIL on root equality.
- [ ] **Step 3:** Rewire each module listed. Audit every remaining `ROOT /` and `root /` use in non-test code: tracked folders (`bots`, `skills`, `templates`, `prompts`, `generate.py`, `index.py`, `dashboard/*`) go to `paths.CODE`; everything else is data. `osctl.py:148` (read-file confinement) and `:796` confine to `paths.DATA`.
- [ ] **Step 4:** Run the full suite `python3 -m unittest discover -t . -s tests`. Expected: all pass. Fix tests that hard-code repo-relative data paths by pointing them at the temp `fileops.ROOT` they already set.
- [ ] **Step 5:** Commit: `git commit -am "Data lives under the home folder, code paths stay in the checkout"`

### Task 3: Server: token, log, pid lock, bot deny lists, `/healthz` version

**Files:**
- Modify: `dashboard/server.py:36-37,81,120,397,1015-1039,3388-3450`, `dashboard/engines/claude_code.py:113-122`, `core/folders.py:49` (and the refuse-to-link check), `core/version.py` (created in Task 5; until then read `CODE / "VERSION"` via a stub function `version.current()` returning `"0.0.0"` if missing; Task 5 replaces it)
- Test: `tests/test_server_home.py`

**Interfaces:**
- Consumes: `paths.*` (Task 1).
- Produces:
  - `server.CODE = paths.CODE`, `server.ROOT = paths.DATA`; `SKILLS_DIR = paths.CODE / "skills"`; `ChatSession(repo_dir=str(paths.CODE))`; terminal cwd `paths.CODE`.
  - `server.TOKEN_FILE = paths.TOKEN_FILE`; `OWNER_TOKEN_FILE = paths.DATA / "team" / "owner-token"`.
  - `server.acquire_pid_lock(port: int) -> None`: writes `{"pid": os.getpid(), "port": port}` JSON to `paths.PID_FILE` (mode 600); if the file names a live pid whose `/healthz` proof verifies, `sys.exit("Leo Bot is already running (pid N, port P).")`; stale file is replaced. Released in `_shutdown_children`.
  - `/healthz` response adds `"version": version.current()`.
  - `main()` calls `paths.ensure_dirs()`, then `datadir.prepare()` (Task 4; no-op stub until then), then the re-index with `[sys.executable, str(paths.CODE / "index.py"), str(paths.DATA)]`.
  - When stdout is not a TTY, `main()` appends to `paths.SERVER_LOG` (the app and service redirect there).
  - Deny lists: `_workspace_deny_dirs` adds `paths.RUN`, `paths.LOGS`, `paths.BACKUPS` and keeps `paths.CODE / "private"`, `.remember`, `.superpowers`, `.tokensave`; data dirs become `paths.DATA / …`. `_workspace_deny_files` uses `db.DB_PATH` plus any legacy db files under `CODE` and `paths.SETTINGS`. `claude_code.private_read_rules` denies `Read(/{paths.LOGS}/**)`, `Read(/{paths.RUN}/**)`, `Read(/{paths.BACKUPS}/**)` on every turn, HQ included.
  - `folders.normalize_path` refuses a folder that is, contains or sits inside `paths.HOME` or `paths.CODE` (message: "Leo Bot's own folders can't be linked.").

- [ ] **Step 1: Write failing tests** in `tests/test_server_home.py`:
  - `test_token_file_in_run`: `server.TOKEN_FILE == paths.RUN / "auth-token"`; after `write_token_file()`, mode `0o600`.
  - `test_second_start_refused`: write a pid file naming `os.getpid()` and patch the healthz probe to verify; `acquire_pid_lock(8765)` raises `SystemExit` whose message contains `"already running"`.
  - `test_stale_pid_replaced`: pid file naming pid `999999` (not alive); `acquire_pid_lock` succeeds and file holds `os.getpid()`.
  - `test_deny_dirs_cover_home_parts`: for a project workspace, `_workspace_deny_dirs` output contains `str(paths.RUN)`, `str(paths.LOGS)`, `str(paths.BACKUPS)`, `str(paths.DATA / "chats")`.
  - `test_private_read_rules_cover_logs_and_run` (HQ turn): rules include `Read(/{paths.LOGS}/**)` and `Read(/{paths.RUN}/**)`.
  - `test_cannot_link_home_or_code`: `folders.normalize_path(str(paths.HOME))`, `(str(paths.DATA / "x"))` (created), `(str(paths.CODE))` each raise the module's existing validation error.
  - `test_healthz_reports_version`: GET `/healthz?nonce=…` via the existing test-server helper returns JSON with `"version"`.
- [ ] **Step 2:** Run `python3 -m unittest tests.test_server_home -v`. Expected: FAIL.
- [ ] **Step 3:** Implement as in Interfaces. Update `tests/test_chat_session.py`, `tests/test_engines_api.py`, `tests/test_osctl_walls.py`, `tests/test_security_boundaries.py` sites that reference `dashboard/.auth-token` so they check the new location and still assert the old relative name stays denied (legacy clones).
- [ ] **Step 4:** Run the full suite. Expected: all pass.
- [ ] **Step 5:** Commit: `git commit -am "Server keeps token, pid and log in the home folder; bots never read them"`

### Task 4: Data marker, migrations, one-time legacy move

**Files:**
- Create: `core/datadir.py`, `core/migrations.py`
- Modify: `dashboard/server.py` (`main()` calls `datadir.prepare()`)
- Test: `tests/test_datadir.py`

**Interfaces:**
- Consumes: `paths.*`.
- Produces:
  - `datadir.MARKER = ".leobot-data"`; marker JSON `{"schema": int, "created": iso8601}`.
  - `datadir.LEGACY_ITEMS: tuple[str, ...] = ("projects", "portfolio", "chats", "team", "database/data", "database/os.db", "os.db", "dashboard/os.db", "dashboard/data.db", "dashboard/.auth-token", "dashboard/server.log")`; `projects` counts only if it holds anything besides `.gitkeep`.
  - `datadir.legacy_items(source: Path) -> list[str]`
  - `datadir.data_is_empty(data: Path) -> bool` (nothing but the marker)
  - `datadir.move_legacy(source: Path, data: Path = paths.DATA, *, replace: bool = False) -> MoveResult` where `MoveResult` is a dataclass `(moved: list[str], backup: Path | None, refused: str | None)`. Order: tar.gz backup of legacy items to `paths.BACKUPS / f"legacy-{stamp}.tar.gz"` → `shutil.move` each item to the same relative path under `data` (`dashboard/.auth-token` and `dashboard/server.log` are dropped, not moved: new ones live in `RUN`/`LOGS`) → write marker with schema 1. If `data` not empty and not `replace`: return `refused` text naming both folders and `leobot doctor`, move nothing. If `replace`: first tar.gz `data` to `paths.BACKUPS / f"replaced-{stamp}.tar.gz"`, empty it, then move.
  - `datadir.prepare() -> None`: `paths.ensure_dirs()`; `r = move_legacy(paths.CODE)` if `legacy_items(paths.CODE)` else ensure marker; on `refused` → `sys.exit(r.refused)`; then `migrations.run(paths.DATA)`.
  - `migrations.MIGRATIONS: list[Callable[[Path], None]]` (index 0 = schema 1 = the move, no-op function), `migrations.LATEST = len(MIGRATIONS)`.
  - `migrations.run(data: Path) -> int`: reads marker schema; `> LATEST` → `sys.exit("Your data is from a newer Leo Bot (schema N). Run: leobot update")`; runs pending functions in order, writes schema after each; returns final schema.

- [ ] **Step 1: Write failing tests** in `tests/test_datadir.py` (fixture: temp `source` with `projects/demo/project.md`, `chats/x.json`, `os.db`, `projects/.gitkeep`):
  - `test_no_legacy_creates_marker`: empty source → marker exists, schema 1, nothing moved.
  - `test_gitkeep_only_is_not_legacy`: source with only `projects/.gitkeep` → `legacy_items == []`.
  - `test_moves_into_empty_data`: items present under `data`, gone from source, backup tar exists and lists `projects/demo/project.md`.
  - `test_marker_alone_does_not_block`: `data` holds only the marker → move happens.
  - `test_both_sides_have_data_refuses`: `data/projects/acme/project.md` exists → `refused` set, source untouched, `data` untouched.
  - `test_replace_backs_up_then_moves`: same setup with `replace=True` → `replaced-*.tar.gz` contains `projects/acme/project.md`, `data/projects/demo` exists, `data/projects/acme` gone.
  - `test_token_and_log_not_moved`: `dashboard/.auth-token` removed from source, absent under `data`.
  - `test_move_with_space_in_home`: `LEOBOT_HOME` temp path containing `"Ana María"` works end to end.
  - `test_migrations_run_in_order_and_idempotent`: register two fake migrations appending to a list; run twice → list `[2, 3]` once; marker schema 3.
  - `test_newer_schema_refuses`: marker schema `LATEST + 1` → `SystemExit` with "newer Leo Bot".
- [ ] **Step 2:** Run `python3 -m unittest tests.test_datadir -v`. Expected: FAIL, module missing.
- [ ] **Step 3:** Implement. Use `tarfile` (gz) and `shutil.move`; write the marker last so a crash mid-move leaves legacy detection true on next start.
- [ ] **Step 4:** Run `python3 -m unittest tests.test_datadir -v` then the full suite. Expected: PASS.
- [ ] **Step 5:** Commit: `git commit -m "Move existing data to the home folder once, with a backup; numbered data migrations"`

### Task 5: Versions and releases

**Files:**
- Create: `VERSION` (content `0.1.0\n`), `core/version.py`, `scripts/release.sh`
- Modify: `dashboard/server.py` (replace Task 3 stub import)
- Test: `tests/test_version.py`

**Interfaces:**
- Produces:
  - `version.current() -> str` (reads `paths.CODE / "VERSION"`, stripped; `"0.0.0"` if missing)
  - `version.parse(tag: str) -> tuple[int, int, int] | None` (accepts `v1.2.3` and `1.2.3`; anything else `None`, including pre-release suffixes)
  - `version.latest_tag(ls_remote_output: str) -> str | None` (parses `git ls-remote --tags` lines, ignores `^{}` peeled lines and non-semver tags, returns highest `vX.Y.Z`)
  - `version.newer(a: str, b: str) -> bool` (b newer than a)
- `scripts/release.sh X.Y.Z [--dry-run]`: refuses unless on `main`, clean tree, `X.Y.Z` is semver and greater than `VERSION`, and `gh run list --branch main --limit 1 --json conclusion` reports `success`; writes `VERSION`, commits `Release vX.Y.Z`, tags `vX.Y.Z`, pushes commit and tag, renders `install.sh` with `__LEOBOT_REPO_URL__` replaced by the origin URL converted to `https://github.com/<account>/<repo>.git` into a temp file, `gh release create vX.Y.Z --generate-notes <tempfile>#install.sh`. `--dry-run` prints each step and the rendered installer's repo line, changes nothing.
- `scripts/render-installer.sh OUT`: the render step on its own (used by `release.sh` and by tests).

- [ ] **Step 1: Write failing tests**:
  - `test_parse`: `parse("v1.2.3") == (1, 2, 3)`, `parse("1.2.3") == (1, 2, 3)`, `parse("v1.2") is None`, `parse("v1.2.3-beta") is None`.
  - `test_latest_tag`: input lines for `v0.1.0`, `v0.10.0`, `v0.9.9`, `v0.10.0^{}`, `nightly` → `"v0.10.0"`; empty input → `None`.
  - `test_newer`: `newer("0.1.0", "v0.1.1")` True, `newer("0.2.0", "v0.1.9")` False, `newer("0.1.0", "v0.1.0")` False.
  - `test_current_reads_version_file`: equals contents of `VERSION`.
- [ ] **Step 2:** Run `python3 -m unittest tests.test_version -v`. Expected: FAIL.
- [ ] **Step 3:** Implement module, `VERSION`, `scripts/release.sh` (`set -euo pipefail`, `chmod +x`).
- [ ] **Step 4:** Run tests and `bash -n scripts/release.sh`. Expected: PASS, no syntax error. Do not run `release.sh` without `--dry-run`.
- [ ] **Step 5:** Commit: `git commit -m "Version file, semver helpers and a release script"`

### Task 6: `leobot` command (everything but service and update)

**Files:**
- Create: `leobot.py` (repo root)
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: `paths`, `datadir`, `version`.
- Produces: `leobot.main(argv: list[str]) -> int` with subcommands:
  - `open` (default): if not running (`status_info()["running"]` false) → `start`; then on macOS open `~/Applications/LeoBot.app` if it exists else `open <login URL>`; on Linux print the login URL.
  - `start [--port N]`: spawns `sys.executable str(paths.CODE/"dashboard/server.py") --port N` detached (`start_new_session=True`), stdout/stderr appended to `paths.SERVER_LOG`; waits up to 20 s for healthz proof; prints URL. Default port 8765.
  - `stop`: SIGTERM pid from `paths.PID_FILE`, wait 10 s, then SIGKILL; only if healthz proof (or the pid's command line contains `dashboard/server.py`).
  - `status`: running, pid, port, version, home dir, data dir, service state; `--json` flag.
  - `version`: prints `version.current()`.
  - `doctor`: one line per check, `OK`/`FIX` + fix command: python ≥ 3.10, git, claude on PATH, `HOME`/`DATA` modes 700, marker present, schema vs `LATEST`, legacy data in `paths.CODE` (and in `~/leo-bot` if that exists) with the exact `leobot move-data --from` command, port free or ours, latest version (from `leobot.json` cache, no network).
  - `backup [--restore FILE] [--yes]`: create `paths.BACKUPS / f"manual-{stamp}.tar.gz"` of `DATA`; `--restore` stops server, snapshots current `DATA` to `pre-restore-{stamp}.tar.gz`, empties `DATA`, extracts FILE (refuse members with absolute paths or `..`), starts server if it was running. Prompts unless `--yes`.
  - `move-data --from DIR [--replace]`: stops server if running, `datadir.move_legacy(Path(DIR), replace=…)`, prints result, re-indexes.
  - `uninstall [--data] [--yes]`: stops server, `service.uninstall()` (Task 7; guarded import), removes `paths.HOME / "app"`, `~/.local/bin/leobot`, `~/Applications/LeoBot.app`; removes `HOME` entirely only with `--data`; prompts unless `--yes`.
- `leobot.status_info() -> dict` keys: `running: bool, pid: int|None, port: int|None, version: str, home: str, data: str, installed: bool`.

- [ ] **Step 1: Write failing tests** (all with temp `LEOBOT_HOME`, `subprocess` and `os.kill` patched where they would touch real processes):
  - `test_version_prints`: `main(["version"])` prints `version.current()`, returns 0.
  - `test_status_not_running`: no pid file → `status_info()["running"] is False`.
  - `test_backup_creates_archive`: file in `DATA/projects/demo/x.md` → one `manual-*.tar.gz` holding it.
  - `test_restore_snapshots_first`: restore from an archive → `pre-restore-*.tar.gz` exists and `DATA` matches archive.
  - `test_restore_rejects_path_traversal`: archive with member `../evil` → non-zero exit, `DATA` untouched.
  - `test_move_data_refuses_when_both_have_data`: exit code 1 and message from `MoveResult.refused`.
  - `test_doctor_reports_legacy`: legacy fixture in a temp `--from` path → output contains `leobot move-data --from`.
  - `test_uninstall_keeps_data_without_flag`: `--yes` removes fake `HOME/app`, leaves `DATA`.
  - `test_start_spawns_server_from_code`: patched `Popen` argv contains `str(paths.CODE / "dashboard" / "server.py")`.
- [ ] **Step 2:** Run `python3 -m unittest tests.test_cli -v`. Expected: FAIL.
- [ ] **Step 3:** Implement with `argparse` subparsers.
- [ ] **Step 4:** Run tests + full suite. Expected: PASS.
- [ ] **Step 5:** Commit: `git commit -m "leobot command: open, start, stop, status, doctor, backup, move-data, uninstall"`

### Task 7: Background service (launchd agent, systemd user unit)

**Files:**
- Create: `core/service.py`
- Modify: `leobot.py` (`service install|uninstall|status`)
- Test: `tests/test_service.py`

**Interfaces:**
- Produces:
  - `service.LABEL = "leobot.server"`; macOS plist path `~/Library/LaunchAgents/leobot.server.plist`; Linux unit `~/.config/systemd/user/leobot.service`.
  - `service.launchd_plist(python: str, code: Path, port: int, home: Path) -> str`: `ProgramArguments` = python, `code/dashboard/server.py`, `--port`, port; `RunAtLoad` true, `KeepAlive` true, `AbandonProcessGroup` true, `StandardOutPath`/`StandardErrorPath` = `paths.SERVER_LOG`, `EnvironmentVariables` with `LEOBOT_HOME` and a `PATH` that includes `~/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin`.
  - `service.systemd_unit(python: str, code: Path, port: int, home: Path, server: bool) -> str`: `ExecStart`, `Restart=always`, `RestartSec=3`, `Environment=LEOBOT_HOME=…`, `Environment=PATH=…`, `NoNewPrivileges=true`, `PrivateTmp=true`, and `Environment=LEOBOT_DEPLOYMENT=server` when `server` is true; `[Install] WantedBy=default.target`.
  - `service.install(port: int = 8765, server: bool = False) -> str` (writes file, `launchctl bootstrap gui/$UID …` / `systemctl --user daemon-reload && systemctl --user enable --now leobot`; returns a one-line message; Linux adds the `loginctl enable-linger $USER` hint when linger is off)
  - `service.uninstall() -> None`, `service.status() -> dict` (`installed: bool, running: bool, kind: "launchd"|"systemd"|None`)
  - `service.restart() -> None` (used by Task 8)
- Quote every path in unit/plist output correctly for spaces (plist is XML-escaped; systemd `ExecStart` arguments double-quoted).

- [ ] **Step 1: Write failing tests** (pure functions; `subprocess.run` patched for install):
  - `test_plist_contents`: `plistlib.loads(launchd_plist(...))` has `Label == "leobot.server"`, `KeepAlive is True`, `AbandonProcessGroup is True`, `EnvironmentVariables["LEOBOT_HOME"]` equal to the given home, `ProgramArguments[1]` ends with `dashboard/server.py`.
  - `test_unit_contents`: unit text contains `Restart=always`, `NoNewPrivileges=true`, `Environment=LEOBOT_HOME=`; contains `LEOBOT_DEPLOYMENT=server` only when `server=True`.
  - `test_unit_quotes_spaces`: code path `"/tmp/Ana María/app"` appears double-quoted in `ExecStart`.
  - `test_install_calls_launchctl_on_mac`: patched `platform.system` → `"Darwin"`; argv of the patched run includes `launchctl` and `bootstrap`.
  - `test_install_calls_systemctl_user_on_linux`: argv includes `systemctl`, `--user`, `enable`, `--now`.
- [ ] **Step 2:** Run `python3 -m unittest tests.test_service -v`. Expected: FAIL.
- [ ] **Step 3:** Implement; wire `leobot service …`.
- [ ] **Step 4:** Run tests + full suite. Expected: PASS.
- [ ] **Step 5:** Commit: `git commit -m "leobot service: launchd agent on macOS, systemd user unit on Linux"`

### Task 8: Updater

**Files:**
- Create: `core/updater.py`
- Modify: `leobot.py` (`update [--check] [--channel stable|main] [--version vX.Y.Z] [--rollback] [--yes]`)
- Test: `tests/test_updater.py`

**Interfaces:**
- Consumes: `paths`, `version`, `datadir`, `migrations`, `service`, `leobot.start/stop` helpers.
- Produces:
  - `updater.settings() -> dict` / `updater.save_settings(d: dict) -> None` on `paths.SETTINGS` (keys: `channel` default `"stable"`, `checked_at`, `latest`, `previous_ref`).
  - `updater.check(code: Path = paths.CODE) -> dict`: `{"current": str, "latest": str|None, "available": bool, "channel": str, "notes_url": str|None}`; stable uses `git ls-remote --tags origin` + `version.latest_tag`; main compares `git ls-remote origin refs/heads/main` sha to `HEAD`. Stores result in settings. Never raises on network failure (`latest: None`).
  - `updater.refuse_reason(code: Path) -> str | None`: not installed (`paths.is_installed()` false) → "This is a development checkout. Update it with git."; `git status --porcelain` non-empty → "The app folder has local changes: …".
  - `updater.apply(target: str | None = None, *, channel: str | None = None, yes: bool = True) -> int` implementing spec steps 1-11; backups named `pre-{target}-{stamp}.tar.gz`, prune to newest 5 `pre-*`; `previous_ref` saved before checkout; health check polls healthz proof + `version` equal to target's `VERSION` for 60 s; on failure: stop, if marker schema changed since backup → snapshot to `failed-{target}-{stamp}.tar.gz` then restore the pre backup; `git checkout previous_ref`; start; print both paths; return 1. All steps logged with timestamps to `paths.UPDATE_LOG`. After checkout, run `scripts/ensure-app.sh` on macOS when `~/Applications/LeoBot.app` exists (Task 11 makes it target that bundle).
  - `updater.rollback(yes: bool) -> int`: checkout previous release tag (or `previous_ref`), same snapshot-then-restore across schema change after prompt.
  - `updater.spawn_detached() -> None`: starts `leobot update --yes` outside the server's process tree: Linux with a systemd user service → `systemd-run --user --unit leobot-update --collect <python> <CODE/leobot.py> update --yes`; macOS with launchd agent → `launchctl submit -l leobot.update -- <python> <leobot.py> update --yes` (remove any previous `leobot.update` job first); otherwise `subprocess.Popen(..., start_new_session=True)`. Output to `paths.UPDATE_LOG`.

- [ ] **Step 1: Write failing tests** with a fixture: a temp bare repo as `origin`, a clone at `LEOBOT_HOME/app` (so `is_installed()` is true by patching `paths.CODE`), commits tagged `v0.1.0` and `v0.2.0` each with its own `VERSION`; server start/stop/healthz patched to a fake that reports the checked-out `VERSION`:
  - `test_check_finds_newer_tag`: on `v0.1.0` → `available`, `latest == "v0.2.0"`.
  - `test_check_offline`: origin path removed → `latest is None`, `available False`, no exception.
  - `test_nothing_newer`: on `v0.2.0` → `apply()` returns 0, prints "up to date", no backup made.
  - `test_dev_checkout_refused`: `is_installed` false → returns 1 with "development checkout".
  - `test_dirty_checkout_refused`: modified tracked file → returns 1, no backup.
  - `test_apply_moves_to_target_and_backs_up`: HEAD at `v0.2.0`, one `pre-v0.2.0-*.tar.gz`, `settings()["previous_ref"]` is the `v0.1.0` sha.
  - `test_stable_ignores_untagged_main`: extra untagged commit on `main` → stable target stays `v0.2.0`.
  - `test_fetch_failure_leaves_old_code`: origin removed after check → returns 1, HEAD unchanged, server restarted.
  - `test_failed_health_rolls_back_and_restores`: fake healthz never matches and fake migration bumps schema → HEAD back at `v0.1.0`, `failed-*.tar.gz` exists, `DATA` equals pre-update content.
  - `test_backups_pruned_to_five`: seven existing `pre-*` → five remain after apply (manual and legacy backups untouched).
  - `test_spawn_detached_linux_uses_systemd_run`: patched service status `systemd` → argv starts with `systemd-run --user`.
- [ ] **Step 2:** Run `python3 -m unittest tests.test_updater -v`. Expected: FAIL.
- [ ] **Step 3:** Implement; wire CLI flags.
- [ ] **Step 4:** Run tests + full suite. Expected: PASS.
- [ ] **Step 5:** Commit: `git commit -m "leobot update: backup, migrate, health check, roll back on failure"`

### Task 9: Update notice and button in the dashboard

**Files:**
- Modify: `dashboard/server.py` (daily check thread, two owner-only routes), `dashboard/app.js`, `dashboard/app.html`, `dashboard/app.css`
- Test: `tests/test_server_updates.py`, `tests/test_settings_screen.py` (extend)

**Interfaces:**
- Consumes: `updater.check`, `updater.spawn_detached`, `paths.is_installed`.
- Produces:
  - Background thread started in `main()` only when `paths.is_installed()`: runs `updater.check()` 60 s after start, then every 24 h.
  - `GET /api/update` (owner only): `{"installed": bool, "current": str, "latest": str|None, "available": bool, "notes_url": str|None, "updating": bool}` (cached settings, no network).
  - `POST /api/update` (owner only, same CSRF/origin check as other owner POSTs): refuses with 409 when not installed or already updating; else `updater.spawn_detached()` and 202 `{"updating": true}`.
  - Both routes return 403 to invited people, shared-thread contexts, runner tokens and the relays (add to the existing owner-only route set, not a new mechanism).
  - UI: when `available`, a small badge on the Settings nav item and a card at the top of Settings: "Leo Bot {latest} is available (you have {current})", a "What's new" link (`notes_url`), an "Update" button. On click: card shows "Updating. Leo Bot restarts in a moment."; poll `/healthz` every 2 s, reload the page when `version` changes, show the log path after 120 s without change. Settings also shows "Data folder: {paths.DATA}" (owner only) with the existing open-folder helper on macOS.

- [ ] **Step 1: Write failing tests**:
  - `test_get_update_owner`: owner GET returns the keys above.
  - `test_update_routes_forbidden_to_member`: invited person cookie → 403 on GET and POST.
  - `test_update_routes_forbidden_to_runner_and_relay`: runner token / relay context → 403.
  - `test_post_refused_in_dev_checkout`: `is_installed` false → 409.
  - `test_post_spawns_once`: patched `spawn_detached` called once; second POST while `updating` → 409.
  - In `test_settings_screen.py`: `app.js` contains the update card hook (`/api/update`) and no em dash in the new strings.
- [ ] **Step 2:** Run `python3 -m unittest tests.test_server_updates tests.test_settings_screen -v`. Expected: FAIL.
- [ ] **Step 3:** Implement routes, thread and UI using existing design tokens in `ui-tokens.css`.
- [ ] **Step 4:** Run tests + full suite. Then start the server from a temp `LEOBOT_HOME` with a fake cached `latest`, open Settings in the browser, check the card in light and dark mode and no console errors.
- [ ] **Step 5:** Commit: `git commit -m "Owner sees when an update is out and installs it from Settings"`

### Task 10: `install.sh`

**Files:**
- Create: `install.sh` (repo root, executable)
- Create: `scripts/render-installer.sh` (if Task 5 did not already)
- Modify: `.github/workflows/privacy-gate.yml` (new job `install-script`: `shellcheck install.sh scripts/*.sh` and `LEOBOT_REPO_URL=<local bare repo> bash install.sh --dry-run --yes` with `HOME` set to a temp dir containing a space)
- Test: `tests/test_install_script.py`

**Interfaces:**
- Consumes: `leobot.py` (`update`, `service install`), `scripts/build-app.sh --out` (Task 11).
- Produces: `install.sh [--channel stable|main] [--version vX.Y.Z] [--service|--no-service] [--no-app] [--yes] [--dry-run]`; env equivalents `LEOBOT_CHANNEL`, `LEOBOT_VERSION`, `LEOBOT_SERVICE` (`1`/`0`), `LEOBOT_NO_APP=1`, `LEOBOT_YES=1`, `LEOBOT_DRY_RUN=1`, `LEOBOT_HOME`, `LEOBOT_REPO_URL` (default: the line `REPO_URL_DEFAULT="__LEOBOT_REPO_URL__"`, filled at release; if still the placeholder and `LEOBOT_REPO_URL` is unset, exit 1 with "Download install.sh from the latest release, or set LEOBOT_REPO_URL."). Whole body inside a `main` function called on the last line, so a truncated download runs nothing. Steps exactly as the spec's "Installer" section. In `--dry-run`, every command that changes the machine is printed as `would: …` instead of run. Server default for service: yes when `LEOBOT_DEPLOYMENT=server` or no `DISPLAY`/not macOS; else no. When stdin is not a TTY (curl pipe), prompts read from `/dev/tty` if available, else take defaults.

- [ ] **Step 1: Write failing tests** in `tests/test_install_script.py` (skip shellcheck test when `shellcheck` is not on PATH):
  - `test_bash_syntax`: `bash -n install.sh` exits 0.
  - `test_shellcheck_clean`: `shellcheck install.sh` exits 0.
  - `test_dry_run_plans_clone_and_wrapper`: `bash install.sh --dry-run --yes` with temp `HOME` (containing a space) and `LEOBOT_REPO_URL` = local bare repo with tag `v0.1.0` → stdout contains `would: git clone`, `v0.1.0`, `.leobot/app`, `.local/bin/leobot`; nothing created under temp `HOME/.leobot/app`.
  - `test_main_channel_uses_main`: `--channel main --dry-run --yes` → stdout contains `main` and no `v0.1.0` checkout line.
  - `test_existing_install_hands_to_update`: temp `HOME/.leobot/app/.git` exists → dry run prints `would: … leobot.py update`.
  - `test_body_wrapped_in_main`: last non-empty line of the file is `main "$@"`.
  - `test_placeholder_refuses`: run without `LEOBOT_REPO_URL` → exit 1, message contains "latest release".
  - `test_render_fills_placeholder`: `scripts/render-installer.sh OUT` in a temp clone whose origin is the scp-style SSH URL for `acme/leo-bot` on github.com (build that string from two parts in the test, split before the `@`, so the privacy guard does not read it as an email) → OUT contains `https://github.com/acme/leo-bot.git` and no `__LEOBOT_REPO_URL__`.
- [ ] **Step 2:** Run `python3 -m unittest tests.test_install_script -v`. Expected: FAIL.
- [ ] **Step 3:** Implement `install.sh` and the CI job.
- [ ] **Step 4:** Run tests. Then a real install into a temp `LEOBOT_HOME` against a local bare repo (`LEOBOT_REPO_URL=file://…`), run `~/.local/bin/leobot status` from a temp `HOME`, then `leobot uninstall --data --yes`. Expected: works, nothing left in temp `HOME`.
- [ ] **Step 5:** Commit: `git commit -m "One-command installer for macOS and Debian/Ubuntu"`

### Task 11: Mac app uses the home folder

**Files:**
- Modify: `LeoBot.app/Contents/Resources/webview.swift:174-260`, `LeoBot.app/Contents/Resources/start-server.sh`, `scripts/build-app.sh` (add `--out DIR`), `scripts/ensure-app.sh` (rebuild `~/Applications/LeoBot.app` too when it exists and this checkout is `~/.leobot/app`)
- Test: `tests/test_app_rules.py` / `tests/test_app_icon.py` (extend, static checks)

**Interfaces:**
- Produces:
  - `repoPath()`: if `<bundle parent>/dashboard/server.py` exists → that folder (development clone); else `$LEOBOT_HOME/app` or `~/.leobot/app`.
  - Token read from `$LEOBOT_HOME/run/auth-token` (or `~/.leobot/run/auth-token`).
  - Failure alert names `~/.leobot/logs/server.log`.
  - While the app is open and a ping fails, retry every 2 s for 90 s before the failure alert (covers update restarts).
  - `start-server.sh`: token path `$HOME_DIR/run/auth-token`, log `$HOME_DIR/logs/server.log`, exports `LEOBOT_HOME`; the stale-server match keeps `dashboard/server.py`.

- [ ] **Step 1: Write failing static tests**: `webview.swift` contains `".leobot/app"` and `"run/auth-token"`; `start-server.sh` contains `run/auth-token` and `logs/server.log` and not `dashboard/.auth-token`; `build-app.sh` accepts `--out`.
- [ ] **Step 2:** Run those tests. Expected: FAIL.
- [ ] **Step 3:** Implement. Build with `scripts/build-app.sh` and with `--out "$TMP/Applications"`.
- [ ] **Step 4:** Tests pass; open the built app against a temp `LEOBOT_HOME`, confirm the dashboard loads; stop the server by hand and confirm the app reconnects after restart.
- [ ] **Step 5:** Commit: `git commit -am "LeoBot.app finds code and token in the home folder and rides out restarts"`

### Task 12: Server scripts and docs

**Files:**
- Modify: `scripts/server-setup.sh`, `scripts/sync-to-server.sh`, `scripts/leobot-connect.sh`, `README.md` (Install, Develop, data location, backups, updating), `docs/server.md`, `docs/guide.md` (osctl examples unchanged; data paths), `.gitignore` (keep legacy entries; add nothing for `~/.leobot`)
- Test: `tests/test_server_mode.py` (extend, static)

**Interfaces:**
- `server-setup.sh`: as root: install packages, create `leobot` user, `loginctl enable-linger leobot`, run `install.sh --service --yes` as that user (from the local copy when piped over ssh: fetch the same `install.sh` from `LEOBOT_REPO_URL` raw URL; keep `LEOBOT_REPO` override), run `leobot move-data --from ~leobot/leo-bot` if that folder has legacy data, disable and remove `/etc/systemd/system/leobot.service` if present, print next steps. Port stays 8766 on servers (`--port` passed to `service install`).
- `sync-to-server.sh`: rsync local `paths.DATA` (`${LEOBOT_HOME:-$HOME/.leobot}/data/`) to `~leobot/.leobot/data/`, stop/start via `sudo -u leobot XDG_RUNTIME_DIR=/run/user/$(id -u leobot) systemctl --user stop|start leobot`.
- `leobot-connect.sh`: token from `~leobot/.leobot/run/auth-token`.

- [ ] **Step 1: Write failing static tests**: `server-setup.sh` contains `enable-linger` and `install.sh` and removes `/etc/systemd/system/leobot.service`; `sync-to-server.sh` contains `.leobot/data`; `leobot-connect.sh` contains `.leobot/run/auth-token`; README contains `releases/latest/download/install.sh`, `../../releases/latest` and `leobot update`.
- [ ] **Step 2:** Run. Expected: FAIL.
- [ ] **Step 3:** Implement scripts and docs. Run `shellcheck` on each script.
- [ ] **Step 4:** Tests pass; `python3 -m unittest tests.test_no_usage_data` passes.
- [ ] **Step 5:** Commit: `git commit -am "Server setup, sync and docs follow the new install"`

### Task 13: Whole-branch verification and an existing clone with data

**Files:** none new.

- [ ] **Step 1:** Full suite `python3 -m unittest discover -t . -s tests`. Expected: all pass.
- [ ] **Step 2:** Back up the main checkout's data by hand (`tar czf` of `projects portfolio chats team database os.db dashboard/os.db dashboard/data.db` into the scratchpad), then start the server from the clone. Expected: legacy move runs, `~/.leobot/data` holds the data, `~/.leobot/backups/legacy-*.tar.gz` exists, dashboard shows the same workspaces, chats and posts.
- [ ] **Step 3:** Rebuild `LeoBot.app` (`scripts/build-app.sh`), open it. Expected: dashboard loads with the token from `~/.leobot/run/`.
- [ ] **Step 4:** Push branch, open PR, wait for CI, resolve review comments, merge to `main`.
- [ ] **Step 5:** After merge, stop. Publishing the first release (`scripts/release.sh 0.1.0`) is a separate step, not part of this plan.
