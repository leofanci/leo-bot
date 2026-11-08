---
name: folder-work
description: Read and change code and docs in the workspace's linked folders. Use for a fix, refactor, rename or small feature in a linked folder with edit access.
---

# Folder work

The linked folders for this turn, with their access, are listed under "Linked folders".

1. Before working in a folder, read its `CLAUDE.md` or `AGENTS.md` if it has one, and follow it.
2. Find the code first: search and read only what the change needs.
3. Make focused changes in folders with edit access only. A read-only folder is for reading.
4. Never touch git, editor or secret files (`.git/`, `.claude/`, `.vscode/`, `.env` and the like):
   they are refused anyway.
5. In the reply, name every file you changed, one per line, with one line on what changed.
6. You cannot run tests, builds or git. Say which commands the owner should run to check the change.

If this turn was not tagged, change nothing: say what you would change and ask to be tagged.
