# WO-010 — schemas-and-harbor-map

Phase: 1 · Branch: `wo/010-schemas-and-harbor-map` · Worktree: `../soundings-worktrees/wo-010-schemas-and-harbor-map` · Est: 4 h · Owner: sim/data seat (unassigned); coordinator Kudrat

## Goal
Every type the rest of Soundings passes around exists as a strict Pydantic v2 model under `soundings/schemas/`. That covers the harbor, contacts and tracks, evidence rows, the scenario file, and the fixed vocabularies (8 hypotheses, 5 rungs, 15 ladder states). `config/harbor.geojson` holds a simplified Port of LA map that every scenario resolves against. Validators reject invalid rungs and states, denylisted words in free text, and `None` in evidence values. Every model round-trips through JSON, and one canonical-JSON helper (6 dp) serves hashing everywhere. WO-011 and WO-012 import from here and define no parallel types.

## Scope
In:
- **Interface-first commit.** Within the first 45 min, push `enums.py` and the model signatures (fields only, validators stubbed) to the branch so WO-011 and WO-012 can branch from it. The validators and tests follow on the same branch and in the same PR.
- **Enums** (`soundings/schemas/enums.py`, all `StrEnum`, members in exactly this order):
  - `Hypothesis` (8): FILED_ON_PLAN, FILED_OFF_PLAN, UNFILED_KNOWN_OPERATOR, BIOLOGIC, DEBRIS_OR_DRIFT, SURFACE_VESSEL_AIS_OFF, UNAUTHORIZED_UUV, UNAUTHORIZED_DIVER.
  - `Rung` (5): watch, ask, slow, hold, hand_to_person (values match `config/ladder.yaml` `rungs`).
  - `LadderState` (15): DETECTED, WATCHING, ASKING, INVESTIGATING, ADVISING_SLOW, HOLDING, AWAITING_APPROVAL, HANDED_OFF, WATCH_AFTER_HANDOFF, RESOLVED_BENIGN, CLEARED_AT_ASK, HAZARD_NOTICE, LOST_VEHICLE, RESOLVED_DEPARTED, FAILED.
  - `TERMINAL_STATES`: a frozenset of RESOLVED_BENIGN, CLEARED_AT_ASK, HAZARD_NOTICE, LOST_VEHICLE, RESOLVED_DEPARTED, FAILED.
  - `HANDLING_TERMINAL_STATES = TERMINAL_STATES | {HANDED_OFF}`. HANDED_OFF is terminal for handling only (SPEC-LADDER §3).
  - `EvidenceKind`: fact, assumption, derived.
  - `FeatureName` (37: the 32 SPEC-INVESTIGATION §2 features in §2 order, then 5 support names):
    - Kinematic: speed_over_ground, speed_variance, depth, depth_variance, heading_persistence, turn_rate, closing_rate_to_nearest_critical_asset, time_to_nearest_critical_asset, dwell_time_in_zone, track_gap_ratio, drift_consistency, loiter_score, transit_pattern.
    - Acoustic: propeller_tonal_present, tonal_band, cavitation_index, breathing_or_click_present, broadband_level.
    - Registry: registry_match, plan_id, modem_id, hail_sent_at, hail_reply, operator_known.
    - Context: zone_id, zone_criticality, nearest_asset_id, hour_of_day, pattern_of_life_anomaly, ais_surface_correlation, sensor_health, visibility_for_camera.
    - Support (provenance rows WO-012 writes; no likelihood row, never scored): track_segment, current_field, pol_baseline, weekday, reacquired_from.
  - `ProbeId` (11): registry_lookup, kinematics_profile, drift_check, pattern_of_life_check, ais_surface_correlate, acoustic_feature_read, acoustic_hail, camera_cue, second_sensor_dwell, ask_operator_by_phone, request_watch_officer_look.
  - `TransitPattern`: straight, lawnmower, orbit, random, stationary.
  - `TonalBand`: none, low, mid, high.
  - `RegistryMatch`: exact, area_mismatch, window_mismatch, none.
  - `HailReply`: valid, invalid, none, timeout.
  - `BehaviorType`: transit, lawnmower, orbit, drift, erratic, stationary, lawnmower_then_drift.
  - `ToggleName` (9): contact_surge, sensor_dropout, network_down, approver_unreachable, ais_spoof, second_coordinated_contact, standing_orders_expired, contact_goes_quiet, fog_no_camera.
  - `Unit`: m, m/s, m/s2, deg/s, s, ratio, bool, category, id, hour, dB, unknown.
