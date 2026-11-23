# Skills rewrite and domain bots Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship spec phase 2: rewrite the five thin skills to the standard, then add one bot per missing domain with its skills, one workflow and one loop template.

**Architecture:** Markdown only, on the machinery phase 1 built. A rewrite replaces a `skills/<name>/SKILL.md` body. A domain bot is a `bots/<id>.md` card owning 3 or 4 new skills, plus one `kind: workflow` file and one `kind: loop` file naming that bot. New bots start switched off in every workspace (`bots.default_active` is unchanged), so no default team, card or prompt grows. Every new skill gets a route in `dashboard/server.py` `_SKILL_ROUTES` so `/skill` and plain asks reach it.

**Tech Stack:** Markdown skill files, Python 3 standard library, `unittest`, `python -m evals` (offline) and `python -m evals --live` (real engine).

**Spec:** `docs/superpowers/specs/2026-11-21-skills-workflows-loops-design.md` ("Standard", "Domains: first cut", "Quality", "Phases" item 2). Plan 5; phase 1 was plans 1 to 4.

## Global Constraints

- Public repo: fixtures and examples use `acme`, `demo`, `profile-a`, `profile-b` only. `python3 -m unittest tests.test_no_usage_data` before every commit.
- No third-party skill text. Everything is written fresh with generic examples.
- Skill standard: reads the project intake and relevant memos first; produces a decision memo with two or three options, trade-offs and one recommendation; saves only after approval. Compact: about 80 to 120 lines, under the 4500-character cap (`tests/test_token_budget.py`).
- Workflow standard: ordered steps, each naming skill, bot, output, gate; a stop condition; an explicit always-ask list.
- Loop template standard: the 13 parts in `core/loop_templates.PARTS`; attempt cap 3 per item; report mode first; never sends, spends or deletes.
- Cost: a bot card lists 3 to 6 skills. No existing card grows. New bots are off by default.
- Quality: run evals before and after each rewrite. A rewrite that does not improve output is dropped.

## Domain order

The first users run small ventures alone or with a few people. Order by what they hit first after marketing and product: **sales**, **finance** (and legal-lite), **support** (and community), **operations** (and personal productivity), **people** (hiring). Marketing, product, research and content keep their existing bots.

| Bot id | Name | Skills (default first) | Workflow | Loop template |
|---|---|---|---|---|
| `sales` | Sales Lead | `discovery-call`, `prospect-list`, `deal-review`, `objection-handling` | `deal-prep`: `icp-research` (research) → `prospect-list` (sales, checker research) → `discovery-call` (sales, gate approval) | `pipeline-watch`: daily, `--check-own list`, flags stale deals |
| `finance` | Finance Lead | `unit-economics`, `runway-plan`, `contract-check` | `price-to-runway`: `pricing-strategy` (cmo) → `unit-economics` (finance, checker cmo) → `runway-plan` (finance, gate approval) | `runway-check`: monthly, burn and runway against plan |
| `support` | Support Lead | `support-triage`, `faq-builder`, `feedback-synthesis` | `feedback-to-test`: `feedback-synthesis` (support) → `problem-validation` (product, checker support) → `experiment-design` (product, gate approval) | `feedback-digest`: weekly, themes from new feedback |
| `ops` | Operations Lead | `weekly-plan`, `process-doc`, `vendor-compare` | `process-setup`: `process-doc` (ops) → `vendor-compare` (ops, checker finance) → `weekly-plan` (ops, gate approval) | `daily-plan`: weekdays 08:00, the day's plan from calendar and priorities |
| `people` | People Lead | `role-scope`, `interview-plan`, `onboarding-plan` | `hire-plan`: `role-scope` (people, checker finance) → `interview-plan` (people) → `onboarding-plan` (people, gate approval) | `hiring-watch`: daily, `--check-own list`, candidates waiting on a step |

Each new bot: `reports_to: cos`, a one-line icon glyph, a body of 3 lines (what it owns, how it works, what it never does: sends, spends, legal or tax advice as fact).

## Review Focus

- A `/skill` of a switched-off domain bot: rolls up to the nearest active manager and still loads the skill.
- A workflow whose step names a switched-off bot: refused at start with where to switch it on (existing runner behavior), not run half-way.
- `contract-check` and `runway-plan` asked for legal or tax certainty: they flag risk and say to confirm with a professional, never state legal or tax advice as fact.
- Plain chat asks ("what should I say on the call", "how long is my runway") route to the new skill, and the old routes still win their own asks (pricing stays `pricing-strategy`, "competitors" stays `competitor-scan`).
- A default venture team's prompt is unchanged in size after the new bots land.

---

### Task 1: Rewrite the five thin skills

**Files:**
- Modify: `skills/competitor-scan/SKILL.md`, `skills/icp-research/SKILL.md`, `skills/pricing-strategy/SKILL.md`, `skills/launch-plan/SKILL.md`, `skills/positioning/SKILL.md`
- Modify: `evals/cases/live.json` (one case per skill)
- Test: `tests/test_skill_standard.py` (new)

