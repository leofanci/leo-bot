---
name: process-doc
description: Write down a repeatable process as a short checklist anyone can follow - trigger, steps, owner, done-when - and spot steps to cut or automate. Use when the user says they keep doing something by hand, wants an SOP, a checklist, or to hand a task to someone.
---

# Process Doc

## Read first
- The owner's description of how the task is done today, or a walkthrough.
- `projects/<project-slug>/strategy/intake.md` for context; existing process canvases
  (update one instead of starting a second).
- Missing: ask the owner to describe the last time they did it, step by step.

## Method
1. **Name it:** a verb and an object ("Onboard a new clinic").
2. **Trigger:** what starts it. **Done when:** the check that it is finished.
3. **Owner:** who does it, and who to ask when stuck.
4. **Steps:** numbered, one action each, starting with a verb. Include where things
   are (which tool, which folder) and the time each takes.
5. **Checks:** the 1 or 2 points where mistakes happen, with what to check.
6. **Cut and automate:** mark steps that add nothing (cut), steps a template would
   speed up, and steps a scheduled job or a connected app could do. Suggest a loop
   only for a repeated check (see `loop-design`).
7. **Measure:** how often it runs and how long it takes, to see if the change helped.

## Options
Two or three ways to run it from now on: as written by the owner, handed to a person
with this checklist, or partly done by a bot job. Trade-off: time saved against setup
and risk.

## Recommendation
The way to run it and the first step to cut or automate.

## Before you reply
- Someone new could follow it without asking.
- Every step is one action with a verb.
- Anything that sends, pays or deletes stays a step for a person to approve.

## Write gate
Show the process in chat. Save it as a canvas page (`create-canvas --template checklist` or a text block) only after the user approves.
