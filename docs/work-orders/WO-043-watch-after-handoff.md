# WO-043 — watch-after-handoff

Phase: 4 · Branch: `wo/043-watch-after-handoff` · Worktree: `../soundings-worktrees/wo-043-watch-after-handoff` · Est: 7 h · Owner: Kudrat (planner / ladder / investigation)
Depends on: WO-010 (`Harbor.zone_at`, `Toggle`, `Expected`), WO-011 (`run_scenario(..., ticks=)`, `track_hash`, `catalog_paths`, `SimRun.truth_paths`), WO-012 (kinematic and context extractors for `ladder_monitor` rows), WO-022 and WO-033 (runner v1 and v2 in `soundings/runner.py`, `Recommendation`), WO-030 (`score_contact`), WO-040 (machine, ports, timers, `NoStandingOrdersGuards`), WO-041 (`BoundaryGuards`, `load_verified`, `scenario_now`, `ApproverScript.from_toggles`, `SimulatedApprover`), WO-042 (`HandoffService`, `AuditSink`, `AuditLog`, `Sitrep`). Wave 8, with WO-060.

## Goal
Observation continues after the handoff, and nothing else does. In WATCH_AFTER_HANDOFF, a contact that stays outside the security zone for `departed_after_s_outside_zone` becomes RESOLVED_DEPARTED. The same detector covers `HOLDING × contact_departed`. A contact that persists gets an updated SITREP every `sitrep_update_interval_s`, built from fresh evidence, with the rung unchanged. A driver turns runner v2 results into ladder events, and `soundings/runner.py` becomes runner v3: sim, then evidence, then ask and investigation (v2), then the ladder with guards, standing orders, approver, SITREP and audit, then watch-after-handoff, in one deterministic call. A verifier checks every catalog scenario's `expected` block against runner v3. That includes S05 passing through HOLDING under signed orders and going straight to HANDED_OFF when unsigned.

## Scope
In:
- `soundings/ladder/watch.py`:
  - `DepartureDetector(harbor, departed_after_s, tick_s)` implements WO-040's `DeparturePort`.
    - A contact counts as outside when its latest observed point has `Harbor.zone_at(lat, lon) is None`. The detector reports departure once the contact has been continuously outside for at least `departed_after_s`.
    - A tick with no detection keeps the last known point. A track lost while its last point is inside the zone is never a departure. The next SITREP update says "track lost at t, last known inside zone", citing the last position row.
  - `WatchSummary(contact_id, updates, departed_at_s, last_known)`.
  - `build_update_inputs(...)` collects the evidence rows added since the previous SITREP (fresh `ladder_monitor` kinematics) for `HandoffService` to render as a template `update`.
- `soundings/ladder/driver.py` (moved here from the WO-040 draft so WO-040 needs only WO-010):
  - `LadderDriver(guards, clock, timings, approver, listeners=(), departure=None, max_tail_ticks=...)`: `.run(run_result, sim_run) -> dict[str, LadderRun]`, and `.start(run_result, sim_run) -> DriverSession` with `.step()` (one tick) and `.done`, so runner v3 can step it.
  - The driver maps runner v2 events to ladder events in tick order:
    - the first detection becomes `contact_detected`;
    - the end of the watch probes becomes `probe_result` (free probes complete);
    - the ask outcome becomes `stop_rule_fired(CLEARED_AT_ASK|LOST_VEHICLE)` or `probe_result` (not cleared);
    - `Recommendation.outcome` becomes `stop_rule_fired(...)`;
    - while ADVISING_SLOW, it emits `standing_order_check(hold_permitted|not_permitted)` only when one of those holds.
  - Each event payload carries the evidence row ids that produced it, the runner v2 `probe_calls` (probe_id, idempotency_key, cost_usd) and, for the handoff, `recommendation_hash`. WO-042's `AuditSink` records these.
  - On `deliver_sitrep` and `request_approval` it builds the WO-042 `SitrepContext` with `question_for_human` and `uncertain` taken from the case's `Recommendation`.
  - It refreshes `time_to_nearest_critical_asset` and `zone_criticality` per tick by running WO-012's kinematic and context extractors on the track prefix. The new rows are appended to the table with source `ladder_monitor`, so guards still read only evidence.
  - The driver emits `handoff_complete` in the same tick, once the `deliver_sitrep` action is recorded. Departures come only through the `departure` hook (`DeparturePort`; runner v3 passes `DepartureDetector`).
  - Once the observations end, it keeps advancing the `TickClock` for up to `max_tail_ticks` while a timer is pending. Only timer and approval events fire in that tail.
  - Each result is `LadderRun(contact_id, checkpoints, actions, guard_decisions, handling_outcome, final_state, interruptions)`. `handling_outcome` is the first state the run reaches in `HANDLING_TERMINAL_STATES`.
