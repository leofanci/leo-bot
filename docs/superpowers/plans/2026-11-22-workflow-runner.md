# Workflow Runner Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The owner starts a workflow template in a private thread with `/<workflow> <brief>`, and the server runs it step by step. Each step goes to that step's bot through the normal ask path. An optional checker bot reviews a step, and a weak step is retried once. The run pauses on any approval card, on a blocked step, at a cost cap, or on Stop.

**Architecture:**
- A workflow is `skills/<name>/SKILL.md` with `kind: workflow` and a `## Steps` list that `core/workflows.py` parses.
- `core/workflows.py` holds the parser, the per-thread run record and the pure state machine.
- The server hooks into `_handle_ask`. `prepare` turns a start message or a `workflow: "next"` tick into an ordinary stage body: a tagged message plus the stage instructions as `context`. `finish` reads that turn's outcome, moves the run along and emits one `workflow` SSE event before `[DONE]`.
- The open browser tab sends the next tick as soon as a stage ends, so every stage streams live and reuses the owner's session, Stop, caps and approval holds. Closing the tab leaves the run idle, and Continue picks it up again.

**Tech Stack:** Python 3 standard library, `unittest`, vanilla JS in `dashboard/app.js`.

**Spec:** `docs/superpowers/specs/2026-11-21-skills-workflows-loops-design.md` (sections "Standard", "Workflow run rules", "Safety"). This is plan 2 of 4. Plan 3 covers loops (jobs that hand off and start workflows), plan 4 covers library content.

## Global Constraints

- Tests run with `python3 -m unittest discover -t . -s tests`. Run `python3 -m unittest tests.test_no_usage_data` before every commit.
- Public repo: fixtures use only `acme` and the shipped bot ids (`cos`, `research`, `cmo`, `gtm`, `product`, `social`, `cto`). No real names and no user quotes in tracked files.
- Plan approval is inert. A workflow never saves on its own: a step either writes its output in the thread (`gate: no`) or proposes the save with one `APPROVAL:` line (`gate: approval`). Each send, spend or delete inside a step keeps its own card.
- A run pauses only on a card (approval or held app action), a blocked step, a step still weak after one retry, the run cost cap, Stop, a failed turn, or a closed tab. Nothing else waits for the person.
- Only the owner starts or drives a workflow, and only in a private thread: never in a shared room, an invited person's turn or a job. One run per thread.
- Every stage is an ordinary chat turn. Chain limits (`chain_hops`, `chain_cost`), daily caps, Stop everything and approval holds apply unchanged.
- Run cost cap: Settings, General, `workflow_cost` in dollars per run. `0` means no cap, the maximum is the same as `CHAIN_COST_MAX`.
- Commit messages carry no co-author trailer. Work on branch `feat/workflow-runner` in the main checkout (no extra worktree), then PR, CI, merge.

**Not in this plan:** starting a workflow from a loop or job (plan 3), team stages with debate rounds (spec phase 5), workflow-specific library content beyond the one reference template.

## Review Focus

- A checker reply that is neither `PASS` nor `WEAK:` counts as weak, because the checker defaults to rejecting.
- A tick for a thread whose run is paused on an unanswered card is refused with a plain message, and no model turn runs.
- Approve on the card of a paused run advances it only when that approval turn raised no new card. Reject leaves the run paused.
- An ordinary message in a thread with a run never advances it. Only ticks and approvals do.
- A `@handle` or `/skill` inside the brief never co-tags a bot or loads a skill. Both are neutralized in the stage label.
- A workflow whose step bot is inactive or lacks the step's skill refuses to start and lists the problems. A bot turned off mid-run pauses the run at that step.
- Deleting a thread deletes its run record.

---

### Task 1: Workflow templates (parse and validate)

**Files:**
- Create: `core/workflows.py`
- Create: `skills/idea-to-experiment/SKILL.md`
- Modify: `dashboard/server.py` (`_skills_index`: add `"kind": fm.get("kind", "skill")`)
- Modify: `tests/test_bots.py:12` (`SKILLS` leaves out folders whose frontmatter has `kind: workflow`)
- Test: `tests/test_workflows.py`

