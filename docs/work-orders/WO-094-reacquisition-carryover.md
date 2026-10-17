# WO-094 — reacquisition-carryover

Phase: 7 · Branch: `wo/094-reacquisition-carryover` · Worktree: `../soundings-worktrees/wo-094-reacquisition-carryover` · Est: 1.5 h · Owner: Claude (coordinator-identified fix)

Depends on: WO-022, WO-033, WO-043

## Goal
When the matcher links a re-issued observed id to a prior lineage (WO-022 `match_reacquisition`), the runner applies what the prior case already established instead of starting cold. The evidence is carried over (WO-012 `carry_over_reacquired`, already in runner v1), and a prior benign handling-terminal outcome is re-decided by the same deterministic stop rule over the carried rows, with no new interruptions, once the new track is shown to be the same vehicle behaving the same way. S04 (seed 7) then ends RESOLVED_BENIGN on its ground-truth lineage with 0 interruptions, and `python -m soundings.runner verify` exits 0 over the whole catalog.

Source of the problem: WO-033 part B and WO-043 part B reports. In S04 the primary track c-0001 resolves RESOLVED_BENIGN at tick 23 by `C_KNOWN_OPERATOR` (P(UNFILED_KNOWN_OPERATOR) 0.862, registry confidence 0.91); the re-acquired c-0009 (tick 366) starts a fresh case whose fresh registry rows carry confidence 0.57 (track-segment capped), so P stays at 0.605 and the case hands off by `C_BUDGET` with one interruption.

## Scope
In:
- Runner v2 (inherited by v3), `# --- WO-094 begin/end ---` blocks only: the carry-over rule on the WATCHING -> ASKING edge of a re-acquired case.
- Rule (b): the prior case of the lineage ended RESOLVED_BENIGN, HAZARD_NOTICE or CLEARED_AT_ASK through the investigation's stop rule, and the new track is consistent with it (speed and depth buckets unchanged, not inside the benign time guard of a critical asset, zone criticality not higher). If the carried hail is older than the configured re-hail interval, the ask hail is re-sent first; a reply naming the same modem as the prior lineage confirms identity, and the prior's registry facts are re-derived for the new id (derived rows, source `reacquired_from:<old id>`, confidence capped by the sources). The investigation then starts as usual: its first decision is the deterministic stop rule over the table; if it fires the prior's clause the case closes with no probe and no interruption, otherwise the investigation proceeds normally (never short-circuited).
- Rule (c): a lineage whose prior case ended HANDED_OFF is not handed off twice: the re-acquired id's handoff reaches the officer as a watch-after-handoff update to the existing handoff (no new interruption). LOST_VEHICLE lineages keep WO-022's behaviour (each re-issued id ends LOST_VEHICLE with an updated operator notice, not an interruption).
- Rule (d): the catalog verifier reports the lineage roll-up of interruptions next to the per-observed-id list; both come from the same per-contact counts.
- Tests: flip the S04 strict xfails (tests/test_investigation.py, tests/test_catalog_expected.py), unit tests for the rule, determinism diffs.
Out:
- Any change to `config/ladder.yaml` thresholds or `config/likelihoods.yaml`.
- Runner v1 (`ScenarioRunner.run()`; WO-022 pins its transitions).
- Matching quality (`match_reacquisition` gates, confidences).

## Spec references
`docs/SPEC-INVESTIGATION.md` §7 (and the `C_KNOWN_OPERATOR` amendment), §9.9; `docs/SPEC-LADDER.md` watch after handoff; CLAUDE.md §8 A5 (one interruption definition).

## Interfaces
- `soundings/runner.py` (WO-094 blocks): `CARRYOVER_OUTCOMES`, `CARRYOVER_SOURCE_PREFIX` (`reacquired_from:`), `CARRYOVER_REASON`, frozen `CarryOver(case_id, contact_id, old_contact_id, old_case_id, prior_outcome, prior_rule, tick, eligible, reason, hail_age_s, rehailed, identity_confirmed, row_ids)` with `.canonical()`, `carryover_consistency(table, old_id, new_id, *, old_first_tick, first_tick, stopping, speed_tol_mps) -> str | None`, `carried_hail_age_s(table, old_id, tick, tick_s)`, `carried_hail_stale(age_s, rehail_interval_s)`, `lineage_of(result, contact_id)`, `lineage_interruption_view(result, per_contact, handoff_counted, handled_off)`.
- `RunResultV2.carryovers: tuple[CarryOver, ...]` (default empty); `RunResultV3.lineage_interruptions: Mapping[str, int]` (keyed by a lineage's first id) and `repeat_handoffs: tuple[str, ...]`.
- `soundings/eval/catalog_verify.py`: `Graded.lineage_interruptions: int`; the JSON row and the text line print it (`lineage=`).
- Config: no new key. The staleness limit is `plan_watch.rehail_interval_s` and the speed tolerance `reacquire.speed_tol_mps` (both config/matcher.yaml, existing).

## Files expected
- `soundings/runner.py`, `soundings/eval/catalog_verify.py`
- `tests/test_reacquisition_carryover.py`, `tests/test_investigation.py`, `tests/test_catalog_expected.py`
- `docs/log/DECISIONS.md`, `docs/STATE.md`

## Exit tests (must all pass with `MODEL_MODE=replay`)
- [ ] `pytest tests/test_investigation.py -k "S04"` — S04 lineage end RESOLVED_BENIGN, lineage interruptions within 0 (xfails flipped)
- [ ] `pytest tests/test_catalog_expected.py` — every catalog scenario, S04 included, meets its expected block
- [ ] `python -m soundings.runner verify --suite catalog` exits 0; S01–S03, S05–S08 outcomes unchanged (table in the PR)
- [ ] `pytest tests/test_reacquisition_carryover.py` — benign carry-over resolves without an interruption; a re-acquired contact closing on a critical asset is not short-circuited and is investigated; a HANDED_OFF lineage never hands off twice; a stale carried hail triggers a re-hail first
- [ ] `pytest tests/test_ask_rung.py tests/test_runner_v3.py` — S02 re-acquisition and runner v3 tests stay green
- [ ] determinism: two runs of S04 seed 1 and S05 seed 11, empty diff
- [ ] guard/rule proof: the closing decision is a `stop_rule_fired` checkpoint of the investigation machine (rule id equals the prior's), never a runner shortcut
- [ ] `tests/test_boundary.py` green

## Model calls
None. No paid run.

## Rungs touched
watch / ask / hand to a person (no second handoff for one vehicle)

## Hard rules exercised
Deterministic decisions from the evidence table (stop rule over carried rows; `tests/test_reacquisition_carryover.py`); the investigator cannot peek (`tests/test_boundary.py`); one interruption definition (`tests/test_catalog_expected.py`).

## Cut line
1. Rule (d)'s lineage roll-up in the verifier output (keep the per-id check).
2. Rule (c) beyond "no second handoff interruption" (no new UI surface).
Never cut: S04 passing through the stop rule, unchanged outcomes for the rest of the catalog, the S02 tests.

## Done
PR: · Merged commit: · Test summary: · STATE.md updated: yes/no
