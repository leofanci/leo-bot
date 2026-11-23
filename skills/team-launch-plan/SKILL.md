---
name: team-launch-plan
kind: workflow
description: Workflow. A launch plan made by a team: @gtm drafts it, @research and @cmo advise on the draft in turn, @gtm revises it and @product checks it before it is saved with your approval.
---

# Team launch plan

Start in your own private thread with `/team-launch-plan <what launches, for whom, and when>`. @gtm drafts the plan with the launch-plan skill. @research then @cmo each advise on the draft, read-only. @gtm revises it with their advice, and @product passes it or sends it back once with notes. The plan is saved only when you approve its card.

## Steps
1. skill: launch-plan | bot: gtm | output: one goal metric with a number and a date, success and kill lines, the launch type, a dated checklist for before, launch day and week one with an owner per item, and the flop plan | gate: approval | checker: product | consult: research, cmo

## Stop when
- The project has no intake, or nothing is ready to launch: say what is missing and end with `BLOCKED: <why>`.

## Always ask first
- Sending or posting anything: each one is its own card. Approving this plan never approves a send.
- Spending money or credits.
- Deleting anything.
- Any write outside the project the thread belongs to.