- `soundings/runner.py` (runner v3, only inside the block `# --- v3 (WO-043) ---`; small commits, rebase early):
  - `ScenarioRunner(scenario, *, seed=None, ...)` keeps WO-022's constructor. `.run(*, llm_roles=False, violations_path=..., fixture_store=None, orders_path="config/standing_orders.yaml", unsigned=False, approver=None, db_url=None) -> RunResult`. `approver` takes any WO-040 `ApproverPort`; the default is `SimulatedApprover(ApproverScript.from_toggles(scenario.toggles))`.
  - `.start(**same kwargs) -> RunSession` with `step() -> list[StepRecord]`, `done`, `pending_approvals()`, and `result() -> RunResult`. `start()` precomputes the sim and the v1/v2 results (they never depend on approvals), then each `step()` advances the ladder driver and the watch by one tick. `run()` is `start()` stepped to the end, so both give identical results. WO-061's live approval path drives `start()` with a UI-backed `ApproverPort` and never edits this file.
  - The `RunResult` gains:
    - `ladder: dict[str, LadderRun]`;
    - `handling_outcome: dict[str, LadderState]`;
    - `sitreps: dict[str, list[Sitrep]]`;
    - `watch: dict[str, WatchSummary]`;
    - `audit_run_id`;
    - and the v1 fields `interruptions` and `probes_used` now also count approval requests and backup pages.
  - Every v2 field and behaviour is kept, so Phase 2 and 3 tests stay green.
  - The sim runs for `duration_ticks + tail_ticks`, where `tail_ticks = ceil((hold.max_minutes·60 + primary.timeout_s + backup.timeout_s + 2·sitrep_update_interval_s + departed_after_s_outside_zone) / tick_s)`. All values come from config, never literals.
  - `scenario.toggles` are applied:
    - `approver_unreachable` and `network_down` go through `ApproverScript.from_toggles`;
    - `standing_orders_expired` moves `scenario_now` past `valid_until`;
    - `network_down` and `approver_unreachable` also set `degraded_reasons`.
  - The approval key is `SOUNDINGS_APPROVAL_KEY`, or the labelled demo key.
- S05 timeline under signed orders (run clock, approximate). The hold starts once `time_to_nearest_critical_asset < 300 s`. `timer(hold_limit)` fires 900 s later. The default scripted primary grants 60 s after that. The handoff therefore lands after the scenario's 1,200 s, which is why the tail exists. WO-062 replays this on the `TickClock` at demo speed; no timer is shortened, because thresholds are config (CLAUDE.md §3).
- `soundings/ladder/verify.py`:
  - `check_expected(result, scenario) -> list[ExpectationFailure]` covers `terminal_state` (compared with `handling_outcome`), `passes_through` (each listed state appears in the checkpoints, in order), `min_threat_mass`, `max_human_interruptions` (from `ActionLedger.interruptions`), `max_probes`, `top_hypothesis`, `sitrep_has_search_box` and `search_box_contains_truth`.
  - `python -m soundings.ladder.verify --suite catalog [--unsigned] [--json]` runs each catalog scenario at its own seed through runner v3 into a tmp DB. It runs `AuditLog.verify()` per run, prints one row per scenario, and exits 0 only if every expectation and every chain holds.
- `soundings/ladder/__main__.py`: `python -m soundings.ladder --scenario <id> --seed <n> [--orders PATH] [--unsigned] [--sitrep-out PATH] [--audit-db PATH]` runs runner v3 and prints canonical JSON of each `LadderRun`; with no `--orders` it uses `NoStandingOrdersGuards`.
- `tests/fixtures/scenarios/X01-handoff-then-departs.yaml`, outside the catalog glob: an S05-like unfiled, non-replying, tonal contact that crosses `fuel_berths` and then makes a transit to a waypoint outside every harbor zone. It uses the waypoint form WO-011's `BehaviorSpec` supports (as S04 does). `expected.terminal_state: HANDED_OFF`.

