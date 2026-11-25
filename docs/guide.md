# Leo Bot — Running & Usage Guide

A local-first operating system for running a portfolio of projects, products,
profiles, and content from your machine. Files are the source of truth; everything
else is derived from them.

---

## Prerequisites

- **Python 3.10+** (stdlib only, nothing to install) — `python3 --version`
- **`claude` CLI**, logged in — `claude --version` (used for content + guideline AI jobs)

No other install. No server, no database to run, no `npm`.

---

## Quick start — the dashboard

```bash
cd /path/to/Leo Bot
python3 dashboard/server.py
```

It re-indexes from your files, then prints:

```
Leo Bot dashboard → http://127.0.0.1:8765/?token=<per-run token>
```

Open that link: the token is exchanged for a login cookie. A plain
`http://127.0.0.1:8765` shows a login-required page. The token changes every run. Stop the server with `Ctrl-C`. Pass `--port 9000` to change the
port, or `--no-reindex` to skip the startup rebuild.

### The layout

Three panels, left to right:

| Panel | What it's for |
|---|---|
| **Projects** (left rail) | Navigation tree. *Across everything* group at top; each project below it. Click a project to expand its sections; click a section or profile to load it in the Workspace. |
| **Workspace** (center) | The selected view — changes based on what you click in the rail. |
| **Consultant** (right) | The always-present AI advisor — arriving in a later build; a static placeholder for now. |

**Projects rail detail**

The *Across everything* group contains:

- **Needs you** — items waiting on your input or decision.
- **Operations** — cross-project to-dos and operational tasks.
- **Calendar** — month grid of all dated items across every project, today highlighted, ‹ Today › month navigation.

Each project expands into six sections:

> **Overview / Problem & validation / Experiments / Positioning & pricing / Product / Operations**

**Profiles** (your content presences) are nested inside each project. Click a profile to open its content board. Each profile has one or more **channels** (per-platform accounts).

**Workspace views**

- **Project section** — shows that section's data.
- **Profile content board** — plain stages **Idea → Draft → Scheduled → Published**, with row actions **＋ Add idea / ✦ Generate ideas / Edit / Delete**.
- **Calendar** — same month grid described above.

---

## Add-on bots

Five domain bots ship switched off in every workspace. Switch one on in the workspace's Team when you need it:

| Bot | Skills |
|---|---|
| @sales, Sales Lead | discovery-call, prospect-list, deal-review, objection-handling |
| @finance, Finance Lead | unit-economics, runway-plan, contract-check |
| @support, Support Lead | support-triage, faq-builder, feedback-synthesis |
| @ops, Operations Lead | weekly-plan, process-doc, vendor-compare |
| @people, People Lead | role-scope, interview-plan, onboarding-plan |

- While an add-on bot is off, its skills are on no bot's card, so they cost nothing per message. Typing `/<skill>` still runs it, as @cos.
- They draft and recommend. Sending, paying, signing, buying and posting stay your actions, or wait for your approval.
- *contract-check*, *runway-plan* and *unit-economics* are not legal, tax or investment advice. They flag what to confirm with a professional.

## Bots passing work on

In chat a bot can pass the work to a teammate with a `HANDOFF @handle: <task>` line, or ask one for a read-only view with `CONSULT @handle: <question>`. The teammate that gets the work may pass it on again, so one message can travel along a chain of bots (for example research, then marketing, then social). Every link shows in the thread (`from @bot · hop 2`), and your next untagged message goes to the last bot.

- Each bot keeps its own engine, access and approvals. A chain never widens what one bot may do.
- A bot can pass work only to an active bot in the same room.
- When two bots keep handing the work back and forth (four turns in a row), the chain pauses and asks you what to do next.
- A bot that asks for your approval ends the chain there: approve or reject, and the thread continues with that bot.
- Settings, General, *Bots passing work on*: hops per message and a cost cap per message. Both default to 0, which means no limit. Daily spending caps and Stop still apply.
- A workspace can set its own limits in Team, *Bot chains here*. An empty field uses the Settings value.
- A scheduled job may pass work along a chain too. Every hop runs in the job's mode, and its spend counts against the job's cap.

## Workflows

A workflow is a fixed sequence of skills, each run by a named bot. Start one in your own thread with `/<workflow> <brief>`, for example `/idea-to-experiment` followed by the idea in a few lines. The `/` menu tags workflows as "workflow".

