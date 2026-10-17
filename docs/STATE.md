# STATE — read this first after CLAUDE.md

Last updated: 2026-10-16 23:35 PDT (Phases 0–1 complete; Phase 2 two-thirds; WO-041 merged; WO-022/030/050 in progress)

## Where we are
- **Phase 0 complete** (scaffold `25e7dbb`). **Phase 1 complete**: WO-010 schemas `6d7fd6c` (#1), WO-011 sim + catalog `32db5a7` (#3), WO-012 evidence table + pattern of life `26cea7d` (#7). `main` CI green, 809 tests, `make check` clean.
- **Phase 2**: WO-020 registry `ff04e56` (#4) and WO-021 challenge-response `fcb8761` (#5) merged; WO-022 matcher + ask rung (runner v1) in progress.
- **Phase 3**: WO-030 posterior in progress; WO-031 → 032 → 033 queued behind it and WO-022.
- **Phase 4**: WO-040 ladder machine `76edbcc` (#6) and WO-041 guards + signed standing orders `1848d92` (#9) merged; WO-042/043 queued.
- **Phase 5**: WO-050 baselines in progress; WO-051 queued. **Phase 6**: WO-060/061/062 queued.
- All work orders WO-010..WO-062 are on `main` with coordinator addenda; rulings in `docs/log/DECISIONS.md`. Zero paid runs.
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
| 022 | matcher + ask rung | pending | wo/022-matcher-ask-rung | | | |
| 030 | posterior | PR open, auto-merge on green CI | wo/030-posterior | (see PR) | squash commit on main (see PR) | 1014 passed after rebase on WO-041 (posterior 43, likelihood tables 8, catalog 13) |
| 031 | probes + planner | pending | wo/031-probes-planner | | | |
| 032 | llm planner guard | pending | wo/032-llm-planner-guard | | | |
| 033 | investigation machine | pending | wo/033-investigation-machine | | | |
| 040 | ladder machine | merged | wo/040-ladder-machine | #6 | 76edbcc | 450 passed (55 ladder/clock) |
| 041 | guards + standing orders | merged | wo/041-guards-standing-orders | #9 | 1848d92 | 950 passed (141 guards/orders/approval) |
| 042 | sitrep + handoff + audit | pending | wo/042-sitrep-handoff-audit | | | |
| 043 | watch after handoff | pending | wo/043-watch-after-handoff | | | |
| 050 | baselines | PR open, auto-merge on green CI | wo/050-baselines | (see PR) | squash commit on main (see PR) | 979 passed after rebase (29 WO-050: unit 24 incl. 10 interruption-table cases, integration 5) |
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
1. Phase 2 → WO-022 (critical path), then Phase 3 WO-031 → 032 → 033.
2. Phase 4 WO-042 → 043 (runner v3), then Phase 6 WO-060/061 and Phase 5 WO-051, then WO-062 demo.
3. Twist 1 (Fri 11:30 PM) has landed by the clock: when the owner pastes the notice, switch to `docs/TWIST-PLAYBOOK.md` on `twist/1-<slug>`.

## Blocked
- Nothing. (Twist notices and Devpost steps need the owner.)

## Twist log
| twist | landed | submitted | branch | commit | toggle/scenario |
|---|---|---|---|---|---|
| 1 | | | | | |
| 2 | | | | | |
| 3 | | | | | |

## Paid runs so far
None. See `docs/log/PAID-RUNS.md`.

## Mentor sessions
- Target pairings: William Doyle (AltaSea / Port of LA startup hub) for the second customer; Dr. Nick Wettels (retired submarine commander) to red-team contact behaviors and false-alarm sources; Alex Piatek (ResQVision) on what Navy trials look for; Ayush Kumar on agent design. Book the moment pairings post Saturday morning.
- Questions to bring: (Doyle) do underwater work crews already notify the port, and who would own a registry; (Wettels) which of our eight scenarios is unrealistic, and what false alarm have we missed.