Out: Phase 5 policies and metrics; Phase 6 API and WS streaming (they call `ScenarioRunner.run` or `start`); any state, action or SITREP section that describes acting on the contact after the handoff. Watching is the only thing allowed after it (CLAUDE.md §2.1).

Parallel start: `driver.py`, `watch.py` and `verify.py` can start against WO-040's ports and WO-033's documented `RunResult` with synthetic data. Runner v3 wiring waits for WO-033 and WO-042.

## Spec references
`docs/SPEC-LADDER.md §1` (WATCH_AFTER_HANDOFF, RESOLVED_DEPARTED), `§3` (rows from HOLDING, HANDED_OFF and WATCH_AFTER_HANDOFF) · `config/ladder.yaml` `watch_after_handoff` · `docs/BUILD-PLAN.md` Phase 4 · `docs/SPEC-SIM.md` (catalog `expected`) · `CLAUDE.md §2.1, §2.4, §2.6` · `docs/WORKFLOW.md §8`.

## Files expected
- `soundings/ladder/driver.py`, `watch.py`, `verify.py`, `__main__.py`
- `soundings/runner.py` (`# --- v3 (WO-043) ---` block only)
- `tests/test_ladder_driver.py`, `tests/test_watch_after_handoff.py`, `tests/test_runner_v3.py`, `tests/test_catalog_expected.py`
- `tests/fixtures/scenarios/X01-handoff-then-departs.yaml`

## Exit tests (must all pass with `MODEL_MODE=replay`, no network)
- [ ] `pytest tests/test_watch_after_handoff.py` — after handoff, a contact that leaves the zone becomes `RESOLVED_DEPARTED`; one that persists re-issues an updated SITREP at the configured interval without climbing the ladder.
- [ ] `pytest tests/test_watch_after_handoff.py::test_detector_units` — synthetic point sequences. Proves:
  - outside for `departed_after_s - tick_s` gives no event; one more tick gives the event;
  - an excursion back inside resets the count;
  - a lost track with its last point inside never departs;
  - a contact departing while HOLDING gives RESOLVED_DEPARTED and a `release_hold` action;
  - the thresholds come from `config/ladder.yaml`.
- [ ] `pytest tests/test_watch_after_handoff.py::test_persisting_updates` — S06 at seed 13 through runner v3 with signed temp orders. Proves:
  - after HANDED_OFF, `update` SITREPs arrive exactly every `sitrep_update_interval_s` of run clock until the run ends;
  - each update cites at least one evidence row newer than the previous SITREP;
  - `recommended_rung == hand_to_person` and the machine rung is constant on every update;
  - zero `llm_call` audit rows come from updates (the template only; budget: 1 LLM call per handoff);
  - no ADVISING_SLOW, HOLDING or AWAITING_APPROVAL checkpoint follows HANDED_OFF.
- [ ] `pytest tests/test_catalog_expected.py::test_check_expected_units` — table-driven over synthetic `RunResult`s. Proves `check_expected` flags each of these:
  - a wrong terminal state;
  - a missing `passes_through` state, or one out of order;
  - interruptions over the maximum and probes over the maximum;
  - threat mass below the minimum;
  - a missing search box, and a box that excludes the truth.

  It passes a clean run, so the verifier itself is trusted before it judges the catalog.
- [ ] integration: `pytest tests/test_ladder_driver.py` — real catalog scenarios at their own seeds through runner v2 and `LadderDriver` with `NoStandingOrdersGuards` (moved from the WO-040 draft): S01 `CLEARED_AT_ASK`; S02 `LOST_VEHICLE`; S04 and S07 `RESOLVED_BENIGN`; S08 `HAZARD_NOTICE`; S05 `HANDED_OFF` with no ADVISING_SLOW or HOLDING checkpoint and 0 interruptions. Proves the machine reproduces each Phase 2 and 3 outcome, and the unsigned path, end to end.
- [ ] integration: `pytest tests/test_runner_v3.py` — S05 at seed 11 through `ScenarioRunner.run` with signed temp orders. Proves:
  - `passes_through` HOLDING;
  - `handling_outcome == HANDED_OFF`;
  - interruptions ≤ 1 and probes ≤ 4;
  - max posterior `UNAUTHORIZED_UUV` ≥ 0.6;
  - the audit verifies.

  With `unsigned=True`, the same run goes INVESTIGATING → HANDED_OFF with no slow or hold checkpoint. X01 ends RESOLVED_DEPARTED after HANDED_OFF, and the final `close_watch` action is audited. The first `duration_ticks` of the extended sim are byte-identical to `run_scenario(scenario, seed=seed)` (compare `track_hash` over the prefix), so the tail does not change Phase 1–3 behaviour. Handoff checks (moved from the WO-042 draft): S05 delivers one draft (at AWAITING_APPROVAL) and one final SITREP; S01 delivers none; S02's SITREP has the search box containing the truth; each run's audit verifies with exactly one `llm_call` row per final SITREP.
