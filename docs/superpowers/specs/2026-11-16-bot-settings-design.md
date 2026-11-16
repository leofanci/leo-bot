# Per-bot settings and General settings

## Why

Bot setup lived in one machine-wide grid (Settings, Bots: engine and access per workspace). A bot's
instructions shipped with the code and were the same in every project, and its model followed the
server's automatic tiering only. Bots should be set up where they work (each project's Team), with
machine-wide defaults each project inherits. A few basic app settings were missing for launch.

## Goals

- Each bot, per project: engine, model, access level, standing instructions.
- The same four as machine-wide defaults ("Every project"), inherited when a project sets nothing.
- Settings, General: About you, reply language, timezone, short replies, daily spending caps.

## Non-goals

- Per-bot spending caps (the spend ledger has no per-bot rows; a turn can hold several bots).
- Editing the shipped bot definitions (`bots/*.md`) from the screen.
- Theme, notifications, data export.

## Approaches

1. Per-project overrides over machine-wide defaults, edited in Team Details and Settings, Bots
   (picked). Builds on the existing `ws/bot` keys in `team/engines.json`.
2. One bot config everywhere with optional project add-on: simpler, but a bot could not run on a
   different engine or level per project, which the existing per-workspace keys already allow.
3. Hermes-style profiles (a full config copy per bot): heavy, duplicates provider keys.

## Design

Storage stays owner-only in the data root's `team/` (denied to bots and to `osctl read-file`):

- `team/engines.json`: `bots`, `access` and a new `models` map accept `*/<bot>` as the machine-wide
  default next to `<ws>/<bot>`.
  - Engine: `ws/bot` > `workspaces[ws]` > `*/bot` > default.
  - Access: `ws/bot` > `*/bot` > ask. A shared thread or invited person still never gets more than ask.
  - Model: `ws/bot` > `*/bot` > auto. `auto` keeps the server's tiering; `fast` and `strong` pin the
    engine's fast or strong model (`resolve_model` maps them per engine).
- `team/bot_instructions.json` (0600): `{"*/bot": text, "ws/bot": text}`, 2000 characters each.
  Both apply, machine-wide first, on the bot's full card (fresh thread or speaker change).
- A deleted workspace drops its keys from both files.
- `leobot.json` (General): `about_me` (1500 chars, private turns only), `reply_language` (empty
  matches the message; goes in the chat rail), `timezone` (IANA name, empty is the machine's; set as
  `TZ` in the server process so jobs and children follow), `caps` (`owner`, `workspace`, `person`
  dollars per 24 hours). An environment variable still wins over a saved cap.

Screens:

- Team, bot Details (owner only): a Setup section with Engine, Model, Access and Instructions, each
  naming what it inherits when left on the default.
- Settings, Bots: Defaults, then Every project (per bot: engine, model, access, instructions), then
  the existing By workspace grid.
- Settings, General: first tab. Short replies moves here from Bots.

## Left to the builder

Exact copy, layout inside the existing card styles, endpoint names.
