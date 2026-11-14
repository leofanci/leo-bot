# Bot browser: design

Bots get a real web browser, so they can work on sites whose apps have no API action for the job
(direct messages, forms, pages behind a login). Composio stays the first choice wherever an app
has the action; the browser covers the rest.

## Two modes, one engine

The engine is `agent-browser` (Vercel Labs, Apache-2.0), pinned, run as a dependency, never copied.
It speaks MCP over stdio and drives Chrome over CDP. One bot picks one mode:

| Mode | Chrome it drives | Logins | Runs with the owner's computer off |
|---|---|---|---|
| `my_chrome` | the owner's everyday Chrome, attached | the owner's own | no |
| `bot` | a separate background Chrome per bot | saved per bot, encrypted | yes, when Leo Bot runs on a server |

The two modes complement each other. Any job can use either one, outreach included: `my_chrome`
has the lowest detection risk (real browser, home connection) but needs the owner's computer on;
`bot` runs unattended, also on a server with the computer off (phase 2 moves its login there and
adds an optional proxy per site). The owner binds each site to one mode, so a login is never in
both and the modes never conflict.

## Access levels

Per bot, same words as connected apps:

- `off`: no browser tools.
- `read`: open, read, screenshot, scroll, click links. Typing is refused.
- `write`: may type. A **send** (see below) runs only when it matches an item the owner approved in
  a send plan.
- `act`: sends run on their own, inside the site's daily cap, each logged in Approvals.

A Report only job never sends. Shared threads (an invited person can see them) never get the browser.

## What counts as a send

After a bot types text on a page, the next click or key press on that page is a **send**. The relay
cannot know which button posts a message, so it treats any commit after typing as one: fail closed.
Clicks with nothing typed are navigation and follow the level.

## Send plans (the `write` level)

The bot calls `leobot_plan_sends` with `[{url, text}]`. Leo Bot holds the items as one batch in
Approvals (Approve all, edit, or No). Approved items wait up to 7 days. A send runs when the page's
URL matches an approved item (same host, same path) and the typed text matches it after trimming
and collapsing whitespace; the item is then used up and logged "Sent from the browser". Anything
else is refused with a one-line reason the bot can pass on.

## Sites

The owner lists sites (host, mode, daily cap). Defaults: `linkedin.com` 20 a day, `instagram.com`
10 a day, both with no mode bound until the owner picks one. A bound site opens only in its mode:
a bot in the other mode is refused, so one account is never signed in from two places. Caps count
every bot and every workspace together, per site, over a rolling 24 hours.

## Safety

- Every page read is outside content: it marks the turn (`dashboard/taint.py`).
- The relay is the only way a bot reaches the browser. A tool name the relay does not know is refused.
- The browser engine's own file, eval and download tools are not exposed.
- `my_chrome` never falls back to `bot` (the logins differ). When Chrome cannot be reached, the call
  says why in one line.

## Phases

1. Both modes when Leo Bot and Chrome run on the same machine.
2. A server Leo Bot borrows the owner's Chrome through the owner's runner (outbound long-poll, as
   turns already do), and a bot browser on the server can receive a login made on the owner's
   computer. Optional proxy per site for server traffic.