- [ ] Test `test_skill_standard.py`: for every non-workflow, non-loop skill named in `REWRITTEN` (the five here, later the domain skills), the file has frontmatter `name` and `description`, a `## Write gate` section, mentions `intake`, names a `Recommendation`, and has 40 to 130 lines.
- [ ] Run it: the five fail on line count or the missing `Recommendation`.
- [ ] Add five live cases, ids `skill-<name>-memo`, each `@<owner> /<skill> <a generic acme brief>` with `expect`: `bot`, `skill`, `mentions` (a word the standard requires, such as "recommend"), `max_chars` 6000. Run `python -m evals --live --case ...` on the five as the baseline and keep the replies.
- [ ] Rewrite each skill to the standard. Keep each existing rule that holds up (status quo as a competitor, bottom-up thinking, behavior questions in interviews, price tests before committees, launch as an experiment, customer words in positioning). Add what each lacked: inputs to read and what to do when they are missing; the method in steps; how to mark facts, estimates and guesses; options with trade-offs; one recommendation with the evidence that would change it; a short quality check before replying.
- [ ] Run the live cases again. Compare each pair of replies: keep a rewrite only when its reply is more specific, better sourced, or more decision-ready. Revert any that is not, and note it in the commit message.
- [ ] Full suite and offline evals pass. Commit.

### Task 2: Domain bots, skills and routes

**Files:**
- Create: `bots/sales.md`, `bots/finance.md`, `bots/support.md`, `bots/ops.md`, `bots/people.md`, and one `skills/<name>/SKILL.md` per skill in the table
- Modify: `dashboard/server.py` (`_SKILL_ROUTES`), `tests/test_bots.py` (default team expectations), `tests/test_skill_standard.py` (`REWRITTEN` gains the new skills), `evals/cases/turns.json` (one routing case per bot), `docs/guide.md` (team section)
- Test: `tests/test_domain_bots.py` (new)

- [ ] Tests in `test_domain_bots.py`:
  - `test_new_bots_off_by_default`: for HQ, a plain venture, a brand, none of the five is active.
  - `test_switched_off_owner_rolls_up`: in a plain venture `team.owner("unit-economics")` is `cos`.
  - `test_routes`: `server._route_skill` (or the function the routes use) maps "what should I ask on the discovery call" to `discovery-call`, "how long is my runway" to `runway-plan`, "review this contract" to `contract-check`, "sort these support tickets" to `support-triage`, "write a job description" to `role-scope`, "plan my week" to `weekly-plan`; and "how much should we charge" still maps to `pricing-strategy`.
  - `test_default_prompt_unchanged`: the system prompt built for a plain venture team does not name any of the five new handles.
- [ ] Run them, see them fail.
- [ ] Write the five cards and 16 skills to the standard (Task 1's shape). `contract-check`, `runway-plan` and `unit-economics` state they are not legal, tax or investment advice and say what to confirm with a professional. `prospect-list` builds a canvas list from public, licensed or owner-supplied sources only, never scraping logged-in sites. `support-triage` drafts replies and never sends them. `weekly-plan` writes calendar activities only after approval and checks `get-busy` first.
- [ ] Add one route per new skill, placed so existing routes keep their asks.
- [ ] Run the suite and offline evals. Commit.

### Task 3: Domain workflows and loop templates

**Files:**
- Create: `skills/deal-prep`, `skills/price-to-runway`, `skills/feedback-to-test`, `skills/process-setup`, `skills/hire-plan` (each `kind: workflow`), `skills/pipeline-watch`, `skills/runway-check`, `skills/feedback-digest`, `skills/daily-plan`, `skills/hiring-watch` (each `kind: loop`)
- Modify: `skills/loop-design/SKILL.md` (template list, still under 4500 characters), `docs/guide.md` (Workflows and Loops sections)
- Test: existing `tests/test_workflows.py` and `tests/test_loop_templates.py` shipped-file checks, plus `tests/test_domain_bots.py::test_workflows_parse` (each new workflow parses and every step's skill is on its bot's card or is a shared skill).

- [ ] Write `test_workflows_parse`, run it, see it fail.
- [ ] Write the ten files per the table. Each workflow's always-ask list names sending, spending, deleting and writes outside the project. Each loop template names its bot, starts in report mode, and keeps the 3-try cap in its State part.
- [ ] Update `loop-design`'s template line to point at the full set by bot, under the cap.
- [ ] Run the suite, offline evals and `python3 -m unittest tests.test_no_usage_data`. Commit.

### Task 4: Real-model check

- [ ] In a temp home (`LEOBOT_HOME=<scratch>`), switch on all five new bots in an `acme` workspace. Run one chat ask per new bot and one step of each new workflow. Run `pipeline-watch` and `daily-plan` as jobs (created an hour ago, every 30m) and let the runner tick.
- [ ] Fix whatever the runs show, with a test for each fix. Then PR, CI, merge.
