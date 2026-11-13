# Results loop and live assumptions

## Why

Leo Bot plans, drafts and tracks, but nothing feeds real results back. An experiment carries
`success_criteria` and `kill_criteria` (core/project_schemas.py), yet a person has to read the
numbers, decide, and edit the result by hand. Memos name "what would change this recommendation",
yet that line is prose: when evidence arrives, nothing says which recommendations it breaks.

The result is strategy that goes stale. This spec closes the loop in two parts that ship in order:

1. **Results loop**: pull metrics from connected apps, score experiments against their criteria,
   write the evidence, and ask the owner before any verdict is saved.
2. **Live assumptions**: turn each memo's "what would change this" into tracked assumptions, and
   flag memos whose assumptions new evidence contradicts.

## Goals

- An experiment can name a **metric source**: an app action (read-only) plus the number to read.
- A scheduled job (existing core/jobs.py) reads the source, compares to the criteria, and proposes
  a verdict: `on track`, `met`, `killed`, or `inconclusive`, with the numbers shown.
- A proposed verdict is a card. Owner Approve writes the result to the experiment and appends an
  entry to the project's evidence log (intake). Nothing is saved before the click.
- A memo's "what would change this" lines become assumptions with a stable id, a status
  (`holding`, `contradicted`, `unknown`), and the evidence entries that bear on each.
- When an approved verdict or evidence entry contradicts an assumption, affected memos show a
  **stale** flag and the bot offers a new memo version (existing `update-memo`/`create-memo` path).
- Everything stays in files. The SQLite index remains derived and disposable.

## Non-goals

- No automatic verdict, memo rewrite or kill without a click. Auto job mode never unlocks this.
- No metric writes, posting or app writes of any kind: sources are read-only actions only.
- No new analytics store or charts. Numbers live in the evidence entry as plain text with a date.
- No cross-project learning or hour allocation (later spec).
- No change to existing experiment, memo or intake shapes beyond added optional fields.

## Anti-goals

- Text a metric source returned (a page, a post caption) steering a verdict or a memo edit. Source
  output is data: it fills the numbers field only and is never read as instructions.
- A job spending past its cap while reading sources.
- A contradicted assumption silently rewriting a recommendation.

## Decisions

### Metric source (part 1)

New optional experiment field `metric`:

```json
{
  "metric": {
    "app": "<connected app slug>",
    "action": "<read-only action>",
    "args": {},
    "path": "<dotted path to the number in the action result>",
    "window_days": 7
  }
}
```

- Only actions the app rules mark read-only are accepted (core/app_rules.py); anything else is
  rejected at save time with the reason.
- `success_criteria` and `kill_criteria` stay free text. The job compares the number to them by
  asking the engine to judge, then shows the number, the criteria and the reason side by side.
  The owner sees the arithmetic, not only the verdict.
- A missing source, failed read or unparseable number yields `inconclusive` with the error, never
  a guess.
- Checked against a real read-only result (an account-info read on a connected social app). The
  result is an object `{"data": {...}, "error": null, "log_id": "...", "result_type": "..."}`; it
  has no `successful` key, so a failure shows as a non-null `error` or a `result_type` of failed
  or blocked. Counts are plain numbers under `data` (for example `data.followers_count`), so a
  dotted path works. Some list reads nest one level deeper (`data.data.0.values.0.value`), which
  the digit segments already address.

### Scoring job

- Reuses the job runner: a normal job whose skill is `score-experiments`, scoped to one project,
  default Report-only mode (reads and writes a summary). Approval mode attaches the verdict cards.
- Per experiment per run: read source, store nothing yet, build one card.
- A card carries: experiment id (composed, e.g. `pr1.sec03.ex1`), numbers with window and date,
  verdict, one-line reason. Approve calls `update-experiment` and `add-evidence`.
- A verdict card is not repeated while the same numbers and verdict are already pending.
- Job cap counts the source reads and the judging turn through the existing live spend tally.

### Evidence log

- Intake already holds an evidence log. `add-evidence --project <slug> --text "..." [--ref <id>]
  [--date]` appends one dated entry. `--ref` is the composed id of the experiment or assumption it
  bears on.
- Entries are append-only. Corrections are a new entry referencing the old one.

### Live assumptions (part 2)

- Each memo type already ends with "what would change this". New `assumptions` array in the memo
  body, each item: `{id, text, status, refs}`. Ids are composed under the memo, for example
  `pr1.sec04.mm1.as2`, via core/ids.py.
- Existing memos are not rewritten. A one-time, owner-approved `extract-assumptions --project
  <slug> --type <memo-type> --version <N>` proposes assumptions from the existing prose; save is
  a normal write-gate step.
- `contradict-assumption` is not a bot command. A status change to `contradicted` happens only
  through an approved card produced when an evidence entry is added with `--ref` to an assumption
  and the judge says it contradicts. Same Approve rule as part 1.
- A memo with any `contradicted` assumption gets `stale: true` in the dashboard and in the project
  snapshot given to chat. The bot's next consultant answer states which assumption broke and
  offers a new version. It does not write one unasked.

### Surfaces

- Experiment row: metric chip (source, last number, date) and verdict state.
- Memo header: stale flag with the assumption that caused it, linking to the evidence entry.
- Team screen job row: unchanged, the scoring job is an ordinary job.
- osctl: `set-metric`, `add-evidence`, `extract-assumptions`, `list-assumptions`. All mutating
  commands follow the write-gate table; none are available to a shared or read-only turn.

### Safety

- Metric reads run through the existing connected-app path, so owner caps, per-workspace account
  choice and the read-only check all apply unchanged.
- Source output is truncated and quoted into the judge as data. The judge prompt forbids acting on
  it and the card shows the raw number so the owner can check it.
- Verdict and assumption writes are owner-only actions, like job changes (spec 2026-11-12 job
  modes): an Approve click on a card, never a bot command.

## Build order

1. `metric` field, validation against read-only actions, `set-metric`.
2. `add-evidence` and evidence refs in IDs.
3. `score-experiments` skill, verdict cards, Approve path.
4. Assumptions field, ids, `extract-assumptions`, `list-assumptions`.
5. Stale flag in dashboard and snapshot, contradiction card.
6. Docs in `docs/guide.md`, README line.

Each step is its own PR with tests. Fixtures use generic slugs only (`demo`, `acme`).

## Tests

- Metric: rejects non-read-only action; accepts read-only; bad path yields `inconclusive`.
- Scoring: card per experiment; no card repeated for identical numbers; cap stops a run.
- Approve writes the result and evidence once; Reject writes nothing.
- Source text containing instructions changes nothing but the numbers field.
- Assumptions: ids stable across memo versions; contradiction needs an approved card; stale flag
  clears when a new version replaces the contradicted assumption.
- Shared and read-only turns cannot call any new mutating command.
- `tests/test_no_usage_data.py` passes.

## Open questions

- Live check of the Composio key is still pending; part 1 step 3 needs one real read-only action
  to confirm the result shape before the `path` syntax is final.
- Whether `window_days` should be fixed per source or set per experiment. Start per experiment.
