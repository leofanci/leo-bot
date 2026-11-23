---
name: runway-check
kind: loop
bot: finance
schedule: mon 09:00
description: Loop template. Each Monday, compare cash, burn and runway with the plan and report only when the runway moved or a decision date is near. Reports only.
---

# Runway check loop

A time loop for @finance. Set it up with `--skill runway-check --schedule "mon 09:00"` and a message naming where the owner keeps the numbers (a runway canvas or list). Starts in report mode.

## Cadence
Weekly, Monday 09:00. Cash moves over weeks; a daily check repeats itself.

## Acts when
The owner's numbers changed since the last run, or the decision date is within 8 weeks. Otherwise one line, then end this run (the loop keeps going).

## Purpose
The owner never learns about a cash problem late.

## Skills used
`runway-plan` and `unit-economics` (load with `get-skill`).

## Body
1. Read the notepad and the owner's numbers (cash, revenue collected, costs by line).
2. Recompute net burn, runway months and the decision date.
3. Compare with last run and the plan: what moved and why (a cost line, revenue).
4. For each cost line that rose without a note, add one try asking the owner about it. At 3 tries, list it under High priority.
5. Write the notepad, then the report.

## Self-check
Every number comes from the owner's data or is marked as a guess. Never estimate the cash balance. Not tax or legal advice: say what to confirm with an accountant.

## State
The notepad keeps:
- Last run: the date.
- High priority: the decision date when within 8 weeks, and items at 3 tries.
- Watch list: rising cost lines, each with its try count (0 to 3).
- Recent noise: changes the owner explained.
- Run log: one line per run with runway months, last 12 runs.
Drop explained items.

## Stop or expiry
Stops when the owner's stop condition holds (for example: break-even for 3 months). It expires after the owner's loop days and asks to be renewed.

## Output
At most 6 lines: runway months and its change, the decision date, what moved, and one move to consider. Nothing is paid, moved or cancelled.

## Post-run critique
End the run log line with one note: a number that was missing, or a flag the owner already knew.

## Cost profile
One model run a week, reading one page. Set `--cap 0.3`.

## Success metrics
The decision date is known at least 8 weeks ahead. The owner updates the numbers at least twice a month.

## Handoff points
- Items at 3 tries and the decision date go to the owner.
- A price question goes to @cmo as a suggestion in the report.
