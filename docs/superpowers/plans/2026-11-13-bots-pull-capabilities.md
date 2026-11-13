# Bots Pull Their Own Capabilities Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Bots see a short menu of what they can reach and load skills, code context, web search and teammates on demand, replacing per-turn regex routing.

**Architecture:** The bot card gains a skill menu; new read-only `osctl get-skill` and `osctl code-context` commands deliver bodies and code context; a `HANDOFF @handle:` reply line passes work to a teammate. Regex routers (`_suggest_skill` as router, `_picked_by_wording`, tag gate, `_needs_app_context` injection) go. Safety gates (shared/locked, full-disk, app-read, job modes) stay and are enforced in `osctl`.

**Tech Stack:** Python 3 stdlib, unittest (run as `python3 -m unittest tests.<module>`), vanilla JS dashboard (`dashboard/app.js`).

**Spec:** `docs/superpowers/specs/2026-11-13-bots-pull-capabilities-design.md`

## Global Constraints

- PUBLIC repo: fixtures use `demo`, `acme`, `profile-a`, `profile-b` only; run `tests/test_no_usage_data.py` before every commit.
- No third-party text or names for the reply style; Leo Bot's own wording.
- Every new osctl command is placed in each allow-set it belongs to (`_READ_ONLY_CMDS`, `_LOCAL_CMDS`, `_SHARED_ALLOWED`, `_AUTO_ALLOWED`); `tests/test_jobs.py` and `tests/test_case_variants.py` fail on a command in neither set.
- Shared/locked threads gain nothing: no `code-context`, no web beyond today, no apps.
- WebFetch stays limited to hosts the user typed (`chat_session.user_web_domains`).
- `may_edit` (folder edits) is unchanged: still needs a tagged turn.
- Run `leobot eval` before Task 4 and after Task 7; no regression in routing evals except cases that asserted removed regex behavior (update those to the new expectation).
- Work on branch `bots-pull-capabilities` in the main checkout (no extra worktree); PR, CI green, squash-merge.

## Review Focus

- Bot loads a skill name that does not exist or is owned by a switched-off bot: clear error listing valid names, turn continues. (Task 1 test.)
- A `HANDOFF` line naming an inactive bot, a non-member in a topic room, or the speaker itself: line stripped, no second run, no crash. (Task 6 test.)
- Two `HANDOFF` lines, or `HANDOFF` plus `CONSULT` in one reply: only the first handoff runs; consults still run before it. (Task 6 test.)
- Auto job loads its own skill: writes still allowed; loads another: writes refused for the rest of that run only, next run clean. (Task 8 test.)
- Short replies off: rail has no style block; osctl text unaffected either way. (Task 9 test.)

---

### Task 1: `core/skill_loads.py` + `osctl get-skill`

**Files:**
- Create: `core/skill_loads.py`
- Modify: `dashboard/osctl.py` (parser near `recall` at :397; allow-sets :44-123)
- Test: `tests/test_skill_loads.py`

**Interfaces:**
- Produces: `skill_loads.record(root, chat: str, skill: str) -> None`; `skill_loads.loaded(root, chat: str) -> list[str]` (order of first load, unique); `skill_loads.clear(root, chat: str) -> None`. Stored at `<root>/team/skill_loads/<chat>.json`; `chat` validated as one path segment (`[A-Za-z0-9_-]+`), else no-op / `[]`.
- Produces: `osctl get-skill --name <skill>` prints the body from `server._load_skill_body`-equivalent (move the loader to `core/skill_loads.body(name) -> str` and have `server._load_skill_body` call it, keeping `_RAIL_COVERED_SKILL_STUBS`). Records the load when `LEOBOT_CHAT` is set.

- [ ] **Step 1: Write failing tests**

```python
def test_record_and_loaded_unique_in_order(self):
    skill_loads.record(self.root, "c1", "pricing-strategy")
    skill_loads.record(self.root, "c1", "positioning")
    skill_loads.record(self.root, "c1", "pricing-strategy")
    self.assertEqual(skill_loads.loaded(self.root, "c1"), ["pricing-strategy", "positioning"])

def test_bad_chat_id_is_ignored(self):
    skill_loads.record(self.root, "../x", "positioning")
    self.assertEqual(skill_loads.loaded(self.root, "../x"), [])

def test_get_skill_prints_body_and_records(self):
    out = run_osctl(["get-skill", "--name", "positioning"], env={"LEOBOT_CHAT": "c1"})
    self.assertIn("positioning", out.lower())
    self.assertEqual(skill_loads.loaded(self.root, "c1"), ["positioning"])

def test_get_skill_unknown_lists_names(self):
    err = run_osctl_error(["get-skill", "--name", "nope"])
    self.assertIn("unknown skill", err)
    self.assertIn("positioning", err)

def test_get_skill_allowed_everywhere(self):
    for s in (osctl._READ_ONLY_CMDS, osctl._LOCAL_CMDS, osctl._SHARED_ALLOWED, osctl._AUTO_ALLOWED):
        self.assertIn("get-skill", s)
```