**Interfaces:**
- Produces:
  - `Step(skill: str, bot: str, output: str, gate: bool, checker: str | None)` (frozen dataclass)
  - `Workflow(name: str, description: str, steps: tuple[Step, ...], body: str)` (frozen dataclass; `body` is the file body without the steps list)
  - `parse(text: str, name: str) -> Workflow` raises `ValueError` with a one-line reason
  - `load(name: str) -> Workflow | None` reads `skill_loads.SKILLS_DIR / name / "SKILL.md"`; None when the file is missing, is not `kind: workflow`, or fails to parse
  - `names() -> list[str]` lists every workflow that loads
  - `problems(wf: Workflow, team) -> list[str]` returns one line per step whose bot is missing or inactive, or whose skill is not in `team.skills_of(bot)`, or whose checker is missing or inactive
  - `MAX_STEPS = 12`

Step line format (under `## Steps`, one numbered line per step, fields split by `|`, keys case-insensitive):
`1. skill: problem-validation | bot: product | output: problem verdict and evidence gaps | gate: no`
`gate` is `no` or `approval`. `checker` is optional. Parse errors: no `## Steps`, zero steps, more than `MAX_STEPS`, a missing `skill`/`bot`/`output`, a `gate` other than `no`/`approval`, a skill name not in `skill_loads.names()`, a skill that is itself a workflow, a checker equal to the step's bot.

- [ ] **Step 1: Write the failing tests** in `tests/test_workflows.py`, class `Templates`:
  - `test_parse_steps`: a fixture text with two steps, one with `checker: research`. Assert `len(wf.steps) == 2`, `wf.steps[1].checker == "research"`, `wf.steps[0].gate is False`, `wf.steps[1].gate is True`, and that `"## Steps"` is not in `wf.body`.
  - `test_parse_errors`: one subTest per error listed above, each `assertRaises(ValueError)`.
  - `test_shipped_template_loads`: `load("idea-to-experiment")` is not None, has 4 steps, and `problems(wf, bots.team(paths.ROOT, bots.HQ, None)) == []`.
  - `test_plain_skill_is_not_a_workflow`: `load("positioning") is None`, and `"idea-to-experiment" in names()`.
  - `test_problems`: a team double whose `get("cmo")` is inactive returns a line naming `@cmo`.
- [ ] **Step 2: Run** `python3 -m unittest -v tests.test_workflows`. Expected: FAIL (`ModuleNotFoundError: core.workflows`).
- [ ] **Step 3: Implement `core/workflows.py`** (parse with `fileops._parse_frontmatter`, the same parser as skills) and write `skills/idea-to-experiment/SKILL.md`:
  - Frontmatter: `name: idea-to-experiment`, `kind: workflow`, and a `description` that starts with `Workflow.`
  - Steps:
    1. `problem-validation`, bot `product`, gate `no`.
    2. `icp-research`, bot `research`, gate `no`, checker `product`.
    3. `positioning`, bot `cmo`, gate `no`.
    4. `experiment-design`, bot `product`, gate `approval`, checker `research`.
  - Body sections: `## Stop when` and `## Always ask first`. The second lists sending, spending, deleting and any write outside the project. Generic wording only.
  - Add `kind` to `_skills_index` and update the `SKILLS` set in `tests/test_bots.py`.
- [ ] **Step 4: Run** `python3 -m unittest -v tests.test_workflows tests.test_bots tests.test_server_ask`. Expected: PASS.
- [ ] **Step 5: Commit** `feat(workflow): workflow templates as skills with kind: workflow`.

### Task 2: Run record and state machine

**Files:**
- Modify: `core/workflows.py`
- Modify: `core/general.py` (add `workflow_cost`, validated like `chain_cost`)
- Test: `tests/test_workflows.py`

