---
name: unit-economics
description: Work out the unit economics of an offer - what a customer is worth, what it costs to win and serve one, and the payback - with the math shown. Use when the user asks if the business makes money per customer, about CAC, LTV, margin, or payback.
---

# Unit Economics

Not investment, tax or accounting advice: a working model to decide with. Say what
to confirm with an accountant when it matters.

## Read first
- `projects/<project-slug>/strategy/intake.md`: offer, price, customers, channels.
- The pricing and channel memos; the evidence log for real numbers (churn, deal size).
- Missing numbers: ask for the few that matter most (price, monthly churn or how
  long customers stay, cost to serve, what was spent to win the last customers).
  Then go on with ranges, marked as guesses.

## Method
1. **Pick the unit:** one customer (or one account, one order). Say which.
2. **Revenue per unit per month:** price after discounts and payment fees.
3. **Cost to serve per unit per month:** hosting, tools per customer, support time
   at a stated hourly value, payment fees. Gross margin = (revenue - cost) / revenue.
4. **Lifetime:** 1 / monthly churn, capped at 36 months for a young business.
5. **Value per customer (LTV)** = monthly gross profit x lifetime.
6. **Cost to win one (CAC):** spend plus the owner's selling time at a stated value,
   divided by customers won in the same period. Unpaid time still counts.
7. **Payback months** = CAC / monthly gross profit. **LTV:CAC ratio.**
8. **Sensitivity:** which input moves the result most. Show the result at low,
   mid and high values of that input.

## Options
Two or three levers to improve the weakest number: raise price, cut churn
(onboarding, annual plans), cut CAC (referrals, a cheaper channel), cut cost to
serve. For each, the change needed to reach a healthy line (payback under 12 months,
LTV:CAC of 3 or more as rough rules) and what it would take.

## Recommendation
The one lever to pull first, the number it moves, and the fact to measure next to
replace the biggest guess.

## Reply shape
A short table of inputs (value, source: fact, estimate or guess), then the results,
then the levers and the Recommendation.

## Before you reply
- Every input says where it came from.
- The owner's time is counted, not treated as free.
- Rules of thumb are labeled as rules of thumb.

## Write gate
Show the model in chat. Save it as a canvas page (`create-canvas`, then `add-block --type text`) only after the user approves.
