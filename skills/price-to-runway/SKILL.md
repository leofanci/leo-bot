---
name: price-to-runway
kind: workflow
description: Workflow. From a price to a runway: pricing options, the unit economics at the chosen price, then the runway plan, saved only after your approval. Needs @finance switched on.
---

# Price to runway

Start in your own private thread with `/price-to-runway <the offer, current price, cash and monthly costs>`. Each step runs as one turn by the bot it names. A checker bot reviews the steps that name one, and a weak step is tried once more with the checker's notes.

## Steps
1. skill: pricing-strategy | bot: cmo | output: the recommended price and model, with the price test | gate: no
2. skill: unit-economics | bot: finance | output: value per customer, cost to win one and payback at that price, with every guess marked | gate: no | checker: cmo
3. skill: runway-plan | bot: finance | output: runway in three scenarios at that price, the decision date and the moves that extend it | gate: approval

## Stop when
- The owner's cash balance or monthly costs are unknown: ask for them and end with `BLOCKED: <what is missing>`.
- The price test has not run and the owner wants a final price: say the plan rests on a guess.

## Always ask first
- Sending any message or post outside Leo Bot.
- Spending money or credits.
- Deleting anything.
- Any write outside the project the thread belongs to.
