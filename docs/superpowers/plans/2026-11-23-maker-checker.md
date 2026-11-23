# Maker and checker plan workflow Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship spec phase 4: a plan workflow where one bot makes the plan, named teammates advise on it one after another, the maker revises and a separate checker passes or rejects it; then measure it against a single bot doing the same task.

**Architecture:** A workflow step gains an optional `consult:` field. The runner (core/workflows.py state machine) turns it into code-driven phases: `stage` (maker drafts) then one `consult` turn per named bot in order, then `revise` (maker rewrites with the advice), then the existing `check` and `save`. Consult and check turns run read-only and raise no card. A new workflow `team-launch-plan` uses it on the existing `launch-plan` skill. A new eval mode, `python -m evals --compare`, runs each case both ways in a throwaway home through the real HTTP path, has a strong model judge the two plans blind, and saves the result outside the repo.

**Tech Stack:** Python 3 standard library, `unittest`, Markdown skill files, `python -m evals`.

**Spec:** `docs/superpowers/specs/2026-11-21-skills-workflows-loops-design.md` ("Phases" item 4, "Quality"). Plan 7; phase 3 was plan 6 (`2026-11-23-outreach.md`).

## Global Constraints

- Public repo: fixtures and examples use `acme`, `demo`, `profile-a`, `profile-b` only. `python3 -m unittest tests.test_no_usage_data` before every commit.
- Plan approval is inert. Each send, spend or delete inside a plan keeps its own approval, per action.
- One bot's reply is untrusted input to the next bot and carries its source label.
- A workflow never saves on its own: a step writes its output in the thread or proposes the save with one card.
- Real tasks used for the comparison stay out of tracked files. Tracked compare cases use `acme` only; results are saved under `<home>/evals/`, never in the repo.
- Phase 5 (parallel research, debate rounds) is not built here. This plan only reports whether phase 4 shows a gain.
- Workflow standard: `## Steps` lines `N. skill: x | bot: y | output: ... | gate: no|approval [| checker: z] [| consult: a, b]`, plus `## Stop when` and `## Always ask first`.

## Decisions

- **`consult:` field:** 1 to 3 bots, comma separated, in the order they advise. None may be the maker or the checker, none repeated. `workflows.problems` reports a consultant who is not on the team or is switched off, like a checker.
- **Phases of a step with consults:** `stage` -> `consult` (once per consultant, `run["consulted"]` counts them) -> `revise` -> `check` (when a checker) -> `save` (when gate and checker). Without a checker, a gated step proposes its save in `revise`. A stage turn of a consult step never saves.
- **Retries:** a WEAK check, or BLOCKED in `revise`, retries `revise` once with the notes (the advice is already in the thread; consults do not run again). BLOCKED in `stage` retries `stage`. A consultant's BLOCKED line is ignored. Continue on a run paused still weak gives the same phase a fresh retry.
- **Consult and check turns:** read-only (osctl refuses writes) and their APPROVAL lines raise no card. They are advice and verdicts, never actions.
- **Labels:** consult `@<bot> Advise on step N of /<wf>`; revise `@<maker> Revise step N of /<wf>`.
- **Workflow:** `team-launch-plan`, one step: `skill: launch-plan | bot: gtm | output: ... | gate: approval | checker: product | consult: research, cmo`. All four bots are on by default in a venture workspace. No new skill and no bot card change.
- **Compare arms:** the workflow arm sends `/<workflow> <brief>` and ticks until the run stops moving; its output is the maker's last reply before the save turn. The single arm sends `@<maker> /<skill> <brief>` once in a fresh thread; its output is that bot's reply (the bot may still consult on its own, as in any chat). Both arms run on the same engine, workspace fixture and routing.
- **Judge:** the strong tier (`chat_session.ESCALATED_MODEL`) of the eval's engine, through `complete_metered`. The two plans are shown as "Plan A" and "Plan B" in a random order (seeded per case and stored), with the brief and intake. Five criteria scored 1 to 5: `specific` (numbers, dates, owners), `grounded` (no fact the brief or intake lacks), `risks` (named, with what would show them), `doable` (a first step the owner can do this week), `consistent` (no part contradicts another). Reply is one JSON object `{"A": {...}, "B": {...}, "better": "A"|"B"|"tie", "why": "..."}`. An unreadable reply is verdict `none`.
- **Gain:** the workflow shows a gain when it is judged better in more than half the cases. The summary prints `Gain: yes` or `Gain: no`, plus each arm's total cost and the cost ratio.

