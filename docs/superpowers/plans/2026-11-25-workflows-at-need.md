# Workflows Built at Need Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A bot drafts a workflow plan for any request as a `PLAN:` block in its reply; the owner approves it on one card and the existing runner runs it, with no file in `skills/`.

**Architecture:** A new `core/plans.py` cuts and parses the plan block with the existing step-line parser (`skill:` optional). `core/workflows.py` learns runs whose steps live in the run record (`workflows.of_run`) and saved templates in the data folder. `dashboard/workflow_turns.py` starts a run from a stored plan message (`workflow: "plan"` turn body); `dashboard/server.py` gives owner turns a short plan rule, cuts the block after the turn, stores it on the message, and serves reject and save-as-template endpoints. `dashboard/app.js` renders the card. `evals/compare.py` gains a plan arm.

**Tech Stack:** Python 3 stdlib (unittest), vanilla JS in `dashboard/app.js`.

**Spec:** `docs/superpowers/specs/2026-11-24-workflows-at-need-design.md`

## Global Constraints

- PUBLIC repo: fixtures, eval cases, comments and commit messages use generic slugs only (`acme`, `demo`, `profile-a`). Run `python -m pytest tests/test_no_usage_data.py` before every commit.
- Limits stay today's: `MAX_STEPS = 12`, `MAX_CONSULTS = 3`, `MAX_DEBATE = 2`, checker differs from maker, `debate` needs `team: yes` or `consult:`.
- `skill:` optional in drafted plans and in data-folder templates; required in shipped `skills/` workflow files.
- A plan step's skill, when given, must exist and must not be a workflow or a loop.
- Notice text for a bad block, exactly: `The plan did not hold up: <reason>`
- `/plan` is reserved: no skill, workflow or saved template may be named `plan`.
- Saved templates: `<data>/team/workflows/<name>.md` (`paths.DATA / "team" / "workflows"`), never `skills/`. Shipped name wins a clash; saving under a shipped or existing name is refused.
- Plan approval only starts the run. Gated steps still raise their own cards; sends, spending and deletes stay always-ask (workflow turns already set `hold_sends`).
- Only the owner sees Approve, Reject and Save as template. Only the turn's own bot's reply (`m["bot"] == lead and not m.get("asked_by")`) can carry a plan.
- No plan rule and no plan cut in workflow step turns. A block in a step turn is dropped silently.
- Copy style: no em dashes in any user-visible string or doc.
- Branch `feat/workflows-at-need` in the main checkout (no extra worktree). PR, CI, then merge.

## Review Focus

