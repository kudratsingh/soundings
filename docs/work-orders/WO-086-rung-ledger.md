# WO-086 — rung-ledger

Phase: 7 · Branch: `wo/086-rung-ledger` · Worktree: `../soundings-worktrees/wo-086-rung-ledger` · Est: 7 h · Owner: coordinator Claude / Kudrat
Depends on: WO-050 (on main: `soundings.eval.policies`), WO-051 (`SoundingsPolicy`, test only; same wave, merge after it), WO-060 (in flight on `origin/wo/060a-ui-core`: event schema, `RunEngine`, `batch_events`, `HarborMap`, `useRun`), WO-061 (`SteppedSource`, live approval path), WO-080 (scenario BT-01), WO-084 (`projection_view`; if WO-084 is dropped at 3:00 PM see the cut line). Not WO-083: separation advisories add no transition, so the ledger never reads them. Wave D.
Parallel start: the `policies.py` helpers, `radar.ts`, `ping.ts` and their unit tests need only main and the WO-060 branch, so they start in Wave C.

## Goal
The watch screen shows a live rung ledger beside the map: contacts resolved at watch, cleared at ask, advisories, holds and handed off. Beside it, in red, sits the counterfactual number "berth closures under the blunt policy". That number comes from WO-050's `bang` baseline, which runs in the background on the same contact stream. The ledger reads the transitions that are already streamed, and the blunt counter reads the same seeded sim run. Both count with the helpers the eval harness uses, so the screen and `artifacts/eval/report.json` agree on the same run by construction.

The map also gets radar-scope styling: a dark basemap (a CSS filter on the existing OSM tiles, so the tile policy and attribution do not change), track trails, 60 s velocity leaders drawn from WO-084's smoothed velocity, and WO-084's 60 s projection ellipse. When a hail is sent, the screen plays a short ping, synthesized with Web Audio and muted by default. The ledger is display only: nothing on it sets a rung, proposes an action, or feeds back into the ladder.

## Scope
In:
- Shared counting helpers in `soundings/eval/policies.py`, added with no change to existing behaviour (see Interfaces).
- `soundings/stream/ledger.py`: `RungLedger`, `BluntCounter`, `LedgerAnnotator`.
- An additive `TickContact.projection: ProjectionView | None = None` (WO-084's model, filled by the engine from the observed track via `projection_view`, never from truth).
- An additive `ledger` event in `soundings/stream/events.py`, with `SCHEMA_VERSION` still 1, appended to `EVENT_TYPES` and to the `StreamEvent` union. `web/src/generated/stream-events.schema.json` is regenerated.
- Engine wiring: the annotator wraps the batch source and the stepped source, and `python -m soundings.stream` emits ledger events too.
- Web: the `RungLedger` panel, the radar styling, `ping.ts` with a mute toggle (muted state kept in `localStorage`), and an extended Playwright smoke.

Out:
- Any new decision logic.
- Any control on the ledger.
- Distinct closure *episodes*: bang merges contacts at an already-closed berth into one episode. The shown count is one closure per contact, which is what the exit test requires. Episodes are an open question.
- New tile providers, audio asset files, and new JS dependencies.

## Spec references
- `docs/SPEC-LADDER.md §1` (states) and `§4` (boundary: `any_closure` is person-approved).
- `docs/BUILD-PLAN.md` Phase 5 (bang baseline) and Phase 6 (one screen).
- `CLAUDE.md §4` (Leaflet, OpenSeaMap) and `§2`.
- Owner directive 2026-10-17 06:05 PDT, WO-086.

## Files expected
- `soundings/eval/policies.py` (helpers only)
- `soundings/stream/ledger.py`
- `soundings/stream/events.py`
- `soundings/stream/engine.py`
- `soundings/stream/__main__.py`
- `web/src/components/RungLedger.tsx`
- `web/src/components/PingToggle.tsx`
- `web/src/radar.ts`
- `web/src/ping.ts`
- `web/src/events.ts`
- `web/src/useRun.ts`
- `web/src/components/HarborMap.tsx`
- `web/src/index.css`
- `web/src/App.tsx` (one marked `WO-086 block`)
- Every edit to a WO-050/WO-060 file (`policies.py`, `events.py`, `engine.py`, `__main__.py`, `useRun.ts`, `events.ts`, `HarborMap.tsx`, `index.css`) sits in a block marked `WO-086`.
- `docs/STATE.md` (WO-086 row only)
- `web/src/generated/stream-events.schema.json`
- `tests/test_ledger.py`
- `tests/test_ledger_integration.py`
- `tests/e2e/test_ledger_smoke.py`

## Exit tests (must all pass with `MODEL_MODE=replay`, no network)
From the owner directive (verbatim):
- [ ] counters match the eval harness numbers for the same run; bang counter increments on every unknown contact; no denylist words in labels; Playwright smoke passes.

Each clause as a command:
- [ ] (directive, verbatim) counters match the eval harness numbers for the same run.
  Command: `uv run pytest tests/test_ledger_integration.py::test_matches_eval_harness`.
  For BT-01 at seed 7 and S05 at seed 11, the test drives `RunEngine` with the stepped source and the default headless approver to `run_finished`. Two equalities must hold:
  - the final `ledger` event's `counts` equal `ledger_from_run(SoundingsPolicy().run(sc, seed))` and equal a `Counter` of `ContactOutcome.max_rung` over that run's `contacts`;
  - `blunt.berth_closures` equals `closure_count(BangPolicy().run(sc, seed).transition_log)`.
  This proves the screen and the eval report count with the same helpers.
- [ ] (directive, verbatim) bang counter increments on every unknown contact.
  Command: `uv run pytest tests/test_ledger.py::test_blunt_steps_per_contact`.
  On BT-01 at seed 7 the blunt counter is a step function with exactly `len(contact_spans(sim))` unit steps. Step *k* lands at the tick of the *k*-th contact's `APPROVAL_REQUESTED` or `COVERED_BY_CLOSURE` transition, and filed contacts are included (bang never reads the registry). This proves no contact escapes the blunt count.
- [ ] (directive, verbatim) no denylist words in labels.
  Commands: `uv run pytest tests/test_ledger.py::test_labels_clean` and `grep -rinEf config/denylist.txt web/src soundings/stream/ledger.py` from the repo root, which must print nothing.
  The test reads the `LEDGER_LABELS`, `PING_LABELS` and tooltip strings exported from `RungLedger.tsx` and `PingToggle.tsx` and runs each through `soundings.schemas.denylist.assert_clean`.
  - The mute toggle is built from conditional rendering and `aria-pressed` only. It never uses the HTML attribute that greys out a control: that word is on `config/denylist.txt`, and `make denylist` greps every `.tsx`.
- [ ] (directive, verbatim) Playwright smoke passes.
  Command: `make e2e`, which runs `tests/e2e/test_ledger_smoke.py` with every non-loopback request aborted. It opens `/?scenario=S05&seed=11&speed=0&tiles=off&source=stepped` and approves when asked. It then expects:
  - `[data-testid=ledger-panel]`, with each `[data-testid=ledger-count][data-bucket=…]` equal to the in-process `ledger_from_run` numbers at `run finished`;
  - `[data-testid=blunt-count]` with its computed colour equal to the `--blunt` token;
  - `.radar` on the map container, with at least one `contact-trail`, one `velocity-leader` and one `projection-ellipse`;
  - `[data-testid=ping-toggle][aria-pressed=false]` on first load. After a click and a reload it is `true`, which proves the state is in `localStorage`;
  - while muted, `data-pings` > 0 after S05's hail row and `window.__soundingsAudioContexts === 0`.
- [ ] `uv run pytest tests/test_ledger.py` (unit tests) proves the following:
  - `rung_ledger` puts each contact in exactly one bucket, its max rung so far (an AWAITING_APPROVAL step keeps the rung that asked, as in `RUNG_OF_STATE`);
  - a contact seen only in `tick` events counts as resolved at watch, matching `ContactOutcome` for a span with no transitions;
  - the bucket total equals the number of distinct contacts seen;
  - a `ledger` event is emitted only when a count changes, right after the `tick` or `transition` event that changed it;
  - `LedgerAnnotator` re-stamps `seq` contiguously from 0, and `parse_event(event_json(e))` round-trips;
  - `BluntCounter.track_hash` must equal the run's `run_finished.track_hash`, or the annotator emits an `error` event and the counter shows "unavailable". It never shows a guess.
- [ ] Integration: `uv run pytest tests/test_ledger_integration.py::test_bt01_story`. BT-01 at seed 7 through the stepped source ends with `handed_off == 1` (the rogue), `cleared_at_ask >= 39`, and `berth_closures >= 40`. It also checks that no `ledger` event precedes the `transition` it reflects. This proves the demo contrast on the real Busy Tuesday run.
- [ ] Determinism (§2.4):
  ```
  for i in a b; do uv run python -m soundings.stream --scenario BT-01 --seed 7 > /tmp/ledger_$i.jsonl; done
  diff /tmp/ledger_a.jsonl /tmp/ledger_b.jsonl
  ```
  `diff` prints nothing, and `grep -c '"type":"ledger"' /tmp/ledger_a.jsonl` is greater than 0. `tests/test_ledger.py::test_thread_timing_irrelevant` repeats the run under `PYTHONHASHSEED=0` and `=1`, and with the `BluntCounter` worker artificially delayed. The event-stream hash is equal each time, which proves the background computation never changes what is streamed or when.
- [ ] Rule proof §2.2: `uv run pytest tests/test_ledger.py::test_display_only`. An AST scan checks three things:
  - `soundings/stream/ledger.py` imports no `soundings.llm`, `anthropic`, or `soundings.ladder.machine` / `guards`;
  - no module under `soundings/ladder`, `soundings/runner.py` or `soundings/investigation` imports `soundings.stream.ledger`;
  - no HTTP route is added.
  The ledger reads the decision stream and cannot write to it.
- [ ] Rule proof §2.1/§2.5: `test_labels_clean` above, plus `bash scripts/denylist-check.sh` exits 0. No label names anything after the handoff, and the blunt label describes the baseline's closure, never a vehicle action.
- [ ] Rule proof §2.6: `tests/test_ledger.py::test_blunt_labeled_counterfactual`. The blunt tooltip contains "counterfactual" and the `config/eval.yaml` approval assumption text, so the red number is never presented as something that happened.
- [ ] `pnpm -C web exec tsc --noEmit && pnpm -C web build` exits 0; `make check` green.
- [ ] Manual (screenshot in PR): BT-01 mid-run with the ledger, the red blunt counter, trails, leaders and an ellipse on the dark map.

## Interfaces
Exposed by WO-086:
- `soundings.eval.policies.StateStep`: a Protocol with `contact_id: str` and `to_state: LadderState`. Eval `Transition` and stream `TransitionPayload` both satisfy it.
- `soundings.eval.policies.LedgerBucket`: a StrEnum with `resolved_at_watch`, `cleared_at_ask`, `advisories`, `holds`, `handed_off`.
- `soundings.eval.policies.LEDGER_BUCKET_OF_RUNG`, which maps `Rung` → `LedgerBucket` one to one.
- `soundings.eval.policies.rung_ledger(steps: Iterable[StateStep], *, seen: Iterable[str] = ()) -> dict[LedgerBucket, int]`, built on the existing `max_rung`.
- `soundings.eval.policies.ledger_from_run(run: PolicyRun) -> dict[LedgerBucket, int]`.
- `soundings.eval.policies.closure_count(transitions: Iterable[Transition]) -> int` and `closure_contacts(transitions) -> tuple[str, ...]`. These count contacts with an `APPROVAL_REQUESTED` step whose `action == CLOSURE_ACTION`, or a `COVERED_BY_CLOSURE` step.
- `soundings.stream.ledger.RungLedger` (`observe(event: StreamEvent) -> bool`, `counts() -> dict[LedgerBucket, int]`).
- `soundings.stream.ledger.BluntCounter(scenario: Scenario, seed: int, *, policy: BangPolicy | None = None)`, with `start()`, `join()`, `closures_at(t_s: float) -> int`, `minutes_at(t_s: float) -> float` and `track_hash`.
- `soundings.stream.ledger.LedgerAnnotator(blunt: BluntCounter)`, with `feed(event: StreamEvent) -> list[StreamEvent]` and `annotate(events) -> list[StreamEvent]`.
- `soundings.stream.events.TickContact.projection` (optional, default None; `SCHEMA_VERSION` stays 1).
- `soundings.stream.events.LedgerPayload` (`counts: {bucket: int}`, `contacts_seen: int`, `blunt: {berth_closures: int, closure_minutes: float, available: bool, label: "counterfactual: bang baseline"}`) and `LedgerEvent` (`type: "ledger"`).
- Web: `web/src/components/RungLedger.tsx` (`LEDGER_LABELS`), `web/src/components/PingToggle.tsx` (`PING_LABELS`), `web/src/ping.ts` (`playPing()`, `PING_MUTED_KEY = "soundings.ping.muted"`), `web/src/radar.ts` (`leaderEnd(lat, lon, ve, vn, s)`, `ellipseLatLngs(center, a_m, b_m, angle_deg, n=36)`).
- `data-testid` values: `ledger-panel`, `ledger-count` (with `data-bucket`), `blunt-count`, `ping-toggle`, `contact-trail`, `velocity-leader`, `projection-ellipse`.

Consumed (all names below are real on `main` or `origin/wo/060a-ui-core` unless marked):
- WO-050: `soundings.eval.policies.{BangPolicy, BangPolicy.plan, contact_spans, Transition, PolicyEvent, PolicyRun, ContactOutcome, CLOSURE_ACTION, max_rung, rung_index, make_policy}`, `soundings.sim.run.run_scenario`, `soundings.sim.hashing.track_hash`.
- WO-051: `soundings.eval.soundings_policy.SoundingsPolicy` (test only).
- WO-040: `soundings.ladder.table.RUNG_OF_STATE`.
- WO-010: `soundings.schemas.{LadderState, Rung, HANDLING_TERMINAL_STATES, ProbeId}`, `soundings.schemas.denylist.assert_clean`.
- WO-060: `soundings.stream.events.{StreamEvent, EVENT_TYPES, STREAM_ADAPTER, parse_event, event_json, export_json_schema, TransitionPayload, TickContact}`, `soundings.stream.engine.{RunEngine, batch_events, BatchReplaySource, register_source, stream_events}`, `web/src/useRun.ts` (`ContactView.track`, `TRACK_POINTS`), `HarborMap` `overlay`.
- WO-061: `soundings.stream.stepped.SteppedSource`.
- WO-080 (not on main): scenario `BT-01` via `resolve_scenario("BT-01")`.
- WO-084 (not on main): `soundings.investigation.kalman.projection_view(track, cfg) -> ProjectionView | None` (`vx`, `vy`, `speed_mps`, `heading_deg`, `proj_lat`, `proj_lon`, `ellipse.{semi_major_m, semi_minor_m, orientation_deg, p}`), `load_kalman_config`. `radar.ts` maps them: leader from `vx`/`vy` (east/north m/s), `ellipseLatLngs(center=(proj_lat, proj_lon), a_m=semi_major_m, b_m=semi_minor_m, angle_deg=orientation_deg)`.

## Cut line
If WO-084 is dropped under the 3:00 PM Wave C rule: `TickContact.projection` is not added, leaders come from `soundings.investigation.features.last_fitted_state`, and the smoke's `projection-ellipse` assertion goes with the ellipse (−0.5 h).
Drop in this order if late:
1. The `closure_minutes` sub-line under the blunt count.
2. A `?style=chart` switch back to the light map (radar becomes the only style).
3. Per-bucket tooltips, except the blunt counterfactual tooltip (an exit test).
4. Trail fading (plain polylines instead).

Never cut an exit test.

## Model calls
None. The ledger and the blunt counter are pure code over the sim and the transition stream. Paid run needed? No.
Dependency adds (logged in DECISIONS.md per owner directive): none (Web Audio, CSS filters and Leaflet polylines are already available).

## Rungs touched
None changed. The panel displays outcomes at watch, ask, slow (advisories), hold and hand to a person; nothing on it can request or set a rung.

## Hard rules exercised
- §2.1, §2.5: `test_labels_clean`, `scripts/denylist-check.sh`.
- §2.2: `test_display_only`.
- §2.3: no model calls; the e2e test aborts non-loopback requests.
- §2.4: JSONL diff and `test_thread_timing_irrelevant`.
- §2.6: `test_blunt_labeled_counterfactual`.

## Done
PR: · Merged commit: · Test summary: · STATE.md updated: yes/no

## Notes for the implementer
- **Bucket definition.** A contact sits in the bucket of its max rung so far, so it moves up as it climbs and the bucket total is monotone. "Cleared at ask" therefore also holds RESOLVED_BENIGN, HAZARD_NOTICE and LOST_VEHICLE terminals at ask. This matches `ContactOutcome.max_rung`, not the gt roll-up in `rung_histogram` (the stream carries no ground truth, so the ledger cannot drop false alarms).
- **When "background" runs.** `BluntCounter.start()` runs `run_scenario` and `BangPolicy.plan` in a worker thread while the runner starts. The engine calls `join()` before it releases tick 0, so streamed content never depends on thread timing. On BT-01 the bang plan takes milliseconds.
- **Ledger events in the stream.** Adding them changes the event count of every stream. Update any WO-060/061 test that pins `n_events`, and keep the existing JSONL determinism checks as they are.
- **Ping trigger.** The ping fires once per distinct `(contact_id, t)` of an `evidence_row` whose `row.probe_id == "acoustic_hail"`. The `AudioContext` is created lazily on the first unmuted ping (browser autoplay rules); the oscillator runs at 1.2 kHz with an 80 ms exponential decay.
- **Dark map.** Apply `filter: invert(1) hue-rotate(180deg) brightness(.7)` to `.leaflet-tile-pane` only, so the seamark overlay and the attribution stay legible. With `tiles=off`, the map container background is `--scope-bg`.

## Coordinator note (WO-060 design pass, 2026-10-17 10:04 PDT)
The radar-scope styling landed early in WO-060 B1 (`wo/060b3-design-pass`): dark scope by default with `?style=chart` keeping the light map, the CSS filter on the OSM base tiles only (`.map-wrap.radar .base-tiles`; seamarks and attribution untouched), `--scope-bg` when `tiles=off`, per-contact fading trails of the last 60 fixes (`contact-trail`), 60 s velocity leaders from the last fixes (`velocity-leader`, `HarborMap.leaderEnd`), and a legend (`legend`). What stays WO-086's: the rung ledger counters (`RungLedger`), `radar.ts` with WO-084's smoothed velocity and projection ellipse (swap `leaderEnd` for it), and `ping.ts` (the WO-062P `sfx.ts` hail ping already sounds on ASKING; reconcile before adding a second one). Colours come from `web/src/theme.css` tokens.
