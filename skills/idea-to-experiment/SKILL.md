---
name: idea-to-experiment
kind: workflow
description: Workflow. Take a raw idea to one designed first experiment: problem check, customer profile, positioning, then the experiment, saved only after your approval.
---

# Idea to first experiment

Start in your own private thread with `/idea-to-experiment <the idea in a few lines>`. Each step runs as one turn by the bot it names. A checker bot reviews the steps that name one, and a weak step is tried once more with the checker's notes.

## Steps
1. skill: problem-validation | bot: product | output: problem verdict, who has it, and the evidence gaps | gate: no
2. skill: icp-research | bot: research | output: one primary customer profile with where to reach them | gate: no | checker: product
3. skill: positioning | bot: cmo | output: a one-paragraph positioning draft for that customer | gate: no
4. skill: experiment-design | bot: product | output: the cheapest test of the riskiest assumption, with success and kill criteria | gate: approval | checker: research

## Stop when
- The problem check finds no real problem worth testing: say so and end with `BLOCKED: <why>`.
- The idea has no project yet and one is needed to save the experiment.

## Always ask first
- Sending any message or post outside Leo Bot.
- Spending money or credits.
- Deleting anything.
- Any write outside the project the thread belongs to.
