# myOS — Claude Code Skills (v2)

A 23-skill operating system for running a portfolio of projects, products, profiles,
and social channels from Claude Code — problem validation, GTM strategy, product
roadmaps, and manual activity tracking, organized in layers.

Every strategic skill behaves like a senior consultant:
options → pros/cons → one recommendation → what would change it.

## Workspace structure

```
my-os/                          ← your clone; data folders below are gitignored
├── database/data/os.db         ← derived SQLite index (disposable; rebuilt from files)
├── portfolio/
│   ├── milestones.json         ← manually entered dates/events (copy from template)
│   ├── activities.md           ← manual non-GTM tasks/micro-items (checklist you tick off)
│   └── timeline-<date>.json    ← unified timeline (generated, rebuilt on demand)
└── projects/
    └── <project-slug>/
        ├── project.md          ← frontmatter: name, kind (venture|brand), priority,
        │                          hours_per_week, status; body = overall project voice
        ├── strategy/           ← (venture projects only)
        │   ├── intake.md       ← venture facts + evidence log
        │   ├── memos/          ← problem-validation, assessment, channels, positioning, pricing, launch
        │   └── experiments/    ← designs + results
        ├── products/           ← 0..n generic products (app|physical|service)
        │   └── <product-slug>/
        │       ├── product.md  ← frontmatter: name, type, status
        │       └── roadmap.md  ← features + micro-features (checklist, hand-edited)
        └── profiles/           ← content presences with their own topic + voice
            └── <profile-slug>/
                ├── profile.md  ← frontmatter: name, topic; body = profile voice
                ├── content/
                │   ├── plan-<period>.json
                │   └── briefs/
                └── channels/   ← per-platform accounts
                    └── <channel-slug>/
                        ├── channel.md      ← frontmatter: platform; body = per-platform notes
                        └── guidelines.md   ← per-channel posting guidelines
```

**Voice cascade** (injected into every content job):
`project.md` body → `profile.md` body → `channels/<slug>/guidelines.md`

