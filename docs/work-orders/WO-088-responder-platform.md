# WO-088 — responder-platform

Phase: 7 · Branch: `wo/088-responder-platform` · Worktree: `../soundings-worktrees/wo-088-*` · Est: 2.5 h · Owner: coordinator Claude / Kudrat
Depends on: WO-011, WO-012, WO-031, WO-033, WO-041, WO-042, WO-060, WO-062P
Directive: owner, 2026-10-17 07:45 PDT. **The owner's chat numbered this directive WO-087; CLAUDE.md section 8 A1 maps it to WO-088** (WO-087 is bounded live mode). CLAUDE.md section 8 A2 records the rules: the responder is a mobile probe at rung *ask*, and the Phase 7 line "no vehicle commands" refers to contacts and operators' vehicles, not the platform's own sensor carrier.
Split (three agents in parallel on the Interfaces contract below; the first to merge sets any detail left open, the others adapt on rebase):
- Part A `wo/088a-responder-sim`: this WO, `config/responder.yaml`, `soundings/sim/responder.py`, `tests/test_responder.py`, `tests/test_responder_catalog.py`, the `docs/STATE.md` row.
- Part B: probes `responder_look` / `responder_search_box`, the `ResponderPort`, EIG terms, budgets and boundary rows in `config/ladder.yaml`, guards, `RESPONDER_POSITION_REQUEST` in the WO-032 guard, S02 search beat and S05/S06 look beat.
- Part C: run-engine `responder` event, `ResponderLayer` (icon, trail, standoff circle, battery bar, "looking" state), the dispatch SFX, presenter beats, demo script and vo-script.

## Goal
A small simulated responder vehicle, one per harbor, docked at a pier, that the ladder can send to **look** at a contact. It is a mobile probe, not an action against anything. It keeps a 25 m standoff, stays inside the declared security zones, never places itself between a contact and an asset, never closes faster than the contact, and returns to dock. The planner may request a look, never a position. The call is still a person's.

## Scope
In:
- Sim (part A): position, heading, speed (1.5 m/s), battery (minutes), states DOCKED -> TRANSIT -> ON_STATION -> RETURNING. Travel time from real distance (routes inside the geofence). Geofence = declared security zones only. Standoff radius 25 m: it orbits at standoff, never inside. If the contact moves, it re-stations (hysteresis 5 m); it never closes faster than the contact and never places itself between the contact and an asset. Battery drains with a cubic law in speed; it returns to dock when the battery would fall below the return cost. One dispatch per contact; a busy responder rejects a second dispatch (a second vehicle is a person's call).
- Probes (part B, allowlist, rung ask): `responder_look` returns a `visual_class` evidence row (confidence scaled by visibility and range) and close-range acoustic features once ON_STATION; latency = transit time; cost = battery minutes. `responder_search_box`: lawnmower over a LOST_VEHICLE drift box; returns found/not_found with position.
- Boundary table rows (part B, `config/ladder.yaml`, additive): dispatch to look inside geofence and standoff = platform decides alone (rung ask); leaving the geofence, standoff < 25 m, or any second vehicle = person approves; nothing after handoff. Guards enforce all three in `guards.py`; the planner can request a look, never a position.
- Investigation (part B): the planner may pick `responder_look` when expected information gain per cost beats static probes (it usually will for S05/S06 and for `fog_no_camera`). Budget: one dispatch per contact.
- UI (part C): responder icon with a trail, standoff circle, battery bar, "looking" state; SFX: short two-note on dispatch. Presenter captions: "The platform goes to look. It never goes closer than 25 m. The call is still a person's."
- Scenarios (part B/C): S02 gains a search beat (the responder finds the glider in the drift box). S05/S06 gain the look beat before the hold. Demo script and vo-script updated.

Out:
- Any action against a contact or an operator's vehicle; any state, tool, word or UI element after `HANDED_OFF` other than watching (CLAUDE.md 2.1).
- Any position, standoff or geofence chosen by a model (CLAUDE.md 2.2; the guard rejects and logs it).
- Hydrodynamics, obstacle avoidance beyond the geofence, multiple responders, docking/charging cycles, comms loss.
- Leaving the declared security zones (CLAUDE.md 6: no surveillance outside declared zones).
- Any threshold in `config/ladder.yaml` (only additive boundary rows and the budgets key).

## Spec references
CLAUDE.md §2 (2.1, 2.2, 2.4, 2.5), §6, §8 A1–A2; `docs/SPEC-INVESTIGATION.md` §5–6 (probes, planner); `docs/SPEC-LADDER.md` §3–4 (boundary table, guards); `docs/SPEC-SIM.md` §1 (harbor map, local frame).

## Files expected
- `config/responder.yaml` (A)
- `soundings/sim/responder.py` (A)
- `tests/test_responder.py`, `tests/test_responder_catalog.py` (A)
- `soundings/schemas/enums.py` (B: ProbeId values), `soundings/investigation/probes.py`, `planner.py` (B, WO-088 blocks)
- `config/ladder.yaml` (B: additive boundary rows, `budgets.responder_dispatches_per_contact: 1`)
- `soundings/ladder/guards.py`, `soundings/llm/guard.py` (B, WO-088 blocks), `docs/log/VIOLATIONS.md` format
- `soundings/ui/` or `soundings/stream/` run engine, `web/src/` ResponderLayer, `web/src/App.tsx` WO-088 block, `web/src/sfx.ts`, `scripts/gen_sfx.py`, `web/public/sfx/responder_dispatch.wav`, `docs/demo-script.yaml` (C)
- `docs/STATE.md` row (A)

## Exit tests (must all pass with `MODEL_MODE=replay`)
Directive exit lines (verbatim):
- [ ] standoff never violated across catalog x seeds 1-5 (property test on the trail)
- [ ] geofence never violated
- [ ] responder_look returns no evidence before ON_STATION
- [ ] S02 search finds the vehicle when the box contains truth
- [ ] a planner request to go inside standoff or outside the geofence is rejected and logged in VIOLATIONS.md
- [ ] denylist clean (use 'look', 'station', 'standoff'; never chase/follow-into/close-with)
- [ ] determinism

Extended list:
- [ ] A `pytest tests/test_responder_catalog.py` — every catalog scenario x seeds 1-5, each ground-truth contact's truth path as the contact track (grading only): every trail point inside a declared security zone; while it has a target, never inside the 25 m standoff and never in the corridor between the contact and its nearest asset; every contact reaches ON_STATION; same seed, byte-identical trail.
- [ ] A `pytest tests/test_responder.py` — state machine DOCKED -> TRANSIT -> ON_STATION -> RETURNING -> DOCKED; arrival within the ETA; travel time = route length / 1.5 m/s (straight legs equal real distance / speed); station point on the side away from the nearest asset; it sweeps its station arc; opening range -> holds (speed 0) then returns beyond look range (no following); closing contact -> re-stations, keeps standoff, and its own move never gives up more range than the contact travelled (plus a 0.5 m orbit chord); hypothesis property test over random contact paths (standoff, geofence, corridor); cubic battery law (8x when speed doubles), no drain while docked, returns before the battery runs out, dispatch rejected when the battery cannot cover the round trip; busy -> second dispatch rejected ("a person's approval"); one dispatch per contact; no station point inside the geofence -> rejected; lawnmower search covers the box and returns; config strict (standoff < 25 m rejected), every config value labeled "demo value"; no wall clock or randomness imported; names and text clean of the denylist and of chase/follow-into/close-with.
- [ ] B probe tests: `responder_look` pending (no rows) before ON_STATION, `visual_class` + close-range acoustic rows after; `responder_search_box` found/not_found with position; latency = travel time; cost = battery minutes; one dispatch per contact budget.
- [ ] B guard tests: boundary rows; `responder_dispatch_permitted` / `responder_request_needs_person`; a model output naming a position, standoff or geofence change -> `RESPONDER_POSITION_REQUEST`, rejected, logged in `docs/log/VIOLATIONS.md` format.
- [ ] B planner: deterministic planner picks `responder_look` for S05/S06 and under `fog_no_camera`; catalog expected outcomes unchanged (any change is a spec change with a DECISIONS line).
- [ ] C: `responder` event per tick with the last 60 trail points; ResponderLayer renders; SFX `responder_dispatch.wav` two-note, < 1 s, -18 dBFS; presenter captions present.
- [ ] determinism: two runs of the same scenario and seed give byte-identical responder trails and evidence rows.
- [ ] guard/rule proof: CLAUDE.md 2.1 (no state after HANDED_OFF; the responder is recalled at handoff), 2.2 (planner requests a look, never a position), 2.5 (vocabulary).

## Interfaces
Binding for all three WO-088 agents; the first to merge sets any detail left open here, the others adapt on rebase.
- `config/responder.yaml`: `responder: {id: resp_1, dock: {lat, lon}` (a pier inside a declared security zone of `config/harbor.geojson`), `speed_mps: 1.5, standoff_m: 25.0, battery_min: 90, drain_coeff` (cubic law; use `soundings.physics.battery_drain_min` if that module exists on main (WO-089), else a local cubic law), `geofence_zone_ids: <all declared security zones>, restation_hysteresis_m: 5, orbit_period_s: 60}`; every value labeled "demo value". Loader `soundings.sim.responder.load_responder_config(path=None) -> ResponderConfig` (strict Pydantic).
- `soundings/sim/responder.py` (part A): `ResponderState` enum {DOCKED, TRANSIT, ON_STATION, RETURNING}; `ResponderSnapshot(tick, t_s, lat, lon, heading_deg, speed_mps, battery_min, state, target_contact_id: str | None, standoff_m, distance_to_target_m: float | None)`; `DispatchResult(accepted: bool, reason: str, eta_s: float)`; `class Responder(config, harbor, *, tick_s)` with `dispatch_to_look(contact_id, contact_lat, contact_lon, tick) -> DispatchResult`, `dispatch_search(contact_id, search_box, tick) -> DispatchResult`, `step(tick, *, contact_lat=None, contact_lon=None, contact_speed_mps=None, nearest_asset_lat=None, nearest_asset_lon=None) -> ResponderSnapshot`, `recall(tick)`, `snapshot`, `trail: tuple[ResponderSnapshot, ...]`, `inside_geofence(lat, lon) -> bool`, `travel_time_s(lat1, lon1, lat2, lon2)`. Invariants: distance to the target never below `standoff_m` once within 2x standoff (it orbits at standoff); the station point is on the side of the contact AWAY from the nearest critical asset and the responder is never inside the corridor between contact and asset; if the contact's range is opening the responder holds station or returns (no following); every trail point is inside a declared security zone; battery drains with speed (cubic) and it returns to dock when `battery_min` would fall below the return cost; deterministic; no wall clock.
- Probes (part B, `# --- WO-088 begin/end ---` block in `soundings/investigation/probes.py`): `ProbeId` values `responder_look` and `responder_search_box` (add to `soundings.schemas.enums.ProbeId`; rung ask; `cost_usd` 0; `cost_battery_min` from config; `latency_s` = travel time); a `ResponderPort` Protocol (`dispatch_to_look`, `dispatch_search`, `step`, `snapshot`) injected into the probe runner; `responder_look` returns rows ONLY once ON_STATION (`visual_class` with confidence = base x `visibility_for_camera` x range factor, plus close-range acoustic rows), before that no rows and the probe stays pending; `responder_search_box` lawnmowers `soundings.matcher.SearchBox` and returns found/not_found with position (truth used in tests only). Additive `budgets.responder_dispatches_per_contact: 1` in `config/ladder.yaml`. EIG terms so the deterministic planner picks `responder_look` when it beats static probes (S05/S06, `fog_no_camera`).
- Guards (part B): `config/ladder.yaml` `boundary.ask.platform_alone += responder_look_inside_geofence_at_standoff`; `boundary.ask.person_approves += responder_leave_geofence, responder_standoff_below_25m, second_responder` (additive); `soundings/ladder/guards.py` WO-088 block: `responder_dispatch_permitted(inputs)` and `responder_request_needs_person(inputs)`; the WO-032 guard gains `ViolationCode.RESPONDER_POSITION_REQUEST`: any model output naming a position, standoff or geofence change is rejected and logged (`docs/log/VIOLATIONS.md` format).
- UI (part C): the run engine streams a `responder` event per tick when a responder exists: `{id, state, lat, lon, heading_deg, battery_min, standoff_m, target_contact_id, trail: last 60 points}`; `web/src` ResponderLayer (icon with trail, standoff circle, battery bar, "looking" state) mounted from a `// --- WO-088 begin/end ---` block in `App.tsx`; SFX `web/public/sfx/responder_dispatch.wav` (two-note, < 1 s, -18 dBFS, from `scripts/gen_sfx.py`) fired on the dispatch event via `web/src/sfx.ts` (WO-062P; no second sound module); presenter beats in `docs/demo-script.yaml`.
- Vocabulary everywhere: "look", "station", "standoff", "dispatch to look", "return to dock". Never chase / follow / close with, nor any word in `config/denylist.txt`. The responder is a probe; nothing happens after HANDED_OFF.

Part A details set here (first to merge):
- Extra config keys, all demo values: `hotel_load_min_per_min` (0.2), `battery_reserve_frac` (0.1), `orbit_half_arc_deg` (45), `corridor_half_width_m` (25), `corridor_tie_margin_m` (100), `look_range_max_m` (150), `transit_timeout_factor` (2), `route_cell_m` (25), `search_track_spacing_m` (50). The loader rejects `standoff_m` below 25 m (`MIN_STANDOFF_M`).
- Battery law (local, until WO-089 swaps in `soundings.physics.battery_drain_min` through a lazy import): `battery_drain_min(speed_mps, dt_s, drain_coeff, hotel) = (hotel + drain_coeff v^3) dt_s / 60`; no drain while DOCKED.
- Station radius = `standoff_m + restation_hysteresis_m` (30 m); the station point fans out up to 90 degrees from the away bearing, then out to `look_range_max_m`, to stay in the geofence and out of every corridor. The responder keeps out of the corridor to the nearest asset **and to every asset within `corridor_tie_margin_m` (100 m) of that distance** (a superset of "nearest"), so a change of nearest asset never puts it in a corridor; corridors to far assets are not kept out (they would wall off the harbor).
- "Never closes faster than the contact": on station, the range its own move gives up (measured to the contact's new position) is at most the contact's travel that tick, plus a 0.5 m chord inside the station band.
- Dispatch is rejected (reason strings in `DispatchResult.reason`): responder busy ("a second vehicle needs a person's approval"); contact already had its one dispatch; the dock lies within the contact's standoff or corridor ("a person decides"); no station point inside the geofence; no route; battery below the round-trip cost.
- `dispatch_search(contact_id, search_box, tick)` takes any object with `min_lat, max_lat, min_lon, max_lon` (a `soundings.matcher.SearchBox` fits); lawnmower legs `search_track_spacing_m` apart, points outside the geofence skipped; state ON_STATION while sweeping; `search_complete`; `nearest_pass_m(lat, lon)` gives the closest ON_STATION trail point to a position (part B decides found/not_found from it).
- Contacts faster than the responder (1.5 m/s) are outside the guarantee; every catalog contact is at or below 1.0 m/s.

## Cut line
In this order if time runs short: UI trail polish, the dispatch SFX, the S06 look beat. Never an exit test.

## Model calls
None. The planner's choice of `responder_look` runs through the existing replayed `probe_planner` role and its deterministic fallback; no new role. Paid run needed? No.

## Rungs touched
ask (the responder look and search are ask-rung probes); hand to a person (a second vehicle, leaving the geofence, or a standoff below 25 m needs a person; nothing after handoff).

## Hard rules exercised
- 2.1: nothing after `HANDED_OFF`; the responder only looks and returns to dock (part A vocabulary test; part B handoff test).
- 2.2: the planner requests a look, never a position (part B guard test, `RESPONDER_POSITION_REQUEST`).
- 2.4: deterministic trail (part A determinism tests; catalog determinism).
- 2.5: vocabulary (part A denylist and name tests; `scripts/denylist-check.sh`).

## Done
PR: · Merged commit: · Test summary: · STATE.md updated: yes/no