**Interfaces:**
- Consumes: `Workflow`, `Step` (Task 1).
- Produces:
  - Run dict keys: `name, chat, ws, room, brief, step (int), phase ("stage"|"check"|"save"|"approve"), card_phase (phase that raised the card, or ""), retried (bool), notes (str), status ("running"|"paused"|"done"|"stopped"), why (str), cost (float), started, updated`
  - `new_run(wf, brief, ws, room) -> dict` (chat None until the first turn saves the thread)
  - `get(root, chat) -> dict | None`, `save(root, run) -> None`, `drop(root, chat) -> None`. File: `root / "team" / "workflow_runs" / f"{chat}.json"`, written with `bots._atomic_write`. Chat ids are checked with the same regex as `skill_loads._CHAT_RE`, and a bad id reads None and writes nothing.
  - `Outcome` dict keys, filled by the server (Task 3): `chat, cost, card (bool), answer ("approved"|"rejected"|None), stopped (bool), failed (bool), replies (list of (bot_id, text))`
  - `advance(wf, run, out, cap) -> dict` returns the new run, pure. Rules:
    - Add `out.cost` to the run's `cost`.
    - A stopped or failed turn leaves the phase as it is and pauses the run, with `why` `"Stopped."` or `"The last step failed."`
    - Any phase: a card moves to phase `approve`, paused, `why` `"Waiting for your approval."`, and `card_phase` keeps the phase that raised it.
    - Phase `stage`:
      - A `BLOCKED: <reason>` line in the step bot's reply counts as weak with that reason (spec: weak or blocked retries once).
      - A step with a checker moves to phase `check`.
      - A step with gate `approval` and no checker is done (its stage proposed the save; no card means the bot found nothing to save).
      - Otherwise the step is done.
    - Phase `check`, on `verdict` of the checker's reply:
      - pass: phase `save` when the step's gate is `approval`, else the step is done.
      - weak and not yet retried: phase `stage`, `retried=True`, `notes` set.
      - weak after the retry: paused, `why` `f"Step {n} is still weak: {notes}"`.
    - Phase `save` (approval gate with a checker: the stage wrote the output without saving, the checker passed it, now the step bot proposes the save): no card means the step is done.
    - Phase `approve`: answer `"approved"` with no new card goes on from `card_phase`: phase `check` when `card_phase` is `stage` and the step has a checker, else the step is done. The run goes back to running. Anything else leaves it unchanged.
    - When a step is done: `step+1`, `retried=False`, `notes=""`, phase `stage`, and status `done` past the last step.
    - After the rules above, a run that is not done with `cap > 0` and `cost >= cap` pauses with `"Reached this run's cost cap."`
  - `verdict(text) -> tuple[bool, str]`: the first line matching `^PASS\b` returns `(True, "")`, and `^WEAK:\s*(.+)` returns `(False, notes)`. Anything else returns `(False, "The checker gave no PASS or WEAK line.")`.
  - `resume(run) -> dict`: a paused run becomes running, and a stopped or done run stays as it is. A run paused in phase `approve` (its card was rejected) goes back to `card_phase`, so Continue redoes that turn. A run paused weak after its retry starts the step again with `retried=False`.
  - `general.view()["workflow_cost"]` returns a float, and `workflows.cost_cap() -> float` never raises.

- [ ] **Step 1: Write the failing tests** in class `Runs`:
  - `test_store_round_trip_and_bad_id` (`"../x"` reads None).
  - `test_plain_steps_advance_to_done` (two gate-no steps, no checker, two outcomes: `status == "done"`, `step == 2`).
  - `test_checker_pass`, `test_checker_weak_retries_once_then_pauses`, `test_checker_without_verdict_is_weak`.
  - `test_card_pauses_then_approve_advances`. Also assert that `answer="rejected"` stays paused and that approved with a new card stays paused.
  - `test_blocked_retries_once_then_pauses_with_reason`, `test_stopped_and_failed_pause`.
  - `test_approval_gate_with_checker_checks_before_save` (stage, check PASS, save, card, approved: done).
  - `test_rejected_card_then_resume_redoes_turn`.
  - `test_cost_cap_pauses` (cap 1.0, outcome cost 1.2), and `test_resume`.
  - In `tests/test_chain.py`, `ChainSettings` gets `test_workflow_cost_validates`, which reuses the `chain_cost` bad values.
