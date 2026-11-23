---
name: daily-plan
kind: loop
bot: ops
schedule: mon,tue,wed,thu,fri 08:00
description: Loop template. Each weekday morning, turn the calendar and the week's outcomes into a short plan for the day, and flag what slipped. Reports only.
---

# Daily plan loop

A time loop for @ops. Set it up with `--skill daily-plan --schedule "mon,tue,wed,thu,fri 08:00"`. Starts in report mode: it suggests the day's blocks and never books them.

## Cadence
Weekdays, 08:00, before the day starts.

## Acts when
Every scheduled run. A day with nothing booked and no open outcome: one line, then end this run (the loop keeps going).

## Purpose
The owner starts each day knowing the one thing that matters most and when it gets done.

## Skills used
`weekly-plan` (load it with `get-skill --name weekly-plan`).

## Body
1. Read the notepad (this week's outcomes) and today's calendar (`get-busy --from <today>`).
2. Pick the day's top task toward the week's outcomes and find a free block for it.
3. List at most 3 other tasks that fit the free time.
4. A task carried over from a past day adds one try. At 3 tries, stop carrying it: list it under High priority for the owner to drop, delegate or schedule.
5. Write the notepad, then the report.

## Self-check
Suggested blocks never overlap a busy slot. Planned hours fit the free hours. Never book, move or cancel anything.

## State
The notepad keeps:
- Last run: the date.
- High priority: tasks at 3 tries, waiting on the owner.
- Watch list: this week's outcomes, and carried tasks with try counts (0 to 3).
- Recent noise: tasks the owner said to drop.
- Run log: one line per run, last 10 runs.
On Mondays, drop last week's done outcomes.

## Stop or expiry
Stops when the owner's stop condition holds (for example: a holiday period). It expires after the owner's loop days and asks to be renewed.

## Output
At most 6 lines: the top task with its block, up to 3 other tasks, and what slipped.

## Post-run critique
End the run log line with one note: a block that did not fit, or a task carried too often.

## Cost profile
One small model run a weekday. Set `--cap 0.2`.

## Success metrics
The top task gets done on most days. Carried tasks rarely reach 3 tries.

## Handoff points
- Tasks at 3 tries go to the owner.
- A task another bot can draft (research, a brief) is named as a suggestion. The loop never books time itself.