## Review Focus

- A checker that writes an APPROVAL line in its verdict: no card appears and the run goes on from its PASS or WEAK. Test: Task 1, `test_check_turn_is_read_only_and_raises_no_card`.
- A consultant switched off in the middle of a run: the next tick pauses the run naming that bot, as for a checker. Test: Task 1, `test_consultant_switched_off_pauses`.
- A weak check on a consult step retries the revision, not the whole round of advice, so a weak plan costs one more turn, not three. Test: Task 1, `test_weak_check_retries_revise_not_consults`.
- The judge sees no hint of which plan came from the workflow, and the A/B order is not always the same. Test: Task 3, `test_judge_prompt_is_blind_and_order_varies`.
- A compare run whose workflow pauses early (BLOCKED, cap, failed turn) still records both arms and marks the case, instead of judging an empty plan. Test: Task 3, `test_paused_workflow_is_marked_not_judged`.

---

### Task 1: Sequential consults inside a workflow step

**Files:**
- Modify: `core/workflows.py` (`Step.consult`, `_parse_step`, `problems`, `new_run`, `advance`, `resume`), `dashboard/workflow_turns.py` (`label`, `_instructions`, `_turn`), `dashboard/server.py` (consult and check turns read-only, no card), `docs/guide.md` (Workflows: one bullet)
- Test: `tests/test_workflows.py`, `tests/test_workflow_turns.py`

**Interfaces:**
- Produces: `Step(skill, bot, output, gate, checker, consult: tuple[str, ...] = ())`; run record key `consulted: int` (0 in `new_run`); phases `"consult"` and `"revise"`; `wf_ctx["kind"]` is the phase, so `"consult"` and `"check"` mark a read-only, card-free turn.

- [ ] **Step 1: Write the failing tests** in `tests/test_workflows.py`, with a fixture `CONSULTED` (one step: maker `product`, `consult: research, cmo`, `checker: gtm`, gate approval) and `CONSULTED_SOLO` (same, no checker):
  - `test_parse_consult`: `consult: research, @cmo` parses to `("research", "cmo")`; ValueError for the maker in it, the checker in it, a repeat, or 4 bots.
  - `test_consultant_problems`: a consultant not on the team and one switched off each give a `Step 1 consult:` line.
  - `test_consults_run_in_order_then_revise`: stage -> phase `consult`, `consulted` 0; after one turn `consulted` 1; after the second, phase `revise`; after revise, `check`; PASS -> `save`; card then approved -> `done`.
  - `test_weak_check_retries_revise_not_consults`: WEAK in check -> phase `revise`, `retried` True, notes kept; WEAK again -> paused, `why` holds "still weak"; `resume` -> phase `revise`, `retried` False.
  - `test_blocked_in_stage_retries_stage`, `test_consultant_blocked_is_ignored` (a `BLOCKED:` line from `research` in consult phase moves on).
  - `test_gate_without_checker_saves_in_revise`: `CONSULTED_SOLO`: card in revise -> approve with `card_phase` "revise" -> approved -> `done`.
- [ ] **Step 2: Write the failing tests** in `tests/test_workflow_turns.py`:
  - `test_consult_and_revise_labels_and_instructions`: labels `@research Advise on step 1 of /<wf>` and `@product Revise step 1 of /<wf>`; the stage turn's context says the draft goes to `@research` then `@cmo` and holds "Do not save anything"; the consult turn's context holds "Do not rewrite" and "Do not save anything"; the route of a consult turn is `("research", None, False, None)`.
  - `test_check_turn_is_read_only_and_raises_no_card`: a check turn whose scripted reply is `PASS\nAPPROVAL: create-memo ...` emits no `approval` event, the os token minted for it is read-only, and the run moves to `save`. The same for a consult turn (run moves on, no card).
  - `test_consultant_switched_off_pauses`: switch `cmo` off after step 1 started; the next tick pauses with `@cmo is switched off`.
