# Harness upgrade: cost ledger, searchable memory, scheduled jobs, leaner turns, evals

## Why

The harness is solid on safety: bots run through one write tool (osctl), workspaces are isolated,
connected-app actions wait on an approval card, and a cheap model is the default. Five things
hold it back:

- **Memory is small and blind.** Each bot keeps one markdown list per workspace (plus a team list
  for shared threads), capped at 40 lines of 300 characters. The last 20 lines go into the bot
  card when a session starts or the speaker changes. Old facts fall off in order, not by
  importance, and a bot cannot look up anything outside that tail.
- **Nothing runs without a message.** Skills such as weekly review exist, but only a person's
  message starts them. There is no scheduler, trigger or persisted job state.
- **Spend is partly visible.** Each reply stores `tokens` (one summed number) and `cost`,
  `/api/costs` sums them per bot over 30 days, and `LEOBOT_PERSON_DAILY_CAP` (default $5) caps an
  invited person per rolling 24 hours. There is no split by model, skill or cache use, no cap for
  the owner or a workspace, and no record of why a model was picked. Engines differ: Claude Code
  and the API engine report a cost, Codex reports tokens only, ACP reports neither.
- **The API engine pays full price for every turn.** It resends the thread history (up to 400k
  characters) on each turn with no prompt-cache breakpoints. Invited people always run on it.
- **Long threads and fixed routing cost tokens and accuracy.** A resumed session grows every turn.
  Untagged messages go to the last bot that spoke. Consults run one at a time, at most two.

Purpose test for every item below: does it make the OS more organized, faster, more accurate,
or cheaper in tokens? An item that fails the test is out.

## Goals

- Every turn's cost is recorded with enough detail to find waste, and caps stop runaway spend.
- Bots find older facts on demand and carry fewer memory lines in each bot card.
- Recurring work runs on a schedule and waits for approval, with no new autonomy.
- Context per turn stays flat as a thread grows, on every engine.
- Agent behavior (routing, output shape) is tested without spending tokens.

## Non-goals

- A server database (Postgres, pgvector), a graph database, or a hosted memory service. Markdown
  stays the source of truth, SQLite stays the derived index. Revisit vectors only through the gate
  in Phase 2.
- An autonomous planner agent or a framework such as LangChain. Fixed workflows live in skills.
- Bots that write without approval, or that run as invited people on the owner's login.
- Any change to workspace isolation, the private/shared memory split, or linked-folder rules.

## Phase 1: cost ledger and caps

**Record.** Each engine's `parse_event` (Claude Code, Codex, API, ACP) currently returns one
summed `tokens` number. Keep the parts instead, mapped per engine:

- Claude Code and the Anthropic API: `input_tokens`, `cache_creation_input_tokens`,
  `cache_read_input_tokens`, `output_tokens`.
- Codex: `input_tokens` minus `cached_input_tokens` as `in`, `cached_input_tokens` as
  `cache_read`, `output_tokens` as `out`.
- OpenAI-compatible APIs: `prompt_tokens` minus `prompt_tokens_details.cached_tokens` as `in`,
  the cached part as `cache_read`, `completion_tokens` as `out`.
- ACP: no usage; the reply is marked `metered: false`.

Each assistant reply's metadata gains: `engine`, `model`, `skill`, `usage` as
`{in, cache_write, cache_read, out}` (`tokens` stays the summed number, since the UI and old
threads read it), `cost`, `reason` (a short code from `_pick_model` such as `explicit-read`,
`explicit-skill`, `web`, `edit-turn`, `skill-thread`, `default`) and `job` (Phase 3, else empty).
When an engine reports no cost but the model is in the price table (`PRICES` in the API engine,
moved to a shared module), the cost is estimated from the token parts. Old replies without
`usage` still sum as today.

On the owner's Claude Code login, `total_cost_usd` is a list-price estimate, not a bill. The
dashboard labels it "estimated"; caps act on the same estimate.

**Report.** `/api/costs` adds groupings by engine, model, skill and day, the cache-read share of
input, and a count of unmetered turns. The dashboard shows them next to the existing per-bot view.

**Cap.** Checked in the server before every engine run in a message (lead, each teammate, the
follow-up), not only once per message. The per-person check stays where it is.

- `LEOBOT_WORKSPACE_DAILY_CAP` and `LEOBOT_OWNER_DAILY_CAP`, rolling 24 hours like the person cap.
  0 or unset means no cap.
