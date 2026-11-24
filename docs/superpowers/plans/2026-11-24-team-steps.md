# Team Steps Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A workflow step whose lead picks its own consultants, gets blind advice from fresh sessions, and settles disagreement in capped debate rounds; plus a run idle timeout and a three-arm compare eval.

**Architecture:** The pure run state machine (`core/workflows.py`) gains `team`/`debate` step fields and `debate`/`answer` phases. `dashboard/workflow_turns.py` writes labels and instructions for them and marks which turns are read-only. The team stage reuses the chat consult path in `dashboard/server.py` (fresh read-only teammate sessions, then the lead's follow-up), with the draft quoted to each consultant, a cap of 3 and `ALSO` lines. `evals/compare.py` gains a baseline arm and order-swapped double judging.

**Tech Stack:** Python stdlib, unittest.

**Spec:** `docs/superpowers/specs/2026-11-24-team-steps-design.md`

## Global Constraints

- Public repo: fixtures use `acme`, `demo`, `profile-a`, `profile-b` only. Run `python3 -m unittest tests.test_no_usage_data` before every commit.
- Suite: `python3 -m unittest discover -t . -s tests` (the `-t .` is required).
- Commits carry no co-author trailer. Branch `feat/team-steps` in the main checkout. Never stage `README.md`, `tests/test_server_mode.py`, `docs/brand/`.
- At most 3 consultants per step (`workflows.MAX_CONSULTS`), at most 2 debate rounds. Consult, debate, answer and team-stage turns are read-only and raise no card.
- Steps without `team`/`debate` behave exactly as before (phase 4 tests stay green unchanged).

## Review Focus

- A lead that writes `CONSULT` lines naming itself, the checker, a switched-off bot or a bot outside the room: those are dropped, the rest run. Test: Task 3, `test_team_stage_pool_drops_lead_checker_and_outsiders`.
- An `ALSO` chain that would pass 3 consultants, or names a bot already consulted: it stops at 3, no repeats. Test: Task 3, `test_also_adds_one_bot_and_stops_at_three`.
- A debater reply with neither AGREE nor OBJECT: counts as agree, never blocks. Test: Task 1, `test_debate_reply_without_verdict_counts_as_agree`.
- A team step where the lead consults nobody: no debate turn runs, it goes to check. Test: Task 1, `test_team_stage_alone_skips_debate`.
- A judge pair whose two calls disagree: a tie, not a win. Test: Task 4, `test_pair_split_verdict_is_a_tie`.

---

### Task 1: State machine for team steps, debate and idle

**Files:** Modify `core/workflows.py`. Test `tests/test_workflows.py`.

**Interfaces:**
- Produces: `Step.team: bool = False`, `Step.debate: int = 0`; run keys `team` (list of bot ids), `round` (int), `panel` (list), `debated` (int), `objections` (list of `[bot, text]`), `open` (str); `IDLE_SECONDS = 1800`; `idle(run, now) -> dict` (a paused copy when running and stale, else the run); `objection(text) -> str | None`; `advance(wf, run, out, cap)` reads `out["asked"]` (list of bot ids the lead consulted in a team stage).

- [ ] Tests: `test_parse_team_and_debate_fields` (`team: yes`, `debate: 2`; `debate: 3` and `debate: 1` without team or consult raise ValueError), `test_team_stage_records_asked_and_goes_to_debate`, `test_team_stage_alone_skips_debate`, `test_debate_objection_then_answer_then_objectors_only`, `test_debate_reply_without_verdict_counts_as_agree`, `test_last_round_open_objections_reach_check`, `test_weak_check_on_team_step_retries_answer`, `test_fixed_consult_step_with_debate_saves_in_save_phase`, `test_idle_run_reads_paused`.
- [ ] Run `python3 -m unittest tests.test_workflows`: new tests FAIL.
- [ ] Implement. Phase order of a step: `stage` → (`consult`… → `revise` for fixed consults) → `debate`… ↔ `answer` → `check` → `save`. `team` = asked bots (team step) or `step.consult`. After the work phases: `check` when a checker; else `save` when gated and the step is team or debate; else done. A WEAK check retries `answer` on team or debate steps, `revise` on plain consult steps, else `stage`.
- [ ] Run tests: PASS. Commit `feat(workflows): team steps and debate rounds in the run state machine`.

### Task 2: Labels, instructions, read-only turns, idle in views

**Files:** Modify `dashboard/workflow_turns.py`. Test `tests/test_workflow_turns.py`.

**Interfaces:**
- Produces: labels `@<bot> Review step N of /<wf>` (debate) and `@<maker> Answer objections on step N of /<wf>`; ctx keys `advice` (read-only turn), `solo` (bot's own consults dropped), `team` (team stage: consults filtered by the workflow); `team_asks(ctx, asks, members, team) -> list`; `teammate_note(ctx, draft) -> str`; `also(text, team, ctx, taken, members) -> (clean_text, bot_id | None)`; `followup_note(ctx) -> str | None`.

- [ ] Tests: `test_team_stage_instructions_offer_alone_or_consult`, `test_debate_and_answer_labels_and_instructions`, `test_open_objections_shown_to_checker`, `test_team_turns_are_read_only`, `test_current_pauses_an_idle_run`.
- [ ] Run: FAIL. Implement. Run: PASS. Commit `feat(workflows): team step turns, debate and answer instructions, idle pause`.

### Task 3: Server wiring of the team stage

**Files:** Modify `dashboard/server.py` (`_plan_teammates` limit param, `_teammate_prompt` workflow note, teammate loop with `ALSO`, `_followup_prompt` ask param, finish payload `asked`). Test `tests/test_workflow_turns.py`.

- [ ] Tests: `test_team_stage_runs_blind_consults_and_revises`, `test_team_stage_pool_drops_lead_checker_and_outsiders`, `test_also_adds_one_bot_and_stops_at_three`, `test_team_step_full_run_through_debate`.
- [ ] Run: FAIL. Implement. Run full suite: PASS. Commit `feat(workflows): the team stage consults blind teammates the lead picks`.

### Task 4: Workflow template and three-arm compare

**Files:** Create `skills/team-launch-debate/SKILL.md`. Modify `evals/compare.py`, `evals/cases/compare.json` (6 acme cases, `baseline: team-launch-plan`, `workflow: team-launch-debate`), docs that list workflows. Test `tests/test_evals.py`, `tests/test_workflows.py`.

**Interfaces:** case key `baseline` optional; `pairs(case) -> list[tuple]`; `judge_pair(case, arms, a, b, judge) -> dict` (two calls, order swapped); result `pairs: {"single": {...}, "baseline": {...}}`; summary lines per pair and `Gain over baseline: yes|no`.

- [ ] Tests: `test_pair_split_verdict_is_a_tie`, `test_pair_agreed_verdict_wins_and_scores_average`, `test_baseline_arm_runs_and_is_judged`, `test_summary_reports_gain_over_baseline`, template loads with `team` and `debate`.
- [ ] Run: FAIL. Implement. Suite PASS. Commit `feat(evals): compare against a baseline workflow, each pair judged twice`.

### Task 5: Live eval, report, PR

- [ ] `python -m evals --compare --yes` on the 6 cases. Read the plans. Report wins, scores, costs to the owner.
- [ ] PR, CI, merge. Update memory.
