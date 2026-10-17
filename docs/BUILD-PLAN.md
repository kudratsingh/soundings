# Build plan — phases, goals, exit tests

> **Revision note (2026-10-17).** Owner decisions after the build opened are recorded in CLAUDE.md §8 "Amendments" (A1–A9) and DECISIONS.md. The original Phase 0–6 text is kept. Phase 7, the revised Phase 8, the Phase 9 twists rule and the work-order numbering are dated sections below; where they differ from older text, the dated sections win.

Clock: build opens Fri Oct 16 ~9:30 PM PT. Submission locks Sat Oct 17 10:00 PM PT. Twists: Fri 11:30 PM, Sat 4:30 AM, Sat 7:00 AM (six hours to respond to each). Finalist deck due Sun 9:00 AM; Devpost links lock Sun 11:00 AM; pitches Sun 1:15 PM.

Each phase is one or more work orders (WOs) under `docs/work-orders/`. A phase is done only when every exit test passes with `MODEL_MODE=replay`. Phases 0–6 are the product. Phase 7 is the owner's Saturday-morning directives (ATC at scale, physics cards). Phase 8 is the submission. Phase 9 is reserved for the twists and runs in parallel. (Until 2026-10-17 the twist section below was headed Phase 7; older work-order text that says "Phase 7 twist proofs" means Phase 9.)

Rule of thumb for the agent: when a phase is behind by more than an hour, cut from the "stretch" list, never from the exit tests.

---

## Phase 0 — Repository and scaffolding (target: Fri 10:00 PM)

**Goal.** A GitHub repo named `soundings` exists under the owner's account, with branch protection, CI, the constitution, and a running skeleton of backend + frontend + sim that boots and serves one static page.

**Work orders.** WO-000 (this phase).

**Steps.**
1. `gh repo create kudratsingh/soundings --public --description "Resolve every unknown underwater contact at the gentlest rung that works. InnovateLA Dual-Use Hackathon 2026, Track 01 Marine." --clone` (ask the owner before creating if `gh auth status` shows a different account).
2. Copy this starter kit into the repo root. Commit as `chore: constitution, build plan, specs, workflow`.
3. Create `.github/workflows/ci.yml` (provided) — ruff, pytest with `MODEL_MODE=replay`, `tsc --noEmit`, `vite build`. CI never sets `MODEL_MODE=live`.
4. Protect `main`: require PR, require CI green, no force-push (`gh api` call in `scripts/protect-main.sh`).
5. `uv init`, add deps, `make check` target. `pnpm create vite` for `web/`.
6. Boot: `make dev` starts FastAPI on :8000 serving `web/dist` and a `/health` route; `make sim` runs the simulator for 60 ticks and prints a contact table.

**Exit tests.**
- `gh repo view kudratsingh/soundings --json name,visibility` returns the repo.
- `gh api repos/kudratsingh/soundings/branches/main/protection` shows required status checks and `allow_force_pushes: false`.
- `make check` exits 0 on a fresh clone.
- `curl localhost:8000/health` → `{"ok": true, "model_mode": "replay"}`.
- `python -m soundings.sim --seed 7 --ticks 60 --scenario smoke` prints 60 ticks and exits 0; running it twice produces byte-identical output (`diff` is empty).
- `docs/STATE.md` has a Phase 0 entry with the commit hash.

---

## Phase 1 — World model, contacts, evidence table (target: Fri 11:30 PM, before Twist 1)

**Goal.** Typed schemas for the harbor, assets, contacts, tracks, and evidence; a seeded simulator that emits contact tracks from a scenario catalog; the evidence table that every later decision reads from.

**Work orders.** WO-010 schemas + harbor map; WO-011 simulator + scenario catalog v1; WO-012 evidence table + feature extractors.

**Specifics the agent decides.** Internal data layout, tick rate, how tracks are smoothed. Constraints: see `docs/SPEC-SIM.md` for the detection model and the scenario catalog, and `docs/SPEC-INVESTIGATION.md` §2 for the evidence table columns, which are fixed.

