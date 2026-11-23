---
name: hiring-watch
kind: loop
bot: people
schedule: daily 09:00
description: Loop template. Each morning, when the candidate list changed, flag candidates waiting on a step and draft the next message for the owner. Reports only.
---

# Hiring watch loop

A turn loop for @people on one candidate list. Set it up with `--skill hiring-watch --schedule "daily 09:00" --check-own list --check-id ls<N>`. Starts in report mode.

## Cadence
Daily, 09:00, with the list check: quiet days cost nothing.

## Acts when
The candidate list changed, or a candidate waited more than 3 working days for a step. Otherwise one line, then end this run (the loop keeps going).

## Purpose
No candidate waits without an answer, and every one goes through the same steps.

## Skills used
`interview-plan` (load it with `get-skill --name interview-plan`).

## Body
1. Read the notepad and the candidate list (`get-list`).
2. Flag candidates waiting on the owner (a reply, a scorecard, a decision).
3. Draft the next message for each, for the owner to send.
4. A candidate flagged again adds one try. At 3 tries, list them under High priority: decide or let them go with a kind note.
5. Write the notepad, then the report.

## Self-check
Every candidate gets the same steps. No note mentions a protected trait. Candidate details stay out of memory; refer to rows by id.

## State
The notepad keeps:
- Last run: the date.
- High priority: candidates at 3 tries, waiting on a decision.
- Watch list: waiting candidates by row id, each with try count (0 to 3).
- Recent noise: rows the owner paused.
- Run log: one line per run, last 10 runs.
Drop candidates hired or closed.

## Stop or expiry
Stops when the owner's stop condition holds (for example: the role is filled). It expires after the owner's loop days and asks to be renewed.

## Output
At most 6 lines: who waits, for what, since when, and the drafted next message. Nothing is sent.

## Post-run critique
End the run log line with one note: a candidate the rules missed, or a draft the owner rewrote.

## Cost profile
A model run only on days the list changed. Set `--cap 0.3`.

## Success metrics
Every candidate hears back within 3 working days. No candidate reaches 3 tries.

## Handoff points
- Decisions go to the owner.
- A pay question goes to @finance as a suggestion in the report. The loop never messages a candidate.
