---
name: gtm-assessment
description: Produce a GTM assessment decision memo for a project - ICP hypotheses, positioning options, pace call (validate quietly vs accelerate), ranked riskiest assumptions. Use for "where does this stand", "what's the GTM", or after new evidence is logged.
---

# GTM Assessment (Job A)

Read `projects/<project-slug>/strategy/intake.md` INCLUDING the evidence log,
plus any prior assessment memos.

## Output: decision memo saved to `projects/<project-slug>/strategy/memos/assessment-vN.json`
```json
{
  "version": 1, "date": "", "stage_read": "1-2 sentences, where this actually is",
  "icp_hypotheses": [{"segment":"","why_them":"","confidence":"low|medium|high","evidence_basis":""}],
  "positioning_options": [{"angle":"","pros":[],"cons":[]}],
  "pace_recommendation": {"call":"validate_quietly|soft_launch|accelerate",
    "reasoning":"tied to riskiest assumption + resources",
    "what_would_change_this":""},
  "riskiest_assumptions_ranked": [],
  "recommendation": "the single path a senior consultant would take, and why",
  "superseded_memo": null
}
```

## Write gate
Propose assessment memo in chat (target `sec01`, `create-memo --type assessment`). Commit only after user approves.

## Rules
- A stale memo (contradicted assumption in the state index): name the assumption and its evidence, offer a new
  version, never write it unasked.
- Pace call MUST follow from evidence: no revenue + unvalidated problem = never "accelerate".
- Each ICP hypothesis cites its evidence_basis from intake; if none, confidence = "low".
- 2-4 positioning options, genuinely different (not the same idea reworded).
- End by recommending the next skill (usually experiment-design or channel-strategy)
  and summarize the memo conversationally — don't just dump JSON.
