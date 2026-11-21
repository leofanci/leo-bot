# Bot-to-Bot Chains Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A bot that receives a handoff can itself hand off or consult, so work passes along a chain of bots in one message, with hop and cost limits that are settings and default to none.

**Architecture:** The single handoff block in `_ask_turn` (`dashboard/server.py`, after the lead's follow-up) becomes a loop over hops. Each hop runs the target with its own rights (as today), settles that target's consults and follow-up, then follows its handoff if any. Two settings (`chain_hops`, `chain_cost`) live in General. Pure helpers live in a new `core/chain.py`.

**Tech Stack:** Python 3 standard library, `unittest`, vanilla JS in `dashboard/app.js`.

**Spec:** `docs/superpowers/specs/2026-11-21-skills-workflows-loops-design.md` (section "Bots passing work to each other"). This is plan 1 of 4. Later plans cover the workflow runner, loops, and library content.

## Global Constraints

- Tests run with `python3 -m unittest discover -t . -s tests` (the `-t .` sets a temp home). Run `python3 -m unittest tests.test_no_usage_data` before every commit.
- Public repo: fixtures use only `acme` and the shipped bot ids (`cos`, `research`, `cmo`, `social`). No real names, no user quotes in tracked files.
- Defaults: no hop limit and no per-message cost cap (`0` means none for both).
- A scheduled job still never hands off (unchanged here; loops change in plan 3).
- A consulted teammate stays read-only and its own handoff lines are still stripped.
- Every hop keeps its own rights, engine and approvals; chains never widen what one bot may do. A consult asked by a hop reads at most what that hop's bot may read, never what the lead may read.
- `MAX_TEAMMATES` (2) stays, but per asking bot and run, not per message: each hop gets its own two consults. That is how the spec's "two other bots per message" limit is lifted.
- Commit messages carry no co-author trailer. Work on a branch `feat/bot-chains` in the main checkout (no extra worktree), then PR, CI, merge.

**Not in this plan:** per-workspace override of the two settings (spec item, goes with the loop management screen in plan 3).

## Review Focus

- Two bots hand back and forth forever with no limit set: pause after the same two bots take four turns in a row.
- A legitimate return (A to B, B back to A once) must not trigger the pause.
- A cap refusal in the middle of a chain: earlier replies are kept, a notice is shown, nothing crashes.
- A handoff to an inactive bot or one outside the room in the middle of a chain: the line is stripped, a notice appears, the chain ends.
- A target that consults and then hands off: consults are answered and the target follows up before the handoff is followed.
- A hop's consults are capped to the hop bot's own read folders and quote that hop's reply, not the lead's.
- A bot co-tagged in the user's message runs once (with the lead), never again on each hop.
- A hop that asks for approval ends the chain there: one card per message, and Approve goes to the bot that asked.
- Engines: a chain over three engines records, per engine, how many of this message's replies it missed.

---

### Task 1: Chain limit settings

**Files:**
- Modify: `core/general.py` (module docstring, `view`, `update`)
- Create: `core/chain.py`
- Test: `tests/test_chain.py`

**Interfaces:**
- Produces: `general.view()` gains `chain_hops: int` and `chain_cost: float`. `general.update({...})` validates both. `chain.limits() -> tuple[int, float]` returns `(max_hops, max_cost)`, `0` meaning none; a broken settings file reads as `(0, 0.0)` (same `try/except` as `general.cap`).

- [ ] **Step 1: Write failing tests** in `tests/test_chain.py` (class `ChainSettings`, same temp-home setup as `tests/test_bot_settings.py` class around line 157):
  - `test_defaults_are_unlimited`: `general.view()["chain_hops"] == 0` and `["chain_cost"] == 0.0`; `chain.limits() == (0, 0.0)`.
  - `test_update_saves_and_validates`: `general.update({"chain_hops": 6, "chain_cost": 1.5})` then `chain.limits() == (6, 1.5)`; each of `{"chain_hops": -1}`, `{"chain_hops": 2.5}`, `{"chain_hops": True}`, `{"chain_hops": 1001}`, `{"chain_cost": -0.1}`, `{"chain_cost": "x"}`, `{"chain_cost": float("inf")}` raises `ValueError` and saves nothing; `{"chain_hops": None}` or `""` resets to 0 (same for `chain_cost`).
  - `test_hand_edited_file_never_breaks`: a non-numeric saved value reads as 0; `chain.limits()` never raises.
- [ ] **Step 2: Run** `python3 -m unittest tests.test_chain -v`. Expected: FAIL (no `core.chain`).
- [ ] **Step 3: Implement** in `core/general.py`: `CHAIN_HOPS_MAX = 1000`, `CHAIN_COST_MAX = CAP_MAX`; `view()` returns the two keys cleaned like `caps` (int for hops, bool rejected; finite float for cost); `update()` validates with the messages `"chain_hops: a whole number from 0 (no limit) to 1000"` and `"chain_cost: dollars from 0 (no cap) to 10000"`. Add both keys to the module docstring. `core/chain.py`: `limits()` reads `general.view()`.
- [ ] **Step 4: Run** the tests again. Expected: PASS.
- [ ] **Step 5: Commit** `feat(chain): hop and cost limit settings, default none`.

### Task 2: Pure chain helpers

**Files:**
- Modify: `core/chain.py`
- Test: `tests/test_chain.py`

**Interfaces:**
- Produces: `chain.within_hops(done: int, max_hops: int) -> bool` (true when another hop may run; `max_hops == 0` always true). `chain.pingpong(path: list[str]) -> bool` (true when the last four entries are exactly two distinct bots alternating, e.g. `["cos","research","cos","research"]`). `chain.missed(runs: list[tuple[str, int]], total: int) -> dict[str, int]`: given `(engine_id, message_index)` for each run on the thread session, each engine's count of this message's replies after its last run (`total` is the message count).

- [ ] **Step 1: Write failing tests** (class `ChainHelpers`): `within_hops(0, 0)`, `within_hops(99, 0)` true; `within_hops(2, 3)` true; `within_hops(3, 3)` false. `pingpong(["cos","research","cos"])` false (three turns); `pingpong(["cos","research","cos","research"])` true; `pingpong(["cos","research","cmo","research"])` false; `pingpong(["x","cos","research","cos","research"])` true (only the tail counts); `pingpong(["cos","cos","cos","cos"])` false. `missed([("a",0)], 1) == {"a": 0}`; `missed([("a",0),("b",1)], 2) == {"a": 1, "b": 0}`; `missed([("a",0),("b",1),("a",2),("c",3)], 4) == {"a": 1, "b": 2, "c": 0}`.
- [ ] **Step 2: Run** and see FAIL.
- [ ] **Step 3: Implement** the three as plain functions in `core/chain.py`.
- [ ] **Step 4: Run** and see PASS.
- [ ] **Step 5: Commit** `feat(chain): hop limit, ping-pong and missed-replies helpers`.

### Task 3: The hop loop in `_ask_turn`

**Files:**
- Modify: `dashboard/server.py` (`_ask_turn`: `run_teammate` about lines 4454-4512; the `if handoff and job:` and `if handoff and not cap_hit and may_run():` blocks, about lines 4621-4667; `engines_seen` at line 4302; `_bot_block` lines 1050 and 1082-1088)
- Test: `tests/test_handoff.py`

**Interfaces:**
- Consumes: `chain.limits()`, `chain.within_hops`, `chain.pingpong`, `chain.missed` (Tasks 1-2), the existing `_turn_setup`, `_mint`, `compose_turn_prompt`, `_plan_teammates`, `_followup_prompt`, `_extract_consults`, `_extract_handoff`, `_HANDOFF_RE`, `run`, `run_teammate`, `may_run`, `notice`, `emit`.
- Produces: per hop, an SSE event `{"chain": {"hop": n}}` before the hop's `msg_start` (n counts from 1), and `"hop": n` saved on the hop's message so a reload shows it. The handoff prompt for a bot-passed target no longer says "No HANDOFF of your own this message"; it says the target may hand off or consult again and should answer in full when nothing is left (`handed_by_bot=True` wording in `_bot_block`). `run_teammate` gains `asker_read` (the asking bot's read folders) and `asker_said` (the asking bot's reply the consult prompt quotes); the lead passes its own, a hop passes the hop's.

- [ ] **Step 1: Write failing tests** in `HandoffTurn` (patch `chain.limits` with `mock.patch.object(server.chain, "limits", return_value=(h, c))` where a limit is needed; import `chain` in `server`):
  - `test_chain_follows_the_targets_handoff`: replies `["HANDOFF @research: a", "HANDOFF @cmo: b", "Done."]`; 3 runs; message bots end `["cos","research","cmo"]`; last message `handoff_from == "research"` and `hop == 2`; events contain `{"chain": {"hop": 1}}` and `{"chain": {"hop": 2}}`; `sess._last_bot == "cmo"`.
  - `test_target_consult_then_handoff`: activate `social` inside this test only (`bots.set_active("social", True, self.root, "acme")`; other tests need it off); replies `["HANDOFF @research: a", "CONSULT @cmo: tone?\nHANDOFF @social: posts", "warm", "follow-up", "posted"]`; 5 runs; message bots `["cos","research","cmo","research","social"]`; the consult prompt (run 3) quotes research's reply, not the lead's.
  - `test_cotagged_bot_runs_once`: message `"@cos @cmo x?"` (`cos` leads, `cmo` co-tagged; confirm the order in the tag resolver first), replies `["HANDOFF @research: a", "cmo view", "Done."]` (no lead follow-up: a co-tag is not a consult); 3 runs; `cmo` appears once in the message bots.
  - `test_hop_consult_reads_only_what_the_hop_may_read`: patch `server._turn_folders` so the lead reads `/a` and `/b` and the hop bot reads only `/a`; a hop consult's `read_dirs` contain no `/b`.
  - `test_hop_handoff_respects_room_members`: patch the room members to `["cos", "research"]`; replies `["HANDOFF @research: a", "Sure.\nHANDOFF @cmo: b"]`; 2 runs; a `Could not pass this on` notice.
  - `test_hop_limit_stops_the_chain`: limits `(1, 0.0)`, replies `["HANDOFF @research: a", "HANDOFF @cmo: b", "never"]`; 2 runs; a notice containing `hop limit`.
  - `test_pingpong_pauses_with_a_notice`: limits `(0, 0.0)`; replies `["HANDOFF @research: a", "HANDOFF @cos: b", "HANDOFF @research: c", "HANDOFF @cos: d", "never"]`; exactly 4 runs; a notice containing `back and forth`.
  - `test_one_return_is_not_pingpong`: replies `["HANDOFF @research: a", "HANDOFF @cos: b", "Done."]`; 3 runs; no `back and forth` notice.
  - `test_chain_cost_cap_stops`: limits `(0, 0.015)`; each run costs `0.01` (the scripted `done` event); replies `["HANDOFF @research: a", "HANDOFF @cmo: b", "never"]`; 2 runs; a notice containing `cost cap`.
  - `test_cap_refusal_mid_chain_keeps_earlier_replies`: patch `server.spend.refusal` to refuse from its third call (call 1 is the check before the lead runs, as in `test_cap_stops_the_handoff`; calls 2 and 3 are `may_run` before hops 1 and 2); replies `["HANDOFF @research: a", "HANDOFF @cmo: b", "never"]`; 2 runs; the first two replies are saved; the refusal text is shown.
  - `test_unavailable_target_mid_chain_ends_it`: replies `["HANDOFF @research: a", "Sure.\nHANDOFF @social: posts"]` with `social` inactive; 2 runs; the last message ends with `Sure.` and carries a `Could not pass this on` notice.
  - `test_approval_in_a_hop_ends_the_chain`: replies `["HANDOFF @research: a", "Memo.\nAPPROVAL: save memo\nHANDOFF @cmo: b"]`; 2 runs; one pending card from `research`; a notice that the chain waits for the approval.
  - `test_three_engines_record_what_each_missed`: like `test_target_on_another_engine_gets_the_thread_and_both_engines_are_saved`, with `cos`, `research`, `cmo` on three engines and a chain `cos -> research -> cmo`; `rec["engines"]` seen counts are `n - 2`, `n - 1`, `n`; the `cmo` run's prompt holds both earlier replies of this message, each labeled with its bot handle.
  Change the existing `test_handoff_runs_the_teammate_next` assertion about "No HANDOFF of your own this message" to assert the new wording, and replace `test_target_handoff_lines_are_ignored` with `test_chain_follows_the_targets_handoff`. Leave `test_consulted_teammates_handoff_never_reaches_the_follow_up` and the job test untouched.
- [ ] **Step 2: Run** `python3 -m unittest tests.test_handoff -v`. Expected: the new tests FAIL.
- [ ] **Step 3: Implement.**
  - `run_teammate(b2, question, asked_by, msg, asker_read=None, asker_said=None)`: cap `c_read` and `c_rows` to `asker_read` (default the lead's `read_dirs`); the prompt quotes `asker_said` (default `msgs[0]["content"]`).
  - Extract the handoff target run into a nested function `run_hop(target, task, from_bot, hop) -> tuple[str, str] | None` inside `_ask_turn` that: builds the setup and prompt as the current block does; when the target's engine differs from an engine that ran earlier, adds the recap plus every reply of this message that engine missed, each as `@handle just replied: ...` (untrusted, labeled, capped at 1500 chars each); emits the `chain` event; saves `hop` on the message; adds the message index to `writers`; runs the target; strips REACT lines into `reacts`; checks `_HANDOFF_RE` before extracting so a named but invalid target gets the `Could not pass this on` notice; extracts the handoff with `speaker=target, members=members`; extracts the target's consults and runs them with `_plan_teammates(team, target, from_bot, "", asks)` (empty user message: co-tagged bots ran with the lead already) passing `asker_read=hst.read_dirs` and `asker_said` the hop's reply; then the target's follow-up on the hop's own setup (`hst.model`, `hst.with_web`, `hst.read_dirs`, `hst.edit_dirs`, its minted tokens; never the lead's `lead_kw`/`apps_kw`), `may_run()` before each run, its index added to `writers`; strips consult lines and takes a follow-up handoff the way the lead's follow-up does; returns the next `(bot_id, task)` handoff, or `None`. When the hop's reply or follow-up carries an `APPROVAL:` line, return `None` with the notice `"Waiting for your approval before passing this on."`.
  - Replace the single block with a `while` loop that, before each hop, stops with a notice when `not chain.within_hops(hops, max_hops)` (`"Stopped at the hop limit (N)."`), when `max_cost` is set and `sum(m.get("cost") or 0 for m in msgs)` reached it (`"Stopped at this message's cost cap."`), when `cap_hit` or `not may_run()`, or when `chain.pingpong(path)` (`"These two bots keep handing back and forth. Paused: tell them what to do next."`). `path` is the list of speakers so far (lead, then each hop target). Keep `if handoff and job:` blocking (jobs never hand off). Call `sess.note_bot(...)` for each target so the last one wins.
  - Track `(engine_id, message_index)` for each run on the thread session (lead, follow-ups, hops) and set `engines_seen = chain.missed(runs, len(msgs))` before `record_turn`, replacing the two-engine special case at line 4660.
  - `_bot_block`: replace the `handed_by_bot` sentence (lines 1086-1088) as the Interfaces block says, and the peers line at 1050 to `"In this channel (teammates; hand off when one of them is better placed):"`.
- [ ] **Step 4: Run** `python3 -m unittest tests.test_handoff tests.test_server_ask tests.test_spend_cap tests.test_halt -v`. Expected: PASS.
- [ ] **Step 5: Commit** `feat(chain): bots hand off along a chain, with hop and cost limits`.

### Task 4: Settings screen and thread display

**Files:**
- Modify: `dashboard/app.js` (the General screen around line 3099 and its save handlers near line 3116; the stream handler that reads `msg_start` near line 6873; the reload path near line 5940)
- Test: `tests/test_short_replies.py` (it already asserts the General screen markup for `gen-short`; add a sibling test there)

**Interfaces:**
- Consumes: the `chain_hops` and `chain_cost` fields of `/api/settings/general` (GET returns `general.view()`, POST calls `general.update`; no server change needed); the `{"chain": {"hop": n}}` event and the saved `hop` field (Task 3).

- [ ] **Step 1: Write the failing test** in the style of the existing `gen-short` markup assertion: the General screen markup contains inputs with ids `gen-hops` and `gen-chain-cost`, each with a label saying `0 = no limit` / `0 = no cap`, and `app.js` reads `obj.chain` in the stream handler and `m.hop` on reload.
- [ ] **Step 2: Run** `python3 -m unittest tests.test_short_replies -v`. Expected: FAIL.
- [ ] **Step 3: Implement.** Two number inputs (`min="0"`, `step="1"` for hops, `step="0.01"` for cost) after the short-replies row; on change they `save({chain_hops: n})` and `save({chain_cost: x})` with the same toast style as short replies; an empty field saves `null` (no limit). In the stream handler, a `chain` event shows a small `hop n` label on the message that follows (reuse the style of the existing `handoff_from` label); the reload path shows the same label from `m.hop`.
- [ ] **Step 4: Run** the test again. Expected: PASS.
- [ ] **Step 5: Commit** `feat(chain): General settings fields and hop label`.

### Task 5: Wording, docs and the full gate

**Files:**
- Modify: `dashboard/ai_rules.py` (`TEAMMATE`, line 128), `docs/guide.md` (the chat section), the spec's "Current state (checked)" bullet on one handoff per message
- Test: existing suite

- [ ] **Step 1: Update** `TEAMMATE` line 128: a bot may `HANDOFF` when another bot is better placed, not only when asked; a received handoff may be passed on or consulted; the last bot answers in full. `bots/cos.md` already says it hands over with `HANDOFF @handle`; leave it. `docs/guide.md`: describe chains, the two settings (default none), the hop label, the pause, and that an approval ends the chain. In the spec, mark the one-handoff bullet as the state before plan 1.
- [ ] **Step 2: Run** `python3 -m unittest discover -t . -s tests` and `python3 -m unittest tests.test_no_usage_data`. Expected: all pass.
- [ ] **Step 3: Commit** `docs(chain): teammate rules and guide for chains`, push `feat/bot-chains`, open the PR, wait for CI, then merge per the repo workflow.
