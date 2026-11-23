---
name: process-setup
kind: workflow
description: Workflow. From a task done by hand to a written process: the process as it runs today, the tool for the steps worth automating, then the final checklist, saved only after your approval. Needs @ops switched on.
---

# Process setup

Start in your own private thread with `/process-setup <the task and how you do it today>`. Each step runs as one turn by the bot it names. A checker bot reviews the steps that name one, and a weak step is tried once more with the checker's notes.

## Steps
1. skill: process-doc | bot: ops | output: the process as it runs today, with the steps to cut, template or automate | gate: no
2. skill: vendor-compare | bot: ops | output: the tool, or no tool, for the steps worth automating, with its cost for 12 months | gate: no | checker: cos
3. skill: process-doc | bot: ops | output: the final checklist using the chosen tool, with its trigger, owner and done-when | gate: approval

## Stop when
- The task runs less than once a month: a written checklist is enough, end after step 1 with `BLOCKED: rare task, no tool needed`.
- The owner cannot describe the steps yet.
- Buying or signing up for a tool is always the owner's action, never a step.

## Always ask first
- Sending any message or post outside Leo Bot.
- Spending money or credits.
- Deleting anything.
- Any write outside the project the thread belongs to.
