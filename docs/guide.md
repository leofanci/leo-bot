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
authored files ──index──▶ database/data/os.db (read-only) ──▶ dashboard views
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
Leo Bot/
├── docs/                    ← this guide
├── database/                ← all DB stuff (the only db-related folder)
│   ├── migrations/          ← schema (0001_init.sql); index.py applies *.sql in order
│   └── data/os.db           ← derived index, regenerated (gitignored)
├── index.py                 ← builds os.db from the files
├── generate.py              ← claude -p jobs: plan, brief, refine-guidelines
├── prompts/                 ← prompt templates + platform constraints
├── hooks/reindex.py         ← auto re-index on source edits (PostToolUse)
├── dashboard/               ← db.py (read), fileops.py (write), server.py, app.html
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

## Typical session

1. `python3 dashboard/server.py` → open the URL
2. Review the **Content board**; reject weak ideas, approve good ones
3. **Expand to full content** on the approved ones; read them in the drawer
4. Spot a recurring rule? Add it in **Channels** → Refine → Save
5. Need more ideas? `python3 generate.py plan <profile-slug> …`, then refresh
6. Strategy work (intake, memos, experiments) is edited as files under
   `projects/<slug>/strategy/`; the dashboard reflects them after a re-index.
