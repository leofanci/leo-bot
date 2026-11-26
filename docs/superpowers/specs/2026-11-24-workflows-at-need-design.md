# Workflows built at need: design

Status: draft, reviewed. Item 1 of the orchestration work after phase 5 (`2026-11-24-team-steps-design.md`).

## Intent

The workflow runner (stages, consult/team/debate, checker, gates, one retry, idle pause, cost cap) only runs from a `kind: workflow` skill file that the owner starts with `/<name>`. A general agent plans for any request instead of waiting for a file someone wrote for that use case. With this change a bot drafts the steps for a request itself, the owner approves them on one card, and the same runner runs them. Shipped workflow files stay as optional examples; saving a drafted plan as a file is optional.

Measure of success: accuracy minus cost. A drafted plan should beat a single bot on multi-area requests, match a hand-written template where one exists, and cost nothing extra on simple requests.

## Goals

- Any bot can draft a plan: steps with bot, output, gate, optional skill, optional checker, optional consult/team/debate.
- Two triggers:
  - The owner asks: `/plan <request>`, or plain words asking for a plan.
  - The bot proposes on its own, only when the request needs two or more teammates' areas. A single-bot request that ends in an approval save does not count. Otherwise it answers as today.
- An unprompted proposal replaces the answer: the reply is the plan plus one line on why a plan fits. On Reject the bot answers alone. The work is never paid for twice.
- One plan card per draft: Approve starts the run; Reject takes a short note and the drafting bot redrafts.
- The run's steps live in the run record, so a drafted plan never needs a file in `skills/`.
- Optional "Save as template": writes a workflow file from a plan into the data folder. The owner's Save click (with the name) is the approval.
- An eval that compares drafted plans against a single bot and against shipped templates.

## Non-goals

- Editing steps on the card (swap a bot, toggle a gate, drop a step). Comes later, once real use shows which edits are common.
- A separate planner model call before every request.
- Plans that create bots, change bots or schedule jobs (item 2, bots managing bots).
- Turns running at the same time (item 3).

## Anti-goals

- Cost on simple requests: a request that one bot can answer well gets one turn, no plan.
- Plan approval as a blanket approval: it starts the run only. Every gated step still raises its own card, and sends, spending and deletes stay always-ask.
- A plan that names bots outside the team, switched-off bots, or a checker equal to its maker: rejected before the card shows.
- Per-use-case code: the trigger rule and plan format are generic instructions, never branches for a kind of request.

## The plan block

The drafting bot ends its reply with a fenced block in the same step-line syntax as `## Steps` in a workflow file:

```
PLAN: <short title>
brief: <the request, written to stand alone: goal, audience, dates, constraints>
1. bot: research | output: three competitor prices with sources | gate: no
2. skill: launch-plan | bot: gtm | output: the launch plan with dates and owners | gate: approval | checker: product | team: yes
```

- `brief:` is required. The drafting bot writes it so a step in a fresh thread has the full context. It is shown on the card and stored as the run's brief, passed to steps as quoted data like today's brief.
- `skill:` is optional in a drafted plan. A step with no skill runs from the bot's own instructions plus the step's output line. Workflow files keep `skill:` required.
- All other fields, limits and checks are today's: at most 12 steps, 3 consults, debate 0 to 2, checker differs from maker, `debate` needs `team: yes` or a `consult:` list, a skill must exist and must not be a workflow or a loop.
- The server cuts the block out of the reply, as it already does for `START:`, `APPROVAL:` and `LOOP DONE:` lines. It parses it with the same parser (relaxed only on `skill:`) and checks it against the team with `workflows.problems`.
- A block that does not hold up shows a notice on the message: "The plan did not hold up: <reason>". No card.
- A reply may carry one plan. Only the bot that owns the turn may propose one. A consulted teammate's plan block is dropped, as its approval lines are today.
- A turn that is already a workflow step never proposes a plan: its block is dropped.

## The card

- The plan shows on the message as a card: title, the brief, and one line per step (`@bot`, skill if any, output, gate, checker, consult/team/debate).
- The plan card is not an `approvals` card: those block workflow ticks in the thread. It is stored on the message, like the loop's Start offer.
- Approve sends only the message reference. The server reads the plan from the stored message, never steps from the browser. The card works once: after Approve it shows "Started" with a link to the run's thread, and a second Approve does nothing.
- **Approve** starts a run with these steps in a private thread of the owner's. If the plan was proposed in the owner's private thread, the run starts in that thread. If it was proposed elsewhere (a shared thread, a DM), it starts in a new private thread in the workspace's general room, as the loop "Start" button does today.
- **Reject** asks for an optional short note. The note goes to the drafting bot as the owner's next message in that thread, with the original request still in the thread. The bot then redrafts if the note asks for a different plan, or answers alone without a plan.
- Only the owner sees Approve and Reject. A plan in a shared thread is visible to others as text only.
- On Approve the server checks the plan against the team again: a bot switched off since the draft blocks the start with the same message as today's `/<workflow>` start.

