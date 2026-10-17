# STATE — read this first after CLAUDE.md

Last updated: 2026-10-16 22:35 PT (Phase 0 complete; Phase 1 starting)

## Where we are
- **Phase 0 complete.** Repo `kudratsingh/soundings` (public), scaffold commit `25e7dbb`. `make check` green locally (ruff, mypy, denylist, 31 tests, web build). `/health` → `{"ok":true,"model_mode":"replay"}`. `python -m soundings.sim --seed 7 --ticks 60 --scenario smoke` is byte-identical across runs.
- **Phase 1 in progress.** WO-010 (schemas + harbor map) first; WO-011 (sim + catalog) and WO-012 (evidence table + features) run in parallel worktrees once WO-010's interfaces land. Phase 1 target of Fri 11:30 PM has slipped (see `docs/log/DECISIONS.md` 22:25 rows).
- Phase 2–6 work orders are being drafted in parallel and land as a docs PR.
- Lens declared at submission: Crisis Mitigation (Track 01 Marine, NULL Labs).

## Owner directives in force (2026-10-16 ~22:00 PT)
- Build Phases 0–6 autonomously; coordinator (Claude) plans and verifies, Opus subagents implement, one WO per worktree under `../soundings-worktrees/`.
- PRs squash-merge on green CI via `gh pr merge --squash --auto`; no review wait. `main` stays protected; nobody pushes to `main` directly.
- Dependency adds are recorded in `docs/log/DECISIONS.md` instead of asked. Zero paid runs. Stretch item 5 (Retell voice handoff, WO-070) is gated on Phase 6 green.

## Work-order status (each agent edits ONLY its own row; coordinator edits everything else)
| WO | title | status | branch | PR | merged commit | tests |
|---|---|---|---|---|---|---|
| 000 | repo + scaffold | merged (direct, pre-protection) | main | — | 25e7dbb | 31 passed |
| 010 | schemas + harbor map | PR open, auto-merge on green CI | wo/010-schemas-and-harbor-map | [#1](https://github.com/kudratsingh/soundings/pull/1) | squash commit on main (see PR #1) | 64 passed |
| 011 | sim + scenario catalog | pending | wo/011-sim-and-scenario-catalog | | | |
| 012 | evidence table + features | pending | wo/012-evidence-table-and-features | | | |
| 020 | registry | pending | wo/020-registry | | | |
| 021 | challenge-response | pending | wo/021-challenge-response | | | |
| 022 | matcher + ask rung | pending | wo/022-matcher-ask-rung | | | |
| 030 | posterior | pending | wo/030-posterior | | | |
| 031 | probes + planner | pending | wo/031-probes-planner | | | |
| 032 | llm planner guard | pending | wo/032-llm-planner-guard | | | |
| 033 | investigation machine | pending | wo/033-investigation-machine | | | |
| 040 | ladder machine | pending | wo/040-ladder-machine | | | |
| 041 | guards + standing orders | pending | wo/041-guards-standing-orders | | | |
| 042 | sitrep + handoff + audit | pending | wo/042-sitrep-handoff-audit | | | |
| 043 | watch after handoff | pending | wo/043-watch-after-handoff | | | |
| 050 | baselines | pending | wo/050-baselines | | | |
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
1. Phase 1 — WO-010 → WO-011 ∥ WO-012.
2. Phase 2 and Phase 4 ladder machine can start as soon as WO-010 merges (they need schemas, not the sim).
3. Twist 1 lands Fri 11:30 PM: switch to `docs/TWIST-PLAYBOOK.md` on `twist/1-<slug>` when the owner pastes the notice.

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
