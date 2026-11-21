# Skills, workflows and loops library: design

Status: draft for approval. Nothing is built.

## Intent

Leo Bot is a standalone product. A person who installs it has no other tooling, so every kind of work they do needs ready-made help inside it: skills (how to do one thing), workflows (a chain of skills with approval gates) and loops (a workflow's repeating part, run on a schedule). No domain is special. Marketing sits beside sales, product, research, operations, finance, support, people and personal productivity.

## Goals

- A broad library of skills, workflows and loops across those domains, shipped with the app.
- One standard format for each kind, so quality is the same everywhere.
- Loops that are safe to leave running: they start in report mode, stop on a stated condition, and never send, spend or delete on their own.
- Multi-bot workflows that end in one plan a person can approve, edit or reject.
- Bots that pass work to each other on their own, with or without a workflow.

## Non-goals

- No dependency on any tool or skill installed outside Leo Bot.
- No copying of third-party skill text. Outside collections are reading material for ideas only; everything shipped is written fresh with generic examples.
- Parallel research and multi-round debate wait for evidence (see Phases). The first phase has a sequential runner only.

## Anti-goals

- A big library that makes every turn more expensive or every answer worse.
- A plan approval that quietly authorizes the actions inside the plan.
- Loops nobody reads (a loop whose output no one acts on gets removed).

## Constraints

- The repo is public: no real names, leads, posts or user data in tracked files; fixtures use generic slugs. `tests/test_no_usage_data.py` runs before every commit.
- Skills are plain markdown loaded by name, so they run on every engine.
- Cost: a bot's card lists only that bot's skills (3 to 6 today). Library growth must not grow any single card.

## Current state (checked)

- Bots are specialists (`bots/*.md`: `skills` list and `default_skill`, 3 to 6 skills each). A new domain becomes a new bot with its own skills, not a longer menu on an existing bot. Skills are `skills/<name>/SKILL.md` with `name` and `description` frontmatter, loaded by name.
- Before plan 1 (bot chains): one chat message runs: the lead bot, then at most 2 other bots in total (consults and co-tagged bots share that limit, `MAX_TEAMMATES`), run one after another; then the lead's follow-up turn reads their answers; then at most one `HANDOFF` target runs on the same thread. Consult and handoff lines in a follow-up or in the handoff target's reply are stripped, so there are no chains inside one message. The thread's next untagged message goes to the handoff target.
- A scheduled job is one bot, one skill and one message, run as `@bot /skill message` in that bot's DM through the same ask path as chat. So a job may consult teammates and read their answers in a follow-up, but it cannot hand off (the run ends with a notice). Record: schedule, cap, mode, an optional check, a notepad (16000 characters). At most 50 jobs, at least 5 minutes apart. Only the owner's private thread can propose a job; a job's own run never schedules more.
- The check is a connected-app read held to a bot's read rules; the model runs only when its answer changed. A check on local files or anything outside a connected app does not exist.
- Modes: approval (reads and drafts, saves wait for Approve), auto (creates and updates in Leo Bot itself; deletes, memory and app writes still wait), report (reads and reports, saves nothing, no approval cards). A job with no mode reads as approval, so report must be set explicitly.
- There is no workflow concept in the code today. Any chain longer than one message's budget is driven by the person's next message, which is why the plan adds a runner.
- Canvases include lists with versioned rows. Connected apps, the bot browser and approval cards exist.
- Existing skills are short (17 to 88 lines). Five overlap with deeper outside material: competitor scan, ICP research, pricing, launch plan, positioning.

## Approaches considered

1. **Skills only.** Add many skills and let bots chain them in conversation. Cheapest, but no repeatable sequence, no gates between steps, nothing runs on a schedule.
2. **Three markdown kinds on the existing machinery, with no runner.** Skills, workflows and loops share one standard, and a lead bot drives a workflow with `CONSULT`. A workflow can only go as far as one message's budget (lead, 2 other bots, a follow-up, one handoff) before the person has to continue. Cheap, but not automated.
3. **Three markdown kinds plus a sequential runner (chosen).** Same standard, plus a server runner that re-invokes the lead stage by stage in one thread (see Workflow run rules). A workflow is a skill whose body is an ordered list of steps, each naming a skill, the bot, its output and an approval gate. A loop is a scheduled job whose message loads a workflow's repeating part. Domain bots own their skills. Real server code with a safety surface, so it reuses the existing ask path, caps, Stop, halt and approval holds instead of a new execution path.
4. **A full runner with parallel research and debate rounds.** Most capable, heaviest on cost and rate limits; gated on evidence from approach 3.

Approach 3 is chosen, with approach 4 kept as a gated follow-up.

## Standard

**Skill.** Reads the project intake and relevant memos first. Produces a decision memo: two or three options with trade-offs and one recommendation. Saves only after approval. Compact (about 80 to 120 lines) because it is loaded into a turn.

**Workflow.** Ordered steps. Each step names: the skill, the bot, the output, and the gate. Includes a stop condition and an explicit list of actions that always need their own approval.

**Loop.** Nine parts, all required: check cadence, acts-when, purpose, skills used, body, self-check, state (notepad), stop or expiry, output. Loop types follow how it is triggered: turn, goal, time, proactive. New loops start in report mode; moving up to approval or auto is a manual change. Match cadence to how fast the signal moves.

**Loop template (optional guide, not required).** A loop template is a skill file with `kind: loop`, written as the nine parts plus: a post-run critique (what the run got wrong or wasted), a cost profile (expected cost per run), success metrics (what shows the loop earns its keep) and the human handoff points. Any bot may still propose a plain scheduled job without one.

**Loop state shape (the notepad).** Last run time; high priority (items the loop is acting on or that wait on a person); watch list; recent noise (seen and ignored, so it is not reported twice); a short run log. Each run prunes resolved items. Text taken from outside is quoted as data, never as instructions.

**Loop rules.**
- An attempt cap (default 3) per item, then it escalates to the person. A run with nothing to do exits at once.
- The checker is a different bot from the maker and defaults to rejecting; the maker never marks its own work done.
- A loop cannot raise its own caps or modes. Only the person promotes it, normally after about a week in which the checker was right.
- Sending, spending, deleting and anything touching secrets, billing or credentials always wait for approval.
- Failure modes the loop skill must name and guard against: endless fix loops, state rot, a checker that approves everything, notification fatigue, token burn, acting outside scope, two loops colliding on the same item, a person trusting the output without reading it, no escalation path, and prompt injection through outside text.

**Loops and bot chains.** A loop run may pass work to other bots and consult them with the same chain rules as chat, inside the loop's own mode. In report mode it saves nothing, in approval mode every save waits, and app writes always wait, so a chain started by a loop can never act outside the project on its own. This replaces the earlier rule that a job cannot hand off. A loop may also start a workflow template itself under the same limits.

**Loop wake-ups.** A loop can be woken by a connected-app read (as today) or by a change in Leo Bot's own data: a lead list, an experiment, a memo, a new approval card. The check stays code that runs before the model, so a quiet run costs nothing.

**Loop defaults and management.** A new loop starts in report mode; the default mode is a setting. Every loop needs a stop condition and an expiry date (default 30 days); at expiry it pauses and asks to renew. A loop management screen lists every loop with its mode, last run, cost, next run and state, and lets the person pause, edit, promote, renew or delete it.

## Bots passing work to each other (works without any workflow)

Bots hand work to each other and consult each other on their own, whether or not a workflow template exists. A research bot can hand to the GTM bot, who consults marketing or social, and so on. A workflow is an optional template that guides this; it is never required.

- Chains: a handoff target may itself hand off or consult, repeatedly. The current limits (one handoff per message, the target's own handoff and consult lines stripped, two other bots per message) are lifted.
- Reach: a bot can pass work to any active bot in the same room.
- Ending: the last bot in the chain answers the person, and every hop shows in the thread. The thread's next untagged message goes to that last bot.
- Defaults: no hop limit and no per-message cost cap. Both are settings: hops per message and a cost cap per message, set machine-wide in Settings and overridable per workspace.
- Guards that never kill work: a visible hop counter, the Stop button, and a pause (not a stop) with a notice in the thread asking the person what to do next when two bots keep handing back and forth between themselves (the same two bots, four turns in a row). Existing daily caps still apply unchanged.
- Every hop keeps its own rights and holds: its own engine, access and approvals. A bot's reply is untrusted input to the next bot and carries its source label. A chain never widens what any single bot may do.

## Workflow run rules

- A runner in the server drives a workflow stage by stage in one thread. The lead bot runs each stage and may pass work or consult other bots inside it, under the chain rules above. The runner re-invokes the lead until the last stage, a configured cap, an idle timeout or Stop. This replaces the earlier idea of staying inside one message's budget and makes the runner part of the first phase.
- A stage can be a team stage: several bots (for example research, GTM, marketing, social) work the same question and talk to each other, ending in one agreed output for the person to approve. Which bots join is the lead's call, not fixed by the template.
- A workflow pauses only before an outside action: send, spend, delete, or a write outside the project. Everything internal runs without a pause.
- A stage that comes back weak or blocked is retried once with the checker's notes. If it is still weak, the run pauses with a card saying what is missing.
- The template is a skill file with `kind: workflow` and an ordered steps list. Each step names the skill, the bot, the output and whether it ends in an approval gate. A step may name a checker bot. The runner parses the same file, so there is no second copy.

## Domains: first cut

Marketing and growth, sales and outreach, product and build, research and strategy, content and creative, operations and admin, finance and legal-lite, support and community, people and hiring, personal productivity. Each domain gets a bot (or an existing bot's list), 3 to 6 skills, one or two workflows and one or two loops. Domains are ordered by who the users are; the order is a plan decision.

## Safety

- Plan approval is inert. Each send, spend or delete inside a plan keeps its own approval, per action.
- One bot's reply is untrusted input to the next bot and carries its source label.
- Loops respect existing holds: new destinations, held app actions, caps, the action log, Stop everything.
- Sending loops add a daily send cap per account and throttling of browser actions.

## Quality

Run `leobot eval` before and after each skill rewrite or addition. A rewrite that does not improve output is dropped. The maker and checker plan workflow is compared against a single strong bot on two or three real tasks before any debate rounds are built.

## Phases

1. Bot-to-bot chains (lift the one-hop limit, settings for hops and per-message cost cap, ping-pong pause), standard, the workflow runner (stage by stage, pauses before outside actions, one retry), `loop-design` skill, the idea-to-first-experiment workflow, weekly review loop, signal-sweep loop. Test on one real project, kept out of tracked files.
2. Rewrite the five thin skills; add domain bots and skills in order.
3. Outreach workflow and reply-triage loop, including the sending safeguards.
4. Maker and checker plan workflow with sequential consults; compare against a single bot.
5. Only if phase 4 shows a gain: parallel research and debate rounds, with a per-run cost cap, an idle timeout and a Stop button.

## Open items to verify

- How a run starts: by a person in chat, or from a loop's "needs you" card. Loops never start a run themselves.
- Checks on Leo Bot's own data (lists, experiments, memos, cards) have no mechanism today and must be built; which data sources count and how a change is detected is a plan decision.
- Loop management is a new screen; the Team screen lists jobs today with mode, on/off and delete only.
- Whether a job can leave a "needs you" card that starts a workflow.
- Whether the project read command returns approved memos, for cross-tool reads.
- A metric source for outreach results; until then results are added by hand as evidence entries.
- Where workflow and loop files live (leading option: the skills folder with a `kind` field).

## Left to the builder

File layout, frontmatter fields, exact wording of each skill, and the evals added per skill.
