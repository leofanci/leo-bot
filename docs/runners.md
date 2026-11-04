# Runners: your messages on your own Mac

When myOS lives on a server, a person can connect their own Mac. Their messages
then run on that Mac, on their own Claude login, and the answers come back to the
shared workspace. No port is opened on the Mac: it connects out to the server.

## Connect

1. In myOS: **Connect a Mac** (HQ, Engines for the owner; Team for invited people).
   Copy the two commands. The token is shown once.
2. On your Mac, inside a checkout of this repo, with Claude Code logged in
   (`claude auth status`), run them. Keep that terminal open.

Your messages now run there. Each reply says "via your Mac". Stop it with Ctrl-C and
your messages go back to the server's engine: for invited people, the API key the
owner picked, or a note saying the runner is off.

The server needs a public address for this ([sharing](sharing.md)), or the owner can
run a runner over the SSH connection.

## What a bot can do on your Mac

Nothing on the Mac itself. A runner turn has no shell, no file tools and no connected
apps. Its one myOS tool is osctl, which runs on the server, in that workspace. The
same rule as any shared thread, checked the same way.

## Good to know

- A turn that needs connected apps runs on the server, not the Mac.
- `generate-*` jobs (content plans, briefs) run on the server's engine: the owner's
  for the owner, the people API key for invited people.
- A new Mac starts a fresh session with a recap of the thread's messages.
- Remove a runner in the same place you connected it. A turn in flight stops at once.
