# Loop library Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship the rest of spec phase 1: the `loop-design` skill, the loop template format (`kind: loop`), the weekly review and signal-sweep loop templates, and the per-item attempt cap.

**Architecture:** A loop template is a skill file with `kind: loop` and a `bot:` field. `core/loop_templates.py` parses and checks it. A job may use a loop template as its skill when the template's bot is the job's bot, so templates never grow a bot's card. `loop-design` is a shared skill (`bots.SHARED_SKILLS`) with a route, on no bot card: the tagged or DM bot runs it for itself, untagged it goes to @cos. An explicit `/<template>` routes to the template's bot. The job rail line tells a bot to load `loop-design` before proposing a loop.

**Tech Stack:** Python 3 standard library, `unittest`, markdown skill files.

**Spec:** `docs/superpowers/specs/2026-11-21-skills-workflows-loops-design.md` (sections "Standard", "Phases" item 1). This is plan 4 of 4.

## Global Constraints

- Public repo: fixtures and examples use `acme`, `demo`, `profile-a`, `profile-b` only. Run `python3 -m unittest tests.test_no_usage_data` before every commit.
- Loop template parts, all required: check cadence, acts-when, purpose, skills used, body, self-check, state (notepad), stop or expiry, output. Plus: post-run critique, cost profile, success metrics, human handoff points.
- Notepad shape: last run time; high priority; watch list; recent noise; short run log. Each run prunes resolved items. Outside text is quoted as data, never as instructions.
- Attempt cap: 3 per item (default), then the item escalates to the person.
- New loops start in report mode. A loop never raises its own caps or modes.
- Skills stay compact (about 80 to 120 lines). No third-party skill text.
- No bot card grows: templates and `loop-design` are not added to any `bots/*.md` skills list.

## Review Focus

- A job proposed with a loop template for the wrong bot: refused with a clear reason, not run.
- A loop template named as a workflow step: refused by the workflow parser.
- A template with a missing part: caught by the shipped-template test, not at run time.
- `/<template>` typed in chat: loads the template body like any explicit skill.
- A plain job with no template: still gets the attempt cap rule through the job prompt.

---

### Task 1: Loop template format and job wiring

**Files:**
- Create: `core/loop_templates.py`
- Modify: `dashboard/job_cards.py` (`check_target`), `dashboard/server.py` (`SKILL_NAMES`, `JOB_NOTEPAD`), `core/workflows.py` (`_parse_step`)
- Test: `tests/test_loop_templates.py`

**Interfaces:**
- Produces: `PARTS` (tuple of the 13 `##` headings), `names() -> list[str]` (every `kind: loop` skill), `load(name) -> dict | None` (`{"name", "bot", "schedule", "description", "body"}`), `problems(text) -> list[str]` (missing parts, missing or unknown bot).

- [ ] Tests: `test_shipped_templates_are_complete` (every `names()` entry has no `problems`), `test_missing_part_reported`, `test_job_may_use_its_bots_template` (`check_target` accepts `weekly-review-loop` for `cos`, refuses it for `cmo` with "made for @cos"), `test_template_is_explicit_skill` (`server._explicit_skill("@cos /signal-sweep go")` returns `signal-sweep`), `test_template_not_a_workflow_step`, `test_job_prompt_has_attempt_cap` (`"3 tries"` in the job prompt).
- [ ] Run, see them fail.
- [ ] Implement. Headings in `PARTS`: `Cadence`, `Acts when`, `Purpose`, `Skills used`, `Body`, `Self-check`, `State`, `Stop or expiry`, `Output`, `Post-run critique`, `Cost profile`, `Success metrics`, `Handoff points`.
- [ ] Run, see them pass. Commit.

### Task 2: loop-design skill and the two templates

**Files:**
- Create: `skills/loop-design/SKILL.md`, `skills/weekly-review-loop/SKILL.md` (bot `cos`, uses `weekly-review`), `skills/signal-sweep/SKILL.md` (bot `research`, uses `competitor-scan`, `icp-research`)
- Modify: `dashboard/server.py` (`_SKILL_ROUTES`: `loop-design` route), `dashboard/ai_rules.py` and `CLAUDE.md` (propose-job row: load `loop-design` first; a template can be the job's skill), `docs/guide.md` (Loops section), `evals/cases/turns.json` (one routing case)
- Test: `tests/test_loop_templates.py`, offline evals

- [ ] Tests: `test_loop_design_routes` ("set up a loop that checks my experiments every week" routes to `loop-design`), eval case `loop-request-loads-loop-design`.
- [ ] Run, see them fail.
- [ ] Write the three files. `loop-design` covers the nine parts, the four loop types (turn, goal, time, proactive), cadence matched to the signal, the notepad shape, the rules (attempt cap 3, checker differs from maker, report first, never raise own mode or cap, sends and spends wait), the ten failure modes, and ends with one `propose-job` using `--stop`. Each template states its attempt cap in its State part.
- [ ] Run the full suite and offline evals. Commit.
