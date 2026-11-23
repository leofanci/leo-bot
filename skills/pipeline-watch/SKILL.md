---
name: pipeline-watch
kind: loop
bot: sales
schedule: daily 09:00
description: Loop template. Each morning, when the deals list changed or a deal went stale, flag stuck deals and give one dated next step for each. Reports only.
---

# Pipeline watch loop

A turn loop for @sales on one deals list. Set it up with `--skill pipeline-watch --schedule "daily 09:00" --check-own list --check-id ls<N>` and a message naming the list. Starts in report mode.

## Cadence
Daily, 09:00, with the list check: a day with no change still runs once a week to catch deals going stale.

## Acts when
The list changed since the last run, or a deal crossed the stale line (no contact in 14 days, or the owner's number). Otherwise one line, then stop.

## Purpose
No deal dies of silence: every live deal has a next step and a date.

## Skills used
`deal-review` (load it with `get-skill --name deal-review`).

## Body
1. Read the notepad and the deals list (`get-list`).
2. Flag deals that are stale, have no next step, or are missing budget, decision maker or timing.
3. For each flagged deal, write one dated next step and, where it helps, a draft message.
4. A deal flagged again adds one try. At 3 tries, stop chasing it: list it under High priority for the owner to close or revive.
5. Write the notepad, then the report.

## Self-check
Each flag names the deal and the fact behind it (last contact date, missing field). Drop a flag you cannot back. Never change a row or send a message.

## State
The notepad keeps:
- Last run: the date.
- High priority: deals at 3 tries, waiting on the owner's call.
- Watch list: flagged deals, each with its try count (0 to 3).
- Recent noise: deals the owner said to leave alone.
- Run log: one line per run, last 10 runs.
Drop deals that were won, lost or moved forward.

## Stop or expiry
Stops when the owner's stop condition holds (for example: the list is archived). It expires after the owner's loop days and asks to be renewed.

## Output
At most 8 lines, most urgent first: the deal, why it is flagged, the next step and its date. Drafts are for the owner to send.

## Post-run critique
End the run log line with one note: a flag repeated with no new fact, or a deal the rules missed.

## Cost profile
A model run only on days the list changed, plus one a week. Set `--cap 0.5`.

## Success metrics
Stale deals drop week over week. Every live deal has a dated next step. The owner acts on at least two flags a week.

## Handoff points
- Deals at 3 tries go to the owner.
- A repeated objection goes to @cmo as a suggestion in the report. The loop never messages a buyer.
