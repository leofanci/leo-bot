---
name: feedback-to-test
kind: workflow
description: Workflow. From customer feedback to one designed test: feedback themes, a problem check on the top theme, then the experiment, saved only after your approval. Needs @support switched on.
---

# Feedback to test

Start in your own private thread with `/feedback-to-test <the feedback, or where it is, and the period>`. Each step runs as one turn by the bot it names. A checker bot reviews the steps that name one, and a weak step is tried once more with the checker's notes.

## Steps
1. skill: feedback-synthesis | bot: support | output: the themes with counts and quotes, and the top theme to act on | gate: no
2. skill: problem-validation | bot: product | output: a verdict on the top theme: how real, who has it, the evidence gaps | gate: no | checker: support
3. skill: experiment-design | bot: product | output: the cheapest test of the riskiest assumption in that theme, with success and kill criteria | gate: approval

## Stop when
- The feedback has fewer than five points from the target customer: say so and end with `BLOCKED: <why>`.
- The problem check finds the top theme is noise or outside the target segment.

## Always ask first
- Sending any message or post outside Leo Bot.
- Spending money or credits.
- Deleting anything.
- Any write outside the project the thread belongs to.