- [ ] `pytest tests/test_runner_v3.py::test_step_session` — `start()` stepped tick by tick with a scripted `ApproverPort` gives a `RunResult` and audit chain head identical to `run()`, and `pending_approvals()` is non-empty exactly while S05 sits in AWAITING_APPROVAL. Proves WO-061 can drive approvals live without touching `runner.py`.
- [ ] integration: `pytest tests/test_catalog_expected.py` — S01–S08 at their own seeds through runner v3 (signed temp orders, zero fixtures). `check_expected` returns no failures for any scenario. Expected outcomes: S01 and S03 `CLEARED_AT_ASK` with 0 interruptions; S02 `LOST_VEHICLE` whose search box contains the truth; S04 and S07 `RESOLVED_BENIGN`; S05 HOLDING then HANDED_OFF; S06 HANDED_OFF; S08 `HAZARD_NOTICE`. `uv run python -m soundings.ladder.verify --suite catalog` exits 0.
- [ ] `pytest tests/test_runner_v3.py::test_toggles` — catalog S05 copies with toggles. Proves:
  - `approver_unreachable` pages the backup once and ends HANDED_OFF with the SITREP flag `unreachable`;
  - `standing_orders_expired` gives the unsigned path;
  - `network_down` sets `degraded_reasons`, the pre-authorized hold still runs within scope, and nothing above hold is ever permitted.
- [ ] `pytest tests/test_investigation.py tests/test_ask_rung.py` — still green on runner v3. This proves the v3 fields were added without changing any v2 result: the S05 posterior, S02's LOST_VEHICLE, and S01/S03 at ask.
- [ ] determinism: `MODEL_MODE=replay uv run python -m soundings.ladder.verify --suite catalog --json > /tmp/v1.json; MODEL_MODE=replay uv run python -m soundings.ladder.verify --suite catalog --json > /tmp/v2.json; diff /tmp/v1.json /tmp/v2.json` → empty. The JSON holds each run's checkpoint hashes, SITREP digests and audit chain head. Also `MODEL_MODE=replay uv run python -m soundings.ladder --scenario S05 --seed 11 --orders config/standing_orders.yaml --sitrep-out /tmp/s1.md --audit-db /tmp/a1.db > /tmp/l1.json`, run again into `/tmp/s2.md`, `/tmp/a2.db` and `/tmp/l2.json`; `diff /tmp/l1.json /tmp/l2.json && diff /tmp/s1.md /tmp/s2.md` → empty. And `test_runner_v3.py::test_pythonhashseed`, where S05 under `PYTHONHASHSEED=0` and `PYTHONHASHSEED=1` gives equal audit chain heads (CLAUDE.md §2.4).
- [ ] rule proof §2.1: `test_watch_after_handoff.py::test_watch_only_observes`. Over S05, S06 and X01, every action after HANDED_OFF is in {`deliver_sitrep`, `start_watch`, `update_sitrep`, `close_watch`}, and every guard decision recorded after HANDED_OFF is empty. A hypothesis variant feeds random persist and depart sequences into WATCH_AFTER_HANDOFF and never sees a rung change.
- [ ] rule proof §2.6: `test_persisting_updates` citations above, plus `validate_sitrep` on every update. `grep -riEf config/denylist.txt soundings/ladder soundings/runner.py tests/test_watch_after_handoff.py tests/test_runner_v3.py tests/test_catalog_expected.py tests/fixtures/scenarios` → empty; `make check` green.

