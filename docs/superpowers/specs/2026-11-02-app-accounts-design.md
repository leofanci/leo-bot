# Connected apps: several accounts per app, picked per workspace

Follows `2026-10-29-composio-integrations-design.md` (round 1).

## Intent

One person often runs several inboxes, calendars or social accounts, and each
project should touch only its own. Round 1 had one Composio session for
everything, so Composio used the most recently connected account of each app.

## Behavior

- **Logos.** Every app row shows the app's logo, served by the dashboard from
  `GET /api/integrations/logo/<slug>` over a letter tile. The browser never
  loads an outside image host, so the CSP stays `img-src 'self'`.
- **Several accounts per app.** Integrations (HQ) shows each connected account
  under its app, with an "Add account" button, up to 10 per app. A click on the
  account name renames it (Composio alias). Disconnect (two clicks, no
  browser dialog) deletes the connection in Composio and drops the account
  from every workspace; a held action on it is then refused on Approve.
- **Accounts per workspace.** Each account row has a checkbox per workspace (HQ
  and every project). A newly connected account joins HQ, where it was
  connected. A project gets an account only when it is ticked for it, never
  automatically. A workspace with no ticked account for an app has no access to
  that app at all, whatever the bot's grant says.
- **Choosing between accounts.** With one ticked account, nothing changes for
  the bot. With several, the bot's turn lists them (`id (name)`) and each tool
  call must name one in `account`; a call without a valid choice is refused and
  told the choices. The bot asks the user when the request does not say which.
- **Approval cards** show the account first. Approve runs on exactly that
  account, and refuses if it is no longer ticked for the workspace or no longer
  connected. It never falls back to another account.

## How

- `team/integrations.json` gains `known` (ACTIVE accounts from the last
  Composio listing, with labels), `seen` (accounts already offered to HQ once),
  `accounts.<ws>.<toolkit>` (ticked ids) and `people.<pid>.sessions.<ws>`.
- One Composio session per workspace, created with
  `toolkits.enable` = apps with a ticked account and
  `connected_accounts` = the ticked ids, so Composio itself cannot reach any
  other account. Several accounts of one app turn on `multi_account` with
  `require_explicit_selection`. The session is remade when the ticks change
  (a fingerprint of the pin is stored with it).
- A separate link-only session (`_link`, multi-account on) makes connect links;
  no bot ever uses it. `POST .../link` returns the new account id, which the
  panel polls for.
- The relay re-reads the workspace's ticks on every call, like grants. Reads
  carry `account` only when there is a choice; held writes always store it.
- Account names come from the provider and are quoted into the bot's turn, so
  they are cut to plain text (letters, digits, space, `. @ + ' -`, 60 chars).
- Deleting a project drops its ticks, grants and sessions, so a new project
  with the same slug starts with no apps.
- A cut-off Composio reply counts as unreachable: an Approve says it may have
  run instead of staying "running".
- Logos: fetched server-side without the API key, https only, every resolved
  address public, TLS verified, no redirects, image types only, 256 KB cap,
  cached per process (capped). Served with `sandbox` CSP so an SVG opened directly runs nothing.

## Not in this round

- Live check: the `GET /connected_accounts` parsing, `account` in
  multi-execute items and the execute body follow Composio's v3.1 reference and
  are verified on first real use, like round 1's `_parse_*` helpers.
