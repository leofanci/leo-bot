---
name: outreach
kind: workflow
description: Workflow. From a target customer to drafted first messages ready to send: a sourced prospect list, one draft per row, then every message planned as its own held send. Nothing is sent until you approve each one. Needs @sales switched on.
---

# Outreach

Start in your own private thread with `/outreach <who you sell to, what you sell, and the list to use or build>`. Each step runs as one turn by the bot it names. A checker bot reviews the steps that name one, and a weak step is tried once more with the checker's notes.

## Steps
1. skill: prospect-list | bot: sales | output: a list of 10 to 25 sourced prospects, each with its source link and signal | gate: no | checker: research
2. skill: outreach-draft | bot: sales | output: one draft per row in the list, in each row's Draft cell, and the angle used | gate: no | checker: cmo
3. skill: outreach-draft | bot: sales | output: every message planned as its own held send, each waiting in Approvals; nothing sent | gate: approval

## Stop when
- Fewer than 10 real prospects fit: say so and end with `BLOCKED: <why>`.
- No channel the owner can send from (no connected mail app and no browser access for the site): say what to connect and end with `BLOCKED: <why>`.

## Always ask first
- Sending any message: each one is its own card. Approving this plan never approves a send.
- Spending money or credits.
- Deleting anything.
- Any write outside the project the thread belongs to.