## Interfaces
Exposed:
- WO-043 `soundings.ladder.driver.LadderDriver`, `.DriverSession`, `.LadderRun` (`contact_id, checkpoints, actions, guard_decisions, handling_outcome, final_state, interruptions`)
- WO-043 CLI `python -m soundings.ladder --scenario <id> --seed <n> [--orders PATH] [--unsigned] [--sitrep-out PATH] [--audit-db PATH]`
- WO-043 `soundings.ladder.watch.DepartureDetector`, `.WatchSummary`, `.build_update_inputs`
- WO-043 `soundings.runner.ScenarioRunner.run` (v3 signature above), `soundings.runner.ScenarioRunner.start`, `soundings.runner.RunSession` (`step`, `done`, `pending_approvals`, `result`), `soundings.runner.StepRecord`, `soundings.runner.RunResult` (v3 fields `ladder`, `handling_outcome`, `sitreps`, `watch`, `audit_run_id`, `interruptions`, `probes_used`)
- WO-043 `soundings.ladder.verify.check_expected`, `.ExpectationFailure` (CLI `python -m soundings.ladder.verify --suite catalog [--unsigned] [--json]`)
- File `tests/fixtures/scenarios/X01-handoff-then-departs.yaml`

Consumed:
- WO-040: `soundings.ladder.machine.LadderMachine`, `soundings.ladder.ports.{DeparturePort, ApproverPort, LadderListener}`, `soundings.ladder.guard_port.NoStandingOrdersGuards`, `LadderTimings`, `ActionLedger.interruptions`, `soundings.clock.TickClock`.
- WO-012: `soundings.investigation.features.extract_kinematics`, `extract_context`; WO-030: `soundings.investigation.posterior.score_contact`.
- WO-041: `BoundaryGuards`, `ApproverStatus`, `load_verified`, `effective_scope`, `scenario_now`, `ApproverScript.from_toggles`, `SimulatedApprover`, `load_boundary`.
- WO-042: `HandoffService`, `AuditSink`, `AuditLog.verify`, `Sitrep`, `SitrepContext`, `context_from`, `validate_sitrep`.
- WO-010: `soundings.schemas.load_harbor`, `Harbor.zone_at`, `Scenario.toggles`, `Expected`, `LadderState`.
- WO-011: `soundings.sim.run.run_scenario(..., ticks=)`, `soundings.sim.hashing.track_hash`, `SimRun.truth_paths`, `soundings.sim.scenario.catalog_paths`.
- WO-022 and WO-033: `soundings.runner.RunResult` v1/v2 fields (`transitions`, `cases`, `evidence`, `notices`, `recommendations`, `probe_calls`, `sim`), and `soundings.investigation.machine.Recommendation`.

## Cut line
If this WO runs more than an hour late, drop in this order: (1) the `--unsigned` flag on the verify CLI (the unsigned S05 test stays in `test_runner_v3.py`); (2) the `network_down` toggle wiring in the runner (`approver_unreachable` and `standing_orders_expired` stay); (3) the "track lost" sentence in updates, leaving a plain update; (4) per-tick re-extraction in the driver, which falls back to re-extracting only on ticks where the machine sits in ADVISING_SLOW or HOLDING. Never an exit test.

## Model calls
Role `sitrep_writer` only, through WO-042's `HandoffService`: at most 1 call per handoff, replay by default. Watch updates are template only. Paid run needed? No.
Dependency adds needing owner approval (CLAUDE.md section 3): none.

## Rungs touched
hand to a person (watch after handoff, SITREP updates); hold (a departure while HOLDING releases the hold); every rung end to end through runner v3.

## Hard rules exercised
- §2.1: `test_watch_only_observes` and its hypothesis variant.
- §2.4: the catalog JSON `diff`, the `PYTHONHASHSEED` chain heads, and the prefix-equality test for the extended sim.
- §2.6: update citations and `validate_sitrep`.
- §2.2: runner v3 passes only `GuardDecision`s to the machine; `tests/test_watch_after_handoff.py::test_no_model_imports` runs WO-040's AST scan over `driver.py`, `watch.py` and `verify.py`.

## Done
PR: part A #26; part B see STATE (`wo/043b-runner-v3`) · Merged commit: squash on merge · Test summary: part B adds tests/test_runner_v3.py 20, tests/test_catalog_expected.py 11 (S04 strict xfail), 3 in tests/test_watch_after_handoff.py, 6 in tests/test_ladder_driver.py · STATE.md updated: yes (WO-043 row)

