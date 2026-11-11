# Cost Ledger and Caps Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every reply records its token parts, model, skill and model-pick reason; `/api/costs` and the Team view break spend down by engine, model, skill and day; owner and workspace daily caps refuse runs before any engine starts.

**Architecture:** Engines return a `usage` dict of four token parts next to today's summed `tokens`. A new `dashboard/engines/prices.py` owns the price table and the cost estimate. The server's `run()` helper stores the new fields on each reply. A new `dashboard/spend.py` owns cap values, rolling-24h sums from the per-thread `spend` ledger, refusal text and the once-per-day 80% notice. A new `dashboard/costs.py` owns the report that `server._costs` returns today.

**Tech Stack:** Python 3.10+ stdlib, `unittest`, vanilla JS in `dashboard/app.js`.

**Spec:** `docs/superpowers/specs/2026-11-11-harness-upgrade-design.md` (Phase 1)

## Global Constraints

- Python stdlib only. No new dependencies.
- `tokens` on a reply stays one integer (the UI and old threads read it). The four parts go in a new field `usage: {"in", "cache_write", "cache_read", "out"}`. This refines the spec's wording; Task 1 updates the spec line.
- Env vars: `LEOBOT_WORKSPACE_DAILY_CAP`, `LEOBOT_OWNER_DAILY_CAP`, dollars per rolling 24 hours. Unset, empty, `0` or not a number means no cap. `LEOBOT_PERSON_DAILY_CAP` keeps its current behavior and default ($5).
- Cache-write price is input price times 1.25 (as `api.py` does today).
- Runner replies (`engine.kind == "runner"`) show no cost, are never capped and never count.
- Notice threshold: 80% of a cap, at most once per cap key per 24 hours.
- The `job` field from the spec is Phase 3 work. Not added here.
- Public repo: fixtures use generic slugs only (`demo`, `acme`, `profile-a`). Run `python3 -m unittest tests.test_no_usage_data` before every commit.
- Commit messages: no `Co-Authored-By` trailer (the privacy guard rejects non-example emails in history).
- User-facing copy: no em dashes.
- Whole suite: `python3 -m unittest discover -t . -s tests`.

## Review Focus

