# Team steps: the lead picks its advisors, blind advice, debate rounds: design

Status: approved. Phase 5 of `2026-11-21-skills-workflows-loops-design.md`.

## Intent

Phase 4 (fixed consultants, one after another) gave better plans than a single bot in 3 of 3 cases at about 4x the cost. Phase 5 aims at the most accuracy for the money: only the bots a task needs take part, their first advice is independent, and they argue only where they disagree.

## Goals

- The step's bot (the lead) decides whether it handles the step alone or needs teammates, and which ones.
- First advice is blind: each consultant sees the brief and the draft, never another consultant's advice.
- A consultant may bring in one more teammate whose area the draft touches.
- Debate rounds: consultants agree with the revised output or object with one specific problem; the lead answers only objections.
- An idle timeout for workflow runs (the per-run cost cap and Stop workflow already exist).
- An eval that tells whether this beats phase 4, not only a single bot.

## Non-goals

- Turns running at the same time. Blind advice comes from fresh sessions, not concurrency; speed is not the goal.
- A free-form team stage with no turn limit.
- Changing phase 4 behaviour: a step with a fixed `consult:` list and no new fields runs as today.

## Anti-goals

- Cost that grows with no bound: at most 3 consultants per step, at most 2 debate rounds, and a round asks only the bots that objected last time.
- Advice turns that act: every consult, debate and draft turn stays read-only and raises no card.

## Step line fields (new)

- `team: yes|no` (default `no`). With `yes`, the lead picks consultants itself. A `consult:` list on such a step is a hint shown to the lead, not a fixed list, and is not checked by `problems` (a switched-off hint is just left out).
- `debate: 0|1|2` (default `0`): the most debate rounds. Needs `team: yes` or a `consult:` list.

## How a team step runs

1. **Stage (one turn, read-only).** The lead is told: if it can do the step well alone, write the full output; if a teammate's area is needed, write a draft under 400 words and end with one `CONSULT @handle: <question>` line per teammate. Pool: active bots in the room other than the lead and the checker, at most 3. The existing chat consult path runs each one once in its own fresh read-only session; in a workflow team step the consultant's prompt carries the brief and the lead's draft (capped), and asks for advice under 150 words. A consultant may end with `ALSO @handle: <why>`; that bot runs next with the same material if the step still has room under 3 and it is in the pool. The lead's follow-up turn writes the whole output in full with the advice (same revise rules as phase 4: stands alone, dates and numbers agree, fits the team). The runner records who was consulted.
2. **Debate (one turn per consultant, read-only).** With `debate` > 0 and at least one consultant: each consultant in the round replies `AGREE` or `OBJECT: <the one problem that matters most, and the fix>`. A reply with neither counts as agree (advice never blocks).
3. **Answer (lead, read-only).** When any consultant objected: the lead writes the whole output again, answering each objection (fix it, or say in one line why it stands). If rounds are left, the next debate round asks only the bots that objected. When the last round ends with objections still open, they go to the checker's instructions as open points.
4. **Check, save:** as in phase 4. A WEAK check retries the answer turn once with the notes. A gated step proposes its save in the `save` phase.

A fixed-`consult:` step with `debate` > 0 runs the debate after its revise turn with its fixed consultants; its revise turn then saves nothing and the save comes in the `save` phase.

## Idle timeout

A run still marked running whose record has not changed for 30 minutes reads as paused, "No step ran for 30 minutes." Nothing runs without a tick, so this only stops a forgotten run from looking live; Continue picks it up.

## Eval

- A compare case may name a `baseline` workflow. The run then has three arms: the workflow, the baseline workflow and the single bot.
- Each pair (workflow vs single, workflow vs baseline) is judged twice, with the plan order swapped. A pair's winner is the arm both calls picked; otherwise a tie. Scores are the mean of the two calls.
- Six `acme` cases. The new workflow `team-launch-debate` (team step, debate 2, checker product, hints research and cmo) against baseline `team-launch-plan` (phase 4).
- The summary prints, per pair: wins, mean score, total cost, cost ratio, and points gained per extra dollar. `Gain over baseline: yes` when the workflow wins against the baseline in more than half the cases.
- The owner decides what ships on from the numbers. Whatever does not earn its cost stays off (the new fields default to off).

## Safety

Unchanged rules: consultants' replies are untrusted input with source labels; no advice or debate turn can save, act or raise a card; Stop workflow, the cost cap and daily caps apply to every turn; consultants keep their own read limits, never wider than the lead's.

## Results (6 `acme` cases, Claude Code engine)

- `team-launch-debate` against a single bot: better in 6/6, cost 2.2x.
- `team-launch-debate` against phase 4 (`team-launch-plan`): better in 5/6, none worse, mean score 22.0 vs 18.3 out of 25, at 0.7x its cost. On these cases the lead chose to work alone every time; the gain comes from a full first output plus the checker.
- Forced consults plus 2 debate rounds against `team-launch-debate`: better in 3/6, worse in 2, mean 20.5 vs 19.9, at 2.2x the cost. Debate ran (mostly AGREE in round 1) but did not earn its cost here, so the lead keeps the choice and the shipped template keeps `debate: 2` only for the cases where it does consult.

## Approaches considered

1. **Blind advice from fresh sessions plus capped debate (chosen).** Phase 4's turn count when nobody objects; extra cost only where there is disagreement.
2. Same, with concurrent turns. Same accuracy, faster, new concurrency code in the turn path for no accuracy gain.
3. Free team stage. Possibly most accurate, unbounded cost, hard to measure.

## Left to the builder

Names of run fields, exact instruction wording, the cap on the draft quoted to consultants, and how the summary lays out its lines.
