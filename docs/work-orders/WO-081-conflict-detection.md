# WO-081 — conflict-detection

Phase: 7 · Branch: `wo/081-conflict-detection` · Worktree: `../soundings-worktrees/wo-081-conflict-detection` · Est: 5 h · Owner: coordinator Claude / Kudrat
Depends on: WO-020 (`RegistryStore`, `Plan`, `PlanView`, `PlanCreate`, `PlanDraft`, `PlanStatus`, router, `registry.html`), WO-010 (`Harbor.zone`, `Zone.polygon`, `soundings.schemas.geo.to_local_m`), WO-022 (`soundings.matcher._active`, which today keeps only `PlanStatus.ACTIVE`), WO-080 part A (scenario `BT-01` with 40 filed plans, `RegistryPlan.job_type`, `soundings.schemas.enums.JobType`), WO-060 (in flight on `origin/wo/060a-ui-core`: `tests/e2e/conftest.py`, `make e2e`; the e2e item waits for it). Wave B: branch after WO-080 part A merges. Before that, the pure function and its unit tests can start; only the BT-01 tests wait.

## Goal
When a plan is filed, the registry finds every plan already in force that overlaps it in all three of area, depth band and time window, and records each one as a reason-coded `Conflict` with its overlap minutes. The new plan is still accepted. It and each plan it conflicts with get the new status `conflicted`. Conflicts show in `GET /api/plans`, in `GET /api/plans/{id}`, and in the form as "conflicts with P-20261017-004 14:00-16:00". The detector is a pure function: shapely polygons in local metres, a `STRtree` over the existing plans, and output ordered by `(start, plan_id)`. A conflicted plan still counts for the matcher, so filed contacts keep clearing at ask. The registry flags the conflict and an operator decides what to do about it. Nothing is moved here. WO-082 proposes slots.

## Scope
In:
- `soundings/registry/conflicts.py` (pure: no I/O, no clock, no model import):
  - `ConflictReason` (StrEnum): `area`, `depth`, `window`.
  - `PlanFootprint(plan_id, area, start_at, end_at, depth_min_m, depth_max_m)` (frozen), plus `footprint_of(view: PlanView, cfg) -> PlanFootprint`, which fills in the job-type default depth band when the plan gives none.
  - `Conflict(plan_id, other_plan_id, reasons: frozenset[ConflictReason], overlap_start, overlap_end, overlap_minutes: int, depth_overlap_m: float, area_overlap_m2: float)` (frozen Pydantic). A `Conflict` exists only when all three overlap, so `reasons` is always the full set. The other fields are informational.
  - `zone_geometry(harbor, zone_id, buffer_m) -> shapely.Polygon`: the zone polygon in `to_local_m` metres, buffered by `buffer_m`. It is cached per `(harbor name, zone_id, buffer_m)`.
  - The overlap rules, all half-open, so back-to-back work is not a conflict:
    - area: `intersection.area > 0` (touching edges do not count);
    - depth: `min(max) - max(min) > 0`;
    - window: `overlap_minutes = floor((min(end) - max(start)) / 60 s) > 0`.
    The existing `store.overlaps` closed-interval rule stays as it is for the matcher.
  - `detect_conflicts(new: PlanFootprint, existing: Sequence[PlanFootprint], harbor, cfg) -> tuple[Conflict, ...]`:
    1. Sort `existing` by `(start_at, plan_id)`.
    2. Build an `STRtree` over their buffered geometries.
    3. `tree.query(new_geom, predicate="intersects")`.
    4. Sort the hit indices.
    5. Run the exact area, depth and window tests.
    6. Return the conflicts sorted by `(other.start_at, other_plan_id)`.
  - `conflicts_among(footprints, harbor, cfg)` is the batch form, used for seeding and for `plan_views_from_scenario`. It files the plans in the given order and gives the same result as sequential `create()` calls.
- `soundings/registry/models.py`:
  - `PlanStatus.CONFLICTED = "conflicted"`, and `IN_FORCE: frozenset[PlanStatus] = {ACTIVE, CONFLICTED}`.
  - `JobType` is imported from WO-080's `soundings.schemas.enums` (`survey`, `hull_cleaning`, `pile_inspection`, `dredge`, `other`); `other` is the registry default for form filings.
  - New optional fields on `PlanDraft`/`PlanCreate`/`Plan`/`PlanView`: `job_type` (default `other`), `depth_min_m`, `depth_max_m` (both or neither; min <= max, else the new `RejectReason.DEPTH_BAND_INVERTED`, checked after `WINDOW_INVERTED`), and `tick_s` (stored when the epoch/tick bridge is used; WO-082 needs it to shift ticks).
  - `ConflictView(other_plan_id, overlap_start, overlap_end, overlap_minutes, reasons: list[str], resolved_by: str | None)`, and `PlanView.conflicts: tuple[ConflictView, ...] = ()`. These are defaulted fields after the existing ones, so current callers keep working.
  - `ConflictConfig(area_buffer_m, depth_defaults_m: dict[JobType, tuple[float, float]])` and `load_conflict_config(path=config/registry.yaml)`, read from a new top-level `conflicts:` block. `RegistryConfig` and its `registry:` block are unchanged (it is `extra="forbid"`).
- `soundings/registry/store.py`:
  - New table `registry_conflict(plan_id, other_plan_id, overlap_start, overlap_end, overlap_minutes, reasons, resolved_by, detected_at)`. It has one row per direction, with primary key `(plan_id, other_plan_id)`.
  - `create()` runs `_check`, then pre-filters the in-force plans with the existing SQL window query (`overlapping_times`), then calls `detect_conflicts`. In the same session it inserts the plan, the conflict rows, and the status flips: the new plan and each partner become `conflicted`.
  - `expire()` deletes the expired plan's conflict rows and sets any partner left with no unresolved conflict back to `active`.
  - `set_conflict_resolution(plan_ids, proposal_id)` and `recompute_conflicts(plan_ids)` are what WO-082 calls; they live here so all status logic sits in one file.
  - `overlapping_ticks`, `overlapping_times` and `list(status="active")` filter on `IN_FORCE`. `list(status="conflicted")` returns only conflicted plans.
  - `plans_from_scenario` copies `RegistryPlan.job_type` (None -> `other`) and the scenario `tick_s`.
  - `plan_views_from_scenario` uses `conflicts_among`, so the database-free views still equal a seeded store read back.
- `soundings/matcher.py` `_active`: `p.status in IN_FORCE`. This is a one-line change and it is required, because otherwise a conflicted plan would stop clearing its contact.
- `soundings/registry/router.py`:
  - `status` accepts `conflicted`, and `active` means in force.
  - `PlanView.conflicts` is serialized on every GET.
  - `GET /api/plans/{id}/conflicts` returns the list alone.
- `registry.html`:
  - a Conflicts column with one line per conflict, `conflicts with <plan> HH:MM-HH:MM` in the plan's filed offset;
  - the result banner after filing names the conflicts;
  - optional Job type, Depth min and Depth max inputs.
- `config/registry.yaml`: a top-level `conflicts:` block with `area_buffer_m: 50` and the depth defaults below (labeled stress-test assumptions, not survey data).

Out: proposing or moving any plan (WO-082); vessel traffic (WO-083); zone capacity (WO-082 config); migrating an existing local `soundings.db` (it is gitignored, so delete it; `create_all` adds the new table but not the new columns; see Notes).

Depth defaults (m below surface, `[min, max]`): `survey [0, 30]`, `hull_cleaning [0, 12]`, `pile_inspection [0, 20]`, `dredge [5, 25]`, `other [0, 30]`.

## Spec references
`docs/work-orders/WO-020-registry.md` (store rules, ordering, Done section) · owner directive 2026-10-17 06:05 PDT (WO-081 text) · `CLAUDE.md §2.4, §2.5, §3, §6` · `docs/WORKFLOW.md §8` (sorted iteration, 6 dp at boundaries).

