# Running myOS on a server

Run myOS on a small server and it stays on when your laptop is closed: bots keep
working, people you invite can reach it, and you open it from anywhere.

There are three ways to run myOS. They use the same code.

| | Where myOS runs | How you open it | What runs your bots |
|---|---|---|---|
| **On your Mac** | your Mac | the app | your Claude login or an API key |
| **On a server** (this guide) | a server | your browser, over SSH | your Claude login on the server, or an API key |
| **Server + runners** | a server | your browser | each person's own Mac, on their own login ([below](#people-using-their-own-subscription)) |

Whichever you pick, invited people's messages never run on your Claude login: they
use an API key you choose in HQ, Engines, or their own runner.

## 1. Set up the server

Any Debian or Ubuntu server with 1 GB of memory is enough (Hetzner, DigitalOcean, a
spare machine). From a checkout of this repo on your laptop:

```sh
ssh root@your-server "MYOS_REPO=$(git remote get-url origin) bash -s" < scripts/server-setup.sh
```

This creates a `myos` user, puts the code in its home folder, installs Claude Code
and starts a service that restarts on its own. myOS listens on `127.0.0.1:8766` only,
so nothing is open to the internet yet. Read the script first if you like: it is short.

## 2. Log in to Claude (optional)

For your own messages to run on your Claude subscription:

```sh
sudo -u myos -i claude auth login
```

Prefer API keys only? Skip this and add a key in HQ, Engines.

## 3. Open it from your laptop

From a checkout of this repo on your laptop:

```sh
scripts/myos-connect.sh root@your-server
```

Your browser opens myOS through the SSH connection. Every owner screen works,
including the terminal, because to myOS this counts as this machine. Close it with
Ctrl-C; myOS keeps running on the server.

## 4. Move your data

Once, from your Mac:

```sh
scripts/sync-to-server.sh root@your-server
```

This copies your projects, chats, team and portfolio. Saved chats carry on with a
recap of their messages. Linked app folders need to be on the server and linked
again. After moving, use the server copy only, or the two will drift apart.

## 5. Invite people

Add a public https address, then invite as usual: [Sharing a workspace](sharing.md).
On a server you have one more option, your own domain with Caddy:

```
myos.example {
    reverse_proxy 127.0.0.1:8766
}
```

Then HQ, Engines: pick the API key that answers invited people.

## People using their own subscription

A teammate who wants to use their own Claude or Codex login connects their Mac as a
**runner**: their messages run on their machine, on their login, and the answers
come back to the shared workspace. See [Runners](runners.md).

## No hosted version yet

There is no hosted myOS yet, but there will be. For now you run it yourself, on your
Mac or your own server. HQ, Engines shows where this copy runs. `MYOS_CLOUD_URL` is
reserved for the hosted service. For now it changes nothing you can see: myOS has no
Cloud screen and makes no Cloud request.

## Updating

Run the setup script again. It pulls the latest code and restarts the service.

## Backups

Everything of yours is in `projects/`, `chats/`, `team/` and `portfolio/` under
the `myos` user's home folder. Back those folders up. `team/` holds your API keys, so keep the
backup private.

## Good to know

- The service runs as its own `myos` user, which holds only myOS, so a bot's reach
  on the server ends at that user's files.
- The login token stays the same across restarts on a server. To change it, delete
  `team/owner-token` and restart: `systemctl restart myos`.
- Logs: `journalctl -u myos -f`.
