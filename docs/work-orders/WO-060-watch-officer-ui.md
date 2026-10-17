# WO-060 — watch-officer-ui

Phase: 6 · Branch: `wo/060-watch-officer-ui` · Worktree: `../soundings-worktrees/wo-060-watch-officer-ui` · Est: 7 h · Owner: frontend / demo (unassigned); coordinator Kudrat
Depends on: WO-010 (schemas, `load_harbor`), WO-011 (`resolve_scenario`, `catalog_paths`, `track_hash`, `load_detection_params`), WO-022 (runner v1), WO-030 (`Posterior`), WO-033 (runner v2), WO-040 (`soundings.clock`, `RUNG_OF_STATE`), WO-041 (`load_boundary`, `load_verified`), WO-042 (`Sitrep` model for the drawer). Not runner v3: the live approval path (stepped source, approve endpoint, the approval half of the BUILD-PLAN Playwright smoke) is WO-061, so this WO merges in wave 8 alongside WO-043.

## Goal
A watch officer and a judge can read one screen in five seconds. It shows a Leaflet map with OpenSeaMap seamarks over OSM tiles, drawing harbor zones, assets, sensor nodes, and live contact tracks. Next to the map sit a contact card, an evidence ledger with facts and assumptions in two colours, a rung indicator showing the five rungs with ask and hand-to-a-person highlighted, exactly one approval button, a SITREP drawer, and an About panel with the honesty text and the boundary table. Behind the screen, a run engine wraps `soundings/runner.py`. It starts a catalog scenario at a seed and streams typed events over a WebSocket, paced by tick. The approval button posts to the approve endpoint that WO-061 adds; the ladder's guard still decides. Nothing in the UI or the API can set a rung.

The UI is built against the event schema below from hour one, using `python -m soundings.stream` JSONL dumps as test input. This WO ships `BatchReplaySource` (runs `ScenarioRunner.run()` to completion and streams its v1/v2 logs paced by tick) and a source registry; WO-061 registers the `SteppedSource` that steps runner v3 and delivers approvals live.

## Scope
In:
- `soundings/stream/events.py`: the Pydantic event models below (`extra="forbid"`), the `StreamEvent` union, and `export_json_schema()`.
- `soundings/stream/engine.py`: `RunEngine` (an in-process registry holding at most 4 live runs, with the oldest finished run evicted first), `RunHandle`, `BatchReplaySource`, and `register_source(name, factory)`. `RunEngine.approve(run_id, request_id, approver)` delegates to the run's source; `BatchReplaySource` has no pending requests (409). `run_to_end(run_id, approve_first)` uses the `stepped` source when it is registered (WO-061) and the batch source otherwise. Pacing uses `soundings.clock.WallClock` with `speed` in sim-seconds per wall-second (default 20 from `config/ui.yaml`), and `speed=0` means unpaced. While a run is AWAITING_APPROVAL, pacing drops to 1× so the approver gets the real `timeout_s` from standing orders. Ladder timers always run on sim time (TickClock), so the decisions stay deterministic.
- `soundings/stream/__main__.py`: `python -m soundings.stream --scenario S05 --seed 11 [--approve-first] [--toggles a,b]` prints the event stream as canonical JSONL at speed 0. It serves debugging, the determinism diff, and WO-062.
- `soundings/api_routes/runs.py` and `soundings/api_routes/meta.py`, mounted in `soundings/api.py` before the static mount. Endpoints: GET /api/scenarios, GET /api/runs (newest first), POST /api/runs, WS /ws/runs/{run_id}, GET /api/boundary, GET /api/about, GET /api/harbor. `soundings/api.py` is shared: this WO owns only a block marked `# --- WO-060: runs and meta ---`.
- `config/ui.yaml`: tile URLs, attribution strings, tile-policy notes with the date they were verified, default speed, and colours for fact, assumption and derived rows.
- `web/src/`: one screen, with `App.tsx` and `api.ts` (both created here; WO-061 and WO-062 each add one marked block to them, nothing else), `events.ts` (hand-written TS mirror of the schema), `useRun.ts` (WebSocket plus a pure reducer, reconnecting with `?after=<seq>`), and `components/` `HarborMap`, `ContactList`, `ContactCard`, `EvidenceLedger`, `RungIndicator`, `ApprovalButton`, `SitrepDrawer`, `AboutPanel`, `ScenarioPicker`. URL params: `scenario`, `seed`, `speed`, and `tiles=off`, which draws only the geojson and is used by tests and offline rehearsal.
- `web/src/generated/stream-events.schema.json`, which a test keeps in sync with the Pydantic models.
- `tests/e2e/test_ui_smoke.py`, a Playwright pytest, plus `tests/e2e/conftest.py`. The conftest starts uvicorn on a free loopback port in a thread, auto-skips when chromium is missing, and turns that skip into a failure under `SOUNDINGS_E2E_REQUIRED=1`.
- Makefile target `e2e`: `pnpm -C web build && SOUNDINGS_E2E_REQUIRED=1 uv run pytest tests/e2e -q`.

Screen layout (the five-second read; one screen, no routing):
- Header: the scenario id and name; sim clock `t_s` and tick; a `model: replay` badge; a state badge with the selected contact's current `LadderState`; links to About and SITREP.
- Left, about 65% of the width: the map. Zones are filled by criticality, assets are diamonds sized by criticality, and sensor nodes are rings drawn at `range_m`. Each contact draws its track polyline and a `circleMarker`, and the selected contact gets a thicker outline.
- Attribution in the corner: "© OpenStreetMap contributors · © OpenSeaMap contributors · Port of Los Angeles, simplified for demonstration".
- Right column, top to bottom:
  - `RungIndicator`: five pills, watch · ask · slow · hold · hand to a person. Ask and hand-to-a-person carry a "we live here" outline from `lives_on`; slow and hold carry a "standing orders" tag; the active rung is filled.
  - `ContactCard`: the contact id, state, top two hypotheses with probabilities tagged "stress-test estimate", threat and benign mass bars, time to the nearest critical asset, and the probes used against the budget.
  - `ApprovalButton`: the only button that changes anything. It shows `action_label`, the scope, and a countdown to `expires_t_s`, and its text is "Approve".
  - `EvidenceLedger`: newest first. Each row reads feature, value, unit, source, confidence and probe, with a FACT, ASSUMPTION or DERIVED chip. The chip label is spelled out so colour is never the only cue. Assumption rows show their `assumes` text.
  - `state-timeline`: every transition, with tick and guard result.
- `SitrepDrawer`: slides up from the bottom. It shows the nine fixed SPEC-LADDER §6 sections. Each fact line ends in evidence-id chips that scroll to and highlight the ledger row. The recommended rung is marked "computed; not editable", and the writer is labelled template or llm.
- `AboutPanel`: a modal with the detection-model parameters under the heading "Detection model: simulated for this demonstration", the probability label, the shared-tables note, the boundary table from GET /api/boundary, standing-orders signer and validity, "The work stops at the handoff.", 30-day retention, tile attribution, and the tile-policy summary with its verified-on date.
- Tile policy, which the builder checks at build time and records in `config/ui.yaml`:
  - The OSM tile usage policy requires visible attribution, no bulk prefetching or offline scraping, a valid Referer (the browser sends it), and light use only.
  - The OpenSeaMap seamark overlay is `https://tiles.openseamap.org/seamark/{z}/{x}/{y}.png` and needs attribution.
  - Record the policy URLs and the date they were checked.

Out: the judge panel, `/api/inject`, the stepped source, the approve endpoint and `ladder_adapter.py` (WO-061), the demo runner, follow mode and video (WO-062), the registry form (WO-020 serves it), any control that writes a rung, state, threshold or scope, a deny button (denial reaches the ladder only by timeout, per SPEC-LADDER §3), auth, and multi-user sessions.

Denylist traps (the grep covers .py, .ts, .tsx, .json and .md):
- The standard HTML attribute that greys out a button, and Playwright's matching assertion, are both on the denylist. The approval button is rendered only while a request is pending. At other times a plain `<span>` reads "No approval pending".
- Tests assert that the button is absent; they never assert that it is greyed out.
- Use `L.circleMarker`, which needs no image assets, and import `leaflet/dist/leaflet.css` once.

## Spec references
`CLAUDE.md §2, §4, §6`; `docs/SPEC-LADDER.md §3` (states the UI may show), `§4` (boundary table in the About panel), `§6` (SITREP sections), `§7` (approval token); `docs/SPEC-INVESTIGATION.md §2` (ledger columns), `§8` (honesty text); `docs/SPEC-SIM.md §1` (map, attribution, "simplified for demonstration"), `§2` (assumptions listed in About); `docs/BUILD-PLAN.md` Phase 6.

## Files expected
- `soundings/stream/{__init__,events,engine,__main__}.py`, `soundings/api_routes/{__init__,runs,meta}.py`, `soundings/api.py` (marked block only)
- `config/ui.yaml`, `web/src/**` as above, `web/src/generated/stream-events.schema.json`
- `tests/test_stream_events.py`, `tests/test_run_engine.py`, `tests/test_api_runs.py`, `tests/test_api_meta.py`, `tests/e2e/conftest.py`, `tests/e2e/test_ui_smoke.py`
- `Makefile` (`e2e` target only; shared file), `pyproject.toml` (only if `websockets` is approved), `docs/STATE.md` (own row)

## Exit tests (must all pass with `MODEL_MODE=replay`, no network)
- [ ] `pnpm -C web exec tsc --noEmit && pnpm -C web build` exits 0, and `make e2e` passes `tests/e2e/test_ui_smoke.py`. The smoke test opens `/?scenario=S05&seed=11&speed=50&tiles=off`, waits for `[data-testid=contact-marker]`, then for `[data-testid=rung-active]` to change from `watch`, then for `HANDED_OFF` in `[data-testid=state-timeline]` (the batch source on runner v2 hands S05 off directly). A `page.route("**/*")` handler aborts every non-loopback request. Proves the screen loads, follows a run and works offline. The BUILD-PLAN smoke line with the approval click is WO-061's.
- [ ] (BUILD-PLAN, verbatim) No panel, label, or button uses a denylisted word (grep in CI). Concretely, CI's `bash scripts/denylist-check.sh` (which covers all of `web/src`, including WO-061's and WO-062's later additions) passes and `grep -rinEf config/denylist.txt web/src soundings/stream soundings/api_routes config/ui.yaml` prints nothing, which proves the vocabulary is clean in the UI and its backend.
- [ ] `uv run pytest tests/test_stream_events.py` checks the following. Every event type round-trips JSON. Unknown fields are rejected. `PosteriorEvent.label` is the literal `"stress-test estimate"`. `export_json_schema()` equals the committed `web/src/generated/stream-events.schema.json`. Together these prove the backend and UI contract cannot drift silently.
- [ ] `uv run pytest tests/test_run_engine.py::test_engine_matches_batch_runner` is an integration test. For S01..S08 at seed 1 (speed 0), engine terminal states and evidence row ids equal `ScenarioRunner` batch output. This proves the UI shows exactly what the runner decided.
- [ ] `uv run pytest tests/test_run_engine.py::test_demo_paths` checks that S01 ends CLEARED_AT_ASK, S02 ends LOST_VEHICLE and its stream carries the search box from the `LostVehicleNotice`, and S05 reaches HANDED_OFF. It also round-trips a WO-042 `template_sitrep` output through `SitrepEvent`. This proves the demo paths stream through the engine (the S05 approval flow is WO-061's).
- [ ] `uv run pytest tests/test_api_runs.py` uses TestClient REST and WS to check the following:
  - GET /api/scenarios lists only `^S\d{2}$` ids, sorted.
  - POST /api/runs returns `run_id`. The WS sends the backlog and then live events with strictly increasing `seq`, and `?after=n` resumes cleanly.
- [ ] `uv run pytest tests/test_api_meta.py` checks three endpoints. GET /api/boundary equals `config/ladder.yaml` `boundary` + `lives_on` plus standing-orders status (signed, signer, valid_until). GET /api/about contains "simulated for this demonstration", "simplified for demonstration", "stress-test estimate", "© OpenStreetMap contributors", "© OpenSeaMap contributors", and the shared-likelihood-tables note from SPEC-SIM §2. GET /api/harbor returns every zone, asset and node in `config/harbor.geojson`. This proves the honesty text is served from config and not hard-coded in the UI.
- [ ] Determinism: `uv run python -m soundings.stream --scenario S05 --seed 11 > /tmp/a.jsonl; uv run python -m soundings.stream --scenario S05 --seed 11 > /tmp/b.jsonl; diff /tmp/a.jsonl /tmp/b.jsonl` prints nothing. `tests/test_run_engine.py::test_stream_deterministic` asserts the same in-process. Events carry ticks and sim seconds only, never wall time.
- [ ] Rule proof §2.2: `tests/test_api_runs.py::test_no_route_sets_rung` walks `app.routes` and the OpenAPI schema and finds no request model with a field named rung, state, threshold, or scope. This proves no route can steer a decision (WO-061 adds the scope-mismatch approval test).
- [ ] Rule proof §2.1: `tests/test_stream_events.py::test_nothing_after_handoff` checks that every state in any S05/S06 stream after HANDED_OFF is WATCH_AFTER_HANDOFF or RESOLVED_DEPARTED, and that every `state` field type is the `LadderState` enum.
- [ ] Rule proof §2.6: `tests/test_run_engine.py::test_sitrep_citations_resolve` checks that every evidence id cited in a streamed SITREP was streamed earlier as an `evidence_row`, and that facts and assumptions sit in separate sections.
- [ ] Rule proof §2.3: `tests/test_run_engine.py::test_no_model_calls_by_default` monkeypatches `ModelClient.call` with a counter, runs S05, and asserts the count is 0. The engine defaults to `use_llm=False`, which means a deterministic planner and the template SITREP, so CI never meets a `ReplayMiss`.
- [ ] `make check` green.
- [ ] Manual (screenshots in the PR): S05 after HANDED_OFF with the ledger showing two colours, and the About panel with the boundary table and the tile attribution.

## Interfaces
Exposed by WO-060:
- `soundings.stream.events`: `EventEnvelope`, `StreamEvent`, `RunStarted`, `TickEvent`, `EvidenceRowEvent`, `PosteriorEvent`, `TransitionEvent`, `RungEvent`, `ApprovalRequestEvent`, `ApprovalResultEvent`, `SitrepEvent`, `RunFinished`, `ErrorEvent`, `export_json_schema`, `SCHEMA_VERSION`
- `soundings.stream.engine`: `RunEngine` (`start(scenario: Scenario, seed: int, *, speed: float | None, toggles: list[ToggleName] = [], use_llm: bool = False) -> RunHandle`, `get(run_id)`, `list()`, `approve(run_id, request_id, approver) -> ApprovalResultEvent`, `subscribe(run_id, after: int) -> AsyncIterator[EventEnvelope]`, `run_to_end(run_id, approve_first: bool) -> list[EventEnvelope]`), `RunHandle`, `BatchReplaySource`, `RunSource` (protocol: `events()`, `approve(request_id, approver)`), `register_source(name, factory)`, `get_engine()` (app-scoped singleton)
- `soundings.api_routes.runs.router`, `soundings.api_routes.meta.router`, `soundings.api_routes.runs.StartRunBody` (`scenario: str`, `seed: int | None`, `speed: float | None`, `toggles: list[ToggleName] = []`, `use_llm: bool = False`, `source: str = "batch"`)
- HTTP: GET /api/scenarios, GET /api/runs, POST /api/runs, WS /ws/runs/{run_id}?after=<seq>, GET /api/boundary, GET /api/about, GET /api/harbor; CLI `python -m soundings.stream`; `make e2e`
- Web: `web/src/events.ts`, `web/src/useRun.ts` (`useRun(runId)`), `web/src/components/HarborMap.tsx` (props `onMapClick?(lat, lon)`, `overlay?`), which WO-061 uses for click-to-place; `web/src/components/ApprovalButton.tsx` (posts to WO-061's approve endpoint; rendered only while an `approval_request` is pending); `data-testid` values `contact-marker`, `rung-active`, `approve`, `state-timeline`, `ledger-row`, `sitrep-open`, `about-open`

WebSocket event schema (`SCHEMA_VERSION = 1`):
- Envelope: `{v: 1, run_id: str, seq: int (from 0, +1), tick: int, t_s: float (6 dp), type: str, payload: {...}}`.
- `run_started`: `scenario_id, seed, tick_s, n_ticks, speed, toggles[], source: "batch"|"stepped"`.
- `tick`: `contacts: [{contact_id, lat, lon, depth_m, confidence}]`. Observed contacts only; ground truth is never streamed.
- `evidence_row`: `row_id`, `row` (WO-010 `EvidenceRow` JSON: contact_id, t, feature, value or "unknown", unit, source, confidence, kind ∈ fact|assumption|derived, probe_id, derived_from, assumes).
- `posterior`: `contact_id, probs{Hypothesis: float}, entropy, top2_margin, threat_mass, benign_mass, label: "stress-test estimate"`.
- `transition`: `contact_id, from_state, event, to_state: LadderState, guard: {name, result, inputs}|null, audit_row_hash`.
- `rung`: `contact_id, rung: Rung, state: LadderState`. It is emitted only when the rung changes and is derived from `transition`.
- `approval_request`: `contact_id, request_id, action, action_label, scope{}, recommended_rung, expires_t_s, approver`.
- `approval_result`: `request_id, outcome: granted|timeout|backup_paged, token_hash|null`.
- `sitrep`: `contact_id, version, writer: template|llm, sitrep` (WO-042 `Sitrep` JSON, fixed sections, line → evidence ids).
- `run_finished`: `terminal: {contact_id: LadderState}, track_hash, n_events`.
- `error`: `message`.

Consumed:
- WO-010: `soundings.schemas.{Scenario, EvidenceRow, Hypothesis, Rung, LadderState, ToggleName, load_harbor}`.
- WO-011: `soundings.sim.scenario.{resolve_scenario, catalog_paths}`, `soundings.sim.hashing.track_hash`, `soundings.sim.detection.load_detection_params`.
- WO-022/033: `soundings.runner.ScenarioRunner` (`run(llm_roles=...)`), `RunResult.{transitions, evidence, notices, recommendations, sim}`. This WO never edits `runner.py`.
- WO-030: `soundings.investigation.posterior.Posterior`.
- WO-040: `soundings.clock.{Clock, TickClock, WallClock}`, `soundings.ladder.table.RUNG_OF_STATE`.
- WO-041: `soundings.ladder.boundary.load_boundary`, `soundings.ladder.standing_orders.load_verified`.
- WO-042: `soundings.ladder.sitrep.Sitrep`, `soundings.llm.sitrep_writer.template_sitrep` (test only).

## Cut line
Drop these, in order: the ContactList for more than one contact (auto-select the contact with the highest rung), ledger click-to-highlight from SITREP citations, the reconnect `?after=` resume in the UI (the backend keeps it), and the speed slider. Never drop an exit test, the About panel, or the two-colour ledger.

## Model calls
None by default (`use_llm=False`). With `use_llm=true` on POST /api/runs, the roles `probe_planner`, `hypothesis_writer` and `sitrep_writer` replay from fixtures with fallbacks, through the existing guard. Paid run needed? No.
Dependency adds needing owner approval (CLAUDE.md section 3): `websockets` (Python). Plain `uvicorn` has no WebSocket library, so WS /ws/runs works under TestClient but not in `make dev`. If this is refused, add SSE at GET /api/runs/{id}/events with the same envelope (no new dependency), and `useRun.ts` switches to `EventSource`. One-time browser download for the local e2e exit test: `uv run playwright install chromium` (already present on the coordinator laptop; CI skips).

## Rungs touched
All five are displayed. The UI can trigger only `approval(granted)` into AWAITING_APPROVAL; no rung is set.

## Hard rules exercised
§2.1 (`test_nothing_after_handoff`, denylist grep), §2.2 (`test_no_route_sets_rung`), §2.3 (`test_no_model_calls_by_default`), §2.4 (JSONL diff, `test_stream_deterministic`), §2.6 (`test_sitrep_citations_resolve`, ledger colours, posterior label).

## Done
PR: · Merged commit: · Test summary: · STATE.md updated:

## Coordinator addenda (2026-10-16 23:05 PT, binding; see docs/log/DECISIONS.md)
- Approved dependency: Python `websockets` (runtime). Tier-2 recommendation = S05's AWAITING_APPROVAL request. While AWAITING_APPROVAL the UI paces at 1× with a configurable speed factor; ladder timers stay on sim ticks.
