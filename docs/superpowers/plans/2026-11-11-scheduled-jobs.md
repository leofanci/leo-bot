# Scheduled Jobs Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A bot's recurring work (a skill plus a message) runs on a schedule inside the server, read-only, with a per-job cap, and leaves writes as approvals in its own thread; a daily HQ digest lists what ran, what it cost, what waits and any memory tidy-ups; `generate.py` model jobs count against the caps.

**Architecture:** `core/jobs.py` owns `team/jobs.json` (records, schedule math, run state). osctl gets `create-job`, `update-job`, `delete-job`, `list-jobs`. The server runs a job turn through its own `_handle_ask` with a third viewer kind, `job`, on a headless handler (no socket, SSE into a buffer), in a fresh session that never touches the owner's open thread. `dashboard/job_runner.py` is the tick thread (once a minute, one job at a time) and builds the digest. Spend rows and reply metadata carry the `job` id; `spend.py` gains a per-job cap.

**Tech Stack:** Python 3.10+ stdlib, `unittest`.

**Spec:** `docs/superpowers/specs/2026-11-11-harness-upgrade-design.md` (Phase 3)

## Global Constraints

- Python stdlib only. No new dependencies.
- Job record in `team/jobs.json` (data folder): `id`, `name`, `workspace`, `bot`, `skill`, `message`, `schedule`, `cap` (required, above 0), `enabled`, `created`, `last_run`, `last_status`.
- Jobs are created and edited only through osctl. The four job commands are not in the shared-allowed set and are refused in read-only mode.
- A job runs only on a metered engine; creating one on an unmetered engine is refused, and a run whose engine became unmetered is refused before it starts.
- A job turn: osctl read-only (`LEOBOT_READONLY`), no edit folders, writes only as an `APPROVAL:` line. Connected-app actions keep their approval card.
- Missed runs run once on wake, never backfilled. One job turn at a time across all workspaces. A run stops on its cap, on a failed engine turn, or after `JOB_TURN_SECONDS = 1800`.
- `last_run` and `last_status` are written before and after each run; a restart never double-runs a job.
- Each run is its own thread titled for the job; its spend rows and replies carry `job: <id>`.
- The digest is built from saved threads, the job ledger and the approvals file, with no model call.
- No test calls a real engine.
- Public repo: fixtures use generic slugs only (`demo`, `acme`, `profile-a`). Run `python3 -m unittest tests.test_no_usage_data` before every commit.
- Commit messages: no `Co-Authored-By` trailer. User-facing copy: no em dashes.
- Whole suite: `python3 -m unittest discover -t . -s tests`.

## Decisions this plan makes

- **Schedule text.** `every <N>m|h|d` (minimum 1 hour), or `<days> HH:MM` where days is `daily` or a comma list of `mon`..`sun`. Local timezone. Stored as the text the owner gave plus nothing else; parsed on read. A daily time is a weekly time on every day, so it stays within the spec's "interval or weekly time".
- **The job cap is dollars per rolling 24 hours for that job**, like every other cap, checked before each run in a job turn (lead, teammates, follow-up) with what the turn already spent.
- **`name` is added to the record**, so the thread can be "named for the job": title `Job: <name>`.
- **`skill` is required and must be one of the bot's skills.** The job message is sent as `@<bot> /<skill> <message>`: a tagged skill turn, so its `APPROVAL:` line gets buttons and "Yes, approved." in the thread continues it as a normal owner turn that may save.
- **Thread room:** the bot's DM (`<ws>/dm-<bot>`), so runs collect where the owner talks to that bot.
- **Status values:** `running`, `done`, `waiting` (an approval is pending), `capped`, `failed`, `timeout`, `refused` (unmetered engine, bot inactive, skill gone, workspace gone), `interrupted` (server stopped mid-run).
- **Digest:** once per local day at or after 08:00 (`DIGEST_HOUR = 8`), covering everything since the last digest (first one: last 24 hours, never more than 7 days), posted as a new thread in `hq/general` from the root bot with title `Digest <YYYY-MM-DD>`. Nothing to report: no thread. `last` digest time is kept in `team/jobs.json` under `digest`.
- **`generate.py`** calls `complete_metered`, checks the owner and workspace caps first (refused as a `JobError` with the cap text), and appends a `job_ledger` row with `job: "generate"`. Runs from a shared thread count as the owner's, as osctl does not know the person.
- **The read-only refusal text** becomes neutral, since a job turn also gets it: `read-only here: you can look things up but not save. Say what should be saved: a teammate saves it, or, in a scheduled job, end with one APPROVAL: line.`
- **Out of scope:** webhook and app-event triggers (spec: later), a jobs screen in the dashboard (osctl, the job threads and the digest cover it), a cost-report grouping by job.