Part B (2026-10-17 08:30 PDT, `wo/043b-runner-v3`; rebased on #45 at 09:40). No model was called.
- `soundings/runner.py` block `# --- WO-043 begin: runner v3 ---`: `ScenarioRunnerV3(scenario, seed=...)` (subclasses v2; v1 `ScenarioRunner.run()` and its CLI untouched) with `.run(**kw) -> RunResultV3` and `.start(**kw) -> RunSession`; kwargs `llm_roles, violations_path, fixture_store, orders_path=config/standing_orders.yaml (None = unsigned), unsigned, approver (any ApproverPort), approver_script ("default"|"denied"|"unreachable"), toggles, db_url (audit DB; default ":memory:"), writer_mode ("template"|"auto"), sitrep_client (a replay ModelClient for auto; runner.py imports nothing from soundings.llm), run_id`. Runner v2 runs unchanged on `duration_ticks`; the ladder observes `run_scenario(..., ticks=duration+tail)` with `tail = min(default_tail_ticks(...), duration_ticks)` (prefix-stable, tested with `track_hash(truncate_run(...))`). `RunSession`: `step() -> list[StepRecord]`, `done`, `tick`, `pending_approvals()`, `open_requests()`, `approve(contact_id, "granted"|"denied")` (needs `approver=ManualApprover()`), `run_to_end()`, `result()`. `ManualApprover` is the live approvals path (`reply(contact_id, decision, at_s, token=)`; a grant carries the WO-041 token the approval UI issued, since `issue_token` is only called from approver paths per tests/test_approval.py; the guard checks it against the requested scope).
- `RunResultV3(RunResultV2)` adds `ladder`, `handling_outcome`, `sitreps` (WO-042 `Sitrep`s by contact), `sitrep_packets` (`HandoffPacket`s), `watch`, `audit_run_id`, `audit_db`, `audit_head`, `audit_ok`, `audit_counts`, `audit`, `ladder_transitions` (`LadderTransition`: seq, contact_id, case_id, tick, t_s, from_state, to_state, event, arg, action, asset_id, probe_id, rung, hash), `ladder_log_hash`, `contact_interruptions`, `contact_cases`, `planner_stats`, `degraded_reasons`, `orders`, `sim_extended`, `tail_ticks`; `interruptions` = per contact eyes-on + request_approval + page_backup + 1 for a handoff SITREP not preceded by an approval request; `probes_used` gains `request_approval` and `page_backup` counts.
- Feeds: one ladder per contact (its first case); detection/watch/ask from the v2 transitions and their rows; the recommendation at its stop tick; after a HANDOFF, `ladder_monitor` snapshots (WO-012 kinematic + context rows on the track prefix, re-labelled) every `evidence_every_ticks` observed tick. A slow advisory's basin is the basin of the critical asset the contact is nearest to (DECISIONS 08:30).
- `soundings/eval/catalog_verify.py` (not `soundings/ladder/verify.py`: tests/test_boundary.py, merged #45, keeps ground truth and `expected` out of the ladder package): `grade(result) -> Graded`, `check_expected(result|Graded, scenario|Expected) -> list[ExpectationFailure]`, `verify_scenario`, CLI `python -m soundings.eval.catalog_verify --suite catalog [--json] [--scenario ID]` and `python -m soundings.runner verify ...`. Local checker: WO-051 B1's `soundings.eval.grader.grade` takes a `PolicyRun`, which needs WO-051 B2's adapter.
- `python -m soundings.runner ladder --scenario --seed --orders --unsigned --toggle --sitrep-out --audit-db` runs runner v3 (`--audit-db` is the WO-042 hash-chained log). `python -m soundings.ladder` stays part A's synthetic stream, because the ladder package may not import the runner (it reaches the sim; tests/test_boundary.py).
- Results (signed orders, catalog seeds): S01/S03 CLEARED_AT_ASK; S02 LOST_VEHICLE, box contains truth; S05 lineage c-0002 HANDED_OFF (0.22), c-0004 ADVISING_SLOW 122 -> HOLDING 197 -> AWAITING_APPROVAL 647 -> granted HANDED_OFF 677, c-0008 HANDED_OFF 539 (0.79; parked at fuel_berth_2 so time-to-asset is not below the hold max); S06 HANDED_OFF; S07 RESOLVED_BENIGN; S08 HAZARD_NOTICE; S04 lineage end HANDED_OFF (WO-033's C_BUDGET re-acquisition; strict xfail, expected block unchanged; primary RESOLVED_BENIGN, 0 interruptions).
- Cut: `--unsigned` on the verify CLI (cut 1); X01 fixture and its departure test not built (the departure path stays covered by part A's synthetic tests). BT-01 not on main, so not run.

## Coordinator addenda (2026-10-16 23:05 PT, binding; see docs/log/DECISIONS.md)
- Runner v3 may extend the sim past `duration_ticks` (cap 2×) until a terminal-for-handling state; relies on WO-011 prefix stability.

## Part A done (coordinator split)
Built (branch `wo/043a-ladder-driver-watch`):
- `soundings/ladder/driver.py`: `LadderDriver(guards, clock, timings, approver, listeners=(), departure=None, max_tail_ticks=0, *, sitrep_writer=None, criteria=None)` with `.start(feeds) -> DriverSession` (`step()`, `done`, `tick`, `pending_approvals()`, `results()`) and `.run(feeds) -> dict[str, LadderRun]`; `LadderDriver.from_config(tick_s=, hour_of_day=, orders_path=, unsigned=, toggles=, approver=, script=, departure=, max_tail_ticks=, listeners=, sitrep_writer=)` builds `NoStandingOrdersGuards` (unsigned / no orders) or `BoundaryGuards` with `epoch=scenario_now(hour, 0)`; `standing_orders_expired` moves the epoch to `valid_until`; default approver `SimulatedApprover(ApproverScript.from_toggles(...), token_ttl_s=approval.token_ttl_s)`; channels from the signed orders.
- Protocols and inputs: `RecommendationLike` (outcome, recommended_rung, threat_mass, benign_mass, rows, basin, nearest_asset_id, time_to_nearest_critical_asset, tick); `Snapshot` (a plain implementation); `ContactFeed(contact_id, detected_tick, detection_rows, watch_tick, watch_rows, ask_tick, ask_outcome, ask_rows, recommendations, points, scope_requests, probe_calls)`.
- `LadderRun(contact_id, checkpoints, actions, guard_decisions, handling_outcome, final_state, interruptions, sitreps, watch)` with `.visited` and `.canonical()`; `transition_log(runs)`; `default_tail_ticks(timings, hold_max_minutes, tick_s)`; `token_ttl_s_from_config()`.
- `soundings/ladder/sitrep_port.py`: `SitrepWriterLike` (`render(inputs) -> str`), `SitrepInputs`, `RenderedSitrep`, `LastKnown`, `StubSitrepWriter` (labelled template), `UNREACHABLE`.
- `soundings/ladder/watch.py`: `DepartureDetector(harbor, departed_after_s, tick_s)` (+ `from_config`, `last_known`, `track_lost_at`), `WatchSummary`, `build_update_inputs(...)`, `departed_after_s_from_config()`.
- `soundings/ladder/machine.py`: opt-in `strict_holds` (release at limit, release a granted hold on departure, drop no-row timers outside their owner state; `TIMER_OWNER`, `TIMER_DROPPED`, `hold_scope`); `actions.HOLD_LONGER` (re-exported by machine); `ActionLedger.active_holds` counts a granted `hold_longer`.
- `config/ladder.yaml`: additive `approval: {token_ttl_s: 600}`; `LadderConfig.approval` (optional).
- `soundings/ladder/synthetic.py` (`s05_feed(ticks=, depart_at_tick=)`) and `python -m soundings.ladder --scenario S05 --seed N [--orders PATH] [--unsigned] [--toggle NAME] [--sitrep-out PATH] [--audit-db PATH]` (replays the synthetic S05 stream; `--audit-db` is a plain SQLite stand-in until WO-042's sink).
- Tests: `tests/test_ladder_driver.py` (14), `tests/test_watch_after_handoff.py` (9, BUILD-PLAN items, detector units, hypothesis rung check).

Remaining for Part B: runner v3 in `soundings/runner.py` (build `ContactFeed`s from runner v2 results + `SimRun`, per-tick re-extraction of `ladder_monitor` rows with WO-012 extractors, `ScenarioRunner.start/RunSession/StepRecord`, tail sizing, toggles, `degraded_reasons`); swap `StubSitrepWriter` for WO-042's writer and the SQLite stand-in for `AuditSink`/`AuditLog`; `SitrepContext` `question_for_human`/`uncertain`; `soundings/ladder/verify.py` and its CLI; X01 fixture; `__main__` over every catalog scenario via the runner; `test_runner_v3.py`, `test_catalog_expected.py`, the real-scenario `test_ladder_driver.py` integration cases (S01/S02/S04/S07/S08/S05), `test_persisting_updates` on S06, `test_no_model_imports` over `verify.py`; allow `release_hold` (granted hold, departure) in `test_watch_only_observes`.
