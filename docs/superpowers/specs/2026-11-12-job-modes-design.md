# Job modes and owner-only job changes

## Why

Scheduled jobs (core/jobs.py, dashboard/job_runner.py) run one way today: read-only. Every save a
job wants comes back as an APPROVAL line in its thread. That is safe, but a job that should simply
keep a draft list or a memo up to date needs a click every run, and a job that only reports still
produces approval cards.

Jobs are also created and changed by the bot itself: the bot proposes a job with an APPROVAL line
and, after a yes, runs `osctl create-job` or `update-job`. Nothing in code ties that command to the
owner's yes. Text a bot read (a web page, an email) can therefore create or change a job, and a job
runs again and again with the owner's budget.

## Goals

- Each job has a mode, chosen when it is created and changeable later:
  - **Approval** (default): reads and drafts. Every Leo Bot save and every app action waits for an
    Approve click in the job's thread. Same as today.
  - **Auto**: creates and updates inside Leo Bot by itself (memos, briefs, posts and their
    drafts, plans). Deletes and bot memory (`remember`) still wait for Approve: text a job read
    must not remove work or steer every later chat through memory. Anything that leaves the
    machine still waits for Approve: app writes (posting, sending email, any non-read app call)
    are held as today.
  - **Report only**: reads and writes a summary. No approval cards, no saves, no app writes.
- Only an owner action creates, changes or deletes a job: an Approve click on a job card in chat,
  or a control on the Team screen. No bot command does it directly.
- Easy: creating a job is a normal chat request that ends in one card; managing jobs is one row
  per job on the Team screen.

## Non-goals

- No mode that lets a job post, send or run any app write without a click. Auto never unlocks
  outward actions.
- No linked-folder edits from a job in any mode (jobs never get edit folders).
- No new screen: jobs live on the existing Team screen.
- No webhook or app-event triggers; schedules only.
- No ceiling on a job's cap beyond what the owner sets (owner and workspace caps still apply).

## Anti-goals

- A bot, or text a bot read, ending up with a job the owner never clicked.
- A mode switch that a bot can make by itself.
- Auto spending past the job's cap through model-running commands (generate-*, revise-post).

## Decisions

### Modes, as the turn sees them

| | Approval | Auto | Report only |
|---|---|---|---|
| osctl reads | yes | yes | yes |
| osctl creates and updates (content) | refused | yes | refused |
| osctl deletes, whole-document and section-list replaces, `remember` | refused | refused, APPROVAL line | refused |
| osctl job commands | refused | refused | refused |
| APPROVAL line | card | card | dropped, no card |
| App reads | yes | yes | yes |
| App writes | held, card | held, card | refused, not held |
| Linked-folder edits | no | no | no |
| Web | search only | search only | search only |

- Run status: `waiting` when the run left a card (APPROVAL or held app action), else `done`.
- Every model call a job causes counts against that job's cap, including model-running osctl
  commands in Auto (their spend rows carry the job id).
- The job's prompt note states its mode and what it may do, so the model does not attempt the
  refused paths.
- Existing jobs with no mode are read as Approval.

### Job cards in chat

- A bot proposes a new job, a change to one (schedule, message, mode, cap, skill, name), or a
  deletion. The proposal becomes a pending approval of a new kind, `job`, in that thread, shown as
  a card: name, bot and skill, schedule in words, next run, mode, cap, and the full message.
- Buttons: Approve, Edit (change the fields before approving), No. Approve makes the server
  create, change or delete the job, exactly once, after re-checking the bot, skill, workspace and
  engine as `create-job` does today. The card then shows the result.
- `create-job`, `update-job` and `delete-job` stop being bot commands. Bots get one proposal
  command; `list-jobs` stays. Proposals are refused in shared threads and in job turns of any mode.
- A proposal is untrusted input: the server validates every field, and the card shows exactly what
  Approve will save.
- The digest's "waiting for your approval" list counts job cards like the others.

### Team screen

- The "Background jobs" area gets one row per job: name, workspace and bot, schedule, next run,
  last run with its status and a link to its thread, mode picker, on/off switch, delete.
- Mode, on/off and delete change at once (owner only, workspace-scoped like other owner
  settings). Turning a job on re-checks it can run (bot active, skill owned, engine metered).
- Changes to schedule, message, cap or skill go through a bot and a card; the row says so.
- Invited people never see jobs or their rows.

## Implementation calls

- Proposal path: osctl `propose-job`. It validates the fields and writes a pending `job` row for
  the turn's thread (chat id, workspace and proposing bot come from the turn's environment or
  relay context, never from arguments). The model gets a bad schedule or skill back as an error at
  once and can fix it. At turn end the server collects the turn's job rows (this chat, created
  since the turn started), like held app actions, and shows them as cards. The server re-validates
  every field before showing a row and again on Approve, and Approve runs from the stored row, not
  from anything the bot sends later. A spoofed chat id in a Bash environment only puts a card in
  another of the owner's threads, where it still needs a click.
- Edit on a job card follows the app-action pattern: edit the stored fields (re-validated), then
  Approve runs what was claimed, and fails if it changed in between.
- A proposal's workspace: from a workspace thread, that workspace only; from HQ, any workspace
  (same rule `create-job --project` follows today).
- An update proposal stores the job id and only the changed fields; Approve fails if the job is
  gone, and applies those fields to the job as it is then.
- `create-job`, `update-job` and `delete-job` are removed from osctl entirely, the owner's terminal
  included: a bot's Bash osctl call cannot be told apart from the owner's. The owner manages jobs
  on the Team screen. `_job_target` moves from osctl to `core/jobs.py` (or server), shared by
  Approve and the Team screen on/off switch.
- Job rows behave like app-action rows: a new message in the thread does not resolve them, a newer
  APPROVAL line does not supersede them, `_save` keeps them while pending, and they expire after
  `APP_TTL`. The Approve endpoint is owner-only; invited people get 403 on it and never see the
  rows.
- A job turn is marked as one in every engine path, separate from `readonly`: a `job` field on
  `TurnPolicy`, the os_relay token context, and an `LEOBOT_JOB=<id>` environment variable for
  osctl. `propose-job` refuses when it is set (any mode), and when `LEOBOT_SHARED` is set.
- Auto turns drop `LEOBOT_READONLY`; Report only and Approval keep it. The read-only refusal text
  follows the mode (Approval: end with an APPROVAL line; Report only: put it in the summary).
- Report only refuses app writes at the relay instead of holding them (a flag on the apps token
  context).
- Auto spend: `generate.py` (and `revise-post`) read `LEOBOT_JOB`, check that job's cap with the
  owner and workspace caps before the model runs, and tag their ledger row with the job id in a new
  field (`for_job`), keeping `job: "generate"` as the category so the cost view and digest still
  group it. `spend.refusal(job=...)` and the digest count ledger rows by `for_job` too.
- The digest lists a job card by its summary (`New job: <name>`, `Change job: <name>`,
  `Delete job: <name>`).
- The mode lives on the job record (`mode`: `approval`, `auto`, `report`).
- Doc and prompt text that teaches `create-job` changes with the code: `CLAUDE.md` and
  `dashboard/ai_rules.py` (the content-write table row), `README.md`, the job comment in
  `dashboard/server.py`, and `JOB_NOTE` (one note per mode).
- Tests cover each mode's table row, the proposal and Approve path, the Team screen endpoints, and
  that no bot path creates, changes or deletes a job.