## Review Focus

1. **Server off for three days** with an `every 6h` job: one run on wake, then the next 6 hours after it. Test in Task 1: `test_missed_runs_run_once`.
2. **The owner is mid-thread when a job fires:** their open session and its id stay as they were, and their Stop button does not stop the job. Test in Task 4: `test_owner_thread_untouched`.
3. **A job outlives its setup** (bot turned off, skill removed, engine switched to an unmetered one, workspace deleted): the run is `refused` with no engine call and the job stays enabled so it resumes once fixed. Test in Task 4: `test_stale_job_refused_without_engine`.
4. **A restart mid-run** (`last_status: running` on disk): the job is marked `interrupted` and waits for its next due time. Test in Task 5: `test_restart_marks_interrupted_and_does_not_rerun`.
5. **A weekly time at the exact minute** (created Monday 09:00:30 with `mon 09:00`): first run next Monday, not now; `last_run` at 09:00:05 does not re-run at 09:01. Test in Task 1: `test_weekly_slot_strictly_after_base`.

---

### Task 1: Job store and schedule math

**Files:**
- Create: `core/jobs.py`
- Test: `tests/test_jobs.py`

**Interfaces:**
- Produces:
  - `RELPATH = Path("team") / "jobs.json"`, `MIN_EVERY = 3600`, `STATUSES` (set above)
  - `parse_schedule(text: str) -> dict`: `{"every": seconds}` or `{"days": [0..6], "at": (h, m)}`; `ValueError` with a readable message otherwise.
  - `next_due(job: dict) -> float`: base = `last_run` or `created`; interval: base + every; weekly: first slot strictly after base (local time).
  - `is_due(job: dict, now: float) -> bool`: enabled and `now >= next_due(job)`.
  - `load(root) -> list[dict]`, `get(root, job_id) -> dict | None`
  - `create(root, *, ws, bot, skill, name, message, schedule, cap, now=None) -> dict` (validates schedule, cap finite and > 0, name 1-60 chars, message 1-2000 chars; `id` = 8 hex chars)
  - `update(root, job_id, **fields) -> dict` (same checks; `enabled` bool), `delete(root, job_id) -> bool`
  - `mark_start(root, job_id, now) -> None` (`last_run=now`, `last_status="running"`), `mark_end(root, job_id, status, chat=None) -> None` (`last_status`, `last_chat`)
  - `recover(root) -> int`: every `running` becomes `interrupted`; returns how many.
  - `digest_state(root) -> dict`, `set_digest(root, ts) -> None`
  - Every write holds `core.memory.locked(path)` and uses `bots._atomic_write`. A corrupt or missing file loads as no jobs.

- [ ] **Step 1: Write the failing tests** in `tests/test_jobs.py` class `Schedule` and `Store`:
  - `test_parse`: `every 6h` → `{"every": 21600}`; `every 90m` → 5400; `every 2d` → 172800; `daily 09:00` → days 0-6; `mon,thu 18:30` → `[0, 3]`, `(18, 30)`; `every 30m`, `tue 25:00`, `someday 09:00`, `""` raise `ValueError`.
  - `test_missed_runs_run_once`: `every 6h`, `last_run` 3 days ago: due now; after `mark_start(now)`, `is_due(now + 60)` is False and `next_due == now + 21600`.
  - `test_weekly_slot_strictly_after_base`: build `created` with `time.mktime` for a Monday 09:00:30 local; `next_due` is the next Monday 09:00; with `last_run` Monday 09:00:05, not due at 09:01.
  - `test_create_validates`: cap `0`, `-1`, `nan` and a bad schedule raise `ValueError`; a good one round-trips through `load` with `enabled True`, `last_run None`.
  - `test_update_delete_and_recover`: `update(enabled=False)` stops `is_due`; `mark_start` then `recover` gives `interrupted`, count 1; `delete` removes it.
  - `test_corrupt_file_reads_empty`: `team/jobs.json` holding `not json` → `load` returns `[]`.

- [ ] **Step 2: Run** `python3 -m unittest tests.test_jobs -v`. Expected: FAIL, `core.jobs` missing.

- [ ] **Step 3: Implement `core/jobs.py`.** Weekly next slot: walk days from the base's local date forward (up to 8 days), build each candidate with `time.mktime((y, m, d, h, mi, 0, 0, 0, -1))`, return the first candidate whose weekday is in `days` and that is `> base`.

- [ ] **Step 4: Run** the tests. Expected: PASS.

- [ ] **Step 5: Commit** `Jobs: store and schedule (phase 3)`.

### Task 2: Job cap, job-tagged spend, thread title

**Files:**
- Modify: `dashboard/spend.py`, `dashboard/chat_store.py`
- Test: `tests/test_spend_cap.py`, `tests/test_chat_store.py`

**Interfaces:**
- Consumes: nothing new.
- Produces:
  - `spend.refusal(ws, by, now=None, pending=0.0, records=None, job=None)` and `spend.near_notice(..., job=None)`, where `job = (job_id, cap)`. A job cap counts spend rows whose `job` equals the id; its refusal text: `Paused: this job reached its cap ($X per 24 hours). It runs again once there is room.`; near text `Heads up: N% of this job's daily cap is used.`
  - `chat_store.record_turn(..., job=None, title=None)`: `job` goes on the spend row and on the record (`rec["job"]`); `title` names a new record. `list_meta` includes `job`.

- [ ] **Step 1: Write the failing tests:**
  - `test_spend_cap.JobCap.test_job_cap_counts_only_that_job`: records with spend rows `{"ts": now, "by": "owner", "cost": 0.6, "ws": "acme", "job": "j1"}` and one for `j2`; `refusal("acme", "owner", records=..., job=("j1", 0.5))` returns the job text; `job=("j3", 0.5)` returns None; `pending=0.5` with `("j3", 0.5)` returns the job text.
  - `test_chat_store.RecordTurn.test_job_and_title`: `record_turn(sess, None, "msg", "reply", meta={"cost": 0.1}, ws="acme", job="j1", title="Job: weekly")` → `rec["title"] == "Job: weekly"`, `rec["job"] == "j1"`, `rec["spend"][-1]["job"] == "j1"`; `list_meta()[0]["job"] == "j1"`.

- [ ] **Step 2: Run** `python3 -m unittest tests.test_spend_cap tests.test_chat_store -v`. Expected: FAIL.

- [ ] **Step 3: Implement.** `_caps(ws, by, job=None)` appends `("job", job_id, cap)` when given; `_counts("job", key, row)` is `row.get("job") == key`.

- [ ] **Step 4: Run** the tests. Expected: PASS.

- [ ] **Step 5: Commit** `Jobs: per-job cap, job-tagged spend rows`.

### Task 3: osctl job commands, metered check, rules

**Files:**
- Modify: `dashboard/engines/registry.py`, `dashboard/osctl.py`, `dashboard/ai_rules.py` (command table), `CLAUDE.md` (same row)
- Test: `tests/test_jobs.py` (class `Osctl`), `tests/test_engines.py`, `tests/test_teammates.py` (read-only text)

**Interfaces:**
- Consumes: Task 1 store functions.
- Produces:
  - `registry.metered(engine) -> bool`: True for Claude Code; for `kind == "api"`, True when both tiers (`CHAT_MODEL`, `ESCALATED_MODEL`) resolve to a model in `prices.PRICES`; False otherwise (Codex, ACP, runner).
  - osctl: `create-job --bot --skill --name --message --schedule --cap [--project]`, `update-job --job <id> [--name --message --schedule --cap --skill --enabled true|false]`, `delete-job --job <id>`, `list-jobs`. Output `{"ok": true, "job": {...}}` / `{"ok": true, "jobs": [...]}`.
  - Workspace: in a company workspace `--project` defaults to it (another one is refused by `_guard`) and `update`/`delete`/`list` see only that workspace's jobs. HQ without `--project` is workspace `hq`.
  - `create-job`/`update-job` refuse: an unknown or inactive bot, a skill not in `team.skills_of(bot)`, an engine for which `metered` is False (`Jobs need an engine that reports cost; <label> does not. Pick another engine for this bot in Settings, Engines.`).
  - All four commands go in `_SHARED_BLOCKED`, none in `_READ_ONLY_CMDS`.
  - Rules row: `| Scheduled job (propose with APPROVAL: first, never unasked) | create-job --bot <id> --skill <skill> --name "..." --message "..." --schedule "every 6h" or "mon,thu 09:00" or "daily 09:00" --cap <usd per day> [--project <slug>] · update-job --job <id> [...] [--enabled false] · delete-job --job <id> · list-jobs |`

- [ ] **Step 1: Write the failing tests** (osctl through `osctl.main([...])` on a temp data root with `fileops.ROOT`/`bots.ROOT` patched, as `tests/test_memory.py` does; engine pick patched):
  - `test_create_list_update_delete`: create in HQ for `acme` with a metered fake engine; `list-jobs` shows it; `update-job --enabled false` saved; `delete-job` empties the list.
  - `test_workspace_bound`: with `LEOBOT_WORKSPACE=acme`, `create-job` without `--project` lands in `acme`; `--project demo` refused; a `demo` job is invisible to `list-jobs` and `delete-job` there.
  - `test_refusals`: unknown bot, skill of another bot, unmetered engine each give `ok: false`.
  - `test_shared_and_readonly_refuse_job_commands`: `LEOBOT_SHARED=1` and `LEOBOT_READONLY=1` each refuse `create-job` and `list-jobs`.
  - `test_engines.Metered.test_metered`: Claude Code True; an `ApiEngine` with priced `fast`/`strong` True, with one unpriced model False; `CodexEngine()` False.
  - Update the read-only text assertion in `tests/test_teammates.py` to the new copy.

- [ ] **Step 2: Run** `python3 -m unittest tests.test_jobs tests.test_engines tests.test_teammates tests.test_case_variants -v`. Expected: FAIL on the new tests.

- [ ] **Step 3: Implement.** Team for validation: `bots.team(bots.ROOT, ws, project_record)` with the record from `db.tree()` (HQ: `None`). `_guard` defaults `args.project = ws` for `create-job`.

- [ ] **Step 4: Run** the tests plus `tests.test_ai_rules` and `tests.test_coherence`. Expected: PASS.

- [ ] **Step 5: Commit** `Jobs: osctl commands, metered-engine check`.

### Task 4: A job turn through the ask path

**Files:**
- Modify: `dashboard/server.py` (`_handle_ask`, `_ask_turn`, new `run_job`, new `JOB_NOTE`)
- Test: `tests/test_jobs_server.py`

**Interfaces:**
- Consumes: Task 1 (`jobs.get`), Task 2 (`spend.refusal(job=)`, `record_turn(job=, title=)`), Task 3 (`registry.metered`).
- Produces: `run_job(job: dict, srv) -> dict` in `server.py`: `{"status": <status>, "chat": <chat id or None>, "cost": float}`. `srv` is the running HTTP server (its port feeds the os relay); tests pass a `mock.Mock(server_address=("127.0.0.1", 8765))`.
- Viewer: `{"kind": "job", "job": job, "live": {}}`. `_handle_ask` applies the invited-person checks only to `kind == "person"` and refuses any other kind but `owner` and `job` with 403. `_ask_turn` writes `live["sess"]`, `live["chat"]`, `live["status"]`.
- In `_ask_turn` when the viewer is a job:
  - the session is a new `ChatSession` with `readonly = True`, never `get_chat_session` (the owner's `_CHAT` stays);
  - never a runner engine; after `engine_registry.pick`, `not engine_registry.metered(engine)` ends the turn as `refused` with a zero-model message;
  - `may_edit = False` (no edit folders); `may_write = True`;
  - the cap check passes `job=(job["id"], job["cap"])` (before the lead and in `may_run`); a cap stop sets `capped`;
  - the workspace block gets `JOB_NOTE`: `## Scheduled job\nThis turn is the scheduled job "<name>". Nobody is watching it live. You can read and look things up; every osctl write is refused. If something should be saved, say what and where, and end with one APPROVAL: line. The owner approves it in this thread.`;
  - the lead's os token (locked or full-disk engines) is minted with `"readonly": True`;
  - teammates use the `_EXTRA` slot `"job"`;
  - each reply's metadata gets `job`; `record_turn` gets `job=job["id"]`, `title=f"Job: {job['name']}"`;
  - status: `failed` on `_RunFailed`, `waiting` when an approval was recorded, else `done`.
- `run_job` first checks the job still fits (workspace exists, bot active, skill owned): else `refused` with no engine pick. It builds the message `@<bot> /<skill> <message>`, room `<ws>/dm-<bot>`, runs `_handle_ask` on a headless handler (`wfile` = `BytesIO`, `client_address = ("job", 0)`, `server = srv`), and sums `cost` from the saved record.

- [ ] **Step 1: Write the failing tests** in `tests/test_jobs_server.py`. Harness: temp data root with an `acme` project, `server.ROOT`, `fileops.ROOT`, `chat_store.CHATS_DIR` patched, `db.tree`/`db.posts` patched, `engine_registry.pick` returning a fake API-kind engine (`metered` patched True unless the test says otherwise), and `ChatSession.ask` patched to record `(self, text, model, edit_dirs)` and yield a scripted reply plus `("done", {"cost": 0.01})`.
  - `test_job_turn_is_read_only_and_proposes`: reply `Weekly numbers.\nAPPROVAL: save the review memo to sec04` → status `waiting`; the session had `readonly True`, `edit_dirs == ()`; the prompt holds `## Scheduled job`; one pending approval for the thread; record title `Job: weekly`, `job` set, spend row `job` set, room `acme/dm-cmo`.
  - `test_owner_thread_untouched`: set `server._CHAT` to a sentinel session first; after `run_job`, `server._CHAT` is the same object.
  - `test_job_cap_stops_before_engine`: a saved record with a `job: j1` spend row of 1.0 and job cap 0.5 → status `capped`, `ChatSession.ask` never called.
  - `test_unmetered_engine_refused`: `metered` False → `refused`, no ask.
  - `test_stale_job_refused_without_engine`: bot set inactive (`bots.set_active`), then a skill not owned, then workspace `gone`: each `refused`, `engine_registry.pick` never called.
  - `test_failed_engine_turn`: scripted `("error", "boom")` → `failed`.

- [ ] **Step 2: Run** `python3 -m unittest tests.test_jobs_server -v`. Expected: FAIL.

- [ ] **Step 3: Implement** as listed under Interfaces.

- [ ] **Step 4: Run** `tests.test_jobs_server`, `tests.test_server_ask`, `tests.test_approvals`, `tests.test_teammates`, `tests.test_spend_cap`, `tests.test_evals`. Expected: PASS.

- [ ] **Step 5: Commit** `Jobs: a job turn runs read-only through the ask path`.

### Task 5: Tick thread, timeout, digest

**Files:**
- Create: `dashboard/job_runner.py`
- Modify: `dashboard/server.py` (start the runner in `main` before `serve_forever`)
- Test: `tests/test_job_runner.py`

**Interfaces:**
- Consumes: Task 1 store, Task 4 `run_job`.
- Produces:
  - `TICK = 60`, `JOB_TURN_SECONDS = 1800`, `DIGEST_HOUR = 8`
  - `class Runner(root, run_turn, stop_turn=None, records=None, now=time.time)`: `tick() -> list[str]` (ids run this tick); `start() -> threading.Thread` (calls `jobs.recover(root)` once, then `tick()` every `TICK` seconds, catching and printing every exception).
  - `tick`: due jobs oldest-due first, one at a time: `mark_start`, `run_turn(job)` under a `threading.Timer(JOB_TURN_SECONDS, stop_turn)`; when the timer fired the status is `timeout`; `mark_end(status, chat)`. An exception from `run_turn` ends as `failed`. Then `maybe_digest()`.
  - `digest_text(root, since, now, records) -> str | None`: lines for job threads updated in the window (name, status, cost), memory tidy-ups from `job_ledger` rows with `job == "memory-consolidate"` (`bot`, `before` → `after` entries, workspace), generate runs (count and cost), and pending approvals (count plus up to 5 summaries with workspace). None when every part is empty.
  - `maybe_digest() -> str | None`: at or after `DIGEST_HOUR` local, once per local day; posts with `chat_store.post_note(title, room="hq/general", bot=bots.ROOT_BOT, text)` (new in `dashboard/chat_store.py`: one assistant message, new id, `scope None`); `set_digest(now)` even when nothing was posted.
  - Server: `stop_turn` closes `live["sess"]` and `_EXTRA["job"]` of the run in flight; `run_turn = lambda job: run_job(job, srv)`.

- [ ] **Step 1: Write the failing tests** with a fake `run_turn` and an injected clock:
  - `test_due_job_runs_and_state_is_written_before`: inside the fake `run_turn`, `jobs.get(...)["last_status"] == "running"`; afterwards `done` and `last_chat` set; a second `tick` at the same time runs nothing.
  - `test_one_at_a_time_in_due_order`: two due jobs, the fake records call order: oldest due first, never overlapping (a flag set during a call).
  - `test_restart_marks_interrupted_and_does_not_rerun`: a job saved `running` with `last_run` 10 minutes ago and `every 6h`; `Runner.start` path (`jobs.recover`) gives `interrupted`, `tick` runs nothing.
  - `test_timeout_stops_the_turn`: `JOB_TURN_SECONDS` patched to 0.05, the fake sleeps until `stop_turn` is called → status `timeout`.
  - `test_exception_is_failed`: the fake raises → `failed`, the runner keeps going.
  - `test_digest_lists_runs_tidyups_and_approvals`: a job record, a `memory-consolidate` ledger row (`before 310`, `after 240`), one pending approval → the text names the job, its cost, `310 → 240`, the approval summary; the posted thread is in `hq/general`; a second call the same day posts nothing; with nothing to report no thread is posted.

- [ ] **Step 2: Run** `python3 -m unittest tests.test_job_runner -v`. Expected: FAIL.

- [ ] **Step 3: Implement** `dashboard/job_runner.py`, `chat_store.post_note`, the server wiring.

- [ ] **Step 4: Run** the tests. Expected: PASS.

- [ ] **Step 5: Commit** `Jobs: tick thread, timeout, daily HQ digest`.

### Task 6: generate.py on the ledger

**Files:**
- Modify: `generate.py` (`run_claude`)
- Test: `tests/test_generate_plan.py` (or the file that covers `run_claude`), new class `Metered`

**Interfaces:**
- Consumes: `engine.complete_metered`, `job_ledger.append`, `spend.refusal`.
- Produces: `run_claude` keeps its signature and return (a JSON result envelope). Before the call: `spend.refusal(ws, "owner", records=<chat_store.records + job_ledger.records(root)>)` raises `JobError(<text>)`. After: one `job_ledger` row `{"ts", "ws", "by": "owner", "bot": None, "job": "generate", "engine", "engine_label", "model", "usage", "cost", "metered", "tokens"}`, cost estimated from `prices` when the engine gives none. `ws` = `LEOBOT_WORKSPACE` or `hq`; root = the data folder.

- [ ] **Step 1: Write the failing tests:** a fake engine whose `complete_metered` returns `{"text": "{}", "model": "claude-haiku-5-5", "usage": {...}, "cost": 0.002, "truncated": False}` → `run_claude` returns an envelope whose `result` is `{}` and one ledger row with `job: "generate"`, `cost 0.002`; with a workspace cap set and spent, `JobError` with `Paused:` and the engine never called. Existing tests that patch `engine.complete` switch to `complete_metered`.

- [ ] **Step 2: Run** `python3 -m unittest tests.test_generate_plan tests.test_generate_revise tests.test_generate_brief_voice_selection -v`. Expected: FAIL on the new tests.

- [ ] **Step 3: Implement.**

- [ ] **Step 4: Run** the tests. Expected: PASS.

- [ ] **Step 5: Commit** `generate.py: metered runs count against caps`.

### Task 7: Docs, full suite, live check

**Files:**
- Modify: `README.md` (a short "Scheduled jobs" bullet: what a job is, read-only, approval in its thread, daily digest), the spec (Phase 3 gets a "Shipped" note naming the decisions above)

- [ ] **Step 1:** Update the docs.
- [ ] **Step 2:** Run `python3 -m unittest discover -t . -s tests` and `python3 -m evals`. Expected: all OK, evals unchanged.
- [ ] **Step 3:** Live check on a throwaway `LEOBOT_HOME` with a local fake API provider (as in Phase 2): create a job through osctl with `every 1h`, set its `last_run` back two hours, run one `Runner.tick()` against a started server, confirm the thread, the approval, the spend row with `job`, and a digest thread after `maybe_digest` with the hour patched.
- [ ] **Step 4: Commit** `Jobs: docs`, open the PR, wait for CI, resolve review threads, squash-merge, delete the branch.
