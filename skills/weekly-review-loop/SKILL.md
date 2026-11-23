---
name: weekly-review-loop
kind: loop
bot: cos
schedule: mon 09:00
description: Loop template. Every Monday, review the portfolio, compare with last week's priorities, and report what moved, what stalled and the week's top 3.
---

# Weekly review loop

A time loop for @cos. Set it up with `--skill weekly-review-loop --schedule "mon 09:00"`.

## Cadence
Weekly, Monday 09:00. Portfolio health moves over days, not hours; faster runs repeat themselves.

## Acts when
Every scheduled run. If nothing changed since the last run (no new memo, experiment result, evidence entry or card), report that in one line and end this run (the loop keeps going).

## Purpose
Keep the owner's week pointed at the three priorities that matter most, and say plainly what is stalled.

## Skills used
`weekly-review` (load it with `get-skill --name weekly-review`).

## Body
1. Read the notepad: last week's priorities and their try counts.
2. Run the weekly review: snapshot, flags, the week's plan.
3. For each of last week's priorities, mark it moved, done or stalled.
4. A priority stalled again adds one try. At 3 tries, stop carrying it: move it to High priority as a decision for the owner (commit time or cut it).
5. Write the new notepad, then the report.

## Self-check
Before reporting, check that each flag names its project and the fact behind it (a date, a missing result). Drop any flag you cannot back with a fact. Never propose more priorities than the intake hours allow.

## State
The notepad keeps:
- Last run: the date.
- High priority: priorities that hit 3 tries, waiting on the owner's decision.
- Watch list: this week's 3 priorities, each with its try count (0 to 3).
- Recent noise: flags the owner already saw and let stand.
- Run log: one line per run, last 8 runs.
Drop done priorities and decisions the owner made.

## Stop or expiry
Stops when the owner's stop condition holds (for example: the quarter ends). It expires after the owner's loop days and asks to be renewed.

## Output
At most 12 lines:
- What moved since last week.
- Stalled items, with try counts.
- This week's top 3.
- Decisions waiting on the owner.
No full memo text.

## Post-run critique
End the notepad's run log line with one note on what this run wasted or missed (a flag repeated from last week, a priority without a goal).

## Cost profile
One model run a week, reading the index. Expected well under the daily cap. Set `--cap 0.5`.

## Success metrics
The owner acts on at least one priority a week. Stalled items get a decision within 3 weeks. Reports stay under 12 lines.

## Handoff points
- Decisions at 3 tries go to the owner.
- A stale memo or a new experiment goes to the owning bot as a suggestion in the report. The loop never writes one.
