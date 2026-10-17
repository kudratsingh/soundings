# WO-020 — registry

Phase: 2 · Branch: `wo/020-registry` · Worktree: `../soundings-worktrees/wo-020-registry` · Est: 4 h · Owner: frontend / demo (unassigned); coordinator Kudrat
Depends on: WO-010 (`Harbor`, `load_harbor`, `Scenario`, `RegistryPlan`, `PlanWindow`, `FreeText`, `load_scenario`, `canonical_json`); WO-000 is merged (`soundings/api.py`). Nothing from WO-011: tests read only the scenario files present at test time (S02 is on `main`). Runs in parallel with WO-011 and WO-040 (wave 2); WO-022 consumes it.

## Goal
A registry of planned underwater work that the ask rung can query. Plans persist in SQLite through SQLModel at the path in `SOUNDINGS_DB`. Each plan carries an area (a declared security zone from `config/harbor.geojson`), a tick window for sim mode and ISO datetimes for live mode, a modem id, an operator, a vehicle and a contact channel. A REST router under `/api/plans` creates, reads, lists and expires plans. A single static HTML form served by FastAPI at `/registry` lets a watch officer or an operator file a plan. The overlapping-window query, which is the matcher's hot path, is indexed and property-tested. A plan whose area is not a declared security zone is rejected with a reason code.

## Scope
In:
- `soundings/registry/models.py`:
  - `Plan` (SQLModel, `table=True`). Columns: `plan_id` (PK), `operator`, `vehicle`, `modem_id`, `area`, `start_tick`, `end_tick`, `start_at`, `end_at` (timezone-aware ISO), `contact_channel`, `status`, `source` ∈ {scenario, form, api}, `created_at`, `expired_at | None`. Composite index on (`status`, `start_tick`, `end_tick`) and on (`status`, `start_at`, `end_at`); index on `modem_id`.
  - `PlanCreate` (Pydantic input; `operator`, `vehicle`, `contact_channel` are `FreeText`). `plan_id` is optional; when it is missing the store assigns `P-<yyyymmdd>-<nnn>`, sequential per date.
  - `PlanView` (frozen Pydantic, the only type the matcher sees), `PlanStatus` (StrEnum: active, expired), `RejectReason` (StrEnum: area_not_security_zone, window_missing, window_inverted, bad_modem_id, duplicate_plan_id, denylisted_text), `PlanRejected(Exception)` carrying `reason: RejectReason` and `detail: str`.
  - Time helpers: `scenario_epoch(scenario) -> datetime` = `registry.sim_epoch_date` at `hour_of_day:00` with the fixed offset from config (no tz database needed). `tick_to_datetime(epoch, tick, tick_s)` and `datetime_to_tick(epoch, dt, tick_s)` (floor).
- Validation, in this order, each failure raising `PlanRejected`:
  1. `area` must be a `zone_id` in the loaded `Harbor`, else `area_not_security_zone` with the list of valid zone ids in `detail`.
  2. At least one complete window (ticks or datetimes), else `window_missing`.
  3. start ≤ end for each window given, else `window_inverted`.
  4. `modem_id` matches `registry.modem_id_pattern` (default `^MOD-[0-9]{4,6}$`; S02 uses `MOD-4471`), else `bad_modem_id`.
  5. A `plan_id` already present gives `duplicate_plan_id`.
  6. A denylisted word in free text gives `denylisted_text`; the router maps the Pydantic error to this reason.
  When only one window is given, the store derives the other from `scenario_epoch` and `tick_s` passed by the caller (sim) or leaves the ticks null (live).