**Exit tests.**
- `pytest tests/test_schemas.py` — every schema round-trips JSON; invalid rungs and denylisted words are rejected by validators.
- `pytest tests/test_sim_determinism.py` — scenarios `S01..S08` run with seeds 1,2,3 produce identical track hashes across two runs.
- `pytest tests/test_detection_model.py` — detection probability decreases monotonically with range; biologic false-alarm rate matches the configured rate within tolerance over 10k samples; track gaps occur at the configured rate.
- `pytest tests/test_evidence.py` — for each scenario the evidence table has every column in `SPEC-INVESTIGATION §2` populated or explicitly `unknown`, never null; `facts` and `assumptions` are disjoint sets.
- Visual: `make sim-plot --scenario S04` writes `artifacts/S04.png` showing the track over the harbor map (checked by a human, attached to the PR).

---

## Phase 2 — Registry, challenge-response, and the "ask" rung (target: Sat 1:30 AM)

**Goal.** The mechanism that makes most contacts free: a registry of planned underwater activity, a simulated acoustic challenge-response, and the deterministic matcher that clears a contact at "ask."

**Work orders.** WO-020 registry (SQLModel + REST + a one-page form); WO-021 challenge-response sim; WO-022 matcher + `ask` rung integration.

**Specifics the agent decides.** Form fields beyond the required set, modem-ID format, how the hail is simulated. Constraints: the matcher is pure, deterministic, and explains every match/mismatch with a reason code.

**Exit tests.**
- `pytest tests/test_registry.py` — create/read/expire plans; overlapping-window query returns correct plans; plans outside the security zone are rejected.
- `pytest tests/test_matcher.py` — table-driven: (filed, in-window, in-area, replies) → `CLEARED_AT_ASK`; (filed, in-window, out-of-area by > tolerance) → `MISMATCH_AREA`; (filed, gone silent > N min) → `LOST_VEHICLE`; (unfiled, no reply) → `ESCALATE`; (unfiled, replies with a known modem id not in any plan) → `UNFILED_KNOWN_OPERATOR`. Every outcome carries a reason code and the evidence rows it used.
- `pytest tests/test_ask_rung.py` — scenarios S01 (filed on plan) and S03 (permitted divers) resolve at `ask` with zero human interruptions; S02 (filed, gone silent) produces a `LOST_VEHICLE` SITREP with a last-known position and a search box.
- Manual: file a plan in the web form, start the sim with S01, watch it clear. Screenshot in PR.

---

## Phase 3 — Investigation loop (target: Sat 5:00 AM; Twist 2 lands at 4:30)

**Goal.** The sophisticated part. A hypothesis-driven investigation that runs only on contacts not cleared at "ask": multi-hypothesis Bayesian scoring over the evidence table, a probe planner that picks the next most informative cheap probe, a drift/kinematics plausibility check, pattern-of-life comparison, and a stopping rule. See `docs/SPEC-INVESTIGATION.md` for the exact design.

**Work orders.** WO-030 hypothesis set + likelihood tables + posterior; WO-031 probes (allowlisted tools) + probe planner with deterministic fallback; WO-032 LLM probe planner behind the allowlist + fixtures; WO-033 stopping rule + investigation state machine.

**Exit tests.**
- `pytest tests/test_posterior.py` — posterior sums to 1; adding a strongly-diagnostic evidence row moves mass in the expected direction (table-driven, ≥ 12 cases); conflicting evidence widens entropy rather than collapsing.
- `pytest tests/test_probes.py` — every probe is in the allowlist, has a cost, a latency, an idempotency key, and returns typed evidence rows; a probe called twice with the same key does not re-execute.
- `pytest tests/test_probe_planner.py` — deterministic planner picks the max expected-information-gain-per-cost probe on a fixed evidence table (≥ 6 cases); LLM planner output outside the allowlist is rejected and the fallback is used; the planner never exceeds the probe budget.
- `pytest tests/test_investigation.py` — scenarios S04–S08: each reaches a terminal state within budget; S04 (unfiled slow contact, benign) ends `RESOLVED_BENIGN` with no human interruption; S05 (unfiled, closing on a critical berth, propeller tonal) ends `HANDED_OFF` with posterior mass on `UNAUTHORIZED_UUV` ≥ 0.6; S07 (sea lion) ends `RESOLVED_BENIGN`; S08 (drifting debris) ends `HAZARD_NOTICE`.
- `pytest tests/test_llm_guard.py` — with `MODEL_MODE=replay` and no fixtures, every role uses its fallback and the full suite still passes; a model response containing a rung, an approval, or a tool call is rejected by the guard with a logged violation.
- Determinism: the full investigation for S05 at seed 11 produces an identical transition log across two runs.

