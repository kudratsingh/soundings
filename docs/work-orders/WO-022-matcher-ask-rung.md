# WO-022 — matcher-ask-rung

Phase: 2 · Branch: `wo/022-matcher-ask-rung` · Worktree: `../soundings-worktrees/wo-022-matcher-ask-rung` · Est: 7.5 h · Owner: planner / ladder / investigation — Kudrat
Depends on: WO-010 (schemas, geo, `LadderState`), WO-011 (`run_scenario`, `SimRun`, `CurrentField`, `TickClock`), WO-012 (`EvidenceTable`, extractors, `carry_over_reacquired`), WO-020 (`RegistryStore`, `PlanView`), WO-021 (`HailSimulator`, `hail_evidence_rows`, `KnownOperatorTable`, `soundings.probe_keys`). Branch from `main` once WO-020 and WO-021 have merged. Until then, write `matcher.py` and its tests against the interface names below; that part needs only WO-010.

## Goal
The ask rung works end to end. `soundings/matcher.py` holds a pure, deterministic `match(...)` over an evidence view, the registry plans and the current tick. It returns a `MatchOutcome` with a reason code, a one-line reason, and the evidence row ids it used. Next to it sit the drift search box for `LOST_VEHICLE` and re-acquisition matching (SPEC-INVESTIGATION 9.9). `soundings/runner.py` v1 (`ScenarioRunner`) runs sim → evidence table → watch probes → `registry_lookup` + `acoustic_hail` → matcher. Every case ends at `CLEARED_AT_ASK`, at `LOST_VEHICLE` with a `LostVehicleNotice` (last known position plus drift search box, sent to the plan's operator), or at `INVESTIGATING`, handed to Phase 3. The runner writes a typed transition log. S01 and S03 clear at ask with zero human interruptions; S02 ends `LOST_VEHICLE` with a search box that contains the vehicle's true position.

## Scope
In:
- **`soundings/matcher.py` (pure: no I/O, no clock, no globals; inputs are frozen models)**
  - `MatchCode` (StrEnum): `CLEARED_AT_ASK, MISMATCH_AREA, MISMATCH_WINDOW, LOST_VEHICLE, ESCALATE, UNFILED_KNOWN_OPERATOR, PENDING_SILENCE`. `PENDING_SILENCE` is a non-terminal code, added for the open question on S02.
  - `MatchOutcome(code, reason: str, evidence_row_ids: tuple[str, ...] (WO-010 16-hex `row_id`s; non-empty, sorted), plan_id | None, silence_s | None)`.
  - `EvidenceView`: a frozen snapshot built by `evidence_view(table, contact_id, track, tick, tick_s)`. It contains:
    - `position: PositionFix(lat, lon, tick, sigma_m, row_id)`. `row_id` is the `track_segment` fact row covering the last point.
    - The latest hail as `hail_reply`, `modem_id` and `operator_known`, each with its row id.
    - `lineage_plan_id`, with a row id, taken from a carried-over or earlier `plan_id` row.
    - `lineage_replied: bool`.
    - `last_heard_tick`, with a row id: the latest valid hail reply or `propeller_tonal_present=true` row in the lineage.
  - `registry_lookup(plans, harbor, lat, lon, tick, cfg, modem_id=None) -> RegistryLookup(match: RegistryMatch, plan_id | None, distance_m)`.
    - Position only (watch rung, before the hail): returns `exact`, `window_mismatch` or `none`, never `area_mismatch`, because an area mismatch needs identity.
    - With a modem: compares that modem's plan, so all four buckets are possible.
    - `registry_lookup_rows(...)` writes `registry_match` (derived from the `track_segment` row and any hail rows) and `plan_id` (fact, `source="registry:<plan_id>"`).
  - `distance_to_zone_m(harbor, zone_id, lat, lon)`: 0 inside the polygon, else the distance to the nearest edge, computed in WO-010 local metres.
  - `match(view, plans, tick, *, harbor, cfg) -> MatchOutcome` checks its rules in this order. The first rule that holds wins:
    1. `hail_reply=valid` and the modem has a plan P:
       - outside P's window → `MISMATCH_WINDOW`;
       - else `distance_to_zone_m > cfg.area_tolerance_m` → `MISMATCH_AREA`;
       - else `CLEARED_AT_ASK`.
    2. `hail_reply=valid`, no plan for the modem, and `operator_known` → `UNFILED_KNOWN_OPERATOR`.
    3. `hail_reply=valid` with an unknown modem, or `hail_reply=invalid` → `ESCALATE`.
    4. `hail_reply ∈ {none, timeout}` and the lineage is filed (`lineage_plan_id` is set because an earlier valid reply or a carry-over named a plan):
       - silence `(tick − last_heard_tick)·tick_s > lost_vehicle_silence_min·60` (read from `config/ladder.yaml` `stopping`, not copied) → `LOST_VEHICLE`;
       - otherwise → `PENDING_SILENCE`.
    5. Anything else → `ESCALATE`.
  - Every outcome cites the rows it read, and the reason string names the numbers it used (for example `"silence 722 s > 600 s since row 3f9c0a1b2d4e5f60"`).
  - `SearchBox(center_lat, center_lon, radius_m, min_lat, max_lat, min_lon, max_lon, anchor: PositionFix, to_tick, assumption_row_id)` with a `contains(lat, lon)` method.
  - `drift_search_box(anchor, to_tick, tick_s, current_field, tide_phase, cfg) -> SearchBox`.
    - Advects the anchor in `cfg.search_box.step_s` steps using `CurrentField.vector_at`.
    - Radius = `base_radius_m + anchor.sigma_m + growth_mps · elapsed_s`.
    - Cites the `current_field` assumption row.
  - `LostTrack(contact_id, last_fix, velocity_mps (east, north), last_tick)`, `ReacquireMatch(old_id, new_id, distance_m, gate_m, match_confidence, reason)`, and `match_reacquisition(first_fix, speed_mps, lost, tick, tick_s, current_field, tide_phase, cfg) -> ReacquireMatch | None`.
    - Predicts each lost track forward by drift and by dead reckoning, and keeps the closer of the two predictions.
    - Gate = `gate_base_m + gate_growth_mps · gap_s`, and requires `|speed_new − speed_old| ≤ speed_tol_mps`.
    - Candidates are ordered by `distance/gate`, ties broken by the lowest `contact_id`. `match_confidence = 1 − distance/gate`.
- **`config/matcher.yaml`** (new; owned by this WO; no change to `config/ladder.yaml`):
  - `area_tolerance_m: 150`, `evidence_every_ticks: 15`, `watch_min_points: 10`, `ask.max_hails_per_case: 2`.
  - `plan_watch.rehail_interval_s: 300`.
  - `reacquire: {max_gap_s: 600, gate_base_m: 75, gate_growth_mps: 0.15, speed_tol_mps: 0.5}`.
  - `search_box: {base_radius_m: 50, growth_mps: 0.10, step_s: 30}`.
  - Header comment: stress-test settings for the demonstration.
- **`soundings/runner.py` v1**
  - Entry point: `ScenarioRunner(scenario, *, seed=None, plans=None, harbor=None, current_field=None, match_cfg=None, hail_cfg=None, known=None).run() -> RunResult`. This is the canonical shape; later WOs add keyword arguments only: WO-033 `run(*, llm_roles, violations_path, fixture_store)`, WO-043 `run(*, orders_path, unsigned, approver, db_url)` plus `start()`, WO-051 the constructor kwarg `sim_likelihoods`.
  - `soundings/runner.py` is a shared file. This WO creates it; WO-033, WO-043 and WO-051 each edit only a block marked `# --- v2 (WO-033) ---`, `# --- v3 (WO-043) ---` or `# --- sim_likelihoods (WO-051) ---`.
  - `plans` is a `RegistryStore` (read through `overlapping_ticks`) or a list of `PlanView`. The default is `plans_from_scenario` loaded into an in-memory list. A tick loop on WO-011's `TickClock` does the following.
  - **New observed id.** Try `match_reacquisition` against contacts dropped for more than the sim's `track_timeout_ticks`. On a match, call `carry_over_reacquired(table, old, new, t=..., match_confidence=..., reason=...)`. Open a case `"<contact_id>#<n>"` and record DETECTED → WATCHING (`contact_detected`).
  - **WATCHING.** Every `evidence_every_ticks`, append WO-012 extractor rows (`extract_kinematics`, `extract_acoustic`, `extract_context`) over the track so far. Run `registry_lookup` (watch rung). Once there are ≥ `watch_min_points` points, record WATCHING → ASKING (`probe_result`).
  - **ASKING.**
    - Send `acoustic_hail` through `HailSimulator.send` (`previously_replied = view.lineage_replied`) and append `hail_evidence_rows` at the reply tick. Re-run `registry_lookup` with the modem, then call `match`.
    - `CLEARED_AT_ASK` → terminal. The case joins the plan watch.
    - `LOST_VEHICLE` → terminal, plus a `LostVehicleNotice`.
    - `PENDING_SILENCE` → one more hail, scheduled for the first tick at which the silence would exceed the threshold. There are at most `max_hails_per_case` hails, and when the budget is spent the outcome is `ESCALATE`.
    - Every other code → ASKING → INVESTIGATING (`probe_result`, not cleared), with `investigation_pending=True`. Runner v1 stops on that case, and runner v2 (WO-033) continues it.
  - **Plan watch.** This is the registry's ordinary-day job (CLAUDE.md §1: finding the research vehicle that went silent).
    - While a cleared contact's plan window is open, re-hail it every `rehail_interval_s`. These hails are costed and counted in `probes_used`.
    - When a re-hail gets no reply and the silence since `last_heard_tick` exceeds the threshold, open a new case for that contact. It runs DETECTED → WATCHING → ASKING and reuses the plan-watch hail through its idempotency key. The cleared case is never reopened: terminal states stay terminal.
  - **Transitions.** Each one is checked against `ASK_TRANSITIONS`, a frozenset of the five SPEC-LADDER §3 rows from DETECTED through ASKING. Anything else raises and is written as a FAILED case.
  - `Transition(seq, case_id, contact_id, tick, from_state, event: RunnerEvent, to_state, reason_code, evidence_row_ids, idempotency_key)`. `RunnerEvent` (StrEnum) takes the SPEC-LADDER §2 names `contact_detected`, `probe_result` and `stop_rule_fired`.
  - `RunResult(scenario_id, seed, sim: SimRun, cases: list[CaseResult], transitions, evidence: EvidenceTable, notices: list[LostVehicleNotice], interruptions: int, probes_used: dict[str, int], transition_log_hash)`.
  - `CaseResult(case_id, contact_id, state, outcome, reacquired_from, investigation_pending)`.
  - The runner never reads `contact_truth` or `truth_paths`; tests use them only for grading.
  - `LostVehicleNotice(case_id, contact_id, plan_id, operator, vehicle, contact_channel, last_known: PositionFix, silence_s, search_box, evidence_row_ids, lines: tuple[NoticeLine(section, text, evidence_row_ids), ...])`.
    - Each line falls under a SPEC-LADDER §6 section name: `Contact`, `What we know`, `What we assume`, `Search box`. WO-042 renders the SITREP from these lines.
    - Recipient: the plan's `contact_channel`. The watch officer is not interrupted.
  - `write_transition_log(result, path)` writes canonical JSONL.
  - CLI: `python -m soundings.runner --scenario S01 --seed 1 [--plans scenario|db] [--out artifacts/runs/S01-1]` prints one line per transition, then `transition_log_hash=<hex>`. `--plans db` reads `RegistryStore()` (`SOUNDINGS_DB`).

Out: posterior, probe planner, Phase 3 probes (`camera_cue`, `ask_operator_by_phone`, ...) and anything after INVESTIGATING (WO-030..033). Ladder machine, checkpoints, guards and the audit log (WO-040..042). The SITREP renderer (WO-042). Runs and WebSocket over the API (WO-060). No rung above ask is reachable from this code.

## Spec references
`docs/SPEC-INVESTIGATION.md §2` (registry/identity columns), `§5` (`registry_lookup`, `acoustic_hail`; `ask_operator_by_phone` deferred), `§7` (CLEARED_AT_ASK and LOST_VEHICLE rules), `§9.9`; `docs/SPEC-LADDER.md §1`, `§2`, `§3` (DETECTED..ASKING rows), `§6` (Search box section); `docs/SPEC-SIM.md §2` (track timeout, current field), `§3`, `§4`; `config/ladder.yaml` (`stopping.lost_vehicle_silence_min: 10`); `config/likelihoods.yaml` (`registry_match`, `hail_reply` buckets).

## Files expected
- `soundings/matcher.py`, `soundings/runner.py`, `config/matcher.yaml`
- `tests/test_matcher.py`, `tests/test_search_box.py`, `tests/test_reacquire.py` (unit); `tests/test_ask_rung.py`, `tests/test_runner_v1.py` (integration); `tests/test_ask_rules.py` (rule proofs)

## Exit tests (must all pass with `MODEL_MODE=replay`, no network)
From BUILD-PLAN Phase 2 (verbatim):
- [ ] `pytest tests/test_matcher.py` — table-driven: (filed, in-window, in-area, replies) → `CLEARED_AT_ASK`; (filed, in-window, out-of-area by > tolerance) → `MISMATCH_AREA`; (filed, gone silent > N min) → `LOST_VEHICLE`; (unfiled, no reply) → `ESCALATE`; (unfiled, replies with a known modem id not in any plan) → `UNFILED_KNOWN_OPERATOR`. Every outcome carries a reason code and the evidence rows it used.
- [ ] `pytest tests/test_ask_rung.py` — scenarios S01 (filed on plan) and S03 (permitted divers) resolve at `ask` with zero human interruptions; S02 (filed, gone silent) produces a `LOST_VEHICLE` SITREP with a last-known position and a search box.
- [ ] Manual: file a plan in the web form, start the sim with S01, watch it clear. Screenshot in PR.

Unit extensions:
- [ ] `uv run pytest tests/test_matcher.py -k extended` — `MISMATCH_WINDOW`; `invalid` reply → `ESCALATE`; a valid reply with an unknown modem → `ESCALATE`. Boundaries: area exactly at `area_tolerance_m` clears and 1 m beyond does not; silence exactly 600 s → `PENDING_SILENCE` and 602 s → `LOST_VEHICLE`. Every cited row id exists in the view, and `reason` is non-empty. Proves the decision table at its edges.
- [ ] `uv run pytest tests/test_matcher.py -k pure` — Hypothesis (`derandomize=True`) over generated views and plans: `match` returns equal outcomes on repeated calls and leaves its inputs unchanged. With `open`, `socket.socket` and `time.time` patched to raise, every call still succeeds. Proves purity.
- [ ] `uv run pytest tests/test_matcher.py -k registry_lookup` — a position-only lookup never returns `area_mismatch`; a lookup with a modem returns all four buckets on crafted cases. Bucket names equal `config/likelihoods.yaml` `registry_match`. Proves WO-030 can score these rows unchanged.
- [ ] `uv run pytest tests/test_search_box.py` — zero current: the centre stays put and the radius is `base + sigma + growth·t`. A uniform 0.2 m/s east current for 600 s moves the centre about 120 m east (±1 m). `contains` is true at the centre and false at radius + 1 m. Proves the advection and growth formula.
- [ ] `uv run pytest tests/test_reacquire.py` — synthetic lost tracks: a drifting track re-appears inside the gate and matches; an inconsistent speed is rejected; past `max_gap_s` there is no match; two candidates resolve by `distance/gate`, then id. Proves 9.9 matching logic.

Integration:
- [ ] `uv run pytest tests/test_ask_rung.py -k s02_box` — at the `LOST_VEHICLE` tick, `notice.search_box.contains(*truth_paths["gt-1"][tick])` is true for S02 at seed 3 and at seeds 1 and 2. The notice has ≥ 1 line in each of `Contact`, `What we know`, `What we assume` and `Search box`, and `interruptions == 0`. Proves the scenario's `search_box_contains_truth` and `max_human_interruptions: 0`.
- [ ] `uv run pytest tests/test_runner_v1.py -k catalog` — S01..S08 at their own seeds run without error. Every case ends in {CLEARED_AT_ASK, LOST_VEHICLE, INVESTIGATING}, and every transition is in `ASK_TRANSITIONS`. No case has more than 2 hails. S05's ground-truth lineage is never `CLEARED_AT_ASK` (it reaches INVESTIGATING). S04's case carries outcome `UNFILED_KNOWN_OPERATOR`. Proves the runner over the whole catalog, and that nothing hostile is cleared at ask.
- [ ] `uv run pytest tests/test_runner_v1.py -k reacquire_sweep` — over S01..S08 × seeds 1,2,3, every carry-over link the runner makes joins two observed ids that share a `gt_id` (precision 1.0), and at least one link occurs in the sweep. Proves re-acquisition on real track gaps.
- [ ] `uv run pytest tests/test_runner_v1.py -k hail_rows_append` — for S02 and S04, every `hail_evidence_rows` output appends to the run's real `EvidenceTable` without `EvidenceError`, and each hail's key equals `soundings.probe_keys.idempotency_key(contact_id, "acoustic_hail", {}, tick_bucket(tick, tick_s))`. Proves WO-021's rows fit WO-012's table (moved here so WO-021 need not wait for WO-012).
- [ ] `uv run pytest tests/test_runner_v1.py -k registry_catalog` — S01, S02 and S03 each seed into a tmp `RegistryStore` with every plan accepted and found by `by_modem`. Proves the full plan-bearing catalog agrees with WO-020's rules (moved here so WO-020 need not wait for WO-011).
- [ ] `uv run pytest tests/test_runner_v1.py -k registry_db` — S01 with plans seeded into a tmp `RegistryStore` gives the same `transition_log_hash` as S01 with the scenario's plan list. Proves the runner reads the WO-020 store on the matcher's hot path.

Determinism:
- [ ] `for s in 0 1; do PYTHONHASHSEED=$s uv run python -m soundings.runner --scenario S02 --seed 3 > /tmp/r$s.txt; done; diff /tmp/r0.txt /tmp/r1.txt` prints nothing, and `pytest tests/test_runner_v1.py -k deterministic` asserts equal `transition_log_hash` and `evidence.table_hash()` across two in-process runs for S01, S02 and S05. Proves CLAUDE.md §2.4 for the matcher and runner.

Guard / rule proof (`tests/test_ask_rules.py`):
- [ ] §2.2: an AST scan shows `soundings/matcher.py` and `soundings/runner.py` import nothing from `soundings.llm`, and `matcher.py` imports no `os`, `sqlite3`, `sqlmodel`, `time` or `datetime.now`. Every state change goes through the `ASK_TRANSITIONS` check, and a forged transition raises. Proves no model output and no hidden state become a control.
- [ ] §2.6: every `MatchOutcome` and every `NoticeLine` in a catalog sweep cites ≥ 1 row id present in `result.evidence`. The search-box line cites the `current_field` assumption row and sits under `What we assume`. Proves evidence first, with facts and assumptions kept separate.
- [ ] §2.1 / §2.5: `MatchCode`, `RunnerEvent`, every notice text and `config/matcher.yaml` contain no denylist match, and `grep -riEf config/denylist.txt soundings/matcher.py soundings/runner.py config/matcher.yaml tests/test_matcher.py tests/test_ask_rung.py tests/test_runner_v1.py tests/test_ask_rules.py tests/test_search_box.py tests/test_reacquire.py` prints nothing. No state exists past ask in this code.
- [ ] `make check` green.

Manual (the BUILD-PLAN item, done as a Playwright screenshot):
- [ ] Steps:
  1. Set `SOUNDINGS_DB=/tmp/s01.db`, run `make dev`, then `uv run python scripts/registry_form_shot.py --base http://localhost:8000 --plan-from S01`. This files S01's plan through the form and saves `artifacts/registry-form.png`.
  2. Run `uv run python -m soundings.runner --scenario S01 --seed 1 --plans db | tee artifacts/s01-run.txt`. The output ends with the ground-truth case at `CLEARED_AT_ASK`.
  Attach the PNG and the text to the PR, marked "Playwright screenshot".

## Interfaces
Exposed by WO-022:
- `soundings.matcher.MatchCode`, `.MatchOutcome`, `.MatchConfig`, `.load_match_config`, `.PositionFix`, `.EvidenceView`, `.evidence_view`, `.match`, `.RegistryLookup`, `.registry_lookup`, `.registry_lookup_rows`, `.distance_to_zone_m`, `.SearchBox`, `.drift_search_box`, `.LostTrack`, `.ReacquireMatch`, `.match_reacquisition`
- `soundings.runner.ScenarioRunner`, `.RunResult`, `.CaseResult`, `.Transition`, `.RunnerEvent`, `.ASK_TRANSITIONS`, `.LostVehicleNotice`, `.NoticeLine`, `.write_transition_log`; CLI `python -m soundings.runner`
- Files: `config/matcher.yaml`
- For Phase 3/4: WO-031 wraps `registry_lookup_rows` and `HailSimulator.send` as allowlisted probes. WO-033 resumes cases where `investigation_pending=True`. WO-043's driver runs these cases through WO-040's full table (`ASK_TRANSITIONS` stays as the v1 check). WO-042 renders `LostVehicleNotice.lines`.

Consumed:
- WO-010: `soundings.schemas.Harbor`, `Zone`, `EvidenceRow` (`row_id`: 16-hex str; `t`: tick), `EvidenceKind`, `LadderState`, `RegistryMatch`, `HailReply`, `ProbeId`, `Scenario`; `soundings.schemas.geo.to_local_m`, `from_local_m`, `distance_m`, `point_in_polygon`; `soundings.schemas.config.load_ladder_config`; `soundings.canonical.canonical_json`, `stable_hash`, `round6`.
- WO-011: `soundings.sim.run.run_scenario`, `soundings.sim.types.SimRun` (`tracks`, `detections`, `tick_s`, `n_ticks`; `contact_truth` and `truth_paths` in tests only), `soundings.sim.clock.TickClock`, `soundings.sim.current.load_current_field`, `CurrentField.vector_at`, `tide_phase_for_hour`, `soundings.sim.detection.load_detection_params` (`track_timeout_ticks`), `soundings.sim.scenario.resolve_scenario`, `catalog_paths`.
- WO-012: `soundings.investigation.evidence.EvidenceTable` (`append`, `rows`, `for_contact`, `latest`, `table_hash`), `EvidenceError`, `carry_over_reacquired`; `soundings.investigation.features.extract_kinematics`, `extract_acoustic`, `extract_context`.
- WO-020: `soundings.registry.store.RegistryStore` (`overlapping_ticks`, `seed_from_scenario`, `by_modem`), `plans_from_scenario`; `soundings.registry.models.PlanView`; `scripts/registry_form_shot.py --plan-from`.
- WO-021: `soundings.challenge.hail.HailSimulator`, `HailConfig`, `load_hail_config`, `load_known_operators`, `hail_evidence_rows`, `HailResult`; `soundings.probe_keys.idempotency_key`, `tick_bucket`.

## Cut line
If more than an hour late, drop these in order (no exit test is dropped):
1. `--out` JSONL writer; the CLI stdout and the in-memory hash stay.
2. Dead-reckoning prediction in `match_reacquisition` (drift only).
3. Evidence cadence below once per 30 ticks.
4. The `PENDING_SILENCE` re-hail for re-acquired contacts. Plan watch alone carries S02.

## Model calls
None. Paid run needed? No. Runner v1 makes no call to `soundings.llm` at all; the Phase 3 roles plug in through WO-032.
Dependency adds needing owner approval (CLAUDE.md section 3): none.

## Rungs touched
watch (free probes, `registry_lookup`) and ask (`acoustic_hail`, the matcher, plan watch). It hands off to Phase 3 at INVESTIGATING. No slow, hold or hand to a person.

## Hard rules exercised
- §2.1 and §2.5: the denylist test and grep; only the five DETECTED..ASKING rows are reachable.
- §2.2: the AST scan and the forged-transition test.
- §2.4: the PYTHONHASHSEED diff and `-k deterministic`.
- §2.6: the citation test (every outcome and notice line cites existing rows; the assumption is labelled).

## Done
PR: [#14](https://github.com/kudratsingh/soundings/pull/14) (branch `wo/022-matcher-ask-rung`) · Merged commit: squash commit on main (see PR) · Test summary: `make check` exit 0, full suite 1080 passed after rebase; WO-022 tests 91 (matcher 31, search box 6, re-acquire 9, ask rung 10, runner v1 27, rules 18) · STATE.md updated: yes (WO-022 row)

- Every exit test passes with `MODEL_MODE=replay`; the denylist grep prints nothing. Determinism: `PYTHONHASHSEED` 0 vs 1 on `--scenario S02 --seed 3` gives an empty diff (sha256 `27555550…d477a`, `transition_log_hash=b4793710…ba67b`); `-k deterministic` checks S01, S02, S05 in process.
- Manual: S01's plan filed through `/registry` with `scripts/registry_form_shot.py --plan-from S01` (cached headless shell via `--executable-path`), then `python -m soundings.runner --scenario S01 --seed 1 --plans db`: both ground-truth cases end `CLEARED_AT_ASK`, and the hash equals the scenario-plans run (`f799c460…0653`).
- Interface as implemented (differences from the text above):
  - `match_reacquisition(..., *, new_id)`; `speed_mps` may be None (speed test skipped). The runner tries it at the new id's first reading, where speed is not yet measured, so on catalog runs the gate is position-only.
  - `drift_search_box(..., *, assumption_row_id)` (the `current_field` row it cites).
  - `registry_lookup_rows(lookup, *, contact_id, track_segment, hail_rows=())`; `RegistryLookup` also carries `modem_id` and `tick`. `plan_id` rows use source `registry:<plan_id lower-case>` when a modem named the plan and `registry.position:<...>` for a position-only lookup (sources are lower case by WO-010's pattern); `lineage_plan_id` reads only the former.
  - `MatchConfig.lost_vehicle_silence_min` is filled from `config/ladder.yaml` by `load_match_config` (refuses the key in matcher.yaml). `EvidenceView` also holds `current_field_row_id`, `reacquired_from` and their row ids; `PositionFix.row_id` may be None.
  - `CaseResult` adds `opened_tick`, `closed_tick`, `outcomes` (every matcher outcome), `hail_keys`, `opened_by` (`new_track` | `plan_watch`), `failure`. `RunResult` adds `hails` (`HailRecord`) and `reacquisitions`. `LostVehicleNotice` adds `tick`. `probes_used` = {`acoustic_hail`: distinct hail keys sent, `registry_lookup`: distinct lookup keys}.
  - Extra exports: `check_transition`, `IllegalTransition`, `V1_END_STATES`, `transition_log_hash_of`, `format_transition`, `hail_count`, `HailRecord`.
- Choices beyond the text (DECISIONS.md 2026-10-16 23:44 WO-022 row): `reacquire.max_gap_s` is 100 s, not 600; evidence extraction continues for every tracked contact (so last-heard stays current), with acoustic rows written once per reading; WATCHING hands to ASKING also when the track drops (`track_dropped`) or the run ends (`run_ended`), and scheduled hails drain past the last tick; plans without ticks are never in window; each re-issued id of a lost lineage gets its own LOST_VEHICLE case and an updated notice.
- Cut line: nothing dropped.

## Coordinator addenda (2026-10-16 23:05 PT, binding; see docs/log/DECISIONS.md)
- Approved: the registry plan watch (re-hail cleared filed contacts every 300 s while the plan window is open; on silence past `lost_vehicle_silence_min` open a NEW case DETECTED → WATCHING → ASKING → LOST_VEHICLE; never reopen a terminal state) and the non-terminal matcher code `PENDING_SILENCE`.
- Interruption definition (one definition for catalog and eval): an interruption is any event that requires the watch officer or backup approver to look or act — approval request, backup page, handoff SITREP delivered to the watch officer, `request_watch_officer_look`. Operator notifications (LOST_VEHICLE to the plan's contact) and watch-after-handoff SITREP updates do not count.