- A job cap, set per job (Phase 3), checked before each job turn.
- A run refused for a cap spends nothing and says which cap and when it frees up. A teammate run
  refused mid-message is skipped; the lead's reply stays.
- A notice at 80% of a cap, at most once per cap per 24 hours (last notice time kept with the
  ledger).
- Unmetered turns (ACP, an unpriced model) do not count against caps. The cost view shows them.

A reply on a runner shows no cost (the person's own login) and does not count against any cap.

**Done when:** the numbers above show in the dashboard from saved threads, a capped run is refused
before the engine starts (tested with a fake engine), a teammate run is refused when the lead
crossed the cap, and nothing changes for threads with no cap set.

## Phase 2: memory

**Entries.** Same files, same locations, same private/shared split. One line per entry:
`- 2026-11-11 [fact] text`, with type one of `fact`, `preference`, `decision`.
`osctl remember` gains `--type` (default `fact`). A line without a type reads as `fact`, so
existing files work unchanged. The 300-character limit stays on the text: one fact per line.

**Index.** An FTS5 table in the existing SQLite index, one row per entry with columns workspace,
scope (`private` or `shared`), bot, date, type, text. It is derived: `index.py` rebuilds it from
the files, and `remember`/`forget` replace that one file's rows in the same call, so recall is
never stale between rebuilds. If the local SQLite lacks FTS5, recall falls back to a plain
substring scan of the file.

**Cap.** The 40-line cap becomes 400 entries per file. At 300 entries consolidation runs (below).
If a file still reaches 400, the oldest `fact` entries go first; `decision` and `preference`
entries are never dropped by the cap.

**Recall.** `osctl recall --bot <id> --query "..." [--limit 8]`, added to the read-only and
shared-allowed command sets. The workspace and scope come from the turn's server-side
environment (`LEOBOT_WORKSPACE`, the shared flag), never from arguments, so a shared thread only
reaches the team memory and a company workspace never reaches another's. Output is entries only,
capped at 800 characters in total.

**Injection.** The bot card keeps its current timing: it carries memory only when a session
starts or the speaker changes, never on every turn. The card gets pinned entries (`decision` and
`preference`, newest first, up to 10 lines) plus the top 5 recall hits for the message that opened
the card, instead of the last 20 lines. Later turns use `osctl recall` on demand. Target: a
smaller memory block per card at equal or better hit rate.

**Consolidation.** When a file passes 300 entries, one background run on the fast tier of the
engine `registry.pick` gives that bot merges near-duplicates and drops superseded facts. It runs
after the turn that crossed the line, never inside a shared thread's turn, counts against the
owner and workspace caps, and is recorded in the ledger with `job: memory-consolidate`. It writes
the previous file to `<bot>.md.bak` beside it (one generation) and lists the change in the next
HQ digest (Phase 3). An output that drops a `decision` or `preference` entry is rejected and the
file stays as it was.

**Optional.** `[[wikilinks]]` between entries so the files read as a graph in any markdown viewer.
No new dependency; the app does not parse them.

**Vector gate.** Add local embeddings (a SQLite extension) only if, on the eval set from Phase 5,
keyword recall misses more than 20% of known-answer queries. Not before.

**Done when:** an old fact beyond the injected set is found by `recall` in a test, a shared thread
cannot recall private entries (tested), a workspace cannot recall another's (tested), existing
untyped files load unchanged, and the injected memory block is smaller than the current tail on
the same fixture.

## Phase 3: scheduled jobs

**Job record**, stored in `team/jobs.json` in the data folder (spans workspaces like
`team/approvals.json`, so company bots cannot read it): `id`, `workspace`, `bot`, `skill`,
`message`, `schedule` (interval or weekly time, local timezone), `cap` (required, above 0),
`enabled`, `last_run`, `last_status`. Created and edited only through osctl (`create-job`,
`update-job`, `delete-job`, `list-jobs`). A bot that wants a job proposes it with an `APPROVAL:`
line; the job commands are not in the shared-allowed set and are refused in read-only mode, so a
job turn can never create or widen a job.

**Runner.** A tick thread inside the server process checks due jobs once a minute and runs each
through the normal ask path, on the engine `registry.pick` gives that workspace and bot. A job
must run on a metered engine (Phase 1); creating one on an unmetered engine is refused. A job turn
is read-only plus proposals, using what exists today: osctl runs with `LEOBOT_READONLY` (the mode
consulted teammates use), the turn gets no edit folders, and a proposed write ends the reply with
an `APPROVAL:` line. Approving it in the job's thread hands the write to a normal owner turn.
Connected-app actions keep their existing approval card. Missed runs (server off) run once on
wake, never backfilled. Concurrency: one job turn at a time across all workspaces. The run stops
on its cap, on a failed engine turn, or after `JOB_TURN_SECONDS` (new, default 1800).

**State.** `last_run` and status are written before and after each run, so a restart resumes at the
next due time and never double-runs a job.

**Output.** Each run is a thread named for the job, tagged in cost records with its `job` id. A
daily digest message in HQ lists what ran, what it cost, what waits for approval and any memory
consolidations, built from the ledger and the approvals file with no model call.

**Triggers.** Webhook and connected-app event triggers are a later step that reuses the same job
record with `trigger` in place of `schedule`. Out of scope until the schedule path ships.

**Done when:** a job with a fake engine runs on schedule in a test, is not run twice across a
restart, stops at its cap, refuses a non-read osctl command, and leaves writes as pending
approvals.

**As built** (plan `docs/superpowers/plans/2026-11-11-scheduled-jobs.md`):

- Schedule text is `every <N>m|h|d` (at least an hour) or `<days> HH:MM`, days `daily` or a
  list of `mon`..`sun`. A daily time is a weekly time on every day.
- The job cap is dollars per rolling 24 hours for that job, like the other caps, checked before
  each run of a job turn. The record also has a `name`; the thread is titled `Job: <name>`.
- `skill` is required and must be one of the bot's skills: the run is the tagged message
  `@<bot> /<skill> <message>` in the bot's DM, so its `APPROVAL:` line gets buttons and an
  approval in the thread continues as a normal owner turn.
- A job turn runs in its own fresh session, never the owner's open thread. A job whose bot,
  skill or workspace went away, or whose engine stopped reporting cost, is `refused` with no
  engine call and stays enabled.
- Statuses: `running`, `done`, `waiting` (an approval is pending), `capped`, `failed`,
  `timeout`, `refused`, `interrupted` (a stop cut the run off; it waits for its next due time).
- The digest posts once a day from 08:00 local, covering the time since the last one (at most
  7 days), as a thread in HQ's #general. Nothing to report means no thread.
- `generate.py` runs on `complete_metered`, checks the owner and workspace caps first and
  writes a ledger row with `job: "generate"`. osctl does not know who asked, so a run from a
  shared thread counts as the owner's.

## Phase 4: leaner, more accurate turns

Each item ships only if the Phase 1 data shows it matters.

- **API engine caching.** Anthropic calls mark the stable prefix (system prompt) and the last
  history message with `cache_control`, so repeat turns read the history from cache. Measure the
  cache-read share before and after.
- **Thread compaction.** When the last turn's context (`in + cache_write + cache_read`) passes a
  threshold (default 60k tokens), start a fresh session with the existing recap of visible
  messages, which costs no model call. Add a model-written summary only if Phase 5 shows the recap
  loses needed context. On the API engine the same threshold trims the resent history.
- **Prompt order.** Stable blocks first (rules, bot definition), changing blocks last (snapshot,
  recalled memory, message). Measure the cache-read share before and after; keep only if it rises.
- **Parallel consults.** Run the up-to-two teammate answers concurrently; Stop kills every run in
  flight (today it tracks one). Add a per-message token budget in place of the hard no-chains
  rule; depth stays 1 unless the budget allows more.
- **Routing.** Untagged messages keep going to the last bot, but a message that `_suggest_skill`
  matches clearly to another bot's skill goes to that bot, with the choice shown in the reply. The
  turn stays untagged: the write gate and the cheap-model rule apply as today.
- **Engine failure.** One retry on a transient failure. A fallback to another engine happens only
  if the owner listed one in Settings, never silently from the owner's login to a paid key, and
  only to an engine allowed for that thread (shared-thread and Mac-only rules unchanged). The reply
  names the engine that answered. Otherwise a plain error.
- **Model picker.** Keep the regex rules, log every decision with its reason (Phase 1), and review
  the log before changing any rule.

## Phase 5: behavior evals

- **Offline (default, free).** A fake engine plus fixed prompts per skill check routing (which
  bot, which skill, which model tier and reason) and output shape (an `APPROVAL:` line when a
  write is proposed, no full memo text in chat). Runs in the normal test suite.
- **Live (manual).** A small set of known-answer prompts run against a real engine on request,
  cost shown, results compared to the last run. Used for the Phase 2 vector gate and the Phase 4
  compaction check.
- Fixtures use generic slugs only (`demo`, `acme`, `profile-a`).

**Shape.** An `evals/` package beside `core/` and `dashboard/`, cases as JSON in `evals/cases/`.

- `turns`: each case is one message through the server's real ask path on a throwaway data
  folder, with a scripted engine whose replies the case gives. It compares the outcome: bot,
  skill, hand-off, model tier and reason, web, tag gate, edit folders, approval card, consults,
  follow-up, reactions, and text in the reply or in the lead and teammate prompts (the memory a
  card carries). Case options set a thread under way, a DM, a shared thread, a connected-app
  grant, a linked folder and memory files. A case marked `gap` states wanted behavior that fails
  today: reported, not failed, and the suite fails once it passes so the mark gets dropped. Phase 4
  routing starts as two open gaps.
- `recall`: known-answer queries against one memory file through `memory.recall`, split into
  keyword and paraphrase queries, on the FTS5 index and on the scan fallback. Its miss rate
  decides the vector gate.
- `live`: a few prompts graded on the real reply (a known answer named, length, bullet count,
  teammates consulted) on top of the turn checks. It runs in a child process with a throwaway
  home, so a bot's osctl writes never reach real data; the engine settings are copied in for the
  run. Runs are saved to `<home>/evals/` (last 20, owner-only) and compared with the previous one.
- Entry points: `python -m evals` and `leobot eval` (offline), `--live` (asks before spending
  unless `--yes`, `--engine`, `--case`).

**First results.** The offline cases found two defects, fixed with this phase: a tagged read
(`/positioning what is...`) always took the strong model, since the read check looked at the
start of the message where the tag sits; and recall matched on words such as "the" and "what",
so unrelated facts filled the card. Recall now drops stop words (unless a query has nothing
else), the index uses the porter stemmer (an index built with the old tokenizer is rebuilt on
first use), and the scan fallback matches rough stems. Keyword recall went from 21/25 to 25/25;
the miss rate from 20% to 13% on FTS5 and from 33% to 17% on the scan. Paraphrases still miss
4 of 5, which keeps the vector gate closed (13% is under 20%). The live paraphrase case (a fact
the card does not carry) passed on the first live run. Add paraphrase queries as real misses
show up; reopen the gate if the rate passes 20%.

**Done when:** `python -m evals` passes every non-gap case and stays under the recall gate in the
suite, a declined live run sends nothing, and one live run on the default engine passes and is
saved for the next comparison.

## Order and dependencies

1. Phase 1, because every later choice needs its numbers and caps make the rest safe to enable.
2. Phase 3 (needs the job cap and metered-engine check from Phase 1) and Phase 2 (independent),
   in either order. Phase 2 consolidation needs Phase 1 caps.
3. Phase 5, offline part first, in parallel with the above; live part before the vector gate.
4. Phase 4 items, one at a time, each justified by Phase 1 data. API engine caching first.

## Risks

- **Recall leaks across scope.** Mitigation: scope and workspace come from the server only;
  tests for shared-vs-private and workspace-vs-workspace.
- **Consolidation loses a fact.** Mitigation: one backup generation, pinned types protected, never
  in shared turns, and a digest line naming the change.
- **Jobs spend while unattended.** Mitigation: required per-job cap, workspace and owner caps,
  metered engines only, one job turn at a time, writes only as proposals.
- **Estimated cost differs from the bill.** Claude Code on a subscription and priced models are
  estimates. Mitigation: label them, and keep token counts beside cost.
- **Compaction drops needed context.** Mitigation: keep recent messages verbatim and measure
  rework (next-turn corrections) before making it default.
- **Cache claims are unverified.** The caching, prompt-order and compaction savings are expected,
  not measured. Phase 1 exists to check them.

## Open questions for the reviewer

- Is a 400-entry file cap plus consolidation at 300 right, or should old entries move to an
  archive file the index still reads?
- Is the vector-gate threshold (20% misses) reasonable, or should the eval set decide it?
  (Phase 5 measured 13% on the recall set, 4 of 5 paraphrases missed: kept at 20% overall.)
- Should the owner and workspace caps default to a nonzero value on a fresh install, as the
  person cap ($5) already does?

## Testing

Unit tests per phase as listed under each "Done when". `tests/test_no_usage_data.py` runs before
every commit. No test calls a real engine.
