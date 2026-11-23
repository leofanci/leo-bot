---
name: hire-plan
kind: workflow
description: Workflow. From "we need help" to a hiring plan: the role and whether to hire at all, a fair interview process, then the onboarding plan, saved only after your approval. Needs @people switched on.
---

# Hire plan

Start in your own private thread with `/hire-plan <the work that is not getting done and your budget>`. Each step runs as one turn by the bot it names. A checker bot reviews the steps that name one, and a weak step is tried once more with the checker's notes.

## Steps
1. skill: role-scope | bot: people | output: hire or not, the role, its 90-day outcomes, cost and a job description draft | gate: no | checker: cos
2. skill: interview-plan | bot: people | output: the stages, the scorecard and the question set for that role | gate: no
3. skill: onboarding-plan | bot: people | output: the first 30 days as a dated checklist, with access to grant and remove | gate: approval

## Stop when
- Step 1 finds a better option than a hire (automate it, stop doing it): say so and end with `BLOCKED: <the better option>`.
- The cost does not fit the runway the owner gave.
- Posting the job or messaging candidates is always the owner's action, never a step.

## Always ask first
- Sending any message or post outside Leo Bot.
- Spending money or credits.
- Deleting anything.
- Any write outside the project the thread belongs to.