1. **A thread recorded before this change** (`tokens` int, no `usage`, no `metered`, spend rows without `ws`) must still show in every grouping and never crash the report. Test in Task 5: `test_old_reply_without_new_fields`.
2. **A malformed cap env value** (`"abc"`, `"-3"`, `""`) means no cap, never a crash on every turn. Test in Task 3: `test_bad_cap_values_mean_no_cap`.
3. **A teammate refused mid-message** must leave the lead's streamed reply on screen and saved. The UI's `obj.error` handler overwrites the lead bubble, so the server sends `notice`, not `error`. Test in Task 4: `test_teammate_refused_keeps_lead_reply`.
4. **A runner turn while the owner is over cap** still runs (the person's own login). Test in Task 4: `test_runner_turn_ignores_caps`.
5. **The 80% notice after a server restart** must not repeat within 24 hours: the last-notice time lives in a file, not in memory. Test in Task 3: `test_notice_once_per_day_survives_reload`.

---

### Task 1: Token parts and cost estimate per engine

**Files:**
- Create: `dashboard/engines/prices.py`
- Modify: `dashboard/engines/claude_code.py:30-50` (`parse_event`), `dashboard/engines/codex.py:47-50`, `dashboard/engines/api.py:31-33` (move `PRICES`), `dashboard/engines/api.py:302-313` (`_done`), `dashboard/engines/api.py:449-454` (OpenAI usage loop), `dashboard/engines/acp.py:197`
- Modify: `docs/superpowers/specs/2026-11-11-harness-upgrade-design.md` (Phase 1 "Record": `tokens` stays the total, parts go in `usage`)
- Test: `tests/test_cost_ledger.py` (new)

**Interfaces:**
- Produces: `prices.PRICES: dict[str, tuple[float, float, float]]` (input, output, cache read per million, same values as today); `prices.estimate(model: str | None, usage: dict | None) -> float | None` (None when model unpriced or usage empty); every engine's `done` payload gains `"usage": {"in": int, "cache_write": int, "cache_read": int, "out": int} | None`. `api.PRICES` stays importable (re-export).

- [ ] **Step 1: Write the failing tests** in `tests/test_cost_ledger.py`, class `EngineUsage`:

```python
def test_claude_code_parts(self):
    _, p = claude_code.parse_event({"type": "result", "total_cost_usd": 0.01, "usage": {
        "input_tokens": 10, "cache_creation_input_tokens": 200,
        "cache_read_input_tokens": 1000, "output_tokens": 90}})
    self.assertEqual(p["usage"], {"in": 10, "cache_write": 200, "cache_read": 1000, "out": 90})
    self.assertEqual(p["tokens"], 1300)

def test_codex_cached_split(self):
    (_, p), = codex.parse_event({"type": "turn.completed", "usage": {
        "input_tokens": 100, "cached_input_tokens": 60, "output_tokens": 5}})
    self.assertEqual(p["usage"], {"in": 40, "cache_write": 0, "cache_read": 60, "out": 5})
    self.assertEqual(p["tokens"], 105)

def test_openai_compatible_cached_split(self):
    eng = api.ApiEngine.__new__(api.ApiEngine)
    _, p = eng._done(["x"], {"prompt_tokens": 50, "completion_tokens": 7, "cached_tokens": 20}, "some-model")
    self.assertEqual(p["usage"], {"in": 30, "cache_write": 0, "cache_read": 20, "out": 7})

def test_estimate(self):
    u = {"in": 1_000_000, "cache_write": 0, "cache_read": 0, "out": 0}
    self.assertAlmostEqual(prices.estimate("claude-haiku-5-5", u), 0.10)
    self.assertIsNone(prices.estimate("unknown-model", u))
    self.assertIsNone(prices.estimate("claude-haiku-5-5", None))
```

Use the real class name from `api.py` (the one with `kind = "api"` at line 106) in place of `ApiEngine` if it differs.

- [ ] **Step 2: Run to verify they fail**

Run: `python3 -m unittest tests.test_cost_ledger -v`
Expected: FAIL / ImportError on `prices`.

- [ ] **Step 3: Implement**

`prices.estimate`: `(in*p_in + cache_write*p_in*1.25 + cache_read*p_cache + out*p_out) / 1e6`. `api._done` builds `usage` from Anthropic keys when present, else from `prompt_tokens`/`completion_tokens`/`cached_tokens` (in = prompt minus cached), and uses `prices.estimate` when the provider sent no `cost`. In the OpenAI stream loop, also add `u.get("prompt_tokens_details", {}).get("cached_tokens")` into `usage["cached_tokens"]`. ACP's done payload gets `"usage": None`. `tokens` totals stay exactly as today (existing tests at `tests/test_approvals.py:57` and `tests/test_engines_cli.py:22` must still pass).

- [ ] **Step 4: Run tests**

Run: `python3 -m unittest tests.test_cost_ledger tests.test_approvals tests.test_engines_cli tests.test_chat_session -v`
Expected: PASS.

- [ ] **Step 5: Edit the spec line** in Phase 1 "Record" so it says `tokens` stays the total and the parts go in `usage`.

- [ ] **Step 6: Commit**

```bash
python3 -m unittest tests.test_no_usage_data
git add dashboard/engines tests/test_cost_ledger.py docs/superpowers/specs/2026-11-11-harness-upgrade-design.md
git commit -m "Engines return token parts; shared price table and cost estimate"
```

### Task 2: Model pick reasons and per-reply ledger fields

**Files:**
- Modify: `dashboard/server.py:830-857` (`_pick_model`), `dashboard/server.py:2490-2493` (lead pick), `dashboard/server.py:2614-2641` (`run`), `dashboard/server.py:2666-2667` (teammate pick), `dashboard/server.py:2791-2798` (`record_turn` call)
- Modify: `dashboard/chat_store.py:312-320` (spend row), `record_turn` signature
- Test: `tests/test_cost_ledger.py`

**Interfaces:**
- Consumes: Task 1 `prices.estimate`, `done` payload `usage`.
- Produces: `server._pick_model_reason(skill, web, user_msg="", session=None, *, explicit=False, editing=False) -> tuple[str, str]` returning `(model, reason)`; `_pick_model` keeps its signature and returns `_pick_model_reason(...)[0]`. Reason codes, one per branch in order: `edit-turn`, `tokensave`, `web`, `explicit-read`, `explicit-skill`, `skill-thread-read`, `skill-thread`, `default`; plus `app` (app turn), `teammate` and `teammate-skill` (teammate run). Each assistant message gains `usage`, `skill`, `reason`, `metered` (bool; omitted on runner replies). `chat_store.record_turn(..., ws=None)` writes `ws` into each `spend` row.

- [ ] **Step 1: Write the failing tests** (class `LedgerFields`):

```python
def test_reason_codes(self):
    self.assertEqual(server._pick_model_reason(None, False, "what is our plan?"), (chat_session.CHAT_MODEL, "default"))
    self.assertEqual(server._pick_model_reason(None, True, "x")[1], "web")
    self.assertEqual(server._pick_model_reason("strategy", False, "draft it", explicit=True)[1], "explicit-skill")

def test_reply_records_usage_skill_reason(self):
    # harness: copy Turn.ask from tests/test_approvals.py; fake done payload
    # {"tokens": 1300, "cost": 0.02, "usage": {"in": 100, "cache_write": 200, "cache_read": 1000, "out": 0}}
    _, rec = self.ask("@cmo positioning for acme", "ok")
    last = rec["messages"][-1]
    self.assertEqual(last["usage"]["cache_read"], 1000)
    self.assertEqual(last["tokens"], 1300)
    self.assertTrue(last["metered"])
    self.assertIn(last["reason"], {"explicit-skill", "explicit-read", "default"})
    self.assertEqual(rec["spend"][-1]["ws"], "acme")

def test_cost_estimated_when_engine_reports_none(self):
    with mock.patch.object(claude_code.ClaudeCodeEngine, "resolve_model", return_value="claude-haiku-5-5"):
        _, rec = self.ask("@cmo positioning for acme", "ok", done={
            "tokens": 1_000_000, "cost": None,
            "usage": {"in": 1_000_000, "cache_write": 0, "cache_read": 0, "out": 0}})
    last = rec["messages"][-1]
    self.assertAlmostEqual(last["cost"], 0.10)
    self.assertTrue(last["metered"])

def test_unpriced_reply_is_unmetered(self):
    _, rec = self.ask("@cmo positioning for acme", "ok", done={"tokens": 50, "cost": None, "usage": None})
    self.assertFalse(rec["messages"][-1]["metered"])
    self.assertNotIn("spend", rec)
```

Put the shared harness in a base class `AskHarness(unittest.TestCase)` in `tests/test_cost_ledger.py`, built from `Turn.ask` in `tests/test_approvals.py` (temp root with `projects/acme`, patched `CHATS_DIR`, `ROOT`, `db.tree`, `db.posts`, `get_chat_session`). It exposes:
- `ask(msg, replies, *, cost=0.02, usage=..., done=None) -> (sse_text, saved_record)`: `replies` is one string or a list, one per engine run in order (lead, teammate, follow-up), as `tests/test_teammates.py:100-140` scripts them; `done` overrides the whole fake payload.
- `engine_calls: int`: runs the fake session's `ask` started.
- `seed_spend(owner: float, ws: str)`: saves one thread whose `spend` holds a row `{"ts": now, "by": "owner", "cost": owner, "ws": ws}`.

Task 4 reuses it.

- [ ] **Step 2: Run to verify they fail**

Run: `python3 -m unittest tests.test_cost_ledger -v`
Expected: FAIL (`_pick_model_reason` missing, no `usage` on reply).

- [ ] **Step 3: Implement**

`run()` gains keyword args `skill_=None, reason_=None`. After the stream: `usage = fin.get("usage")`; `cost = fin.get("cost")`; if `cost is None`: `cost = prices.estimate(model_, usage)`; set `msg` fields `usage`, `cost`, `skill`, `reason`, and `metered = cost is not None` unless `eng.kind == "runner"`. The lead passes `(skill, reason)` from `_pick_model_reason` (or `"app"`), the follow-up reuses the lead's, the teammate passes `c_skill` and `teammate-skill`/`teammate`. Server passes `ws=ws` to `record_turn`.

- [ ] **Step 4: Run tests**

Run: `python3 -m unittest tests.test_cost_ledger tests.test_approvals tests.test_teammates tests.test_reactions -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
python3 -m unittest tests.test_no_usage_data
git add dashboard/server.py dashboard/chat_store.py tests/test_cost_ledger.py
git commit -m "Record token parts, skill, model-pick reason and metered flag per reply"
```

### Task 3: Spend module (caps, sums, refusal text, 80% notice)

**Files:**
- Create: `dashboard/spend.py`
- Test: `tests/test_spend_cap.py` (extend)

**Interfaces:**
- Consumes: `chat_store.records()`; spend rows `{"ts", "by", "cost", "ws"}` from Task 2.
- Produces:
  - `spend.cap_value(kind: str) -> float` for `kind` in `{"owner", "workspace"}`; 0.0 means no cap.
  - `spend.spent(kind: str, key: str | None, now: float | None = None) -> tuple[float, float | None]`: total over the last 86 400 s and the oldest counted row's `ts`. `owner`: rows with `by == "owner"`; `workspace`: rows with `ws == key`.
  - `spend.refusal(ws: str, by: str, now: float | None = None, pending: float = 0.0) -> str | None`: the first cap reached (`spent + pending >= cap`), workspace before owner; owner cap applies only when `by == "owner"`. `pending` is what this message already spent but has not saved yet (`record_turn` runs at the end of the message). Text names the cap, its amount and the local `HH:MM` when the oldest counted spend leaves the window (now + 24 h when only `pending` counts).
  - `spend.near_notice(ws: str, by: str, root: Path, now: float | None = None) -> str | None`: text when a cap is at 80% or more and that cap key (`"owner"` or `"ws:<ws>"`) has no notice in the last 24 h; records the time in `root / "team" / "cap_notices.json"` (atomic write, same pattern as `core/approvals.py:_save`).

- [ ] **Step 1: Write the failing tests** (class `Caps` in `tests/test_spend_cap.py`). Helpers in the class: `setUp` makes a temp dir `root` and patches `chat_store.CHATS_DIR` to `root / "chats"`; `save_rows(rows)` saves one thread with that `spend` list; `env(**kw)` is `mock.patch.dict(os.environ, kw)`; `now = time.time()`.

```python
def test_workspace_cap_counts_only_that_workspace(self):
    self.save_rows([{"ts": now, "by": "owner", "cost": 4.0, "ws": "acme"},
                    {"ts": now, "by": "owner", "cost": 9.0, "ws": "demo"}])
    with env(LEOBOT_WORKSPACE_DAILY_CAP="5"):
        self.assertIsNone(spend.refusal("acme", "owner"))
        self.assertIn("workspace", spend.refusal("demo", "owner"))

def test_owner_cap_skips_invited_people(self):
    self.save_rows([{"ts": now, "by": "owner", "cost": 6.0, "ws": "acme"}])
    with env(LEOBOT_OWNER_DAILY_CAP="5"):
        self.assertIsNotNone(spend.refusal("acme", "owner"))
        self.assertIsNone(spend.refusal("acme", "p1"))

def test_old_rows_leave_the_window(self):
    self.save_rows([{"ts": now - 90_000, "by": "owner", "cost": 99.0, "ws": "acme"}])
    with env(LEOBOT_OWNER_DAILY_CAP="5"):
        self.assertIsNone(spend.refusal("acme", "owner"))

def test_pending_spend_counts(self):
    with env(LEOBOT_OWNER_DAILY_CAP="5"):
        self.assertIsNone(spend.refusal("acme", "owner"))
        self.assertIsNotNone(spend.refusal("acme", "owner", pending=5.0))

def test_bad_cap_values_mean_no_cap(self):
    for v in ("abc", "-3", "", "0"):
        with env(LEOBOT_OWNER_DAILY_CAP=v):
            self.assertEqual(spend.cap_value("owner"), 0.0)

def test_notice_once_per_day_survives_reload(self):
    self.save_rows([{"ts": now, "by": "owner", "cost": 4.2, "ws": "acme"}])
    with env(LEOBOT_OWNER_DAILY_CAP="5"):
        self.assertIsNotNone(spend.near_notice("acme", "owner", root))
        importlib.reload(spend)
        self.assertIsNone(spend.near_notice("acme", "owner", root))
        self.assertIsNotNone(spend.near_notice("acme", "owner", root, now=time.time() + 90_000))
```

- [ ] **Step 2: Run to verify they fail**

Run: `python3 -m unittest tests.test_spend_cap -v`
Expected: FAIL / ImportError on `spend`.

- [ ] **Step 3: Implement `dashboard/spend.py`** with the signatures above. Refusal copy:
`"Paused: this workspace reached its daily cap ($5.00 per 24 hours). Room frees up from 14:05. Raise LEOBOT_WORKSPACE_DAILY_CAP to go on now."` (owner variant says "your daily cap" and names `LEOBOT_OWNER_DAILY_CAP`). Notice copy: `"Heads up: 84% of this workspace's daily cap is used."`

- [ ] **Step 4: Run tests**

Run: `python3 -m unittest tests.test_spend_cap -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
python3 -m unittest tests.test_no_usage_data
git add dashboard/spend.py tests/test_spend_cap.py
git commit -m "Owner and workspace daily caps with refusal text and a daily 80% notice"
```

### Task 4: Wire caps into the ask path

**Files:**
- Modify: `dashboard/server.py:2456-2462` (after engine pick, before the tag gate), `dashboard/server.py:2719-2731` (teammate loop and follow-up), `dashboard/server.py:2785-2801` (after `record_turn`, before `[DONE]`)
- Modify: `dashboard/app.js:5261` area (SSE handler: `obj.notice`)
- Test: `tests/test_cost_ledger.py`, `tests/test_people_ui.py` style string check in `tests/test_cost_ledger.py`

**Interfaces:**
- Consumes: Task 3 `spend.refusal`, `spend.near_notice`; Task 2 `AskHarness`.
- Produces: SSE event `{"notice": str}`; UI shows it as a `tool-chip` line under the reply, never replacing a bubble.

- [ ] **Step 1: Write the failing tests** (class `CapWiring`, using `AskHarness`):

```python
def test_capped_turn_never_starts_engine(self):
    self.seed_spend(owner=6.0, ws="acme")
    with env(LEOBOT_OWNER_DAILY_CAP="5"):
        out, _ = self.ask("@cmo positioning for acme", "should not run")
    self.assertIn("daily cap", out)
    self.assertEqual(self.engine_calls, 0)

def test_teammate_refused_keeps_lead_reply(self):
    # the lead's own cost (not yet saved) pushes the owner past the cap before the consult
    with env(LEOBOT_OWNER_DAILY_CAP="0.01"):
        out, rec = self.ask("@cmo plan for acme", ["Lead text.\nCONSULT @research: rivals?", "never"], cost=0.02)
    self.assertIn('"notice"', out)
    self.assertNotIn('"error"', out)
    self.assertEqual(self.engine_calls, 1)
    self.assertEqual(rec["messages"][-1]["content"], "Lead text.")

def test_runner_turn_ignores_caps(self):
    self.seed_spend(owner=6.0, ws="acme")
    with env(LEOBOT_OWNER_DAILY_CAP="5"), runner_online("owner"):
        self.ask("@cmo positioning for acme", "ran")
    self.assertEqual(self.engine_calls, 1)

def test_no_cap_set_changes_nothing(self):
    self.seed_spend(owner=600.0, ws="acme")
    self.ask("@cmo positioning for acme", "ran")
    self.assertEqual(self.engine_calls, 1)

def test_ui_handles_notice(self):
    self.assertIn("if (obj.notice)", JS)
```

The consult needs `research` active in the fixture workspace: use the `VENTURE` record from `tests/test_teammates.py` as the harness tree. `AskHarness` counts `engine_calls` in the fake session's `ask`. `runner_online` patches `server.runners.HUB.online` to return True and `server.RunnerEngine` to a fake with `kind = "runner"` whose session yields the same events.

- [ ] **Step 2: Run to verify they fail**

Run: `python3 -m unittest tests.test_cost_ledger.CapWiring -v`
Expected: FAIL.

- [ ] **Step 3: Implement**

- After the engine is picked: `if engine.kind != "runner"` and `spend.refusal(ws, sender)` returns text, `return self._sse_message(text)`.
- Before each teammate run and before the follow-up, same check with `pending=sum(m.get("cost") or 0 for m in msgs)`; on refusal `emit({"notice": text})` and stop planning further runs (no follow-up). Do not raise `_RunFailed`. Remove the refused teammate's empty `msg_start` (check before emitting it).
- After `record_turn`, `if engine.kind != "runner"`: `note = spend.near_notice(ws, sender, ROOT)`; emit it as `{"notice": note}` when set. Wrap in `try/except (OSError, ValueError)`.
- `app.js`: in the SSE loop add `if (obj.notice) {` create a `div.tool-chip` with `textContent = obj.notice`, append after the current reply. Keep the note on screen like the zero-model notes (`reload = stored` logic unchanged).

- [ ] **Step 4: Run tests**

Run: `python3 -m unittest tests.test_cost_ledger tests.test_spend_cap tests.test_approvals tests.test_teammates -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
python3 -m unittest tests.test_no_usage_data
git add dashboard/server.py dashboard/app.js tests/test_cost_ledger.py
git commit -m "Check caps before every engine run; notice instead of error mid-message"
```

### Task 5: Cost report groupings and Team view

**Files:**
- Create: `dashboard/costs.py`
- Modify: `dashboard/server.py:1288-1305` (`_costs` becomes a one-line wrapper), `dashboard/app.js:1264-1268` (team total)
- Test: `tests/test_cost_ledger.py`

**Interfaces:**
- Consumes: reply fields from Task 2.
- Produces: `costs.summarize(ws: str, records: list[dict], spaces: set[str], days: int = 30, now: float | None = None) -> dict` with today's keys (`days`, `bots`, `total`) plus `by_engine`, `by_model`, `by_skill`, `by_day` (each `{key: {"cost": float, "tokens": int, "turns": int}}`; missing key groups under `"-"`; day key `YYYY-MM-DD` local), `cache_read_share: float | None` (cache_read / (in + cache_write + cache_read) over replies with `usage`, None when none have it), `unmetered: int` (replies with `metered is False`). `server._costs(ws, projects, days=30)` keeps its signature and output for existing callers.

- [ ] **Step 1: Write the failing tests** (class `Report`):

```python
def test_groupings_and_cache_share(self):
    recs = [thread("acme", [reply("cmo", engine="claude-code", model="haiku", skill="strategy",
                                  cost=0.02, tokens=1300, usage={"in": 100, "cache_write": 200, "cache_read": 1000, "out": 0}),
                            reply("cfo", engine="codex", model="gpt", skill=None, cost=None,
                                  tokens=50, usage=None, metered=False)])]
    out = costs.summarize("acme", recs, {"acme"})
    self.assertAlmostEqual(out["by_model"]["haiku"]["cost"], 0.02)
    self.assertEqual(out["by_skill"]["-"]["turns"], 1)
    self.assertAlmostEqual(out["cache_read_share"], 1000 / 1300)
    self.assertEqual(out["unmetered"], 1)

def test_old_reply_without_new_fields(self):
    recs = [thread("acme", [{"role": "assistant", "bot": "cmo", "ts": now, "tokens": 1500, "cost": 0.02}])]
    out = costs.summarize("acme", recs, {"acme"})
    self.assertEqual(out["total"]["tokens"], 1500)
    self.assertEqual(out["by_engine"]["-"]["turns"], 1)
    self.assertIsNone(out["cache_read_share"])
    self.assertEqual(out["unmetered"], 0)

def test_ui_shows_breakdown(self):
    for s in ("by_model", "by_skill", "cache_read_share", "unmetered"):
        self.assertIn(s, JS)
```

`thread(ws, msgs)` builds a record with `room = f"{ws}/general"`; `reply(...)` builds an assistant message with `ts = now`. Existing `tests/test_approvals.py:129-133` and `tests/test_reactions.py:172` must keep passing.

- [ ] **Step 2: Run to verify they fail**

Run: `python3 -m unittest tests.test_cost_ledger.Report -v`
Expected: FAIL / ImportError on `costs`.

- [ ] **Step 3: Implement**

Move the loop from `server._costs` into `costs.summarize`, keeping the workspace filter (`rooms.room_of_chat` and `rooms.split_id`, HQ sees all). `server._costs` calls `costs.summarize(ws, chat_store.records(), {w["id"] for w in _workspaces(projects)}, days)`. In `app.js`, under the existing `team-total` line add a `<details class="team-spend">` (owner only, hidden for invited people like the total) with: top 5 of `by_model`, `by_skill`, `by_engine` as `name $cost · N replies`, last 7 days of `by_day`, `Cache reads: N% of input` when `cache_read_share` is not null, and `N replies on engines that report no cost` when `unmetered`. Label costs "(est.)" as the total line does.

- [ ] **Step 4: Run tests**

Run: `python3 -m unittest discover -t . -s tests`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
python3 -m unittest tests.test_no_usage_data
git add dashboard/costs.py dashboard/server.py dashboard/app.js tests/test_cost_ledger.py
git commit -m "Cost report by engine, model, skill and day, with cache share and unmetered count"
```

### Task 6: Live check and docs

**Files:**
- Modify: `README.md` (new short "Spending caps" section; no Markdown file documents `LEOBOT_PERSON_DAILY_CAP` today)

- [ ] **Step 1:** Add a "Spending caps" section to `README.md` listing all three env vars, one line each: `LEOBOT_PERSON_DAILY_CAP` (default 5, per invited person), `LEOBOT_WORKSPACE_DAILY_CAP` and `LEOBOT_OWNER_DAILY_CAP` (default off). Say: dollars per rolling 24 hours, estimated from list prices, runner turns never count, 0 turns a cap off.
- [ ] **Step 2:** Start the dashboard against a temp data folder, send one message, open Team: the breakdown shows the new reply under its model and skill. Set `LEOBOT_OWNER_DAILY_CAP=0.0001`, restart, send a message: the refusal shows and no engine process starts (`pgrep -f "claude -p"` stays empty).
- [ ] **Step 3:** Run `python3 -m unittest discover -t . -s tests` and `python3 -m unittest tests.test_no_usage_data`. Expected: PASS.
- [ ] **Step 4: Commit**

```bash
git add README.md
git commit -m "Document owner and workspace daily caps"
```
