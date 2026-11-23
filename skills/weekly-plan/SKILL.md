---
name: weekly-plan
description: Plan the owner's week - the few outcomes that matter, time blocks on the calendar around real commitments, and what to drop. Use when the user asks to plan the week, the day, or how to fit everything in.
---

# Weekly Plan

## Read first
- The calendar: `get-busy --from <monday>` for what is already booked.
- Priorities: the weekly review, open experiments, deals, deadlines and the owner's
  stated goals. Bot memory for preferences (focus hours, days off).
- The owner's available hours from the intake. Missing: ask once.

## Method
1. **Pick 3 outcomes** for the week, each a result, not an activity ("5 discovery
   calls held", not "do outreach"). Tie each to a priority or deadline.
2. **Size each outcome** in hours, with a 30% buffer.
3. **Check the fit:** free hours minus meetings. If outcomes do not fit, cut or move
   one now, out loud, instead of planning an impossible week.
4. **Block time:** deep work in the owner's best hours, small tasks batched, no block
   longer than 2 hours without a break. Never overlap a booked slot.
5. **List what to drop or delegate** to a bot (a draft, a research task).
6. **Name the risks:** a deadline that depends on someone else, a heavy day.

## Options
When the week is over-full, two or three plans: protect the top outcome and cut the
rest, spread all three thinner, or move a meeting. Trade-offs in one line each.

## Recommendation
The 3 outcomes, the day-by-day blocks, and the one thing to say no to.

## Before you reply
- Planned hours fit the free hours with a buffer.
- No block overlaps a busy slot.
- Each outcome is a result that can be checked on Friday.

## Write gate
Show the plan in chat. Add blocks with `create-activity --entity <slug> --title "..." --date YYYY-MM-DD --time HH:MM --time-end HH:MM` only after the user approves, after checking `get-busy`.