Use the existing osctl test helpers in `tests/test_osctl*.py` for `run_osctl`.

- [ ] **Step 2: Run, expect FAIL** — `python3 -m unittest tests.test_skill_loads` → ImportError / unknown command.
- [ ] **Step 3: Implement** `core/skill_loads.py` (atomic write via `bots._atomic_write`), move body loader, add `get-skill` parser and handler, add to the four allow-sets. Valid names = `server.SKILL_NAMES` equivalent: directory names under `paths.CODE / "skills"` plus `workspace`.
- [ ] **Step 4: Run, expect PASS**, plus `tests/test_jobs.py tests/test_case_variants.py`.
- [ ] **Step 5: Commit** `feat: osctl get-skill loads a skill body on demand`

### Task 2: Skill menu in the bot card

**Files:**
- Modify: `dashboard/server.py` `_bot_block` (:968-1018), `_skills_index` (:1103)
- Test: `tests/test_bot_menu.py`

**Interfaces:**
- Consumes: `team.skills_of(bot_id)`, `_skills_index()` descriptions.
- Produces: `_skill_menu(team, bot_id) -> str`: header line `Your skills (load one with \`osctl get-skill --name <name>\` when the task needs it):` then `- <name>: <description>` per skill, description cut at 100 chars at a word boundary. Empty string when the bot has no skills.

- [ ] **Step 1: Failing tests**: fresh card contains the header and one line per `skills_of`; non-fresh card (`(speaking as @x)`) does not; a bot with no skills gets no header; descriptions are one line, ≤ 100 chars.
- [ ] **Step 2: Run, expect FAIL.**
- [ ] **Step 3: Implement** `_skill_menu`; append to `lines` in the fresh branch after the persona line.
- [ ] **Step 4: Run, expect PASS.**
- [ ] **Step 5: Commit** `feat: bot card lists its own skills`

### Task 3: A loaded skill becomes the thread's active skill

**Files:**
- Modify: `dashboard/server.py` (after the lead `run(...)` at :3294, and after a handoff run in Task 6)
- Test: `tests/test_server_ask.py` (new class `SkillLoadTests`)

**Interfaces:**
- Consumes: `skill_loads.loaded(ROOT, chat)`, `skill_loads.clear(ROOT, chat)`, `sess.note_skill(name, explicit=True)`.
- Produces: after each bot run, if `sess.session_id` has loads, the last loaded skill is noted as explicit and the marker cleared. Next turn's `_pick_model_reason` returns `skill-thread` (stronger model) via the existing `_in_active_skill_thread`.

- [ ] **Step 1: Failing tests**: fake engine run that calls `skill_loads.record(ROOT, chat, "positioning")`; after the turn `sess._last_skill == "positioning"` and `sess._skill_explicit`; marker file gone; a following untagged "draft it" turn picks `ESCALATED_MODEL` with reason `skill-thread`; a following "what is X" read turn picks `CHAT_MODEL`.
- [ ] **Step 2: Run, expect FAIL.**
- [ ] **Step 3: Implement** helper `_adopt_loaded_skill(sess) -> str | None`, called after the lead run and after the follow-up run. Engines that do not pass `LEOBOT_CHAT` to osctl simply record nothing (no escalation, no error). Verify `policy.chat` is set for private chat turns on Claude Code, Codex, API engines; for the relay path, `os_relay` already maps `chat` to `LEOBOT_CHAT`.
- [ ] **Step 4: Run, expect PASS.**
- [ ] **Step 5: Commit** `feat: a skill the bot loads stays active for the thread`

### Task 4: Remove the tag gate and skill hint; untagged turns may propose saves

Run `leobot eval` first and keep the output for comparison.

