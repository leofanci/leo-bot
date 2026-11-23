---
name: deal-prep
kind: workflow
description: Workflow. From a target customer to a call plan and a first prospect list: customer profile, discovery call plan, then the list, saved only after your approval. Needs @sales switched on.
---

# Deal prep

Start in your own private thread with `/deal-prep <who you sell to and what you sell>`. Each step runs as one turn by the bot it names. A checker bot reviews the steps that name one, and a weak step is tried once more with the checker's notes.

## Steps
1. skill: icp-research | bot: research | output: the beachhead segment, its buying trigger and where to find them | gate: no
2. skill: discovery-call | bot: sales | output: a call plan for that segment: questions, qualification and the next step to ask for | gate: no
3. skill: prospect-list | bot: sales | output: the list filter and a first batch of 10 to 25 sourced prospects | gate: approval | checker: research

## Stop when
- The segment cannot be named narrowly enough to find 10 real members: say so and end with `BLOCKED: <why>`.
- No source the owner may use lists these buyers.

## Always ask first
- Sending any message or post outside Leo Bot.
- Spending money or credits.
- Deleting anything.
- Any write outside the project the thread belongs to.
