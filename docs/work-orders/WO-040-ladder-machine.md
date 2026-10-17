# WO-040 — ladder-machine

Phase: 4 · Branch: `wo/040-ladder-machine` · Worktree: `../soundings-worktrees/wo-040-ladder-machine` · Est: 3.5 h · Owner: Kudrat (planner / ladder / investigation)
Depends on: WO-010 (`LadderState`, `Rung`, `HANDLING_TERMINAL_STATES`, `TERMINAL_STATES`, `EvidenceRow`, `stable_hash`, `canonical_json`, `find_denylisted`, `load_ladder_config`, `load_standing_orders`). Nothing else: no sim, runner or investigation import, so this WO merges in wave 2 alongside Phase 1. The driver that feeds it real runs, and the `python -m soundings.ladder` CLI, are WO-043's.

## Goal
The ladder exists as a hand-rolled, typed state machine whose transition table is data copied row for row from SPEC-LADDER §3 and nothing else. Every transition writes a checkpoint. Every action the machine emits carries an idempotency key and runs through a ledger that will not execute the same key twice. A machine can be rebuilt from its checkpoints. Timers (the hold limit, approval timeouts, the SITREP update interval) run on one `Clock` interface: `TickClock` in the sim, `WallClock` only in live UI mode. It ships with a conservative guard port that pre-authorizes nothing (the `standing_orders.signed=false` path, where a handoff goes straight to `HANDED_OFF`) and the port protocols that WO-041 (boundary guards, approver), WO-042 (listeners) and WO-043 (the driver over real runs, departure detection) plug into.

## Scope
In:
- `soundings/clock.py`: the `Clock` protocol (`now_s() -> float`). `TickClock(tick_s, tick=0)` with `now_s() == tick * tick_s` and `advance(n=1)` is defined here, standalone (no import of `soundings.sim`; the sim keeps its own internal tick counter). `WallClock` uses `time.monotonic`, and this is the only wall-clock read under `soundings/` outside the API. `Timer(name, contact_id, deadline_s)` and `TimerSet.due(clock) -> list[Timer]` return due timers sorted by (deadline, name, contact_id).
- `soundings/ladder/types.py`:
  - `EventKind` holds the SPEC-LADDER §2 events plus three named events that the §3 table uses without listing them in §2: `timer`, `scope_exceeded`, `handoff_complete`.
  - `LadderEvent(kind, arg, tick, payload)`.
  - `GuardName`: `free_probes_complete`, `registry_exact_and_hail_valid`, `not_cleared`, `slow_permitted`, `hold_permitted`, `nothing_preauthorized`, `approval_token_valid_and_scope_matches`, `backup_exists`, `no_backup`.
  - `GuardVerdict` (`permit`, `block`) and `GuardDecision(guard, verdict, reason, inputs_hash, granted_scope)`.
  - `ActionScope(action, basin, asset_id, gates, max_minutes)`. `action` is a boundary action name from `config/ladder.yaml`.
  - `ActionKind`: `open_evidence_table`, `issue_slow_advisory`, `start_hold`, `release_hold`, `request_approval`, `page_backup`, `record_approved_action`, `deliver_sitrep`, `update_sitrep`, `start_watch`, `close_watch`.
  - `LadderTimings`, built by `from_config()` from `config/ladder.yaml` `watch_after_handoff` and the standing-orders `approvers` timeouts. This read goes through WO-010's loader and does not check the signature.
  - `GuardContext`: contact_id, state, event, now_s, threat_mass, benign_mass, time_to_nearest_critical_asset_s, zone_criticality, basin, nearest_asset_id, active_holds, backup_paged, approval_token, requested_scope. Unknown inputs are `None`, never imputed.
- `soundings/ladder/table.py`: `TRANSITIONS: tuple[TransitionRow, ...]`. Each row is (from, event kind, event arg or None, guard or None, to, actions). Three rows go beyond a literal one-row-per-line copy, and each cites §3:
  - The three INVESTIGATING handoff rows are evaluated in table order: slow, then hold, then nothing pre-authorized.
  - `any non-terminal × error → FAILED` expands to every state not in `HANDLING_TERMINAL_STATES`.
  - `HANDED_OFF × handoff_complete → WATCH_AFTER_HANDOFF` encodes the "—" row.
- `RUNG_OF_STATE` covers all 15 states. AWAITING_APPROVAL keeps the rung of the state it came from.
- `soundings/ladder/machine.py`: `LadderMachine(contact_id, guards, clock, ledger, store, timings, listeners=())`.
  - Members: `.state`, `.rung`, `.step(event) -> StepResult`, `.advance() -> list[StepResult]` (fires due timers as `timer(hold_limit)`, `approval(timeout)` and `contact_persisted(interval)` events), and `LadderMachine.restore(checkpoints, ...)`.
  - A (state, event) pair with no row returns `StepResult(transitioned=False, reason="no_row")` and leaves the state unchanged. A guarded row whose guard blocks also changes nothing, and its `GuardDecision` is still recorded.
  - A permitted `start_hold` or `issue_slow_advisory` arms a timer from `GuardDecision.granted_scope.max_minutes`.
- `soundings/ladder/checkpoint.py`: `LadderCheckpoint(seq, contact_id, tick, now_s, from_state, event, guard_decisions, to_state, rung, action_keys, prev_hash, hash)`. Also the `CheckpointStore` protocol, `MemoryCheckpointStore`, and `JsonlCheckpointStore(path)`. The pattern matches WO-033's investigation checkpoints.
- `soundings/ladder/actions.py`:
  - `LadderAction(kind, contact_id, scope, tick, idempotency_key)`, where `idempotency_key = stable_hash([contact_id, kind, scope, from_state, to_state, event.tick])`.
  - `ActionLedger.execute(action) -> ActionRecord` returns the first record for a repeated key.
  - `ActionLedger.active_holds() -> int`, counted across contacts.
  - `ActionLedger.interruptions(contact_id) -> int`, which counts `request_approval` and `page_backup`.
- `soundings/ladder/guard_port.py`:
  - The `GuardPort` protocol: `decide(name, ctx) -> GuardDecision`.
  - `evidence_guard(name, ctx)`, a pure function for the three evidence guards. It reads the evidence rows the event payload names; it does not read free text.
  - `NoStandingOrdersGuards`. Its evidence guards are real, `nothing_preauthorized` always permits, and every standing-order and approval guard blocks with reason `no_signed_standing_orders`.
- `soundings/ladder/ports.py`:
  - `ApproverPort` protocol: `request(contact_id, scope, channel, now_s)` and `poll(now_s) -> list[ApprovalReply]`. `ApprovalReply(contact_id, decision ∈ {granted, denied}, token | None, channel, at_s)`. `NoReplyApprover` implements the port.
  - `DeparturePort` (`observe(contact_id, tick, point | None) -> bool`) and `LadderListener` (callback `(checkpoint, actions, decisions) -> None`).
  - The table attaches `deliver_sitrep` to the rows entering HANDED_OFF, LOST_VEHICLE and HAZARD_NOTICE, `request_approval` to the rows entering AWAITING_APPROVAL, and `start_watch` to the `handoff_complete` row; `start_watch` arms the `sitrep_update` timer from `LadderTimings.sitrep_update_interval_s`.

Out: the standing-orders guards, signature checks and approval tokens (WO-041); SITREP and audit (WO-042); the driver over real runs, the `python -m soundings.ladder` CLI, departure detection and `soundings/runner.py` edits (WO-043); any state, event, action or word for anything after the handoff.

Parallel start: this WO needs only WO-010 and merges in wave 2. WO-041 follows straight after (wave 3); WO-042 also waits for WO-032; WO-043 for WO-033 and WO-042.

## Spec references
`docs/SPEC-LADDER.md §1–§3` (exhaustive table, property test) · `CLAUDE.md §2.1, §2.2, §2.4, §2.5, §4` (hand-rolled machine, checkpoint and idempotency pattern) · `docs/WORKFLOW.md §8` (Clock) · `docs/SPEC-INVESTIGATION.md §7` (stop-rule outcomes).