**Files:**
- Modify: `dashboard/server.py` (:2913-2976 tag gate, `_tag_gate_message` :954, `compose_turn_prompt` :1042-1086 `suggest`/`suggest_owner` params and hint block, call site :3062-3076)
- Modify: `dashboard/ai_rules.py` (tagging text near :160-164 and `TEAM_TAGGING`)
- Test: `tests/test_server_ask.py` (replace `test_untagged_save_word_still_hits_the_tag_gate` :593, hint test :343, gate text test :366), `tests/test_server_apps.py:495`

**Interfaces:**
- Produces: `compose_turn_prompt(*, user_msg, context, skill, session, projects, explicit=False, workspace=None, bot=None, only_project=None)` (no `suggest`, `suggest_owner`). `may_write = not shared and not job` on non-app turns, else today's rule; jobs keep today's value.

- [ ] **Step 1: Rewrite tests**: untagged "save the positioning memo" on a private turn runs the model (no gate text) and an `APPROVAL:` line in its reply produces an approval card; same message in a shared thread produces no card; prompt never contains `Skill hint`; `_tag_gate_message` no longer exists; `may_edit` still false for an untagged message.
- [ ] **Step 2: Run, expect FAIL.**
- [ ] **Step 3: Implement**: delete gate block, `_tag_gate_message`, `suggest` plumbing (keep `_suggest_skill` only where `_route_dm` and `run_teammate` use it, until Task 6 removes the latter). Rewrite RAIL lines: untagged turns read and answer; to save, load the skill with `get-skill`, say what and where in one line, end with one `APPROVAL:` line; tagged `/skill` still injects the body up front.
- [ ] **Step 4: Run** `tests/test_server_ask.py tests/test_server_apps.py tests/test_bots.py`, expect PASS.
- [ ] **Step 5: Commit** `feat: untagged turns propose saves through approval, tag gate removed`

### Task 5: `osctl code-context` and web search on private turns

**Files:**
- Modify: `dashboard/osctl.py` (new parser + allow-sets), `dashboard/server.py` (:2983-3031 graph injection, `_needs_app_context` :814, `_needs_web` :738, `_pick_model_reason` :924), `_bot_block` (one line naming `code-context` when the workspace has an indexed folder)
- Test: `tests/test_code_context.py`, `tests/test_server_ask.py`

**Interfaces:**
- Produces: `osctl code-context --query "<text>"`: workspace from `LEOBOT_WORKSPACE`; folder = `folders.first_indexed(folders.for_turn(ws, None, None, ROOT))`; calls `tokensave_bridge.context(ws, query, active_project=ws, include_code=True, folder=folder)`. Refused when `LEOBOT_SHARED` is set, when no indexed folder ("no linked folder has a code index"), when `tokensave` is not on PATH. In `_READ_ONLY_CMDS`, `_LOCAL_CMDS`, `_AUTO_ALLOWED`; not `_SHARED_ALLOWED`.
- Produces: `_needs_web(text, skill=None, *, private=False) -> bool` returns True when `private` (caller passes `not locked`), else today's rule. `with_web` stays false once `apps_used`.
- Produces: `_pick_model_reason(..., web_asked: bool)` escalates with reason `web` only when the user typed `/web` or `@web` (or a `_WEB_SKILLS` skill is explicit), not because web is available.

- [ ] **Step 1: Failing tests**: `code-context` prints bridge output (bridge mocked); refused with `LEOBOT_SHARED=1`; refused with no indexed folder; private untagged turn passes `with_web=True` to the engine and picks `CHAT_MODEL`; `/web` turn picks `ESCALATED_MODEL` reason `web`; thread with `apps_used` gets `with_web=False`; shared turn unchanged; prompt no longer contains graph context for a code-word message without `/tokensave`; `/tokensave` path unchanged.
- [ ] **Step 2: Run, expect FAIL.**
- [ ] **Step 3: Implement**; delete `_needs_app_context` and the `graph_context` injection except the explicit `/tokensave` branch; keep `_needs_workspace` (it drives the "App workspace unavailable" note).
- [ ] **Step 4: Run, expect PASS.**
- [ ] **Step 5: Commit** `feat: code context and web search on demand`

### Task 6: `HANDOFF @handle:` and removal of wording routing

**Files:**
- Modify: `dashboard/server.py` (`_picked_by_wording` :674, `_route_bot_plain` :703-730, `Route.picked`, `_ask_turn` :2906-3050 and :3290-3359, `run_teammate` `c_skill` :3227-3235)
- Modify: `dashboard/app.js` (`picked` display near :4825, :4423, live bot event)
- Modify: `dashboard/ai_rules.py` (`TEAM_TAGGING`: when to `HANDOFF` vs `CONSULT`)
- Test: `tests/test_handoff.py`, `tests/test_bots.py` (:185-223 picked tests → removed or rewritten)

