# Bots pull their own capabilities

Date: 2026-11-13. Status: draft for approval.

## Intent

Today a regex on the user's message decides, per turn, what a bot gets: which skill body is
injected (`_explicit_skill`, `_suggest_skill`), whether web search exists (`_needs_web`),
whether code context is injected (`_needs_app_context`), which bot answers
(`_picked_by_wording`), and whether a save is allowed at all (`_WRITE_TURN_RE` tag gate).
When the wording misses, the bot never learns the capability exists, and the owner has to
learn tags to get work done.

The model is capable of deciding what it needs. This change shows each bot a short menu of
what it can reach and lets it pull the rest on demand, at a cost no higher than today for
plain questions.

## Goals

- A bot sees, in its card, a one-line menu of its own skills and how to load one.
- A bot loads a skill body, code context or a web search only when it decides to.
- A bot passes work to a teammate by name, with full rights, instead of a keyword router.
- An untagged message can lead to a save; the owner's approval stays the gate.
- Scheduled jobs run without a cost cap by default; a cap is optional per job.
- Chat replies are short by default (a reply style), saving output tokens.

## Non-goals

- Linking profile channels to connected accounts (separate spec).
- Names-first state snapshot (separate spec).
- A model-based router call before each message.
- Claude Code's own Skill tool (engine-specific, and it re-enables skill discovery).
- Untagged folder edits: edit folders still need a tagged turn (no approval card guards a
  code edit).

## Anti-goals

- Plain questions cost more than today.
- A thread an invited person can see gains any capability it does not have today.
- An Auto job saves work its approved message did not ask for.
- Persisted content (memos, posts, briefs, intake) written in the short reply style.

## Constraints

- Engine-neutral: everything goes through `osctl` and the prompt, so Claude Code, Codex,
  ACP and API engines behave alike.
- Existing safety gates stay: shared/locked threads, full-disk engines never get apps, no
  web search or network osctl after an app read, WebFetch only to hosts the user typed,
  job modes enforced in `osctl`.
- Run `leobot eval` before and after the routing change.

## Approaches

1. **Bot pulls what it needs (chosen).** Menu in the card, bodies and code context via
   `osctl`, handoff via a reply line. Pays only for what is used; works on every engine.
2. **Claude Code's Skill tool limited to the bot's skills.** Less code, but Claude Code only:
   other engines stay regex-routed, and it turns skill discovery back on, which is the token
   cost the current design removed. Rejected.
3. **Cheap router model call before each message.** One extra call on every message and the
   bot still cannot change course mid-task. Rejected on cost.

## Design

### 1. Skill menu and `get-skill`

- `_bot_block` adds, on a fresh card only, `Your skills (load one with osctl get-skill
  <name> when the task needs it):` and one line per skill from `team.skills_of(bot)`:
  name plus the SKILL.md `description`, truncated to one line. Teammates' skills stay as
  names only in the existing roster lines.
- New read-only `osctl get-skill --name <skill>`: prints the body via `_load_skill_body`
  (rail stubs included). Unknown name: error listing valid names. Added to
  `_READ_ONLY_CMDS`, `_LOCAL_CMDS`, `_SHARED_ALLOWED`, `_AUTO_ALLOWED`.
- When a turn loads a skill, the session marks that skill active (`_last_skill`,
  `_skill_explicit`), so the next turn takes the stronger model through the existing
  active-skill-thread rule. How the server learns of the load (tool-call stream, or a
  marker `osctl` writes keyed by the turn token) is left to the builder; it must work for
  every engine kind or degrade to "no escalation" without breaking the turn.
- Removed: the `## Skill hint (not active)` block, `_suggest_skill` as a router, the
  `suggest` plumbing. A tagged `/skill` still injects its body up front, as today.

### 2. Untagged saves

- The tag gate (`_WRITE_TURN_RE` early return, `_tag_gate_message`) is removed.
- On a private turn, an untagged bot may propose a save: summary plus one `APPROVAL:` line,
  as tagged turns do. The owner's approval runs the save, on the stronger model (the skill
  is active by then).
- `may_write` becomes true on every private, non-job turn; shared threads keep today's rule.
- `may_edit` (folder edits) is unchanged: still needs a tag.
- RAIL text that teaches tagging (`Untagged write → blocked`, "tag who should do it") is
  rewritten to describe the menu, `get-skill`, and propose-then-approve.

### 3. Code context on demand

- New read-only `osctl code-context --query "<question>"`: calls `tokensave_bridge.context`
  on the workspace's first indexed folder for this turn (same folder rule as today), with
  `include_code=True`, output capped as today.
- Not allowed in shared/locked threads (same reason as today: a guest's question must not
  steer a read of the owner's code). Added to `_READ_ONLY_CMDS`, `_LOCAL_CMDS`,
  `_AUTO_ALLOWED`; not to `_SHARED_ALLOWED`.
- The per-turn injection driven by `_needs_app_context` is removed. Explicit `/tokensave`
  keeps working as today.
- The card names the command only when the workspace has an indexed folder.

### 4. Web search on private turns

- `WebSearch` is mounted on every private turn, except in a thread that read an app
  (existing rule). Shared threads unchanged.
- `WebFetch` keeps the typed-hosts rule.
- Having web search no longer escalates the model; explicit `/web` still does.

### 5. Handoff by name

- A bot may end a reply with `HANDOFF @handle: <task>`. The server strips the line and runs
  that teammate next in the same message, as the speaker, with the rights a tagged turn
  would give it (not read-only), within the room's members.
- One handoff per message, no chains. The handed-to bot opens with one line that it is
  taking it over (existing `handoff_from` text).
- The handed-to bot becomes `_last_bot`, so the thread's next untagged message goes to it.
- `CONSULT @handle:` stays, read-only, as today.
- Removed: `_picked_by_wording` and the `Route.picked` path. Untagged messages go to the
  thread's last speaker, else the room lead; the bot hands off if the work is not its own.
- Roster lines in the card add the workspace's connected apps and accounts the bot may use
  (from `_apps_accounts_note`), so a bot knows who holds which app.

### 6. Scheduled jobs

- `cap` becomes optional: empty or `0` means no cap. `propose-job --cap` optional; the job
  card and Jobs screen show "no cap" and let the owner set one.
- `spend._caps` adds the job cap only when set.
- Auto boundary, enforced in `osctl`: in an Auto run, `get-skill` for a skill other than
  the job's own turns the rest of that run into Approval mode (writes refused, APPROVAL
  line). State lives in a per-run marker keyed by the run's job token.
- Unmetered engines are still refused for jobs (the cost report needs a figure).

### 7. Short reply style

- A Leo Bot-owned rule block in `CHAT_RAIL`: lead with the answer, no preamble or recap, no
  filler, fragments allowed, code and ids verbatim, full sentences for warnings and for
  questions to the user. Written in Leo Bot's own words; no third-party text.
- Applies to chat replies only. Text passed to `osctl` (memos, posts, briefs, intake,
  memory) is written normally; the rule says so.
- Global setting "Short replies", on by default, in Settings. Off removes the block.
- Engines start clean (`--setting-sources ""`, Codex skill flags), so the owner's own CLI
  plugins never reach bots; this setting is the way to get the style.

### 8. ACP engines start clean

- Check Gemini CLI and OpenCode for a flag or env that skips user extensions and context
  files; pass it when one exists. Where none exists, the Engines screen says the CLI's own
  extensions load on its turns.

## Left to the builder

- How the server detects a skill load per engine (section 1).
- Exact menu wording and truncation length.
- Test updates for removed routing (`_picked_by_wording`, tag gate, skill hint).
- Settings storage for "Short replies" (follow existing global settings).