- A plan block wrapped in a code fence (```` ``` ````) or with markdown bold on `PLAN:`: the block is still found and the fences are cut too, not left as an empty fence in the reply. Test in Task 1.
- Double Approve (two clicks, or two tabs): exactly one run starts; the second request gets `This plan already started.` Test in Task 4.
- A bot switched off between draft and Approve: start refused with `/<title> cannot start here` style lines plus `SWITCH_ON`, plan stays `offered` so Approve can be retried after switching on. Test in Task 4.
- Reloading a thread after a run started: the card shows "Started" with the run thread link (status persisted on the message, not only in the event). Test in Task 5.
- A data-folder template named like a shipped skill or workflow, or a hand-edited broken one: shipped wins, broken file is skipped by `names()`, never crashes `load()`. Test in Task 2.

---

### Task 1: Plan block parser (`core/plans.py`) and optional `skill:` in the step parser

**Files:**
- Create: `core/plans.py`
- Modify: `core/workflows.py` (`Step`, `Workflow`, `_parse_step`, new `parse_lines`, `problems`)
- Test: `tests/test_plans.py`, `tests/test_workflows.py`

**Interfaces:**
- Produces:
  - `workflows.Workflow` gains `plan: bool = False` and `lines: tuple = ()` (raw step lines, without the `N. ` prefix). `parse()` fills `lines`.
  - `workflows.parse_lines(lines: list[str], need_skill: bool = True) -> tuple[Step, ...]` (raises `ValueError` with today's messages; empty list: `"the Steps list is empty"`; over 12: `f"more than {MAX_STEPS} steps"`).
  - `_parse_step(n, line, need_skill=True)`: with `need_skill=False` a missing `skill` gives `Step.skill == ""`; a given skill is checked as today.
  - `workflows.problems(wf, team)`: skips the skill check for a step whose `skill == ""`.
  - `plans.extract(text: str) -> tuple[str, str | None]`: (reply without the block, block text or None). The block starts at a line matching `^[ \t>*_-]*PLAN[*_ \t]*:` and runs to the last following line that is `brief:` or a numbered step line; a ```` ``` ```` fence directly around it is cut too. Only the first block counts; every block is cut.
  - `plans.parse(block: str) -> workflows.Workflow` with `name` = title (trimmed, max 80 chars), `description` = brief, `body` = "", `plan=True`, `lines` = step lines. Raises `ValueError` with: `"no title"`, `"no brief"`, or the step parser's message.
  - `plans.BRIEF_MAX = 2000`.

- [ ] **Step 1: Write failing tests**

```python
# tests/test_plans.py
BLOCK = ("PLAN: Pricing change\nbrief: Move acme to 29 per month for new customers from March.\n"
         "1. bot: research | output: three rival prices with sources | gate: no\n"
         "2. skill: launch-plan | bot: gtm | output: the plan | gate: approval | checker: product")

def test_extract_cuts_block_and_keeps_text(self):
    clean, block = plans.extract("Here is why.\n\n" + BLOCK + "\n")
    self.assertEqual(clean, "Here is why.")
    self.assertTrue(block.startswith("PLAN: Pricing change"))

def test_extract_fenced_and_bold(self):
    clean, block = plans.extract("Why.\n```\n**PLAN:** Pricing change\n" + BLOCK.split("\n", 1)[1] + "\n```")
    self.assertEqual(clean, "Why.")
    self.assertIsNotNone(block)

def test_extract_none(self):
    self.assertEqual(plans.extract("plain answer"), ("plain answer", None))

def test_parse_plan(self):
    wf = plans.parse(plans.extract(BLOCK)[1])
    self.assertTrue(wf.plan)
    self.assertEqual(wf.name, "Pricing change")
    self.assertEqual(wf.steps[0].skill, "")
    self.assertEqual(wf.steps[1].checker, "product")
    self.assertEqual(len(wf.lines), 2)

def test_parse_errors(self):
    for block, why in (("PLAN:\nbrief: x\n1. bot: gtm | output: y", "no title"),
                       ("PLAN: T\n1. bot: gtm | output: y", "no brief"),
                       ("PLAN: T\nbrief: x\n1. bot: gtm | output: y | checker: gtm", "checker must be a different bot"),
                       ("PLAN: T\nbrief: x\n1. skill: nope-skill | bot: gtm | output: y", "unknown skill")):
        with self.assertRaises(ValueError) as e:
            plans.parse(block)
        self.assertIn(why, str(e.exception))

# tests/test_workflows.py (Templates)
def test_shipped_file_still_needs_skill(self):
    with self.assertRaises(ValueError):
        workflows.parse(wf_text(["1. bot: gtm | output: x | gate: no"]), "demo-flow")

def test_problems_skip_empty_skill(self):
    wf = plans.parse("PLAN: T\nbrief: b\n1. bot: gtm | output: x")
    self.assertEqual(workflows.problems(wf, FakeTeam()), [])
```

- [ ] **Step 2: Run** `python -m pytest tests/test_plans.py tests/test_workflows.py -q`. Expected: FAIL (`No module named core.plans`).

- [ ] **Step 3: Implement** `parse_lines` (move the count checks out of `parse`), the `need_skill` flag, the two `Workflow` fields, the `problems` skip, and `core/plans.py`. `plans.parse` splits the block into `PLAN:` title, `brief:` line (prefix case-insensitive) and numbered lines via `workflows._STEP_LINE_RE`.

- [ ] **Step 4: Run** the same command. Expected: PASS. Then `python -m pytest tests -q -x` to confirm no workflow regressions.

- [ ] **Step 5: Commit** `feat(plans): parse a drafted plan block with the step-line parser, skill optional`

---

### Task 2: Runs from the record, and saved templates in the data folder

**Files:**
- Modify: `core/workflows.py` (`new_run`, new `of_run`, `_is_workflow_file`, `load`, `names`, new `SAVED_DIR`, new `save_template`)
- Modify: `core/skill_loads.py` (refuse `plan` in `names()`)
- Test: `tests/test_workflows.py`

**Interfaces:**
- Consumes: Task 1 `Workflow.plan`, `Workflow.lines`, `parse_lines`.
- Produces:
  - `workflows.new_run(wf, brief, ws, room)`: when `wf.plan`, the run also holds `"plan": True, "title": wf.name, "steps": list(wf.lines)`; `"name"` is the title.
  - `workflows.of_run(run: dict) -> Workflow | None`: plan run: rebuilt from `run["steps"]` with `parse_lines(..., need_skill=False)` (None on `ValueError`), `plan=True`; file run: `load(run["name"])`. Never looks a plan title up as a file.
  - `workflows.SAVED_DIR = paths.DATA / "team" / "workflows"`; `load(name)` reads `skills/<name>/SKILL.md` first, then `SAVED_DIR/<name>.md` (`kind: workflow`, parsed with `need_skill=False`). `names()` is the sorted union.
  - `workflows.RESERVED = frozenset({"plan"})`.
  - `workflows.save_template(wf: Workflow, name: str) -> str | None`: writes `SAVED_DIR/<name>.md` (frontmatter `name`, `kind: workflow`, `description` = `wf.description`; body `## Steps` with `N. <line>`); returns a refusal line or None. Refusals: `"Use lowercase letters, digits and dashes."` (not `_NAME_RE`), `"/plan is reserved."`, `f"/{name} already exists."` (in `skill_loads.names()`, `names()`, or file exists).

- [ ] **Step 1: Write failing tests** in `tests/test_workflows.py`, patching `workflows.SAVED_DIR` to a temp dir:

```python
def test_plan_run_rebuilds_from_record(self):
    wf = plans.parse("PLAN: Hire a designer\nbrief: b\n1. bot: product | output: role brief")
    run = workflows.new_run(wf, "b", "acme", "acme/general")
    self.assertEqual((run["plan"], run["name"], run["steps"]), (True, "Hire a designer", list(wf.lines)))
    again = workflows.of_run(run)
    self.assertTrue(again.plan)
    self.assertEqual(again.steps, wf.steps)

def test_plan_title_never_loads_a_file(self):
    with mock.patch.object(workflows, "load") as load:
        workflows.of_run({"plan": True, "name": "launch-plan", "steps": ["bot: gtm | output: x"]})
    load.assert_not_called()

def test_save_and_load_template(self):
    wf = plans.parse("PLAN: T\nbrief: Vendor pick\n1. bot: cto | output: shortlist")
    self.assertIsNone(workflows.save_template(wf, "vendor-pick"))
    got = workflows.load("vendor-pick")
    self.assertEqual((got.description, got.steps[0].skill), ("Vendor pick", ""))
    self.assertIn("vendor-pick", workflows.names())

def test_save_refusals(self):
    wf = plans.parse("PLAN: T\nbrief: b\n1. bot: cto | output: x")
    self.assertEqual(workflows.save_template(wf, "plan"), "/plan is reserved.")
    self.assertIn("already exists", workflows.save_template(wf, "launch-plan"))  # a shipped skill
    self.assertIn("lowercase", workflows.save_template(wf, "Bad Name"))

def test_shipped_wins_and_broken_saved_skipped(self):
    for name in ("launch-plan", "team-launch-plan"):  # a shipped skill, a shipped workflow
        (self.saved / f"{name}.md").write_text("---\nkind: workflow\n---\n## Steps\n1. bot: cto | output: x\n")
    (self.saved / "broken.md").write_text("---\nkind: workflow\n---\nno steps\n")
    self.assertIsNone(workflows.load("launch-plan"))  # a shipped name is never shadowed
    self.assertNotEqual(workflows.load("team-launch-plan").steps[0].bot, "cto")
    self.assertIsNone(workflows.load("broken"))
    self.assertNotIn("broken", workflows.names())

def test_plan_name_reserved(self):
    self.assertNotIn("plan", skill_loads.names())
```

- [ ] **Step 2: Run** `python -m pytest tests/test_workflows.py -q`. Expected: FAIL on the new tests.

- [ ] **Step 3: Implement** per Interfaces. `_is_workflow_file` returns `(text, need_skill)` or None; keep `_inside` for both paths. Write with `bots._atomic_write`. `skill_loads.names()` filters out `"plan"`.

- [ ] **Step 4: Run** `python -m pytest tests/test_workflows.py tests/test_skill_loads.py -q` (skip the second file if absent). Expected: PASS.

- [ ] **Step 5: Commit** `feat(workflows): runs keep a drafted plan's steps; templates saved in the data folder`

---

### Task 3: Workflow turns read plan runs (labels, routes, every `load` site)

**Files:**
- Modify: `dashboard/workflow_turns.py` (`label`, `_instructions`, `_turn`, `prepare`, `stop`, `current`)
- Modify: `dashboard/server.py` (job `START:` offer keeps `workflows.load`; nothing else here)
- Test: `tests/test_workflow_turns.py`

**Interfaces:**
- Consumes: Task 2 `workflows.of_run`.
- Produces: `workflow_turns.title(wf) -> str`: `wf.name` for a plan, `f"/{wf.name}"` for a file. Every user-visible `/{wf.name}` in `label`, `_instructions` head and `prepare` refusals uses it.

- [ ] **Step 1: Write failing tests** in `tests/test_workflow_turns.py` that save a plan run directly (`workflows.new_run(plans.parse(...))` with `chat=CHAT`, `workflows.save`) and tick it:

```python
def test_plan_run_ticks_with_no_skill(self):
    self.save_plan_run("PLAN: Hire a designer\nbrief: b\n1. bot: product | output: role brief\n"
                       "2. bot: cmo | output: job post | gate: no")
    out = self.ask("go", replies=["role brief done"], chat_id=CHAT, workflow="next")
    ev = wf_event(out)
    self.assertEqual((ev["step"], ev["status"]), (2, "running"))
    self.assertTrue(ev["label"].startswith("@cmo Step 2 of 2 of Hire a designer"))
    self.assertNotIn("/", ev["label"].split(" of ")[-1])

def test_plan_step_routes_without_skill(self):
    run = self.save_plan_run("PLAN: T\nbrief: b\n1. bot: product | output: x")
    body, ctx = workflow_turns._turn({"messages": []}, run, workflows.of_run(run))
    self.assertEqual(ctx["route"], ("product", None, False, None))

def test_stop_and_current_on_plan_run(self):
    self.save_plan_run("PLAN: T\nbrief: b\n1. bot: product | output: x")
    self.assertEqual(workflow_turns.current(self.root, CHAT)["name"], "T")
    self.assertEqual(workflow_turns.stop(self.root, CHAT)["status"], "stopped")
```

- [ ] **Step 2: Run** `python -m pytest tests/test_workflow_turns.py -q`. Expected: FAIL.

- [ ] **Step 3: Implement**: replace each `workflows.load(live["name"])` / `load(run["name"])` in `workflow_turns.py` with `workflows.of_run(...)`; route `(step.bot, step.skill or None, bool(step.skill), None)`; stage label omits ` /{skill}` when empty; `_instructions` drops `wf.body` when empty (already filtered by `if p`).

- [ ] **Step 4: Run** `python -m pytest tests/test_workflow_turns.py tests/test_workflows.py -q`. Expected: PASS.

- [ ] **Step 5: Commit** `feat(workflows): plan runs tick, label and stop like file runs`

---

### Task 4: Approve starts a run from the stored plan message

**Files:**
- Modify: `dashboard/workflow_turns.py` (`involved`, `prepare`, `begin`; new `_plan_start`)
- Modify: `dashboard/chat_store.py` (`kept` includes `"plan"`)
- Test: `tests/test_workflow_turns.py`

**Interfaces:**
- Consumes: Task 1 `plans.parse`; Task 2 `workflows.of_run`, `new_run`.
- Produces:
  - Message field (written by Task 5, read here): `m["plan"] = {"title": str, "brief": str, "lines": [str], "bot": str, "status": "offered" | "started" | "rejected", "run_chat": str | None}`.
  - Turn body for Approve: `{"workflow": "plan", "plan_ref": {"chat": <source chat id>, "msg": <int index>}, "room_id": ..., "messages": [{"role": "user", "content": "Approved plan: <title>"}]}`. The browser sends nothing else from the plan.
  - `prepare` on that body: owner only, not shared (else `PRIVATE_ONLY`); loads the source record (`chat_store.load`), checks it is in this workspace; message `msg` must hold a plan with status `offered` (else `"This plan already started."` when `started`, `"This plan was rejected."` when `rejected`, `"No plan to start here."` otherwise); rebuilds the workflow with `plans`/`parse_lines` from the stored fields; `workflows.problems` refusal as for `/<workflow>` with `SWITCH_ON`. Home: the source thread when it is the owner's private, non-DM thread with no live run; otherwise `body["chat_id"] = None` and `body["room_id"] = f"{ws}/general"` (a new private thread). Under a module `threading.Lock` it re-reads the record, sets `status = "started"` and saves, then returns `_turn(...)` with `ctx["plan_ref"] = (src_chat, msg)`.
  - `begin(ctx, chat, root)`: when `ctx.get("plan_ref")`, writes `run_chat = chat` on the source message.

- [ ] **Step 1: Write failing tests** (helper `offer_plan()` writes a chat record with one bot message holding `plan` status `offered`):

```python
def test_approve_starts_once(self):
    src = self.offer_plan()
    out = self.ask("Approved plan: T", replies=["done"], workflow="plan", plan_ref={"chat": src, "msg": 1})
    self.assertEqual(wf_event(out)["name"], "T")
    msg = server.chat_store.load(src)["messages"][1]["plan"]
    self.assertEqual(msg["status"], "started")
    self.assertTrue(msg["run_chat"])
    again = self.ask("Approved plan: T", workflow="plan", plan_ref={"chat": src, "msg": 1})
    self.assertIn("This plan already started.", again)

def test_approve_refused_for_switched_off_bot_keeps_offer(self):
    src = self.offer_plan(lines=["bot: social | output: x"])
    with mock.patch.object(bots, "team", return_value=self.team_with_off("social")):
        out = self.ask("Approved plan: T", workflow="plan", plan_ref={"chat": src, "msg": 1})
    self.assertIn("switched off", out)
    self.assertEqual(server.chat_store.load(src)["messages"][1]["plan"]["status"], "offered")

def test_approve_not_owner(self):
    src = self.offer_plan()
    out = self.ask("x", workflow="plan", plan_ref={"chat": src, "msg": 1}, viewer={"kind": "person", "id": "p1"})
    self.assertIn(workflow_turns.PRIVATE_ONLY, out)

def test_approve_from_dm_starts_new_thread_in_general(self):
    src = self.offer_plan(room="acme/dm-gtm")
    out = self.ask("Approved plan: T", replies=["done"], workflow="plan", plan_ref={"chat": src, "msg": 1})
    self.assertNotEqual(wf_event(out)["chat"], src)
```

- [ ] **Step 2: Run** `python -m pytest tests/test_workflow_turns.py -q -k approve`. Expected: FAIL.

- [ ] **Step 3: Implement** per Interfaces. `involved` returns True for `body.get("workflow") == "plan"`. Validate `plan_ref` shape (`chat` matches `skill_loads._CHAT_RE`, `msg` a non-negative int) before any load.

- [ ] **Step 4: Run** `python -m pytest tests/test_workflow_turns.py -q`. Expected: PASS.

- [ ] **Step 5: Commit** `feat(workflows): Approve on a plan card starts its run once, read server-side`

---

### Task 5: The turn: plan rule, `/plan`, cutting the block, the notice

**Files:**
- Modify: `dashboard/server.py` (rule into `context` in `_ask_turn` before `compose_turn_prompt`; cut after the approvals block near the job `START:` offer, ~line 5033; `/plan` prefix in the chat POST path before routing; `JOB_OFFER` mentions plans)
- Modify: `dashboard/workflow_turns.py` (new `plan_rule(team, asked: bool) -> str`, `PLAN_NOTICE = "The plan did not hold up: {why}"`)
- Test: `tests/test_workflow_turns.py`

**Interfaces:**
- Consumes: Task 1 `plans.extract`, `plans.parse`; `workflows.problems`.
- Produces:
  - `workflow_turns.plan_rule(team, asked)`: owner turns that are not workflow steps and not invited-person turns. Short form (always, under ~120 words): when to propose (owner asks; or the request needs two or more teammates' areas; a single-bot request that ends in one approval does not count), that an unprompted plan replaces the answer with the plan plus one line on why, the block format (title, `brief:`, numbered step lines with `bot`, `output`, `gate`, optional `skill`, `checker`, `consult`, `team`, `debate`), and the handles of active bots. `asked=True` adds: "The owner asked for a plan: answer with one PLAN block." and each active bot's skills (`team.skills_of`), workflows and loops excluded.
  - `/plan <request>` (owner, first word): server strips `/plan` from the last message before routing and sets `body["plan_asked"] = True`; an empty request gets `"Say what the plan is for: /plan <request>."` with no model turn.
  - After the turn, for every message: `plans.extract`; cut text is emitted as `{"clean": ..., "msg": i}`. Only the lead's own message (`m["bot"] == lead and not m.get("asked_by")`, first one) on a non-step turn becomes a card: on parse or `problems` failure append the notice `The plan did not hold up: <first reason>` (emit `{"notice", "msg"}`); on success `m["plan"] = {..., "status": "offered", "run_chat": None}` and emit `{"plan": m["plan"], "msg": i}`. Brief capped at `plans.BRIEF_MAX`.
  - `chat_store.kept` includes `"plan"` (if not done in Task 4).

- [ ] **Step 1: Write failing tests**:

```python
GOOD = ("Needs research and gtm.\n\nPLAN: Pricing change\nbrief: New price for acme.\n"
        "1. bot: research | output: rival prices\n2. bot: gtm | output: rollout | gate: approval | checker: product")

def test_lead_plan_becomes_card(self):
    out = self.ask("@gtm change our price", replies=[GOOD])
    ev = [e for e in events(out) if "plan" in e][-1]
    self.assertEqual((ev["plan"]["title"], ev["plan"]["status"]), ("Pricing change", "offered"))
    saved = self.last_bot_message()
    self.assertNotIn("PLAN:", saved["content"])
    self.assertEqual(saved["plan"]["lines"][1].split("|")[0].strip(), "bot: gtm")

def test_bad_plan_shows_notice(self):
    out = self.ask("@gtm x", replies=["PLAN: T\nbrief: b\n1. bot: nobody | output: x"])
    self.assertIn("The plan did not hold up: Step 1: @nobody is not on the team.", out)
    self.assertNotIn("plan", self.last_bot_message())

def test_teammate_plan_dropped(self):
    out = self.ask("@gtm x", replies=["CONSULT @research: rivals?", GOOD, "final"])
    self.assertFalse([e for e in events(out) if "plan" in e])

def test_step_turn_drops_plan(self):
    self.save_plan_run("PLAN: T\nbrief: b\n1. bot: product | output: x")
    out = self.ask("go", replies=[GOOD], chat_id=CHAT, workflow="next")
    self.assertFalse([e for e in events(out) if "plan" in e])

def test_rule_in_owner_turn_only(self):
    self.ask("@gtm hello", replies=["hi"])
    self.assertIn("PLAN:", self.runs[-1]["text"])
    self.save_plan_run("PLAN: T\nbrief: b\n1. bot: product | output: x")
    self.ask("go", replies=["ok"], chat_id=CHAT, workflow="next")
    self.assertNotIn("PLAN: <short title>", self.runs[-1]["text"])

def test_slash_plan_asks_and_lists_skills(self):
    self.ask("/plan hire a designer", replies=["ok"])
    text = self.runs[-1]["text"]
    self.assertIn("The owner asked for a plan", text)
    self.assertIn("hire a designer", text)
    self.assertNotIn("/plan hire", text)
```

- [ ] **Step 2: Run** `python -m pytest tests/test_workflow_turns.py -q -k "plan"`. Expected: FAIL.

- [ ] **Step 3: Implement** per Interfaces. Keep the rule's exact wording in `workflow_turns.py` constants. Add one line to `JOB_OFFER`: a job's bot may also end with a PLAN block; it shows as a card the owner approves later.

- [ ] **Step 4: Run** `python -m pytest tests -q`. Expected: PASS (full suite: the rule adds text to every owner prompt, so prompt-size or snapshot tests may need their expected text updated; update them, do not weaken them).

- [ ] **Step 5: Commit** `feat(chat): bots propose a PLAN block; the lead's becomes a card, a bad one a notice`

---

### Task 6: Reject and Save as template endpoints, and the card in the browser

**Files:**
- Modify: `dashboard/server.py` (POST `/api/workflow/plan/reject`, POST `/api/workflow/plan/save`, next to `/api/workflow/stop`)
- Modify: `dashboard/workflow_turns.py` (new `reject_plan(root, ws, chat, msg) -> dict | str`, `save_plan(root, ws, chat, msg, name) -> dict | str`)
- Modify: `dashboard/app.js` (card next to the `workflow_offer` bar, ~line 6593; handle the `plan` SSE event), `dashboard/app.css` if a class is needed
- Modify: `docs/guide.md` (workflows section: plans, `/plan`, saved templates)
- Test: `tests/test_workflow_turns.py`

**Interfaces:**
- Consumes: Task 4 message `plan` field and Approve body; Task 2 `workflows.save_template`.
- Produces:
  - `reject_plan`: owner only (server checks viewer kind), plan must be `offered`; sets `rejected`; returns `{"ok": True, "bot": plan["bot"]}`. The browser then sends a normal message in that thread: `@<bot> <note>`, or `@<bot> No plan: answer the request yourself.` when the note is empty.
  - `save_plan`: plan must be `started`; returns `{"ok": True, "name": name}` or the refusal line from `save_template` (HTTP 400 with `{"error": line}`). The owner's click after typing the name in the card is the save's approval (no second card: the owner is the only one who can press it and it writes nothing else).
  - Card (owner only, else plain text of title, brief, steps): title, brief, one line per step (`@bot`, `/skill` if any, output, `gate: approval`, `checker`, consult/team/debate). Status `offered`: Approve, Reject (with an optional note field). `started`: "Started" with a link that opens `run_chat`, and Save as template (name field + Save). `rejected`: "Rejected".

- [ ] **Step 1: Write failing tests**:

```python
def test_reject_once(self):
    src = self.offer_plan()
    self.assertEqual(workflow_turns.reject_plan(self.root, "acme", src, 1), {"ok": True, "bot": "gtm"})
    self.assertEqual(workflow_turns.reject_plan(self.root, "acme", src, 1), "This plan was rejected.")
    out = self.ask("Approved plan: T", workflow="plan", plan_ref={"chat": src, "msg": 1})
    self.assertIn("This plan was rejected.", out)

def test_save_needs_started_and_writes_data_folder(self):
    src = self.offer_plan()
    self.assertIn("not started", workflow_turns.save_plan(self.root, "acme", src, 1, "vendor-pick"))
    self.ask("Approved plan: T", replies=["done"], workflow="plan", plan_ref={"chat": src, "msg": 1})
    self.assertEqual(workflow_turns.save_plan(self.root, "acme", src, 1, "vendor-pick")["name"], "vendor-pick")
    self.assertTrue((workflows.SAVED_DIR / "vendor-pick.md").is_file())
    self.assertFalse((skill_loads.SKILLS_DIR / "vendor-pick").exists())

def test_endpoints_owner_only(self):
    # POST both endpoints as an invited person: 403
```

- [ ] **Step 2: Run** `python -m pytest tests/test_workflow_turns.py -q -k "reject or save or endpoints"`. Expected: FAIL.

- [ ] **Step 3: Implement** the two functions, the endpoints (403 for non-owner, 400 with `{"error"}` for a refusal line), the card and the guide section. Save refusal for a plan not started: `"This plan has not started yet."`

- [ ] **Step 4: Run** `python -m pytest tests -q`, then check the card by hand with the `run` skill: send a reply containing `GOOD` from a stub or a real bot, Approve, see "Started" and the run thread, reload, see the same, Save as template, then `/vendor-pick <brief>` starts. Expected: all pass, card behaves as listed.

- [ ] **Step 5: Commit** `feat(chat): plan card with Approve, Reject with a note, and Save as template`

---

### Task 7: Evals: plan arm in compare, and the zero-unprompted check

**Files:**
- Modify: `evals/compare.py` (`KEYS`, `load`, `arm_order`, `rivals` → `pairs`, `_child`, `summary`, new `plan_arm`, `--plan` flag)
- Modify: `evals/cases/compare.json` (3 new `lead` cases)
- Modify: `evals/turns.py` (`_EQUAL` gains `"plan"`; outcome `"plan"` = the lead message has a `plan` field)
- Modify: `evals/cases/live.json` (3 simple requests with `"expect": {"plan": false}`)
- Test: `tests/test_evals_compare.py` (or the existing compare test file), `tests/test_evals_turns.py` if present

**Interfaces:**
- Consumes: Task 4 Approve body; Task 5 `plan` SSE event.
- Produces:
  - Case keys: `id`, `brief`, `intake`, and exactly one of `workflow` or `lead`; optional `baseline` (needs `workflow`). `load` raises `ValueError` otherwise.
  - `pairs(case, with_plan: bool) -> list[tuple[str, str]]`: workflow case: `("workflow", "single")`, `("workflow", "baseline")` if baseline, and with `with_plan` `("plan", "single")`, `("plan", "workflow")`. Lead case: `[("plan", "single")]` always.
  - `arm_order(case, with_plan)`: every arm in `pairs`, rotated by the same per-case seed as today.
  - `plan_arm(post, case, lead) -> dict`: posts `@<lead> /plan <brief>`; takes the last `plan` event (`chat_id`, `msg`); none: `{"status": "failed", "why": "No plan was drafted.", "plan": "", ...}`; else `drive(post, <Approve body>)` and judges the last step's bot output (`workflow_plan(msgs, maker)` with maker = bot of the plan's last step).
  - Single arm for a lead case: `@<lead> <brief>`.
  - Summary prints wins, mean score, cost and cost ratio per pair (`subject vs rival`).
  - New cases (generic `acme` only): `acme-price-change` (lead `product`), `acme-first-hire` (lead `cos`, or `product` if `cos` is not on a venture team), `acme-vendor-pick` (lead `cto`).
  - Live cases: three one-bot requests (`@cmo` tagline idea, `@research` one competitor's price, `@gtm` a subject line), each `"expect": {"plan": false}`.

- [ ] **Step 1: Write failing tests**: `pairs` for each case shape; `load` refuses a case with both or neither of `workflow`/`lead`; `plan_arm` with a fake `post` returning a `plan` event then workflow events gives `status` from the last event; with no `plan` event gives `"No plan was drafted."`; `summary` lists `plan vs single`. Turns harness: a case whose fake lead reply holds `GOOD` reports `plan: True`.

- [ ] **Step 2: Run** `python -m pytest tests -q -k "compare or turns"`. Expected: FAIL.

- [ ] **Step 3: Implement** per Interfaces. Keep the existing arms' behavior byte-identical without `--plan` on workflow cases.

- [ ] **Step 4: Run** `python -m pytest tests -q`. Expected: PASS. Then the live runs (billed, ask the owner before spending): `python -m evals --compare --plan` and the live set with the 3 new cases. Record per-pair wins, mean score, cost ratio, and the unprompted-plan count (target 0) for the PR.

- [ ] **Step 5: Commit** `feat(evals): drafted-plan arm in compare and a zero-unprompted-plan check`

---

### Task 8: Finish

- [ ] `python -m pytest tests -q` green, `python -m pytest tests/test_no_usage_data.py -q` green.
- [ ] `superpowers:requesting-code-review` on the branch; fix every finding.
- [ ] Push `feat/workflows-at-need`, open PR (body: what, eval numbers, the Save-as-template approval reading), wait for CI, resolve review comments, merge, delete the branch.
