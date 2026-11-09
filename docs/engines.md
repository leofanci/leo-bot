# Engines: what runs your bots

Pick them in Settings, Engines. Each reply says what it ran on.

| Engine | Login | Where it can run | Who it can answer |
|---|---|---|---|
| **Claude Code** | your Claude subscription or a key | Mac, server, runner | you |
| **API key**: Anthropic, OpenAI, OpenRouter, Ollama, any OpenAI-compatible address | a key you add | Mac, server | you and invited people |
| **Codex** | your ChatGPT login | your own Mac | you, in private threads |
| **Other CLIs** over ACP: Gemini CLI, OpenCode | that CLI's login | your own Mac | you, in private threads |
| **Runner** ([docs](runners.md)) | a person's own Claude login | their Mac | that person |

## Who runs on what

- Your messages: your default engine. A workspace can have its own.
- Invited people: the API key you pick under **Invited people**, or their own runner.
  With neither, bots tell them so and nothing is spent. They never use your login.
- Connected apps: Claude Code or an API key.
- Content jobs (plans, briefs): the engine of the turn that started them. Codex and other
  CLIs keep tools of their own, so jobs started there use Claude Code or your people key.

## Why Codex and other CLIs stay on your Mac

Those CLIs keep tools of their own that Leo Bot can't fully switch off, so a bot on one
may be able to read any file your user can. They run only where that equals you running
the CLI in your own terminal: your private threads, on your own machine, with no
connected apps. A thread anyone else can see moves to the people key, or tells you to
pick one. Claude Code and API keys are locked down in shared threads (see
[sharing](sharing.md)).

## Linked folders

On Claude Code and API-key engines, bots can change files in a workspace's Edit folders
on a tagged message, never git, editor or secret files. **Codex and other CLIs over ACP
are read-only in linked folders**: their own tools have no per-folder rule for that
never-write list, so Leo Bot never gives them edit access, and the Engines screen says so.

## Keys

Keys live in `team/engines.json`, owner-only. No bot can read that file and no screen
shows more than the last four characters.
