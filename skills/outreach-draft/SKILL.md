---
name: outreach-draft
description: Draft one first message per prospect in a list - email or DM - grounded in each row's public source, then plan every send for its own approval. Use when the user wants outreach, cold emails or messages written for a prospect list.
---

# Outreach Draft

## Read first
- `projects/<project-slug>/strategy/intake.md`: offer, customer, proof.
- The positioning and ICP memos: the problem in the customer's words, the claims the
  memos back. A claim no memo backs stays out.
- The prospect list (`get-list --id ls<N>`): each row's source link, signal and status.
- Missing list: suggest `/prospect-list` first. Missing positioning: ask for the
  problem and the offer in one line each.

## Method
1. **Skip rows** marked do-not-contact, opted out, lost, or contacted in the last 30
   days. Name how many were skipped and why.
2. **Pick the channel per row:** email when the row has a business address from a
   public page or the owner; a DM on the site the row's source is on otherwise.
3. **Write one message per row:**
   - First line: one fact from the row's own source note or signal. Never invented
     familiarity ("loved your post" with no post named), never flattery.
   - The problem in the customer's words, one sentence.
   - What the offer changes, one sentence, only what the memos back.
   - One small ask: a reply, a 15-minute call, or a yes or no question.
   - Email: under 90 words, a plain subject under 7 words, and a last line saying
     how to opt out ("Reply no and I will not write again."). DM: under 300 characters.
   - No false urgency, no fake scarcity, no "just following up" openers.
4. **Mark what you inferred:** a role or a pain you guessed, not read, gets a note.
5. **Check each draft against its row:** the fact in line one is in the source.

## Options
Two angles for the batch, for example the buying signal first or the problem first.
One line each on the reply rate you expect (an estimate) and the risk (a signal that
is weeks old reads as stale).

## Recommendation
The angle to use, the 3 rows to send first (strongest signal), and what result after
20 sends would make you change the angle.

## Sending
- Plan every message as its own send: an app send waits as its own card in Approvals,
  and a browser DM goes in a send plan (`leobot_plan_sends`) for the owner's approval.
  Say in the reply that each one waits there.
- In a workflow step or a scheduled run every send waits, whatever the bot's access.
- Never send to a row marked do-not-contact. Never send more than the owner asked for.

## Before you reply
- Every first line traces to the row's source; no made-up facts or names.
- Every email has the opt-out line.
- Nothing has been sent by this turn.

## Write gate
Show the angle and three sample drafts in chat. Write each draft into its row's `Draft` cell (`get-list`, then `update-row` with the version you read) only after the user approves.