- Each step is one ordinary chat turn by the bot the step names, so caps, chain limits, Stop and approval cards all apply as usual. The open tab sends the next step as soon as one ends.
- A step can name a checker: a second bot that answers PASS or WEAK. A weak or blocked step is tried once more with the notes. If it is still weak, the run pauses.
- A step can name up to 3 teammates under `consult:`. The step's bot writes a draft, each teammate advises on it in turn, then the bot revises it before the checker sees it. Advice and checks are read-only: they save nothing and raise no card. A weak check retries only the revision.
- A workflow never saves on its own. A step either writes its output in the thread, or proposes the save with an approval card. Approve and the run goes on; reject and it waits.
- The run pauses on a card, a step still weak after its retry, a failed turn, Stop, a closed tab, the cost cap, or 30 minutes with no step. The bar above the message box shows the step and why it waits. Continue picks it up; Stop workflow ends it.
- Settings, General, *Cost per workflow run*: dollars one run may spend over all its steps. 0 means no cap.
- Workflows run only in your own private threads, one run per thread. A thread invited people can see, an invited person's message or a scheduled job never starts one.
- The default team brings `/idea-to-experiment`, `/team-launch-plan` (@gtm drafts a launch plan, @research and @cmo advise, @product checks) and `/team-launch-debate` (@gtm asks only the teammates it needs, each advises without seeing the others, they agree or object for up to two rounds, @product checks).
- A step line may say `team: yes`: its bot decides whether to work alone or which teammates to consult (at most 3; a teammate may bring in one more), and a `consult:` list becomes a hint. `debate: 1` or `2` adds rounds where the consultants reply AGREE or OBJECT to the full output and the bot answers only the objections. Every advice, debate and draft turn is read-only; the save comes in its own turn.
- The add-on bots bring one workflow each: `/deal-prep` and `/outreach` (@sales), `/price-to-runway` (@finance), `/feedback-to-test` (@support), `/process-setup` (@ops) and `/hire-plan` (@people). Switch the bot on first.
- `leobot eval --compare` (or `python -m evals --compare`) checks whether a one-step workflow beats its maker bot working alone, and a case's `baseline` workflow when it names one. It runs each case every way in a throwaway home on your default engine, then a strong model scores each pair of plans twice, in both orders, without knowing which is which: a pair's winner is the plan both calls picked. It spends, so it asks first. Results are saved in `~/.leobot/evals/`. The shipped cases are made up; `--cases-file <path>` runs your own cases from a file kept outside the repo. `--plan` adds a plan the maker bot drafts itself (`/plan`, approved automatically), judged against the bot alone and against the workflow; a case with `lead` instead of `workflow` always compares a drafted plan with that bot alone.
- A workflow is a skill file with `kind: workflow` and a `## Steps` list (`skills/idea-to-experiment/SKILL.md` is the reference). It cannot start when a step's bot is switched off (switch it on in the workspace's Team) or lacks the step's skill. Stop workflow works from step 1 on.

### Plans: workflows built for one request

You do not need a workflow file for a request that needs several teammates. The bot drafts the steps itself and you approve them.