- **Strictness everywhere.** Every model uses `ConfigDict(extra="forbid", frozen=True, strict=True, allow_inf_nan=False)`. YAML and GeoJSON enter through `model_validate` on a plain dict, with a lax-mode override for enums only, so a string from a file becomes an enum member but `True` never coerces to `1`.
- **Unknown sentinel.** `Unknown = Literal["unknown"]` and `UNKNOWN: Unknown = "unknown"`. `EvidenceValue = StrictBool | StrictInt | StrictFloat | StrictStr | Unknown`. `None` is rejected in `value`, `unit`, and `confidence`. The only nullable column is `probe_id`, because SPEC-INVESTIGATION §2 types it `probe_id | null`.
- **EvidenceRow** (`soundings/schemas/evidence.py`). The fixed columns are `contact_id: ContactId`, `t: int` (the tick, ≥ 0; seconds = t·tick_s), `feature: FeatureName`, `value: EvidenceValue`, `unit: Unit`, `source: Source`, `confidence: float in [0, 1] | Unknown`, `kind: EvidenceKind`, and `probe_id: ProbeId | None`. Three fields support the §2 rules and SITREP citation:
  - `derived_from: tuple[str, ...] = ()`: non-empty if and only if kind=derived; each entry is a row_id.
  - `assumes: FreeText = ""`: non-empty if and only if kind=assumption.
  - `row_id`: a computed field, the first 16 hex characters of `stable_hash` over the fixed columns plus derived_from and assumes.
  - Formats: `ContactId` matches `^c-\d{4,6}$`. `Source` matches `^[a-z0-9_.:-]+$` (for example `sim.detector:hyd_1`, `features.kinematics`, `probe:acoustic_hail`).
- **Tracks** (`soundings/schemas/track.py`):
  - `AcousticReading`: propeller_tonal_present, tonal_band, cavitation_index, breathing_or_click_present, broadband_level.
  - `TrackPoint`: tick, lat, lon, depth_m ≥ 0, sigma_m > 0, sensor_id, confidence in [0, 1], acoustic.
  - `TrackGap`: start_tick ≤ end_tick, inclusive.
  - `Track`: contact_id, tick_s > 0, points, gaps. Ticks rise strictly; gaps are sorted, disjoint, and never cover a point's tick. `gap_ratio` is a property.
  - `Contact`: contact_id, track, `reacquired_from: ContactId | None`. This field supports SPEC-INVESTIGATION 9.9; Phase 1 only defines it (the re-acquisition matcher that fills it is not in Phase 1).
  - Lat must lie in [33.70, 33.78] and lon in [-118.30, -118.22].
- **Harbor** (`soundings/schemas/harbor.py`, `geo.py`):
  - `Zone`: zone_id, name, criticality in [0, 1], basin, polygon.
  - `Asset`: asset_id, name, criticality, asset_kind ∈ {berth, gate, piling}, lat, lon.
  - `SensorNode`: node_id, name, lat, lon, range_m > 0, health in [0, 1].
  - IDs match `^[a-z][a-z0-9_]*$` and are unique across all three kinds.
  - `Harbor` methods:
    - `zone_at(lat, lon)` returns the containing zone with the highest criticality (ties go to the lowest zone_id), or None.
    - `nearest_asset(lat, lon, min_criticality=0.0)` returns the nearest asset; ties go to the lowest asset_id.
    - `basins()` returns the sorted basin names.
    - `asset(id)` and `zone(id)` raise `KeyError` on an unknown ID.
  - `geo.py` holds pure functions with no new dependency: `to_local_m` and `from_local_m` (an equirectangular projection about a fixed origin at 33.74, -118.265), `distance_m`, and `point_in_polygon` (ray casting).
- **`config/harbor.geojson`.** A GeoJSON FeatureCollection whose top-level name is `"Port of Los Angeles (simplified for demonstration)"`. Each feature's `properties.kind` is one of zone, asset, or sensor_node. Coordinates are approximate, and the polygons are drawn around the start points already fixed in S02 and S05.
  - Zones:
    - `main_channel` (basin main_channel, crit 0.5). Contains S05's start (33.7350, -118.2620).
    - `inner_harbor_west` (basin inner_harbor_west, crit 0.4).
    - `inner_basin_research` (basin inner_harbor_west, crit 0.3). Contains S02's start (33.7405, -118.2690).
    - `fuel_berths` (basin inner_harbor_west, crit 0.9).
    - `pier_400_approach` (basin outer_harbor, crit 0.6).
    - `vincent_thomas_bridge` (basin main_channel, crit 0.8).
  - Assets:
    - `fuel_berth_1` and `fuel_berth_2` (berth, crit 0.9 and 0.95). fuel_berth_2 sits near (33.7430, -118.2655), 700 to 1100 m from S05's start.
    - `pier_400_gate` (gate, 0.7).
    - `vtb_piling_west` and `vtb_piling_east` (piling, 0.8), near lat 33.7497.
  - Sensor nodes: `hyd_1` through `hyd_4`, each with range_m 400 and health 1.0. Suggested anchors: (33.7410, -118.2650), (33.7490, -118.2710), (33.7220, -118.2530), (33.7300, -118.2620). They may move as long as the coverage tests pass.
- **Scenario schema** (`soundings/schemas/scenario.py`, SPEC-SIM §4):
  - `Scenario` has id `^S\d{2}$` (or the literal `smoke` for WO-011's `scenarios/smoke.yaml`, or `^L-\d{8}T\d{6}$` for `scenarios/live/`), name, seed, duration_ticks > 0, tick_s > 0, ground_truth: Hypothesis, hour_of_day in 0..23, `registry_plans: tuple[RegistryPlan, ...] = ()`, `known_operators: tuple[KnownOperator, ...] = ()` (`KnownOperator`: operator: FreeText, modem_id; needed by S04), contacts (at least 1, unique ids), `toggles: tuple[Toggle, ...] = ()`, `expected: Expected`, and `notes: FreeText = ""`.
  - `RegistryPlan`: plan_id, operator, vehicle, modem_id, area (a zone_id), `window: PlanWindow` (start_tick, end_tick), contact.
  - `ScenarioContact`: id, `start: StartPoint` (lat, lon, depth_m), `behavior: BehaviorSpec`, `acoustic: AcousticSpec` (propeller_tonal: bool | None, tonal_band: TonalBand | None, biologic_signature: bool | None, each default None; None means WO-011 samples the value from the hypothesis row of `config/likelihoods.yaml`), `registry: ContactRegistrySpec` (filed, plan_id?, modem_id?, replies_to_hail, silent_after_tick?).
  - `BehaviorSpec` has `type: BehaviorType` plus optional target_asset, waypoint (lat, lon), area, speed_mps, radius_m, lane_spacing_m, heading_deg, fault_tick, jitter_mps ≥ 0, and drift_weight in [0, 1]. A model validator requires the fields each type needs: transit needs target_asset or waypoint (not both) and speed_mps; orbit needs target_asset and radius_m; lawnmower needs area; lawnmower_then_drift needs area or a plan and a fault_tick below duration.
  - `Toggle`: name: ToggleName plus optional n, node_id, contact_id, and at_tick. A bare string in YAML is coerced to `{name: ...}`.
  - `Expected`: terminal_state: LadderState, `passes_through: tuple[LadderState, ...] = ()`, and optional top_hypothesis, min_threat_mass, max_human_interruptions, max_probes, sitrep_has_search_box, and search_box_contains_truth. Unknown keys are rejected; a twist that adds a key adds a field here.
  - Cross-field checks: plan_id references a plan in `registry_plans`; a plan window lies within the duration; silent_after_tick is below the duration.
  - `load_scenario(path, harbor=None)` resolves area and asset IDs against the harbor (the default harbor when none is passed). `load_scenarios(root)` reads `scenarios/*.yaml` and `scenarios/live/*.yaml`, sorted by path.
- **Config loaders** (`soundings/schemas/config.py`):
  - `load_likelihood_spec()` returns `LikelihoodSpec{features: dict[FeatureName, FeatureBuckets{buckets: tuple[str, ...], thresholds: tuple[float, ...] | None, rows: dict[Hypothesis, tuple[float, ...]]}]}`. YAML reads `[false, true]` as booleans, so the loader turns them back into the strings "false" and "true". The loader checks shape only: row keys are exactly the 8 hypotheses, row length equals the bucket count, and len(thresholds) equals buckets − 1. Row-sum tests belong to WO-030.
  - `load_priors()`: keys of `base` are exactly the 8 hypotheses.
  - `load_ladder_config()`: `rungs` equals the `Rung` order, `lives_on` ⊆ Rung, and the boundary keys equal Rung.
  - `load_standing_orders(harbor)`: basins resolve in `harbor.basins()`, and `allowed_rungs` ⊆ Rung. This WO does not check the signature.
- **Denylist** (`soundings/schemas/denylist.py`):
  - At import, the module compiles each non-blank, non-`#` line of `config/denylist.txt` into `DENYLIST: tuple[re.Pattern[str], ...]` with `re.IGNORECASE`. A missing file raises at import.
  - `find_denylisted(text) -> list[str]` and `assert_clean(text) -> str` do the matching.
  - `FreeText = Annotated[str, AfterValidator(assert_clean)]` is the type of every human-readable string: zone, asset, and node `name`; scenario `name` and `notes`; plan operator, vehicle, and contact; and EvidenceRow `assumes`.
- **Canonical JSON.** Move the helper (with `_normalize` and `FLOAT_DECIMALS`) into `soundings/canonical.py` and add `round6` and `stable_hash(obj) -> sha256 hex`. Pydantic models are dumped with `model_dump(mode="json")`. `soundings/llm/client.py` re-exports `canonical_json`, and its output stays byte-identical, so the existing `tests/test_llm_client.py` goldens still pass.

Out: sim behaviour and detection, and every file under `scenarios/` (WO-011). The feature extractors, the EvidenceTable container, and pattern of life (WO-012). Likelihood row sums and prior sums (WO-030). The ladder transition table and events (WO-040). Standing-orders signature verification (WO-041). The registry DB models (WO-020). No ground-truth types leak into investigation types.

## Spec references
`docs/SPEC-SIM.md §1, §4`; `docs/SPEC-INVESTIGATION.md §1, §2, §5, §9.9`; `docs/SPEC-LADDER.md §1, §2`; `docs/WORKFLOW.md §8`; `config/*.yaml`; `config/denylist.txt`.

## Files expected
- `soundings/canonical.py`, `soundings/llm/client.py` (re-export only)
- `soundings/schemas/__init__.py` (re-exports the public names listed under Interfaces), `enums.py`, `evidence.py`, `track.py`, `harbor.py`, `geo.py`, `scenario.py`, `config.py`, `denylist.py`
- `config/harbor.geojson`
- `tests/test_schemas.py` (unit), `tests/test_config_integration.py` (integration)

## Exit tests (must all pass with `MODEL_MODE=replay`)
These are copied verbatim from BUILD-PLAN Phase 1:
- [ ] `pytest tests/test_schemas.py` — every schema round-trips JSON; invalid rungs and denylisted words are rejected by validators.

Unit tests (`tests/test_schemas.py`; Hypothesis runs with `@settings(derandomize=True, max_examples=200)`):
- [ ] `pytest tests/test_schemas.py::test_enum_members_exact` — proves `Hypothesis`, `Rung`, and `LadderState` hold exactly 8, 5, and 15 members in spec order, and that `TERMINAL_STATES` and `HANDLING_TERMINAL_STATES` match SPEC-LADDER §3.
- [ ] `pytest tests/test_schemas.py::test_invalid_rung_and_state_rejected` — proves a model field typed Rung or LadderState rejects off-vocabulary values ("climb", "HOLD", "hand to person", "", None), so a stray string cannot become a rung.
- [ ] `pytest tests/test_schemas.py::test_denylist_compiled_from_file` — proves the pattern count equals the non-comment lines of `config/denylist.txt`. For each pattern, the test builds a sample word from the pattern itself (strip `\b`, keep the first character of each class, drop optional groups, take the first alternative), so no denylisted literal is stored in the test source.
- [ ] `pytest tests/test_schemas.py::test_free_text_fields_reject_denylist` — proves every field annotated `FreeText` rejects every generated sample. The test finds those fields by walking each model's `model_fields`, so none can be missed. It also proves the near-misses "program", "ramp", "network", and "netting" are accepted.
- [ ] `pytest tests/test_schemas.py::test_evidence_row_rules` — proves the evidence-row rules: None is rejected in value, `"unknown"` is accepted, `True` is not coerced to `1`, and NaN is rejected. derived without derived_from fails, assumption without assumes fails, and a fact carrying either fails. `row_id` is stable across two constructions.
- [ ] `pytest tests/test_schemas.py::test_scenario_validators` — proves transit with neither or both of target_asset/waypoint fails, lawnmower_then_drift with fault_tick ≥ duration fails, a plan_id not in `registry_plans` fails, a plan window past the duration fails, an unknown `expected` key fails, a bare toggle string becomes `Toggle(name=...)`, and an off-list toggle name fails.
- [ ] `pytest tests/test_schemas.py::test_track_validators` — proves non-increasing ticks, overlapping gaps, a gap covering a point, and an out-of-bbox position are each rejected.
- [ ] `pytest tests/test_schemas.py::test_roundtrip_evidence_row_property` — proves that for any generated EvidenceRow, `model_validate_json(dump_json(x)) == x` and `canonical_json` is unchanged by the round trip.
- [ ] `pytest tests/test_schemas.py::test_roundtrip_track_property` — proves the same for any generated Track, including gaps and Contact with `reacquired_from`.
- [ ] `pytest tests/test_schemas.py::test_canonical_json_6dp` — proves 0.1234565 and 0.1234564 round as specified, -0.0 becomes 0.0, keys are sorted, Pydantic models and enums serialize, and NaN raises. `soundings.llm.client.canonical_json is soundings.canonical.canonical_json`.
- [ ] `pytest tests/test_llm_client.py` — proves the move kept the Phase 0 canonical goldens and fixture keys byte-identical.
- [ ] `pytest tests/test_schemas.py::test_likelihood_bucket_names` — proves the bucket tuples equal the YAML exactly (`("false", "true")` as strings) and the threshold lengths are buckets − 1. No sums are checked.

Integration tests (`tests/test_config_integration.py`; they load the real files on disk):
- [ ] `pytest tests/test_config_integration.py::test_harbor_required_ids` — proves the harbor contains:
  - the zones main_channel, inner_harbor_west, inner_basin_research, fuel_berths, pier_400_approach, and vincent_thomas_bridge;
  - the basin inner_harbor_west;
  - the assets fuel_berth_1, fuel_berth_2, pier_400_gate, and both piling assets;
  - 3 or 4 sensor nodes, each with range_m 400 and health 1.0.
- [ ] `pytest tests/test_config_integration.py::test_every_scenario_parses` — proves each `scenarios/*.yaml` file (one parametrized case per file, so the six WO-011 catalog files and `smoke.yaml` are covered automatically) validates as `Scenario` and round-trips JSON.
- [ ] `pytest tests/test_config_integration.py::test_scenario_refs_resolve` — proves every target_asset and plan area resolves in the harbor. It also proves every contact start lies inside some zone and within 2·range_m of a sensor node, and that S02's start lies inside `inner_basin_research`.
- [ ] `pytest tests/test_config_integration.py::test_s05_geometry_reachable` — proves `distance_m(S05 start, fuel_berth_2) / speed_mps < duration_ticks·tick_s`, and that fuel_berth_2 lies within 400 m of a node, so the pitch scenario can arrive and is observed.
- [ ] `pytest tests/test_config_integration.py::test_every_config_validates` — proves likelihoods, priors, ladder, and standing_orders all load through their loaders, and that the standing-orders basins resolve in the harbor.

Toolchain:
- [ ] `uv run mypy soundings && uv run ruff check . && make check` — proves the types are sound and the whole repo stays green.

Determinism:
- [ ] `for s in 1 2; do PYTHONHASHSEED=$s uv run python -c "from soundings.schemas import load_harbor, load_scenarios; from soundings.canonical import stable_hash; print(stable_hash([load_harbor(), *load_scenarios('scenarios')]))" > /tmp/h$s.txt; done; diff /tmp/h1.txt /tmp/h2.txt` (empty diff) — proves parsing and hashing do not depend on set or hash ordering (CLAUDE.md §2.4).

Guard and rule proof:
- [ ] `pytest tests/test_schemas.py::test_no_identifier_matches_denylist` — proves no enum value, model class name, field name, or harbor ID matches the denylist, and that `LadderState` has no member after HANDED_OFF other than WATCH_AFTER_HANDOFF and RESOLVED_DEPARTED. This covers CLAUDE.md §2.1 and §2.5: the vocabulary cannot even express a post-handoff action.
- [ ] `grep -riEf config/denylist.txt soundings/schemas soundings/canonical.py config/harbor.geojson tests/test_schemas.py tests/test_config_integration.py` — prints nothing. This proves CLAUDE.md §2.5 over this WO's files.

No manual check: the Phase 1 manual item (the S04 plot) belongs to WO-011.

## Interfaces
Exposed. Every name below is re-exported from `soundings.schemas` unless it says otherwise.
- `soundings.canonical`: `canonical_json`, `round6`, `stable_hash`, `FLOAT_DECIMALS`. `soundings.llm.client.canonical_json` re-exports `canonical_json`.
- `soundings.schemas.enums`: `Hypothesis`, `Rung`, `LadderState`, `TERMINAL_STATES`, `HANDLING_TERMINAL_STATES`, `EvidenceKind`, `FeatureName`, `ProbeId`, `TransitPattern`, `TonalBand`, `RegistryMatch`, `HailReply`, `BehaviorType`, `ToggleName`, `Unit`.
- `soundings.schemas.evidence`: `Unknown`, `UNKNOWN`, `EvidenceValue`, `ContactId`, `Source`, `EvidenceRow`.
- `soundings.schemas.track`: `AcousticReading`, `TrackPoint`, `TrackGap`, `Track`, `Contact`.
- `soundings.schemas.harbor`: `Zone`, `Asset`, `SensorNode`, `Harbor`, `load_harbor`, `DEFAULT_HARBOR_PATH`.
- `soundings.schemas.geo`: `to_local_m`, `from_local_m`, `distance_m`, `point_in_polygon`.
- `soundings.schemas.scenario`: `Scenario`, `ScenarioContact`, `StartPoint`, `BehaviorSpec`, `AcousticSpec`, `ContactRegistrySpec`, `RegistryPlan`, `PlanWindow`, `KnownOperator`, `Toggle`, `Expected`, `load_scenario`, `load_scenarios`.
- `soundings.schemas.config`: `LikelihoodSpec`, `FeatureBuckets`, `load_likelihood_spec`, `load_priors`, `load_ladder_config`, `load_standing_orders`.
- `soundings.schemas.denylist`: `DENYLIST`, `find_denylisted`, `assert_clean`, `FreeText`.
- Files: `config/harbor.geojson`.

Who uses what:
- WO-011 uses Scenario and its loaders, Harbor and geo (no sim-local geo module), Track/TrackPoint/TrackGap/AcousticReading, Hypothesis, ToggleName, BehaviorType, load_likelihood_spec and load_priors (acoustic sampling, surge mix), and stable_hash (for its track hash).
- WO-012 uses EvidenceRow (including derived_from, assumes, row_id), UNKNOWN, FeatureName, Unit, EvidenceKind, TransitPattern, Track/TrackPoint, Harbor.zone_at and nearest_asset, geo (no investigation-local geo module), and load_likelihood_spec. The bucket literal `BUCKETS` lives in WO-012's `features.py` (the `config/likelihoods.yaml` header names that file as canonical); WO-012 tests it against `load_likelihood_spec()`.
- If either WO needs a new field, it goes in as a PR to these files, not as a local type.

Consumed: from Phase 0, `soundings/llm/client.py` (`canonical_json`, `fixture_key`) and `tests/conftest.py`. Nothing comes from WO-011 or WO-012, except that the scenario files WO-011 adds must pass `test_every_scenario_parses`.

## Cut line
If this WO runs more than an hour late, drop these in order:
1. `from_local_m` and `Harbor.basins()` sorting niceties.
2. The `ProbeId` typing on `probe_id` (fall back to a `Source`-style pattern string).
3. `Unit` as an enum (fall back to a non-empty `StrictStr | Unknown`).
4. The extra `BehaviorSpec` per-type requirements beyond transit and lawnmower_then_drift.
No exit test is cut.

## Model calls
None. Paid run needed? No.
Dependency adds needing owner approval (CLAUDE.md section 3): none. Point-in-polygon and distance are hand-written, and `hypothesis` is already a dev dependency.

## Rungs touched
None executed. This WO defines the typed vocabulary for all five rungs (watch, ask, slow, hold, hand to a person) and all 15 states.

## Hard rules exercised
- §2.1: `test_no_identifier_matches_denylist` and `test_enum_members_exact` show that no post-handoff action state exists.
- §2.2: `test_invalid_rung_and_state_rejected` shows that only enum members can occupy a rung or state field.
- §2.4: the PYTHONHASHSEED diff, the derandomized property tests, `test_canonical_json_6dp`, and the Phase 0 goldens.
- §2.5: `test_free_text_fields_reject_denylist` and the grep.
- §2.6: `test_evidence_row_rules` (fact, assumption, and derived are distinct; assumptions name what they assume; derived rows name their sources; no nulls).

## Done
PR: · Merged commit: · Test summary: · STATE.md updated:
