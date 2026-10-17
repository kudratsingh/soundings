# WO-080 — busy-tuesday

Phase: 7 · Branch: `wo/080-busy-tuesday` · Worktree: `../soundings-worktrees/wo-080-busy-tuesday` · Est: 8.5 h · Owner: coordinator Claude / Kudrat
Depends on: WO-010 (schemas, geo, harbor), WO-011 (sim, behaviors, toggles, `track_hash`), WO-012 (`extract_context`), WO-020 (`plan_views_from_scenario`), WO-021 (`HailSimulator`), WO-022 (runner v1). The rogue's `HANDED_OFF` exit needs WO-033 runner v2 on main (in flight: `origin/wo/033a-investigation-core`; see the gated test below). Wave A.
Merge split (critical path: WO-081 and WO-083 need only part A):
- **Part A (~4 h, merge first):** schema additions, `JobType`, `vessel_transit` + AIS feed, generator, `scenarios/BT-01.yaml`, `grading.py`; tests `test_vessel_transit.py`, `test_generate.py`, `-k false_alarm_rate`, `-k no_show`, rules §2.4/§2.5.
- **Part B (~4.5 h):** runner tick skipping, `test_tick_skipping.py`, the four directive-clause tests, `-k reacquire_precision`, `-k ais_feature`, rules §2.1/§2.2/§2.6.

## Goal
A busy working day exists as a committed, regenerable scenario. `scenarios/BT-01.yaml` holds 40 filed jobs, 3 AIS-reporting vessel transits on the surface, biologic false alarms at the configured rate, and one unfiled contact that uses S05's definition. A deterministic generator writes it: `python -m soundings.sim.generate --jobs 40 --vessels 3 --seed 7 --out scenarios/BT-01.yaml`. The sim gains a `vessel_transit` surface behavior. It is never detected by the subsurface model and is reported only on an AIS-style feed (`SimRun.ais_tracks`), which WO-083 reads. The runner gains an opt-in `skip_idle_ticks` mode that does not change outcomes, so BT-01 runs headless in under 60 s. Soundings still only clears at ask or hands to a person. Nothing here plans a route or commands a vessel.

## Scope
In:
- **Schema (additive; every catalog file still validates unchanged)**
  - `ScenarioId` also accepts `BT-\d{2}`.
  - `ScenarioContact` adds:
    - `start_tick: NonNegInt = 0`: the contact enters the water. It must be < `duration_ticks`.
    - `end_tick: NonNegInt | None = None`: the contact leaves the water and its path ends. It must be > `start_tick` and ≤ `duration_ticks`.
    - `hypothesis: Hypothesis | None = None`: overrides `Scenario.ground_truth` for this contact (BT-01 mixes FILED_ON_PLAN jobs with an UNAUTHORIZED_UUV).
  - `JobType` (`soundings/schemas/enums.py`, `_VocabEnum`): `survey`, `hull_cleaning`, `pile_inspection`, `dredge`, `other` (`other` is never generated; it is WO-081's form default). `RegistryPlan.job_type: JobType | None = None`; the generator sets it on every BT-01 plan.
  - `BehaviorType.VESSEL_TRANSIT = "vessel_transit"`. `BehaviorSpec` validates `vessel_transit`: it needs `waypoint` (the exit point), and `speed_mps` must be in [3, 6]. Every other field stays None, and `start.depth_m` must be 0.
  - `Expected.contacts: tuple[ExpectedContact, ...] = ()` with `ExpectedContact(id: TruthContactId, terminal_state: LadderState, max_interruptions: NonNegInt | None = None)`. A validator checks that every id names a scenario contact.
- **Sim (`soundings/sim/behaviors.py`, `run.py`, `types.py`, `hashing.py`, new `ais.py`)**
  - `VesselTransit(waypoints: tuple[Point, ...], speed_mps)`: constant speed along the legs at depth 0, and it stops at the last waypoint. `step_vessel_transit` is pure and draws nothing.
  - `behavior_params` maps `VESSEL_TRANSIT` from the start point to `waypoint`.
  - In `run_scenario`:
    - Each entry's first tick is `contact.start_tick` (surge contacts keep `at_tick`). The path length is `min(end_tick or n_ticks, n_ticks) - start_tick`. The hypothesis is `contact.hypothesis or scenario.ground_truth`.
    - Vessel contacts get kinematic and acoustic streams like every other contact, so spawn order is unchanged, and they get a `truth_paths` entry under their id (`ais-01`...). They skip `_detect_contact` entirely: no readings, no observed id, no `contact_truth` row.
  - `SimRun.ais_tracks: dict[str, tuple[AisReport, ...]]` (default empty) with `AisReport(ais_id, tick, t_s, lat, lon, sog_mps, cog_deg)`. These are truthful reports every `ais.report_interval_s` (new `ais:` block in `config/sim.yaml`, loaded by `soundings.sim.ais.load_ais_config`; default 10 s, labeled a stand-in). Floats are rounded to 6 dp.
  - `track_hash_payload` adds the key `ais_tracks` only when the dict is non-empty, so S01..S08 hashes stay byte-identical.
- **Generator (`soundings/sim/generate.py`, pure; `main()` is the CLI)**
  - `generate_busy_day(*, seed, jobs=40, vessels=3, no_shows=1, rogue=True, hours=8, tick_s=5.0, hour_of_day=7) -> Scenario`.
    - The only randomness is `numpy.random.default_rng(seed)`, drawn in a fixed order: the job-type sequence, then per job its start, duration and position, then the vessels.
    - The mix comes from `JOB_TYPES` (the four generated `soundings.schemas.enums.JobType` members), cycled in a seeded shuffle. Each type has a zone, a window range and a behavior:

      | Type | Zone | Window | Behavior |
      |---|---|---|---|
      | survey | `inner_basin_research` | 2–4 h | lawnmower, 1.0 m/s, 8 m |
      | hull_cleaning | a berth in `fuel_berths` or `pier_400_approach` | 1–2 h | stationary, jitter 0.2, 3 m, breathing |
      | pile_inspection | `vincent_thomas_bridge`, at `vtb_piling_west`/`east` | 1–3 h | orbit, radius 15 m, 0.3 m/s, 6 m |
      | dredge | `main_channel` | 4–6 h | lawnmower, 0.5 m/s, lane 30 m, 10 m, low tonal |

    - Starts are uniform in [0, 2.5 h], clamped so every window ends ≤ `duration_ticks`, and quantized to 60 s.
  - Plan and job details:
    - Each job gets a plan `P-BT01-NNN` with modem `MOD-7NNN`, an operator "<name> (demo)", and `contact` "sim-phone-<type>-NN".
    - The plan window is the in-water interval padded by `WINDOW_PAD_S = 300` on both sides, clamped to the run. The pad covers a re-issued id hailed just after the job leaves the water. Plan watch already drops a watch when its track drops, so the pad cannot cause a LOST_VEHICLE.
    - Contacts are `job-NN`, filed, and reply to hails.
    - `no_shows` plans (the last plan ids) have no contact: the operator filed but never entered the water. That makes 40 plans and 39 filed contacts. See the open question.
  - Positions:
    - Every job start lies within 0.6 × `range_m` of its nearest sensor node, so it is heard (WO-011's coverage rule).
    - Every job stays ≥ 250 m from the rogue's straight-line path (re-acquisition gate 75 m + 0.15 m/s × 100 s, plus margin), so re-acquisition cannot hand a plan lineage to the rogue.
  - The rogue `rogue-1` is S05's contact read from `scenarios/S05-unfiled-closing-fuel-berth.yaml` and not copied by hand: same start, `transit` to `fuel_berth_2` at 1.0 m/s, depth 6, tonal mid, unfiled, no reply, `hypothesis: UNAUTHORIZED_UUV`. Its `start_tick` is the earliest tick of peak job concurrency.
  - Vessels `ais-01`..`ais-03` run the length of `main_channel` (either direction, seeded) at 3–6 m/s, staggered across the day. They are filed false, reply false, and have no plan.
  - `expected`: `terminal_state: HANDED_OFF` (the rogue), plus `contacts` entries: 39 × `{job-NN, CLEARED_AT_ASK, max_interruptions: 0}` and `{rogue-1, HANDED_OFF}`.
  - `scenario_yaml(scenario) -> str`: a stable key order and a header comment naming the command that wrote it. `--check` regenerates the file and diffs it against the committed one.
- **Resolver**: `scenario_path("BT-01")` → `scenarios/BT-01.yaml`. `CATALOG_GLOB` stays `S[0-9][0-9]-*.yaml`, so the eval catalog and its 70% claim are untouched. Two edits to existing tests, and only these: `tests/test_scenario_catalog.py::test_every_scenario_file_validates_via_resolver` derives refs as `path.name[:3]` (use the file's `id`), and `tests/test_schemas.py` asserts `len(BehaviorType) == 7` (becomes 8).
- **Runner tick skipping (`soundings/runner.py`)**
  - `ScenarioRunner(..., skip_idle_ticks: bool = False)` is a constructor kwarg, added in WO-022's signature block. Default False keeps every catalog log as it is.
  - The rest of the code lives in a new block `# --- tick skipping (WO-080) ---`. The only edits inside WO-022's block are `_V1Run.execute` calling `next_wake_tick` and `step` iterating an active-set index.
  - `_V1Run.next_wake_tick(tick) -> int` returns the minimum of these future ticks:
    - any contact's `first_tick`, its evidence-cadence tick (`(t - first_tick) % evidence_every_ticks == 0`) while it is tracked, and its `dropped_at`;
    - the WATCHING readiness tick (`ticks[watch_min_points-1]`);
    - the head of `events`;
    - every watch's `next_tick`;
    - `n_ticks`.
  - Ticks in between run no code. `clock.tick` jumps.
  - `step` iterates only the contacts whose `[first_tick, dropped_at]` covers the tick (a sorted index), never all ids.
  - If profiling shows it is needed, a cleared contact under plan watch extracts at its re-hail ticks instead of every cadence. This is kept only if the equality test below stays green.
  - `RunResult.ticks_stepped: int` (new, defaulted) proves that skipping happened.
  - Correct by default for later runners: while any case is in a state past ASKING that is not terminal (INVESTIGATING, AWAITING_APPROVAL, ADVISING_SLOW, HOLDING, WATCH_AFTER_HANDOFF), `next_wake_tick` returns `tick + 1` (`_wake_every_tick()`). WO-033/WO-043 need no change to stay correct; they may narrow this with their own scheduled ticks for speed, only with `test_tick_skipping.py` green.
- **Grading helper** `soundings/sim/grading.py` (reads ground truth, so tests and eval only; the runner never imports it):
  - `lineages(result) -> dict[gt_id, tuple[CaseResult, ...]]`, with cases in open order;
  - `lineage_terminal(cases) -> LadderState` (the latest-opened case's state);
  - `check_expected_contacts(result, scenario) -> list[str]` (empty means pass). A filed lineage passes only if every one of its cases is CLEARED_AT_ASK.
- **AIS feature (first item on the cut line)**: `extract_context(..., ais: Mapping[str, Sequence[AisReport]] | None = None)`. With a feed, `ais_surface_correlation` = 1.0 if any report lies within 50 m and 10 s of the last reading, else 0.0, as a fact with `source="sim:ais"`. With no feed it stays `unknown` (`no_ais_feed`), so catalog evidence hashes are unchanged.

Out: conflict detection (WO-081), slot proposals (WO-082), separation advisories (WO-083), Kalman tracks (WO-084), triage (WO-085), UI (WO-086). Any vessel instruction, route, or speed command. Changes to `config/ladder.yaml` or detection constants.

## Spec references
`docs/SPEC-SIM.md §2` (detection, false alarms, track timeout), `§3` (S05), `§4` (scenario file); `docs/SPEC-INVESTIGATION.md §2` (`ais_surface_correlation`), `§5` (`ais_surface_correlate`); `docs/SPEC-LADDER.md §3`; `docs/WORKFLOW.md §8`; WO-022 coordinator addenda (interruption definition, plan watch).

## Files expected
- `tests/test_scenario_catalog.py`, `tests/test_schemas.py` (the two named edits only); `docs/STATE.md` (WO-080 row only); `docs/log/DECISIONS.md` (rows for BT-01 counts, `WINDOW_PAD_S`, the AIS stand-in interval, and the tick_s choice)
- `soundings/schemas/scenario.py`, `soundings/schemas/enums.py`, `soundings/sim/{behaviors,run,types,hashing,ais,generate,grading,scenario}.py`, `soundings/runner.py` (WO-080 block plus the two named edits), `soundings/investigation/features.py` (`extract_context` kwarg only), `config/sim.yaml` (`ais:` block)
- `scenarios/BT-01.yaml` (generated, committed)
- `tests/test_vessel_transit.py`, `tests/test_generate.py`, `tests/test_tick_skipping.py`, `tests/test_busy_tuesday.py`, `tests/test_busy_tuesday_rules.py`, `tests/fixtures/sim_hashes_pre_wo080.json` (S01..S08 × seeds 1, 2, 3, captured on main before any change)

## Exit tests (must all pass with `MODEL_MODE=replay`, no network)
From the owner directive (verbatim):
- [ ] 39 filed contacts end CLEARED_AT_ASK with 0 interruptions; the rogue ends HANDED_OFF; two runs at seed 7 are byte-identical; runtime < 60 s at 1x tick skipping.

Each clause as a command:
- [ ] `uv run pytest tests/test_busy_tuesday.py -k filed_cleared`. BT-01 through `ScenarioRunner(..., skip_idle_ticks=True)`: `check_expected_contacts` reports nothing for the 39 `job-NN` lineages. Every case of a filed lineage is CLEARED_AT_ASK; none is LOST_VEHICLE, INVESTIGATING or HANDED_OFF. No filed case has a `recommendations` entry or a transition past ASKING. Under the one WO-022 interruption definition, that means 0 interruptions. Proves the first clause, and that the registry holds all 40 plans (`len(plan_views_from_scenario(bt01)) == 40`) and that the hail answers for every filed contact.
- [ ] `uv run pytest tests/test_busy_tuesday.py -k rogue_handed_off` (gated: runs once WO-033 runner v2 is on main; before that the same test asserts the v1 end state INVESTIGATING with `investigation_pending=True`, never CLEARED_AT_ASK). The `rogue-1` lineage terminal is HANDED_OFF, and no case of it is CLEARED_AT_ASK or RESOLVED_*. Proves the second clause.
- [ ] Determinism: `for i in 1 2; do PYTHONHASHSEED=$i uv run python -m soundings.runner --scenario BT-01 --seed 7 > /tmp/bt$i.txt; done; diff /tmp/bt1.txt /tmp/bt2.txt` prints nothing. `pytest tests/test_busy_tuesday.py -k two_runs` asserts equal `transition_log_hash`, `evidence.table_hash()` and `track_hash` in process. Proves the third clause (CLAUDE.md §2.4).
- [ ] `uv run pytest tests/test_busy_tuesday.py -k runtime`: sim plus runner for BT-01 with `skip_idle_ticks=True` and no UI pacing (the headless 1× path) completes in < 60 s of `time.perf_counter` in the test harness (the sim itself reads no clock). It also asserts `ticks_stepped < n_ticks`. Proves the fourth clause. The PR records the cProfile top 10 before and after.

Unit:
- [ ] `uv run pytest tests/test_vessel_transit.py`: constant speed along the legs, depth 0, stops at the last waypoint. A spec with speed 2.9 or 6.1, depth > 0, or no waypoint is rejected. A vessel produces a `truth_paths` entry and `ais_tracks` reports at the interval, and contributes zero detections, observed ids or `contact_truth` rows. Proves "no subsurface detection".
- [ ] `uv run pytest tests/test_vessel_transit.py -k schema_additive`: every S01..S08 and smoke file validates unchanged, and `tests/fixtures/sim_hashes_pre_wo080.json` equals today's `track_hash` for S01..S08 × seeds 1, 2, 3. `start_tick`/`end_tick` bounds are enforced, and the `hypothesis` override reaches `contact_truth`. Proves the additions change nothing existing.
- [ ] `uv run pytest tests/test_generate.py`:
  - same seed gives the same `scenario_yaml` bytes, and seed 8 differs;
  - 40 plans, 39 jobs, 3 vessels, 1 rogue;
  - each window lies inside its type's hour range plus the pads;
  - each job starts inside its zone and within 0.6 × node range;
  - every job is ≥ 250 m from the rogue's path;
  - peak concurrency is ≥ 30 jobs in the water;
  - the rogue's fields equal S05's `gt-1`;
  - `--check` passes on the committed file.
- [ ] `uv run pytest tests/test_busy_tuesday.py -k false_alarm_rate`: the count of `fa-*` truth ids is within 3σ of Poisson(4 nodes × `false_alarms_per_node_hour` × 8 h). Proves "biologic false alarms at the configured rate" with no new constant.

Integration:
- [ ] `uv run pytest tests/test_tick_skipping.py`: S01..S08 at seeds 1, 2, 3, plus a BT-01 truncated to 1,800 ticks (`generate_busy_day(..., hours=2.5)`), each run with `skip_idle_ticks` False and True. The results have identical `transition_log_text`, `cases` and `notices`. `-m slow` adds the full BT-01. Proves that skipping never changes an outcome.
- [ ] `uv run pytest tests/test_busy_tuesday.py -k reacquire_precision`: every carry-over link in BT-01 joins two observed ids with the same `gt_id`, and no `job-NN` plan id appears in any `rogue-1` case's evidence. Proves that a crowded harbor cannot launder the rogue through a filed lineage.
- [ ] `uv run pytest tests/test_busy_tuesday.py -k no_show`: the no-show plan opens no case and is named in no `LostVehicleNotice`. `RegistryStore.seed_from_scenario(bt01)` in a tmp DB accepts all 40 plans, and `--plans db` gives the same `transition_log_hash`.
- [ ] `uv run pytest tests/test_busy_tuesday.py -k ais_feature` (cut-line item 1): with the feed, a BT-01 reading under a vessel gets `ais_surface_correlation=1.0`. Catalog `evidence.table_hash()` is unchanged.

Guard / rule proof (`tests/test_busy_tuesday_rules.py`):
- [ ] §2.1 and the directive's product line: an AST and text scan of `generate.py`, `ais.py` and the WO-080 runner block finds no function, field or string that sends anything to a vessel or contact. The only output types are `Scenario`, `SimRun` and `RunResult`. No `LadderState` beyond HANDED_OFF is reachable.
- [ ] §2.2: `generate.py`, `grading.py` and the skip block import nothing from `soundings.llm`. `runner.py` never imports `soundings.sim.grading` or reads `truth_paths`/`ais_tracks` in the decision path; `ais` reaches the evidence table only through `extract_context`.
- [ ] §2.4: an AST scan of `generate.py` and `ais.py` finds no `time`, `datetime.now` or module-level RNG. The generator runs twice under `PYTHONHASHSEED` 0 and 1 with an empty `cmp` of the YAML.
- [ ] §2.5: `grep -iEf config/denylist.txt soundings/sim/generate.py soundings/sim/ais.py soundings/sim/grading.py scenarios/BT-01.yaml tests/test_busy_tuesday*.py tests/test_vessel_transit.py tests/test_generate.py tests/test_tick_skipping.py` prints nothing.
- [ ] §2.6: every BT-01 case's transitions cite row ids present in `result.evidence` (WO-022's citation check reused over BT-01).
- [ ] `make check` green.

## Interfaces
Exposed by WO-080:
- `soundings.schemas.enums.BehaviorType.VESSEL_TRANSIT`, `soundings.schemas.enums.JobType`; `soundings.schemas.scenario.RegistryPlan.job_type`, `ScenarioContact.start_tick`, `.end_tick`, `.hypothesis`; `Expected.contacts`, `ExpectedContact`; `ScenarioId` (adds `BT-NN`)
- `soundings.sim.behaviors.VesselTransit`, `.step_vessel_transit`; `soundings.sim.types.AisReport`, `SimRun.ais_tracks`; `soundings.sim.ais.AisConfig`, `.load_ais_config`
- `soundings.sim.generate.JOB_TYPES`, `.WINDOW_PAD_S`, `.generate_busy_day`, `.scenario_yaml`, `.main`; CLI `python -m soundings.sim.generate --jobs --vessels --seed --out [--no-shows] [--hours] [--tick-s] [--check]`
- `soundings.sim.grading.lineages`, `.lineage_terminal`, `.check_expected_contacts`
- `soundings.runner.ScenarioRunner(..., skip_idle_ticks=False)`, `RunResult.ticks_stepped`, `_V1Run.next_wake_tick`
- `soundings.investigation.features.extract_context(..., ais=None)`
- Files: `scenarios/BT-01.yaml`, `config/sim.yaml` `ais:` block

Consumed (names on main):
- WO-010: `soundings.schemas.scenario.Scenario`, `ScenarioContact`, `BehaviorSpec`, `RegistryPlan`, `PlanWindow`, `Expected`, `Toggle`, `scenario_from_data`, `load_scenario`, `scenario_paths`; `soundings.schemas.enums.Hypothesis`, `LadderState`, `ToggleName`; `soundings.schemas.geo.to_local_m`, `from_local_m`; `soundings.schemas.harbor.load_harbor`, `Harbor.zone`, `Harbor.asset`, `Harbor.nearest_asset`; `soundings.canonical.canonical_json`, `stable_hash`, `round6`
- WO-011: `soundings.sim.run.run_scenario`, `behavior_params`, `truncate_run`; `soundings.sim.behaviors.step`, `simulate_path`, `KinState`, `BehaviorParams`, `make_lawnmower`; `soundings.sim.types.SimRun`, `TruthPoint`, `ContactTruth`; `soundings.sim.hashing.track_hash`, `track_hash_payload`; `soundings.sim.scenario.resolve_scenario`, `scenario_path`, `CATALOG_GLOB`; `soundings.sim.detection.load_detection_params`
- WO-012: `soundings.investigation.features.extract_context`; WO-020: `soundings.registry.store.plan_views_from_scenario`, `RegistryStore.seed_from_scenario`; WO-021: `soundings.challenge.hail.HailSimulator`
- WO-022: `soundings.runner.ScenarioRunner`, `RunResult`, `CaseResult`, `transition_log_text`, `transition_log_hash_of`, `main` (`--scenario BT-01` works through the resolver)
- WO-033 (gated test only; in flight on `origin/wo/033a-investigation-core`, runner v2 not yet on any branch): `ScenarioRunner.run(*, llm_roles=False)`, `RunResult.recommendations`
- Used by: WO-081 (BT-01's 40 plans, timing), WO-083 (`SimRun.ais_tracks`), WO-084, WO-085 (BT-01, `skip_idle_ticks`, `grading`), WO-086, WO-061 addendum

## Cut line
Drop in this order (no exit test is dropped; the ais_feature test goes with item 1):
1. The `ais_surface_correlation` wiring in `extract_context` (WO-083 still reads `SimRun.ais_tracks` directly).
2. Cleared-contact extraction held to re-hail ticks. If 60 s is then missed, the generator's `--tick-s` goes from 5 to 10 for BT-01 only, which is recorded in DECISIONS.md.
3. `scenario_yaml` header comment and `--check` (the generate test compares bytes itself).
4. Seeded vessel direction; all vessels run inbound.

## Model calls
None. Paid run needed? No. The generator, sim and runner make no `soundings.llm` call; runner v2 runs with `llm_roles=False`.
Dependency adds (logged in DECISIONS.md per owner directive): none. numpy, pyyaml and pydantic are already deps.

## Rungs touched
watch (vessel feed, evidence), ask (39 filed jobs clear on hail and registry), hand to a person (the rogue, through WO-033). No slow or hold. Advisories are WO-083's.

## Hard rules exercised
- §2.1 and the product line: the AST/text scan in `test_busy_tuesday_rules.py`; nothing past HANDED_OFF.
- §2.2: the import scans; outcomes come from WO-022's matcher and WO-033's stop rule only.
- §2.4: the seed-7 two-run diff, `-k two_runs`, the generator `cmp`, `test_tick_skipping.py` and the pre-change hash fixture.
- §2.5: the denylist grep.
- §2.6: the BT-01 citation check.

## Done
PR: #33 (part A), part B PR on `wo/080-busy-tuesday-b` · Merged commit: see STATE.md · Test summary: see part B notes · STATE.md updated: yes (WO-080 row)

### Part B done (2026-10-17 07:31 PDT)
- `ScenarioRunner(..., skip_idle_ticks=False)`, `RunResult.ticks_stepped`, `_V1Run.next_wake_tick` (delegates to `_next_wake_tick` in the `# --- tick skipping (WO-080) ---` block), `_ActiveIndex` (step iterates only the contacts whose `[first_tick, dropped_at]` covers the tick), `_wake_every_tick` (any case in INVESTIGATING/ADVISING_SLOW/HOLDING/AWAITING_APPROVAL/WATCH_AFTER_HANDOFF wakes every tick; exact `_V1Run` parks INVESTIGATING because v1 never acts on it, and any subclass is stepped per tick).
- Profiling found the time in event handling, not the per-tick loop. Before: 113 s profiled, with `try_reacquire` building a `LostTrack` for every older track (52 s). One more edit inside WO-022's block skips lost tracks past `reacquire.max_gap_s` before building them; `match_reacquisition` rejects those by the same test, so outcomes are identical. After: 64 s profiled, 33.7 s wall, 4,065 of 5,760 ticks stepped. tick_s stays 5 and needs no fallback. Cut item 2 (re-hail extraction) was not needed.
- Tests: `test_tick_skipping.py` (S01..S08 x seeds 1-3 plus the 2.5 h BT-01, skipped equal to unskipped); `test_busy_tuesday.py` `-k runtime`, `-k two_runs`, `-k no_show` (`--plans db` gives the same hash), `-k ais_feature`, `-k reacquire_precision`; part B rules (§2.1 the skip block only schedules, §2.2 it reads no truth).
- Rogue test ungated: WO-033 runner v2 (`ScenarioRunnerV2`) reached main (#36), so `-k rogue_handed_off` runs v2 with `llm_roles=False`. The lineage terminal is HANDED_OFF, the first case is HANDED_OFF, no case is CLEARED_AT_ASK, and `check_expected_contacts` passes (46.8 s). v2 resolves 3 of the rogue's 8 re-issued ids, parked at fuel_berth_2, as RESOLVED_BENIGN; the WO's "no RESOLVED_*" clause is a strict xfail and an open question for WO-033. `_V2Run` overrides `execute` and does not skip ticks; its INVESTIGATING cases would force per-tick stepping anyway.
- reacquire_precision: the rogue is never laundered. No `registry:` plan row and no lineage plan reach a rogue case, and no carry-over link touches the rogue. Position-only `registry.position:` rows appear while it crosses a dredge plan's main_channel area; DECISIONS 23:44 says those are area matches, not identities. The every-link-same-gt clause is a strict xfail: v1 links co-working jobs that share one survey or dredge box (job-02/job-07, job-17/job-37), and three biologic false alarms to jobs. See the open question.

### Part A done (2026-10-17 07:01 PDT)
- Schema: `JobType` (enums), `BehaviorType.VESSEL_TRANSIT`, `RegistryPlan.job_type`, `ScenarioContact.start_tick/end_tick/hypothesis`, `Expected.contacts`/`ExpectedContact`, `ScenarioId` accepts `BT-NN`; `VESSEL_SPEED_MIN_MPS/MAX_MPS` (3, 6) in `soundings.schemas.scenario`.
- Sim: `VesselTransit`/`step_vessel_transit` (behaviors), `AisReport` + `SimRun.ais_tracks` (types), `soundings.sim.ais` (`AisConfig`, `load_ais_config`, `ais_reports`), `config/sim.yaml` `ais.report_interval_s: 10` (stand-in). A vessel's start state is under way (speed and course set), so its first AIS report has a real `sog_mps`/`cog_deg`. `track_hash_payload` adds `ais_tracks` only when non-empty; `truncate_run` truncates it.
- Generator `soundings.sim.generate` (`JOB_TYPES`, `JOB_SPECS`, `WINDOW_PAD_S`, `generate_busy_day`, `scenario_yaml`, `main`, plus `peak_tick`, `rogue_segment`, `job_clearance_m`); `scenarios/BT-01.yaml` committed (40 plans, 39 jobs, 3 vessels, rogue at the peak tick 1764 with 32 jobs in the water).
- `soundings.sim.grading` (`lineages`, `lineage_terminal`, `lineage_interruptions`, `check_expected_contacts`); `extract_context(..., ais=None)` (`ais_surface_correlation`, source `sim:ais`).
- BT-01 through runner v1 as merged (no skipping): all 39 `job-NN` lineages CLEARED_AT_ASK, 0 interruptions; rogue lineage INVESTIGATING (8 cases, never cleared); ~80-93 s headless (part B adds tick skipping for the < 60 s clause).
- Tests: `test_vessel_transit.py` (28), `test_generate.py` (13), `test_busy_tuesday.py` (6, one shared run), `test_busy_tuesday_rules.py` (9). No dependency added.
- Geometry findings (see DECISIONS): with 0.6 x range and >= 250 m from the rogue path, hull cleaning lands only in `pier_400_approach` and pile inspection only at `vtb_piling_west`; survey clearance is checked at the start (every lawnmower box in `inner_basin_research` reaches its east edge near `fuel_berth_2`), dredge clearance over every leg. `BEHAVIOR_TYPES` stays the seven subsurface names (test pins it). Deferred to part B: `--plans db` hash equality (needs the fast runner), `-k ais_feature`, `-k reacquire_precision` (v1 shows two job-to-job carry-over links, job-02/job-07 and job-17/job-37, to address there).

### Part A done (2026-10-17 07:01 PDT)
- Schema: `JobType` (enums), `BehaviorType.VESSEL_TRANSIT`, `RegistryPlan.job_type`, `ScenarioContact.start_tick/end_tick/hypothesis`, `Expected.contacts`/`ExpectedContact`, `ScenarioId` accepts `BT-NN`; `VESSEL_SPEED_MIN_MPS/MAX_MPS` (3, 6) in `soundings.schemas.scenario`.
- Sim: `VesselTransit`/`step_vessel_transit` (behaviors), `AisReport` + `SimRun.ais_tracks` (types), `soundings.sim.ais` (`AisConfig`, `load_ais_config`, `ais_reports`), `config/sim.yaml` `ais.report_interval_s: 10` (stand-in). A vessel's start state is under way (speed and course set), so its first AIS report has a real `sog_mps`/`cog_deg`. `track_hash_payload` adds `ais_tracks` only when non-empty; `truncate_run` truncates it.
- Generator `soundings.sim.generate` (`JOB_TYPES`, `JOB_SPECS`, `WINDOW_PAD_S`, `generate_busy_day`, `scenario_yaml`, `main`, plus `peak_tick`, `rogue_segment`, `job_clearance_m`); `scenarios/BT-01.yaml` committed (40 plans, 39 jobs, 3 vessels, rogue at the peak tick 1764 with 32 jobs in the water).
- `soundings.sim.grading` (`lineages`, `lineage_terminal`, `lineage_interruptions`, `check_expected_contacts`); `extract_context(..., ais=None)` (`ais_surface_correlation`, source `sim:ais`).
- BT-01 through runner v1 as merged (no skipping): all 39 `job-NN` lineages CLEARED_AT_ASK, 0 interruptions; rogue lineage INVESTIGATING (8 cases, never cleared); ~80-93 s headless (part B adds tick skipping for the < 60 s clause).
- Tests: `test_vessel_transit.py` (28), `test_generate.py` (13), `test_busy_tuesday.py` (6, one shared run), `test_busy_tuesday_rules.py` (9). No dependency added.
- Geometry findings (see DECISIONS): with 0.6 x range and >= 250 m from the rogue path, hull cleaning lands only in `pier_400_approach` and pile inspection only at `vtb_piling_west`; survey clearance is checked at the start (every lawnmower box in `inner_basin_research` reaches its east edge near `fuel_berth_2`), dredge clearance over every leg. `BEHAVIOR_TYPES` stays the seven subsurface names (test pins it). Deferred to part B: `--plans db` hash equality (needs the fast runner), `-k ais_feature`, `-k reacquire_precision` (v1 shows two job-to-job carry-over links, job-02/job-07 and job-17/job-37, to address there).

## Notes for downstream
- WO-033 and WO-043: no obligation. Runner states past ASKING force per-tick stepping, so skipping stays outcome-identical. If BT-01 runtime under v3 misses 60 s because the rogue sits in WATCH_AFTER_HANDOFF for hours, WO-043 returns its update ticks from `next_wake_tick` and extends `tests/test_tick_skipping.py`; skipped must equal unskipped.
- WO-081: `RegistryPlan.job_type` and `soundings.schemas.enums.JobType` (incl. `other`).
- WO-083: vessel positions and velocities come from `SimRun.ais_tracks` (`AisReport.sog_mps`, `.cog_deg`), never from `truth_paths`. The in-water filed jobs are BT-01's `job-NN` cases at CLEARED_AT_ASK.
- WO-085: build the surge variant in memory with `bt01.model_copy(update={"toggles": (Toggle(name=ToggleName.CONTACT_SURGE, n=25, at_tick=...),)})`; the committed BT-01 stays surge-free.