## Files expected
- `soundings/clock.py`
- `soundings/ladder/__init__.py`, `types.py`, `table.py`, `machine.py`, `checkpoint.py`, `actions.py`, `guard_port.py`, `ports.py`
- `tests/test_ladder.py`, `tests/test_ladder_property.py`, `tests/test_clock.py`, `tests/test_ladder_checkpoint.py`, `tests/test_ladder_integration.py`
- `tests/support/scripted_guards.py` (`ScriptedGuards`, test only: permit or block per `GuardName`)

## Exit tests (must all pass with `MODEL_MODE=replay`, no network)
- [ ] `pytest tests/test_ladder.py` — every transition in `SPEC-LADDER §3` is reachable; no transition outside the table is possible (property test); `HANDED_OFF` and `RESOLVED_*` are terminal for handling; there is no state, event, or tool whose name matches the denylist.
- [ ] `pytest tests/test_ladder.py::test_every_row_reachable` — proves that for each `TRANSITIONS` row, a BFS over (state, event, `ScriptedGuards` setting) from DETECTED finds a path that fires that exact row, and that the set of fired rows equals the table.
- [ ] `pytest tests/test_ladder.py::test_table_matches_spec` — parses the §3 markdown table out of `docs/SPEC-LADDER.md` and asserts that the (from, event, to) triples equal `TRANSITIONS` after the documented expansions. This proves the table is the only source and that it has not drifted.
- [ ] `pytest tests/test_ladder_property.py` — hypothesis, 2,000 random event sequences under random `ScriptedGuards`. Proves the machine:
  - never holds a state outside `LadderState`;
  - never enters ADVISING_SLOW or HOLDING without a `permit` `GuardDecision` in that checkpoint;
  - after HANDED_OFF, takes only the WATCH_AFTER_HANDOFF transition;
  - from any RESOLVED_*, CLEARED_AT_ASK, HAZARD_NOTICE, LOST_VEHICLE or FAILED state, never transitions again;
  - in WATCH_AFTER_HANDOFF, never changes rung.
- [ ] `pytest tests/test_ladder.py::test_vocabulary_clean` — every `LadderState`, `EventKind`, `GuardName` and `ActionKind` value and every boundary action name in `config/ladder.yaml` passes `soundings.schemas.denylist.find_denylisted` with no hits.
- [ ] `pytest tests/test_ladder_checkpoint.py` — proves:
  - every transition writes exactly one checkpoint;
  - the `prev_hash` chain links;
  - `LadderMachine.restore(store.load())` gives the same state, rung, pending timers and ledger keys;
  - re-delivering the last event after a restore executes no action twice (ledger hit), so one hold stays one hold.
- [ ] `pytest tests/test_clock.py` — proves `TimerSet` order is deterministic under ties, and `TickClock.now_s() == tick * tick_s`. An AST scan finds `time.time`, `time.monotonic` and `datetime.now` only in `soundings/clock.py::WallClock` and nowhere in `soundings/ladder/`.
- [ ] integration: `pytest tests/test_ladder_integration.py` — `LadderMachine` with `NoStandingOrdersGuards`, a `TickClock`, `LadderTimings.from_config()` over the real `config/ladder.yaml` and `config/standing_orders.yaml`, and a `JsonlCheckpointStore` in `tmp_path`. Scripted event streams shaped like each Phase 2 and 3 outcome, carrying real `EvidenceRow` payloads, reach CLEARED_AT_ASK, LOST_VEHICLE, RESOLVED_BENIGN, HAZARD_NOTICE, and HANDED_OFF → WATCH_AFTER_HANDOFF. The handoff stream never enters ADVISING_SLOW or HOLDING and records 0 interruptions (the unsigned path). `LadderMachine.restore` from the JSONL file reproduces every final state. Proves the machine, the store and the real config work together; the same outcomes on real catalog runs are WO-043's `test_ladder_driver.py`.
- [ ] determinism: `uv run pytest tests/test_ladder_integration.py -k pythonhashseed` — the scripted streams run in two subprocesses under `PYTHONHASHSEED=0` and `=1` print byte-identical checkpoint hashes, and two in-process runs give equal chain heads (CLAUDE.md §2.4).
- [ ] rule proof §2.2: `pytest tests/test_ladder.py::test_no_model_imports` — an AST scan of `soundings/ladder/{types,table,machine,checkpoint,actions,guard_port,ports}.py` and `soundings/clock.py` finds no import of `soundings.llm` or `anthropic`, so no model output can reach a transition.
- [ ] rule proof §2.1 and §2.5: the property test above, plus `test_vocabulary_clean`, plus `grep -riEf config/denylist.txt soundings/ladder soundings/clock.py tests/test_ladder*.py tests/test_clock.py` → empty; `make check` green.