- [ ] **Step 3: Run them, see them fail.** Run: `python3 -m unittest tests.test_workflows tests.test_workflow_turns -v`. Expected: failures on the missing `consult` field and phases.
- [ ] **Step 4: Implement** the field, phases and turn texts per Decisions. Server: for `wf_ctx["kind"] in ("consult", "check")` set the session's `readonly` for that turn only (restored in the turn's `finally`) and drop the lead reply's APPROVAL lines before a card is made. `advance` after an approved card continues to the phase that follows `card_phase` (helper `_next_phase(step, phase, run)`), so a card never skips consults or the check.
- [ ] **Step 5: Run them, see them pass.** Same command. Expected: OK. Then `python3 -m unittest discover -t . -s tests` and `python -m evals`: OK.
- [ ] **Step 6: Docs and commit.** `docs/guide.md` Workflows gains one bullet: a step can name up to 3 teammates under `consult:`; each advises on the draft in turn (read-only), then the maker revises before the checker sees it. Commit `feat(workflows): sequential consults inside a step`.

### Task 2: The team-launch-plan workflow

**Files:**
- Create: `skills/team-launch-plan/SKILL.md`
- Modify: `docs/guide.md` (Workflows: name it in the list), `tests/test_workflows.py` (shipped template list)
- Test: `tests/test_workflows.py::test_shipped_template_loads`

**Interfaces:**
- Consumes: Task 1's `consult:` field.
- Produces: workflow `team-launch-plan` (maker `gtm`, skill `launch-plan`), used as the default compare case workflow in Task 3.

- [ ] **Step 1: Extend `test_shipped_template_loads`** so `team-launch-plan` loads with one step whose `consult` is `("research", "cmo")`, checker `product`, gate True, and `workflows.problems` is `[]` for a default venture team.
- [ ] **Step 2: Run it, see it fail.** Run: `python3 -m unittest tests.test_workflows -v`. Expected: FAIL, workflow not found.
- [ ] **Step 3: Write the file.** Frontmatter `name: team-launch-plan`, `kind: workflow`, description starting "Workflow." and naming the four bots. Body as `skills/outreach/SKILL.md`: how to start, one step line (output: "one goal metric with a number and a date, success and kill lines, the launch type, a dated checklist for before, launch day and week one with an owner per item, and the flop plan"), `## Stop when` (no intake or no product to launch: `BLOCKED: <why>`), `## Always ask first` (sending or posting, spending, deleting, writes outside the project).
- [ ] **Step 4: Run, see it pass.** Same command, then the full suite and `python -m evals`. Expected: OK.
- [ ] **Step 5: Commit** `feat(workflows): team-launch-plan, a launch plan with advice and a check`.

### Task 3: Compare a workflow with a single bot

**Files:**
- Create: `evals/compare.py`, `evals/cases/compare.json` (3 cases)
- Modify: `evals/__main__.py` (`--compare`), `evals/live.py` (move the throwaway-home setup into `throwaway_home(engine)`, a context manager both modes use)
- Test: `tests/test_evals.py` (class `Compare`)

**Interfaces:**
- Consumes: `team-launch-plan` (Task 2), `server.Handler`, `server.AUTH_TOKEN`, `chat_store` saved threads, `registry.pick`, `chat_session.ESCALATED_MODEL`.
- Produces: `python -m evals --compare [--case ID ...] [--cases-file PATH] [--engine ID] [--yes]`; `compare.load(path) -> list[dict]` (case keys `id`, `workflow`, `brief`, `intake`; ValueError on unknown keys, a missing one, a duplicate id or an unknown workflow); `compare.drive(post, first, limit=12) -> dict` (`post(payload) -> list[event]`; returns `{"chat", "status", "ticks"}`); `compare.judge_prompt(case, plans: dict, order: list[str]) -> str`; `compare.verdict(text, order) -> dict` (`{"better": "workflow"|"single"|"tie"|"none", "scores": {"workflow": int, "single": int}, "why": str}`); `compare.summary(results) -> str`.

