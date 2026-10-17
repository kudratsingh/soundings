# WO-011 — sim-and-scenario-catalog

Phase: 1 · Branch: `wo/011-sim-and-scenario-catalog` · Worktree: `../soundings-worktrees/wo-011-sim-and-scenario-catalog` · Est: 6.5 h · Owner: sim/data seat (unassigned); coordinator Kudrat

## Goal
The Phase 0 placeholder sim becomes the real `soundings/sim/`: a seeded, tick-clocked simulator that moves ground-truth contacts through the WO-010 harbor with seven behavior types plus a `silent_after_tick` modifier, passes them through the SPEC-SIM §2 detection model (range falloff, range-dependent noise, track gaps with re-issued contact ids, biologic false alarms), attaches per-reading confidence and acoustic features, and returns a `SimRun` whose observed tracks are kept apart from ground truth. All eight catalog scenarios exist and run end to end with byte-identical results per seed. `make sim` still works on a tiny dedicated scenario, and `make sim-plot scenario=S04` draws the track over the harbor.

## Scope
In: tick clock; behaviors `transit`, `lawnmower`, `orbit`, `drift`, `erratic`, `stationary`, `lawnmower_then_drift` (`fault_tick`), and the `silent_after_tick` modifier (acoustics go to none and hail replies stop after that tick; recorded in `ContactTruth`); detection model; per-reading confidence (SPEC-INVESTIGATION §9.10); acoustic sampling from `config/likelihoods.yaml`; `config/current_field.yaml`; `config/sim.yaml` (detection constants); a scenario resolver over WO-010's loader; every file under `scenarios/` (S01, S03, S04, S06, S07, S08 new, `smoke.yaml` new, and any edit S02 or S05 need to pass WO-010's schema); toggle plumbing; track hash; CLI and plot.

**Binding design decisions.**
- **Detection constants** live in `config/sim.yaml`, loaded into `soundings.sim.detection.DetectionParams` with SPEC defaults: `p_max: 0.95`, `r0_m: 400` (a node's own `range_m` overrides), `max_range_factor: 2.0`, `noise_sigma_base_m: 5.0`, `noise_sigma_per_m: 0.03`, `gap_rate_per_tick: 0.08`, `gap_mean_ticks: 5`, `track_timeout_ticks: 15`, `false_alarms_per_node_hour: 6`, `false_alarm_click_prob: 0.7`, `false_alarm_mean_ticks: 20`. `gap_mean_ticks`, `track_timeout_ticks` and `false_alarm_mean_ticks` are this WO's choice (SPEC gives none); they are config, not code.
- **Detection per tick per node**: `p = p_max · exp(−(range/r0)^2) · health`, zero beyond `2·r0`. Position noise per axis `σ = 5 + 0.03·range`. Fusion: the detecting node with the smallest range supplies the reading; ties broken by `node_id`.
- **Gaps**: each detected tick, a contact enters a gap with probability `gap_rate_per_tick`; gap length is geometric with mean `gap_mean_ticks`. Any run of consecutive undetected ticks (gap, miss, or out of range) longer than `track_timeout_ticks` closes the observed track; the next detection opens a new observed id (`c-0001`, `c-0002`, … in order of first detection, ties by ground-truth id; matches WO-010 `ContactId`). Gaps are written to WO-010 `Track.gaps`. `ContactTruth` maps every observed id to its `gt_id`, so later re-acquisition work can be graded.
- **Reading confidence** = `round(health · exp(−(range/r0)^2), 6)`, in [0, 1]; same falloff as detection, without `p_max`. Written to `TrackPoint.confidence`.
- **False alarms**: Poisson arrivals per node per tick at rate `6 · tick_s / 3600`; each spawns a short erratic shallow track (depth 0–3 m) inside the node's `r0` disk, emitted by that node for a geometric lifetime, `gt_id = fa-NNNN`, hypothesis `BIOLOGIC`, `breathing_or_click_present = true` with probability 0.7.
- **Acoustics**: per contact, a field set in the scenario's `acoustic` block (`propeller_tonal`, `tonal_band`, `biologic_signature`) is used as is; a field left `None` is sampled once from the contact's hypothesis row via `soundings.schemas.config.load_likelihood_spec()`. `cavitation_index` and `broadband_level` come from `config/sim.yaml` defaults keyed by `tonal_band`. Each `TrackPoint.acoustic` and `Detection` carries these values. **Shared-tables honesty note** (module docstring of `soundings/sim/acoustics.py`): the sim samples from the same tables the scorer reads, so demo results flatter the scorer; the Phase 5 eval adds runs where the sim draws from perturbed tables.
- **Current field**: `config/current_field.yaml` holds static vectors (u, v in m/s, magnitudes 0.05–0.3 so drift is measurable against position noise) on a coarse lat/lon grid for each tide phase `flood`, `ebb`, `slack`, plus an hour-to-phase table. The phase comes from `hour_of_day` via `tide_phase_for_hour`; there is no per-scenario override. `CurrentField.vector_at(lat, lon, tide_phase) -> tuple[float, float]` is a nearest-cell lookup, no interpolation.
- **Geometry**: positions integrate in local metres through WO-010 `soundings.schemas.geo.to_local_m` / `from_local_m`; there is no sim-local geo module.
- **Ground truth stays separate**: `SimRun.tracks` holds WO-010 `Track` objects (observed data only). Ground truth lives only in `contact_truth`, `truth_paths`, and `Detection.gt_id` / `is_false_alarm`, for tests and grading; the investigation never reads them (WO-012 grep).
- **Smoke**: `scenarios/smoke.yaml` is a full valid `Scenario` (id `smoke`, one `transit` and one `stationary` contact, `ground_truth: FILED_ON_PLAN`, `expected.terminal_state: CLEARED_AT_ASK`, never graded). `smoke.py` calls `run_scenario` with a one-node harbor subset (`hyd_1`) and false alarms set to 0. The catalog is the glob `scenarios/S[0-9][0-9]-*.yaml`, so `make sim` does not depend on the catalog.
- **Toggles**: all nine `ToggleName` members parse (WO-010 `Toggle`). Implemented now, using `Toggle`'s flat fields: `sensor_dropout{node_id, at_tick=0}` (health → 0 from that tick), `contact_surge{n, at_tick=0}` (n extra contacts, hypotheses drawn from `load_priors()` base, one fixed behavior per hypothesis), `contact_goes_quiet{contact_id, at_tick}` (applies `silent_after_tick`). `IMPLEMENTED_TOGGLES: frozenset[ToggleName]` holds those three; the other six are listed in `SimRun.toggle_report.parsed_not_implemented` and change nothing.
- **Determinism checklist** (WORKFLOW §8), each enforced by a test below:
  1. One `numpy.random.default_rng(seed)` per run, split with `SeedSequence.spawn` in a fixed order (ground-truth contacts by sorted id, then nodes by sorted id, then false alarms, then acoustics); every function that draws takes `rng` as an argument; no module-level RNG, no `np.random.<dist>` or `random.<fn>` global calls.
  2. No wall clock: time is `TickClock.tick · tick_s`; no `time.time`, `datetime.now`, `perf_counter` in `soundings/sim/`.
  3. Insertion-ordered dicts only; every set or dict keyed by ids is sorted before iteration where output depends on it.
  4. Positions integrate in local metres (float64); lat/lon/depth/speeds/confidences round to 6 dp only at the `SimRun` output boundary, before hashing and printing.
  5. `track_hash(run)` = `soundings.canonical.stable_hash` over observed tracks, detections, and the observed-to-truth map, sorted by observed id.

**Scenario catalog** (new files; `toggles: []` in all; `tick_s: 2`; S06 at `hour_of_day: 2`, others daytime). Every start lies inside a zone and within 800 m of a node, and every zone/asset id exists in WO-010's `config/harbor.geojson` (WO-010 `test_scenario_refs_resolve` checks both).

| id | seed | ticks | ground_truth | contacts and behavior | registry | `expected` |
|---|---|---|---|---|---|---|
| S01 | 1 | 600 | FILED_ON_PLAN | 1 AUV, `lawnmower` in `inner_basin_research`, 1.0 m/s, 8 m, tonal low | plan filed for that area and window, replies | `terminal_state: CLEARED_AT_ASK`, `max_human_interruptions: 0` |
| S03 | 5 | 600 | FILED_ON_PLAN | 2 divers, `stationary` with `jitter_mps: 0.2` by `fuel_berth_1`, 3 m, `propeller_tonal: false`, `biologic_signature: true` (breathing) | one diver plan covering both, replies | `terminal_state: CLEARED_AT_ASK`, `max_human_interruptions: 0`; the SPEC-SIM slow advisory if a vessel approaches goes in `notes` (no vessel traffic in Phase 1) |
| S04 | 7 | 600 | UNFILED_KNOWN_OPERATOR | 1 vehicle, `transit` to a `waypoint` in `main_channel` at 0.5 m/s, 6 m, tonal low | `filed: false`, `modem_id` listed in `known_operators`, replies | `terminal_state: RESOLVED_BENIGN`, `top_hypothesis: UNFILED_KNOWN_OPERATOR`, `max_human_interruptions: 0` |
| S06 | 13 | 600 | UNAUTHORIZED_UUV | 1 vehicle, `orbit` r=40 m around `fuel_berth_1` at 0.6 m/s, 8 m, tonal high | not filed, no reply | `terminal_state: HANDED_OFF`, `max_human_interruptions: 1` |
| S07 | 17 | 600 | BIOLOGIC | 1 animal, `erratic` with `drift_weight: 0.6`, 2 m, `biologic_signature: true`, `propeller_tonal: false` | not filed, no reply | `terminal_state: RESOLVED_BENIGN`, `top_hypothesis: BIOLOGIC`, `max_human_interruptions: 0` |
| S08 | 19 | 600 | DEBRIS_OR_DRIFT | 1 object, `drift` with `jitter_mps: 0.05`, 1.5 m, `propeller_tonal: false`, `biologic_signature: false` | not filed, no reply | `terminal_state: HAZARD_NOTICE`, `top_hypothesis: DEBRIS_OR_DRIFT`, `max_human_interruptions: 0` |

**Order of work.** Rebase on `main` after WO-000 merges; branch from WO-010's interface-first push and rebase on WO-010 before opening the PR. Then: `config/sim.yaml` + `detection.py` with `test_detection_model.py` (a), (d), (e) first → `clock.py`, `current.py`, `behaviors.py` with `test_sim_behaviors.py` → `run.py` tick loop (truth step → per-node detection → gaps/timeout/id issue → false alarms → acoustics → 6-dp output), `types.py`, `hashing.py`, rest of `test_detection_model.py` → `scenario.py`, `smoke.yaml`, rewired `__main__.py`/`smoke.py`, `make sim` diff → catalog files and their tests → `toggles.py` → `plot.py` → `make check`, STATE.md, PR with the PNG.

Out: Pydantic schemas for harbor/track/scenario and `config/harbor.geojson` (WO-010); feature extraction, evidence table, pattern-of-life table (WO-012); the hail/challenge-response sim and registry matching (Phase 2; this WO only records `replies_to_hail` and `silent_after_tick` as truth); vessel and surface traffic; toggles other than the three named above; the `Makefile` (the existing `sim` and `sim-plot` targets are used unchanged).

## Spec references
`docs/SPEC-SIM.md` §1–§4 · `docs/SPEC-INVESTIGATION.md` §1 (hypothesis ids), §2 (acoustic feature names), §9.7, §9.9, §9.10 · `docs/SPEC-LADDER.md` §1 (terminal state names used in `expected`) · `docs/WORKFLOW.md` §8 · `config/likelihoods.yaml`, `config/priors.yaml`.

## Files expected
- `soundings/sim/__init__.py`, `__main__.py` (same flags; `--scenario` takes `smoke`, a catalog id, or a path; prints one line per tick, then `track_hash=<hex>`), `smoke.py` (thin wrapper over `scenarios/smoke.yaml`)
- `soundings/sim/clock.py`, `behaviors.py`, `current.py`, `detection.py`, `acoustics.py`, `toggles.py`, `scenario.py`, `types.py`, `run.py`, `hashing.py`, `plot.py` (accepts the existing Makefile's `--scenario <id> --out <path>`; Agg backend)
- `config/sim.yaml`, `config/current_field.yaml`
- `scenarios/smoke.yaml`, `scenarios/S01-filed-survey-on-plan.yaml`, `S03-permitted-hull-cleaning-divers.yaml`, `S04-unfiled-slow-contact-benign.yaml`, `S06-loitering-under-hull.yaml`, `S07-sea-lion.yaml`, `S08-drifting-debris.yaml`
- `tests/test_sim_determinism.py`, `tests/test_detection_model.py`, `tests/test_sim_behaviors.py`, `tests/test_sim_toggles.py`, `tests/test_scenario_catalog.py`, `tests/test_sim_rules.py`, `tests/test_sim_catalog_integration.py`; `tests/test_sim_smoke.py` (updated for the new smoke output)

## Exit tests (must all pass with `MODEL_MODE=replay`, no network)
From BUILD-PLAN Phase 1 (verbatim; the BUILD-PLAN visual item is under Manual below):
- [ ] `pytest tests/test_sim_determinism.py` — scenarios `S01..S08` run with seeds 1,2,3 produce identical track hashes across two runs.
- [ ] `pytest tests/test_detection_model.py` — detection probability decreases monotonically with range; biologic false-alarm rate matches the configured rate within tolerance over 10k samples; track gaps occur at the configured rate.

Unit tests (extensions):
- [ ] `pytest tests/test_detection_model.py` also asserts, numerically (proves SPEC-SIM §2 and SPEC-INVESTIGATION §9.10):
  - (a) `detection_probability` is non-increasing over 0–1000 m in 10 m steps and over random range pairs (hypothesis, `derandomize=True`), equals 0 beyond `2·r0`, and the empirical detection fraction over 10k ticks at 100/300/500/700 m is strictly decreasing;
  - (b) over 10,000 simulated node-hours the false-alarm arrival rate is within 10% relative of 6 per node-hour (about 60k arrivals, so >20 sigma of margin), and the click fraction over ≥10k false alarms is 0.70 ± 0.03;
  - (c) over ≥10k eligible detected ticks at range 0 the gap-start rate is within 15% relative of 0.08 and the mean gap length within 15% relative of 5 ticks;
  - (d) position error std per axis at 300 m over 10k samples is within 10% relative of 14 m;
  - (e) reading confidence is non-increasing in range, scales linearly with health, and health 0 yields no detections.
- [ ] `pytest tests/test_detection_model.py -k timeout` — a contact forced out of range for more than `track_timeout_ticks` comes back under a new observed id, both ids map to the same `gt_id` in `SimRun.contact_truth`, and the gap between them exceeds the timeout — proves re-acquisition is a real problem for later work.
- [ ] `pytest tests/test_sim_behaviors.py` — each of the seven behavior types and the modifier on a no-noise fixture: `transit` closes distance to its target (asset or waypoint) monotonically and stops at it; `lawnmower` produces ≥ 3 legs with ~180° turns inside its zone; `orbit` stays within radius ±10%; `drift` velocity matches the current-field cell within 0.05 m/s; `stationary` stays within 2 m; `lawnmower_then_drift` switches at exactly `fault_tick`; `erratic` has higher turn-rate variance than `transit`; `silent_after_tick` sets tonal false and `replies_to_hail` false after the tick — proves the kinematics WO-012's features will measure.
- [ ] `pytest tests/test_sim_toggles.py` — all nine toggle names parse; `sensor_dropout` zeros detections from that node after `at_tick`; `contact_surge{n: 5}` adds exactly 5 ground-truth contacts; `contact_goes_quiet` silences only the named contact; the other six appear in `parsed_not_implemented` and leave the track hash unchanged — proves twist plumbing is ready.
- [ ] `pytest tests/test_scenario_catalog.py` — `catalog_paths()` finds exactly S01–S08 (not `smoke.yaml`); `ground_truth` and `expected.terminal_state` match the SPEC-SIM §3 table row for each id — proves the catalog matches the spec (schema validity and harbor references are WO-010's `test_config_integration.py`).
- [ ] `pytest tests/test_sim_smoke.py` — `python -m soundings.sim --seed 7 --ticks 60 --scenario smoke` prints 60 tick lines plus the `track_hash` line, exits 0, and is byte-identical across two runs; an unknown scenario exits 2 — proves the Phase 0 exit test still holds after the rewire.

Determinism:
- [ ] `pytest tests/test_sim_determinism.py` also runs `python -m soundings.sim --seed 3 --ticks 120 --scenario S05` in two subprocesses with `PYTHONHASHSEED=0` and `PYTHONHASHSEED=1` and asserts byte-identical stdout, and asserts seeds 1 and 2 give different hashes — proves output depends on the seed and nothing else (CLAUDE.md §2.4).
- [ ] `make sim > /tmp/sim_a.txt; make sim > /tmp/sim_b.txt; diff /tmp/sim_a.txt /tmp/sim_b.txt` — empty diff; proves `make sim` is byte-identical across runs from `scenarios/smoke.yaml`.

Guard / rule proof:
- [ ] `pytest tests/test_sim_rules.py` — (a) runs S05 with `time.time`, `time.perf_counter`, and `datetime.datetime.now` patched to raise, and the run completes; (b) an AST scan of `soundings/sim/*.py` finds no `np.random.seed`, no `np.random.<dist>(` or `random.<fn>(` global calls, and no module-level RNG; (c) every behavior name, toggle name, and string in the new YAML files has no match against `config/denylist.txt` — proves CLAUDE.md §2.4 (seeded, no wall clock) and §2.5 (vocabulary).
- [ ] `grep -riEf config/denylist.txt soundings/sim config/sim.yaml config/current_field.yaml scenarios` — no output; proves CLAUDE.md §2.5 over this WO's files.

Integration:
- [ ] `pytest tests/test_sim_catalog_integration.py` — for each S01–S08 at its own seed: `run_scenario` from the YAML on disk (WO-010 loader and harbor, current field, detection, acoustics, toggles) returns `n_ticks == duration_ticks`, at least one detection of a ground-truth contact (not only false alarms), every `SimRun.tracks` value re-validates as WO-010 `Track`, every observed id maps to a known `gt_id` or `fa-*`, and two runs give equal `track_hash`; the whole file finishes in under 60 s — proves the full pipeline from YAML to tracks works for every catalog entry.

Manual (screenshot attached to PR):
- [ ] BUILD-PLAN verbatim: Visual: `make sim-plot --scenario S04` writes `artifacts/S04.png` showing the track over the harbor map (checked by a human, attached to the PR). Run it as `make sim-plot scenario=S04 && file artifacts/S04.png` (Make does not accept `--scenario`); `file` reports PNG, and the image shows zone polygons, sensor nodes with range circles, the ground-truth path, and observed points coloured by contact id.

## Interfaces
Exposed by WO-011:
- `soundings.sim.run.run_scenario(scenario, *, seed=None, ticks=None, harbor=None, params=None) -> SimRun` (`seed=None` uses `scenario.seed`)
- `soundings.sim.types.SimRun` (`scenario_id, seed, tick_s, n_ticks, tracks: dict[ContactId, Track], detections: list[Detection], contact_truth: dict[str, ContactTruth], truth_paths: dict[str, list[TruthPoint]], toggle_report: ToggleReport`)
- `soundings.sim.types.Detection` (`observed_contact_id, gt_id, tick, t_s, node_id, lat, lon, depth_m, range_m, sensor_health, confidence, propeller_tonal_present, tonal_band, cavitation_index, breathing_or_click_present, broadband_level, is_false_alarm`), `soundings.sim.types.ContactTruth` (`gt_id, hypothesis, replies_to_hail, silent_after_tick`), `soundings.sim.types.TruthPoint`
- `soundings.sim.scenario.resolve_scenario(ref) -> Scenario` (`smoke`, a catalog id such as `S04`, or a path; calls `soundings.schemas.scenario.load_scenario`), `soundings.sim.scenario.catalog_paths() -> list[Path]`
- `soundings.sim.current.CurrentField` (with `.vector_at(lat, lon, tide_phase) -> tuple[float, float]`), `soundings.sim.current.load_current_field(path=DEFAULT_CURRENT_FIELD_PATH)`, `soundings.sim.current.tide_phase_for_hour(hour)`
- `soundings.sim.detection.DetectionParams`, `load_detection_params(path)`, `detection_probability(range_m, r0_m, health, p_max)`, `position_sigma_m(range_m)`, `reading_confidence(range_m, r0_m, health)`
- `soundings.sim.clock.TickClock`, `soundings.sim.hashing.track_hash`, `soundings.sim.toggles.ToggleReport`, `soundings.sim.toggles.IMPLEMENTED_TOGGLES`
- Files: `config/sim.yaml`, `config/current_field.yaml`, `scenarios/S01..S08`, `scenarios/smoke.yaml`

WO-012 uses: `run_scenario`, `SimRun.tracks` / `tick_s` / `scenario_id` only (never `contact_truth`, `truth_paths`, or `detections`, which carry ground truth), `CurrentField.vector_at` + `tide_phase_for_hour` + `load_current_field` (drift_consistency), `resolve_scenario` and `catalog_paths` (CLI and tests), and `run_scenario` on generated scenarios for the normal-month table in `pol.py`.

Consumed from WO-010: `soundings.schemas.harbor.{Harbor, Zone, Asset, SensorNode, load_harbor}`, `soundings.schemas.geo.{to_local_m, from_local_m, distance_m, point_in_polygon}`, `soundings.schemas.track.{AcousticReading, TrackPoint, TrackGap, Track}`, `soundings.schemas.evidence.ContactId`, `soundings.schemas.scenario.{Scenario, ScenarioContact, BehaviorSpec, Toggle, load_scenario}`, `soundings.schemas.enums.{Hypothesis, BehaviorType, ToggleName, TonalBand}`, `soundings.schemas.config.{load_likelihood_spec, load_priors}`, `soundings.canonical.{canonical_json, stable_hash}`, `config/harbor.geojson`.

## Cut line
If more than an hour late, drop in this order (no exit test is dropped): (1) `contact_surge` and `contact_goes_quiet` implementations become parsed-not-implemented (their toggle-test cases drop with them; `sensor_dropout` stays for SPEC-INVESTIGATION §9.7); (2) the current field shrinks to one vector per tide phase (no grid); (3) the plot drops range circles and legend; (4) multi-node fusion becomes nearest-node-only without the tie-break test.

## Model calls
None. Paid run needed? No.
Dependency adds needing owner approval (CLAUDE.md section 3): `matplotlib` as a dev extra (used only by `soundings/sim/plot.py`, imported lazily so the package and `make check` do not need it at runtime).

## Rungs touched
watch (the sim feeds the evidence that watch reads; no rung logic here).

## Hard rules exercised
§2.4 — `test_sim_determinism.py` (hash equality, PYTHONHASHSEED subprocess diff), `test_sim_rules.py` (a, b), `make sim` diff. §2.5 — `test_sim_rules.py` (c) and the denylist grep. §2.3 — no model calls; all tests run with `MODEL_MODE=replay` and no network.

## Done
PR: · Merged commit: · Test summary: · STATE.md updated:
