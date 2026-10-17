# WO-012 — evidence-table-and-features

Phase: 1 · Branch: `wo/012-evidence-table-and-features` · Worktree: `../soundings-worktrees/wo-012-evidence-table-and-features` · Est: 6.5 h · Owner: sim/data seat (unassigned); coordinator Kudrat

## Goal
The typed, append-only evidence table that every later decision reads (SPEC-INVESTIGATION §2). It is built deterministically from the observed tracks of a WO-011 `SimRun`, never from ground truth, with at least one row per catalog feature per contact: a value, or the explicit `unknown` sentinel. It comes with kinematic, acoustic and context extractors; bucket definitions that match `config/likelihoods.yaml` exactly; a first pattern-of-life table generated from a seeded simulated normal month and committed to the repo; and a carry-over function that later re-acquisition work (§9.9) builds on. The ledger UI (WO-060) and the posterior (WO-030) consume this table as it is.

## Scope
In: `EvidenceTable` and its table-level invariants; `features.py` (bucket specs, feature catalog, kinematic/acoustic/context extractors, `transit_pattern` classifier); registry/identity placeholder rows; `pol.py`, last; `carry_over_reacquired`; the `python -m soundings.investigation` dump CLI; a `pol` Makefile target.

**Binding definitions.**

Row and table
- **Row** = the WO-010 `soundings.schemas.evidence.EvidenceRow`: the nine SPEC §2 columns `contact_id, t, feature, value, unit, source, confidence, kind, probe_id`, plus `derived_from` (tuple of row_ids), `assumes` (text) and the computed `row_id` (16-hex content hash). WO-010's validators already enforce: no `None` outside `probe_id`, derived ⇔ non-empty `derived_from`, assumption ⇔ non-empty `assumes`. There is no wrapper type.
- `t` is the integer tick (WO-010); seconds = `t · tick_s`. Every window below is given in seconds and converted with the track's `tick_s`.
- `probe_id` is `None` on every Phase 1 row (no probes run yet; SPEC §2 types it `probe_id | null`).
- Support feature names (WO-010 `FeatureName`, not scored): `track_segment`, `current_field`, `pol_baseline`, `weekday`, `reacquired_from`.

`unknown` versus absent
- **`unknown`** means the catalog feature applies to this contact but cannot be computed at `t`. The row is still written:
  - `value="unknown"`, the feature's own unit, and `confidence=0.0`.
  - A `source` that says why: `insufficient_points`, `no_critical_asset`, `no_sensor_coverage`, `not_reported_by_detector`, `no_ais_feed`, `placeholder:phase2`, `pol_cell_empty` or `no_pol_table`.
- **Absent** (no row at all) happens only in two cases: the feature is outside `FEATURE_CATALOG`, or no extraction pass has run yet for that contact.
- After `build_evidence` returns, every catalog feature has at least one row per contact.
- `bucketize(feature, "unknown")` returns `None`, so the posterior skips `unknown` and absent alike. Neither is ever imputed (SPEC §3).

When a feature is `unknown` (the `MIN_POINTS` table in `features.py`)
- `speed_over_ground`: fewer than 2 points, or a span under 4 s.
- `speed_variance`, `depth_variance`, `turn_rate`, `drift_consistency`: fewer than 3 points.
- `heading_persistence`: fewer than 3 steps whose displacement is at least 2σ position noise. An all-noise track is `unknown` here but `stationary` in `transit_pattern`.
- `transit_pattern`: fewer than 10 points, or a span under 60 s.
- `loiter_score`: a span under `LOITER_WINDOW_S` = 300 s.
- `closing_rate_to_nearest_critical_asset`, `time_to_nearest_critical_asset`: fewer than 2 points, or no asset with criticality ≥ 0.7.
- `zone_id`, `zone_criticality`, `dwell_time_in_zone`: `Harbor.zone_at` returns None for the last point.

Values that look missing but are real
- A receding contact gets `time_to_nearest_critical_asset = NOT_CLOSING_S` (1e9 s; JSON cannot hold infinity) and its closing rate as computed.
- `loiter_score = 0.0` when no asset is within 200 m.

Velocity estimation
- Reading noise is σ ≈ 10–20 m at 2 s ticks, so tick-to-tick differences are meaningless. Velocities come from a least-squares line fit of local-metre positions (`soundings.schemas.geo.to_local_m`) over sliding 60 s windows; speed, heading, turn rate and `drift_consistency` use these.

Kinds
- fact = observed: `track_segment`, acoustic readings, `sensor_health`, `hour_of_day`, `zone_id`, `zone_criticality`, `nearest_asset_id`, and the Phase 2 placeholders.
- assumption = configured, not observed. Each one names what it assumes in `assumes`:
  - `current_field`: "current field is the configured default; no live gauge".
  - `pol_baseline`: "pattern of life is a simulated normal month (seed N); not observed history".
  - `visibility_for_camera`: "visibility from hour-of-day default; no visibility sensor".
  - `weekday`: "weekday is the default (`DEFAULT_WEEKDAY`); scenarios carry no weekday".
  - `reacquired_from`: the match reason passed to `carry_over_reacquired`.
- derived = computed from earlier rows, all listed in `derived_from`. Kinematic rows derive from one `track_segment` fact row; `drift_consistency` derives from `track_segment` plus `current_field`; `pattern_of_life_anomaly` derives from its input rows plus `pol_baseline`.

Confidence (§9.10 groundwork)
- Every row has a confidence in [0,1].
- `track_segment.confidence` is the min `TrackPoint.confidence` in its window. WO-011 sets that per reading from range and sensor_health.
- A derived row's confidence must be ≤ the min of its sources. `append` enforces this with a 1e-9 tolerance.

Disjointness
- For each contact, the set of feature names with kind fact and the set with kind assumption do not intersect. `append` rejects any row that would break this.

Buckets
- `BUCKETS[f] = BucketSpec(names, thresholds)` is a literal in `features.py` (the `config/likelihoods.yaml` header names this file as the canonical definition).
- Intervals are left-closed: `v < t0` gives `names[0]` and `t0 ≤ v < t1` gives `names[1]`, so 0.3 maps to `mid`.
- Boolean features map to `"false"` / `"true"`. The test compares against `soundings.schemas.config.load_likelihood_spec()`, which already turns PyYAML's `[false, true]` booleans back into strings.

**Work items (in this order).** Items 1–6 need only WO-010 schemas and synthetic tracks; items 7–10 and the integration tests need WO-011 merged (rebase onto it).
1. `evidence.py`: `EvidenceTable`, keyed by contact.
   - `append(row: EvidenceRow) -> str` (returns `row.row_id`) checks what a single row cannot: every `derived_from` id is already in the table; the row_id is not already present; the confidence ceiling; disjointness. Violations raise `EvidenceError`.
   - Read API: `rows()` (tuple, append order), `for_contact`, `latest(contact_id, feature)`, `contacts()` (sorted), `to_json()` (6-dp canonical JSON), `table_hash()` (`stable_hash`).
   - There is no update or delete method. Rows are frozen WO-010 models.
2. `features.py`: `BucketSpec`, `BUCKETS`, `bucketize`, and `FEATURE_CATALOG: dict[FeatureName, FeatureSpec(group, unit, extractor_name | "placeholder:phase2")]` covering every SPEC §2 feature plus `track_segment`.
3. Kinematic extractors read a WO-010 `Track`; `drift_consistency` also takes a `CurrentField` and tide phase (`tide_phase_for_hour`).
   - Windows: the last `KIN_WINDOW_S` = 120 s for speed, heading and turn; the last 600 s for `transit_pattern` and `loiter_score`.
   - `drift_consistency` = mean over fitted-velocity windows of `clip(1 − ‖v_obs − v_cur‖ / (‖v_obs‖ + ‖v_cur‖ + ε), 0, 1)`.
   - `track_gap_ratio` = WO-010 `Track.gap_ratio`.
4. `classify_transit_pattern(points) -> TransitPattern`. Rules are checked in this order; the thresholds are module constants.
   - Mean speed < 0.1 m/s gives `stationary`.
   - |cumulative signed turn| ≥ 360° with one turn sign held for ≥ 80% of steps gives `orbit`.
   - At least 2 heading reversals of 150–210°, separated by legs with `heading_persistence` ≥ 0.9, gives `lawnmower`.
   - `heading_persistence` ≥ 0.9 gives `straight`. Anything else gives `random`.
5. Acoustic passthrough: each `TrackPoint.acoustic` field (`propeller_tonal_present`, `tonal_band`, `cavitation_index`, `breathing_or_click_present`, `broadband_level`) becomes a fact row with `source="acoustic_detector:<sensor_id>"` and the point's confidence. A field the detector never reports becomes `unknown` (`not_reported_by_detector`).
6. Context rows:
   - `zone_id`, `zone_criticality` (`Harbor.zone_at`), `nearest_asset_id` (`Harbor.nearest_asset`), and `hour_of_day` (the scenario hour plus elapsed time).
   - One `sensor_health` row per node covering the contact, with `source="sensor:<id>"`.
   - `ais_surface_correlation`: always `unknown` with source `no_ais_feed` in Phase 1 (WO-011 simulates no surface traffic).
   - `visibility_for_camera` and `weekday` as assumption rows.
   - Registry/identity (`registry_match, plan_id, modem_id, hail_sent_at, hail_reply, operator_known`) as `unknown` fact rows with `source="placeholder:phase2"`.
7. `build_evidence(run, harbor, current_field, pol, *, hour_of_day) -> EvidenceTable`: reads `run.tracks` and `run.tick_s` only (no `Scenario`, so no ground truth is in reach). One extraction pass per contact at that contact's last tick. Contacts in sorted `contact_id` order, features in catalog order. The `current_field` and `pol_baseline` assumption rows go in before any rows that cite them. `pol=None` gives `pattern_of_life_anomaly = unknown` with source `no_pol_table`.
8. `carry_over_reacquired(table, old_id, new_id, *, t, match_confidence, reason) -> list[str]`:
   - First appends a `reacquired_from` assumption row for `new_id`.
   - Then re-appends each of `old_id`'s rows under `new_id` as kind derived, with `derived_from=(original, assumption)` and confidence = min(original, `match_confidence`).
   - The old rows are not modified. Deciding which contact is re-acquired is not in this WO.
9. `__main__.py`: `python -m soundings.investigation --scenario S05 --seed 11` resolves the scenario (`resolve_scenario`), runs the sim, loads `data/pol_table.json`, and prints `to_json()`.
10. **Last** — `pol.py`:
   - `generate_normal_month(seed=42, days=28) -> PolTable` builds benign-only synthetic scenarios (FILED_ON_PLAN, BIOLOGIC, DEBRIS_OR_DRIFT, SURFACE_VESSEL_AIS_OFF) for each zone × hour × weekday and runs them through `soundings.sim.run.run_scenario`. It finishes in under 60 s (short runs, one contact each; tune counts to meet the bound).
   - Each cell (`pol[zone][hour][weekday]`, SPEC §4) stores expected contact density, the expected hypothesis mix (sums to 1), the mean and std of speed, depth and dwell, and `transit_pattern` frequencies.
   - `anomaly_score` = mean of `erf(|z_k|/√2)` over the numeric features and `1 − freq(pattern)/max_freq`. It stays in [0,1] and needs no scipy. An empty cell gives `unknown` (`pol_cell_empty`).
   - The table is persisted to `data/pol_table.json`; `make pol` regenerates it.

Out: the posterior and likelihood scoring (WO-030), probes and probe idempotency (WO-031), the registry lookup and hail values (Phase 2; placeholders only here), re-acquisition matching by position/speed/drift box (not in Phase 1; this WO only carries the table over), the confidence-weighted posterior (§9.10 scoring half), any edits to schemas (WO-010) or sim and scenarios (WO-011), SQLite persistence.

## Spec references
`SPEC-INVESTIGATION §2` (columns, features, assumption/derived rules), `§3` (missing features skipped, never imputed), `§4` (pattern of life), `§5` (free probes these extractors back: `kinematics_profile`, `drift_check`, `pattern_of_life_check`, `ais_surface_correlate`, `acoustic_feature_read`), `§9.3, §9.5, §9.7, §9.9, §9.10`; `SPEC-SIM §1–§3`; `config/likelihoods.yaml` (canonical bucket names and thresholds); `config/priors.yaml` (`critical_zone.zone_criticality_min: 0.7` = "critical"); `WORKFLOW.md §8`.

## Files expected
- `soundings/investigation/__init__.py`, `__main__.py`, `evidence.py`, `features.py`, `pol.py`
- `data/pol_table.json` (generated, committed); `Makefile` (adds the `pol` target only)
- `tests/test_evidence.py` (integration), `tests/test_evidence_table.py`, `tests/test_features.py`, `tests/test_transit_pattern.py`, `tests/test_pol.py` (unit)

## Exit tests (must all pass with `MODEL_MODE=replay`, no network)
From BUILD-PLAN Phase 1 (verbatim):
- [ ] `pytest tests/test_evidence.py` — for each scenario the evidence table has every column in `SPEC-INVESTIGATION §2` populated or explicitly `unknown`, never null; `facts` and `assumptions` are disjoint sets.

Integration (`tests/test_evidence.py`, parametrized over WO-011 `catalog_paths()` × seeds 1,2,3; asserts exactly 8 scenarios):
- [ ] `uv run pytest tests/test_evidence.py -k completeness` — builds the table from a real WO-011 `SimRun` for S01..S08; for every contact, every `FEATURE_CATALOG` feature has a row; every column except `probe_id` is non-null, and `probe_id` is null on every row (no probes in Phase 1) — proves no catalog gap reaches the posterior silently.
- [ ] `uv run pytest tests/test_evidence.py -k disjoint` — fact and assumption feature sets do not intersect per contact, and every assumption has a non-empty `assumes` — proves §2's labeling rule over real runs.
- [ ] `uv run pytest tests/test_evidence.py -k provenance` — every derived row's `derived_from` ids are present earlier in `rows()`, and its confidence ≤ the min of its sources; the `current_field` row text equals "current field is the configured default; no live gauge" — proves the traceability the SITREP will cite.
- [ ] `uv run pytest tests/test_evidence.py -k signatures` — spot checks: S08 `drift_consistency` buckets to `high`; S05 `closing_rate_to_nearest_critical_asset` > 0 and `transit_pattern == straight`; S06 `transit_pattern == orbit`; S07 `breathing_or_click_present` has a `true` row — proves the extractors see what the catalog says each scenario shows.

Unit:
- [ ] `uv run pytest tests/test_features.py -k buckets_match_yaml` — for every feature in `load_likelihood_spec()`, `BUCKETS` has identical names and thresholds; table-driven boundary values (below, at, and above each threshold) — proves the scorer and the extractors cannot disagree on a bucket.
- [ ] `uv run pytest tests/test_features.py -k catalog_covers_likelihoods` — every likelihoods feature is in `FEATURE_CATALOG` with an extractor or an explicit `placeholder:phase2` (`registry_match`, `hail_reply`); every SPEC §2 feature is listed — proves no spec feature is silently missing.
- [ ] `uv run pytest tests/test_features.py -k unknown_rules` — table-driven synthetic tracks of 1, 2, 3, 9 and 10 points, a last point outside every zone, a harbor with no critical asset, and a receding contact each produce `unknown`, a value, or `NOT_CLOSING_S` exactly as the definitions say; kinematic rows' confidence equals the min point confidence — proves `unknown` is deliberate, never a gap.
- [ ] `uv run pytest tests/test_features.py -k velocity_fit` — a straight 1.0 m/s track with σ=15 m noise at 2 s ticks (3 seeds) gives `speed_over_ground` within 10% of 1.0 and `heading_persistence` ≥ 0.9 — proves kinematics survive realistic reading noise.
- [ ] `uv run pytest tests/test_transit_pattern.py` — table-driven synthetic tracks: straight line, 4-leg lawnmower, 1.5-turn orbit, seeded random walk, stationary with 5 m noise, each under 3 noise seeds, plus edge cases (half orbit gives `random`, a single reversal does not give `lawnmower`) — proves the classifier is deterministic and separable.
- [ ] `uv run pytest tests/test_evidence_table.py` — `append` rejects an unknown `derived_from` id, a duplicate row_id, a fact/assumption collision, and a confidence over the ceiling with `EvidenceError`; there is no mutation API; `carry_over_reacquired` on a synthetic S02-style gap re-keys every row as derived, leaves the old rows unchanged and caps confidence at `match_confidence` — proves the table is append-only and its invariants hold.
- [ ] `uv run pytest tests/test_pol.py` — anomaly is in [0,1]; a benign-like vector in a populated cell scores < 0.5; a fast deep orbit at 02:00 scores ≥ 0.85; an empty cell gives `unknown`; every populated cell has density > 0 and a hypothesis mix summing to 1; the persisted table equals a fresh `generate_normal_month(seed=42)`, generated in under 60 s — proves SPEC §4 is met and the committed table is reproducible.

Determinism:
- [ ] `uv run python -m soundings.investigation --scenario S05 --seed 11 > /tmp/e1.json; uv run python -m soundings.investigation --scenario S05 --seed 11 > /tmp/e2.json; diff /tmp/e1.json /tmp/e2.json` → empty; and `make pol && git diff --exit-code data/pol_table.json` → exit 0; proves CLAUDE.md §2.4 for the evidence table and the pattern-of-life baseline.

Guard / rule proof:
- [ ] CLAUDE.md §2.6 (facts and assumptions labeled separately; evidence traceable) is proven by the `disjoint` and `provenance` integration tests above.
- [ ] `! grep -rnE "contact_truth|truth_paths|gt_id|is_false_alarm|ground_truth|\.detections" soundings/investigation/evidence.py soundings/investigation/features.py soundings/investigation/__main__.py` — exit 0 (no match); proves the evidence table is built from observed tracks only, so §2.6's evidence is honest (`pol.py` is exempt: the simulated baseline is built from its own generated scenarios).
- [ ] CLAUDE.md §2.2 and §2.5: `! grep -rn "soundings.llm" soundings/investigation` → exit 0 (no model path feeds the table), and `grep -riEf config/denylist.txt soundings/investigation tests/test_evidence*.py tests/test_features.py tests/test_transit_pattern.py tests/test_pol.py data/pol_table.json` → empty; `make check` green.

No manual check: BUILD-PLAN's Phase 1 visual (`make sim-plot scenario=S04`) belongs to WO-011.

## Interfaces
Exposes (to WO-030/031/060 and later re-acquisition work; WO-010 and WO-011 consume none of these):
- `soundings.investigation.evidence.EvidenceTable`, `.EvidenceError`, `.carry_over_reacquired`
- `soundings.investigation.features.BucketSpec`, `.BUCKETS`, `.bucketize`, `.FeatureSpec`, `.FEATURE_CATALOG`, `.MIN_POINTS`, `.NOT_CLOSING_S`, `.extract_kinematics`, `.classify_transit_pattern`, `.extract_acoustic`, `.extract_context`, `.build_evidence`
- `soundings.investigation.pol.PolTable`, `.generate_normal_month`, `.load_pol_table`, `.anomaly_score`
- Files: `data/pol_table.json`; CLI `python -m soundings.investigation`; `make pol`

Consumes from WO-010: `soundings.schemas.evidence.{EvidenceRow, UNKNOWN, ContactId}`, `soundings.schemas.enums.{EvidenceKind, FeatureName, Unit, Hypothesis, TransitPattern}`, `soundings.schemas.track.{Track, TrackPoint}`, `soundings.schemas.harbor.{Harbor, Zone, Asset, SensorNode, load_harbor}`, `soundings.schemas.geo.{to_local_m, distance_m}`, `soundings.schemas.scenario.Scenario` (pol.py only), `soundings.schemas.config.load_likelihood_spec`, `soundings.canonical.{canonical_json, stable_hash}`.
Consumes from WO-011: `soundings.sim.run.run_scenario`, `soundings.sim.types.SimRun` (`.tracks`, `.tick_s`, `.scenario_id`), `soundings.sim.scenario.{resolve_scenario, catalog_paths}`, `soundings.sim.current.{CurrentField, load_current_field, tide_phase_for_hour}` with `CurrentField.vector_at(lat, lon, tide_phase)`; `TrackPoint.confidence` and `TrackPoint.acoustic` populated per reading.

## Cut line
If the WO runs more than an hour late, drop in this order (no exit test changes): (1) the weekday axis of `PolTable` (key `*`; zone × hour only); (2) the normal month shrinks from 28 to 7 days; (3) `visibility_for_camera` goes to an `unknown` row (completeness still holds).

## Model calls
None. Paid run needed? No. Dependency adds needing owner approval (CLAUDE.md §3): none (statistics use `numpy` and `math.erf`; no scipy).

## Rungs touched
watch (backs the free probes `kinematics_profile`, `drift_check`, `pattern_of_life_check`, `ais_surface_correlate`, `acoustic_feature_read`). Nothing at ask or above.

## Hard rules exercised
§2.6 (the `disjoint` and `provenance` tests, and the ground-truth grep); §2.4 (the two-run `diff` and the `data/pol_table.json` regeneration diff); §2.2 (the `soundings.llm` grep: no model output enters the table); §2.5 (the denylist grep plus `make check`).

## Done
PR: [#7](https://github.com/kudratsingh/soundings/pull/7) · Merged commit: squash commit on main (see PR) · Test summary: `754 passed` (full suite; tests/test_evidence.py 116, test_evidence_table.py 24, test_features.py 134, test_transit_pattern.py 65, test_pol.py 20) · STATE.md updated: yes (WO-012 row)

- Exit tests: all pass with `MODEL_MODE=replay`. `make check` exits 0. The S05 seed 11 CLI dump is byte-identical across two runs (sha256 `28486872…e993`), and `make pol && git diff --exit-code data/pol_table.json` exits 0. The ground-truth, `soundings.llm` and denylist greps find nothing.
- `make pol` regenerates `data/pol_table.json` (seed 42, 28 days, 970 populated cells including the pooled `*` cells) in about 11 s locally. `test_pol.py` regenerates it and checks the 60 s bound.
- Interface differences from the WO text:
  - `build_evidence(run, harbor, current_field, pol=None, *, hour_of_day, tide_phase_for_hour=None)`. `current_field` may be a `CurrentField`, in which case pass `tide_phase_for_hour`.
  - `features.pol_observation(track, harbor, *, hour_of_day)` gives the section 4 vector that pol.py builds the month from.
  - `PolTable.anomaly(zone, hour, weekday, observed)` falls back to the pooled `*` cell when the weekday cell has fewer than 8 characterized contacts.
  - `PolCell` also carries `n_characterized`.
- Interpretations and deviations: see DECISIONS.md 2026-10-16 22:29 (noise-robust kinematics) and 2026-10-16 23:10 (pattern-of-life details, and the S05 check at tick 180). No cut-line item was used.
