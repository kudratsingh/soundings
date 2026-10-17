# WO-083 — separation-advisories

Phase: 7 · Branch: `wo/083-separation-advisories` · Worktree: `../soundings-worktrees/wo-083-separation-advisories` · Est: 4.5 h · Owner: coordinator Claude / Kudrat
Depends on: WO-080 part A (`SimRun.ais_tracks`, `AisReport`, scenario BT-01), WO-041 (`BoundaryGuards`, `GuardInputs`, `slow_permitted`, `boundary_decision`, `StandingScope`), WO-042 part A (in flight: PR #18, `origin/wo/042a-audit-sitrep-template`; `AuditLog.append`, `AuditKind`, `guard_decision_payload`, `ActionPayload`; WO-083 merges after it), WO-033 (only for the gated rogue assertion in the BT-01 integration, as in WO-080), WO-022 (`ScenarioRunner`, `CaseResult`, `matcher.in_window`), WO-012 (`track_arrays`, `last_fitted_state`), WO-010 (`to_local_m`, `Harbor`). Wave B.

## Goal
For every vessel transit and every in-water filed job, Soundings computes the closest point of approach (CPA) and the time to CPA (TCPA) each tick with the standard ARPA formulas. When CPA < `separation_m` and 0 < TCPA < `advisory_window_s`, it proposes a `slow` advisory (`advisory_one_basin`) for the job's basin. The proposal goes through the existing standing-orders guard in `soundings/ladder/guards.py`, exactly like any other slow. It is issued only when signed, unexpired orders put that basin in their slow scope. It is blocked when the orders are unsigned or expired. Every decision is written to the audit log with the geometry it rests on. The advisory tells vessel operators about a nearby job, and the operator accepts or ignores it. Soundings plans no route and commands no vessel. This WO adds no ladder state, and no filed contact leaves CLEARED_AT_ASK.

## Scope
In:
- `soundings/advisories/__init__.py`, `soundings/advisories/cpa.py` (pure, numpy-free):
  - `cpa_tcpa(own_pos, own_vel, other_pos, other_vel) -> CpaResult(cpa_m, tcpa_s)`. Inputs are local metres (x east, y north) and m/s.
  - The formula: r = other_pos − own_pos and v = other_vel − own_vel. TCPA = −(r·v)/|v|², and CPA = |r + v·TCPA|. When |v|² < 1e-9 (no relative motion), TCPA = 0.0 and CPA = |r|. A negative TCPA is returned as computed, meaning the pair is already opening. Values are rounded to 6 dp.
  - `velocity_from_course(speed_mps, heading_deg) -> (vx, vy)`. The heading is compass (0 = north, clockwise), so vx = s·sin h and vy = s·cos h.
- `soundings/advisories/separation.py`:
  - `AdvisoryConfig` (strict) and `load_advisory_config(path="config/advisories.yaml")`.
  - `VesselFix(vessel_id, tick, lat, lon, speed_mps, heading_deg)`, built by `vessel_fixes(sim, tick) -> list[VesselFix]` from WO-080's `SimRun.ais_tracks` only (never `truth_paths`): the latest `AisReport` at or before `tick`, dead-reckoned to `tick * tick_s` with its `sog_mps`/`cog_deg`, skipped when older than `2 × ais.report_interval_s`; sorted by `vessel_id`. And `JobFix(plan_id, contact_id, basin, tick, x_m, y_m, vx, vy, velocity_source)`.
  - `in_water_jobs(result, plans, harbor, tick, timeout_ticks) -> list[JobFix]`. A job qualifies when all four hold:
    - the plan is `in_window` at `tick`;
    - a `CaseResult` has `state == CLEARED_AT_ASK`, `outcome.plan_id == plan_id` and `closed_tick <= tick`;
    - the contact has a reading in `(tick − timeout_ticks, tick]`;
    - `last_fitted_state` over the track prefix up to `tick` is not `None`.
    The basin is `harbor` zone `plan.area`.basin. Jobs are sorted by `plan_id`.
  - `separation_check(vessels, jobs, cfg) -> list[SeparationAdvisory]` is pure. It takes vessels × jobs in `(vessel_id, plan_id)` order and returns those inside both thresholds (strict `<` on both edges). Each `SeparationAdvisory(basin, vessel_id, plan_id, contact_id, tick, cpa_m, tcpa_s, inputs)` carries `inputs = SeparationFacts`: both positions and velocities, `velocity_source`, `separation_m` and `advisory_window_s`.
  - `SeparationAdvisor(guards, audit, run_id, cfg)`.
    - `.step(tick, tick_s, vessels, jobs) -> list[AdvisoryDecision]`. Every advisory that opens a new encounter goes through the guard. An encounter is a `(vessel_id, plan_id)` pair that is inside both thresholds and was not inside them at the previous tick. An encounter that stays open is not re-decided.
    - `AdvisoryDecision(advisory, decision: GuardDecision, status ∈ {issued, blocked}, granted_scope, audit_seq)`.
  - `advise_run(result, plans, harbor, guards, audit, run_id, cfg) -> SeparationReport` steps every tick of `result.sim` in order.
- `soundings/ladder/guards.py` (additive):
  - `SeparationFacts` (strict, no free text) and `separation_slow_permitted(inputs, criteria, facts) -> GuardDecision` with `guard = GuardName.SLOW_PERMITTED`.
  - The standing-orders half of `_slow_problem` is factored out as `_slow_scope_problem`: signed orders, slow in the scope, the degraded-mode narrowing, and the basin in `scope.slow.basins`. Both guards call it. The new guard swaps the threat_mass and time criteria for `facts.cpa_m < separation_m` and `0 < facts.tcpa_s < advisory_window_s`. `slow_permitted` behaves exactly as before.
  - `inputs_hash` is `stable_hash` over `{guard_inputs, facts}`. The granted scope is `ActionScope(action=criteria.slow.action, basin, max_minutes=scope.slow.max_minutes)`, and it is then checked with `scope_exceeded`.
  - `BoundaryGuards.separation_decision(facts, *, contact_id, basin, now_s) -> tuple[GuardDecision, GuardInputs]` builds the inputs through `status_at(now_s)` and `effective_scope`, so an expired epoch blocks.
- Audit (WO-042 part A, real names on its branch):
  - Each decision writes `AuditLog.append(AuditKind.GUARD_DECISION, guard_decision_payload(decision, {"guard_inputs": ..., "separation": facts}), run_id=..., t_s=tick*tick_s, contact_id=contact_id)`.
  - An issued advisory also writes `AuditKind.ACTION` with `ActionPayload(action="advisory_one_basin", idempotency_key=f"sep:{vessel_id}:{plan_id}:{tick}", rung=Rung.SLOW)`.
- `config/advisories.yaml`: `separation_m: 150`, `advisory_window_s: 600`, with a stress-test header comment.

Out:
- No new LadderMachine rows or states.
- Nothing about route planning, vessel commands or slot changes (WO-082).
- UI rendering of advisories (WO-086).
- Kalman velocity (WO-084 may later set `velocity_source="kalman"`).
- Widening the standing-orders basins, which would need a re-sign and the owner.
- Any change to `config/ladder.yaml` thresholds.

## Spec references
`docs/SPEC-LADDER.md §3` (slow row), `§4` (boundary: `advisory_one_basin` is `standing_orders`), `§5` (standing orders, degraded mode), `§8` (audit) · `CLAUDE.md §2.1, §2.2, §2.4, §2.5, §2.6, §3` · WO-041 Notes for downstream ("a slow permit needs `payload.basin` in the orders' basins") · `config/standing_orders.yaml` (slow basins: `inner_harbor_west`, 30 min).

## Files expected
- `soundings/advisories/__init__.py`, `cpa.py`, `separation.py`
- `soundings/ladder/guards.py` (additive block `# --- WO-083: separation ---` for `SeparationFacts`, `separation_slow_permitted`, `BoundaryGuards.separation_decision`; the `_slow_scope_problem` extraction is the one named edit outside it)
- `docs/STATE.md` (WO-083 row only), `docs/log/DECISIONS.md` (the rows listed under Model calls)
- `config/advisories.yaml`
- `tests/test_cpa.py`, `tests/test_separation.py`, `tests/test_separation_guard.py`, `tests/test_separation_bt01.py`

## Exit tests (must all pass with `MODEL_MODE=replay`, no network)
From the owner directive (verbatim):
- [ ] known-geometry tests (head-on, crossing, diverging) give the hand-computed CPA/TCPA to 1 m / 1 s; advisory fires only inside both thresholds; blocked when orders unsigned; advisory appears in the audit log with its inputs.

Each clause as a command:
- [ ] known-geometry tests (head-on, crossing, diverging) give the hand-computed CPA/TCPA to 1 m / 1 s — `pytest tests/test_cpa.py::test_known_geometry`. Proves the ARPA formulas against these hand-computed values (own vessel first, positions in m, velocities in m/s):
  - head-on: own (0,0) at (5,0); other (1000,50) at (−5,0). Expect CPA 50.0 m, TCPA 100.0 s.
  - crossing: own (0,−500) at (0,5); other (−700,0) at (6,0). Expect CPA 64.018 m, TCPA 109.836 s.
  - wide crossing: other (−900,0). Expect CPA 192.055 m, TCPA 129.508 s.
  - diverging: own (0,0) at (5,0); other (200,100) at (8,0). Expect CPA 100.0 m, TCPA −66.667 s.
  - parallel, same velocity: own (0,0) at (3,0); other (100,120) at (3,0). Expect CPA 156.205 m, TCPA 0.0 s.
  - far: own (0,0) at (1,0); other (7000,0) at (−1,0). Expect CPA 0.0 m, TCPA 3500.0 s.
- [ ] advisory fires only inside both thresholds — `pytest tests/test_separation.py::test_thresholds`. Proves:
  - of the six cases above, only head-on and crossing produce a `SeparationAdvisory`. Wide crossing fails on CPA. Diverging and parallel fail on TCPA ≤ 0. Far fails on TCPA ≥ 600.
  - edge cases at CPA = `separation_m` ± 0.5 m and TCPA = `advisory_window_s` ± 1 s, plus 0 s, fire only strictly inside. The thresholds are read from `config/advisories.yaml`, never written as literals in the test.
- [ ] blocked when orders unsigned — `pytest tests/test_separation_guard.py::test_blocked_unsigned_or_expired`. Proves the same head-on advisory for basin `inner_harbor_west` behaves as follows:
  - signed temp orders: `issued`, `granted_scope == ActionScope(action="advisory_one_basin", basin="inner_harbor_west", max_minutes=30)`;
  - the placeholder signature: `blocked`, reason `standing_orders_unsigned`;
  - an epoch past `valid_until`: `blocked`;
  - basin `main_channel`: `blocked`, reason `basin_out_of_standing_scope`.
- [ ] advisory appears in the audit log with its inputs — `pytest tests/test_separation_guard.py::test_audited_with_inputs`. On an in-memory `AuditLog`:
  - each decision writes one `guard_decision` row whose payload `inputs.separation` holds `cpa_m`, `tcpa_s`, both positions and velocities and both thresholds;
  - an issued advisory adds one `action` row (`advisory_one_basin`, rung `slow`), and a blocked one adds none;
  - `AuditLog.verify().ok` is true.
- [ ] `pytest tests/test_cpa.py::test_course_conversion` — proves heading 0 gives (0, s), 90 gives (s, 0) and 225 gives (−s/√2, −s/√2) to 1e-9, and that `cpa_tcpa` is symmetric in CPA under swapping own and other.
- [ ] `pytest tests/test_cpa.py::test_properties` — hypothesis over finite inputs. Proves CPA ≤ current range whenever TCPA > 0, CPA ≥ 0, and that the result does not change when both positions are translated by the same offset (to 1e-6).
- [ ] `pytest tests/test_separation.py::test_encounter_once` — a scripted head-on pair stays inside the thresholds for 20 ticks. Proves exactly one decision and one `guard_decision` row; a second encounter after the pair opens and closes again is decided once more.
- [ ] `pytest tests/test_separation.py::test_in_water_jobs` — a hand-built `RunResult` with a cleared contact, a contact cleared later, an expired plan window and a stale track. Proves only the in-window, cleared, freshly tracked job is returned, sorted by `plan_id`.
- [ ] `pytest tests/test_separation_guard.py::test_slow_permitted_unchanged` — the WO-041 suites `tests/test_guards.py` and `tests/test_guards_integration.py` pass unchanged after the refactor (run inside this test via `pytest.main`, or as listed CI targets). Also proves `separation_slow_permitted` under each `degraded_mode` reason can only remove the permit.
- [ ] integration — `MODEL_MODE=replay uv run pytest tests/test_separation_bt01.py`. Runs BT-01 at seed 7 through `ScenarioRunner(...).run()` and `advise_run` with signed orders. Proves:
  - BT-01 yields at least one `SeparationAdvisory` (dredge jobs share `main_channel` with the transits), so the integration is not vacuous. If seed 7 has none, the test adds one in-memory vessel transit over a dredge job's track (`model_copy`; the committed BT-01 is unchanged) and asserts on that;
  - every advisory names a vessel from the 3 BT-01 transits and a plan that is in the water at that tick;
  - each issued advisory's basin is in the orders' slow basins;
  - every advisory recomputed from its `inputs` with `cpa_tcpa` matches to 1e-6;
  - with `--unsigned`-equivalent temp orders the same advisories are all `blocked` and no `action` rows exist;
  - the WO-080 terminal states are unchanged (39 CLEARED_AT_ASK; rogue HANDED_OFF, gated on runner v2 exactly as WO-080's `-k rogue_handed_off`).
  - `vessel_fixes` reads `ais_tracks` only: an AST scan of `separation.py` finds no `truth_paths` or `contact_truth`.
- [ ] determinism (§2.4) — `uv run pytest tests/test_separation_bt01.py -k deterministic`. BT-01 at seed 7 is advised twice in-process and once each in subprocesses under `PYTHONHASHSEED=0` and `=1`. All runs give identical canonical JSON of the `SeparationReport`, the same guard `inputs_hash`es and the same audit chain head. The test uses no wall clock: time is `tick * tick_s`.
- [ ] rule proof §2.2 — `pytest tests/test_separation_guard.py::test_no_model_imports`. An AST scan of `soundings/advisories/*.py` and `guards.py` finds no `soundings.llm` or `anthropic` import. `SeparationFacts` has no free-text field.
- [ ] rule proof §2.1/§2.5 — `grep -iEf config/denylist.txt soundings/advisories config/advisories.yaml tests/test_cpa.py tests/test_separation*.py docs/work-orders/WO-083-separation-advisories.md` → empty. Also `pytest tests/test_separation_guard.py::test_vocabulary`: advisory text uses only `advisory`/`slow`/`proposes`, and no action other than `advisory_one_basin` is ever produced. `make check` is green.

## Interfaces
Exposed:
- WO-083 `soundings.advisories.cpa.CpaResult`, `.cpa_tcpa`, `.velocity_from_course`
- WO-083 `soundings.advisories.separation.AdvisoryConfig`, `.load_advisory_config`, `.VesselFix`, `.vessel_fixes`, `.JobFix`, `.SeparationAdvisory`, `.AdvisoryDecision`, `.SeparationReport`, `.in_water_jobs`, `.separation_check`, `.SeparationAdvisor`, `.advise_run`
- WO-083 `soundings.ladder.guards.SeparationFacts`, `.separation_slow_permitted`, `.BoundaryGuards.separation_decision`
- WO-083 `config/advisories.yaml` keys `separation_m`, `advisory_window_s`

Consumed (read on main; adapt to merged names):
- WO-041: `soundings.ladder.guards.{GuardInputs, BoundaryGuards, slow_permitted, scope_exceeded, inputs_hash, UNSIGNED}`, `soundings.ladder.boundary.{LadderCriteria, SlowCriteria, load_boundary}`, `soundings.ladder.standing_orders.{load_verified, effective_scope, scenario_now, StandingScope}`.
- WO-040: `soundings.ladder.types.{GuardName.SLOW_PERMITTED, GuardDecision, GuardVerdict, ActionScope}`.
- WO-042 part A (branch `wo/042a-audit-sitrep-template`): `soundings.ladder.audit.{AuditLog.append(kind, payload, *, run_id, t_s, contact_id), AuditKind.GUARD_DECISION, AuditKind.ACTION, guard_decision_payload, ActionPayload, AuditLog.verify}`.
- WO-022: `soundings.runner.{ScenarioRunner, RunResult, CaseResult}`, `soundings.matcher.in_window`, `soundings.registry.store.plan_views_from_scenario`.
- WO-012: `soundings.investigation.features.{track_arrays, last_fitted_state}`. WO-010: `soundings.schemas.geo.to_local_m`, `soundings.schemas.harbor.Harbor`, `soundings.schemas.enums.Rung`.
- WO-080 part A (not on main): `soundings.sim.types.SimRun.ais_tracks: dict[str, tuple[AisReport, ...]]`, `AisReport(ais_id, tick, t_s, lat, lon, sog_mps, cog_deg)`, `soundings.sim.ais.load_ais_config` (`report_interval_s`), scenario id `BT-01`.

## Cut line
If late, drop in this order: (1) the hypothesis property test is replaced by 20 fixed random cases; (2) the multi-tick encounter memory gives way to one decision per `(vessel_id, plan_id)` per run; (3) `advise_run` over the whole run, with the BT-01 integration stepping only the 3 transit windows. Never an exit test.

## Model calls
None. Paid run needed? No.
Dependency adds (logged in DECISIONS.md per owner directive): none. `math` is stdlib, and the geometry stays scalar.
DECISIONS.md rows this WO adds:
- the new `config/advisories.yaml`;
- `separation_slow_permitted` uses the slow rung's standing-orders checks, with CPA/TCPA in place of threat_mass and time criteria;
- one decision per encounter.

## Rungs touched
slow (standing orders, `advisory_one_basin`); ask is the ceiling when orders are unsigned. watch is unchanged.

## Hard rules exercised
- §2.1: no state after an advisory and no action besides `advisory_one_basin` (`test_vocabulary`).
- §2.2: `test_no_model_imports`, and the guard alone decides.
- §2.4: the determinism item above.
- §2.5: the denylist grep.
- §2.6: `test_audited_with_inputs`, where every advisory carries the geometry it rests on.
- §3: thresholds come from config only (`test_thresholds`).

## Done
PR: (see STATE row) · Merged commit: (squash, see STATE row) · Test summary: `make check` green locally; WO-083 suites 25 passed (test_cpa 8, test_separation 5, test_separation_guard 6, test_separation_bt01 6 with BT-01 present; the 3 BT-01 cases skip with a reason until WO-080 part A is on main) · STATE.md updated: yes

Implemented as specified, with these differences (DECISIONS 2026-10-17 07:01):
- `soundings/advisories/{__init__,cpa,separation}.py`, `config/advisories.yaml`; guards.py gains `_slow_scope_problem` (marked WO-083 block; `_slow_problem` calls it, `slow_permitted` unchanged: test_guards + test_guards_integration pass unmodified) and a `# --- WO-083 begin: separation ---` block with `SeparationFacts`, `separation_inputs_hash`, `separation_slow_permitted`, and `BoundaryGuards.separation_decision` (in-class block).
- `SeparationFacts` also carries `vessel_id` and `plan_id` (patterned ids, no free text). Own = vessel, other = job.
- Block reasons: `standing_orders_unsigned` (unsigned or expired), `slow_not_in_standing_scope`, `unknown_input:basin`, `basin_out_of_standing_scope`, `cpa_not_below_separation_m`, `tcpa_outside_advisory_window`, `scope_exceeded:*`; permit reason `separation_criteria_met`.
- `inputs_hash = stable_hash({"guard_inputs": ..., "separation": facts})`: the same mapping the audit row stores as `inputs`, so each row re-hashes to its `inputs_hash`.
- `AdvisoryDecision.label`: "slow advisory proposed for basin <basin>" or "advisory blocked: out of standing-orders scope" (basin/scope) / "advisory blocked: standing orders unsigned or expired"; blocked advisories never count as interruptions and `advise_run` is read-only over the `RunResult`.
- `in_water_jobs` dead-reckons the fitted position from the last reading to `tick * tick_s`; two cleared cases of one lineage on one plan yield one job (latest reading). `separation_check` raises if a vessel and a job fix carry different ticks.
- `vessel_fixes(sim, tick, *, interval_s=None)` reads `sim.ais_tracks` via `getattr` (empty when absent) and `report_interval_s()` imports `soundings.sim.ais` when present (else WO-080's 10 s default), so the module works before and after WO-080 merges. Stale: older than 2 x report interval.
- BT-01 at seed 7 (checked against WO-080's branch): 101 encounter decisions, issued ones all in inner_harbor_west, main_channel ones blocked `basin_out_of_standing_scope`; no in-memory vessel needed. An S01 + one in-memory vessel case exercises the pipeline without BT-01. The subprocess determinism runs re-advise the parent's pickled BT-01 run (the runner's own determinism is WO-080's test).
- Cut line: nothing dropped. No dependency added.

## Notes for downstream
- WO-084: `JobFix.velocity_source` is `"window"` here. When WO-084's flag is on, `in_water_jobs` may take the Kalman state for the job contact. The CPA inputs then record `velocity_source="kalman"`, and the hashes change accordingly.
- WO-086: separation advisories add no ladder transition, so by default they are not in the ledger's "advisories" bucket (which must match eval's max-rung count). If the owner wants them shown, `SeparationReport.decisions` gives `issued` and `blocked` counts for a separate line. Every UI string must say "advisory" or "slow advisory", never a verb that implies control of a vessel.
- WO-085: an advisory changes no contact's queue priority.
