---
name: team-launch-debate
kind: workflow
description: Workflow. A launch plan where @gtm brings in only the teammates it needs, each advises on its own, they debate what they disagree on, and @product checks it before it is saved with your approval.
---

# Team launch plan with debate

Start in your own private thread with `/team-launch-debate <what launches, for whom, and when>`. @gtm decides whether it can write the plan alone. If it needs other areas, it drafts the plan and asks only the teammates it needs (research and cmo often help); each one advises on the draft without seeing the others' advice, and may bring in one more teammate. @gtm writes the full plan with their advice. Then each teammate agrees or raises its one real objection, and @gtm answers the objections, for at most two rounds. @product passes the plan or sends it back once with notes. The plan is saved only when you approve its card.

## Steps
1. skill: launch-plan | bot: gtm | output: one goal metric with a number and a date, success and kill lines, the launch type, a dated checklist for before, launch day and week one with an owner per item, and the flop plan | gate: approval | checker: product | team: yes | debate: 2 | consult: research, cmo

## Stop when
- The project has no intake, or nothing is ready to launch: say what is missing and end with `BLOCKED: <why>`.

## Always ask first
- Sending or posting anything: each one is its own card. Approving this plan never approves a send.
- Spending money or credits.
- Deleting anything.
- Any write outside the project the thread belongs to.