**Interfaces:**
- Produces: `_extract_handoff(text, team, *, speaker, members=None) -> tuple[str, tuple[str, str] | None]`: strips every `HANDOFF @handle: task` line (same line grammar as `_CONSULT_RE`, task capped at `MAX_CONSULT_CHARS`); returns the first valid `(bot_id, task)` where bot is active, not `speaker`, and in `members` when given.
- Produces: `_turn_setup(...) -> TurnSetup` (dataclass) extracted from `_ask_turn` :2906-3050: `bot_id, skill, explicit, may_write, may_edit, app_grant, app_turn, mount, engine, folder_rows, read_dirs, edit_dirs, with_web, model, reason, workspace`. Called once for the lead and once for a handoff target with the route forced to `(target, None, True, lead)` (tagged rights).
- Behavior: after the lead's run, consults run first (unchanged), then the handoff target runs on the same `sess` with `compose_turn_prompt(user_msg=f"{task}\n\n(Original request: {user_msg})", bot=_bot_block(..., handoff_from=lead, fresh=True))`, its own `apps_kw` minted from its own setup, `may_run()` checked first. Its reply is a new `msgs` entry `{"bot": target, "handoff_from": lead}`; its `APPROVAL:` line may create a card (it is the speaker now). `sess.note_bot(target)` so the next untagged message goes to it. One handoff per message; the target's own `HANDOFF` lines are stripped and ignored.
- Removed: `_picked_by_wording`, `Route.picked`, `"picked"` in SSE events and saved messages, `c_skill` keyword pick in `run_teammate` (teammate gets the menu instead).

- [ ] **Step 1: Failing tests**: `_extract_handoff` cases from Review Focus (inactive, non-member, self, two lines, with CONSULT); turn where lead replies with `HANDOFF @research: size the market` produces two messages, second from `research` with `handoff_from == lead`, and the next untagged message routes to `research`; untagged message with pricing words in a thread whose last speaker was `cos` routes to `cos` (no wording pick); target's `APPROVAL:` creates a card; cap reached before handoff stops it with the cap notice.
- [ ] **Step 2: Run, expect FAIL.**
- [ ] **Step 3: Implement** `_turn_setup` extraction first (no behavior change; run the full `tests/test_server_ask.py` green), then handoff, then removals and `app.js` cleanup.
- [ ] **Step 4: Run** `tests/test_handoff.py tests/test_bots.py tests/test_server_ask.py tests/test_server_people.py`, expect PASS. Run `leobot eval`; compare with Task 4's output.
- [ ] **Step 5: Commit** `feat: bots hand off by name; keyword routing removed`

### Task 7: Apps and accounts in the roster

**Files:**
- Modify: `dashboard/server.py` `_bot_block` peers section (:994-998), `_apps_accounts_note`
- Test: `tests/test_bot_menu.py`

**Interfaces:**
- Consumes: `integrations.effective_grant(ws, bot_id, ROOT)` per peer and for the speaker.
- Produces: roster line suffix ` · apps: instagram (read+write), gmail (read)` when a bot has a grant; nothing when none. Speaker gets `Your apps: ...` line. Omitted entirely in shared threads and when no API key.

- [ ] **Step 1: Failing tests**: peer with grant shows apps suffix; peer without shows none; shared card shows no apps; no API key shows no apps.
- [ ] **Step 2: Run, expect FAIL.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run, expect PASS.**
- [ ] **Step 5: Commit** `feat: roster shows which teammate holds which app`

### Task 8: Jobs: optional cap and Auto boundary

**Files:**
- Modify: `core/jobs.py` (`clean` cap :180-187), `dashboard/spend.py` `_caps` (:92), `dashboard/osctl.py` (`--cap` help :422, `main` gate :1115-1125), `dashboard/server.py` (`job_cap` construction), `dashboard/app.js` (job card / Jobs screen cap display and edit)
- Test: `tests/test_jobs.py`, `tests/test_spend_cap.py`