## The run

- The run record gains the plan's steps and title. A run started from a plan reads its steps from the record; a run started from a file reads the file, as today.
- The run's name shown in labels and the bar is the plan's title, without the `/`. A run marks that it came from a plan, so its title is never looked up as a workflow file name.
- A drafted plan has no body text. Its steps get the brief and step instructions only.
- A step with no skill routes to its bot with no skill loaded; `problems` skips the skill check for it.
- Everything after start is the current runner: same phases, same read-only rules, same retry, idle pause, cost cap, Stop workflow, Continue.

## Triggers in the bot's instructions

- Every owner chat turn and job run that is not a workflow step gets a short rule (never an invited person's turn): when to propose a plan (the owner asks for one, or the request needs two or more teammates' areas), that an unprompted plan replaces the answer, the plan format, the active bots it may name, and the skills each has.
- `/plan` is reserved: no skill or workflow may take that name.
- `/plan <request>` routes like any message to the addressed or routed bot, with the rule that it must answer with a plan.
- Scheduled jobs keep their `START: /<workflow>` offer. A job's bot may also propose a plan; it then shows as a card the owner approves later, and never starts by itself.

## Save as template

- A started plan's card offers "Save as template". The owner gives a name; the server writes `<data>/team/workflows/<name>.md` (`~/.leobot/data` by default) with `kind: workflow`, the brief as description, and the steps. Only the owner sees the button and it writes nothing else, so the click is the approval: no second card.
- Never `skills/`: it is the tracked code folder and updates replace it.
- `workflows.load` and `workflows.names` read both `skills/` and the data folder. A shipped name wins on a clash, and saving under a shipped or existing name is refused.
- Saved templates may keep steps with no skill: `skill:` is optional in data-folder workflow files, required in shipped ones.
- A saved template starts with `/<name> <brief>` like a shipped one.

## Eval

- `leobot eval --compare` gains a drafted-plan arm: the case's request goes to the lead bot with `/plan`, the plan is approved automatically in the eval, and the run's output is judged like any arm.
- Cases: the existing `acme` launch-plan cases, where the drafted plan runs against the single bot and against the shipped template; and new `acme` cases that no shipped template covers (for example a pricing change, a hiring plan, a vendor pick), where the drafted plan runs against the single bot.
- The summary prints, per pair, wins, mean score, cost and cost ratio, as for phase 5.
- A second check counts how often a bot proposes a plan unprompted on a set of simple requests. The target is zero.

## Safety

- Plan blocks from teammates, tool output, web pages or app reads never become cards. Only the owning bot's own reply text counts, and it is parsed like any other control line.
- The brief stored in the run is the one the drafting bot wrote and the owner approved on the card. Steps get it as quoted data labelled as drafted by a bot, never as instructions. A plan drafted after outside content (a web page, app data) carries that mark into its run, so the run keeps the same limits wherever it starts.
- The plan names only bots on the team. Each step runs with that bot's own access and caps, never wider.
- Shared threads never run a plan.

## Approaches considered

1. **Plan block in the reply (chosen).** Reuses the step-line parser, the control-line cutting, the card, and the runner. No extra model call. Works in read-only turns. Risk: a malformed block, handled with a notice and a redraft.
2. An osctl command `propose-plan --steps-json`, saved when the reply ends, like `propose-job`. Stricter format, but a tool round trip, osctl access needed in the turn, and a second syntax for what workflow files already express.
3. A separate planner call before every bot reply. Clean split, but an extra model call on every request, which works against accuracy minus cost.

## Decided

- Triggers: owner asks, or the bot proposes when the request needs two or more teammates' areas.
- An unprompted plan replaces the answer; on Reject the bot answers alone.
- Card: approve, or reject with a note. Editing comes later.
- Format: plan block in the reply, step-line syntax, `skill:` optional.

## Calls made during review (builder may revisit)

- Saved templates live in the data folder, read by the workflow loader next to `skills/`.
- The drafting bot writes the run's brief in the plan block.
- Approve reads the plan server-side and works once.

## Left to the builder

Exact instruction wording, the run record's field names, the plan card's layout, how the eval auto-approves, the new compare cases' content (generic `acme` fixtures only), and where in the turn path the block is cut.