- [ ] **Step 2: Run** `python3 -m unittest -v tests.test_workflows tests.test_chain`. Expected: FAIL (missing names).
- [ ] **Step 3: Implement** the record functions, `advance`, `verdict`, `resume`, `cost_cap` and the `workflow_cost` setting.
- [ ] **Step 4: Run** the same command. Expected: PASS.
- [ ] **Step 5: Commit** `feat(workflow): run record and step state machine`.

### Task 3: Server hook (start, tick, finish)

**Files:**
- Create: `dashboard/workflow_turns.py`
- Modify: `dashboard/server.py`. Changes:
  - `_handle_ask` calls `prepare` before `_mark_busy` and passes `viewer={**viewer, "workflow": ctx}`.
  - `_ask_turn`'s finally fills `viewer["workflow"]["out"]` after `record_turn`, then calls `finish` and emits its event before `[DONE]`.
  - Add the routes `GET /api/workflow?chat=` and `POST /api/workflow/stop` to the owner's handlers.
  - The chat delete route calls `workflows.drop`.
- Test: `tests/test_workflow_turns.py`

**Interfaces:**
- Consumes: Tasks 1-2.
- Produces:
  - `prepare(body, viewer, team, room, shared) -> tuple[dict, dict | None] | str`. It returns the body to run plus a ctx `{"run", "wf", "kind": "stage"|"check"|"turn"}`, or a refusal string that the server sends with `_sse_message`. Cases:
    - A non-owner, a job, or a shared room passes through with ctx None when the message is not a workflow start or tick. A workflow start or tick there is refused: `"Workflows run in your own private threads."`
    - Start (the message's first word is `/<name>`, `load(name)` succeeds, and there is no running or paused run in `chat_id`): `problems` are refused as one line each. Otherwise a new run is made and stage 1 is built.
    - Tick (`body.get("workflow") == "next"`): the run must exist and not be done or stopped. A pending approval card in the chat (`approvals.pending` filtered by chat) is refused: `"Approve or reject the card first."` A paused run is resumed. The next stage or check is built.
    - Any other turn in a chat with a run paused in phase `approve`: pass through with ctx `kind: "turn"`, so an approval answer can advance it.
  - Stage body: `messages=[{"role": "user", "content": label}]`, with the client's `context` replaced by the stage instructions and `chat_id`/`room_id` kept. The label is `f"@{step.bot} /{step.skill} Step {n} of {total} of /{wf.name}"`, plus `f": {brief}"` on step 1. In the brief, every `@` and `/` that starts a word is dropped, and the brief is cut to 300 characters.
  - Stage instructions (context) cover:
    - The workflow body.
    - The brief.
    - The step's output.
    - The gate line. For `no`, and for `approval` with a checker: "Write the output in your reply. Do not save anything." For `approval` without a checker: "Propose the save with one APPROVAL: line."
    - "If you cannot do this step, end with one line `BLOCKED: <what is missing>`."
    - On a retry, the checker's notes.
  - Check body: label `f"@{step.checker} Check step {n} of /{wf.name}"`. Context: the step's output spec and "Reply with one line, PASS or WEAK: <what is missing>. Do not save anything."
  - Save body: label `f"@{step.bot} Save step {n} of /{wf.name}"`. Context: "Propose the save of the output from your earlier reply in this thread with one APPROVAL: line. Do not redo the work."
  - `finish(ctx, out) -> dict | None`:
    - Kind `stage`/`check`: `advance`, then `save` (setting `chat` from `out` on the first turn).
    - Kind `turn`: advance only when phase is `approve` and the turn carried an approval answer.
    - Returns the event `{"workflow": view(run, wf)}`.
  - `view(run, wf) -> dict` returns `{name, step, total, status, why, phase, next: status == "running", label}`, where `label` is the next stage or check label. The client sends `label` as the tick's text.
  - Outcome in `_ask_turn`:
    - `card`: an approval was added, or `held` app actions this turn.
    - `answer`: `body.get("approval")`.
    - `stopped`: `may_run()` is false at the end, or the client left (BrokenPipe).
    - `failed`: `live["status"] == "failed"`-equivalent. Add a local `failed` flag, because `live` is job-only.
    - `replies`: `[(m["bot"], m["content"]) for m in msgs]`.

- [ ] **Step 1: Write the failing tests** in `tests/test_workflow_turns.py`, driving `Handler._handle_ask` with a fake engine the way `tests/test_handoff.py` does:
  - `test_start_runs_step_one_and_emits_next`: `/idea-to-experiment acme sells widgets @cmo` → the lead is `product`, `cmo` never runs (neutralized), the saved user message starts with `@product /problem-validation Step 1 of 4`, the context holds "Do not save anything", and the event is `next: True`.
  - `test_tick_runs_checker_then_retry_then_pause`: step 2 with replies `WEAK: no segment size` then `WEAK: still none` → the run is paused with `why` containing `still weak`.
  - `test_gate_card_pauses_and_approve_advances`: step 4 writes, its checker passes, the save turn asks `APPROVAL:` → paused. A tick is refused with "Approve or reject the card first." `approval: "approved"` → the run is done (`next: False`).
  - `test_ordinary_message_never_advances`.
  - `test_shared_room_and_person_refused`, `test_problem_refuses_start` (a team where `cmo` is inactive → the reply names `@cmo`, no model turn).
  - `test_stop_route_and_delete_drop_the_run`.
- [ ] **Step 2: Run** `python3 -m unittest -v tests.test_workflow_turns`. Expected: FAIL.
- [ ] **Step 3: Implement** `dashboard/workflow_turns.py` and the server wiring above. Keep `_ask_turn`'s changes to the outcome fill and one `finish` call.
- [ ] **Step 4: Run** `python3 -m unittest -v tests.test_workflow_turns tests.test_handoff tests.test_approvals tests.test_server_ask`. Expected: PASS.
- [ ] **Step 5: Commit** `feat(workflow): server runs a workflow step by step in a thread`.

### Task 4: Client run bar and auto-tick

**Files:**
- Modify: `dashboard/app.js`, `dashboard/style.css` (or the stylesheet the chat uses)
- Modify: Settings, General in `app.js` (a `workflow_cost` field next to the chain fields at about line 3105)
- Modify: `docs/` user doc that covers bot chains (one short section: start, pause reasons, Continue, Stop, cost cap)
- Test: `tests/test_workflow_turns.py` (static checks in the style of existing app.js tests, if the repo has them; otherwise none for JS)

**Interfaces:**
- Consumes: the `workflow` SSE event and `GET /api/workflow?chat=` → `view` or `{}` (Task 3).

Client behavior:
- On a `workflow` event, keep the view. After the stream ends, and if the person did not press Stop, call `send(view.label, { workflow: "next" })` when `view.next` is true.
- A run bar sits above the composer when the open thread has a run that is not done. It shows `/<name> · step n of total · running|paused: why`, plus Continue (paused only, sends the tick) and Stop workflow (POST `/api/workflow/stop`, then hide).
- Opening a thread fetches `/api/workflow?chat=`.
- Stop (ESC or the button) aborts the tick chain. The run stays as the server saved it, and Continue resumes.
- The skill picker labels `kind: "workflow"` entries "workflow".

- [ ] **Step 1:** Wire the event, the auto-tick and the run bar, then the Settings field.
- [ ] **Step 2: Verify in the browser**:
  - Start `/idea-to-experiment` in a demo workspace thread. Watch two steps stream.
  - Press Stop mid-step. The bar shows paused, and Continue resumes.
  - At the gate, approve the card. The run goes on.
  - Expected: no console errors.
- [ ] **Step 3: Run** the full suite `python3 -m unittest discover -t . -s tests`. Expected: OK.
- [ ] **Step 4: Commit** `feat(workflow): run bar, auto-continue and cost cap setting`.
