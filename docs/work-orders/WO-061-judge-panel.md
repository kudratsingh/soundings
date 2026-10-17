# WO-061 — judge-panel

Phase: 6 · Branch: `wo/061-judge-panel` · Worktree: `../soundings-worktrees/wo-061-judge-panel` · Est: 6 h · Owner: frontend / demo (unassigned); coordinator Kudrat
Depends on: WO-010 (`Scenario`, `load_scenario`, `Harbor.zone_at`, `Harbor.nearest_asset`), WO-011 (`catalog_paths`), WO-021 (`config/known_operators.yaml`), WO-040 (`soundings.ladder.ports.ApproverPort`, `ApprovalReply`), WO-041 (`issue_token`, `ApprovalToken`, `token_hash`), WO-042 (`AuditLog`), WO-043 (`ScenarioRunner.start`, `RunSession`, `ApproverPort` injection), WO-060 (`RunEngine`, `register_source`, event schema, `HarborMap`, `useRun`, `ApprovalButton`). Wave 9.

> **Addenda:** the judge panel also shows a QR code to the registry form (owner directive 06:05; section appended below) and the model-path badge "model: live" / "model: fallback" from `GET /api/model_mode` (WO-087).

## Goal
A judge can spawn a contact live. They click a point on the map, choose a behavior (filed / unfiled / closing / loitering / silent) and an acoustic profile, and press Go. The server turns that choice, through a fixed and deterministic mapping, into a scenario file `scenarios/live/L-<YYYYMMDDTHHMMSS>.yaml`, which is gitignored and has an empty `expected` block. It validates the file with the same loader the catalog uses, starts a run through WO-060's `RunEngine`, and returns the `run_id`. The UI switches to that run and shows the first contact within 2 s. Because the run goes through the same pipeline as the catalog, the judge's contact can be reproduced afterwards from the file and its seed (SPEC-SIM §5).

This WO also owns the live approval path (moved from the WO-060 draft so WO-060 need not wait for runner v3): a `SteppedSource` that steps runner v3 once per paced tick, the approve endpoint, and the BUILD-PLAN Playwright smoke that clicks approve and sees `HANDED_OFF`.

Parallel start: `soundings/inject.py` (the mapping and the YAML writer) and its unit tests need only WO-010/011, so they can be built while WO-060 and WO-043 are in flight. The routes, the stepped source and the panel go in once both have merged.

## Scope
In:
- `soundings/inject.py`, which is pure apart from the one write call:
  - `InjectRequest` (`extra="forbid"`): `lat: float`, `lon: float`, `behavior: JudgeBehavior`, `acoustic: AcousticProfile | None` (None means the behavior's default), `seed: int | None`.
  - `JudgeBehavior` is a StrEnum: filed, unfiled, closing, loitering, silent.
  - `AcousticProfile` is a StrEnum: propeller, biologic, quiet.
  - `build_live_scenario(req, stamp: datetime, harbor, known_ops) -> Scenario`.
  - `write_live_scenario(scenario, live_dir) -> Path`.
  - `live_seed(req) -> int`, the first 8 hex digits of `sha256(canonical_json(req))` when `seed` is None.
  - `live_dir()`, which reads env `SOUNDINGS_LIVE_DIR` and defaults to `scenarios/live`.
- The mapping table, which is the same data in code and in the panel's help text. Every row uses `duration_ticks: 600`, `tick_s: 2`, and `hour_of_day` from the stamp in PT. "Nearest critical asset" means the highest-criticality asset within 1 km, and otherwise the nearest one.

| behavior | BehaviorSpec | registry | default acoustic | ground_truth (metadata only) |
|---|---|---|---|---|
| filed | lawnmower over the clicked zone | filed, generated plan `plan-live-<stamp>` covering zone and full window, replies_to_hail | quiet | FILED_ON_PLAN |
| unfiled | lawnmower over the clicked zone | not filed, replies_to_hail with the first modem id in `known_operators.yaml` | quiet | UNFILED_KNOWN_OPERATOR |
| closing | transit to nearest critical asset, 1.0 m/s | not filed, no reply | propeller (mid band) | UNAUTHORIZED_UUV |
| loitering | orbit nearest critical asset, radius 40 m | not filed, no reply | propeller (low band) | UNAUTHORIZED_UUV |
| silent | lawnmower_then_drift, fault_tick 60 | filed with generated plan, `silent_after_tick: 60` | quiet | FILED_OFF_PLAN |

- Acoustic profiles map onto WO-010 `AcousticSpec`:
  - propeller: `propeller_tonal: true`, band as listed in the table.
  - biologic: `propeller_tonal: false`, `biologic_signature: true`.
  - quiet: both false.
- A point outside every zone polygon returns 422 "outside the declared security zones". This keeps the system inside its declared scope (CLAUDE.md §6: no surveillance outside declared zones).
- The id is `L-<stamp>`, matching the WO-010 pattern `^L-\d{8}T\d{6}$`. If the file already exists, the stamp moves forward 1 s until a free name is found, which is deterministic for a given directory state. Writing goes through a temp file and `os.replace`. The server makes every path, so a request cannot name one.
- Live `expected`: an empty `expected` for `^L-` ids needs `Expected.terminal_state` to be optional for those ids in WO-010's `soundings/schemas/scenario.py`. WO-010 is still pending, so the coordinator is asked to fold that into WO-010 (open question). This WO does not edit WO-010's files; if WO-010 merges without it, the coordinator lands a one-commit amendment before this WO starts. `catalog_paths()` already globs `S[0-9][0-9]-*.yaml` (WO-011) and GET /api/scenarios lists only `^S\d{2}$` ids (WO-060).
- `soundings/stream/stepped.py`: `SteppedSource`, registered with WO-060's `register_source("stepped", ...)`. It calls WO-043's `ScenarioRunner.start(approver=UiApprover(...))` and `RunSession.step()` once per paced tick, and emits `transition`, `rung`, `approval_request`, `approval_result` and `sitrep` events from the v3 step records. `UiApprover` implements WO-040's `ApproverPort` and queues replies posted through the endpoint. Pacing is WO-060's engine (1× while AWAITING_APPROVAL).
- `soundings/stream/ladder_adapter.py`: besides WO-041's `approver_sim.py`, the only caller of `soundings.ladder.approval.issue_token`. `grant(request, approver, now_s) -> ApprovalToken` builds the token for exactly the requested scope (`channel="ui"`).
- `soundings/api_routes/approve.py`: POST /api/runs/{run_id}/approve with `ApproveBody` (`request_id: str`, `approver: str = "Watch Officer"`, `extra="forbid"`). 404 unknown run, 409 no pending request or a second approve, 422 on any extra field.
- `soundings/api_routes/inject.py`: POST /api/inject takes an `InjectRequest` and returns `{run_id, scenario_id, path, seed}`. Both routers are mounted in `soundings/api.py` inside a block marked `# --- WO-061: inject and approve ---` (shared file). The run is started with `RunEngine.start(scenario, seed, speed=None, source="stepped")`.
- `web/src/components/JudgePanel.tsx`, collapsed by default and opened by a "Judge" tab or `?judge=1`:
  - "Place contact" arms map click through `HarborMap.onMapClick` and shows a pin overlay.
  - Five behavior radio buttons, labelled "Filed survey", "Unfiled, answers hail", "Closing on an asset", "Loitering near an asset", "Filed, then silent".
  - Acoustic select: "Propeller tonal", "Biologic clicks", "Quiet", "Behavior default".
  - A "Go" button. It shows only once a pin is placed; no greyed-out state is used (WO-060 explains the denylist trap). After a POST it calls `setRunId` so the screen follows the new run.
- `data-testid` values: `judge-open`, `judge-place`, `judge-behavior-<name>`, `judge-acoustic`, `judge-go`, `judge-error`.

Panel states and errors (all inside the panel; the rest of the screen keeps showing the current run):
- Idle: "Place contact" is shown. Placing: the cursor becomes a crosshair, and the next map click drops the pin and leaves placing mode. Placed: the behavior and acoustic controls appear, and Go appears.
- After Go: the panel shows "Started L-<stamp> (seed n)" with the file path, so a judge can ask for a replay later. The panel collapses, and the screen follows the new run.
- 422 outside the zones: `judge-error` reads "That point is outside the declared security zones. Click inside a zone." The pin stays so the judge can move it.
- A network or 5xx error shows the message and keeps the current run; nothing is retried automatically.
- Repeat use: each Go writes a new file and a new run. The engine's cap of 4 live runs evicts the oldest finished run first, and when all 4 are still active a fifth start returns 429 "too many live runs".

Reproducing a judge contact afterwards (for the Q&A or a twist proof):
- `uv run python -m soundings.stream --scenario scenarios/live/L-<stamp>.yaml --seed <seed>` prints the identical event stream.
- To keep one permanently, copy the file to `scenarios/` under a new `S\d\d` id and add an `expected` block. That is a catalog change and needs owner approval (CLAUDE.md §3).

Out: free-form scenario editing, a speed or seed control in the panel, toggles from the panel (twists stay in the catalog), multi-contact injection, setting an `expected` block, persisting live runs beyond the gitignored file, and any field that names a rung or state.

## Spec references
`docs/SPEC-SIM.md §4` (scenario file shape), `§5` (judge injection); `docs/SPEC-INVESTIGATION.md §1` (hypothesis ids used as metadata); `CLAUDE.md §2, §6`; `docs/BUILD-PLAN.md` Phase 6.

## Files expected
- `soundings/inject.py`, `soundings/api_routes/inject.py`, `soundings/api_routes/approve.py`, `soundings/stream/stepped.py`, `soundings/stream/ladder_adapter.py`, `soundings/api.py` (marked block only)
- `web/src/components/JudgePanel.tsx`, `web/src/App.tsx` and `web/src/api.ts` (one marked block each: the panel mount and `postInject`)
- `tests/test_inject.py`, `tests/test_api_inject.py`, `tests/test_api_approve.py`, `tests/test_stepped_source.py`, `tests/e2e/test_judge_panel.py`, `tests/e2e/test_ui_approval.py`
- `docs/STATE.md`

## Exit tests (must all pass with `MODEL_MODE=replay`, no network)
- [ ] (BUILD-PLAN, verbatim) `pnpm build` green; Playwright smoke: load page, see a contact appear, see the rung indicator change, click approve on a Tier-2 recommendation, see `HANDED_OFF`. Concretely: `pnpm -C web exec tsc --noEmit && pnpm -C web build` exits 0, and `make e2e` passes `tests/e2e/test_ui_approval.py`. It opens `/?scenario=S05&seed=11&speed=50&tiles=off&source=stepped`, waits for `[data-testid=contact-marker]`, then for `[data-testid=rung-active]` to change from `watch`, clicks `[data-testid=approve]` once it appears (S05's AWAITING_APPROVAL request, which goes to a person), and expects `HANDED_OFF` in `[data-testid=state-timeline]`. A `page.route("**/*")` handler aborts every non-loopback request, so the test proves the approval path works offline.
- [ ] (BUILD-PLAN, verbatim) Judge panel: place a contact by clicking the map, choose behavior (filed / unfiled / closing / loitering / silent), press go; the investigation starts within 2 s and the UI follows it.
  - Concretely, `make e2e` runs `tests/e2e/test_judge_panel.py`, with the server's `SOUNDINGS_LIVE_DIR` set to a tmp dir. The test opens `/?tiles=off&judge=1`, clicks `judge-open`, then `judge-place`, then the map pixel of the fuel-berth approach, then `judge-behavior-closing`, then `judge-go`, and records `t0`.
  - It then expects a `contact-marker` and a `transition` entry in `state-timeline` with `t - t0 <= 2.0 s`, and `rung-active` changing from `watch` within the run.
  - Non-loopback requests are aborted, so the panel works offline.
- [ ] Rule proof §2.5 (the BUILD-PLAN denylist line itself is WO-060's, and its CI grep covers these files): `grep -rinEf config/denylist.txt web/src/components/JudgePanel.tsx soundings/inject.py soundings/api_routes soundings/stream` prints nothing.
- [ ] `uv run pytest tests/test_stepped_source.py::test_s05_approval_flow` (moved from the WO-060 draft) — S05 at seed 11 through `RunEngine` with the stepped source passes HOLDING, emits `approval_request`, and after `RunEngine.approve` yields HANDED_OFF then WATCH_AFTER_HANDOFF; its final state and audit chain head equal `ScenarioRunner.run()` with a scripted approver granting at the same tick. Proves the live path decides exactly what the batch runner decides.
- [ ] `uv run pytest tests/test_api_approve.py` — approve returns 404 for an unknown run, 409 with no pending request, 409 on a second approve, and 422 when the body carries `rung`, `scope` or `action`; a granted approval's `token_hash` appears in the audit chain and `uv run python -m soundings.audit verify` exits 0.
- [ ] `uv run pytest tests/test_inject.py` covers the following:
  - Every behavior × acoustic pair (5 × 4) builds a `Scenario` that `load_scenario` accepts after a write and read-back.
  - Each behavior's BehaviorSpec type, registry flags and default acoustic match the table above.
  - A point outside all zones raises the scope error.
  - Same request + same stamp gives byte-identical YAML.
  - A name collision moves the stamp forward 1 s.
  - The written `expected` is `{}`.
  - `live_seed` is stable.
  - This proves the mapping is exactly the documented table.
- [ ] `uv run pytest tests/test_api_inject.py::test_inject_starts_within_2s` is an integration test using TestClient and WS. A closing request at a point 400 m from `fuel_berth_2` returns `run_id`. The first `tick` and the first `transition` event arrive within 2.0 s (`time.monotonic`), and the run reaches HOLDING or `approval_request` at speed 0. This proves the server half of the 2 s promise with the real runner.
- [ ] `uv run pytest tests/test_api_inject.py::test_inject_rejects` checks the rejections. Extra fields (`rung`, `expected`, `path`, `state`) return 422, an out-of-zone point returns 422, and an unknown behavior returns 422. This proves a judge can describe a contact but cannot steer a decision.
- [ ] `uv run pytest tests/test_inject.py::test_catalog_excludes_live` checks that, with a live file present, `catalog_paths()` and GET /api/scenarios list only S01..S08. This proves judge files never reach the eval `--suite catalog` run or the demo catalog.
- [ ] Determinism (the judge's contact is reproducible): `tests/test_api_inject.py::test_live_file_replays` re-runs the written file with `python -m soundings.stream --scenario <written path> --seed <seed>` twice. The two JSONL outputs are identical, and they equal the events the API streamed (sim ticks only).
- [ ] `git check-ignore -q scenarios/live/L-20261017T120000.yaml` exits 0, which proves judge files stay out of git.
- [ ] Rule proof §2.2: `test_inject_rejects` above shows the only inputs are a position, a behavior and an acoustic profile; `tests/test_api_approve.py::test_scope_mismatch_blocked` posts a token whose scope differs from the request, the guard blocks it, and the run stays AWAITING_APPROVAL. The button only asks; the guard decides.
- [ ] Determinism (live path): `uv run python -m soundings.stream --scenario S05 --seed 11 --approve-first > /tmp/a.jsonl; uv run python -m soundings.stream --scenario S05 --seed 11 --approve-first > /tmp/b.jsonl; diff /tmp/a.jsonl /tmp/b.jsonl` prints nothing (moved from the WO-060 draft).
- [ ] Rule proof §2.1: `tests/test_inject.py::test_vocabulary` checks the following. `JudgeBehavior`, `AcousticProfile` and every panel label string (exported from `JudgePanel.tsx` as `JUDGE_LABELS`, then copied into a test fixture by `tests/test_inject.py` reading the file) pass `soundings.schemas.denylist.assert_clean`. No label names anything past the handoff.
- [ ] `pnpm -C web exec tsc --noEmit && pnpm -C web build` exits 0; `make check` green.
- [ ] Manual (screenshots in PR): the panel open with a pin placed, the screen following the injected run at HOLDING, and S05 at AWAITING_APPROVAL with the Approve button showing scope and countdown.

## Interfaces
Exposed by WO-061:
- `soundings.inject.InjectRequest`, `soundings.inject.JudgeBehavior`, `soundings.inject.AcousticProfile`, `soundings.inject.BEHAVIOR_TABLE`, `soundings.inject.build_live_scenario`, `soundings.inject.write_live_scenario`, `soundings.inject.live_seed`, `soundings.inject.live_dir`, `soundings.inject.OutsideZonesError`
- `soundings.api_routes.inject.router`; HTTP POST /api/inject returns `{run_id, scenario_id, path, seed}`
- `soundings.stream.stepped.SteppedSource`, `soundings.stream.stepped.UiApprover`, `soundings.stream.ladder_adapter.grant(request: ApprovalRequestEvent, approver: str, now_s: float) -> ApprovalToken`
- `soundings.api_routes.approve.router`, `soundings.api_routes.approve.ApproveBody`; HTTP POST /api/runs/{run_id}/approve
- Web: `web/src/components/JudgePanel.tsx` (`JUDGE_LABELS`), `web/src/api.ts` `postInject(req) -> Promise<{run_id, scenario_id, path, seed}>`
- Files: `scenarios/live/L-*.yaml` (gitignored)

Consumed:
- WO-010: `soundings.schemas.{Scenario, ScenarioContact, BehaviorSpec, AcousticSpec, ContactRegistrySpec, RegistryPlan, KnownOperator, Expected, Hypothesis, load_harbor}`, `soundings.schemas.denylist.assert_clean`, `soundings.canonical.canonical_json`.
- WO-010 (also): `soundings.schemas.scenario.load_scenario`, `Harbor.zone_at`, `Harbor.nearest_asset`.
- WO-011: `soundings.sim.scenario.catalog_paths`.
- WO-021: `config/known_operators.yaml`.
- WO-041: `soundings.ladder.approval.{ApprovalToken, issue_token, token_hash}`.
- WO-042: `soundings.ladder.audit.AuditLog` (`verify`).
- WO-043: `soundings.runner.ScenarioRunner.start`, `RunSession` (`step`, `done`, `pending_approvals`, `result`), `StepRecord`; WO-040: `soundings.ladder.ports.{ApproverPort, ApprovalReply}`.
- WO-060: `soundings.stream.engine.{get_engine, RunEngine, RunSource, register_source}`, `soundings.stream.events` (approval and sitrep event types), `HarborMap.onMapClick`, `useRun`, `ApprovalButton`, CLI `python -m soundings.stream`.

## Cut line
Drop these, in order: the "Behavior default" acoustic option (always use the default), the pin overlay (place directly on click), and the `?judge=1` URL param (the tab only). Never drop an exit test, the out-of-zone check, or the 2 s follow.

## Model calls
None (the run engine defaults to `use_llm=False`). Paid run needed? No. Dependency adds needing owner approval (CLAUDE.md section 3): none.

## Rungs touched
None directly. An injected contact enters at watch, and the ladder takes it wherever its guards allow. The approve endpoint delivers `approval(granted)` into AWAITING_APPROVAL; the guard decides.

## Hard rules exercised
§2.1 (`test_vocabulary`, denylist grep), §2.2 (`test_inject_rejects`), §2.4 (`test_live_file_replays`, byte-identical YAML), and CLAUDE.md §6 scope (out-of-zone 422 in `test_inject.py`).

## Done
PR: · Merged commit: · Test summary: · STATE.md updated:

## Coordinator addenda (2026-10-16 23:05 PT, binding; see docs/log/DECISIONS.md)
- If `soundings/schemas/scenario.py` on main still requires `Expected.terminal_state` for every id, make it optional when the scenario id matches `^L-` (judge-injected live scenarios) with a unit test in tests/test_schemas.py; this one-line schema edit is approved for WO-061.

## Coordinator addenda (QR code, owner directive 06:05 PDT)

Binding. Recorded in `docs/log/DECISIONS.md`. Est: +3 h on top of WO-061. Depends on: WO-061 itself (`JudgePanel.tsx`, `JUDGE_LABELS`, `tests/test_inject.py::test_vocabulary`), WO-020 (registry router and `/registry` form), WO-060 (in flight on `origin/wo/060a-ui-core`: `HarborMap` `overlay`, `api.ts`, `GET /api/harbor`, `tests/e2e/conftest.py`), WO-081 (conflicts on `PlanView`), WO-082 (CP-SAT proposal with accept/decline). Wave C; the QR part merges after WO-082. If WO-082 is dropped under the 3:00 PM rule, `ProposalCard` shows the WO-081 conflict line only and `test_conflicted_plan_shows_proposal` narrows to the conflict marker; owner to confirm (the directive clause names the proposal).

### What is added
The judge panel shows a QR code that opens the registry form, so a judge can file a plan from a phone. The plan they file must appear on the map within 2 s. If the plan is conflicted, the screen shows WO-082's CP-SAT proposal with Accept and Decline. The operator decides, and nothing changes until they accept. This is the registry's ordinary-day path. It proposes slots and never sets a rung.

### Backend
- **Router.** `soundings/api_routes/qr.py`, `router` mounted in `soundings/api.py` before the `web/dist` mount, inside a marked `WO-061 QR` block.
- **SVG route.** `GET /api/qr?target=/registry` returns `image/svg+xml` and sends the encoded URL in the `X-QR-Payload` response header. `target` must be in `QR_TARGETS = frozenset({"/registry"})`; anything else returns 422, so the endpoint cannot be turned into an open link generator.
- **Info route.** `GET /api/qr/info?target=/registry` returns `{url, loopback: bool}`. The panel prints the URL under the code and warns when `loopback` is true ("a phone cannot reach localhost; open the screen on the LAN address").
- **Base URL.** `qr_base_url(request)` takes the env value `SOUNDINGS_PUBLIC_BASE_URL` (for example `http://192.168.1.20:8000`) when it is set. Otherwise it uses `str(request.base_url)`, so the host comes from the request. It never probes the network.
- **Rendering.** `qr_svg(url: str) -> bytes` is `segno.make(url, error="m", micro=False)` saved as SVG with `scale=4`, `border=2` and `xmldecl=False`. This gives byte-identical output for the same URL.
- **Make target.** `make dev-lan` runs uvicorn on `0.0.0.0` for the demo laptop. `make dev` stays on loopback (see the open question on LAN exposure).

### Frontend
- **QR in the panel.** `JudgePanel.tsx` renders `<img data-testid="judge-qr" src="/api/qr?target=/registry">`, with the URL text in `[data-testid=judge-qr-url]` (carrying `data-url`). Every new string goes into `JUDGE_LABELS`.
- **Plan polling.** `web/src/usePlans.ts` polls `GET /api/plans?status=active` every `PLAN_POLL_MS = 1000`. Polling is chosen over pushing plan events into the run WebSocket because a plan filed from a phone arrives in wall-clock time. If it were put into the seeded run stream, two runs would no longer be byte-identical (§2.4).
- **Plan layer.** `web/src/components/PlanLayer.tsx` (passed to `HarborMap` as `overlay`) draws each plan's `area` zone polygon, dashed, with a centroid marker `[data-testid=plan-marker][data-plan=<plan_id>][data-conflicted=true|false]`.
- **Proposal card.** `web/src/components/ProposalCard.tsx` appears for a conflicted plan. It shows WO-082's proposed shifts ("P-… start 14:00 → 14:40") or WO-082's explicit "no feasible slot", naming the binding constraint. Its buttons are `[data-testid=proposal-accept]` and `[data-testid=proposal-decline]`, and they call WO-082's accept and decline endpoints. The card is labelled "proposal: the operator accepts or declines".
- **Form on a phone.** `registry.html` already carries a viewport meta tag. Check it at 390×844, and make the WO-081 conflict line ("conflicts with <plan> 14:00-16:00") wrap rather than overflow.

### Files expected
- `soundings/api_routes/qr.py`; `soundings/api.py` (block `# --- WO-061 QR ---`); `Makefile` (`dev-lan` target)
- `web/src/usePlans.ts`, `web/src/components/PlanLayer.tsx`, `web/src/components/ProposalCard.tsx`; `web/src/components/JudgePanel.tsx` and `web/src/App.tsx` (inside WO-061's existing marked blocks)
- `soundings/registry/static/registry.html` (block `<!-- WO-061 QR -->`, CSS wrap only)
- `pyproject.toml` (the `segno` line with a trailing `# WO-061 QR` comment; its own `[[tool.mypy.overrides]]` table for `segno.*` if stubs are missing), `uv.lock`
- `tests/test_api_qr.py`, `tests/e2e/test_judge_qr.py`; `docs/log/DECISIONS.md` (segno row; `make dev-lan` LAN exposure); `docs/STATE.md` (WO-061 row only)

### Exit tests (must all pass with `MODEL_MODE=replay`, no network)
- [ ] (directive, verbatim) The judge panel also shows a QR code to the registry form so a judge can file a plan from a phone; the filed plan must appear on the map within 2 s and (if conflicted) show the CP-SAT proposal.
- [ ] `make e2e` runs `tests/e2e/test_judge_qr.py::test_filed_plan_on_map_within_2s` with non-loopback requests aborted and `SOUNDINGS_DB` set to a temporary file. The test:
  1. opens `/?tiles=off&judge=1` and clicks `judge-open`;
  2. asserts `[data-testid=judge-qr]` has loaded with `naturalWidth > 0`;
  3. reads `judge-qr-url[data-url]`, which must end in `/registry` on the live server;
  4. opens that URL in a second page with a 390×844 viewport, fills the form and submits it, recording `t0` at the 201 response;
  5. waits on the first page for `[data-testid=plan-marker][data-plan=<id>]` and asserts the elapsed time is at most 2.0 s.

  This proves the phone-to-map path end to end.
- [ ] `tests/e2e/test_judge_qr.py::test_conflicted_plan_shows_proposal`:
  - Setup: file 3 plans through the API in one zone, with overlapping depth and window (more than the default capacity of 2), then file a fourth from the phone page.
  - Proposal card: the marker shows `data-conflicted=true`, and `proposal-card` appears within 2 s with WO-082's shift text.
  - Decline: clicking `proposal-decline` leaves `GET /api/plans/<id>` byte-equal to its value before the click.
  - Accept: refiling and clicking `proposal-accept` updates the plan's window and turns the marker to `data-conflicted=false` within 2 s.

  This proves that nothing changes until the operator accepts.
- [ ] `uv run pytest tests/test_api_qr.py` checks the following:
  - the response is 200 with `image/svg+xml`, and `xml.etree.ElementTree.fromstring` parses it with an `{http://www.w3.org/2000/svg}svg` root;
  - the payload given to the generator is exactly `<base>/registry`, checked by monkeypatching `soundings.api_routes.qr.segno.make` to record its argument (no pure-Python QR decoder is a dependency, so the test checks the input, not a decode);
  - the `X-QR-Payload` header matches that payload;
  - the body equals `qr_svg("<base>/registry")`;
  - `target=/api/plans`, `target=https://example.com` and an empty target each return 422;
  - with `SOUNDINGS_PUBLIC_BASE_URL` set, the env value is used; with it unset and a `testserver` host, the request host is used; `info.loopback` is true for `127.0.0.1` and `localhost`.
- [ ] Determinism: `uv run pytest tests/test_api_qr.py::test_svg_byte_identical` renders the same URL twice in-process and once in a subprocess under `PYTHONHASHSEED=1`, with equal sha256. The existing `python -m soundings.stream` JSONL diff (above) still prints nothing, which proves plan polling added nothing to the run stream.
- [ ] Rule proof §2.2:
  - `tests/test_api_qr.py::test_qr_display_only`: an AST scan shows `soundings/api_routes/qr.py` imports nothing from `soundings.ladder`, `soundings.llm` or `anthropic`.
  - `tests/e2e/test_judge_qr.py::test_conflicted_plan_shows_proposal`: the proposal card has no control except accept and decline, and neither touches a run.
  - A plan filed from the phone changes the registry only. The ladder still decides every contact.
- [ ] Rule proof §2.1/§2.5: `grep -rinEf config/denylist.txt soundings/api_routes/qr.py web/src/components/JudgePanel.tsx web/src/components/PlanLayer.tsx web/src/components/ProposalCard.tsx web/src/usePlans.ts` prints nothing; `test_vocabulary` covers the new `JUDGE_LABELS`. No string implies route planning or directing a vehicle; the card says "proposes", "proposal" and "operator accepts".
- [ ] `pnpm -C web exec tsc --noEmit && pnpm -C web build` exits 0; `make check` green.

### Interfaces
Exposed:
- WO-061 `soundings.api_routes.qr.router`, `.QR_TARGETS`, `.qr_base_url(request) -> str`, `.qr_svg(url: str) -> bytes`
- HTTP routes: `GET /api/qr?target=`, `GET /api/qr/info?target=`
- Make target: `make dev-lan`
- Web: `web/src/usePlans.ts` (`usePlans()`, `PLAN_POLL_MS`), `web/src/components/PlanLayer.tsx`, `web/src/components/ProposalCard.tsx`
- `data-testid` values: `judge-qr`, `judge-qr-url`, `plan-marker`, `proposal-card`, `proposal-accept`, `proposal-decline`

Consumed:
- WO-020 (on main): `soundings.registry.router.{router, registry_page}` and the `GET /api/plans?status=active` route.
- WO-020 (on main): `soundings.registry.models.{PlanView, PlanStatus}` (`PlanView.area` is a zone id).
- WO-060 (in flight, `origin/wo/060a-ui-core`): `Harbor.zones[].polygon` via `GET /api/harbor`, `HarborMap` `overlay` prop, `tests/e2e/conftest.py`.
- WO-081 (not on main; names per WO-081): the `conflicted` status and the `conflicts[]` on `PlanView`.
- WO-082 (not on main; names per WO-082): the proposal GET route and the accept/decline POST routes for one plan. The coordinator reconciles exact paths in WO-082's Notes for downstream.

### Rungs touched and hard rules
None. The QR route and plan layer touch the registry only. §2.1/§2.5: denylist grep and `test_vocabulary`; §2.2: `test_qr_display_only`; §2.4: `test_svg_byte_identical` and the unchanged stream JSONL diff.

### Model calls and dependencies
None. Dependency adds (logged in DECISIONS.md per owner directive): `segno>=1.6.6` (runtime, pure Python, no transitive dependencies on 3.12; chosen over browser-side QR so `package.json` is unchanged).

### Cut line
Drop in this order:
1. `GET /api/qr/info`'s loopback warning text (keep the URL text).
2. The dashed zone outline (keep the centroid marker).
3. `make dev-lan` (set `SOUNDINGS_PUBLIC_BASE_URL` by hand).

Never cut an exit test.

### Notes
- Venue Wi-Fi often isolates clients. Rehearse on a phone hotspot with the laptop joined to it, and set `SOUNDINGS_PUBLIC_BASE_URL` to the laptop's hotspot address.

## Coordinator note (WO-060 design pass, 2026-10-17 10:04 PDT)
Where things mount in the case-centric layout (`wo/060b3-design-pass`):
- Approval card: `web/src/components/ApprovalButton.tsx` renders in the ladder rail at the bottom of the right column. While an `approval_request` is pending it shows `approval-card` (action label, scope, approver, expiry, request id); put the real `approve` button there, replacing the `approval-placeholder` span. With nothing pending it is one quiet line; keep the `approval-placeholder` text or update the part A smoke in the same PR. The case's LADDER step (`[data-step=ladder]`) shows the request and its result from the stream.
- Judge panel: mount it in the `{/* --- WO-061 block (judge panel) --- */}` block of `App.tsx` as an overlay (fixed position, z-index under the presenter bar, above `.rail`), or as a drawer like `SitrepDrawer`; the right rail has no spare height. A top-bar button belongs beside `sitrep-open`; the bar is one line at 1440 px, so keep it short.
- Map layers (plans, proposals) still go through `HarborMap`'s `overlay` prop; it is shared with the WO-088 responder layer, so wrap both in a fragment.
