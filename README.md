# Leo Bot — Claude Code Skills (v2)

A 23-skill operating system for running a portfolio of projects, products, profiles,
and social channels from Claude Code — problem validation, GTM strategy, product
roadmaps, and manual activity tracking, organized in layers.

Every strategic skill behaves like a senior consultant:
options → pros/cons → one recommendation → what would change it.

## Workspace structure

```
~/.leobot/data/                   ← your data (a clone's own folders are legacy, moved here)
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

Prerequisites: Python 3.10+ (stdlib only, nothing to `pip install`), git, and the Claude Code CLI
logged in (`claude --version`; the installer offers to set it up, and bots can also run on an API key).

One command, on macOS or Debian/Ubuntu, no sudo on a Mac. Replace `<repo>` with this repository's
address; its [latest release](../../releases/latest) page has the same line:

```bash
curl -fsSL <repo>/releases/latest/download/install.sh | bash
```

It creates `~/.leobot` (or `$LEOBOT_HOME`) with the code in `app/`, your data in `data/`, and
`run/`, `logs/` and `backups/`, adds a `leobot` command to `~/.local/bin`, and on a Mac builds
`LeoBot.app` into `~/Applications` when the Command Line Tools are there. Flags: `--channel main`
for the latest code instead of releases, `--version vX.Y.Z`, `--service` / `--no-service`,
`--no-app`, `--claude` (install Claude Code without asking), `--yes`, `--dry-run`.

`leobot` basics:

- `leobot` opens Leo Bot (starts it if needed): the app on a Mac, the browser on a Linux desktop,
  and on a server it prints the link to open.
- `leobot start`, `leobot stop`, `leobot status`
- `leobot update` updates to the latest release (a backup is taken first); Settings has the same button.
  Once a day the running server asks the repo it was installed from whether a release is out
  (a `git ls-remote`: that host sees your address, nothing else is sent). Turn it off with
  `leobot update --auto-check off`.
- `leobot service install` keeps it running in the background (launchd on a Mac, a systemd user unit on Linux).
- `leobot doctor` checks the setup; `leobot version` prints the version.
- `leobot backup` snapshots your data into `~/.leobot/backups/`.
- `leobot eval` checks bot behavior offline for free: routing, model choice, the save gate,
  approval cards and memory recall, on a throwaway workspace. `leobot eval --live` sends a few
  known-answer prompts to your engine (it asks first and shows the cost) and compares the run
  with that engine's last one, saved in `~/.leobot/evals/`. `--engine all` runs the same cases
  on every ready engine, so they stay alike.
- `leobot uninstall` removes the app and keeps your data unless you add `--data`.

Your data lives in `~/.leobot/data` (`projects/`, `portfolio/`, `chats/`, `team/`). It never
leaves your machine, which also means nobody else backs it up: `leobot backup` makes a local
snapshot, and an update takes one first, but copy `~/.leobot/backups/` somewhere safe yourself.

### Develop

To work on Leo Bot itself, run it from a clone:

```bash
git clone <this repository> leo-bot && cd leo-bot
python3 dashboard/server.py   # open the login link it prints
```

The clone shares `~/.leobot` with an installed copy, so only one server runs at a time: stop the
other first (`leobot stop`). On first start, data that older versions kept inside the clone
(`projects/`, `portfolio/`, `chats/`, `team/`) is moved to `~/.leobot/data`, after a backup in
`~/.leobot/backups/`. If `~/.leobot/data` already holds data, nothing is moved and `leobot doctor`
shows the two ways out: `leobot move-data --from <clone> --archive` keeps the home folder and
archives the clone's copy into `~/.leobot/backups/`, `--replace` uses the clone's copy instead
(after a backup of the home folder). Tests: `python3 -m unittest discover -t . -s tests` (the
`-t .` matters: it pins a temp home; without it the tests stop at once rather than touch yours).

The dashboard chat loads skills from `skills/` by itself. For plain `claude`
sessions in a terminal, Claude Code does not read `skills/`: copy them into
your global Claude Code skills directory first:

```bash
cp -r skills/* ~/.claude/skills/
```

### Always on: run it on a server

Run Leo Bot on a small server and it stays on when your laptop is closed: [docs/server.md](docs/server.md). Bots run on your Claude login or an API key (Settings, Engines), and people you invite never use your login: [docs/sharing.md](docs/sharing.md).

### Linked folders

Each workspace, HQ included, can link any number of folders on the machine running Leo Bot
(the workspace's Folders screen). Each folder is **Edit** or **Read**, and a channel or a
bot can be limited to some folders or to read access. Bots read and search them on your
private threads, and look up a folder's code index with `osctl code-context` when it has
one. On Claude Code and API-key engines, a tagged message (@bot, /skill, a DM)
can change files in Edit folders; an untagged one only reads. Bots' file tools never touch
git, editor or secret files (`.git/`, `.claude/`, `.vscode/`, `.env` and the like). Codex
and other CLIs stay read-only. Invited people, shared threads and runners never reach a
linked folder or see its path.

Each bot has an **access level** (Settings, Bots), for your private threads only:

- **Ask** (default): changes files only when you tag it; its shell runs `osctl` only.
- **Auto-accept edits**: changes files in its Edit folders on any of your messages.
- **Full access**: also runs any shell command in its Edit folders (Claude Code on your own
  Mac; not on a server or a runner): build, test, install, commit. An Auto job on that bot
  gets the shell too. Leo Bot checks each command as it runs: commands that could wreck the
  machine (deleting the disk, the home folder or a system folder, formatting a disk, a fork
  bomb, shutting down) never run, and once the conversation holds mail, pages or app data,
  commands that send data out (curl, git push, ssh, a publish, gh) are refused. This is a
  check on top of your choice, not a sandbox: the bot runs commands as you do.

### Reactions

Hover any saved message in a thread and pick an emoji; click a chip to add or take back
yours, hover it to see who reacted. You, invited people (on threads they can read) and
bots all react. A bot reacts to the message it answers with one line `REACT: 👀` in its
reply, and a reply of only that line shows as the emoji alone. Bots see people's reactions
as context on their next turn; a reaction never triggers an action.

### Bot memory

Each bot keeps one short list per workspace (a private one and, in a company, a team
one), one entry per line: `- 2026-11-11 [decision] text`. Types are `fact`,
`preference` and `decision`; a line without one reads as a fact. Add entries in Team
under the bot's Memory, or let a bot save one with `osctl remember --type ...`.

- A bot's card carries its decisions and preferences (newest 10) plus the 5 entries that
  best match the message that opened it. Older entries stay a lookup away:
  `osctl recall --bot <id> --query "..."`, scoped to the thread's workspace and scope.
- A list holds up to 400 entries. Past that the oldest facts go first; decisions and
  preferences are never dropped, so a list full of them asks you to forget one.
- At 300 entries a bot's list gets one background tidy-up on the fast model: near
  duplicates merged, replaced facts dropped. It keeps the previous list as
  `<bot>.md.bak`, is rejected if it drops or changes a decision or preference, never
  runs from a thread invited people can see, and counts against your caps. Its cost
  shows on the Team screen under "Background jobs".

### Scheduled jobs

A job is one bot's later or recurring work: a skill plus a message, on a schedule such as
`every 30m`, `daily 09:00`, `mon,thu 09:00` or `once 2026-12-01 09:00` (local time; at
most every 5 minutes; a `once` job turns itself off after its run). A bot sets one up
itself when the work needs it (`osctl propose-job`), in your private threads only: the
job is saved when its reply ends, and the thread shows a receipt with what was saved
(bot, skill, schedule, next run, mode, cap and message), Undo and Pause. Changes and
deletions work the same way. A job's own run never sets up more jobs.

Each job has a mode:

- **Approval** (default): reads and drafts. Every save waits for your click in the job's
  thread.
- **Auto**: creates and updates in Leo Bot by itself (memos, briefs, posts, plans) and
  changes files in the workspace's edit folders, for what the job's message asks. Deletes, replacing a whole document (intake, technical,
  roadmap, section lists, a brief spec or voice) and bot memory still wait for your click, and so does every app
  action that is not a read (posting, sending email). A run that loads a skill other than
  the job's own (`osctl get-skill`) saves nothing more by itself: the rest of that run
  waits for your click, and the next run starts clean.
- **Report only**: reads and writes a summary. No cards, no saves, no app writes.

The Team screen lists every job: where and when it runs, how the last run went (with a
link to its thread), and its mode, cap, on/off and delete. Schedule, message and skill
change through a bot's card.

- A job may have a check: a read of one of the bot's connected apps (say, unread mail)
  that the server runs before each run. The model runs only when the answer changed since
  the last run that went through; otherwise the run ends `quiet` and costs nothing.
- Each job has a notepad the bot keeps itself (`osctl job-note`): every run's prompt
  carries it, so work spread over days (sent 12 of 30, who replied) continues where it
  stopped.
- Each run is its own thread in the bot's DM, titled `Job: <name>`. It gets the same tools
  as a private chat turn: web search, the bot's connected apps, its linked folders.
- A job's message was written by a bot, so a site named in it is never opened: a job
  can search the web, not fetch a page.
- A job may have its own cap in dollars per 24 hours (none by default: leave it empty
  or set 0), and always counts against your workspace and owner caps. Generation an Auto job runs (`generate-*`, `revise-post`)
  counts against it too, and is checked against what the job's run has spent up to that
  moment. A job runs only on an engine that reports cost or token usage (Claude Code,
  Codex, API providers; a model with no list price is counted at the costliest one, so a
  cap is never exceeded), and consults only teammates whose engine does too. ACP agents
  report neither, so they run no jobs. At most 50 jobs exist at once.
- One job runs at a time. A run that was missed while Leo Bot was off runs once when it
  starts again, never once per missed slot. A run goes on as long as it keeps working (no
  step limit); it is stopped after 10 minutes with no activity, teammates included, and
  after 6 hours in any case.
- Each morning from 08:00 a digest thread in HQ's #general lists what ran, what it cost,
  which memory lists were tidied and what waits for your approval. It costs nothing.

### Spending caps

Every reply records its tokens, model, skill and estimated cost; the Team screen shows
where spend went (by model, skill, engine and day). Three caps, in dollars per rolling
24 hours, set as environment variables on the machine running Leo Bot:

- `LEOBOT_PERSON_DAILY_CAP`: each invited person's turns. Default 5.
- `LEOBOT_WORKSPACE_DAILY_CAP`: every turn in one workspace. Default off.
- `LEOBOT_OWNER_DAILY_CAP`: your own turns, across workspaces. Default off.

A cap is checked before every engine run, teammates and follow-ups included: a capped
message never starts the engine and says when room frees up. At 80% of a cap you get
one heads-up a day. Costs are estimates from list prices when an engine reports none;
engines that report neither cost nor a known model are counted on the Team screen but
not toward caps. Turns on your own Mac (runners) never count. `0` turns a cap off.
Generation runs (plans, briefs, revisions) and memory tidy-ups count too, and show on the
Team screen under "Background jobs". The same view shows why each model was picked and how
much of each engine's input was read from cache.

### Long threads

A thread does not grow without limit. Each reply records how large its prompt was. Once
a reply passes `LEOBOT_COMPACT_TOKENS` (default 60000; `0` turns it off), the next turn
starts a fresh session that gets the thread's visible messages as a recap, plus the
project snapshot and the bot's card, instead of the whole transcript. On an API engine the
same limit trims the history it sends, at a point that stays put across turns so the
provider's prompt cache keeps working. Anthropic API calls mark the system prompt and the
newest message for caching.

### How bots pick up work

Tags are optional. An untagged message goes to the bot that spoke last in the thread, else
the channel's lead, and that bot decides what it needs:

- **Skills.** Its card lists its own skills in one line each. When the task needs one it
  loads it (`osctl get-skill --name <skill>`); the skill then stays active for the thread
  and the next turn runs on the stronger model. A tagged `/skill` still gives it the skill
  up front.
- **Saves.** Any message in your private threads can lead to a save: the bot says what it
  will make and where, and ends with one `APPROVAL:` line. Nothing is saved until you
  approve the card, and Approve goes back to the bot that asked. A skill a bot loads by
  itself never unlocks folder edits. A thread invited people can see still needs a tagged skill for that.
  Folder edits still need a tagged message.
- **Web.** Web search is on in your private threads and scheduled jobs until a conversation
  holds data from your connected apps: from then on no search or page runs in it, checked at
  the moment the tool would run, so a query can never carry that data out. A page is fetched
  only from a site you typed (a job's message grants none). Only `/web` takes the stronger
  model. Codex searches its index on those turns and never fetches a page live.
- **Outside content.** Mail, pages, app data and a guest's message are someone else's words.
  Once a conversation holds any, nothing a bot does goes out on its own: its app actions wait
  for one Approve all, and an Auto job saves only through an approval card. A bot acts alone
  only on what you wrote.
- **Saved from outside content.** Whatever a bot saves after reading outside content (a memo,
  a memory, a doc) keeps that label, so it cannot pass someone else's words to a later, clean
  conversation. While a workspace holds a labeled file you have not trusted, its Auto jobs
  save only through your click and a Full access shell sends nothing off the machine. Approvals lists each one: read the whole file, then Trust it, or delete it.
  Trust covers the version you read; a later change is labeled again.
- **Apps (sending on your behalf).** Each bot gets each app per workspace as Read, Asks
  every time (anything that is not a read waits for your click), or Sends without asking
  (it sends, posts and messages on its own, also after reading mail or pages; deletes,
  payments, ads, databases, code, sharing and paid lookups still wait). Each action it runs shows in Approvals as done or
  failed. Actions held together in one call wait for one Approve all. A bot that needs an app,
  an account or more access asks you for it.
- **Working alone.** Given a job, a bot checks memory, files, apps and the web first, and
  asks only when it is blocked.
- **Teammates.** A bot ends its reply with `HANDOFF @handle: <task>` to pass the work on:
  that teammate answers next in the same message with its own skills and apps, and the
  thread's next message goes to it. One handoff per message. `CONSULT @handle: <question>`
  still asks a teammate for a read-only view first. The card lists which teammate holds
  which connected app.
- **Short replies.** Bots answer first and skip filler in chat (Settings, Bots, Short
  replies; on by default). Saved work (memos, posts, briefs, memory) stays in full prose.

### Dashboard (macOS app)

The app's launcher binary is not committed: build it from source once after cloning. With the repo hooks enabled (`git config core.hooksPath .githooks`), every pull or checkout that changes the app's source rebuilds it automatically.

```bash
scripts/build-app.sh   # needs Xcode Command Line Tools
open LeoBot.app
```

Without the app, run `leobot` (or `python3 dashboard/server.py` from a clone) and open the login link it prints (it contains a per-run token; a plain `http://127.0.0.1:8765` shows a login-required page).

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
| `score-experiments` | Scheduled job: reads each experiment metric source, judges it, holds a verdict card to approve |
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

Leo Bot is source-available under the [PolyForm Shield License 1.0.0](LICENSE).

- Allowed: using it for anything, including running your own business;
  modifying it; sharing copies and forks (keep the `LICENSE` file with them).
- Not allowed: offering a product or service that competes with Leo Bot, such as
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
scripts/export-public.sh /tmp/LeoBot-public.zip
```

The exporter refuses a dirty tree and uses `git archive`, so ignored local data
cannot enter the archive.

### Automatic commit protection

Enable the repository's privacy hooks once per clone:

```bash
git config core.hooksPath .githooks
```

The pre-commit hook scans the exact staged snapshot and blocks every file under
`projects/`, `portfolio/`, and other Leo Bot data directories—even if it was added
with `git add -f`. It also blocks databases, logs, credentials, secrets, personal
paths, private project terms, and unapproved network hosts. The pre-push hook
checks the complete committed history again before anything leaves the machine.