## Files expected
- `soundings/registry/conflicts.py` (new), `models.py`, `store.py`, `router.py`, `__init__.py`, `static/registry.html`
- `soundings/matcher.py` (the `_active` line only)
- `config/registry.yaml` (`conflicts:` block)
- `tests/test_conflicts.py`, `tests/test_conflicts_property.py`, `tests/test_conflicts_api.py`, `tests/test_conflicts_integration.py`, `tests/e2e/test_registry_conflicts.py`
- `pyproject.toml` (the `shapely` line with a trailing `# WO-081` comment, and its own `[[tool.mypy.overrides]]` table for `shapely.*` marked `# WO-081`), `uv.lock`, `docs/log/DECISIONS.md` (dependency row plus one for depth defaults and `area_buffer_m`), `docs/STATE.md` (WO-081 row only)
- Shared files are edited only inside marked blocks: `router.py` `# --- WO-081: conflicts ---`, `registry.html` `<!-- WO-081: conflicts -->` (one block for the column/banner JS, one for the form inputs). WO-082 adds its own blocks after these.

## Exit tests (must all pass with `MODEL_MODE=replay`, no network)
From the owner directive (verbatim):
- [ ] table-driven tests (overlap in all three -> conflict; any one disjoint -> none); 40-plan BT-01 filing runs < 200 ms; form shows "conflicts with <plan> 14:00-16:00".

Each clause as a command:
- [ ] (directive, verbatim) table-driven tests (overlap in all three -> conflict; any one disjoint -> none). `uv run pytest tests/test_conflicts.py::test_three_way_table` proves this. It runs 8 rows: all three overlap; each one disjoint alone; each pair disjoint; all three disjoint. It also runs edge rows: windows touching at 14:00, depth bands touching at 12 m, and zones that only touch after buffering by 0 m. Only the all-overlap row yields a `Conflict`, and its `reasons == {area, depth, window}`.
- [ ] (directive, verbatim) 40-plan BT-01 filing runs < 200 ms. `uv run pytest tests/test_conflicts_integration.py::test_bt01_filing_under_200ms` proves this. It files the 40 BT-01 plans one by one into a fresh `":memory:"` store (store construction and imports excluded) and takes the best of 3 `time.perf_counter` passes, which must be < 0.200 s. The pure `conflicts_among` over the same 40 plans must run < 0.050 s. Timing is read in the test only, never in the code under test.
- [ ] (directive, verbatim) form shows "conflicts with <plan> 14:00-16:00". `SOUNDINGS_E2E_REQUIRED=1 uv run pytest tests/e2e/test_registry_conflicts.py` (Playwright; reuses WO-060's `tests/e2e/conftest.py` server fixture; part of `make e2e`) proves this. It files P-X `inner_basin_research` 13:00-16:00, then files P-Y in the same zone 14:00-18:00 through the form, and asserts that P-Y's row text contains `conflicts with P-X 14:00-16:00` and P-X's row contains `conflicts with P-Y 14:00-16:00`. A unit fallback, `pytest tests/test_conflicts_api.py::test_form_render_contract`, checks that the page holds the JS template string `"conflicts with "`.
- [ ] `uv run pytest tests/test_conflicts.py` covers the rest of the unit cases:
  - overlap minutes are exact (13:00-16:00 vs 14:00-18:00 gives 120 with start 14:00 and end 16:00), mixed UTC offsets compare as instants, and seconds are floored;
  - a plan with no depth fields takes its `job_type` default from config, never a literal in the test;
  - `area_buffer_m` makes adjacent zones conflict at 50 m and not at 0 m (the pair is chosen from `config/harbor.geojson` by the test);
  - `fuel_berths` inside `inner_harbor_west` conflicts with no buffer;
  - results are ordered by `(start_at, plan_id)` even when `existing` is passed shuffled;
  - `DEPTH_BAND_INVERTED` rejects and writes nothing.
- [ ] `uv run pytest tests/test_conflicts_property.py` is a hypothesis property test over random windows, depth bands and zone pairs. It proves that `detect_conflicts` returns a conflict iff a brute-force reference (shapely `intersection().area > 0` with no tree, and plain interval arithmetic) says all three overlap. It also proves symmetry: A conflicts with B iff B conflicts with A, with equal `overlap_minutes`.
- [ ] `uv run pytest tests/test_conflicts_api.py` covers the API:
  - `POST /api/plans` of a conflicting plan returns 201 with `status: "conflicted"` and a non-empty `conflicts`;
  - `GET /api/plans/{id}` and `GET /api/plans` show the conflict on both plans;
  - `status=conflicted` filters to those plans;
  - expiring one plan returns its partner to `active` with `conflicts == []`;
  - a rejected plan creates no conflict rows.
- [ ] integration over a real scenario: `uv run pytest tests/test_conflicts_integration.py::test_conflicted_plan_still_clears` proves conflicted plans stay in force for the ask rung. It seeds S01 into a store, files a second plan for S01's zone and window (another modem), and asserts that S01's plan is `conflicted`. `ScenarioRunner("S01", plans=store).run()` still ends the S01 contact `CLEARED_AT_ASK` with 0 interruptions. The same check runs on BT-01: every filed case's state is unchanged from the run with conflict detection switched off (`area_buffer_m` set below any overlap, through a test-only config).
- [ ] determinism: `uv run pytest tests/test_conflicts_integration.py -k deterministic` proves registry seeding is byte-stable (CLAUDE.md §2.4). Seeding BT-01 twice in-process, and once each in subprocesses under `PYTHONHASHSEED=0` and `=1`, gives byte-identical canonical JSON of `store.list("all")` with conflicts (sha256 equal). It also asserts that `plan_views_from_scenario(BT-01)` equals that read-back.
- [ ] rule proof §2.4: `uv run pytest tests/test_conflicts.py::test_no_clock_no_model` is an AST scan of `conflicts.py`. It finds no `datetime.now`, `time.`, `random`, `soundings.llm` or `anthropic`, and `STRtree` results are sorted before use (asserted by passing a tree-order-reversed input).
- [ ] rule proof §2.1/§2.5: run `grep -inEf config/denylist.txt soundings/registry/conflicts.py soundings/registry/static/registry.html tests/test_conflicts*.py` from the repo root; it prints nothing. `make check` is green.

## Interfaces
Exposed:
- WO-081 `soundings.registry.conflicts.ConflictReason`, `.PlanFootprint`, `.footprint_of`, `.Conflict`, `.zone_geometry`, `.detect_conflicts`, `.conflicts_among`
- WO-081 `soundings.registry.models.PlanStatus.CONFLICTED`, `.IN_FORCE`, `.ConflictView`, `.ConflictConfig`, `.load_conflict_config`, `.RejectReason.DEPTH_BAND_INVERTED`, `PlanView.{job_type, depth_min_m, depth_max_m, tick_s, conflicts}`
- WO-081 `soundings.registry.store.RegistryStore.conflicts_of(plan_id)`, `.set_conflict_resolution(plan_ids, proposal_id)`, `.recompute_conflicts(plan_ids)`, and the `list(status="conflicted")` filter
- WO-081 routes: `GET /api/plans?status=conflicted`, `GET /api/plans/{plan_id}/conflicts`, and the `conflicts` array in `GET /api/plans` and `GET /api/plans/{plan_id}`

Consumed:
- WO-020: `soundings.registry.models.{Plan, PlanDraft, PlanCreate, PlanView, PlanStatus, PlanSource, RejectReason, RegistryConfig, load_registry_config, to_utc, from_storage, offset_minutes}`, `soundings.registry.store.{RegistryStore.create, .expire, .overlapping_times, .list, plans_from_scenario, plan_views_from_scenario}`, `soundings.registry.router.router`
- WO-010: `soundings.schemas.harbor.{Harbor, Zone}`, `Harbor.zone(zone_id)`, `soundings.schemas.geo.to_local_m`, `soundings.canonical.canonical_json`
- WO-022: `soundings.matcher._active`, `soundings.runner.ScenarioRunner(scenario, plans=...)`, `RunResult.case`
- WO-080 part A: `soundings.schemas.enums.JobType`, `soundings.schemas.scenario.RegistryPlan.job_type`, `scenarios/BT-01.yaml` (`resolve_scenario("BT-01")`)
- WO-060 (in flight, `origin/wo/060a-ui-core`): `tests/e2e/conftest.py` server fixture, `make e2e`

## Cut line
Drop these first if more than an hour late: (1) the Job type and Depth inputs on the form (the API still takes them, and the defaults apply); (2) `GET /api/plans/{id}/conflicts` (the array in the plan GET is enough); (3) `area_overlap_m2` and `depth_overlap_m` on `Conflict`. Never drop an exit test.

## Model calls
None. Paid run needed? No.
Dependency adds (logged in DECISIONS.md per owner directive): `shapely>=2.2.0` (runtime; cp312 wheels exist for macOS arm64 and manylinux x86_64, about 2 MB, needs only numpy, which is already a dependency). Add `shapely.*` to the mypy `ignore_missing_imports` override if its typing is incomplete.

## Rungs touched
ask (the matcher's plan set now includes `conflicted` plans; outcomes unchanged). No rung is added or changed.

## Hard rules exercised
- §2.4: `-k deterministic` (two-run and `PYTHONHASHSEED` hash equality), `test_no_clock_no_model`, and STRtree hits sorted by `(start, plan_id)`.
- §2.2: no model import in `conflicts.py` (`test_no_clock_no_model`). A filed plan is human input, and the detector is deterministic code.
- §2.1/§2.5: the denylist grep above. The UI wording is "conflicts with", never anything that moves a vehicle.
- §6: no new retention. Conflict rows go with their plan in `purge_older_than` (asserted in `test_conflicts_api.py::test_purge_removes_conflicts`).

## Done
PR: see branch `wo/081-conflict-detection` · Merged commit: (squash, see STATE) · Test summary: `1546 passed, 1 skipped` (make check exit 0; 52 WO-081 tests, BT-01 runner check skipped until WO-080 part A) · STATE.md updated: yes
- Dependency: `uv add shapely>=2.2.0` (installed 2.2.0; approved DECISIONS 2026-10-17 06:37); `shapely.*` mypy override in its own `# WO-081` table.
- Filing time (stand-in 40 plans, all conflicted, 276 pairs): about 45 ms best of 3 on a loaded laptop (first CI run measured 204 ms with the ORM insert path, so the filing path now writes conflict rows and partner flips with Core statements and builds footprints straight from rows); `conflicts_among` about 11 ms.
- Deviations (DECISIONS 2026-10-17 07:03): `JobTypeName` Literal adapter in `models.py` until `soundings.schemas.enums.JobType` lands (swap the alias; values identical); nested-zone test uses `fuel_berths` in `main_channel` (geojson); `ConflictView.reasons` is a tuple; `tests/support/busy_plans.bt01_or_standin()` returns a seeded 40-plan stand-in until `scenarios/BT-01.yaml` exists, then the real one (automatic); `test_bt01_cases_unchanged_by_conflict_detection` skips until then.
- `tests/test_registry.py` REJECT_CASES gained two `DEPTH_BAND_INVERTED` rows (the cover-every-reason test requires each reason; nothing removed).
- e2e: `tests/e2e/test_registry_conflicts.py` uses WO-060's `browser` fixture (part of `make e2e`) and its own `registry_server` fixture (real app, temp-file store injected through `get_store`, so `soundings.db` is never touched). Playwright chromium-headless-shell v1248 was installed into the local cache once.

## Notes for downstream
- WO-082 reads `PlanView.conflicts`, the `IN_FORCE` set, `PlanView.tick_s` and `PlanView.job_type`. After an accepted proposal it calls `store.recompute_conflicts(component)` and then `store.set_conflict_resolution(component, proposal_id)`. A plan's status is `conflicted` iff it has at least one conflict row with `resolved_by IS NULL`.
- WO-080/WO-085/WO-086: `status=active` in the API means in force (active or conflicted). Code that compared `PlanStatus.ACTIVE` directly must use `IN_FORCE`.
- WO-061 (+QR): a phone filing goes through the same `POST /api/plans`, so the conflict list comes back in the 201 body. The panel can render it without a second request.
- No migrations: anyone with a local `soundings.db` from before this WO deletes it (`rm soundings.db`; `make clean` does not remove it). The test suite uses temp or memory stores only.