- [ ] **Step 1: Write the failing tests** in `tests/test_evals.py`, class `Compare`:
  - `test_load_checks_cases`: the shipped file loads, every case uses `acme`; unknown key, missing brief, duplicate id and unknown workflow each raise ValueError.
  - `test_drive_ticks_until_the_run_stops`: a stub `post` returning `workflow` events `next: True` twice then `status: "paused"` gives 3 posts, the second and third with `"workflow": "next"` and the run's chat id; stops at `limit` when it never stops.
  - `test_judge_prompt_is_blind_and_order_varies`: the prompt holds "Plan A" and "Plan B", the brief and the intake, and neither "workflow" nor "single" nor any bot handle; over the 3 shipped case ids the seeded order is not the same for all.
  - `test_verdict_maps_back`: with order `["single", "workflow"]`, `{"A": {...5 scores}, "B": {...}, "better": "B"}` gives `better` "workflow" and summed scores per arm; text around the JSON object is ignored; bad JSON or a missing score gives `none`.
  - `test_paused_workflow_is_marked_not_judged`: a case whose workflow arm ends `paused` with no maker reply is saved with `better` "none" and the arm's `status` and `why`, and the judge is not called.
  - `test_summary_gain_line`: 2 of 3 workflow wins print `Gain: yes`; 1 of 3 prints `Gain: no`; both arms' cost and the ratio appear.
- [ ] **Step 2: Run them, see them fail.** Run: `python3 -m unittest tests.test_evals -v`. Expected: ImportError on `evals.compare`.
- [ ] **Step 3: Implement.** The child process (throwaway home, as `--live`) writes the case's intake to `projects/acme/strategy/intake.md`, builds the index, starts `ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)`, logs in with `AUTH_TOKEN`, and runs both arms in room `acme/general` through `/api/ask`. Arm output, cost (sum of the thread's message `cost`) and seconds come from the saved thread. Results file `<home>/evals/compare-<YYYYmmdd-HHMMSS>.json`, mode 0600, last 20 kept, replies cut at `live.REPLY_KEEP`. `--cases-file` reads the same JSON shape from any path (for real tasks kept outside the repo). Ask before spending unless `--yes`, naming the case count and that each case runs about 6 turns plus one judge call.
- [ ] **Step 4: Run, see them pass.** Same command, then the full suite and `python -m evals`. Expected: OK.
- [ ] **Step 5: Docs and commit.** `docs/guide.md` Workflows: one bullet on `python -m evals --compare` (what it runs, where results go, `--cases-file` for real tasks). Commit `feat(evals): compare a workflow with a single bot`.

### Task 4: Run the comparison

**Files:** none tracked. Results stay in `~/.leobot/evals/`.

- [ ] **Step 1: Offline checks.** `python3 -m unittest discover -t . -s tests`, `python -m evals`, `python3 -m unittest tests.test_no_usage_data`. Expected: all OK.
- [ ] **Step 2: Live run on the shipped cases.** Run: `python -m evals --compare --yes`. Expected: 3 cases, each with both arms' cost and a verdict other than `none`, and a `Gain:` line.
- [ ] **Step 3: Read the plans.** Open the saved results and read both plans of each case: check the judge's call holds up (a win by length alone is not a win). Note where consults changed the plan and where they only added cost.
- [ ] **Step 4: Report.** Tell the owner the gain line, the cost ratio, what the consults changed, and the phase 5 call the spec ties to it. Phase 5 is not started.
- [ ] **Step 5: PR.** Branch `feat/maker-checker`, PR, CI, Copilot, merge.