- `soundings/registry/store.py`:
  - `RegistryStore(db_path=None, *, harbor=None, now=None)`. `db_path` defaults to `db_path_from_env()` (`SOUNDINGS_DB`, else `soundings.db`). `now` is an injected clock; tests pass a fixed one, so nothing in a test reads the wall clock.
  - Methods: `create(PlanCreate, *, epoch=None, tick_s=None) -> PlanView`, `get(plan_id)`, `list(status=None)`, `expire(plan_id, *, at=None) -> PlanView`, `overlapping_ticks(start_tick, end_tick, *, include_expired=False)`, `overlapping_times(start_at, end_at, *, include_expired=False)`, `by_modem(modem_id)`, `seed_from_scenario(scenario) -> list[PlanView]`, `purge_older_than(days=30, *, now=None) -> int`.
  - Overlap means closed intervals: `a.start ≤ b.end ∧ b.start ≤ a.end`, done in SQL with the index. Results are ordered by (`start_tick`, `plan_id`) so callers see a deterministic order.
  - `plans_from_scenario(scenario) -> list[PlanCreate]` maps each WO-010 `RegistryPlan` (window ticks, `contact` → `contact_channel`) and derives datetimes from `scenario_epoch`. It sets `source=scenario` and `created_at = scenario_epoch`, so seeding is deterministic.
  - Retention: `purge_older_than` deletes plans whose `end_at` is more than `registry.retention_days` (30) in the past (CLAUDE.md §6: no retention beyond 30 days).
- `soundings/registry/router.py`: `router = APIRouter()` and a `get_store()` dependency that reads `SOUNDINGS_DB` at request time, so tests override it with `app.dependency_overrides`. Routes:
  - `POST /api/plans` → 201 `PlanView`; 422 `{"reason": <RejectReason>, "detail": ...}`.
  - `GET /api/plans` with optional `status`, `start_tick` & `end_tick`, or `start_at` & `end_at` → list.
  - `GET /api/plans/zones` → the declared security zones (`zone_id`, `name`, `criticality`) for the form dropdown. Declared before the `{plan_id}` route.
  - `GET /api/plans/{plan_id}` → 200 or 404.
  - `POST /api/plans/{plan_id}/expire` → 200 `PlanView` with `status=expired`.
  - There is no DELETE route. Expiry is the only way a plan leaves the active set, and retention purge is the only real delete.
- `soundings/registry/static/registry.html`: one page of plain HTML and vanilla JS, no build step, no framework, not in `web/src`.
  - Fields: zone dropdown (loaded from `/api/plans/zones`), operator, vehicle, modem id, contact channel, start and end datetimes, and an optional "sim ticks" pair.
  - Behaviour: submit calls `POST /api/plans` and shows the plan id or the reject reason inline. Below the form, a table of active plans has an Expire button per row.
  - Footer: "Registry entries are retained for at most 30 days. Simplified for demonstration."
- `soundings/api.py`: `app.include_router(router, prefix="/api/plans")` and `GET /registry` returning the static file. Both are registered before the `web/dist` static mount, so the root mount does not swallow them. Shared file: this WO owns only a block marked `# --- WO-020: registry ---`; keep the commit small and rebase early.
- `config/registry.yaml` with a `registry:` block: `modem_id_pattern`, `sim_epoch_date: "2026-10-17"`, `sim_epoch_utc_offset: "-07:00"`, `retention_days: 30`, `plan_id_prefix: "P"`. (`*.db` is already in `.gitignore`.)
- `scripts/registry_form_shot.py`, a Playwright (Python, sync API) script. It opens `/registry`, files the first plan of `--plan-from <scenario id>` (default S02, the only plan-bearing scenario on `main` today), files one plan with area `not_a_zone` to show the rejection, and saves `artifacts/registry-form.png`. WO-022 reuses it for the phase manual check.

Out: matching logic and tolerances (WO-022); the hail (WO-021); runner wiring (WO-022); the React UI (WO-060); authentication; editing a plan in place (expire it and file a new one instead).

## Spec references
`docs/BUILD-PLAN.md` Phase 2; `docs/SPEC-INVESTIGATION.md §2` (registry/identity columns: `registry_match`, `plan_id`, `modem_id`), `§5` (`registry_lookup`: watch rung, cost 0, latency 0); `docs/SPEC-SIM.md §4` (the `registry_plans` block in S02); `CLAUDE.md §4` (SQLite via SQLModel), `§6` (declared security zones only; 30-day retention); `docs/WORKFLOW.md §8`.

## Files expected
- `soundings/registry/__init__.py`, `models.py`, `store.py`, `router.py`, `static/registry.html`
- `soundings/api.py` (router mount and `/registry` route only)
- `config/registry.yaml`, `scripts/registry_form_shot.py`
- `tests/test_registry.py` (unit), `tests/test_registry_api.py` (router), `tests/test_registry_integration.py` (catalog)

## Exit tests (must all pass with `MODEL_MODE=replay`, no network)
From BUILD-PLAN Phase 2 (verbatim):
- [ ] `pytest tests/test_registry.py` — create/read/expire plans; overlapping-window query returns correct plans; plans outside the security zone are rejected.

Unit (`tests/test_registry.py`; every test uses `tmp_path / "reg.db"` and a fixed `now`):
- [ ] `uv run pytest tests/test_registry.py -k crud` — create returns a `PlanView` equal to the input plus the assigned id; get returns it; list(status=active) contains it; expire sets `status=expired` and `expired_at`; an expired plan drops out of both overlap queries unless `include_expired=True`. Proves the lifecycle.
- [ ] `uv run pytest tests/test_registry.py -k overlap_property` — Hypothesis (`derandomize=True`, 200 examples) generates up to 30 random tick windows and query windows, and `overlapping_ticks` equals a brute-force closed-interval filter, in (`start_tick`, `plan_id`) order. Touching endpoints (end == start) count as overlap. Proves the hot-path query is exact.
- [ ] `uv run pytest tests/test_registry.py -k overlap_times` — the same check on datetimes across a UTC offset boundary. Proves the live-mode query.
- [ ] `uv run pytest tests/test_registry.py -k reject` — table-driven, one case per `RejectReason`, including `area="open_ocean"` and an empty area for `area_not_security_zone`. Each raises `PlanRejected` with the expected reason, and nothing is written (row count unchanged). Proves outside-zone plans and malformed plans never enter the store.
- [ ] `uv run pytest tests/test_registry.py -k env_path` — with `SOUNDINGS_DB` set by monkeypatch to a tmp path, `RegistryStore()` creates the file there, and with it unset the default is `soundings.db`. Proves tests never touch the developer's database.
- [ ] `uv run pytest tests/test_registry.py -k retention` — a plan ending 31 days before `now` is purged and one ending 29 days before is kept. Proves the 30-day line.
- [ ] `uv run pytest tests/test_registry.py -k epoch` — `scenario_epoch` for `hour_of_day: 14` is `2026-10-17T14:00:00-07:00`, and `tick_to_datetime` / `datetime_to_tick` round-trip for ticks 0, 1, 899 at `tick_s=2`. Proves the sim/live window bridge.

Router (`tests/test_registry_api.py`, FastAPI `TestClient` with `get_store` overridden to a tmp store):
- [ ] `uv run pytest tests/test_registry_api.py` — POST 201 then GET by id 200. POST with a bad zone returns 422 with `reason == "area_not_security_zone"`. GET `/api/plans?start_tick=400&end_tick=410` returns only overlapping plans. POST `/expire` then the list excludes the plan. GET `/api/plans/zones` returns exactly the harbor zone ids, sorted. GET `/registry` returns 200 `text/html` containing `<form`. GET `/health` still returns `{"ok": true, "model_mode": "replay"}`. Proves the REST surface and that the mount order left Phase 0 intact.

Integration (`tests/test_registry_integration.py`, real files on disk):
- [ ] `uv run pytest tests/test_registry_integration.py -k s02` — loads `scenarios/S02-filed-vehicle-gone-silent.yaml`, POSTs each `plans_from_scenario` entry through the API into a tmp DB, then `overlapping_ticks(450, 450)` returns exactly `P-2026-1017-003` with `modem_id == "MOD-4471"`, `area == "inner_basin_research"`, and `start_at == 2026-10-17T14:00:00-07:00`. Proves a real catalog plan travels scenario → REST → SQLite → hot-path query intact.
- [ ] `uv run pytest tests/test_registry_integration.py -k catalog` — parametrized over every `scenarios/S*.yaml` with a `registry_plans` block present at test time (S02 today; S01 and S03 join automatically when WO-011 merges, and WO-022's `-k registry_catalog` asserts all three), `seed_from_scenario` accepts every plan (so every catalog area is a declared zone), and `by_modem` finds each plan. Proves the catalog and WO-010's harbor agree with the registry rules.

Determinism:
- [ ] `uv run pytest tests/test_registry_integration.py -k deterministic_seed` — seeds every plan-bearing catalog file into two fresh tmp DBs, and `canonical_json([p.model_dump(mode="json") for p in store.list()])` is byte-identical between them; also runs in a subprocess with `PYTHONHASHSEED=0` and `=1` and compares stdout. Proves plan ids, `created_at` and ordering do not depend on the wall clock or hash seed (CLAUDE.md §2.4), which matters because the WO-022 runner reads these plans.

Guard / rule proof:
- [ ] CLAUDE.md §6 scope line (no activity outside declared security zones; 30-day retention) is proven by `-k reject` and `-k retention`.
- [ ] CLAUDE.md §2.2: `grep -rn "soundings.llm" soundings/registry` prints nothing. No model path writes or reads the registry.
- [ ] CLAUDE.md §2.5: `grep -riEf config/denylist.txt soundings/registry config/registry.yaml scripts/registry_form_shot.py tests/test_registry*.py` prints nothing, and `-k reject` proves denylisted free text is refused.
- [ ] `make check` green.

Manual (Playwright screenshot; counts as the WO's manual check):
- [ ] With `make dev` running and `SOUNDINGS_DB=/tmp/reg-shot.db`: `uv run python scripts/registry_form_shot.py --base http://localhost:8000` writes `artifacts/registry-form.png`. The image shows the filed S02 plan in the active table and the inline rejection for `not_a_zone`. Attached to the PR, marked "Playwright screenshot". Needs `uv run playwright install chromium` once (a browser download; not part of the replay suite).

## Interfaces
Exposed by WO-020:
- `soundings.registry.models.Plan`, `.PlanCreate`, `.PlanView`, `.PlanStatus`, `.RejectReason`, `.PlanRejected`, `.scenario_epoch`, `.tick_to_datetime`, `.datetime_to_tick`, `.load_registry_config`
- `soundings.registry.store.RegistryStore` (`create`, `get`, `list`, `expire`, `overlapping_ticks`, `overlapping_times`, `by_modem`, `seed_from_scenario`, `purge_older_than`), `.plans_from_scenario`, `.db_path_from_env`, `.DEFAULT_DB_PATH`
- `soundings.registry.router.router`, `.get_store`
- HTTP: `POST /api/plans`, `GET /api/plans`, `GET /api/plans/zones`, `GET /api/plans/{plan_id}`, `POST /api/plans/{plan_id}/expire`, `GET /registry`
- Files: `config/registry.yaml`, `soundings/registry/static/registry.html`, `scripts/registry_form_shot.py` (`--base URL --plan-from <scenario id>`)
- `PlanView` fields (WO-022 relies on these exactly): `plan_id, operator, vehicle, modem_id, area, start_tick: int | None, end_tick: int | None, start_at: datetime, end_at: datetime, contact_channel, status`.

Consumed:
- WO-000: `soundings.api.app`.
- WO-010: `soundings.schemas.Harbor`, `load_harbor`, `Scenario`, `RegistryPlan`, `PlanWindow`, `FreeText`, `soundings.schemas.scenario.load_scenario` (integration tests); `soundings.canonical.canonical_json`.

## Cut line
If more than an hour late, drop these in order (no exit test is dropped):
1. The Expire button in the form; the REST route stays.
2. Auto-assigned plan ids; require `plan_id` in the form.
3. The datetime-range query parameters on `GET /api/plans`; `overlapping_times` stays in the store and its test.
4. The form's sim-ticks pair; the store derives ticks from datetimes.

## Model calls
None. Paid run needed? No.
Dependency adds needing owner approval (CLAUDE.md section 3): none. SQLModel, FastAPI, httpx and playwright are in the Phase 0 set. Fixed UTC offsets avoid needing `tzdata`.

## Rungs touched
watch (the `registry_lookup` probe reads this store at the watch rung) and ask (the matcher in WO-022 clears contacts against it). Nothing above ask.

## Hard rules exercised
- §2.2: the `soundings.llm` grep. The registry is filled only by humans (form or API) and scenario files.
- §2.4: `-k deterministic_seed`, with two DBs and a PYTHONHASHSEED subprocess diff.
- §2.5: the denylist grep and `-k reject` (`denylisted_text`).
- §6 scope line: `-k reject` (security zones only) and `-k retention`.

## Done
PR: · Merged commit: · Test summary: · STATE.md updated:
