# STATE — read this first after CLAUDE.md

Last updated: 2026-10-17 06:38 PDT (truth-first rewrite; Phase 7 'ATC at scale' + eval/model-run directives accepted)

## Where we are (2026-10-17 06:38 PDT)
- **Merged on `main`** (CI green, ≈1,200 tests, zero paid runs): Phase 0 scaffold `25e7dbb`; Phase 1 WO-010 `6d7fd6c`, WO-011 `32db5a7`, WO-012 `26cea7d`; Phase 2 WO-020 `ff04e56`, WO-021 `fcb8761`, WO-022 `c2074f2`; Phase 3 WO-030 `69bd327`, WO-031 `5f10629`, WO-032 part A `51fcc2d`; Phase 4 WO-040 `76edbcc`, WO-041 `1848d92`, WO-043 part A (ladder driver, watch-after-handoff) `8fee1bb`; Phase 5 WO-050 `f2e8bc3`; Phase 6 WO-062 part A (demo runner skeleton) `39c22eb`; Twist 1 proof trail `1dded8e`.
- **PRs open, CI/rebase cycling:** WO-042 part A audit log + SITREP template (#18), WO-060 part A UI core over runner v1 (#20), WO-033 part A investigation machine core (#23), WO-051 part A eval metrics/report/CLI (#24), WO-062 video/sound addenda (#25).
- **Running:** WO-032 part B (LlmPlanner wiring) in the dependency-driven workflow; the Phase 7 work-order writers (WO-080..WO-086, WO-061 QR addendum). Part B of every split is completed by the dependency-driven workflow once upstream merges.
- **Not started:** WO-061 judge panel (waits for the UI core), WO-062P presenter mode (starts when WO-060 part A merges), WO-080..WO-087 (as their WOs land), Twist 2 and 3 (notices not yet received).
- **Incident:** the Mac slept 01:52–05:37 PDT (lid closed on battery); one agent died and was restarted; nothing on `main` was affected. Caffeinate runs until 13:39 PDT; keep the lid open and plug in.
- Lens declared at submission: Crisis Mitigation (Track 01 Marine, NULL Labs).

## Phase 7 directives (owner, 2026-10-17 06:05 and 06:35 PDT)
Product line unchanged: Soundings proposes slots and advisories, operators accept, it never steers anything, the work stops at the handoff. No route planning, no vehicle commands. New work orders (written from the template with exit tests before coding): WO-080 Busy Tuesday scenario (BT-01), WO-081 conflict detection at filing (shapely STRtree), WO-082 slot proposal (OR-Tools CP-SAT), WO-083 separation advisories (CPA/TCPA through guards), WO-084 projected tracks (Kalman), WO-085 surge triage queue, WO-086 rung ledger + blunt-policy counter + radar styling, WO-087 bounded live mode (build now, enable Sunday only), WO-061 addendum (QR code to the registry form). WO-062 addenda: presenter mode, 3:00 shot list, recording, voiceover prep, sound design (`docs/work-orders/WO-062-ADDENDUM-video-sound.md`). WO-051 addendum: mis-specified sim ±20%, toggle sweep, threshold sensitivity curve, BT-01 scale 40/100/200, seeds 1–20 with mean and min–max, planner ablation (`docs/work-orders/WO-051-ADDENDUM-eval-directive.md`).
Waves: **A** (now) WO-060, WO-031, WO-080 · **B** WO-032, WO-033, WO-081, WO-083 · **C** WO-042, WO-043, WO-082, WO-084, WO-087, WO-061(+QR) · **D** WO-051, WO-085, WO-086, WO-062 (demo runner + ≤3 min video). If Wave C is not merged by 3:00 PM, drop WO-082 and WO-084 and report hours saved; never drop exit tests.
Clock: submission 10:00 PM; freeze non-demo work 6:00 PM; hard freeze 8:30 PM; final video render 8:00 PM; Devpost 9:30 PM.
**Fixture run (the one paid step before Sunday):** at ~12:30, after BT-01, WO-033 and WO-042 merge: probe_planner + hypothesis_writer over S04–S08 and the BT-01 rogue, sitrep_writer over every handoff scenario (~60 calls). Print the real count and dollar estimate via `scripts/paid-run-gate.py`, wait for the owner's approval phrase, record once, return to replay. If the owner is not there within 15 min, keep building and re-raise. Planner ablation (LLM vs deterministic vs random) runs after it. Stage live mode (WO-087) is enabled only by `approve paid run stage live budget 40` on Sunday.
**Paid-run budget (owner, 06:40 PDT): $15 total for now; every paid run still requires the owner's explicit approval phrase before it starts.**
**Twists:** scored at final submission only; no per-twist deadline. Twists 2 and 3 must each produce a toggle, scenario, or feature with a test; the Devpost writeup paragraphs live in `docs/DECK-INPUTS.md` → Twists.

## Sunday 9:00 AM tech check
- HDMI audio out confirmed; laptop volume 80%; play the hail ping in the room before the pitch; presenter mode (`/?presenter=1`) reset to step 0 (< 2 s); `MODEL_MODE=replay` unless the owner enables stage live mode; lid open, plugged in.

## Owner directives in force (2026-10-16 ~22:00 PT)
- Build Phases 0–6 autonomously; coordinator (Claude) plans and verifies, Opus subagents implement, one WO per worktree under `../soundings-worktrees/`.
- PRs squash-merge on green CI via `gh pr merge --squash --auto`; no review wait. `main` stays protected; nobody pushes to `main` directly.
- Dependency adds are recorded in `docs/log/DECISIONS.md` instead of asked. Zero paid runs. Stretch item 5 (Retell voice handoff, WO-070) is gated on Phase 6 green.

## Work-order status (each agent edits ONLY its own row; coordinator edits everything else)
| WO | title | status | branch | PR | merged commit | tests |
|---|---|---|---|---|---|---|
| 000 | repo + scaffold | merged (direct, pre-protection) | main | — | 25e7dbb | 31 passed |
| 010 | schemas + harbor map | merged | wo/010-schemas-and-harbor-map | #1 | 6d7fd6c | 64 passed (tests/test_schemas.py, test_config_integration.py) |
| 011 | sim + scenario catalog | merged | wo/011-sim-and-scenario-catalog | #3 | 32db5a7 | 273 passed |
| 012 | evidence table + features | merged | wo/012-evidence-table-and-features | #7 | 26cea7d | 809 passed |
| 020 | registry | merged | wo/020-registry | #4 | ff04e56 | 330 passed (57 registry) |
| 021 | challenge-response | merged | wo/021-challenge-response | #5 | fcb8761 | 395 passed (65 hail) |
| 022 | matcher + ask rung | merged | wo/022-matcher-ask-rung | #14 | c2074f2 | see PR #14 |
| 030 | posterior | merged | wo/030-posterior | #11 | 69bd327 | 1043 passed (test_posterior) |
| 031 | probes + planner | merged | wo/031-probes-planner | #19 | 5f10629 | see PR #19 |
| 032 | llm planner guard | part A merged (#17, 51fcc2d); part B in progress | wo/032-llm-planner-guard | #17 | 51fcc2d | guard/fallback/fixture tests |
| 033 | investigation machine | merged (part A #23 acdfa9c; part B #36) | wo/033b-investigation-wiring | #36 | squash of #36 (part A acdfa9c) | 1657 passed, 2 xfailed (S04 re-acquired track, open question); test_investigation 51 passed |
| 040 | ladder machine | merged | wo/040-ladder-machine | #6 | 76edbcc | 450 passed (55 ladder/clock) |
| 041 | guards + standing orders | merged | wo/041-guards-standing-orders | #9 | 1848d92 | 950 passed (141 guards/orders/approval) |
| 042 | sitrep + handoff + audit | merged (part A #18, 77e5b48; part B #37) | wo/042b-sitrep-writer-handoff (part B) | #37 | squash on merge | 1734 passed after rebase (part B: test_sitrep +17, test_audit +4) |
| 043 | watch after handoff | part A merged; part B pending | wo/043a-ladder-driver-watch | | | see PR (part A: test_ladder_driver 14, test_watch_after_handoff 9) |
| 050 | baselines | merged | wo/050-baselines | #13 | f2e8bc3 | 979 passed (29 baselines) |
| 051 | eval harness | part A merged; part B pending | wo/051a-eval-core (part A) | #24 | | 1454+ passed after rebase (42 WO-051 part A) |
| 060 | watch-officer UI | part A merged; part B pending | wo/060a-ui-core | #20 | | 1343 passed after rebase (part A: 32 + 1 e2e smoke) |
| 061 | judge panel | pending | wo/061-judge-panel | | | |
| 062 | demo runner | part A + presenter mode merged; recording (part B) pending | wo/062a-demo-runner-core; wo/062p-presenter-mode | #21; WO-062P see branch PR | 39c22eb (part A) | part A 53; WO-062P: test_demo_script 29, test_presenter_api 5, test_sfx 12, e2e presenter smoke 1 |
| 080 | Busy Tuesday scenario BT-01 | WO being written | wo/080-busy-tuesday | | | |
| 081 | conflict detection at filing | PR open (auto-merge on green); JobType via local adapter until WO-080 part A | wo/081-conflict-detection | see branch PR | | 1546 passed, 1 skipped (52 WO-081 tests) |
| 082 | slot proposal (CP-SAT) | WO being written | wo/082-slot-proposal | | | |
| 083 | separation advisories (CPA/TCPA) | WO being written | wo/083-separation-advisories | | | |
| 084 | projected tracks (Kalman) | PR open (flag flipped to kalman for build_evidence; runner stays window) | wo/084-projected-tracks | see PR | | 1436 passed (24 test_kalman*) |
| 085 | surge triage queue | WO being written | wo/085-surge-triage | | | |
| 086 | rung ledger + blunt counter UI | WO being written | wo/086-rung-ledger | | | |
| 087 | bounded live mode (stage) | WO written | wo/087-bounded-live-mode | | | |
| 089 | physics cards (sonar-equation detection, sourced physics config, About -> Physics, live range rings) | WO written (owner numbered it WO-088; renumbered; docs #40). P2 (API, sensor_ranges stream, About -> Physics, map rings, deck slide 10) PR open, gated: inert until P1's `soundings/physics.py` + `config/physics.yaml` land (P1 code not started at 08:34). Next: P1 code; then `make deck` | wo/089a-physics-core, wo/089b-physics-ui-deck | #40 (docs); P2 see branch PR | 2f10e33 (docs) | P2: 1863 passed, 12 skipped (physics-gated), 2 xfailed |
| 090 | deck + Devpost fields | in review | wo/090-deck-devpost | | | 1515 passed (28 tests/test_deck.py) |
| 092 | mentor brief | in review | wo/092-mentor-brief | | | see PR (test_mentor_brief 8) |
| 091 | README for the review committee | in review | wo/091-readme-committee | | | README + docs/architecture.svg + 3 UI screenshots; fresh-clone `make setup && make demo` verified |
| 042-fix | audit commit semantics (row coverage, write-failure rollback, idempotent re-delivery) | PR open (auto-merge on green) | fix/audit-commit-semantics | see PR | | tests/test_audit_semantics.py 14 new; full suite in PR |

## Team
| role | name | github | owns |
|---|---|---|---|
| planner / ladder / investigation | Kudrat | kudratsingh | Phases 3–4, guards, model policy |
| sim / data | (unassigned; coordinator Kudrat) | | Phase 1, scenario catalog, twist toggles |
| frontend / demo | (unassigned; coordinator Kudrat) | | Phase 6, judge panel, video |
| domain / eval / deck | (unassigned; coordinator Kudrat) | | Phase 5, 7, 8, mentor liaison, Devpost |

## Next
1. Wave A: land WO-060 part A (#20); start WO-080 as soon as its WO is written; WO-062P presenter mode after #20.
2. Wave B: WO-032 part B (running), WO-033 part B, WO-081, WO-083. Then Waves C and D per the directive.
3. Fixture recording gate at ~12:30 after BT-01, WO-033, WO-042 (owner approval phrase required); planner ablation after it.
4. Twist 2 and Twist 3: when the owner pastes each notice, produce a toggle, scenario, or feature with a test (scored at final submission).

## Blocked
- Nothing. (Twist notices and Devpost steps need the owner.)

## Twist log
| twist | notice received | in Devpost writeup | branch | commit | toggle/scenario/feature + test |
|---|---|---|---|---|---|
| 1 | Fri 11:30 PM (scheduled); response received 2026-10-17 05:43 PDT | pending (DECK-INPUTS → Twists) | twist/1-funding-pathways | see PR | none — written response (funding pathway) |
| 2 | | | | | |
| 3 | | | | | |

## Paid runs so far
None. See `docs/log/PAID-RUNS.md`.

## Mentor sessions
- Brief for the 10:00 sessions (Doyle, Wettels): `docs/MENTOR-BRIEF.md` (WO-092).
- Target pairings: William Doyle (AltaSea / Port of LA startup hub) for the second customer; Dr. Nick Wettels (retired submarine commander) to red-team contact behaviors and false-alarm sources; Alex Piatek (ResQVision) on what Navy trials look for; Ayush Kumar on agent design. Book the moment pairings post Saturday morning.
- Questions to bring: (Doyle) do underwater work crews already notify the port, and who would own a registry; (Wettels) which of our eight scenarios is unrealistic, and what false alarm have we missed.
