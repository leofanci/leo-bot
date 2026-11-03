# Sharing a workspace with people

myOS runs on your machine and listens only on `127.0.0.1`. To let people you
invite reach it, put a tunnel in front of it. The tunnel gives you an https
address; you save that address once in myOS and invite links use it.

Whoever runs the server owns it. Each person you invite gets into one project
workspace (never HQ) as **admin** or **member**:

| | Owner (you) | Admin | Member |
|---|---|---|---|
| Invite | admins, members | members | no |
| Remove | anyone | members | leave only |
| Manage channels (open or private) | yes | yes | no |
| Chat with people and bots in channels they can see | yes | yes | yes |
| Content, Files, Calendar, Approvals, Integrations, terminal | yes | no | no |

**Shared threads are locked down.** Any thread an invited person can see
(#general, open channels, private channels you added them to, their own DMs
with a bot) runs without a shell, file access or connected apps, for your
messages there too. The bot's only tool is myOS itself (osctl), run by the
server inside that workspace: it can read and save that workspace's content,
but not your other workspaces, your files, your keys, bot memory or your mail.
Bots can still answer from a summary of the project's linked app code. For
mail, calendar or anything on your machine, use a DM with a bot or a private
channel with no people in it. On the shared calendar, other workspaces show
only as busy time. The terminal only opens on this machine, never over the
shared address.

## 1. Pick a tunnel

Your machine has to be awake and myOS running for people to reach it. For
always-on access, run myOS on a small server instead and use the same steps.

**Tailscale Serve** (only people on your tailnet, or people you share the
machine with in Tailscale):

```sh
tailscale serve --bg 8765        # myOS's port
tailscale serve status           # shows https://<machine>.<tailnet>.ts.net
```

**Tailscale Funnel** (anyone with the link, over the internet):

```sh
tailscale funnel --bg 8765
```

**Cloudflare Tunnel** (anyone with the link; a domain on Cloudflare):

```sh
cloudflared tunnel --url http://127.0.0.1:8765
```

A quick tunnel prints a random `https://….trycloudflare.com` address that
changes on every run; a named tunnel keeps one address.

## 2. Save the address in myOS

HQ, Team, **Sharing address**: paste the https address (no path) and Save.
myOS then accepts requests for that address and builds invite links from it.
Clear the field to stop sharing; existing people keep their seats but cannot
reach the server until an address is set again.

## 3. Invite

Open a project workspace, Team, People: pick a role and how long the link
lives (1, 7 or 30 days), **Create invite link**, Copy, and send it to one
person. A link works once. Pending invites can be revoked.

The person opens the link, types their name and joins. Their browser keeps a
private cookie; that cookie is their identity on your server, so they should
keep using the same browser. Removing someone from their last workspace ends
that cookie at once.

## Notes

- Private channels show only to you and the people ticked in the channel form.
  Adding someone to a channel shows them its existing threads, except threads
  that read a connected app: those stay yours.
- DM threads with a bot belong to whoever started them. When a channel is
  deleted or a bot is switched off, its threads move to #general for you only.
- Two people can talk with bots at the same time in different threads. A
  thread that is mid-reply asks the other person to wait.
- What invited people save through a bot (intake, memos, posts) is read by
  bots on your own turns later, like any document a teammate edits. Check it
  before acting on it.
- Every bot reply runs on your own Claude account, so invited people spend your
  usage.
- Owner login works over the sharing address too, but the address travels
  through the tunnel provider. Prefer this machine for owner work.