**Interfaces:**
- Produces: `jobs.clean("cap", v)` → `None` for `None`, `""`, `0`, `"0"`; positive finite float otherwise; negative or non-number raises `ValueError("cap: dollars per day, 0 or empty for no cap")`.
- Produces: `job_cap = (job["id"], job["cap"]) if job.get("cap") else None`; `spend._caps` unchanged signature, adds the job cap only when given.
- Produces: in `osctl.main`, when `LEOBOT_JOB` and `LEOBOT_JOB_MODE == "auto"` and any skill in `skill_loads.loaded(ROOT, LEOBOT_CHAT)` differs from the job's own skill (read the job record from `core/jobs.py` by id), every command not in `_READ_ONLY_CMDS` (except `get-skill`) raises `READONLY_TEXT["approval"]`. Marker is per chat; each job run uses its own chat id, so the next run starts clean (verify; if runs share a chat, `skill_loads.clear` at run start).

- [ ] **Step 1: Failing tests**: create job without cap → `cap is None`; job runs with no cap never refused by spend; job with cap 2.0 still refused at 2.0; Auto run that loaded its own skill can `create-memo`; Auto run that loaded another skill gets the approval text on `create-memo` but `get-project` works; next run (fresh chat) can write.
- [ ] **Step 2: Run, expect FAIL.**
- [ ] **Step 3: Implement**, including UI "No cap" display and an empty cap field meaning none.
- [ ] **Step 4: Run, expect PASS.**
- [ ] **Step 5: Commit** `feat: job caps optional; Auto runs that load another skill wait for approval`

### Task 9: Short replies setting

**Files:**
- Modify: `core/updater.py` (`DEFAULTS` gains `"short_replies": True`; `settings()` coerces non-bool to `True`), `dashboard/ai_rules.py` (new `SHORT_REPLIES` block), `dashboard/server.py` (`RAIL` :119 → `rail() -> str`; every `rail=RAIL` call site), Settings API + `dashboard/app.js` Settings screen toggle "Short replies"
- Test: `tests/test_short_replies.py`

**Interfaces:**
- Produces: `ai_rules.SHORT_REPLIES` text (Leo Bot's own words): answer first; no greeting, preamble, recap or closing offer; no filler words; fragments fine; code, commands, ids and numbers verbatim; full sentences for warnings and for questions to the user; text passed to osctl (memos, posts, briefs, intake, memory) is written in normal full prose.
- Produces: `server.rail() -> str` = `CHAT_RAIL` plus `"\n\n## Reply style\n" + SHORT_REPLIES` when `updater.settings()["short_replies"]`.

- [ ] **Step 1: Failing tests**: default settings → `rail()` contains `## Reply style`; `short_replies: false` → absent; non-bool value → treated as on; Settings POST toggles it; a new `ChatSession` gets the current `rail()`.
- [ ] **Step 2: Run, expect FAIL.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run, expect PASS.**
- [ ] **Step 5: Commit** `feat: short reply style for bots, on by default`

### Task 10: ACP agents start clean

**Files:**
- Modify: `dashboard/engines/acp.py` (agent launch args/env), Engines screen note in `dashboard/app.js`
- Test: `tests/test_engines_cli.py`

**Interfaces:**
- Produces: per known agent id, extra launch args or env that skip user extensions and context files, from each CLI's own `--help` / docs (Gemini CLI, OpenCode). Unknown or unsupported: `agent["clean"] = False` and the Engines screen shows "This CLI loads its own extensions and context files on its turns."

- [ ] **Step 1:** Check each CLI's flags (`gemini --help`, `opencode --help` if installed; else their docs). Record findings in the test as the expected args.
- [ ] **Step 2: Failing test** for the launch command / note.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run, expect PASS.**
- [ ] **Step 5: Commit** `feat: ACP agents launch without the user's own extensions where supported`

### Task 11: Docs, full suite, PR

**Files:**
- Modify: user guide chat section (`docs/` page that explains tagging), `README.md` if it mentions tags for saving, `CLAUDE.md` osctl table rows for `get-skill` and `code-context`.

- [ ] **Step 1:** Update docs: tags optional; bots load skills themselves; saves need Approve; HANDOFF vs CONSULT; job cap optional; Short replies setting.
- [ ] **Step 2:** Run full suite `python3 -m unittest discover -s tests -q` → all pass; `tests/test_no_usage_data.py` pass.
- [ ] **Step 3:** `leobot eval` → no unexpected regressions vs Task 4 baseline.
- [ ] **Step 4:** Push branch, open PR, wait for CI and CodeQL, resolve review comments, squash-merge, delete branch, restart the local server.
