---
name: workspace
description: Master dispatcher for the GTM Operating System. Use at session start, when intent is unclear, or when the user says fill tabs / left panel / divide spec across project sections. Routes to the right skill and maps content to the six dashboard project tabs (not product roadmap rows only).
---

# Leo Bot — Dispatcher

Decision memos: 2-4 options, pros/cons, one recommendation. Tab map + write gate live in system prompt (TAB ROUTING, WRITE GATE) — do not repeat them here.

## Fill tabs / divide spec
1. **Routing plan** — map blocks → `sec01`–`sec06`, one-line bullets only. No osctl. No full artifact text.
2. **User confirms** tab placement.
3. **One tab per commit** via osctl. Confirm with tab id only.

## Route to skill
| Intent | Skill |
|--------|-------|
| Validate problem | problem-validation |
| Project facts / evidence | venture-intake |
| GTM stand / pace | gtm-assessment |
| Channels | channel-strategy |
| Test design | experiment-design |
| Test results | experiment-review |
| ICP | icp-research |
| Market size / SAM/SOM | market-sizing |
| Positioning | positioning |
| Competitors | competitor-scan |
| Pricing | pricing-strategy |
| Launch | launch-plan |
| Brand voice | brand-identity |
| Content calendar | content-plan |
| Post brief / revise | content-brief |
| Copy variants | copy-variants |
| Product roadmap | product-build |
| Scaffold folders | portfolio-map |
| Timeline | portfolio-timeline |
| Cross-entity sync | portfolio-sync |
| Weekly priorities | weekly-review |

## Rules
Read intake + recent memos first. No accelerate while validation weak. osctl only after user approval. Files win over os.db.