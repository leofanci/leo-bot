# Security model

Leo Bot runs bots that read things other people wrote (mail, web pages, app data, an invited
person's messages) and can act: send from connected apps, type into web pages, run shell
commands in linked folders. This page says what Leo Bot trusts, what it defends against, how,
and what it does not cover. To report a vulnerability, see [SECURITY.md](../SECURITY.md).

## Who and what is trusted

| Party | Trusted? | Why |
|---|---|---|
| The owner (the computer account Leo Bot runs as) | Yes | Owns the data folder, the login token and every setting. |
| Invited people | No | They prompt bots on the owner's machine. Their threads run with no tools beyond the workspace relay ([sharing](sharing.md)). |
| Outside content: mail, pages, app data, guests' messages, files labeled as outside | No | Any of it can carry instructions written for the bot (prompt injection). |
| Bots (the models) | No | A bot does what the text it read says as readily as what the owner said. Every rule below is code around the model, never an instruction to it. |
| Model providers, Composio, GitHub | Partly | They see what is sent to them (see "Where data goes"). |

## Threats and controls

### 1. Malicious content steering a bot (prompt injection)

The rule: no conversation holds private data, outside content and a way out at once without a
click from the owner. Each piece is enforced in code at the moment a tool runs.

- **Outside content is tracked.** Each request carries a taint record (`dashboard/taint.py`).
  An app read, a web search or page, a guest's message or a file labeled as outside marks it,
  and the mark never clears within that request.
- **Saved words keep their origin.** Files a tainted request writes (docs, memos, bot memory)
  are labeled (`core/provenance.py`). While a workspace holds a labeled file the owner has not
  trusted, every conversation there counts as holding outside content.
- **Nothing in content raises autonomy.** Job modes, access levels, app grants and browser
  levels change only by the owner's hand. There is no "emergency" or "urgent" mode that a
  message could trigger.
- **New destinations wait.** "Sends without asking" and "Acts on its own" still hold a send for
  one click when the request read outside content and the send names an address, phone number,
  host, channel or recipient this workspace never used (`core/destinations.py`). A browser at
  "Acts on its own" sends without a plan only on sites the owner listed or approved before.
- **Some actions always ask.** Payments, ads, deletes, code and query runs, sharing and access
  changes wait for a click on every grant (`core/app_rules.py`).
- **Web fetches stay on typed hosts.** A fetch reaches only addresses the owner typed; a page
  cannot steer it elsewhere.
- **Shell after outside content runs offline.** Allowed commands run in the OS sandbox with no
  network and writes limited to edit folders (`dashboard/shell_rules.py`). Named sending
  commands are refused.
- **Auto jobs stop saving.** A scheduled job in Auto that read outside content saves nothing by
  itself; it asks once.

### 2. Secrets reaching a model

- Bots never see web login passwords. The owner logs a bot in by hand in a visible window;
  the bot gets the site's cookies, encrypted at rest with a key in `<data>/run/`
  ([browser](browser.md)). Proxy passwords never reach the browser engine or its environment.
- API keys and tokens live in `<data>/team/` and `<data>/run/` (folder 0700, files 0600) and
  are never put in a prompt. `osctl read-file` refuses those folders and secret file names.
- Bots never type a stored secret: there is no secret store a bot can read from.

Limit: secrets are files protected by the computer account and disk encryption, not a hardware
key store. Anyone who can read the owner's files can read them.

### 3. Stop everything (kill switch)

The stop button at the bottom of the workspace bar, `Ctrl+Shift+.`, or `leobot halt` in a
terminal:

- ends every bot run in flight: the owner's, invited people's, the scheduled job, runner turns
  and every bot browser;
- revokes every turn token, so a tool call already on its way is refused at its relay;
- keeps everything stopped, across restarts, until Resume (top bar) or `leobot resume`.

No model takes part: it is a flag file and token revocation (`core/halt.py`).

### 4. Action log

Every app write that ran, every browser send, every shell command a gate allowed, and every
stop and resume is appended to `<data>/team/actions/<YYYY-MM>.jsonl`. No setting turns it off.
Each line carries the hash of the line before it, so a removed or edited line is found by
`leobot actions --verify`. `leobot actions` prints the newest entries. The log shows tampering
after the fact; it cannot stop someone with the owner's account from replacing the whole folder.

### 5. Poisoned memory

Bot memory is a file like any other: a tainted request that writes to it labels it (see 1), and
an invited person's thread cannot write the owner's private memory at all.

### 6. Runaway cost

Daily caps per invited person (on by default), per workspace and for the owner's own turns, and
an optional cap per scheduled job (Settings, General; `LEOBOT_*_DAILY_CAP`). A job also stops
after 10 idle minutes or 6 hours.

### 7. A lost or stolen computer

Leo Bot's data folder (`~/.leobot/data`) holds conversations, memory, keys and logins in
files readable by the owner's account. Turn on disk encryption (FileVault on a Mac, LUKS on
Linux). After a loss: revoke the API keys and app connections at their providers, run
`leobot rotate-token` on a server, and log the bots out of their sites.

### 8. Malware on the same computer

Out of scope. Code running as the owner can read the data folder, the keys and the browser
logins. Leo Bot does not defend against its own account.

### 9. A server reachable from the internet

Leo Bot listens on `127.0.0.1` only. The owner reaches a server over SSH; invited people reach
it through an https address the owner sets up ([server](server.md), [sharing](sharing.md)).
Owner routes refuse requests without the owner's cookie and a matching origin; people's
routes are limited to their workspaces. On a server the owner answers only through the SSH
connection: a request through the sharing address is refused for the owner unless
`LEOBOT_OWNER_REMOTE=1`. Answers over https carry HSTS. The hosting company can read the
server's disk, keys included. Hardening the machine itself (SSH keys only, firewall,
updates) is the owner's job: [server](server.md#security-on-a-server).

### 10. Updates

`leobot update` takes code from the `origin` repository over https: release tags on the stable
channel, `main` on the main channel. It backs up the data first and rolls back when the new
version does not start. The browser engine is a pinned release checked against a SHA-256 hash.
Trust in an update is trust in the repository's owner account.

## Where data goes

| Data | Goes to | Stays local when |
|---|---|---|
| What a bot reads and writes in a conversation | The model provider of the bot's engine (Claude Code, Codex, an API key) | The bot uses a local model (an Ollama or other local address in Engines) |
| Connected-app calls and their results | Composio and the app | Never: apps are remote |
| Web searches and pages | The search provider and the sites | Never |
| Update checks | GitHub (`git ls-remote`: the machine's address only) | `leobot update --auto-check off` |
| Everything else (data folder, action log, memory) | Nowhere | Always |

## Known limits

- A bot that already holds private data and an allowed way out (a known destination at "Sends
  without asking") can still send that data there: the owner chose that level of trust.
- Tamper evidence in the action log is not tamper proof (see 4).
- Secrets are protected by the computer account, not a hardware key store (see 2).
