# Go-to-market tab (sec07) — design

Date: 2026-07-28
Status: approved, implementing

## Problem

"Leo Bot" had no GTM home. Go-to-market was scattered: `channels` memo buried under
Positioning & pricing, `launch` memo in Overview, experiments in sec03, activities in
Calendar/Ops — with no object representing a go-to-market *motion* and no view tying a
channel to the activities that execute it, the experiments that validate it, and the
launch that ships it.

## Decision

Add a seventh project tab, **Go-to-market** (`sec07`), built on a **lens model**: one
source of truth per object, GTM is a view that references objects living in their real
homes. No duplication.

## Structure

Two nested levels:

```
Phase      time-boxed bet ("First 10 customers")          container, ordered
  └─ Strategy   channel-motion ("LinkedIn founder-led outbound")
        - channel_slug   → refs a connected channel
        - hypothesis, status, primary?
        - (lens) experiments · activities · launch tagged to it
```

IDs cascade: `prN.sec07.phM`, `prN.sec07.phM.stM`.

## Tab re-division

- `channels` memo: MEMO_SECTION `pricing` → `gtm`
- `launch` memo: MEMO_SECTION `overview` → `gtm`
- Positioning & pricing keeps: positioning, pricing, competitors, icp, market-sizing.
- Six existing `secNN` numbers stay fixed (composed IDs are position-derived; renumbering
  would break them). GTM is `sec07`, placed in **display order** right after
  Positioning & pricing via PROJECT_SECTION_LAYOUT insertion order.
- Moving the two memos shifts their computed composed IDs (e.g. a channels memo
  `pr1.sec04.mmX` → `pr1.sec07.mmX`). Harmless: IDs are computed, not stored.

## Storage (files-as-truth)

- `projects/<slug>/gtm/phases.json` — ordered list: `[{id, name, goal, timebox, status}]`
- `projects/<slug>/gtm/strategy-<stem>.json` — one per strategy:
  `{id, phase_id, name, channel_slug, hypothesis, status, primary, notes}`

## The lens (anti-repetition)

Tag lives on the child, pointing up. No strategy copies children in.

- experiment JSON gains optional `strategy: <strategy-id>`
- activity gains optional `strategy: <strategy-id>`
- launch memo may carry `strategy: <strategy-id>` (frontmatter)

GTM tab renders each strategy by querying children WHERE `strategy = <id>`. Breadcrumb is
bidirectional: experiment/activity shows parent strategy; strategy card deep-links to each
child in its home tab.

## UX

Vertical phase timeline. Under each phase, strategy cards showing channel + status +
hypothesis + rolled-up counts (`3 experiments · 2 activities · launch ✓`). Expand a card →
the referenced items, each a link to its home tab. Primary channel = the strategy flagged
`primary` (this is what the `channel-strategy` skill now outputs).

## osctl

New, following existing verb patterns:

- `create-phase --project <slug> --name "..." [--goal] [--timebox]`
- `update-phase --project <slug> --id <phase-id> [--name] [--goal] [--timebox] [--status]`
- `delete-phase --project <slug> --id <phase-id>`
- `create-strategy --project <slug> --phase <phase-id> --channel <channel-slug> --hypothesis "..." [--primary]`
- `update-strategy --project <slug> --stem <stem> [--name] [--phase] [--channel] [--hypothesis] [--status] [--primary]`
- `delete-strategy --project <slug> --stem <stem>`
- `--strategy <id>` flag added to `create-experiment` and `create-activity`.

## Scope cut (YAGNI, first release)

- posts→strategy linking deferred (posts already flow profile→project).
- Reordering phases handled by editing `phases.json` order; no dedicated reorder verb yet.

## Tests

- schemas: `sec07` exported, gtm in section_layout, memo_section remaps for channels/launch.
- ids: phase/strategy composed-id cascade.
- fileops: create/update/delete phase & strategy; strategy tag round-trips on experiment/activity.
- osctl: each new verb.
- indexer/db: phases + strategies aggregate into `project()` payload.
- no-usage-data guard stays green.
