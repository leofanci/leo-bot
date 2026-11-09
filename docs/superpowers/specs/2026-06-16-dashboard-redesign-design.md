# Leo Bot Dashboard — Redesign Design Spec

Date: 2026-06-16
Status: approved visual direction; functional design under review

## 1. Why

The current dashboard is organized by *tool* (Content board, Timeline, Roadmaps,
Channels, Overview) when the user's mental model is by *project*. It's visually
generic, has no AI consultant, and offers no way to create/delete the user's own
items. This redesign re-centers the app on **projects**, adds an always-present
**AI consultant**, and makes **everything CRUD-able** — by hand and by the AI.

It is a rewrite of `dashboard/app.html` plus extensions to `dashboard/server.py`,
`dashboard/db.py`, and `dashboard/fileops.py`. The backend invariants do not change.

## 2. The invariant (unchanged — hard constraint)

```
authored files ──index.py──▶ database/data/os.db (read-only) ──▶ dashboard reads
      ▲                                                              │
      └──────── mutate FILE ◀── action (manual OR AI-applied) ──▶ reindex
```

- Authored files are the single source of truth (markdown + JSON).
- `os.db` is a derived, disposable index. The dashboard opens it **read-only**
  (`file:…?mode=ro`) and never writes it.
- Every change — manual button or AI "Apply" — mutates a **file**, then re-runs
  `index.py`. There is exactly one write path: `fileops`.
- The consultant chat is **ephemeral** (held by the browser; the server is
  stateless per call). Nothing about a conversation is indexed.

## 3. Visual direction (locked)

- **Three docked glass panels** on a flat canvas, 14px gutters, 24px radius:
  left **Projects rail** · center **Workspace** · right **Consultant**.
- **Flat glassmorphism**: translucent blurred panels, hairline white borders,
  soft shadows. No gradients anywhere.
- **Palette — Graphite & Sky**: graphite `#323a47` (brand, primary buttons,
  avatar, your chat replies); sky `#2f80ed` (active nav, links, "idea" markers,
  @-mentions, AI emphasis); teal `#17916f` ("written/done"); canvas `#e9ecf1`.
- **Type**: Avenir Next display, system sans body.
- Reference mockup: `.superpowers/brainstorm/.../final-graphite-sky.html`.

## 4. Information architecture

Left rail has two sections:

- **Across everything** — `Needs you`, `Activities`, `Timeline` (span all projects).
- **Projects** — each project, expandable, with a **type chip**
  (`venture` | `social` | `standalone`). Children, nested on the rail:
  `Overview & strategy`, `Channels` (each channel nested under it),
  `Product roadmap`, `Experiments`. Footer: **＋ New project**.

A project does **not** have to be a venture. Most will be, but a standalone
social-media project (channels only, no venture strategy) is a first-class case.

## 5. Data-model implications

The schema is mostly sufficient; the gaps:

1. **Standalone projects.** Today a "project" is implied by a venture entity.
   We need a project that may have *no* venture semantics. Approach: keep
   `relationships.md` as the authored source, but allow a `## Projects` entry
   whose `type` is `social`/`standalone` and which need not carry venture fields
   (hours_per_week, validation, etc.). `entities.type` already has no CHECK, so
   the index accepts new types; `index.py` gains a small parser branch.
2. **Channels under any project.** Brands already reference a `venture`; generalize
   the field to `project` (keep `venture` as an accepted alias for back-compat).
3. Everything else (activities, experiments, memos, posts, features, timeline view)
   stays as-is.

`index.py` changes: parse standalone projects; treat `project`/`venture` as the
same grouping key; otherwise unchanged (still full wipe + rebuild).

## 6. Center workspace — the views

Each view reads from `os.db` (read-only) and writes via `fileops`.

1. **Needs you (home).** A single prioritized action list across all projects:
   posts in `briefed` awaiting approval, `approved` awaiting schedule, experiments
   `planned` but not started, weak-validation ventures with no live experiment,
   stale activities. Each row deep-links to the item. Pure derivation from the index.
2. **Activities.** Cross-project to-do/GTM list (`portfolio/activities.md`).
   Add / edit / delete inline.
3. **Timeline.** The `timeline` SQL view, grouped by week, kind-colored. Read-only
   (items are edited where they live).
4. **Project · Overview & strategy.** Intake summary, decision memos
   (problem-validation, assessment), validation status + pace call, riskiest
   assumption. Edit opens the underlying file's fields.
