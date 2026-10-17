# STATE — read this first after CLAUDE.md

Last updated: 2026-10-17 00:27 PDT (Phases 0–2 complete; Phase 3 one-quarter; Phase 4 half; Phase 5 half)

## Where we are
- **Phase 0 complete** (scaffold `25e7dbb`). **Phase 1 complete**: WO-010 `6d7fd6c` (#1), WO-011 `32db5a7` (#3), WO-012 `26cea7d` (#7).
- **Phase 2 complete**: WO-020 registry `ff04e56` (#4), WO-021 challenge-response `fcb8761` (#5), WO-022 matcher + drift search box + re-acquisition + runner v1 `c2074f2` (#14). S01/S03 clear at ask with zero interruptions; S02 ends LOST_VEHICLE with a search box containing the true position.
- **Phase 3**: WO-030 posterior `69bd327` (#11) merged; WO-031 probes + planner starting; WO-032 → WO-033 queued.
- **Phase 4**: WO-040 ladder machine `76edbcc` (#6) and WO-041 guards + signed standing orders `1848d92` (#9) merged; WO-042 SITREP + audit and WO-043 driver + runner v3 queued.
- **Phase 5**: WO-050 baselines `f2e8bc3` (#13) merged; WO-051 eval harness queued. **Phase 6**: WO-060/061/062 queued.
- `main` CI green; full suite ≈ 1,050 tests, `make check` clean. Zero paid runs.
- Lens declared at submission: Crisis Mitigation (Track 01 Marine, NULL Labs).

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
| 031 | probes + planner | pending | wo/031-probes-planner | | | |
| 032 | llm planner guard | part A merged; part B pending | wo/032a-llm-guard-core | #17 | (squash on merge) | 1223 passed (73 test_llm_guard, 6 test_llm_roles) |
| 033 | investigation machine | pending | wo/033-investigation-machine | | | |
| 040 | ladder machine | merged | wo/040-ladder-machine | #6 | 76edbcc | 450 passed (55 ladder/clock) |
| 041 | guards + standing orders | merged | wo/041-guards-standing-orders | #9 | 1848d92 | 950 passed (141 guards/orders/approval) |
| 042 | sitrep + handoff + audit | pending | wo/042-sitrep-handoff-audit | | | |
| 043 | watch after handoff | pending | wo/043-watch-after-handoff | | | |
| 050 | baselines | merged | wo/050-baselines | #13 | f2e8bc3 | 979 passed (29 baselines) |
| 051 | eval harness | pending | wo/051-eval-harness | | | |
| 060 | watch-officer UI | pending | wo/060-watch-officer-ui | | | |
| 061 | judge panel | pending | wo/061-judge-panel | | | |
| 062 | demo runner | pending | wo/062-demo-runner | | | |

## Team
| role | name | github | owns |
|---|---|---|---|
| planner / ladder / investigation | Kudrat | kudratsingh | Phases 3–4, guards, model policy |
| sim / data | (unassigned; coordinator Kudrat) | | Phase 1, scenario catalog, twist toggles |
| frontend / demo | (unassigned; coordinator Kudrat) | | Phase 6, judge panel, video |
| domain / eval / deck | (unassigned; coordinator Kudrat) | | Phase 5, 7, 8, mentor liaison, Devpost |

## Next
1. Phase 3: WO-031 → WO-032 → WO-033 (critical path to the CLI demo).
2. Phase 4: WO-042 (after WO-032) → WO-043 runner v3 (after WO-033). Then Phase 6 WO-060/061, Phase 5 WO-051, WO-062 demo.
3. Twist 1 landed by the clock at Fri 11:30 PM: when the owner pastes the notice, switch to `docs/TWIST-PLAYBOOK.md` on `twist/1-<slug>`.

## Blocked
- Nothing. (Twist notices and Devpost steps need the owner.)

## Twist log
| twist | landed | submitted | branch | commit | toggle/scenario |
|---|---|---|---|---|---|
| 1 | Fri 11:30 PM (scheduled); response received 2026-10-17 05:43 PDT | pending (owner submits via portal) | twist/1-funding-pathways | see PR | none — written response (funding pathway) |
| 2 | | | | | |
| 3 | | | | | |

## Paid runs so far
None. See `docs/log/PAID-RUNS.md`.

## Mentor sessions
- Target pairings: William Doyle (AltaSea / Port of LA startup hub) for the second customer; Dr. Nick Wettels (retired submarine commander) to red-team contact behaviors and false-alarm sources; Alex Piatek (ResQVision) on what Navy trials look for; Ayush Kumar on agent design. Book the moment pairings post Saturday morning.
- Questions to bring: (Doyle) do underwater work crews already notify the port, and who would own a registry; (Wettels) which of our eight scenarios is unrealistic, and what false alarm have we missed.