**ID cascade** (dashboard @-mentions): every artifact gets a path id that mirrors
the UI tree — `pr1.sec04.mm1` (pricing tab → positioning memo),
`pr1.pf1.sec00.po3.br1.fd02` (profile → Posts → post → brief field). See
[`docs/guide.md`](docs/guide.md#id-cascade-canonical-references).

---

## Install

Prerequisites: Python 3.10+ (stdlib only, nothing to `pip install`), the Claude Code CLI logged in
(`claude --version`), and for the macOS app, Xcode Command Line Tools.

```bash
git clone <this repository> my-os && cd my-os
python3 dashboard/server.py   # open the login link it prints
```

Your data lives inside the clone, in `projects/`, `portfolio/` and `chats/`.
All three are gitignored: they never leave your machine, which also means git
does not back them up. Include the folder in your own backups.

The dashboard chat loads skills from `skills/` by itself. For plain `claude`
sessions in a terminal, Claude Code does not read `skills/`: copy them into
your global Claude Code skills directory first:

```bash
cp -r skills/* ~/.claude/skills/
```

### Always on: run it on a server

Run myOS on a small server and it stays on when your laptop is closed: [docs/server.md](docs/server.md). Bots run on your Claude login or an API key (HQ, Engines), and people you invite never use your login: [docs/sharing.md](docs/sharing.md).

### Linked folders

Each workspace, HQ included, can link any number of folders on the machine running myOS
(the workspace's Folders screen). Each folder is **Edit** or **Read**, and a channel or a
bot can be limited to some folders or to read access. Bots read and search them on your
private threads. On Claude Code and API-key engines, a tagged message (@bot, /skill, a DM)
can change files in Edit folders; an untagged one only reads. Bots never touch git, editor
or secret files (`.git/`, `.claude/`, `.vscode/`, `.env` and the like), and they cannot run
tests, builds or git: they name the files they changed and you review them in your own git.
Codex and other CLIs stay read-only. Invited people, shared threads and runners never reach
a linked folder or see its path.

### Dashboard (macOS app)

The app's launcher binary is not committed: build it from source once after cloning. With the repo hooks enabled (`git config core.hooksPath .githooks`), every pull or checkout that changes the app's source rebuilds it automatically.

```bash
scripts/build-app.sh   # needs Xcode Command Line Tools
open myOS.app
```

Without the app, run `python3 dashboard/server.py` and open the login link it prints (it contains a per-run token; a plain `http://127.0.0.1:8765` shows a login-required page).

---

## The 24 skills

### Layer 0 — OS dispatcher
| Skill | What it does |
|---|---|
| `workspace` | Master dispatcher: routing table, file conventions, hard rules |

### Layer 1 — Portfolio (cross-entity coordination)
| Skill | What it does |
|---|---|
| `portfolio-map` | Scaffold the project/profile/product/channel folder structure |
| `portfolio-timeline` | Build the unified timeline across all entities (queries `os.db`) |
| `portfolio-sync` | Surface this week's cross-entity coordination actions |

### Layer 2 — Project / GTM brain (per-project, kind=venture)
| Skill | What it does |
|---|---|
| `problem-validation` | Validate the problem is real before any build/GTM (full or quick) |
| `venture-intake` | Interview → strategy/intake.md with evidence log |
| `gtm-assessment` | ICP hypotheses, positioning options, pace call |
| `channel-strategy` | Channel comparison, entry strategy, social mandate |
| `experiment-design` | Cheapest test, success/kill criteria |
| `experiment-review` | Log results, persist/pivot/kill |
| `icp-research` | Segment memo, where to find 10 this week, interview guide |
| `positioning` | Category framing, differentiation, messaging hierarchy |
| `competitor-scan` | Landscape map, gaps, how to play it |
| `pricing-strategy` | Model + price point options, price-as-test |
| `launch-plan` | Soft vs loud, sequencing, assets, failure plan |

### Layer 2b — Product (per product, generic: app|physical|service)
| Skill | What it does |
|---|---|
| `product-build` | Roadmap, feature planning, manual micro-features, releases → content ramp |
| `tokensave` | Read-only look at a linked folder through its TokenSave index |
| `folder-work` | Read and change code and docs in a linked folder (CTO's default) |

> Manual micro-features and non-GTM tasks: add them by hand to
> `products/<slug>/roadmap.md` or `portfolio/activities.md` (markdown checklists)
> and tick them off — no skill needed; the timeline picks them up.

### Layer 3 — Profile & content (per profile)
| Skill | What it does |
|---|---|
| `brand-identity` | Create/refresh profile.md and channel guidelines from interview or project mandate |
| `content-plan` | Calendar skeleton with channel targets (approve before expanding) |
| `content-brief` | Full briefs with visual brief + genai prompt draft |
| `copy-variants` | Cross-channel adaptation, hook batches, A/B pairs |

### Layer 4 — Operating cadence
| Skill | What it does |
|---|---|
| `weekly-review` | Per-project snapshot + priorities (queries `os.db`) |
| `portfolio-sync` | Cross-entity coordination actions for this week |

---

## The full loop

```
SETUP
  portfolio-map → scaffold projects/<slug>/ structure

PER PROJECT (kind=venture)
  problem-validation → is the problem real? (gates the pace call)
  venture-intake → strategy/intake.md
  gtm-assessment → memo: ICP + pace call
  channel-strategy → memo: primary channel + social mandate
  experiment-design → design file (run the test)
  experiment-review → result logged to intake evidence
  [assessment rerun when evidence changes]

PER PRODUCT (app|physical|service)
  product-build → products/<slug>/roadmap.md (features, micro-features, releases)
  [a release triggers a content ramp on linked profiles]

PER PROFILE (may be triggered by channel-strategy social mandate)
  brand-identity → profile.md + channel guidelines
  content-plan → skeleton (you approve slots)
  content-brief → full briefs for approved slots → scheduler

COORDINATION (all query os.db, the derived index)
  portfolio-timeline → unified view rebuilt after any major change
  portfolio-sync → cross-entity actions this week
  weekly-review → per-project priorities

FUTURE (plugs into visual_brief.genai_prompt_draft)
  → image gen tool (Midjourney / Ideogram / Flux)
  → video gen tool (Runway / Kling / Hailuo)
```

---

## First session

```
1. "Set up the portfolio structure"             → portfolio-map
2. "Is [idea] worth doing?"                     → problem-validation
3. "Intake for [your first project]"            → venture-intake
4. "GTM assessment for [project]"               → gtm-assessment
5. React to the memo. Push back.
6. "Design an experiment for the riskiest assumption"  → experiment-design
7. (Later) "Build the unified timeline"         → portfolio-timeline
```

---

## Design principles

- **Decision memos, not plans**: every strategic output gives options, not one answer.
- **Evidence-driven**: pace calls, recommendations, and ICP confidence are tied to
  logged evidence — not vibes.
- **Timeline is derived**: coordination skills query `os.db`, a disposable SQLite
  index rebuilt from the source files; source files always win. Rebuild after any
  significant update.
- **Manual layers are hand-edited files**: `milestones.json` (fixed dates — releases,
  investor meetings, deadlines) and `activities.md` / `products/<slug>/roadmap.md`
  (tasks and micro-features you tick off). Things no skill knows unless you tell it.
- **Skills improve like docs**: when output feels off, edit the SKILL.md.
  The OS gets better permanently.

---

## License

myOS is source-available under the [PolyForm Shield License 1.0.0](LICENSE).

- Allowed: using it for anything, including running your own business;
  modifying it; sharing copies and forks (keep the `LICENSE` file with them).
- Not allowed: offering a product or service that competes with myOS, such as
  reselling it, rebranding it, or hosting it as a paid service.
- Want to build a competing product? That needs a separate license: open an
  issue to ask.

The bundled terminal (`dashboard/vendor/`) is xterm.js, MIT-licensed: see
`dashboard/vendor/LICENSE-xterm`.

---

## Contributing: privacy guard

### Safe public export

The working directory can contain ignored local projects, databases, logs, and
machine settings. Never publish or zip the working directory itself. Export only
Git-tracked content from a clean commit:

```bash
scripts/export-public.sh /tmp/myOS-public.zip
```

The exporter refuses a dirty tree and uses `git archive`, so ignored local data
cannot enter the archive.

### Automatic commit protection

Enable the repository's privacy hooks once per clone:

```bash
git config core.hooksPath .githooks
```

The pre-commit hook scans the exact staged snapshot and blocks every file under
`projects/`, `portfolio/`, and other myOS data directories—even if it was added
with `git add -f`. It also blocks databases, logs, credentials, secrets, personal
paths, private project terms, and unapproved network hosts. The pre-push hook
checks the complete committed history again before anything leaves the machine.
