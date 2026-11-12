# Leaner turns (harness upgrade phase 4) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Cut tokens per turn and route untagged messages better: prompt caching on the Anthropic API engine, history trimmed at a stable cut, thread compaction at a context threshold, routing by wording, and a cost view that shows why each model was picked.

**Architecture:** Engines report the size of their last model call (`context`) in the done payload; the server saves it on the reply. Before a turn, if the thread's last reply on that engine passed `LEOBOT_COMPACT_TOKENS`, the server asks the engine to drop its session and sends the existing recap of visible messages instead. The API engine handles the same threshold itself by trimming the resent history at a cut kept in its engine state, so the cached prefix stays the same for many turns. Routing stays a pure string classifier.

**Tech Stack:** Python 3 stdlib, unittest, vanilla JS dashboard.

**Spec:** `docs/superpowers/specs/2026-11-11-harness-upgrade-design.md` (Phase 4)

## Global Constraints

- Public repo: no real venture, profile, product or channel names, and no usage numbers from real threads, in code, tests, docs or commits. Fixtures use `demo`, `acme`, `profile-a`, `profile-b`. `tests/test_no_usage_data.py` passes before every commit.
- No test calls a real engine. API tests use the fake provider in `tests/test_engines_api.py`.
- Workspace isolation, the shared-thread lockdown, the write gate and the cheap-model rule do not change.
- No commit trailer with an email (`Co-Authored-By` line is not added).
- Compaction threshold: `LEOBOT_COMPACT_TOKENS`, default `60000`, `0` turns compaction and API trimming-by-threshold off (the API engine then keeps today's `HISTORY_CHARS = 400_000`).
- Run evals before and after the routing change: `python -m evals`.

## Scope decisions (from the spec's "ships only if the data shows it matters")

- **Ships:** API engine caching, thread compaction (with API trimming), routing, model-picker log view (by reason).
- **Prompt order: no change.** `RAIL` is the whole system prompt and never changes per turn; the snapshot goes only into a fresh session's first message; every changing block (bot card, workspace, skill, recap) is in the turn's own message, which is the newest part of the prompt. Recorded in the spec, nothing to build.
- **Deferred: parallel consults and the per-message token budget.** Saved threads show no consult turns yet; revisit when the cost report shows them.
- **Deferred: engine fallback.** Claude Code and Codex retry API errors themselves; the API engine already retries 408/429/5xx/529 twice (`RETRIES`). A fallback engine needs a Settings field and is not justified by the failure rate seen. Revisit if failures show up in the cost view's error count.

## Review Focus

1. A stored API history line must never carry `cache_control`: the breakpoint goes on a request copy only (test in Task 1).
2. The last request message can be a plain string (new user text) or a list of `tool_result` blocks: both get the breakpoint on their last block (tests in Task 1).
3. A reply with no `context` (old replies, ACP, a runner) never triggers compaction, and a runner engine never compacts (tests in Task 4).
4. The trim cut must land on a message that starts a turn, never between a tool call and its result (test in Task 3).
5. Routing by wording never moves a turn to an inactive bot, to a bot outside a topic room's members, or out of a DM or a skill continuation (tests in Task 5).

---

### Task 1: Prompt caching on the Anthropic API engine

**Files:**
- Modify: `dashboard/engines/api.py` (`_anthropic_turn`, new `_cached`)
- Test: `tests/test_engines_api.py`

**Interfaces:**
- Produces: `ApiEngine._cached(rail: str, messages: list) -> tuple[list, list]`, a static method returning `(system_blocks, request_messages)`: `system_blocks = [{"type": "text", "text": rail, "cache_control": {"type": "ephemeral"}}]`; `request_messages` is a shallow copy of `messages` whose last message has `cache_control: {"type": "ephemeral"}` on its last content block (a string content becomes `[{"type": "text", "text": ..., "cache_control": ...}]`). The input list and its dicts are not mutated.

- [ ] **Step 1: Write the failing tests** in `tests/test_engines_api.py`, using the fake provider that records request bodies:
  - `test_anthropic_request_marks_system_and_last_message`: one plain turn; the recorded body has `system[0]["cache_control"] == {"type": "ephemeral"}` and `messages[-1]["content"][-1]["cache_control"] == {"type": "ephemeral"}`, and exactly 2 `cache_control` keys in the whole body.
  - `test_tool_loop_moves_breakpoint_to_tool_result`: a turn with one tool call then text; the second recorded body's last message is the `tool_result` list, its last block carries `cache_control`, and no earlier message in that body carries one.
  - `test_cached_prefix_is_stable_across_turns`: two turns in one session; the second turn's first body, cut at the first turn's last breakpoint message (with `cache_control` keys stripped from both), equals the first turn's last body's messages (same stripping). This is the condition for a cache read.
  - `test_history_file_never_stores_cache_control`: after a tool turn, `"cache_control"` does not occur in the session's `.jsonl` history file.
  - `test_openai_request_unchanged`: an OpenAI-format turn's body has no `cache_control` and `messages[0] == {"role": "system", "content": rail}`.

- [ ] **Step 2: Run them to see them fail**

Run: `python -m pytest tests/test_engines_api.py -k "cache or breakpoint or openai_request_unchanged" -v`
Expected: the four Anthropic tests FAIL (no `cache_control`), the OpenAI one PASSES.

- [ ] **Step 3: Implement `_cached` and use it in `_anthropic_turn`**: the `/v1/messages` payload uses `system, msgs = self._cached(session.rail, messages)`, recomputed on every loop iteration so the breakpoint moves to the newest message. `messages` itself (appended to, and stored by `_append`) stays unmarked. `_one_shot` is unchanged.

- [ ] **Step 4: Run the API engine tests**

Run: `python -m pytest tests/test_engines_api.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit** `git commit -m "API engine: prompt-cache breakpoints on the system prompt and newest message"`

---

### Task 2: Engines report the size of their last model call

**Files:**
- Modify: `dashboard/engines/claude_code.py` (`parse_event`, `ClaudeCodeEngine.turn`), `dashboard/engines/api.py` (`_anthropic_turn`, `_openai_turn`, `_done`), `dashboard/engines/codex.py` (`parse_event`), `dashboard/server.py` (`run` inside `_handle_ask`)
- Test: `tests/test_engines.py`, `tests/test_engines_api.py`, `tests/test_engines_cli.py`, `tests/test_cost_ledger.py`

**Interfaces:**
- Produces: the done payload of every metered engine gains `context: int | None`, the prompt size of the turn's last model call in tokens (`in + cache_write + cache_read` of that call). The server saves it on the reply as `msg["context"]` (so `chat_store` stores it with the other reply meta).
  - Claude Code: `parse_event` maps `stream_event` / `message_start` to `("context", n)` with `n = input_tokens + cache_creation_input_tokens + cache_read_input_tokens` of `event.message.usage`. `turn()` keeps the last one and does not yield it; it sets `payload["context"]` on the `done` event.
  - API engine: the last request of the turn (Anthropic: its `message_start` usage, same sum; OpenAI-compatible: its `prompt_tokens`). `_done(parts, usage, model, context=None)`.
  - Codex: `turn.completed` `input_tokens` (the turn's total input; an upper bound, documented in a comment).
  - ACP and runner: no `context` key.

- [ ] **Step 1: Write the failing tests**
  - `tests/test_engines.py::test_claude_code_message_start_is_context`: `parse_event({"type": "stream_event", "event": {"type": "message_start", "message": {"usage": {"input_tokens": 5, "cache_creation_input_tokens": 100, "cache_read_input_tokens": 1000}}}}) == ("context", 1105)`.
  - `tests/test_engines_cli.py::test_claude_code_done_carries_last_context`: a fake `claude` script printing two `message_start` lines (sums 1000 then 3000) and a `result`; the turn yields no `"context"` event and its `done` payload has `context == 3000`.
  - `tests/test_engines_api.py::test_done_context_is_last_request`: tool turn whose two `message_start` events report `input_tokens` 10 and 40 with `cache_read_input_tokens` 0 and 500; `done["context"] == 540`.
  - `tests/test_engines.py::test_codex_done_context_is_turn_input`: `turn.completed` with `input_tokens: 900` gives `context == 900`.
  - `tests/test_cost_ledger.py::test_reply_saves_context`: through `_handle_ask` with a scripted engine whose done payload has `context: 1234`, the recorded reply meta has `context == 1234`.

- [ ] **Step 2: Run them to see them fail**

Run: `python -m pytest tests/test_engines.py tests/test_engines_cli.py tests/test_engines_api.py tests/test_cost_ledger.py -k context -v`
Expected: FAIL.

- [ ] **Step 3: Implement as in Interfaces.** In `server.run`, add `context=fin.get("context")` to the `msg.update(...)` call.

- [ ] **Step 4: Run the engine and ledger tests**

Run: `python -m pytest tests/test_engines.py tests/test_engines_cli.py tests/test_engines_api.py tests/test_cost_ledger.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit** `git commit -m "Engines report the last model call's prompt size as context"`

---

### Task 3: API history trimmed at a stable cut, sized by the compaction threshold

**Files:**
- Create: `dashboard/compaction.py`
- Modify: `dashboard/engines/api.py` (`_trim`, both `_*_turn` methods)
- Test: `tests/test_compaction.py` (new), `tests/test_engines_api.py`

**Interfaces:**
- Produces in `dashboard/compaction.py`:
  - `CHARS_PER_TOKEN = 4`
  - `threshold() -> int`: `int(os.environ.get("LEOBOT_COMPACT_TOKENS", 60000))`, `0` on a non-integer or negative value.
  - `last_context(rec: dict | None, engine_id: str) -> int | None`: the `context` of the newest assistant message in `rec["messages"]` whose `engine == engine_id` and that has no `asked_by` (a consulted teammate ran in its own session); `None` if there is none or it has no int `context`.
  - `due(rec, engine_id) -> bool`: `threshold() > 0` and `last_context(...) >= threshold()`.
- Changes `ApiEngine._trim(self, messages, state)`: `state` is `session.engine_state[self.id]`. Budget in chars: `threshold() * CHARS_PER_TOKEN`, or `HISTORY_CHARS` when `threshold()` is 0. The cut is an index into the loaded history, kept as `state["cut"]` (default 0). If the messages from `cut` on exceed the budget, the cut moves forward until they fit in half the budget, then on to the next message that starts a turn (`_starts_turn`); the message being answered always stays. The request is `[messages[0], bridge] + messages[cut:]` (thinking blocks stripped, as today) when `cut > 0`, else `messages`. `_repair` runs on `messages[cut:]` after the cut, not before, so indices refer to the stored file.

- [ ] **Step 1: Write the failing tests**
  - `tests/test_compaction.py`: `threshold()` default 60000, env `"0"` gives 0, env `"abc"` gives 0; `last_context` skips replies of another engine and replies with `asked_by`, and returns `None` for a record whose replies have no `context`; `due` is False at 59999 and True at 60000 with the default.
  - `tests/test_engines_api.py::test_trim_cut_is_stable_between_turns`: with `LEOBOT_COMPACT_TOKENS=100` (400 chars budget) and a history that passes it, two consecutive turns send the same `messages[2]` (first message after the bridge) and `state["cut"]` is unchanged between them.
  - `tests/test_engines_api.py::test_trim_cut_starts_a_turn`: a history whose half-budget point falls on a `tool_result` message; the first message after the bridge is a user message with plain text content.
  - `tests/test_engines_api.py::test_trim_off_keeps_history_chars`: `LEOBOT_COMPACT_TOKENS=0` and a history under `HISTORY_CHARS` sends every message.

- [ ] **Step 2: Run them to see them fail**

Run: `python -m pytest tests/test_compaction.py tests/test_engines_api.py -k "compaction or trim" -v`
Expected: FAIL (`dashboard.compaction` missing, cut not kept).

- [ ] **Step 3: Implement `dashboard/compaction.py` and the new `_trim`.** Both turn methods call `self._trim(self._load(session), session.engine_state[self.id])` and repair what it returns.

- [ ] **Step 4: Run the API engine tests**

Run: `python -m pytest tests/test_compaction.py tests/test_engines_api.py -v`
Expected: all PASS, including Task 1's stable-prefix test.

- [ ] **Step 5: Commit** `git commit -m "API engine: trim history at a stable cut sized by LEOBOT_COMPACT_TOKENS"`

---

### Task 4: Thread compaction for engines that keep their own session

**Files:**
- Modify: `dashboard/engines/base.py` (`Engine.compact`), `dashboard/engines/claude_code.py` (`compact`, session id in `build_cmd`), `dashboard/engines/api.py` and `dashboard/engines/runner.py` (`compact` returns False), `dashboard/server.py` (`_handle_ask`, before `compose_turn_prompt`), `evals/turns.py`, `evals/cases/turns.json`
- Test: `tests/test_engines.py`, `tests/test_server_ask.py`, `tests/test_evals.py`

**Interfaces:**
- Consumes: `compaction.due(rec, engine_id)` (Task 3), `chat_store.recap(rec)`, `sess.pending_recap`.
- Produces:
  - `Engine.compact(self, session) -> bool`: base drops this engine's state (`session.engine_state.pop(self.id, None)`) and returns True; Codex and ACP inherit it (their `is_fresh` reads `engine_state[id]["session"]`).
  - `ClaudeCodeEngine.compact`: sets `session._started = False` and `session.engine_state[self.id] = {"session": str(uuid.uuid4())}`, returns True. `build_cmd` uses `ClaudeCodeEngine.session_of(session) -> str` = `engine_state[self.id]["session"]` when set, else `session.session_id`, for both `--session-id` and `--resume`. Check every other place that passes `session.session_id` to `claude` (`tokensave_callers` on `build_cmd`) and switch those too.
  - `ApiEngine.compact` and `RunnerEngine.compact` return False (the API engine trims itself; a runner's session lives on another Mac).
  - Server: right before `compose_turn_prompt`, load the saved record once (reuse it for the recap lines below); if `compaction.due(saved_rec, engine.id)` and `getattr(engine, "compact", None)` and `engine.compact(sess)`, set `sess.pending_recap = chat_store.recap(saved_rec) or None` and log `chat compact engine=%s context=%d`. A job turn (`job`) is skipped: it already runs in a fresh session.
  - `evals/turns.py`: new case key `context` (int): the thread's last lead reply on the eval engine had that `context`; `THREAD_ID` becomes a fixed 36-character id (`chat_store._path` refuses `eval-thread`), and `run()` saves a record with that reply through `chat_store.save` under the patched `CHATS_DIR`, and the scripted `Sess.is_fresh` returns True once `compact` was called. The eval `_Engine` gains `compact(self, session)` that records the call and returns True.

- [ ] **Step 1: Write the failing tests**
  - `tests/test_engines.py::test_claude_code_compact_starts_new_session_id`: after `compact`, `build_cmd` has `--session-id <new id>` (not the chat id) and no `--resume`; after `session._started = True`, it has `--resume <new id>`.
  - `tests/test_engines.py::test_runner_and_api_never_compact`: both return False and leave `engine_state` unchanged.
  - `tests/test_server_ask.py::test_compaction_sends_recap_and_snapshot`: a saved thread whose last lead reply on the fake engine has `context: 70000`; the next turn's prompt contains `chat_store.RECAP_HEAD`'s first line and the snapshot heading, and the engine's `compact` was called once.
  - `tests/test_server_ask.py::test_no_compaction_below_threshold_or_without_context`: `context: 59000`, and a reply with no `context`: `compact` is not called, no recap in the prompt.
  - `tests/test_server_ask.py::test_compaction_skips_other_engine_and_teammate_replies`: newest reply with `context: 90000` is on another engine or has `asked_by`: no compaction.
  - `evals/cases/turns.json`: add case `long-thread-compacts-with-recap` (category `context`, `thread: {"bot": "cos"}`, `context: 80000`, msg `what did we decide earlier?`, expect `prompt_has: ["Earlier in this thread"]`, `bot: "cos"`).

- [ ] **Step 2: Run them to see them fail**

Run: `python -m pytest tests/test_engines.py tests/test_server_ask.py -k "compact" -v && python -m evals`
Expected: FAIL (no `compact`, no recap).

- [ ] **Step 3: Implement as in Interfaces.**

- [ ] **Step 4: Run engines, server and evals**

Run: `python -m pytest tests/test_engines.py tests/test_engines_cli.py tests/test_server_ask.py tests/test_chat_session.py tests/test_evals.py -v && python -m evals`
Expected: all PASS; evals report every non-gap case passing.

- [ ] **Step 5: Commit** `git commit -m "Thread compaction: a fresh session with the recap past LEOBOT_COMPACT_TOKENS"`

---

### Task 5: Untagged messages go to the bot whose skill the wording matches

**Files:**
- Modify: `dashboard/server.py` (`_route_bot`, the `_handle_ask` call site, the `bot` SSE event), `dashboard/app.js` (`botLabel` and its callers), `evals/turns.py` (`picked` outcome), `evals/cases/turns.json` (drop the two `gap` marks)
- Test: `tests/test_bots.py`, `tests/test_evals.py`

**Interfaces:**
- Produces:
  - `class Route(tuple)` in `server.py`: the same four items `(bot, skill, explicit, handoff_from)`, plus attribute `picked: str | None` (the skill whose wording moved the turn). Equal to the plain 4-tuple, so existing tests keep comparing tuples. `_route_bot` returns `Route` everywhere.
  - `_route_bot(team, user_msg, session, default_bot=..., dm=None, members=None)`: in the two untagged branches (last bot, room lead), with `candidate` the bot it picks today: `hit = _suggest_skill(user_msg)`; if `hit` and `owner = team.owner(hit)` differs from `candidate`, `team.get(owner).active`, and (`members is None` or `owner in members`), return `Route((owner, None, False, None), picked=hit)`. Tagged turns, DMs (`_route_dm`) and continuations are unchanged.
  - Server passes `members=room["members"]` for a topic room, sets `msgs[0]["picked"] = route.picked` when set, and adds `"picked"` to the `bot` SSE event.
  - `app.js`: `botLabel(div, bot, handoffFrom, askedBy, picked)`; the note is `picked for ${picked}` when `picked` is set and there is no asker or handoff. Live replies pass `obj.bot.picked`; saved replies pass `m.picked` wherever saved bot replies get their label.
  - `evals/turns.py`: `"picked"` joins `_EQUAL`; the outcome reads `bot_ev.get("picked")`.

- [ ] **Step 1: Write the failing tests** in `tests/test_bots.py` (the `route` helper there; extend it to pass `members`):
  - `test_untagged_wording_moves_to_skill_owner`: `route("how much should we charge for the pro tier?")` equals `("cmo", None, False, None)` and its `.picked == "pricing-strategy"`.
  - `test_untagged_wording_moves_from_last_bot`: `route("who is our ideal customer?", last_bot="cmo")[0] == "research"`.
  - `test_wording_owner_already_speaking_stays`: `route("and the premium tier price?", last_bot="cmo")` is `("cmo", None, False, None)` with `.picked is None`.
  - `test_wording_never_moves_to_inactive_or_non_member`: an inactive owner, and `members=["cos"]`, both keep `cos`.
  - `test_dm_and_continuation_unchanged`: the existing DM and continuation expectations, with a pricing-worded message, are unchanged.
  - In `evals/cases/turns.json`: drop `gap` from `untagged-price-question-reaches-pricing-owner` and `untagged-icp-question-reaches-research`, and add `"picked": "pricing-strategy"` / `"picked": "icp-research"` to their `expect`.

- [ ] **Step 2: Run them to see them fail**

Run: `python -m pytest tests/test_bots.py -v && python -m evals`
Expected: the new tests FAIL; evals report the two cases failing.

- [ ] **Step 3: Implement as in Interfaces.**

- [ ] **Step 4: Run routing tests and evals**

Run: `python -m pytest tests/test_bots.py tests/test_server_ask.py tests/test_evals.py -v && python -m evals`
Expected: all PASS; no open routing gaps left.

- [ ] **Step 5: Commit** `git commit -m "Routing: untagged wording that matches another bot's skill goes to that bot"`

---

### Task 6: Cost view shows model picks by reason and cache share per engine

**Files:**
- Modify: `dashboard/costs.py` (`summarize`, `_add`), `dashboard/app.js` (cost view, line with `top(c.by_model, "Models")`)
- Test: `tests/test_cost_ledger.py`

**Interfaces:**
- Produces: `summarize(...)` gains `by_reason` (key: the reply's `reason`, label `"not recorded"` when missing), and each `by_engine` row gains `cache_read_share: float | None` (that engine's cache read over its `in + cache_write + cache_read`). The dashboard shows a "Model picks" list from `by_reason` and the share next to each engine.

- [ ] **Step 1: Write the failing tests**: `test_by_reason_groups_replies` (two replies `reason: "default"`, one `"explicit-skill"`, one with none: rows with turns 2, 1, 1 and the missing one labeled `not recorded`); `test_engine_rows_carry_cache_share` (one engine with usage `in 100, cache_write 0, cache_read 900` gives `0.9`; an engine with no usage gives `None`).

- [ ] **Step 2: Run them to see them fail**

Run: `python -m pytest tests/test_cost_ledger.py -k "reason or cache_share" -v`
Expected: FAIL.

- [ ] **Step 3: Implement as in Interfaces.**

- [ ] **Step 4: Run the ledger tests**

Run: `python -m pytest tests/test_cost_ledger.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit** `git commit -m "Cost view: model picks by reason, cache share per engine"`

---

### Task 7: Docs, full suite, live check, PR

**Files:**
- Modify: `docs/superpowers/specs/2026-11-11-harness-upgrade-design.md` (Phase 4 "As built" list), `README.md` (env var `LEOBOT_COMPACT_TOKENS`, if the README lists env vars)

- [ ] **Step 1: Write the spec's Phase 4 "As built"**: what shipped, `LEOBOT_COMPACT_TOKENS`, Claude Code's own session id per compaction, the API engine's stable cut, routing by wording with the `picked for` note, the reasons view, "prompt order: no change" with the reason above, and the deferred items with their revisit condition. No usage numbers.

- [ ] **Step 2: Full suite and evals**

Run: `python -m pytest -q && python -m evals && python -m pytest tests/test_no_usage_data.py -q`
Expected: all PASS.

- [ ] **Step 3: Live check on a throwaway home**: a local fake Anthropic SSE server that records request bodies and reports `cache_read_input_tokens` equal to the length of the prefix it saw before; point an API engine in a temp `LEOBOT_HOME`'s `team/engines.json` at it, run three turns, and confirm the second and third report a cache read and the cost view's engine row shows a share above 0. Then a Claude Code thread with `LEOBOT_COMPACT_TOKENS=1000`: the second turn's log shows `chat compact`, the thread keeps answering, and reopening the chat resumes the new session.

- [ ] **Step 4: Commit, push, PR, CI, merge**: branch `harness-phase4-leaner-turns`, PR titled "Leaner turns (harness upgrade phase 4)", wait for CI, resolve review comments, squash-merge, delete the branch.
