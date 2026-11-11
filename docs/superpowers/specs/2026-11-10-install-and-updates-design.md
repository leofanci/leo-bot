# Install and updates: one command, a home folder, in-app updates

## Why

Today Leo Bot is installed by cloning the repo, building the Mac app by hand and running the
server from the clone. Data lives inside the clone (`projects/`, `portfolio/`, `chats/`, `team/`,
`database/`, several `*.db` files) and only `.gitignore` keeps it out of git. That does not work
for anyone installing it fresh:

- A new user has no single install step and no way to update except `git pull`.
- Any update that replaces code risks the data sitting next to it.
- There are no version numbers, so there is nothing to say "you are behind" against.

Self-hosted agent tools commonly use one shape: a `curl … | bash` installer that works the same
on macOS and Linux, a single home folder with an env var override, an `update` command that
follows tagged releases, backup before update, and an "update available" notice in the
dashboard. Leo Bot follows that shape, with the code kept as a git checkout inside its home folder.

Leo Bot runs on the user's own machines only: their Mac, their server, or both.

## Goals

- One Terminal command installs Leo Bot on macOS and on Debian/Ubuntu. Running it again updates.
- Code and data are separate. Replacing the code can never touch the data.
- One home folder, `~/.leobot`, overridable with `LEOBOT_HOME`.
- A `leobot` command: open, start, stop, status, update, service, doctor, backup, version, uninstall.
- Versioned releases (`VERSION` file, `vX.Y.Z` git tags, GitHub Releases with notes).
- The owner sees "update available" in the dashboard and updates with one click. `leobot update`
  does the same from Terminal.
- Every update backs up the data first, migrates it, health-checks the new version and rolls the
  code back if the new version does not come up.
- Existing installs (a clone with data inside) move their data to the home folder once,
  automatically, with a backup.
- The existing Mac app (`LeoBot.app`, built locally) keeps working and is built by the installer.

## Non-goals

- A prebuilt, signed, notarized `.dmg` and an app that updates itself through an appcast. Later
  work; it reuses everything here.
- Windows, Docker images, Homebrew formula.
- Installing Python for the user on macOS. The installer explains what to install and stops.
- Applying updates without the owner clicking or typing `leobot update`.
- Signed tags or release signature checks. Updates trust the GitHub repo over HTTPS.
- A custom domain. Until one exists, the installer is a GitHub Release asset (see Installer).
- Publishing a release as part of this work. The release script exists; the first release is a
  separate step.
- Several Leo Bot instances on one machine. `LEOBOT_HOME` allows it for testing, nothing more.

## Anti-goals: a failure even if it works

- An update loses or corrupts data, or leaves the user with no backup to go back to.
- Anything writes user data inside the code folder after the move.
- The installer or updater needs `sudo` on macOS.
- An invited person, a shared thread, a bot or a runner sees update controls, triggers an update,
  or reads the home folder outside the data paths they already had.
- The stable channel ever runs untagged code from `main`.
- A test reads or writes the real `~/.leobot`.
- Moving data silently overwrites data already in the home folder.

## Layout

```
~/.leobot/                 (LEOBOT_HOME, mode 700)
  app/                     code: git checkout of the repo at a release tag
  data/                    everything that is the user's
    .leobot-data           marker + data schema version (JSON)
    projects/  portfolio/  chats/  team/  database/  ...
  run/                     auth token (600), pid file, port
  logs/                    server.log, update.log
  backups/                 data snapshots taken before updates and before the one-time move
  leobot.json              install settings: channel, last update check, latest seen version
```

Inside `data/`, keep the paths relative to the old repo root (`data/projects/…`,
`data/database/data/os.db`, …), so every `ROOT / "projects"` becomes `DATA / "projects"` with no
other change. Builder's call whether to flatten the three `os.db`/`data.db` locations while moving;
if so, it is one migration.

Code and data are found through one module, `core/paths.py`:

- `CODE` = the checkout the running code came from (today's `ROOT`).
- `HOME` = `$LEOBOT_HOME` or `~/.leobot`.
- `DATA`, `RUN`, `LOGS`, `BACKUPS` under `HOME`.

Every module that defines its own `ROOT` today (`core/bots.py`, `core/ws_logos.py`,
`dashboard/db.py`, `dashboard/engines/config.py`, `dashboard/fileops.py`, `dashboard/runner.py`,
`dashboard/server.py`, `hooks/reindex.py`, `generate.py`, `index.py`) uses `core/paths.py`
instead. Code paths (`skills/`, `bots/*.md`, `templates/`, `generate.py`, `index.py`) stay under
`CODE`; user state goes under `DATA`. The builder audits every write, not only the `ROOT /` hits.

A development clone uses the same `HOME`. The owner's clone and an installed copy therefore share
one data folder, and only one server may run against it at a time (pid file in `run/`, checked
on start).

## One-time move of existing data

The move is one function, `move_legacy(source)`, used in three places: on server start with
`source = CODE`, by `leobot move-data --from <folder>`, and by `scripts/server-setup.sh` for the
old server folder (`~leobot/leo-bot`).

"`DATA` is empty" means it holds nothing but the marker. The marker alone never blocks a move,
so an installer that created an empty `DATA` first cannot strand a clone's data.

1. Look in `source` for legacy data: `projects/*` other than `.gitkeep`, `portfolio/`, `chats/`,
   `team/`, `database/data/`, `database/os.db`, `os.db`, `dashboard/os.db`, `dashboard/data.db`,
   `dashboard/.auth-token`, `dashboard/server.log`.
2. None found: make sure `DATA` exists with the marker. Done.
3. Found, and `DATA` is empty: write a `.tar.gz` of the legacy data to `backups/legacy-<time>.tar.gz`,
   then move it into `DATA`, then write the marker. Then re-index (`index.py`) against the new path.
4. Found, and `DATA` already has content: do not move anything. On server start, refuse to start
   with a message naming both folders and `leobot doctor`, which explains the choices (keep the
   home folder and archive the legacy copy, or `leobot move-data --from <folder> --replace`, which
   backs up `DATA` first).

Existing servers: `scripts/server-setup.sh` runs the move from `~leobot/leo-bot` into
`~leobot/.leobot/data` before starting the new service, then leaves the old folder in place for
the owner to delete.

Absolute paths stored inside the data (saved chats, the SQLite index, linked-folder records,
anything else) must be found and rewritten, or proven not to point into the old location. The
builder audits this.

Owner-machine folders that are not product data (`private/`, `.remember/`, `.superpowers/`,
`.tokensave/`, `.claude/`) stay in the clone and are not moved.

## Installer: `install.sh`

At the repo root. Tracked files never contain the repository's hosting account, so the tracked
`install.sh` holds a placeholder repo URL (`__LEOBOT_REPO_URL__`) and refuses to run with it
unless `LEOBOT_REPO_URL` is set. `scripts/release.sh` fills the placeholder from
`git remote get-url origin` (as an HTTPS URL) and uploads the result as the `install.sh` asset of
each GitHub Release. Usage:

```sh
curl -fsSL <repo>/releases/latest/download/install.sh | bash
```

The README links to it with a relative link (`../../releases/latest`), which GitHub resolves to
the repository's own Releases page. A future domain redirects to the same asset.

Flags (also as env vars): `--channel stable|main`, `--version vX.Y.Z`, `--service` /
`--no-service`, `--no-app`, `--yes` (no prompts), `--dry-run`. Respects `LEOBOT_HOME`.

Steps:

1. Detect OS: macOS, Debian/Ubuntu. Other Linux: continue with a warning, no package installs.
2. Check `git`. macOS without it: run `xcode-select --install` and stop with "run this command
   again when it finishes".
3. Check `python3` ≥ 3.10. Missing on macOS: point to python.org or Homebrew and stop. On
   Debian/Ubuntu as root: `apt-get install python3`; as a normal user: print the command and stop.
4. Check `claude`. Missing: offer to install it with Anthropic's official installer, or skip
   (bots can run on an API key, Settings, Engines). Never fails the install.
5. Code: clone into `HOME/app` at the latest release tag (highest `vX.Y.Z` from
   `git ls-remote --tags`), or `main` on the main channel. If `HOME/app` exists, this is an
   update: hand over to `leobot update`.
6. Create `HOME/data` with the marker, `run/`, `logs/`, `backups/`; set modes.
7. Install `~/.local/bin/leobot` (a small shell wrapper that runs the CLI with `python3`). Add
   `~/.local/bin` to `PATH` in the shell's rc file if missing, and say so.
8. macOS, if `swiftc` is present and not `--no-app`: build `LeoBot.app` into `~/Applications`
   (no sudo). Missing `swiftc`: skip with one line; the browser works.
9. Background service: ask (or follow the flag). Default no on macOS, yes on a server.
10. Print one next step: `leobot` to open it.

`scripts/server-setup.sh` keeps its job (root prepares a `leobot` user) but the service becomes a
**systemd user unit** of the `leobot` user with `loginctl enable-linger`, installed by running
`install.sh --service --yes` as that user. Root's system unit (`/etc/systemd/system/leobot.service`)
is removed when found. Reason: the `leobot` user must be able to stop, start and update its own
service without sudo, the same on Mac (launchd agent) and Linux. The hardening options of today's
unit stay where user units support them. The setup script also runs the one-time move from the
old `~leobot/leo-bot` folder (see above).
`scripts/sync-to-server.sh` copies `DATA` to `DATA` instead of repo folders.
`scripts/leobot-connect.sh` stays as is.

## The `leobot` command

Python, stdlib only, like the rest of the code. Lives in the repo (builder picks the file).

- `leobot` (no args) / `leobot open`: start the server if it is not running, then open the
  dashboard (Mac app if installed, else the browser with the login link).
- `leobot start | stop | status`
- `leobot update [--check] [--channel stable|main] [--version vX.Y.Z] [--rollback]`
- `leobot service install | uninstall | status`: launchd agent on macOS
  (`~/Library/LaunchAgents/`), systemd user unit on Linux (mention `loginctl enable-linger` for
  running after logout). A server set up by `server-setup.sh` uses this same user unit.
- `leobot move-data --from <folder> [--replace]`: the one-time move, run by hand.
- `leobot doctor`: Python, git, Claude Code, folder modes, data marker, pending legacy data, port,
  running pid, version vs latest.
- `leobot backup`: snapshot `DATA` into `backups/`.
- `leobot version`
- `leobot uninstall [--data]`: remove app, wrapper, service, Mac app; keep data unless `--data`.

## Releases

- `VERSION` at the repo root, first release `0.1.0`.
- Tag `vX.Y.Z` on `main` after CI is green; a GitHub Release with short notes per tag.
- `scripts/release.sh X.Y.Z`: checks clean `main` and green CI, bumps `VERSION`, commits, tags,
  pushes, creates the release with `gh`, and attaches `install.sh` with the repo URL filled in.

## Updates

**Check.** When running from an install (`CODE` is `HOME/app`), the server checks once a day in a
background thread with `git ls-remote --tags origin` (no API token, no rate limit) and stores the
result in `leobot.json`. Development clones never check and never offer updates.

**Notice.** Owner only. A small badge in the sidebar and a card in Settings: current version,
new version, link to the release notes, an **Update** button. Invited people, shared threads,
bots and runners never see it or reach its endpoints.

**Apply.** The button calls an owner-only endpoint (same auth and CSRF rules as other owner
actions) that starts `leobot update` outside the server's own process tree, so stopping the
server does not kill it: a transient unit (`systemd-run --user`) on Linux, a separate launchd job
or `AbandonProcessGroup` on macOS, a plain `setsid` child when no service is installed. The page shows
"Updating" and reconnects when the server is back, then shows the new version.

`leobot update`:

1. Refuse on a development clone or a checkout with local changes, with a message.
2. Resolve the target: latest tag on stable, `origin/main` on main, or `--version`.
3. Nothing newer: say so, exit.
4. Back up `DATA` to `backups/pre-<version>-<time>.tar.gz`. Keep the last five update backups.
5. Stop the server (service, or the pid in `run/`).
6. `git fetch --tags` and check out the target. Remember the previous ref.
7. Rebuild the Mac app if its Swift sources changed (reuse `scripts/ensure-app.sh`).
8. Start the server. It runs data migrations before serving.
9. Health check: the server answers and reports the target version within 60 s.
10. Fails: stop the new server, check out the previous ref. If the data schema changed, first
    snapshot the current `DATA` to `backups/failed-<version>-<time>.tar.gz`, then restore the
    step 4 backup, so the previous code can start and nothing written in between is lost. Start
    the previous code, print both backup paths.
11. Log every step to `logs/update.log`.

`--rollback` checks out the previous release tag and restarts. Across a schema change it does the
same snapshot-then-restore as step 10, after a confirmation prompt (skipped with `--yes`), since
the older code cannot read newer data. `leobot backup --restore <file>` does a restore by hand,
always snapshotting the current `DATA` first.

## Data migrations

`DATA/.leobot-data` holds `{"schema": N}`. Numbered, idempotent migration functions in the repo
run in order on start. If the data's schema is newer than the code knows (after a code rollback),
the server refuses to start and says which version to update to. The one-time move above is
schema 1.

## Mac app

`LeoBot.app` today assumes it sits inside the clone (`repoPath()` in `webview.swift`) and reads
the token from `dashboard/.auth-token`. It changes to: code from `HOME/app` (or the clone it sits
in, for development), token from `HOME/run/`. It keeps working while the server restarts during
an update (retry instead of the failure alert). `start-server.sh` logs to `HOME/logs/`.

## Security

- `HOME` and `DATA` are mode 700, the token 600.
- The deny-list of paths bots and linked-folder tools may never read (`dashboard/server.py`
  around line 1021) covers `HOME` as a whole, not only the old repo folders.
- Linking a folder that is, contains or sits inside `HOME` or `CODE` is refused.
- The updater only fetches the configured `origin` over HTTPS and only checks out tags (stable)
  or `origin/main` (main). It never runs downloaded scripts other than the checked-out code.
- Update endpoints are owner-only and never exposed through the relay to invited people.

## Tests

- `core/paths.py`: defaults, `LEOBOT_HOME` override.
- A guard: the test run sets `LEOBOT_HOME` to a temp folder, and a test fails if anything touches
  the real `~/.leobot`.
- One-time move: fixture clone with generic data (`projects/demo`), each branch of the rules above,
  path rewriting, refusal when both sides have data.
- Migrations: order, idempotence, refusal on a newer schema.
- Updates against a local bare repo with tags as `origin`: check, apply, nothing newer, dirty
  checkout refused, failed health check rolls back, backups pruned to five.
- Update endpoints: owner only; invited person and relay get nothing.
- `install.sh`: `shellcheck` in CI, and a `--dry-run` run in a temp `HOME`.
- `tests/test_no_usage_data.py` still passes.

## Docs

- README "Install": the one command, what it creates, `leobot` basics, where data lives, backups.
  A "Develop" section for running from a clone.
- `docs/server.md`: updated for the new layout and `leobot update`.

## Left to the builder

- File names for the CLI, migrations and update modules.
- Exact flattening of database files inside `data/`.
- Service labels and unit names.
- Badge and card styling, within the existing design tokens.
- Health endpoint shape.
