# Running Leo Bot on a server

Run Leo Bot on a small server and it stays on when your laptop is closed: bots keep
working, people you invite can reach it, and you open it from anywhere.

There are three ways to run Leo Bot. They use the same code.

| | Where Leo Bot runs | How you open it | What runs your bots |
|---|---|---|---|
| **On your Mac** | your Mac | the app | your Claude login or an API key |
| **On a server** (this guide) | a server | your browser, over SSH | your Claude login on the server, or an API key |
| **Server + runners** | a server | your browser | each person's own Mac, on their own login ([below](#people-using-their-own-subscription)) |

Whichever you pick, invited people's messages never run on your Claude login: they
use an API key you choose in Settings, Bots, or their own runner.

## 1. Set up the server

Any Debian or Ubuntu server with 1 GB of memory is enough (Hetzner, DigitalOcean, a
spare machine). From a checkout of this repo on your laptop:

```sh
ssh root@your-server "LEOBOT_REPO_URL=$(git remote get-url origin) bash -s" < scripts/server-setup.sh
```

This creates a `leobot` user, turns on `loginctl enable-linger` for it, and runs the Leo Bot
installer (`install.sh --no-service --no-app --yes`) as that user, moves any legacy data, then
runs `leobot service install --port 8766 --server`. Leo Bot lands in `~leobot/.leobot`
(code in `app/`, data in `data/`) and runs as a systemd user unit named `leobot`, which
restarts on its own and starts at boot. It follows the latest release; set `LEOBOT_CHANNEL=main`
for the latest code. Leo Bot listens on `127.0.0.1:8766` only, so nothing is open to the
internet yet. Read the script first if you like.

Upgrading an older server: the script removes the old system unit
(`/etc/systemd/system/leobot.service`), moves your data from `~leobot/leo-bot` into
`~leobot/.leobot/data` (with a backup first), and leaves the old folder in place. Delete it
yourself once you have checked your data.

## 2. Log in to Claude (optional)

For your own messages to run on your Claude subscription:

```sh
sudo -u leobot -i claude auth login
```

Prefer API keys only? Skip this and add a key in Settings, Engines.

## 3. Open it from your laptop

From a checkout of this repo on your laptop:

```sh
scripts/leobot-connect.sh root@your-server
```

Your browser opens Leo Bot through the SSH connection. Every owner screen works,
including the terminal, because to Leo Bot this counts as this machine. Close it with
Ctrl-C; Leo Bot keeps running on the server.

## 4. Move your data

Once, from your Mac:

```sh
scripts/sync-to-server.sh root@your-server
```

This copies this Mac's data folder (`~/.leobot/data`: projects, chats, team and portfolio) to
`~leobot/.leobot/data` on the server. Saved chats carry on with a
recap of their messages. Linked folders are not copied: clone your repos on the
server and link them there, on the workspace's Folders screen (type the path; there
is no folder chooser on a server). Paths synced from your Mac show as "Missing on
this machine" until you replace them. After moving, use the server copy only, or the
two will drift apart.

## 5. Invite people

Add a public https address, then invite as usual: [Sharing a workspace](sharing.md).
On a server you have one more option, your own domain with Caddy:

```
leobot.example {
    reverse_proxy 127.0.0.1:8766
}
```

Then Settings, Bots: pick the API key that answers invited people.

## People using their own subscription

A teammate who wants to use their own Claude or Codex login connects their Mac as a
**runner**: their messages run on their machine, on their login, and the answers
come back to the shared workspace. See [Runners](runners.md).

## Updating

From Settings in Leo Bot (the update button), or on the server:

```sh
sudo -iu leobot ~leobot/.local/bin/leobot update
```

`-i` gives the command the `leobot` user's own home folder. Leo Bot finds that user's service
manager by itself (it uses `/run/user/<uid>`, which `enable-linger` keeps), and if the service
cannot be stopped the update stops before changing anything and says why. It backs up your data,
moves to the latest release and restarts the service. Running the setup script again does the same.

## Managing the service

The `leobot` user can stop, start and update its own service without sudo. As that user
(`sudo -u leobot -i`):

```sh
leobot status
leobot service status
leobot service install --port 8766 --server   # reinstall the unit
leobot doctor                                 # checks folders, port, version
```

## Backups

Everything of yours is in `~leobot/.leobot/data` (`projects/`, `chats/`, `team/`, `portfolio/`).
`leobot backup` snapshots it into `~leobot/.leobot/backups/`, and an update takes one first.
Copy that folder off the server yourself. `team/` holds your API keys, so keep the backup private.

## Good to know

- The service runs as its own `leobot` user, which holds only Leo Bot, so a bot's reach
  on the server ends at that user's files.
- The login token stays the same across restarts on a server. To change it, delete
  `~leobot/.leobot/data/team/owner-token` and restart: `leobot stop && leobot start` as the `leobot` user.
- Logs: `~leobot/.leobot/logs/server.log`, or `journalctl --user -u leobot -f` as the `leobot` user.