---

## Phase 4 — Ladder, guards, standing orders, handoff (target: Sat 9:00 AM; Twist 3 lands at 7:00)

**Goal.** The ladder as a typed state machine with deterministic guards, the boundary table (platform alone / standing orders / person approves) as config, the SITREP writer, the handoff, and watch-after-handoff. See `docs/SPEC-LADDER.md`.

**Work orders.** WO-040 ladder state machine + checkpoints; WO-041 boundary table + standing orders config + guards; WO-042 SITREP writer (LLM + template fallback) + handoff + audit log; WO-043 watch-after-handoff + verify.

**Exit tests.**
- `pytest tests/test_ladder.py` — every transition in `SPEC-LADDER §3` is reachable; no transition outside the table is possible (property test); `HANDED_OFF` and `RESOLVED_*` are terminal for handling; there is no state, event, or tool whose name matches the denylist.
- `pytest tests/test_guards.py` — table-driven over the boundary matrix: a `hold` on one gate ≤ 15 min under signed standing orders executes without a human; a `hold` > 15 min or on > 1 gate blocks until approval; with `standing_orders.signed=false` nothing above `ask` executes; approver unreachable → system stays at the highest pre-authorized rung and escalates to the backup approver, never auto-climbs.
- `pytest tests/test_sitrep.py` — every SITREP line cites ≥ 1 evidence row id; facts and assumptions are in separate sections; the recommended rung equals the state machine's rung (the LLM cannot change it); template fallback produces a valid SITREP with no model.
- `pytest tests/test_audit.py` — every transition, probe, guard decision, and approval writes an append-only audit row with a hash chain; tampering with one row breaks verification.
- `pytest tests/test_watch_after_handoff.py` — after handoff, a contact that leaves the zone becomes `RESOLVED_DEPARTED`; one that persists re-issues an updated SITREP at the configured interval without climbing the ladder.

---

## Phase 5 — Baselines and evaluation (target: Sat 12:00 PM)

**Goal.** Honest evidence. Two baselines replayed on identical scenarios and the metrics the deck will show.

**Work orders.** WO-050 baselines (`bang`: close the berth on any unknown contact; `human_queue`: every contact goes to a person); WO-051 eval harness + metrics + report.

**Exit tests.**
- `python -m soundings.eval --suite catalog --seeds 1..5 --mode replay` runs all scenarios × 3 policies and writes `artifacts/eval/report.md` + `report.json`.
- Metrics present per policy: where each contact resolved (rung histogram), missed threats (must be 0 for Soundings on the catalog), false shutdowns, median seconds from seen to handled, human interruptions per hour, shutdown-minutes × configured $/min, probes used.
- `pytest tests/test_eval.py` — report is deterministic; Soundings resolves ≥ 70% of catalog contacts at or below `ask`; `bang` has 0 missed threats and the highest shutdown-minutes; `human_queue` has the highest interruptions.
- The report has a section titled "Where the LLM layer added nothing" that is auto-populated by diffing LLM-planner vs fallback-planner outcomes.

---

## Phase 6 — Watch-officer UI and judge injection (target: Sat 4:00 PM)

**Goal.** One screen that a watch officer and a judge can read in five seconds, and a panel where a judge spawns a contact live.

**Work orders.** WO-060 UI (map, contact card, evidence ledger, rung indicator, approval button, SITREP drawer); WO-061 judge-injection panel; WO-062 demo script runner (`make demo` replays the pitch sequence).

**Exit tests.**
- `pnpm build` green; Playwright smoke: load page, see a contact appear, see the rung indicator change, click approve on a Tier-2 recommendation, see `HANDED_OFF`.
- Judge panel: place a contact by clicking the map, choose behavior (filed / unfiled / closing / loitering / silent), press go; the investigation starts within 2 s and the UI follows it.
- `make demo` runs the five-minute sequence end to end with `MODEL_MODE=replay` and no network; a recorded screen capture is saved to `artifacts/demo-fallback.mp4`.
- No panel, label, or button uses a denylisted word (grep in CI).

---

## Phase 7 — Owner directives: ATC at scale and physics cards (Sat Oct 17, 06:05–07:50 PT)

**Goal.** The same product at a working harbor's scale, with the physics made explicit. Soundings proposes slots and advisories, and operators accept them. It never steers anything, and the work stops at the handoff. There is no route planning and no vehicle command.

**Work orders** (each written from the template with exit tests before coding; waves and drop rule in `docs/STATE.md` → Phase 7 directives):

| WO | title | wave | depends on |
|---|---|---|---|
| WO-080 | Busy Tuesday scenario BT-01 (40 filed jobs, vessel transits on an AIS-style feed, one rogue) | A | WO-011, WO-012, WO-020, WO-021, WO-022 |
| WO-081 | conflict detection at filing (shapely STRtree) | B | WO-020, WO-080 part A |
| WO-082 | slot proposal (OR-Tools CP-SAT) | C (dropped if Wave C is not merged by 3:00 PM) | WO-081 |
| WO-083 | separation advisories (CPA/TCPA through the guards) | B | WO-080 part A, WO-041, WO-042 part A |
| WO-084 | projected tracks (2-D constant-velocity Kalman) | C (dropped if Wave C is not merged by 3:00 PM) | WO-010, WO-011, WO-012 |
| WO-085 | surge triage queue | D | WO-080 parts A and B, WO-033, WO-043, WO-031, WO-030 |
| WO-086 | rung ledger, blunt-policy counter, radar styling | D | WO-050, WO-051, WO-060, WO-061, WO-080, WO-084 |
| WO-087 | bounded live mode (built now, enabled on stage Sunday only by the owner's approval phrase) | C | WO-032, WO-042, WO-061 |
| WO-088 | responder platform | in flight | work order not yet on `main` |
| WO-089 | physics cards: passive-sonar-equation detection, sourced `config/physics.yaml`, hail/camera/battery laws, About → Physics, live range rings, deck slide 10 (the owner numbered it WO-088; renumbered) | after WO-080 part A | WO-011, WO-012, WO-021, WO-060, WO-080, WO-088, WO-090 |

Addenda: WO-061 (QR code to the registry form), WO-062 (presenter mode, 3:00 shot list, sound design), WO-051 (mis-specified sim ±20%, toggle sweep, threshold sensitivity, BT-01 at scale, seeds 1–20, planner ablation).

**Exit tests.** Each work order's own list. Two are common to all of them:
- The catalog S01..S08 expected outcomes do not move. Any move is a spec change, with a line in `docs/log/DECISIONS.md` and an explanation in the PR.
- `make check` is green with `MODEL_MODE=replay`.

### Phase 7 — ATC at scale: per-WO exit tests, waves and clock (added 2026-10-17)

Sources: owner Phase 7 directives 06:05 and 06:35 PDT (quoted in `docs/STATE.md`), CLAUDE.md §8 A1–A9, the WO files named below, DECISIONS.md rows 2026-10-17 06:37–07:51. Exit tests are listed by command; the full clause text is in each WO file and binds over this summary.

**Product line (CLAUDE.md §8 A9).** Soundings proposes slots and advisories, operators accept, it never steers anything, the work stops at the handoff. No route planning, no commands to contacts or to operators' vehicles. Separation advisories go through `guards.py` like any other `slow`; slot proposals change nothing until an operator accepts. The platform's own responder vehicle is a probe under A2 (WO-088).

**WO-080 Busy Tuesday (BT-01)** · Wave A · depends on WO-010/011/012/020/021/022; rogue `HANDED_OFF` gated on WO-033 runner v2.
- Goal: a committed, regenerable busy day: 40 filed jobs (one no-show), 3 AIS vessel transits, biologic false alarms at the configured rate, one rogue with S05's definition; opt-in `skip_idle_ticks`.
- Exit tests: `tests/test_busy_tuesday.py -k filed_cleared` (39 filed CLEARED_AT_ASK, 0 interruptions) · `-k rogue_handed_off` · two runs at seed 7 byte-identical (`-k two_runs` + CLI diff) · `-k runtime` (< 60 s headless, `ticks_stepped < n_ticks`) · `tests/test_vessel_transit.py` (incl. `-k schema_additive`) · `tests/test_generate.py` · `-k false_alarm_rate` · `tests/test_tick_skipping.py` · `-k reacquire_precision` · `-k no_show` · `-k ais_feature` · `tests/test_busy_tuesday_rules.py` (§2.1, §2.2, §2.4, §2.5, §2.6) · `make check`.

**WO-081 conflict detection at filing** · Wave B · depends on WO-020, WO-010, WO-022, WO-080 part A, WO-060 (e2e).
- Goal: on filing, find every in-force plan overlapping in area, depth band and window; record reason-coded conflicts; both plans `conflicted`; the plan is still accepted and still counts for the matcher.
- Exit tests: `tests/test_conflicts.py::test_three_way_table` · `tests/test_conflicts_integration.py::test_bt01_filing_under_200ms` · e2e `tests/e2e/test_registry_conflicts.py` (form shows "conflicts with <plan> 14:00-16:00") · `tests/test_conflicts.py`, `tests/test_conflicts_property.py`, `tests/test_conflicts_api.py` · `::test_conflicted_plan_still_clears` · `-k deterministic` · `::test_no_clock_no_model` · denylist grep · `make check`.

**WO-082 slot proposal (CP-SAT)** · Wave C (drop candidate at 3:00 PM) · depends on WO-081, WO-020, WO-022, WO-080 part A, WO-060 (e2e).
- Goal: for a conflicted plan, propose start shifts minimizing total delay under zone capacity, earliest/latest start and no shift for plans in the water; single worker, fixed seed, 2 s; accept/decline by an operator; nothing sent to any vehicle.
- Exit tests: `tests/test_slots.py::test_six_plan_optimum` · `-k deterministic` · `tests/test_slots_api.py::test_accept_updates_and_clears` · `::test_decline_changes_nothing` · `::test_infeasible_names_binding` · `tests/test_slots.py`, `tests/test_slots_api.py` · `tests/test_slots_integration.py::test_s01_in_water_plan_kept` · e2e `tests/e2e/test_registry_proposal.py` · `::test_proposal_is_not_control` · `test_no_clock` · denylist grep · `make check`.

**WO-083 separation advisories (CPA/TCPA)** · Wave B · depends on WO-080 part A, WO-041, WO-042 part A, WO-022, WO-012, WO-010 (WO-033 for the gated rogue assertion).
- Goal: CPA/TCPA per vessel transit × in-water filed job; a `slow` (`advisory_one_basin`) proposed when CPA < `separation_m` and 0 < TCPA < `advisory_window_s`, through the standing-orders guard; every decision audited with its geometry; no ladder state added.
- Exit tests: `tests/test_cpa.py::test_known_geometry` (1 m / 1 s) · `tests/test_separation.py::test_thresholds` · `tests/test_separation_guard.py::test_blocked_unsigned_or_expired` · `::test_audited_with_inputs` · `test_course_conversion`, `test_properties`, `test_encounter_once`, `test_in_water_jobs`, `test_slow_permitted_unchanged` · `tests/test_separation_bt01.py` (+ `-k deterministic`) · `::test_no_model_imports` · denylist grep · `make check`.

**WO-084 projected tracks (Kalman)** · Wave C (drop candidate at 3:00 PM) · depends on WO-012, WO-011, WO-010.
- Goal: hand-rolled 2-D constant-velocity Kalman filter per contact: smoothed position, 60 s projection, uncertainty ellipse; `closing_velocity_source` flag.
- Exit tests: `tests/test_kalman.py::test_straight_track_rmse` · `::test_projection_60s` (< 30 m) · `::test_deterministic` · features/evidence/posterior catalog tests unchanged · `test_ellipse_closed_form`, `test_filter_basics`, `test_projection_view` · `tests/test_kalman_vs_window.py` (+ `::test_flag_on_catalog`) · `::test_no_truth_in_package` · denylist grep.

**WO-085 surge triage queue** · Wave D (pure core may start in Wave B; DECISIONS 06:37) · depends on WO-080 A+B, WO-033, WO-043, WO-031, WO-030, WO-012, WO-011, WO-050, WO-060, WO-061.
- Goal: under `contact_surge(n)`, investigations and interruptions are allocated in a deterministic priority queue; the rogue is still handed off within budget.
- Exit tests: `tests/test_triage_runner.py -k rogue_within_budget` · `-k interruptions_per_hour` (surge ≤ no-surge + 1) · `-k order_deterministic` · `tests/test_triage.py -k priority|tie_break|admit|arbiter|config` · `-k catalog_unchanged`, `-k bt01_no_surge`, `-k surge_toggle_wired`, `-k surge_ids_queued` · `tests/test_triage_rules.py` (§2.1, §2.2, §2.4, §2.5, §2.6).

**WO-086 rung ledger + blunt-policy counter + radar styling** · Wave D (pure parts may start in Wave C) · depends on WO-050, WO-051, WO-060, WO-061, WO-080, WO-084.
- Goal: a live rung ledger beside the map and a red counterfactual "berth closures under the blunt policy" from the `bang` baseline, counted with the eval harness helpers so screen and `report.json` agree.
- Exit tests: counters match the eval harness for the same run · bang counter increments per unknown contact · no denylist words in labels · Playwright smoke · `tests/test_ledger.py` · `tests/test_ledger_integration.py::test_bt01_story` · determinism · `::test_display_only` · `::test_blunt_labeled_counterfactual` · `pnpm -C web build`, `make check`.

**WO-087 bounded live mode (stage)** · Wave C · depends on WO-032, WO-042, WO-061 (badge).
- Goal: `MODEL_MODE=live` honors `LIVE_CALL_BUDGET`, 6 s per-call timeout, deterministic fallback on any error, every call audited with its path, UI badge "model: live" / "model: fallback". Built now, enabled only Sunday (A4).
- Exit tests: `tests/test_live_mode.py -k budget|timeout|error|persisted|ci_refuses|guard_unchanged` · `-k path_recorded` / `-k llm_path` · `tests/test_api_model_mode.py` · S05 seed 11 with budget 0 deterministic · `make check`, denylist clean.

**WO-088 responder platform (mobile probe)** · added 07:45 (CLAUDE.md §8 A1, A2; DECISIONS 07:51) · in flight · WO file not yet on `main`.
- Goal: one simulated responder vehicle per harbor dispatched **to look** at a contact, at rung ask: standoff ≥ 25 m, geofenced to declared security zones, re-stations if the contact moves, never closes faster than the contact, never between contact and asset, one dispatch per contact; the planner requests a look, never a position (guard-enforced, logged to VIOLATIONS.md).
- Exit tests: defined by the WO file when written (§7: a WO without exit tests is not a WO).

**WO-089 physics cards (detection model)** · added 07:50, renumbered (CLAUDE.md §8 A1, A3; DECISIONS 07:51, 07:53) · after WO-080 part A · split P1 `wo/089a-physics-core`, P2 `wo/089b-physics-ui-deck` · depends on WO-011, WO-012, WO-021, WO-060, WO-080 part A, WO-088 (battery hook), WO-090 (deck).
- Goal: passive-sonar-equation detection with sourced "demo value" parameters in `config/physics.yaml`; About → Physics cards; live range rings; deck slide 10. See SPEC-SIM §2a.
- Exit tests: detection probability monotone in range and in noise; range rings shrink during a BT-01 transit; S07/S08 unchanged in outcome; catalog outcomes unchanged except where the report explains · `tests/test_physics.py` · `tests/test_detection_model_sonar.py` · `tests/test_catalog_outcomes_physics.py` · ring shrink (P2) · S05 seed 11 and BT-01 seed 7 byte-identical · `tests/test_deck.py` (still 11 slides) · About header "simulated for this demonstration", denylist clean · `make check`.

**WO-061 addendum (QR to the registry form)** · Wave C with WO-061 · see the WO-061 "Coordinator addenda (QR code, owner directive 06:05 PDT)" section.

**Waves and clock (owner directive 06:35; STATE.md).**
- A (now): WO-060, WO-031, WO-080 · B: WO-032, WO-033, WO-081, WO-083 · C: WO-042, WO-043, WO-082, WO-084, WO-087, WO-061 (+QR) · D: WO-051, WO-085, WO-086, WO-062 (demo runner + ≤ 3 min video).
- **3:00 PM drop rule:** if Wave C is not merged by 3:00 PM, drop WO-082 and WO-084 and report hours saved; never drop exit tests. If WO-082 is dropped the QR addendum shows only the conflict line (DECISIONS 06:37).
- Clock (Sat 2026-10-17, PDT): freeze non-demo work **18:00**; final video render **20:00**; hard freeze **20:30**; Devpost **21:30**; submission **22:00**.


---

## Phase 8 — Submission (target: Sat 9:00 PM; hard stop 9:45 PM)

**Goal.** Devpost complete, deck inputs ready, video uploaded.

**Work orders.** WO-090 deck + Devpost fields; WO-091 README for the review committee; WO-092 mentor brief (owner Phase 8 directive, 2026-10-17 06:45 PT; hourly STATUS lines are coordinator work, not a WO).

**Checklist.** Project name + lens (Crisis Mitigation) + one line · pitch deck · demo video · code repo link · all teammates added · two-line responsible-use note · tools used (including a state-machine agent design the owner has used before, Anthropic API, OpenSeaMap tiles, Leaflet, FastAPI, any IBM Bob use if claiming that prize) · hardware: n/a.

**Exit tests.**
- `README.md` quickstart works on a fresh clone in < 5 min (`make setup && make demo`).
- `docs/DECK-INPUTS.md` contains: the ladder slide, the boundary table slide, the three-tests slide, the eval table, the three twist diffs, the one-liner, the responsible-use note.
- `artifacts/demo-fallback.mp4` ≤ 3 min, uploaded.
- Devpost shows all required fields; a screenshot is committed to `docs/log/`.

---

## Phase 8 — submission deliverables (revised 2026-10-17)

Sources: owner Phase 8 directive 06:45 PDT (items 1–5, as cited in WO-090/091/092), WO-062 addenda (`docs/work-orders/WO-062-ADDENDUM-video-sound.md`, owner directives ~06:15 PDT), STATE.md fixture-run and budget lines, CLAUDE.md §8 A4. This revises the checklist above; the exit tests above still apply where not replaced.

- **WO-090 deck + Devpost fields.** `make deck` renders an 11-slide 16:9 deck (`docs/deck/index.html`, PDF export) in hub order: Title, The seat and the moment, The ladder, Two lives, Live demo, The boundary table, Evidence, Twists, What it runs on, Honesty + responsible use, Next. Every number comes from `artifacts/eval/report.json`; boundary from `config/ladder.yaml`; twist rows from `docs/DECK-INPUTS.md`; BOM figures labeled estimate (DECISIONS 06:47). No hand-typed figures.
- **Tools-used line.** Generic: "a state-machine agent design the owner has used before"; no other repository is named or linked anywhere in the submission (owner directive 2026-10-17; CLAUDE.md §8 and WORKFLOW §10).
- **WO-091 committee README.** `README.md` ≤ 120 lines, two screens: one paragraph, `docs/architecture.svg`, three UI screenshots, quickstart (`make setup && make demo`) verified on a fresh clone, eval and deck locations, honesty section, read-in-order list, repo map. Deadline Sat 18:00.
- **WO-092 mentor brief.** `docs/MENTOR-BRIEF.md` for the 10:00 sessions: one page per mentor, the one decision we are stuck on, three questions, `make demo` steps to show, files to have open.
- **WO-062 presenter mode, video, sound** (addenda; split per coordinator notes: WO-062P presenter mode + sound events after WO-060 part A; WO-062 part B recording/VO/YouTube in Wave D after WO-086 and WO-061).
  - Presenter mode `/?presenter=1`: caption bar, step counter, Space advances, reset to step 0 in < 2 s, replay only.
  - Shot list (2:50–3:00) in `docs/demo-script.yaml`: cold open 0:00 · problem 0:12 · Busy Tuesday at 20× 0:30 · Harbor Night at 1× 1:00 · twist 1:50 · evidence 2:15 · dual use 2:40.
  - Recording: Playwright headless Chromium 1920×1080, 30 fps; `artifacts/demo-fallback.mp4` (silent, captions baked in) via bundled imageio-ffmpeg (H.264, yuv420p, CRF 18, AAC, faststart); `artifacts/thumbnail.png` 1280×720.
  - Voiceover prep: `docs/vo-script.md` (≤ 28 words per 10 s, no vendor names, "simulated" once), `make video-merge VO=…` (−16 LUFS, SFX −22 dB), `docs/youtube.md`; owner records the final take and uploads.
  - Sound: five < 1 s SFX generated locally (hail ping, advisory chime, hold tone, handoff chime, twist klaxon-lite), fired on audit-log events only.
  - Deadlines: final render **20:00** (addendum §6). The 16:00 and 18:00 checkpoints come from the coordinator schedule (no DECISIONS row yet).
- **Hourly STATUS line** (directive item 1): one line per hour in `docs/STATE.md` → "Hourly status": time, merged WOs, ETA, slips.
- **Fixture run** (the one paid step before Sunday; WO-051 addendum item 1, STATE.md): ~12:30, after BT-01, WO-033 and WO-042 merge; probe_planner + hypothesis_writer over S04–S08 and the BT-01 rogue, sitrep_writer over every handoff scenario (~60 calls); count and dollar estimate from `scripts/paid-run-gate.py`; waits for the owner's exact `approve paid run …` phrase; record once; back to replay. Budget **$15 total** (DECISIONS 06:40). Planner ablation runs after it.
- **Stage live mode** (WO-087; CLAUDE.md §8 A4): Sunday only, enabled by `approve paid run stage live budget 40`; otherwise `MODEL_MODE=replay`.

---

## Phase 9 — Twists (scored at final submission; no per-twist deadline)

*Heading revised 2026-10-17 (was "Twist responses (runs in parallel: Fri 11:30 PM, Sat 4:30 AM, Sat 7:00 AM)"; CLAUDE.md §8 A8).* Requirement: Twists 2 and 3 each produce a toggle, scenario, or feature with a test.

**Goal.** Each twist response changes the build and ships proof. Pipeline in `docs/TWIST-PLAYBOOK.md`.

**Exit tests (per twist).**
- `twists/0N/notice.md` (verbatim), `twists/0N/delta.json` (parsed constraint diff, facts vs assumptions), `twists/0N/response.md` (what changed, why, proof), `twists/0N/proof/` (test output, screenshots, eval diff), commit hash of the change.
- The twist's scenario toggle exists in the catalog and is covered by a test.
- Submitted through the portal within 6 h; timestamp recorded in `docs/STATE.md`.
- Amended 2026-10-17 (STATE → Phase 7 directives): twists are scored at final submission only, with no per-twist deadline. Twists 2 and 3 must each produce a toggle, scenario, or feature with a test. The Devpost writeup paragraphs live in `docs/DECK-INPUTS.md` → Twists.

---

## Stretch (only after Phase 6 is green, owner approval required)

1. False-alarm memory: per-cell, per-hour benign history raises the auto-clear threshold; show interruptions/hour falling over a simulated month.
2. Multi-sensor fusion: a second simulated sensor (DIDSON-style imaging sonar on a camera cue) added to the evidence table with its own likelihood column.
3. Coverage-gap heatmap: where a contact could approach undetected; "where would two more hydrophones go."
4. Acoustic classifier on real recordings (ShipsEar / DeepShip) replacing the feature-table stub for the tonal feature. Only if someone is idle; label clearly.
5. Voice handoff via Retell for the SITREP read-out to the backup approver.

---

## Work-order numbering (added 2026-10-17)

Status lives in `docs/STATE.md` → "Work-order status" (one row per WO); this table is the index only. Numbers follow `docs/work-orders/`, not chat (CLAUDE.md §8 A1).

| WO | title | phase |
|---|---|---|
| 000 | repo + scaffold | 0 |
| 010 | schemas + harbor map | 1 |
| 011 | sim + scenario catalog | 1 |
| 012 | evidence table + features | 1 |
| 020 | registry | 2 |
| 021 | challenge-response | 2 |
| 022 | matcher + ask rung | 2 |
| 030 | posterior | 3 |
| 031 | probes + planner | 3 |
| 032 | LLM planner guard | 3 |
| 033 | investigation machine | 3 |
| 040 | ladder machine | 4 |
| 041 | guards + standing orders | 4 |
| 042 | SITREP + handoff + audit | 4 |
| 043 | watch after handoff | 4 |
| 050 | baselines | 5 |
| 051 | eval harness (+ eval directive addendum) | 5 |
| 060 | watch-officer UI | 6 |
| 061 | judge panel (+ QR addendum, model badge) | 6 |
| 062 | demo runner (+ presenter mode 062P, video, sound) | 6 / 8 |
| 070 | voice handoff (stretch 5; gated on Phase 6 green) | stretch |
| 080 | Busy Tuesday scenario BT-01 | 7 |
| 081 | conflict detection at filing | 7 |
| 082 | slot proposal (CP-SAT) | 7 |
| 083 | separation advisories (CPA/TCPA) | 7 |
| 084 | projected tracks (Kalman) | 7 |
| 085 | surge triage queue | 7 |
| 086 | rung ledger + blunt-policy counter + radar styling | 7 |
| 087 | bounded live mode (stage) | 7 |
| 088 | responder platform (mobile probe) | 7 |
| 089 | physics cards (sonar-equation detection) | 7 |
| 090 | deck + Devpost fields | 8 |
| 091 | README for the review committee | 8 |
| 092 | mentor brief | 8 |
