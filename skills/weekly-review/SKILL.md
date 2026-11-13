---
name: weekly-review
description: Portfolio operating cadence — status, flags, and the week's top 3 priorities across all projects and profiles. Use for "weekly review", "what should I focus on", "status check-in", or "what needs attention". Not cross-entity coordination — use portfolio-sync for that.
---

# Weekly Review

## When to use this (not portfolio-sync)
- **weekly-review**: portfolio health — stage, momentum, stale memos/experiments, priority conflicts, max 3 priorities for the week.
- **portfolio-sync**: cross-entity coordination — profile content vs launch phase, sequencing between projects/products, actions that touch multiple entities.

If the user wants both, run weekly-review first (what matters), then portfolio-sync (what to align).

Pull statuses, dates, and priorities from `os.db` (fast path); open source files
(strategy/intake.md goals, memos) only for the prose you need to quote. If
`os.db` is missing, read files directly. Covers projects, profiles, products,
and channels.

## Output (conversational, not JSON)
1. **Portfolio snapshot**: one line per project — stage, momentum
   (moving/stalled/blocked), the single most important open question.
2. **Flags**:
   - experiments "running" with no logged result past their duration
   - projects with evidence logged but a stale assessment (recommend rerun)
   - content plans expiring within a week with no next plan
   - product features stuck in "building" with no movement; releases shipped with no content follow-up
   - priority conflicts (3 "primary" projects + 10 h/week = a decision, surface it)
3. **The week's plan**: max 3 priorities across the whole portfolio, each tied
   to a project goal — and explicitly what is NOT getting attention and why
   that's okay.
4. End by asking which priority to start, and route to the right skill.

Rules: if the state index shows a stale memo, say which assumption broke (and its evidence) and offer a new
version; never write one unasked. Be the honest chief of staff — stalled is stalled. Don't propose more
work than intake hours allow; cutting is part of the job.