- Ask for one with `/plan <request>` (after a bot's handle too: `@gtm /plan a price change for May`), or in plain words. A bot also proposes a plan on its own when the request needs two or more teammates' areas. That plan replaces its answer: one line on why, then the plan. A request one bot can do well gets a normal answer.
- The plan shows as a card under the reply: a title, the brief the steps work from, and one line per step (bot, skill if any, output, gate, checker, consults). Only you see its buttons.
- **Approve** starts the run, once. It runs in the same thread when that is your private thread, otherwise in a new thread in #general. Everything after that is a normal workflow run: the bar, Continue, Stop workflow, the cost cap, and every gated step still asks with its own card. Approving a plan approves no save and no send.
- **Reject** takes an optional note. It goes to the bot as your next message: the bot redrafts the plan, or answers the request alone.
- A plan that names a bot not on the team (or not in this channel), a switched-off bot, or a checker equal to the step's bot shows "The plan did not hold up" with the reason, and no card.
- A plan's step may leave out `skill:`: the bot then works from its own instructions.
- **Save as template**, on a started plan's card: give a name and press Save. The plan becomes `~/.leobot/data/team/workflows/<name>.md`, and `/<name> <brief>` starts it like any workflow. Shipped workflows and skills keep their names: a saved one cannot take a name in use, nor `plan`.
- A scheduled job's bot may also end with a plan. It waits as a card for you; it never starts by itself.

## Loops

A loop is a scheduled job: one bot, one skill and one message, run on a schedule. A bot sets one up from your private thread, and you see it with Undo. Manage every loop in Team, *Manage loops*.

- A new loop starts in Report only: it reads and reports and saves nothing. Settings, General, *New loops start in* changes that default. You promote a loop to Approval or Auto yourself, on the Loops screen. A loop never raises its own mode or cap.
- Every new loop has a stop condition. When a run finds that it holds, the bot ends its reply with `LOOP DONE: <why>` and the loop turns itself off.
- A loop expires after 30 days (Settings, General, *Loops expire after*; 0 means it never expires). An expired loop pauses until you click Renew. Renewing never runs the slots it missed: it runs once at the next check.
- A loop can wake only when something changed. It can watch a connected app (as before), or Leo Bot's own data: the workspace's waiting cards, one list, its memos or its experiments. The check runs as code before the model, so a quiet run costs nothing.
- A loop never starts a workflow. It can suggest one with a `START: /<workflow> <brief>` line. You then get a *Start* button that opens a new thread with that message typed in for you to send.
- Before proposing a loop, a bot loads the *loop-design* skill: it picks the loop type and cadence, writes down the stop condition and what the notepad keeps, and shows you the design first. Each item a loop works on gets at most 3 tries; after that the loop stops and lists it for you.
- Two loop templates ship with Leo Bot. *weekly-review-loop* (@cos, Mondays) compares the week with last week's priorities. *signal-sweep* (@research, daily) reports only new outside signals that touch a decision. A template runs only as the bot it was made for.
- The add-on bots bring one template each: *pipeline-watch* (@sales, daily, wakes on the deals list), *reply-watch* (@sales, every 2 hours, wakes on new mail), *runway-check* (@finance, Mondays), *feedback-digest* (@support, Fridays), *daily-plan* (@ops, weekday mornings) and *hiring-watch* (@people, daily, wakes on the candidate list).
- The Loops screen shows each loop's mode, cap, schedule, stop condition, expiry, last and next run, cost over 7 days and its notepad (*State*). It lets you pause, edit, renew or delete each loop.

## Outreach

@sales can run outreach from end to end: `/outreach <who you sell to>` builds a sourced prospect list, drafts one first message per row, then plans every message as its own send. The *reply-watch* loop sorts the replies as they come in.

- Nothing goes out without your click. In a workflow step or a scheduled run every send waits as its own card, even when the bot's app is set to "Sends without asking" or its browser to "Acts on its own". Approving the workflow's plan approves no send. In your own chat those settings apply as you chose them.
- Each connected account has a daily send limit: 50 unless you set 1 to 500 in Apps, *Sends per day*. It counts every bot and workspace using that account for 24 hours. Over the limit, a bot's own send is refused and an Approve click leaves the card waiting.
- Browser sends are paced per site: LinkedIn 120 seconds apart, Instagram 180, other sites 30 (Settings, Browser, *Seconds between sends*). A send due within two minutes waits; a later one is refused with the time it can go.
- A reply that asks to stop is marked do-not-contact and gets no answer.
- Results have no metric source. *reply-triage* ends with a tally line (sent, replied, interested, opt-outs) that you save as an evidence entry (`add-evidence`).

## The content pipeline (two stages)

The system splits **cheap idea → expensive content** so you never generate posts
you'll throw away.

1. **Plan** = a calendar of lightweight idea-slots (date, channel targets, pillar, concept).
2. **Expand to full content** = turn one *approved* idea into the finished post
   (hook, caption, structure, hashtags, CTA, visual brief + AI-image prompt).

Status flow (each step is a button on the board):

```
planned → approved_slot → briefed → approved → scheduled → published
                                  (rejected at any point)
```

Content generation uses the **VOICE CASCADE**: project voice (`project.md` body)
→ profile voice (`profile.md` body) → channel guidelines (`channels/<slug>/guidelines.md`).
All three layers are injected automatically.

Generating a new plan (more ideas on the board):

```bash
python3 generate.py plan <profile-slug> \
  --period "2026-06-30 to 2026-07-13" \
  --platforms tiktok,instagram --cadence 3 \
  --focus "optional steer, e.g. push the launch"
# then refresh the dashboard
```

`<profile-slug>` is looked up under `projects/*/profiles/<profile-slug>/` automatically.

Expanding one slot from the terminal (same as the board button):

```bash
python3 generate.py brief <profile-slug> <post-id>
```

---

## Channel guidelines

Each channel has `projects/<project-slug>/profiles/<profile-slug>/channels/<channel-slug>/guidelines.md`
with a `## General` section plus per-platform sections (`## Instagram`, `## TikTok`, …).
These are **injected into every generation** for that channel via the voice cascade,
so your rules ("on IG always Reels", "end with a question") are actually followed.

Edit them in the **Channels** tab on the profile board. **Refine with AI** sends
rough notes through `claude -p` and returns clean structured guidelines into the
editor — review, then **Save** (it doesn't save until you do).

The refine command:
```bash
echo "rough notes..." | python3 generate.py refine-guidelines <channel-slug>
```

`<channel-slug>` is looked up under `projects/*/profiles/*/channels/<channel-slug>/` automatically.

---

## How it works (the one invariant)

```
authored files ──index──▶ ~/.leobot/data/database/data/os.db (read-only) ──▶ dashboard views
      ▲                                                          │
      └──────── mutate FILE ◀── dashboard action ──▶ re-index ───┘
```

- **Authored files = source of truth** (markdown + JSON you write/edit).
- **`os.db` = a derived, disposable index** — fully rebuilt from the files by
  `index.py`. Never hand-edit it; deleting it is safe.
- The **dashboard mutates files, then re-indexes** — it never writes `os.db`
  directly (its DB connection is read-only, so it physically can't).
- A **PostToolUse hook** re-indexes automatically when a source file changes.

Rebuild the index manually anytime (rarely needed):

```bash
python3 index.py
```

---

## Where things live

```
Leo Bot/                     ← the code (~/.leobot/app, or a clone)
├── docs/                    ← this guide
├── database/                ← all DB stuff (the only db-related folder)
│   └── migrations/          ← schema (0001_init.sql); index.py applies *.sql in order
├── index.py                 ← builds os.db from the files
├── generate.py              ← claude -p jobs: plan, brief, refine-guidelines
├── prompts/                 ← prompt templates + platform constraints
├── hooks/reindex.py         ← auto re-index on source edits (PostToolUse)
├── dashboard/               ← db.py (read), fileops.py (write), server.py, app.html
```

Your data lives outside the code, in `~/.leobot/data` (or `$LEOBOT_HOME/data`):

```
~/.leobot/data/
├── database/data/os.db      ← derived index, regenerated
├── portfolio/               ← activities.md, milestones.json
└── projects/                ← all projects (nested: strategy/, products/, profiles/, channels/)
    └── <project-slug>/
        ├── project.md       ← project voice + metadata
        ├── subsections.json ← per-project tab subsection lists (intake, technical, roadmap, validation_tab)
        ├── strategy/        ← intake.md, memos/, experiments/
        ├── products/<slug>/ ← roadmap.md
        └── profiles/<slug>/ ← profile.md, content/, channels/<slug>/
```

---

## ID cascade (canonical references)

Every clickable thing in the dashboard gets a **composed ID**: a dot-separated path
from root → tab → artifact. The string *is* the parent chain — never skip a level.

```
pr2                         project 2
├── sec04                   Positioning & pricing tab
│   ├── mm1                 positioning memo
│   └── mm2                 pricing memo
├── sec03                   Experiments tab
│   └── ex1                 experiment
├── sec05                   Product tab
│   └── pd1                 product
│       └── ft1             roadmap feature
└── pf1                     profile
    ├── sec00               Posts tab
    │   └── po1             plan slot / idea
    │       ├── sl01        slot field (working_title, concept)
    │       └── br1         brief
    │           └── fd01    brief field (caption, slide-1, …)
    └── ch1                 channel
        └── sec00           Guidelines tab
```

**Segment prefixes**

| Prefix | Thing |
|--------|--------|
| `pr` | project |
| `sec` | tab (project section, profile Posts/Setup, channel Guidelines/Setup) |
| `mm` | strategy memo |
| `ex` | experiment |
| `pd` / `ft` | product / feature |
| `pf` / `ch` / `po` | profile / channel / post |
| `sl` / `br` / `fd` | slot field / brief / brief field |
| `vw` | global view (calendar, operations, …) |

**Which tab owns what**

| Tab (`sec`) | Artifacts |
|-------------|-----------|
| `sec01` Overview | assessment memo |
| `sec02` Problem & validation | problem-validation memo |
| `sec03` Experiments | experiments (`ex`) |
| `sec04` Positioning & pricing | positioning, pricing, competitors, icp, channels memos |
| `sec05` Product | products (`pd`) and features (`ft`) |
| `pfN.sec00` Posts | posts (`po`) and everything under them |

Form metadata (project name, profile voice, channel handle, post date/format) has
**no** field ids — edit via the entity id (`pr1`, `pr1.pf1`, `pr1.pf1.sec00.po3`).

**Lookup**

```bash
python3 dashboard/osctl.py get-id-catalog          # full list
python3 dashboard/osctl.py get-id-catalog --text # readable
python3 dashboard/osctl.py resolve-id --id pr2.sec04.mm1
```

In chat/terminal, `@pr2.sec04.mm1` or a bare slug (`acme`) when unambiguous.
Source of truth: `core/ids.py`. Dashboard tab layout from `GET /api/schemas`.

---

## Tab subsections (per project)

The six project tabs (`sec01`–`sec06`) are fixed for every project. **Subsections**
are the ordered `##` headings inside a tab's markdown doc — and they can differ
per project.

| Doc key | File | Tab |
|---------|------|-----|
| `intake` | `strategy/intake.md` | Problem & validation (filtered) |
| `technical` | `technical.md` | Technical |
| `roadmap` | `products/<slug>/roadmap.md` | Product (shared list per project) |

Config: `projects/<slug>/subsections.json`. Defaults ship in `GET /api/schemas`
(`subsections.docs.*.default_subsections`). Runtime reads the project file after
first write or re-index.

```bash
python3 dashboard/osctl.py get-subsections --project demo
python3 dashboard/osctl.py update-subsections --project demo --doc technical \
  --subsections "Stack,Prompt,Tools"
python3 dashboard/osctl.py add-subsection --project demo --doc technical --title "Evals"
python3 dashboard/osctl.py update-validation-tab --project demo \
  --subsections "Stage & evidence,Market,Goals"
```

- `update-subsections` / `add-subsection` — change doc heading lists; normalize
  rewrites the markdown file.
- `update-validation-tab` — choose which **intake** subsections appear on sec02
  (never includes "What it is"; titles must already be in the intake list).
- `update-intake` / `update-technical` / `update-roadmap` — extra `##` headings in
  pasted text auto-register on save.

Source: `core/subsections.py`. Chat and dashboard mutations go through `osctl` /
`fileops` (same normalize path).

---

## Results loop

Experiments and memos stay honest against what actually happened.

- **Metric source.** `osctl set-metric --project demo --stem will-pay --app acme --action ACME_GET_STATS --path data.followers` saves where an experiment's number comes from. Only a read-only app action is accepted; a write action, a URL argument or a paid or blocked app is refused with the reason. `path` is dotted keys, with digits for a list index. `update-experiment` cannot set it.
- **Scoring job.** Create a scheduled job with the `score-experiments` skill (ask a bot in your private thread; manage it in Loops). On each run the server reads every metric source (no click), takes the number, and asks the engine for a verdict against the success and kill criteria written in advance: on track, met, killed or inconclusive. What a source returns is quoted as data and never read by a bot. A failed read or bad path is `inconclusive` with the reason. Calls count against the job's cap.
- **Verdict card.** Each verdict is a card in the job's thread with the number, window, date, criteria and reason. Approve saves the result on the experiment (status `done` for met or killed, `running` for on track) and adds one evidence entry. No saves nothing. A second click does nothing. A repeat run with the same number does not make a second card; a new number replaces the old pending one.
- **Evidence log.** `osctl add-evidence --project demo --text "..." --ref <experiment or assumption id>` appends a dated, numbered entry (ids like `pr1.sec02.ev3`) under the intake's Evidence log. Entries are never edited.
- **Assumptions.** `osctl extract-assumptions` proposes the assumptions a memo rests on from its prose; once you agree, `--items-json` saves them with stable ids (`pr1.sec04.mm1.as2`) that carry across memo versions. `list-assumptions` shows them with their status.
- **Contradictions.** Evidence added against an assumption is judged once; only a contradiction raises a card, and only Approve marks the assumption `contradicted`.
- **Stale memos.** A memo holding a contradicted assumption shows a Stale flag naming the assumption and its evidence, and the state index tells the bots. Stale is worked out on every read, never stored. It clears when a new version replaces that assumption's text.

---

## Typical session

1. `python3 dashboard/server.py` → open the URL
2. Review the **Content board**; reject weak ideas, approve good ones
3. **Expand to full content** on the approved ones; read them in the drawer
4. Spot a recurring rule? Add it in **Channels** → Refine → Save
5. Need more ideas? `python3 generate.py plan <profile-slug> …`, then refresh
6. Strategy work (intake, memos, experiments) is edited as files under
   `projects/<slug>/strategy/`; the dashboard reflects them after a re-index.
