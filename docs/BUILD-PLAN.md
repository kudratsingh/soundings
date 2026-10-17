# Build plan — phases, goals, exit tests

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

---

## Phase 8 — Submission (target: Sat 9:00 PM; hard stop 9:45 PM)

**Goal.** Devpost complete, deck inputs ready, video uploaded.

**Work orders.** WO-090 deck + Devpost fields; WO-091 README for the review committee; WO-092 mentor brief (owner Phase 8 directive, 2026-10-17 06:45 PT; hourly STATUS lines are coordinator work, not a WO).

**Checklist.** Project name + lens (Crisis Mitigation) + one line · pitch deck · demo video · code repo link · all teammates added · two-line responsible-use note · tools used (including the owner's own open-source agent framework, Anthropic API, OpenSeaMap tiles, Leaflet, FastAPI, any IBM Bob use if claiming that prize) · hardware: n/a.

**Exit tests.**
- `README.md` quickstart works on a fresh clone in < 5 min (`make setup && make demo`).
- `docs/DECK-INPUTS.md` contains: the ladder slide, the boundary table slide, the three-tests slide, the eval table, the three twist diffs, the one-liner, the responsible-use note.
- `artifacts/demo-fallback.mp4` ≤ 3 min, uploaded.
- Devpost shows all required fields; a screenshot is committed to `docs/log/`.

---

## Phase 9 — Twist responses (runs in parallel: Fri 11:30 PM, Sat 4:30 AM, Sat 7:00 AM)

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