5. **Channel.** Sub-segment control: **Content** (full written posts) · **Planned**
   (idea slots) · **Calendar** (by date) · **Guidelines**. Actions: `＋ Add post`
   (manual slot), `✦ Plan content` (`generate.py plan`), `Expand to full content`
   (`generate.py brief`), and the status pipeline
   (`planned → approved_slot → briefed → approved → scheduled → published`,
   `rejected` reopen). Guidelines editor keeps **Refine with AI** + **Save**.
6. **Product roadmap.** Features grouped by build status (`idea/planned/building/
   shipped`). Add / edit / delete.
7. **Experiments.** List + detail (assumption, options, recommendation,
   success/kill criteria). Add / edit / delete; mark started / log result.

## 7. The Consultant (right panel)

- **Always docked.** Context-aware of the current selection: each turn sends a
  compact OS snapshot (built from `os.db`) plus the selected entity's data.
- **@-mentions.** Typing `@` opens a picker of projects/channels/posts/
  experiments/activities; a chosen item's data is injected into the prompt so the
  user can "refer to projects or micro stuff."
- **Advise + Propose → Apply** (chosen model). The model replies with prose and,
  when a change is warranted, a structured **proposal**: a file mutation
  (create/edit/delete a post, activity, experiment, project, channel, or guideline).
  The UI renders the proposal as an **Apply** card with a human summary + diff.
  *Nothing is written until the user clicks Apply* — which calls the same `fileops`
  endpoint a manual edit would. This keeps one write path and the files-are-truth
  invariant intact.
- **Backend:** `POST /api/consult` with `{messages, context_ref, mentions}`.
  Server builds the context blob, shells `claude -p --output-format json`
  (identity/strategy via stdin), returns `{reply, proposals[]}`. v1 returns the
  full reply (no token streaming); streaming can come later.
- **Ephemeral:** the browser holds the transcript and sends recent turns back;
  the server keeps no state and nothing is indexed.

## 8. CRUD architecture (manual and AI share one path)

`fileops` gains create/update/delete for every type, each mapping to authored files:

| Type | Authored file(s) |
|---|---|
| Post / idea slot | `brands/<slug>/content/plan-*.json` (+ `briefs/*.json`) |
| Activity | `portfolio/activities.md` |
| Experiment | `ventures/<slug>/experiments/*.json` |
| Project | `portfolio/relationships.md` (+ scaffold dirs) |
| Channel | `relationships.md` brands entry (+ brand dir, identity, guidelines) |
| Guideline | `brands/<slug>/guidelines.md` (not indexed) |

Every mutation: **write file → `index.py` reindex → return fresh data.** AI
proposals produce the *same* payloads; **Apply** calls the *same* endpoints. So
manual and AI changes are identical at the file layer, and `os.db` is never
written directly by either.

## 9. API surface (server.py, stdlib `http.server`)

- **Reads:** `/api/tree` (projects + nested children + counts), `/api/needs`,
  `/api/activities`, `/api/timeline`, `/api/project/<slug>`,
  `/api/channel/<slug>/{content|planned|calendar|guidelines}`,
  `/api/roadmap/<slug>`, `/api/experiments/<slug>`, `/api/post/<id>`.
- **Writes (generic):** `POST/PATCH/DELETE /api/<type>/…` for each type above;
  status transition; `plan`; `expand`; guidelines `refine`/save.
- **Consultant:** `POST /api/consult`.
- Startup re-index unless `--no-reindex`; default port 8765.

## 10. Tech constraints (unchanged)

Python stdlib only — `http.server`, `sqlite3` (read-only conn), `subprocess`
(`claude -p`, `index.py`), `json`, `pathlib`. One `app.html` with vanilla JS and
the design system inline. No framework, no build step, no server DB, no ORM.
Quality floor: responsive enough for a laptop, visible focus states, no motion
that reads as AI-generated.

## 11. Build sequence (hand-off to the plan)

1. **Shell + reads.** New `app.html` shell (three glass panels, rail tree),
   `/api/tree`, project/channel/timeline reads. Replaces the tab UI.
2. **Channel + content CRUD.** Content/Planned/Calendar/Guidelines, add post,
   plan, expand, status pipeline, guidelines refine/save.
3. **Consultant — advise.** `/api/consult`, context blob, @-mentions, chat UI.
4. **Propose → Apply + full CRUD.** Generic `fileops` create/update/delete for
   all types; AI proposal cards wired to the same endpoints.
5. **Remaining views.** Needs-you, Activities, Experiments, Roadmap, Overview
   polish; standalone-project + project-grouping `index.py` changes.

## 12. Out of scope (YAGNI)

Multi-user / auth / remote deploy (the architecture stays "server-shaped" but
local). Token-by-token streaming. Drag-and-drop calendar. Conversation history
persistence. A real migration off SQLite.
