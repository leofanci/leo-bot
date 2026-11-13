# Leo Bot CLAUDE.md

## PUBLIC REPO: zero usage data (absolute)

Repo is public. NEVER write real venture/profile/product/channel names, post content, or any user data into tracked files: code, tests, docs, comments, examples, placeholders, commit messages. Real data lives only in gitignored `projects/`. Fixtures and examples use generic slugs only: `demo`, `acme`, `profile-a`, `profile-b`. Enforced by `tests/test_no_usage_data.py`, run it before every commit.


## Private folder

`private/` is gitignored: owner-only notes and unreleased work. Tracked files, commit messages and PRs never describe what is in it.


## Git workflow

Feature branches and worktrees are fine (superpowers workflow OK). When a job is done and approved, merge it into `main`. Keep `main` the single source of truth: don't leave finished work stranded on side branches, and delete branches/worktrees once merged.


## Response style

Respond like smart caveman. Cut all filler, keep technical substance.
- Drop articles (a, an, the), filler (just, really, basically, actually).
- Drop pleasantries (sure, certainly, happy to).
- No hedging. Fragments fine. Short synonyms.
- Technical terms stay exact. Code blocks unchanged.
- Pattern: [thing] [action] [reason]. [next step].
- Never em dash (—). Use comma, period, or colon instead.
- Memos/chat: blank line between paragraphs; bullets as `- ` lines, one per line.

## After taking action

Never repeat, quote, or paste back output from actions you completed — created posts, plans, briefs, ventures, calendars, validation results, osctl output, etc. User reads it directly. One-sentence confirmation max.

<!-- CANONICAL: dashboard/ai_rules.py — keep in sync -->
## Content writes (mandatory — chat and terminal)

Never write content files directly. All mutations go through osctl:

| What | Command |
|------|---------|
| Venture intake file | `create-intake --project <slug>` or `update-intake --project <slug>` (--text or stdin) |
| Technical doc | `create-technical --project <slug>` or `update-technical --project <slug>` (--text or stdin) |
| Tab subsections (per project) | `get-subsections --project <slug>` · `update-subsections --project <slug> --doc intake/technical/roadmap --subsections "A,B,C"` · `add-subsection --project <slug> --doc technical --title "Prompt"` · `update-validation-tab --project <slug> --subsections "Stage & evidence,Market"` |
| Strategy memo | `create-memo --project <slug> --type <memo-type> [--summary] [--recommendation]` · `update-memo --project <slug> --type <memo-type> --version <N> [--summary] [--recommendation]` (patches that version in place — does not create a new one) · `delete-memo --project <slug> --type <memo-type> --version <N>` |
| Experiment | `create-experiment --project <slug> --assumption "..."` |
| Product scaffold | `create-product --project <slug> --slug <prod-slug> [--name] [--type]` |
| Roadmap feature | `add-feature --product <prod-slug> --title "..." [--section Next]` · `update-feature --product <prod-slug> --id <feature-id> [--title] [--why] [--section] [--priority]` · `delete-feature --product <prod-slug> --id <feature-id>` |
| Roadmap (full replace) | `update-roadmap --product <prod-slug>` (--text or stdin) |
| Experiment patch/delete | `update-experiment --project <slug> --stem <stem> [--success-criteria] [--kill-criteria]` · `delete-experiment --project <slug> --stem <stem>` |
| Experiment metric (owner) | `set-metric --project <slug> --stem <stem> --app <app> --action <READ_SLUG> --path <a.b> [--args JSON] [--window-days N] [--clear]` |
| Post slide row | `add-slide --id <post-id> --overlay "<text>"` |
| Profile name/topic | `update-profile --slug <slug> [--name] [--topic]` |
| Brief spec (one of several) | `create-brief-spec` / `update-brief-spec --profile <slug> [--id br2] [--platforms ...] --text "..."` |
| Voice (one of several) | `create-voice` / `update-voice --profile <slug> [--id vc2] [--platforms ...] --text "..."` |
| Post brief (NL) | `update-brief --id <post-id> --instruction "<user's words>"` |
| Post brief (auto) | `generate-brief --id <post-id>` |
| Revise slot/draft | `revise-post --id <post-id> --instruction "..."` |
| Bot memory (per project, or HQ) | `remember --bot <bot-id> [--project <slug>] [--type fact|preference|decision] --text "..."` · `recall --bot <bot-id> --query "..."` (older entries; workspace and scope come from the thread) · `get-memory --bot <bot-id> [--project <slug>]` |
| Scheduled job (only when asked; no APPROVAL: line for it) | `propose-job --bot <bot-id> --skill <skill> --name "..." --message "..." --schedule "every 6h" or "daily 09:00" or "mon,thu 09:00" --cap <usd per day> [--mode approval|auto|report] [--project <slug>]` (the owner gets a card in this thread; nothing is saved until they approve) · change one: `propose-job --job <id> [--schedule ...] [--cap ...] [--mode ...] [--message ...] [--skill ...] [--name ...]` · delete one: `propose-job --job <id> --delete` · `list-jobs` |
| Calendar activity (one calendar across workspaces) | `create-activity --entity <slug> --title "..." --date YYYY-MM-DD [--time HH:MM --time-end HH:MM]` (timed ones may not overlap: `get-busy --from <date>` first) |
| Content calendar | `generate-plan --profile <slug> --period "YYYY-MM-DD to YYYY-MM-DD"` (unscheduled by default — add `--dates` ONLY if the user explicitly asks posts to be dated/scheduled now) |

Banned: `set-brief`, `patch-brief`, direct edits to `briefs/*.json`, `brief-specs/*.md`, or `voices/*.md`.

A profile can have several brief-specs and several voices (`br1`, `br2`, ... / `vc1`, `vc2`, ...), each optionally tagged `platforms:` (`all` or a comma list from that profile's channels). Tag is informational only: selection between multiple is always manual, never auto-matched. Default is br1/vc1 when nothing is specified; a post remembers which pair produced it. Path: `projects/<project>/profiles/<profile>/brief-specs/br{N}.md` and `.../voices/vc{N}.md`. Changing one does not rewrite existing post briefs.
To edit one from NL: `get-brief-spec --id <id>` first, then MINIMAL EDIT — keep every line the user did not touch verbatim, change only what they asked, never rewrite/reword the rest.

**Write gate (same as posts):** routing + short summary in chat → user approves tab placement → osctl writes content once (review in dashboard, not chat). Never paste full memo/intake/roadmap text in chat and again in osctl. Full rules: `dashboard/ai_rules.py` WRITE_GATE + TAB_ROUTING.

"Fill tabs" / left panel = six project sections per TAB_ROUTING in `ai_rules.py`. Product roadmap features go under **Next**, not **Shipped**, until checked off. Never `create-experiment`, `create-activity`, `create-milestone`, or `generate-plan` for AI-invented plans without explicit user approval.

Artifact shapes: `core/project_schemas.py` + `core/subsections.py` (also `GET /api/schemas`). Six tabs are fixed; **subsections** (`##` headings inside intake/technical/roadmap) are per project in `projects/<slug>/subsections.json`. Manual dashboard, chat, and osctl share same normalize path.

## IDs

Composed IDs: full parent→child cascade. `pr1` `pr1.sec04.mm1` `pr1.sec03.ex1` `pr1.sec05.pd1.ft1` `pr1.pf1.sec00.po3` `pr1.pf1.sec00.po3.br1.fd02`. Artifacts nest under UI tab/entity — never skip levels. No fd under pr/pf/ch for form metadata. `get-id-catalog`, `resolve-id`. Source: `core/ids.py`. Dashboard loads tab layout from `/api/schemas` (not hardcoded in `os-ids.js`).