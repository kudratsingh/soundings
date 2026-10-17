# WO-082 — slot-proposal

Phase: 7 · Branch: `wo/082-slot-proposal` · Worktree: `../soundings-worktrees/wo-082-slot-proposal` · Est: 5.5 h · Owner: coordinator Claude / Kudrat
Depends on: WO-081 (`PlanStatus.CONFLICTED`, `IN_FORCE`, `PlanView.conflicts`/`tick_s`, `zone_geometry`, `footprint_of`, `RegistryStore.recompute_conflicts`/`set_conflict_resolution`), WO-020 (store, router, form, `tick_to_datetime`), WO-022 (`ScenarioRunner`), WO-080 part A (BT-01), WO-060 (in flight on `origin/wo/060a-ui-core`: e2e conftest, `make e2e`). Wave C. Branch after WO-081 merges. The pure solver and the hand-built cases can start once WO-081's `PlanView` fields are fixed. They do not need its store code.

## Goal
For a conflicted plan, the registry can compute a slot proposal: start-time shifts, in minutes, that minimize total delay. The constraints are the zone's capacity (config, default 2 concurrent), each plan's earliest and latest start, and no shift for any plan already in the water. The model is OR-Tools CP-SAT, built deterministically and run single-worker with a fixed seed and a 2 s limit. When no slot exists, the proposal says "no feasible slot" and names the binding constraint. A proposal changes nothing. In the form, an operator accepts it, which shifts the plans and clears the conflict, or declines it, which leaves everything as it was. Nothing is sent to any vehicle: an accepted proposal edits registry records, and the operator tells their crew.

## Scope
In:
- `soundings/registry/slots.py` (pure: no I/O, no clock, no store import; `ortools` is imported lazily inside `solve`):
  - `SlotConfig(zone_capacity_default=2, zone_capacity: dict[zone_id, int], slot_step_min=15, latest_start_slack_min=120, horizon_end_local="24:00", random_seed=7, max_time_in_seconds=2.0)`, from a new top-level `slots:` block in `config/registry.yaml` via `load_slot_config()` (`RegistryConfig` untouched).
  - `BindingConstraint` (StrEnum): `capacity`, `latest_start`, `in_water`, `time_limit`.
  - `PinReason` (StrEnum): `in_water`, `not_conflicted`.
  - `PlanShift(plan_id, shift_min, new_start_at, new_end_at, pinned: PinReason | None)`.
  - `SlotProposal(proposal_id, target_plan_id, zone_id, capacity, now, status: Literal["feasible", "infeasible"], optimal: bool, shifts: tuple[PlanShift, ...], total_delay_min, binding_constraint: BindingConstraint | None, detail, inputs_hash, solver_status)` (frozen). `proposal_id = "SP-" + inputs_hash[:12]`.
  - `model_set(target, in_force, harbor, cfg)` builds the plans the model covers. These are every in-force plan whose WO-081 footprint overlaps the target's zone in buffered area and depth, and whose window falls on the target's local day. The set is sorted by `(start_at, plan_id)`.
  - Which plans can move:
    - Movable: conflicted plans with `start_at > now`.
    - Pinned `in_water`: `start_at <= now`.
    - Pinned `not_conflicted`: plans with no conflict, which sit in the model as fixed context.
  - `propose_slots(target_plan_id, plans, *, harbor, now, cfg) -> SlotProposal` builds the model as follows:
    - Each plan i has an integer step `k_i`. Its start is `filed_start_i + k_i·step`, kept within `[earliest_i, latest_i]` (the bounds are snapped to the step grid toward the filed start), and it must end by the horizon.
    - The defaults are `earliest = filed start` and `latest = filed start + latest_start_slack_min`.
    - Pinned plans have `k_i = 0`.
    - Each plan gets an `IntervalVar`, and the plans share one `AddCumulative(demand 1, capacity)`.
    - The delay is `|k_i|·step` (via `AddAbsEquality`; an earliest bound before the filed start allows earlier slots).
    - The objective minimizes `M·Σ|k_i| + Σ(n − rank_i)·|k_i|`, where `rank` follows `(start_at, plan_id)`. The second term only breaks ties. It prefers to move later-filed plans, so the reported optimum is unique.
  - Solver parameters:
    - `num_search_workers = 1` (directive) and `num_workers = 1`;
    - `random_seed = cfg.random_seed`;
    - `max_time_in_seconds = 2.0`;
    - `max_deterministic_time = 2.0` (a reproducible cutoff, in case the wall limit is ever reached);
    - `log_search_progress = False`.
    Variables and constraints are added in sorted plan order only.
  - Status mapping:
    - `OPTIMAL` maps to `feasible`, `optimal=True`.
    - `FEASIBLE` maps to `feasible`, `optimal=False`. The form says "best found in 2 s".
    - `INFEASIBLE` runs `diagnose`.
    - `UNKNOWN` maps to `infeasible`, `time_limit`.
  - `diagnose(...)` is deterministic and names the first constraint that holds, in this order:
    1. `in_water`: the pinned in-water plans alone exceed capacity at some minute. The detail names those plans and the minute.
    2. `latest_start`: relaxing the latest start of one movable plan, tried in sorted order, makes the model feasible. The detail names the plan and its latest start (for example `P-C latest_start 10:30`). If that fails but relaxing all of them works, the detail names all of them.
    3. `capacity`: everything else.
    The detail is the user-facing string `no feasible slot: <binding> (<detail>)`.
  - Already within capacity: if no shift is needed, the proposal is `feasible` with `total_delay_min=0`, and the detail says "within zone capacity; accept to confirm shared slot".
  - `inputs_hash = stable_hash(canonical_json({model set views, now, capacity, cfg}))`. Floats and datetimes are rendered as ISO / 6 dp.
- `soundings/registry/store.py` gains:
  - the table `registry_proposal(proposal_id PK, target_plan_id, status ∈ {pending, accepted, declined, stale}, payload_json, inputs_hash, created_at, resolved_at)`;
  - `save_proposal(p)`, which is idempotent: the same id upserts the same payload;
  - `get_proposal(id)`;
  - `accept_proposal(id, *, at)`. In one session it does the following:
    1. Re-runs `model_set` and checks `inputs_hash`. On a mismatch it marks the proposal `stale`, raises `ProposalStale`, and changes nothing.
    2. Rejects anything not `pending` or not `feasible` with `ProposalNotAcceptable`.
    3. Shifts `start_at`/`end_at` by `shift_min`, and `start_tick`/`end_tick` by `round(shift_min·60 / tick_s)`. A plan that has ticks but no `tick_s` makes the whole accept fail (`no_tick_scale`) with nothing written.
    4. Calls `recompute_conflicts(model set)`, then `set_conflict_resolution(model set, proposal_id)`, so that every remaining overlap among the set (within capacity by construction) is marked resolved.
    5. Marks the proposal `accepted`.
  - `decline_proposal(id, *, at)` sets only the proposal's status.
- `soundings/registry/router.py`:
  - `POST /api/plans/{id}/proposal?now=<iso, optional; default store.now()>` returns 200 with the `SlotProposal`. It returns 404 for an unknown plan and 409 `not_conflicted`.
  - `POST /api/plans/{id}/proposal/accept` takes `{proposal_id}` and returns 200 with `{proposal, plans}`. It returns 409 `stale_proposal` or `not_pending`, and 422 `infeasible` or `no_tick_scale`.
  - `POST /api/plans/{id}/proposal/decline` takes `{proposal_id}` and returns 200.
  - `GET /api/plans/{id}/proposal` returns the latest pending proposal or 404.
- `registry.html`: conflicted rows get a "Propose slot" button. The proposal panel lists `P-B 09:00-11:00 → 11:00-13:00 (+120 min)` for each moved plan, marks pinned plans "in the water, not moved", and shows the total delay with Accept and Decline buttons. An infeasible proposal shows `no feasible slot: latest_start (P-C latest_start 10:30)` and has no Accept button.
- `PlanCreate`/`PlanView` gain optional `earliest_start_at` and `latest_start_at` (WO-081 owns the model file, so add them after its fields). There is a new reject reason `START_BOUNDS_INVALID` (earliest > latest, or a filed start outside them).

Out: auto-accepting anything; moving a plan that is not conflicted; cross-day slots; notifying the operator's crew (the human does that); audit-log rows for accept/decline (add them only if WO-042's `AuditLog` is on main and time allows; it is not an exit test); WO-061's QR panel rendering of the proposal (WO-061 addendum).

## Hand-built case (zone `inner_basin_research`, 2026-10-17, offset −07:00, `now` 08:00, capacity 2, step 15 min)
| plan | filed window | earliest | latest | conflicted | expected shift |
|---|---|---|---|---|---|
| P-F | 07:30-09:30 | — | — | yes | 0 (pinned `in_water`) |
| P-A | 09:00-11:00 | 09:00 | 09:00 | yes | 0 |
| P-B | 09:00-11:00 | 09:00 | 11:00 | yes | +120 → 11:00-13:00 |
| P-C | 09:30-11:30 | 09:30 | 11:30 | yes | +90 → 11:00-13:00 |
| P-D | 10:00-11:00 | 10:00 | 10:00 | yes | 0 |
| P-E | 13:00-14:00 | 13:00 | 15:00 | no | 0 (pinned `not_conflicted`) |

This was checked by brute force over the 15-minute grid. The unique optimum has `total_delay_min = 210`, and the next best is 225. The infeasible variants come from the same table:
- (i) P-C latest 10:30 gives `latest_start` with detail `P-C latest_start 10:30`.
- (ii) `now` 09:15 with capacity 1 gives `in_water` (P-F and P-A both started and overlap 09:00-09:30).
- (iii) Three 600-min plans in `fuel_berths`, filed 09:00, with capacity 1 give `capacity` (1800 min cannot fit before 24:00).

## Spec references
Owner directive 2026-10-17 06:05 PDT (WO-082 text) · `docs/work-orders/WO-081-conflict-detection.md` (conflict semantics, `IN_FORCE`) · `WO-020` Done section · `CLAUDE.md §2.1, §2.2, §2.4, §2.5, §3` · `docs/WORKFLOW.md §8`.

## Files expected
- `soundings/registry/slots.py` (new), `store.py`, `router.py`, `models.py` (bounds fields), `__init__.py`, `static/registry.html`
- `config/registry.yaml` (`slots:` block)
- `tests/test_slots.py`, `tests/test_slots_api.py`, `tests/test_slots_integration.py`, `tests/e2e/test_registry_proposal.py`
- `pyproject.toml` (the `ortools` line with a trailing `# WO-082` comment, and its own `[[tool.mypy.overrides]]` table for `ortools.*` marked `# WO-082`), `uv.lock`, `docs/log/DECISIONS.md` (dependency row plus one for `slots:` defaults), `docs/STATE.md` (WO-082 row only)
- Shared files only inside marked blocks placed after WO-081's: `router.py` `# --- WO-082: slot proposals ---`, `registry.html` `<!-- WO-082: slot proposals -->`, `store.py` `# --- WO-082: proposals ---`, `models.py` `# --- WO-082: start bounds ---`.

## Exit tests (must all pass with `MODEL_MODE=replay`, no network)
From the owner directive (verbatim):
- [ ] hand-built 6-plan case has a known optimum and the solver returns it; identical proposals across two runs; an accepted proposal updates the plan and clears the conflict; a declined one leaves everything unchanged; infeasible -> explicit "no feasible slot" with the binding constraint named.

Each clause as a command:
- [ ] (directive, verbatim) hand-built 6-plan case has a known optimum and the solver returns it. `uv run pytest tests/test_slots.py::test_six_plan_optimum` proves this. The shifts equal the table, `total_delay_min == 210`, `optimal is True`, and `solver_status == "OPTIMAL"`. A `brute_force_reference` helper in the test, which uses pure Python and no ortools, recomputes 210 so that the table cannot drift from the config.
- [ ] (directive, verbatim) identical proposals across two runs. `uv run pytest tests/test_slots.py -k deterministic` proves this. The 6-plan case and every BT-01 component are solved twice in-process, and once each in subprocesses under `PYTHONHASHSEED=0` and `=1`. All runs give byte-identical canonical JSON (sha256 equal), including `proposal_id`. Shuffling the input plan list changes nothing.
- [ ] (directive, verbatim) an accepted proposal updates the plan and clears the conflict. `uv run pytest tests/test_slots_api.py::test_accept_updates_and_clears` proves this. After POST proposal and then accept, P-B reads 11:00-13:00 with its ticks shifted by `120·60/tick_s` and P-C reads 11:00-13:00. Every plan in the set has status `active` and no unresolved conflict. P-F's row is byte-identical to before.
- [ ] (directive, verbatim) a declined one leaves everything unchanged. `uv run pytest tests/test_slots_api.py::test_decline_changes_nothing` proves this. Canonical JSON of `store.list("all")` with conflicts is equal at three points: before the proposal, after POST proposal, and after decline. Only the proposal row's status differs.
- [ ] (directive, verbatim) infeasible -> explicit "no feasible slot" with the binding constraint named. `uv run pytest tests/test_slots.py::test_infeasible_names_binding` proves this. Variants (i), (ii) and (iii) give `status == "infeasible"`, `binding_constraint` equal to `latest_start`, `in_water` and `capacity` respectively, and a `detail` that starts `no feasible slot:`. The API returns 422 `infeasible` on an accept of such a proposal.
- [ ] `uv run pytest tests/test_slots.py` covers the remaining cases:
  - an in-water plan is never shifted (hypothesis over random `now`);
  - capacity comes from `zone_capacity[zone]`, then from the default (read from config, never a literal);
  - `solve` sets `num_search_workers == 1` or `num_workers == 1` and the seed (both asserted on the built parameters);
  - a within-capacity pair gives total delay 0 with the "within zone capacity" detail;
  - bounds snap to the step grid;
  - `START_BOUNDS_INVALID` rejects.
- [ ] `uv run pytest tests/test_slots_api.py` covers the API:
  - nothing changes until accept (the compute route writes only `registry_proposal`, checked by a table-by-table row count and a hash);
  - a stale proposal returns 409 and changes nothing (a plan is filed between compute and accept);
  - a second accept returns 409 `not_pending`;
  - an unknown plan returns 404;
  - a not-conflicted plan returns 409.
- [ ] integration over a real scenario: `uv run pytest tests/test_slots_integration.py::test_s01_in_water_plan_kept` proves an accepted proposal never disturbs a vehicle already working. It seeds S01, files a second plan in S01's zone that starts at tick 300 (another modem), and uses capacity 1 via a test config. The proposal at `now` = tick 0 pins S01's plan (`in_water`) and shifts only the new plan. After the accept, `ScenarioRunner("S01", plans=store).run()` still ends `CLEARED_AT_ASK` with 0 interruptions. The BT-01 test proposes for every conflicted component at tick 0, and each solve finishes with `solver_status == "OPTIMAL"` in < 2 s.
- [ ] Manual check: Playwright `SOUNDINGS_E2E_REQUIRED=1 uv run pytest tests/e2e/test_registry_proposal.py` (part of `make e2e`) files two overlapping plans, clicks Propose slot, sees the shift line, clicks Accept, and sees the conflict text gone. It saves a screenshot to `artifacts/registry-proposal.png` for the PR.
- [ ] rule proof §2.2 (no model output becomes a control; a proposal is not an action): `uv run pytest tests/test_slots.py::test_proposal_is_not_control` runs two AST scans:
  - `slots.py` imports no `soundings.registry.store`, `soundings.llm`, `anthropic`, `soundings.challenge` or `soundings.ladder`;
  - `RegistryStore.accept_proposal` is called only from `router.py`'s accept route, so no code path accepts on its own.
- [ ] rule proof §2.4: `test_no_clock` is an AST scan of `slots.py` that finds no `datetime.now`, `time.time` or `random`. `now` is always a parameter.
- [ ] rule proof §2.1/§2.5: run `grep -inEf config/denylist.txt soundings/registry/slots.py soundings/registry/static/registry.html tests/test_slots*.py` from the repo root; it prints nothing. Form labels say "proposed slot", "accept", "decline". `make check` is green.

## Interfaces
Exposed:
- WO-082 `soundings.registry.slots.SlotConfig`, `.load_slot_config`, `.BindingConstraint`, `.PinReason`, `.PlanShift`, `.SlotProposal`, `.model_set`, `.propose_slots`, `.diagnose`
- WO-082 `soundings.registry.store.RegistryStore.save_proposal`, `.get_proposal`, `.accept_proposal`, `.decline_proposal`, `ProposalStale`, `ProposalNotAcceptable`
- WO-082 `soundings.registry.models.PlanView.{earliest_start_at, latest_start_at}`, `RejectReason.START_BOUNDS_INVALID`
- WO-082 routes `POST /api/plans/{plan_id}/proposal`, `POST /api/plans/{plan_id}/proposal/accept`, `POST /api/plans/{plan_id}/proposal/decline`, `GET /api/plans/{plan_id}/proposal`

Consumed:
- WO-081: `soundings.registry.models.{PlanStatus.CONFLICTED, IN_FORCE, ConflictView}`, `soundings.registry.conflicts.{zone_geometry, footprint_of, PlanFootprint}`, `RegistryStore.{recompute_conflicts, set_conflict_resolution, conflicts_of}`, `PlanView.{conflicts, tick_s, job_type}`
- WO-020: `soundings.registry.models.{PlanView, RegistryConfig, load_registry_config, tick_to_datetime, to_utc}`, `soundings.registry.store.RegistryStore.{list, get, now, seed_from_scenario}`, `soundings.registry.router.{router, get_store}`
- WO-010: `soundings.canonical.{canonical_json, stable_hash}`, `soundings.schemas.harbor.Harbor`
- WO-022: `soundings.runner.ScenarioRunner(scenario, plans=...)`. WO-080: the BT-01 scenario file.

## Cut line
Wave rule (STATE.md, owner directive): if Wave C is not merged by 3:00 PM, drop WO-082 whole and report the hours saved (5.5 h). WO-081's conflict flag stays; the WO-061 QR card then shows the conflict line without a proposal (owner to confirm that this meets the addendum).
Drop these first if more than an hour late: (1) `GET /api/plans/{id}/proposal` (the form keeps the compute response); (2) earlier-than-filed starts (keep `earliest = filed start` only, so delays are never negative); (3) the per-plan `latest_start` naming in `diagnose`, which then reports `latest_start` without naming the plan; (4) the e2e screenshot (keep the API tests). Never drop an exit test.

## Model calls
None. Paid run needed? No. CP-SAT is a local deterministic solver, not a model call.
Dependency adds (logged in DECISIONS.md per owner directive): `ortools>=9.15.6755` (runtime). cp312 wheels exist for macOS arm64 and manylinux x86_64; the Linux wheel is about 29 MB. It pulls in `pandas`, `protobuf>=6.33.1,<6.34`, `absl-py`, `immutabledict` and `typing-extensions`, so expect `uv sync` on a cold CI cache to take noticeably longer (about 30-60 s). Import it lazily inside `solve`, so API startup and the other tests do not pay its import time. Add `ortools.*` to the mypy override if stubs are missing.

## Rungs touched
None of the ladder rungs. This is registry planning ahead of any contact. The ask rung sees shifted windows only after an operator accepts.

## Hard rules exercised
- §2.1: proposals edit registry records only; there is no vehicle command, route or contact action (`test_proposal_is_not_control`, denylist grep).
- §2.2: `test_proposal_is_not_control`. Accept is a human HTTP action.
- §2.4: `-k deterministic` (single worker, fixed seed, sorted construction, hash equality), plus `test_no_clock`.
- §3: dependency add logged; no `config/ladder.yaml` threshold touched (capacity lives in `config/registry.yaml`).

## Done
PR: see branch `wo/082-slot-proposal` · Merged commit: (squash, see STATE) · Test summary: `1823 passed, 2 skipped` (make check exit 0; 38 WO-082 tests) · STATE.md updated: yes
- Dependency: `uv add ortools>=9.15.6755` (installed 9.15.6755 with absl-py, immutabledict, pandas, protobuf 6.33.6; about 5 s warm on this machine); imported only inside `slots.solve`/`build_model`/`make_solver` (`test_ortools_imported_lazily`); `ortools.*` mypy override in its own `# WO-082` table.
- Hand-built case: OPTIMAL, 210 min (P-B +120, P-C +90), about 0.3 s cold; the brute-force reference in the test recomputes 210 and the next best 225.
- Deviations (DECISIONS 2026-10-17 08:23): only `num_search_workers=1` is set (OR-Tools 9.15 returns MODEL_INVALID with both worker fields); `diagnose` lifts all latest starts first, then names the single lift with the least total delay (sorted-first would name P-A in variant (i)); variant (ii) names P-F, P-A and P-B (P-B also started at 09:00); `inputs_hash` covers `target_plan_id`; `PlanShift` adds the filed `start_at`/`end_at`; infeasible proposals list every plan with shift 0; accept checks `not_pending` before staleness; `no_tick_scale` applies to moving plans only.
- "P-F's row is byte-identical" is read as every field except `status` and `conflicts` (the WO also requires every plan in the set to end `active`, which changes P-F's status).
- Accept writes the shifts and the proposal status in one session; `recompute_conflicts` and `set_conflict_resolution` (WO-081) then run in their own sessions. All refusals (`not_pending`, `stale_proposal`, `infeasible`, `no_tick_scale`) happen before any plan row is written.
- BT-01: `scenarios/BT-01.yaml` is not on main, so the stand-in is used; at default capacity 2 its components are proven INFEASIBLE (binding `capacity`), each in well under 2 s CPU. The strict OPTIMAL assertion turns on automatically when the real file lands.
- e2e: `tests/e2e/test_registry_proposal.py` (capacity 1 via a test slot config) passes locally with `SOUNDINGS_E2E=1`; it writes `artifacts/registry-proposal.png` (gitignored).
- Retention: `purge_older_than` also deletes proposals created before the cutoff (CLAUDE.md section 6).
- No migrations: the plan table gained `earliest_start_at`/`latest_start_at` and there is a new `registry_proposal` table; delete any local `soundings.db` from before this WO.

## Notes for downstream
- WO-061 (+QR): after a phone filing returns `status: "conflicted"`, the panel calls `POST /api/plans/{id}/proposal` and renders `shifts`, `total_delay_min` and `detail`. Accept and decline use the same routes.
- `now` is a parameter everywhere. Sim callers pass `tick_to_datetime(scenario_epoch(s), now_tick, s.tick_s)`.
- OR-Tools 9.15 treats `num_search_workers` as a deprecated alias. Set both fields, and keep the assertion on whichever field the installed version exposes.
