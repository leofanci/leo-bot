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
- `read`: open, read, screenshot, scroll, click links. Typing is refused. A link is what the last
  snapshot's refs give the role `link`: a click passes only on such an `@ref` (see "Recorded
  deviations").
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
Approvals (Approve all or No; see "Recorded deviations"). Approved items wait up to 7 days. A send runs when the page's
URL matches an approved item (same host, same path, same query) and the typed text matches it after trimming
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

## Recorded deviations

Agreed during the build, recorded here so the spec matches what shipped:

- Send match: the page must match the approved item's host, path and query (the fragment aside),
  stricter than same host and path, so one approved item never covers another thread or recipient
  carried in the query.
- Plan card: no Edit. The bot types exactly the approved text, so an edited text would not match
  what is typed; the owner says No and asks for new text.
- Click at `read`: "click links" is a click on an `@ref` whose role in the last snapshot is
  `link`. No snapshot, an unknown ref, another role or a CSS selector is refused, and refs go
  stale at every navigation or click. On a listed site the field read still runs before it.
- A shell beside the browser: a run with a browser runs its shell commands in Leo Bot's sandbox,
  away from the browser's debug port, engine sockets and keys. Where that sandbox cannot wrap the
  shell, a browser level that can send and a shell level do not run together.

## Phases

1. Both modes when Leo Bot and Chrome run on the same machine.
2. A server Leo Bot borrows the owner's Chrome through the owner's runner (outbound long-poll, as
   turns already do), and a bot browser on the server can receive a login made on the owner's
   computer. Optional proxy per site for server traffic.
