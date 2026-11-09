# Coworkers: invite people into a workspace

## Intent

Leo Bot is one person's OS today: one login (the owner token), one bot chat in
flight at a time, every screen full-trust. A project often has a small team
around it. They should be able to join that project's workspace and talk with
the owner and the bots in the same channels, without getting the owner's
files, terminal, keys or other companies.

Prior art: Buzz (open source, Apache-2.0). Its model is copied where it fits:
whoever runs the server owns it, invites are links that expire, three roles,
people and agents are both members of a room, a room is open or private. Buzz
code is not used (different stack); only the behavior.

## Goals

- The owner invites a person into one project workspace as **admin** or
  **member** with a link that expires. The person opens it, types a name, and
  is in.
- Invited people see only the workspaces they were invited to and, inside
  them, only the channels they may see. They chat with people and bots there.
- Two people can talk with bots at the same time in different threads.
- Works on the owner's own Mac reached through a tunnel (Tailscale Serve or
  Funnel, Cloudflare Tunnel) or on an always-on machine. Same code either way.

## Non-goals

- No hosted Leo Bot run for other owners. Each owner runs their own server.
- No passwords, email, SSO. Identity is the device cookie the invite link
  gives (Buzz keeps a private key on the device; this is the same idea).
- Members do not get Content, Files, Calendar, Operations, Approvals,
  Integrations, terminal, settings or HQ. Those stay owner-only in this round.
- No realtime push. An open thread refreshes by polling.
- No role changes in place. Remove and re-invite.

## Anti-goals

- Anything a member can reach that leaks another workspace, the owner's
  connected apps (mail, calendar, socials), keys, or the terminal.
- Owner flows getting slower or different.

## Roles (per workspace)

| | Owner | Admin | Member |
|---|---|---|---|
| Who | holder of the server's owner token, every workspace | invited | invited |
| Invite | admins and members | members | no |
| Remove | anyone | members | no (can leave) |
| Create/edit/delete channels, set private + people | yes | yes | no |
| Chat in visible channels, own DMs with bots | yes | yes | yes |
| See pending invites | all | the workspace's | no |
| Bots on/off, app grants, memory edits | yes | no | no |

HQ (the holding) cannot be invited into.

## Behavior

- **Invite.** Team page, People section, "Invite" (owner, admin). Pick role
  (admin is owner-only) and expiry (1 day, 7 days default, 30 days). The link
  shows once with a Copy button; only its hash is stored. Pending invites list
  with role, expiry, Revoke. A link works once.
- **Join.** `/join/<code>` serves a small standalone page (no app shell): the
  workspace name, the role, a name field (1-40 chars), Join. Joining sets an
  HttpOnly member cookie (1 year) and opens the app. An expired, used or
  revoked link says so. A device that already has a member cookie adds the new
  workspace to the same person.
- **Sharing address.** Owner sets the public URL (`https://<host>[:port]`) once
  on HQ's Team page. The server then accepts that Host and Origin too. Invite
  links are built from it; without it, Invite explains how to set one (link to
  `docs/sharing.md`). The server keeps listening on 127.0.0.1 only; the tunnel
  forwards to it.
- **Terminal** is served only when the request Host is localhost/127.0.0.1,
  whoever asks. Owner login (`?token=`) also works over the public URL.
- **People list.** Team shows People (owner first, then admins, members, with
  role and joined date, Remove where allowed, Leave for yourself) above Bots.
  Members see Bots read-only (no switches, grants, memory).
- **Private channels.** The channel form gains "Private" and a people picker.
  A private channel is visible to the owner and the people added; its bots
  are its bot members as today. #general is always open to every member of the
  workspace. Topic threads are shared by everyone who can see the channel.
- **DMs with bots** are per person: a thread started in `dm-<bot>` is visible
  only to the person who started it (owner's existing threads stay the owner's).
- **Authors.** Each user message records who wrote it; threads show the name
  above human messages that are not yours. Bots are told who is speaking:
  `[<name>, <role> in this workspace]` before a non-owner message.
- **Concurrency.** One live Claude session per thread (pool, LRU-closed when
  idle, cap 8). A second message into a thread that is mid-reply gets
  "Someone is replying in this thread". Stop stops only that thread's turn.
- **Shared threads.** A thread an invited person can see (the room is not a
  DM and some person passes `can_see`, or the speaker is a person) runs with
  no built-in file or shell tools and no connected apps, whoever speaks.
  Claude Code runs read-only tools and read-only shell commands (Read, `cat`,
  `osctl ... < file`) on any path without asking, so a Bash/Read toolset
  cannot be scoped to one workspace. The turn instead gets one HTTP MCP server
  on the dashboard (`/api/internal/os/mcp`, per-request token, this machine
  only) whose single tool runs osctl with the token's workspace and
  `MYOS_SHARED`: `remember` refused, `read-file` only under `projects/<ws>/`
  and the public docs folders. Web rules unchanged. The linked app's TokenSave
  summary is still injected by the server.
  Saving through a bot ("yes" on an approval in the thread) works for members:
  it writes only that workspace's files.
- **Visibility follows the recorded room.** A person sees a thread only in the
  room stored on it (never via the #general fallback for deleted channels or
  switched-off bots) and never a thread with `apps_used`. Chats sent to a
  person drop `scope` (it holds the linked folder path).
- **Local only.** Terminal and both bot relays require a local Host, a local
  or absent Origin and no proxy headers (`X-Forwarded-*`, `Forwarded`,
  `CF-*`, `Tailscale-*`), since a tunnel may rewrite Host. Cookies get
  `Secure` when the request came through the sharing address.
- **Refresh.** An open thread re-fetches every 5 s while idle and the tab is
  visible; the rail re-fetches threads of the open room on the same tick.
- **Removal** deletes the membership; a person with no workspaces left is
  deleted, so their cookie stops working at once.

## How

- `core/people.py`: `team/people.json` `{people: {pid: {name, token_hash,
  joined, ws: {slug: role}}}}`, `team/invites.json` `{invites: [{id,
  code_hash, ws, role, by, created, expires, used}]}`, `team/access.json`
  `{public_url}`. sha256 hashes, `hmac.compare_digest`, atomic writes, a lock.
  Pure functions with `root` like `core/integrations.py`.
- `dashboard/server.py`: `_viewer()` → `Viewer(kind="owner"|"person", pid,
  roles)` from the owner cookie or the member cookie. Owner path unchanged.
  A person passes only an allowlist of routes (`/`, assets, `/join/*`,
  `/api/me`, `/api/workspaces`, `/api/bots`, `/api/rooms`, `/api/chats`,
  `/api/chats/<id>`, `/api/ask`, `/api/chat-stop`, `/api/people*`,
  `/api/invites*`, `/api/rooms` writes for admins, `/api/schemas`); every
  `ws` must be one of theirs; chats and rooms are filtered by visibility.
  Everything else 403. Host/Origin sets include the public URL.
- Session pool replaces `_CHAT`/`_CHAT_SCOPE` (keyed by chat id, per-session
  busy flag); `/api/chat-session` and `/api/chat-reset` keep their owner
  meaning for the active owner thread.
- `core/rooms.py`: topic rows gain `private` and `people`; `visible(room,
  viewer)`; `chat_store` records `by` on the chat (starter) and on each user
  message.
- `dashboard/app.js`: `/api/me` at boot; member mode trims the rail (no
  Approvals, Files, Content, Integrations, Your time, new project, logo menu)
  and routes (team, rooms); People section, invite dialog, private toggle,
  author names, polling.
- `docs/sharing.md`: Tailscale Serve (team on your tailnet), Funnel or
  Cloudflare Tunnel (anyone with the link), always-on machine.

## Left to the builder

Exact copy, layout of the People section and invite dialog, poll wiring.