## Interfaces
Exposed:
- WO-040 `soundings.clock.Clock`, `soundings.clock.TickClock`, `soundings.clock.WallClock`, `soundings.clock.Timer`, `soundings.clock.TimerSet`
- WO-040 `soundings.ladder.types.EventKind`, `.LadderEvent`, `.GuardName`, `.GuardVerdict`, `.GuardDecision`, `.ActionScope`, `.ActionKind`, `.LadderTimings`, `.GuardContext`
- WO-040 `soundings.ladder.table.TRANSITIONS`, `.TransitionRow`, `.RUNG_OF_STATE`, `.CLIMB_STATES`
- WO-040 `soundings.ladder.machine.LadderMachine`, `.StepResult`
- WO-040 `soundings.ladder.checkpoint.LadderCheckpoint`, `.CheckpointStore`, `.MemoryCheckpointStore`, `.JsonlCheckpointStore`
- WO-040 `soundings.ladder.actions.LadderAction`, `.ActionRecord`, `.ActionLedger`
- WO-040 `soundings.ladder.guard_port.GuardPort`, `.evidence_guard`, `.NoStandingOrdersGuards`
- WO-040 `soundings.ladder.ports.ApproverPort`, `.ApprovalReply`, `.NoReplyApprover`, `.DeparturePort` (`observe(contact_id, tick, point | None) -> bool`), `.LadderListener` (callback `(checkpoint, actions, decisions) -> None`; WO-042 attaches the audit sink here)

Consumed:
- WO-010 only: `soundings.schemas.LadderState`, `Rung`, `HANDLING_TERMINAL_STATES`, `TERMINAL_STATES`, `EvidenceRow`, `soundings.canonical.stable_hash`, `canonical_json`, `soundings.schemas.denylist.find_denylisted`, `soundings.schemas.config.load_ladder_config`, `load_standing_orders`.

## Cut line
If this WO runs more than an hour late, drop in this order: (1) the `CLIMB_STATES` helper; (2) the `PYTHONHASHSEED` subprocess variant (the in-process two-run check stays). Never an exit test; `JsonlCheckpointStore` stays because the integration test restores from it.

## Model calls
None. Paid run needed? No.
Dependency adds needing owner approval (CLAUDE.md section 3): none.

## Rungs touched
watch, ask (the evidence guards), slow and hold (states and rows exist; nothing permits them in this WO), hand to a person (HANDED_OFF, WATCH_AFTER_HANDOFF as a state only).

## Hard rules exercised
- §2.1: the property test shows the only way out of HANDED_OFF is WATCH_AFTER_HANDOFF, and no table row reaches a state after it.
- §2.2: `test_no_model_imports`; transitions depend only on `GuardDecision`s computed from evidence.
- §2.4: the two-run `diff`, the `PYTHONHASHSEED` test, and the `TickClock` AST scan.
- §2.5: `test_vocabulary_clean` and the grep.

## Done
PR: · Merged commit: · Test summary: · STATE.md updated: yes/no

## Coordinator addenda (2026-10-16 23:05 PT, binding; see docs/log/DECISIONS.md)
- Events `timer`, `scope_exceeded`, `handoff_complete` are confirmed additions to SPEC-LADDER §2 (record them in the WO's Done section and in the spec via a one-line docs edit in this PR).
- When slow and hold criteria both pass at the handoff recommendation, evaluate SPEC-LADDER §3 in table order: ADVISING_SLOW first, then HOLDING on the next `standing_order_check`. Keep the literal reading of 'nothing pre-authorized'; S05 geometry is tuned upstream (WO-011).
