# WO-095 — bt01-window-flex

Phase: 7 · Branch: `wo/095-bt01-window-flex` · Worktree: `../soundings-worktrees/wo-095-bt01-window-flex` · Est: 2 h · Owner: coordinator Claude / Kudrat
Depends on: WO-080 part A (generator, `scenarios/BT-01.yaml`, PR #33), WO-081 (conflicts, `plan_views_from_scenario`), WO-082 (CP-SAT slot proposer, start bounds `earliest_start_at`/`latest_start_at`). Opened by the coordinator (DECISIONS 2026-10-17 12:00 PDT, "follow-up WO-095"). Does not touch `soundings/runner.py` (WO-080 part B, PR #55, is open against it).

## Goal
BT-01's filed jobs carry real filing-window flexibility, so a slot proposal on the busy day has room to move a plan: each plan keeps its filed window (the padded in-water interval, unchanged) and gains start bounds per job type, as declared demo values in one place. The real BT-01 is re-run through `propose_slots`, and the test that was relaxed on 2026-10-17 asserts again what is true. Nothing moves a vehicle; a proposal still changes nothing until an operator accepts it (CLAUDE.md §8 A9).

## Scope
In:
- **Schema (additive)**: `PlanWindow.earliest_start_tick: NonNegInt | None = None` and `latest_start_tick: NonNegInt | None = None`, validated `earliest <= start_tick <= latest`. `Scenario` checks that a plan moved to its latest start still ends by `duration_ticks`. Every catalog file validates unchanged (no bounds, defaults omitted from YAML).
- **Registry**: `plans_from_scenario` maps the ticks to `earliest_start_at`/`latest_start_at` (WO-082's fields); `plan_views_from_scenario` passes them through, so the view and a seeded store agree.
- **Generator**: `WINDOW_FLEX_MIN` (module constant with a source comment, demo values): survey ±90 min, hull_cleaning ±120, pile_inspection ±180, dredge ±45. `flex_window()` sets `earliest = max(start - flex, 0)` and `latest = min(start + flex, n_ticks - (end - start))` (clipped to the scenario day). Placement, starts, durations, vessels, false alarms, the rogue and the no-show are untouched: same seed, same draws, same code path. `scenarios/BT-01.yaml` regenerated with the CLI; `--check` passes.
- **Tests**: `tests/test_bt01_window_flex.py` (new); `tests/test_slots_integration.py` (the relaxed BT-01 test rewritten, two tests added).

Out: `config/registry.yaml` capacities (owner decision, see Finding), the proposer's model (`soundings/registry/slots.py`), `soundings/runner.py`, the ask-rung matcher, any threshold.

Skipped (scope item 5): the `JobTypeName` → `soundings.schemas.enums.JobType` swap in `soundings/registry/models.py`. It is not a pure alias swap: `JobType` is a StrEnum, so `PlanDraft`/`PlanView.job_type` would change runtime type from `str` to an enum (python-mode `model_dump()` output, YAML dumps of views, and the OpenAPI schema all move). Left for a WO that can own that change.

## Finding (scope item 2): capacity 2 binds on every BT-01 component, whatever the windows
Re-running the real BT-01 at tick 0 (07:00) through `propose_slots` with the config capacity 2:

| component (target zone) | plans in model set | filed work (h) | capacity × (24:00 − 07:00) (slot-h) | status | binding |
|---|---|---|---|---|---|
| inner_basin_research | 20 (10 survey, 10 dredge) | 84.9 | 34 | INFEASIBLE | capacity |
| main_channel | 40 (all) | 123.3 | 34 | INFEASIBLE | capacity |
| pier_400_approach | 20 (10 hull_cleaning, 10 dredge) | 69.7 | 34 | INFEASIBLE | capacity |
| vincent_thomas_bridge | 20 (10 pile_inspection, 10 dredge) | 71.7 | 34 | INFEASIBLE | capacity |

Each component's filed work exceeds the capacity-hours left in the day, so no start-time window can fit it: this is arithmetic, not the windows. Before WO-095 the same components said `capacity` too; the coordinator's 12:00 note (`latest_start` binds at capacity 6) is now what the solver says at capacity 6–8 with the new windows. The 10 main_channel dredges (4–6 h each) are in every component because the channel's 50 m-buffered polygon meets every other zone, and WO-082's single cumulative counts every member against the target zone's capacity. Even with the widest possible windows (earliest = now, latest = 24:00) capacity 4 stays INFEASIBLE.

What-if (test-only, config unchanged): at capacity 16 three of four components come back OPTIMAL with the WO-095 windows (vincent_thomas_bridge: 1 plan shifted, 15 min; pier_400_approach: 2 plans, 60 min; inner_basin_research: 8 plans, 540 min); main_channel (40 plans) still names `latest_start`. Every solve < 0.25 s.

Per the WO text, capacity was not raised. The honest refusal stays on every component at capacity 2, with `capacity` named. Owner options (open question): (a) per-zone capacities in `config/registry.yaml`; (b) the proposer counts only same-zone plans against a zone's capacity (a WO-082 model change; same-zone at capacity 6 is OPTIMAL for the bridge and pier_400_approach); (c) a less crowded busy day (generator density). The demo script's shot 3 runs on S01, where `test_s01_in_water_plan_kept` proves a feasible OPTIMAL proposal is accepted, so its caption stays true.

## Spec references
`docs/work-orders/WO-080-busy-tuesday.md` (generator, `WINDOW_PAD_S`), `docs/work-orders/WO-082-slot-proposal.md` (start bounds, binding constraints), CLAUDE.md §2.4, §8 A9.

## Files expected
- `soundings/schemas/scenario.py`, `soundings/registry/store.py`, `soundings/sim/generate.py`
- `scenarios/BT-01.yaml` (regenerated)
- `tests/test_bt01_window_flex.py`, `tests/test_slots_integration.py`
- `docs/work-orders/WO-095-bt01-window-flex.md`, `docs/log/DECISIONS.md` (one row), `docs/STATE.md` (`| 095 |` row)

## Exit tests (must all pass with `MODEL_MODE=replay`, no network)
- [x] `uv run pytest tests/test_bt01_window_flex.py`: bounds per type and clipped to the day for all 40 plans; with the bounds stripped a fresh generation is byte-identical to PR #33's `BT-01.yaml` (sha256 `668d9f13…a8c0` pinned); the schema rejects bounds that do not bracket the start or run past the day; S01..S08 carry no bounds; view and seeded store agree.
- [x] `uv run python -m soundings.sim.generate --check`: `scenarios/BT-01.yaml: up to date`.
- [ ] (blocked on the owner's capacity decision) the strong assertion on the real BT-01 at config capacity: `"OPTIMAL" in statuses`. Impossible at capacity 2 (Finding). What ships instead in `tests/test_slots_integration.py`:
  - `test_bt01_components_solve_in_time`: every component a proven status, each solve < 2.0 s process time, every INFEASIBLE names its binding constraint, and on the real BT-01 that constraint is `capacity` for all four;
  - `test_bt01_capacity_binds_by_arithmetic`: the table above, without the solver;
  - `test_bt01_slots_with_capacity_what_if`: `"OPTIMAL" in statuses` on the real BT-01 at a test-only capacity 16, the bridge OPTIMAL with a real shift, every shift inside its plan's WO-095 bounds.
- [x] BT-01 clauses unchanged: `tests/test_busy_tuesday.py`, `tests/test_generate.py` (no edit needed: the plan window itself keeps the 5-minute pad, so WO-080's pinned expectations hold), `tests/test_conflicts_integration.py`, `tests/test_separation_bt01.py`, `tests/test_busy_tuesday_rules.py`, `tests/test_catalog_expected.py`; `tests/fixtures/sim_hashes_pre_wo080.json` unchanged. (`tests/test_tick_skipping.py` is on PR #55, not main.)
- [x] Determinism: generator under `PYTHONHASHSEED=0` and `=1` byte-identical; two BT-01 runner runs at seed 7 have the same `transition_log_hash` (hashes in the PR body).

## Model calls
None. No paid run.

## Rungs touched
ask (the registry's plan data the matcher reads; the matcher's windows are unchanged). Slot proposals are advice under A9, not a rung.

## Hard rules exercised
§2.2 (no model in the path: generator and proposer are deterministic code), §2.4 (seeded generator, byte-identical output, `test_only_the_bounds_changed`), §2.6 (the refusal names its binding constraint; the what-if is labeled test-only), §2.1/§2.5 (no vocabulary or state added; `make check` denylist clean).

## Done
PR: see branch PR · Merged commit: · Test summary: · STATE.md updated: yes
