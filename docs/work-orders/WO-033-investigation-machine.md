# WO-033 — investigation-machine

Phase: 3 · Branch: `wo/033-investigation-machine` · Worktree: `../soundings-worktrees/wo-033-investigation-machine` · Est: 6 h · Owner: Kudrat
Depends on: WO-010 (`LadderState`, `Rung`, `load_ladder_config`), WO-011 (`run_scenario`, catalog S04 to S08), WO-012 (`build_evidence`), WO-022 (runner v1 `ScenarioRunner`, `MatchOutcome`, `LostVehicleNotice`), WO-030 (posterior), WO-031 (probes, `DeterministicPlanner`, `BudgetLedger`), WO-032 (`soundings.llm.planner.LlmPlanner`, `HypothesisWriter`, `template_question`). Runs in parallel with WO-042 (wave 7).

## Goal
The stopping rule from SPEC-INVESTIGATION §7, implemented exactly, with every threshold read from the `stopping` block of `config/ladder.yaml`.

It sits inside a hand-rolled, typed investigation state machine with a checkpoint per transition and an idempotency key per action. The machine emits `stop_rule_fired(outcome)` events (SPEC-LADDER §2) and ends in a frozen `Recommendation`. The recommendation carries the outcome, a recommended rung computed here, the posterior, an uncertain flag, and the question for the human. Nothing downstream may edit it.

Runner v2 routes the matcher's ESCALATE into the investigation and maps the outcome to a terminal `LadderState`. In Phase 3, HANDOFF goes straight to `HANDED_OFF` (the "nothing pre-authorized" row). The pass-through to `ADVISING_SLOW`/`HOLDING` is Phase 4 (WO-040/041).

Expected scenario outcomes:
- S04 ends `RESOLVED_BENIGN` with zero interruptions, through the `C_KNOWN_OPERATOR` clause below.
- S05 ends `HANDED_OFF` with `threat_mass` ≥ 0.6.
- S06 ends `HANDED_OFF`.
- S07 ends `RESOLVED_BENIGN`.
- S08 ends `HAZARD_NOTICE`.

S05 at seed 11 yields an identical transition log across two runs.

Concurrency: steps 1 and 2 (the stopping-rule function and its tests) need only WO-030 and can be built in parallel with WO-031/032. The machine needs WO-031. Runner v2 needs WO-022 and WO-032 merged.

## Scope
In:
- in `soundings/investigation/machine.py`: the stopping rule, the machine, checkpoints, `Recommendation`, and `RECOMMENDED_RUNG`;
- runner v2 in `soundings/runner.py`, only inside the block `# --- v2 (WO-033) ---` (shared file; WO-022 owns the rest), extending WO-022's CLI output with the nested investigation transitions;
- the S04 to S08 end-to-end tests, including the zero-fixture run (in `tests/test_investigation.py`; `tests/test_llm_guard.py` is WO-032's alone).

Out:
- the ladder machine, standing orders, slow/hold, approvals (WO-040/041);
- SITREP text and the audit chain (WO-042);
- watch-after-handoff (WO-043);
- re-acquisition (§9.9);
- recording fixtures (a later owner-approved paid run of about 40 calls; not in this WO);
- any threshold change.

## Spec references
`docs/SPEC-INVESTIGATION.md §6` (budgets), `§7` (stopping rule; outcomes; the recommended rung is computed here and cannot be altered by the SITREP writer); `docs/SPEC-LADDER.md §1` (INVESTIGATING at rung ask), `§2` (`stop_rule_fired(outcome)`, `probe_result`, `posterior_updated`, `error`), `§3` (ASKING and INVESTIGATING rows); `config/ladder.yaml` (`stopping`, `budgets`); CLAUDE.md §2.1, §2.2, §2.4, §4 (hand-rolled machine pattern); `docs/WORKFLOW.md §8`.

## Definitions (binding)
- **`StoppingConfig`** is loaded from `config/ladder.yaml` `stopping`: `benign_mass_resolve` (0.85), `benign_time_guard_s` (300), `threat_mass_handoff` (0.35), `imminent_time_s` (180), `imminent_benign_mass_max` (0.6), `lost_vehicle_silence_min` (10).
  - Spec §7 calls the last one `lost_vehicle_minutes`. The config key is the source.
  - No threshold literal appears in `machine.py`; a grep test enforces this.
- **`StopInputs`** come from the posterior and the latest evidence rows: `benign_mass`, `threat_mass`, `top`, `p_known_operator` (P(UNFILED_KNOWN_OPERATOR)), `registry_match`, `hail_reply`, `operator_known`, `ttc_s` (`time_to_nearest_critical_asset`; `unknown` or `NOT_CLOSING_S` means no critical asset is approaching), `silence_s`, and `budget_exhausted`.
  - `silence_s` = `t_now` minus the `t` of the latest acoustic-detector fact row or `hail_reply=valid` row for the contact.
- **`evaluate_stop(inputs, cfg) -> StopDecision | None`.** Each clause is evaluated in this precedence; the first match wins. The order is safety-first, and the precedence is tested.
  1. `C_CLEARED`: `registry_match == exact` and `hail_reply == valid` gives `CLEARED_AT_ASK`.
  2. `C_LOST`: `top == FILED_OFF_PLAN` and `hail_reply ∈ {none, timeout}` and `silence_s > lost_vehicle_silence_min*60` gives `LOST_VEHICLE`.
  3. `C_THREAT`: `threat_mass ≥ threat_mass_handoff`, **or** (`ttc_s < imminent_time_s` and `benign_mass < imminent_benign_mass_max`), gives `HANDOFF`.
  4. `C_KNOWN_OPERATOR`: `top == UNFILED_KNOWN_OPERATOR` and `p_known_operator ≥ benign_mass_resolve` and `hail_reply == valid` and `operator_known` and not (`ttc_s < benign_time_guard_s`) gives `RESOLVED_BENIGN`. SPEC §3 leaves UNFILED_KNOWN_OPERATOR out of `benign_mass`, so without this clause S04 cannot meet its catalog outcome (SPEC-SIM §3, BUILD-PLAN Phase 3). It reuses two existing keys and adds no threshold; the PR adds a `docs/log/DECISIONS.md` line, and the owner may veto it (then S04's expected terminal state becomes HANDED_OFF).
  5. `C_BENIGN`: `benign_mass ≥ benign_mass_resolve` and not (`ttc_s < benign_time_guard_s`) gives `HAZARD_NOTICE` if `top == DEBRIS_OR_DRIFT`, else `RESOLVED_BENIGN`.
  6. `C_BUDGET`: `budget_exhausted` gives `HANDOFF` with `uncertain=True`. Budget is exhausted when the planner returns `None` (no informative probe left in budget) or the scenario runs out of ticks.
- **`StopOutcome`**: `RESOLVED_BENIGN`, `HAZARD_NOTICE`, `CLEARED_AT_ASK`, `LOST_VEHICLE`, `HANDOFF`. The event is `stop_rule_fired(outcome)`, matching SPEC-LADDER §3.
- **`RECOMMENDED_RUNG`**: CLEARED_AT_ASK, RESOLVED_BENIGN, HAZARD_NOTICE, and LOST_VEHICLE map to `ask`; HANDOFF maps to `hand_to_person`.
  - The investigation never recommends `slow` or `hold`. The ladder (Phase 4) decides whether standing orders apply first.
- **States and transitions.** `InvestigationState`: `SCORING, DECIDING, PROBING, STOPPED, FAILED`. `TRANSITIONS` is exhaustive:

  | from | event | to |
  |---|---|---|
  | SCORING | posterior_updated | DECIDING |
  | DECIDING | stop_rule_fired(outcome) | STOPPED |
  | DECIDING | probe_selected(probe_id) | PROBING |
  | PROBING | probe_result | SCORING |
  | any non-terminal | error | FAILED |

  Any other pair raises `IllegalTransition`. `STOPPED` and `FAILED` are terminal.
- **Action key.** Each action gets an idempotency key: `action_key = sha256(canonical_json([contact_id, seq, event, payload]))`. For a probe action the payload is the WO-031 probe idempotency key, so a resumed run hits the probe cache instead of re-executing.
- **Checkpoint.** `InvestigationCheckpoint(seq, contact_id, tick, from_state, event, to_state, action_key, posterior (6 dp), evidence_rows, ledger, prev_hash, hash)`.
  - It is written for every transition to an in-memory list.
  - When `checkpoint_path` is given, it is also appended to a JSONL file.
  - `InvestigationMachine.resume(checkpoints)` replays to the last state.
- **`Recommendation`.** A frozen Pydantic model: `contact_id, outcome, rule_id, recommended_rung, posterior, entropy_bits, threat_mass, benign_mass, top, uncertain, question_for_human, evidence_row_ids, probes_used, interruptions, label="stress-test estimate"`, plus the computed field `recommendation_hash`.
  - `question_for_human` comes from `HypothesisWriter` (LLM or template). It is text only and never feeds a field above it.
  - WO-043 puts `recommendation_hash` in the handoff event payload, so WO-042's audit row ties each SITREP to the recommendation it rests on.
- **Clock.** Each probe advances `tick` by `ceil(latency_s / tick_s)` ticks. The machine reads the precomputed `SimRun` at that tick. There is no wall clock.
- **Runner v2.**
  - `ScenarioRunner(scenario, *, seed=None, ...)` keeps WO-022's constructor; `.run(*, llm_roles=False, violations_path=DEFAULT_VIOLATIONS_PATH, fixture_store=None) -> RunResult`.
  - The v1 path runs unchanged up to the matcher. ESCALATE moves the ladder state ASKING to INVESTIGATING (`probe_result`, not cleared).
  - `InvestigationMachine` runs, then `outcome_to_ladder_state` maps the result: HANDOFF to HANDED_OFF; each other outcome to the same-named state; LOST_VEHICLE reuses v1's `LostVehicleNotice`.
  - `RunResult` gains `recommendations: dict[str, Recommendation]` (by case id), `investigation_checkpoints: dict[str, tuple[InvestigationCheckpoint, ...]]`, `probe_calls: tuple[ProbeCall, ...]` (`ProbeCall(case_id, contact_id, probe_id, tick, idempotency_key, cost_usd, cached)`), and `planner_choices: tuple[PlannerChoice, ...]`. WO-022's `probes_used` and `interruptions` now include investigation probes.
  - The transition log is canonical JSONL: ladder transitions with the investigation transitions nested.
  - `llm_roles` defaults to False so CI never meets a replay miss (owner may flip the default once S04–S08 fixtures are recorded).

## Steps
1. `StoppingConfig`, `load_stopping_config`, `StopInputs`, `StopDecision`, `StopOutcome`, and `evaluate_stop`, with table tests.
2. `RECOMMENDED_RUNG` and `Recommendation`.
3. `InvestigationState`, `InvestigationEvent`, `TRANSITIONS`, `IllegalTransition`, `InvestigationCheckpoint`, `action_key`.
4. `InvestigationMachine(contact_id, table, probe_ctx, planner, writer, tables, prior_cfg, stopping, budgets, checkpoint_path=None)`.
   - `.run() -> InvestigationResult` loops: score, decide (stop rule first, then planner), probe, rescore.
   - The loop is bounded by the budgets, so it cannot run unbounded.
   - `.resume(checkpoints)`.
5. Runner v2 wiring and `outcome_to_ladder_state`; `python -m soundings.runner --scenario S05 --seed 11` (WO-022's CLI) now prints the nested log.
6. Tests, `make check`, a STATE.md line, and a PR noting rungs and rules.

## Files expected
- `soundings/investigation/machine.py`, `soundings/runner.py` (`# --- v2 (WO-033) ---` block only; small commits, rebase early)
- `tests/test_investigation.py`, `tests/test_stopping_rule.py`, `tests/test_investigation_machine.py`

## Exit tests (must all pass with `MODEL_MODE=replay`, no network)
From BUILD-PLAN Phase 3 (verbatim):
- [ ] `pytest tests/test_investigation.py` — scenarios S04–S08: each reaches a terminal state within budget; S04 (unfiled slow contact, benign) ends `RESOLVED_BENIGN` with no human interruption; S05 (unfiled, closing on a critical berth, propeller tonal) ends `HANDED_OFF` with posterior mass on `UNAUTHORIZED_UUV` ≥ 0.6; S07 (sea lion) ends `RESOLVED_BENIGN`; S08 (drifting debris) ends `HAZARD_NOTICE`.
- [ ] Determinism: the full investigation for S05 at seed 11 produces an identical transition log across two runs.

Unit:
- [ ] `uv run pytest tests/test_stopping_rule.py -k clauses` — table-driven, ≥ 14 cases: every clause at, just above, and just below each threshold (for example `threat_mass` 0.3499 gives None and 0.35 gives HANDOFF; `ttc_s` 179 with benign 0.59 gives HANDOFF and 0.60 gives None; benign 0.85 with `ttc_s` 299 gives None); `HAZARD_NOTICE` only when top is debris; LOST_VEHICLE at 601 s but not at 600 s; budget gives an uncertain HANDOFF; `C_KNOWN_OPERATOR` fires at `p_known_operator` 0.85 and not at 0.8499, and not when `operator_known` is false or `ttc_s` is 299. Proves §7 exactly, plus the one documented clause.
- [ ] `uv run pytest tests/test_stopping_rule.py -k precedence` — inputs satisfying both `C_CLEARED` and `C_THREAT` give CLEARED_AT_ASK; inputs satisfying `C_THREAT` and `C_BUDGET` give HANDOFF with `uncertain=False`; inputs satisfying `C_THREAT` and `C_KNOWN_OPERATOR` give HANDOFF. Proves the documented order.
- [ ] `uv run pytest tests/test_stopping_rule.py -k thresholds_from_config` — a tmp `ladder.yaml` with `threat_mass_handoff: 0.5` flips the 0.4 case, and `grep -nE "0\.(35|85|6)\b|\b(180|300)\b" soundings/investigation/machine.py` → empty. Proves thresholds are read from config (CLAUDE.md §3).
- [ ] `uv run pytest tests/test_investigation_machine.py -k transitions` — every `TRANSITIONS` row is reachable on synthetic inputs; every other (state, event) pair raises `IllegalTransition`. A Hypothesis property test (`derandomize=True`) of random event sequences never reaches a state outside `InvestigationState` and never leaves STOPPED or FAILED.
- [ ] `uv run pytest tests/test_investigation_machine.py -k checkpoint_resume` — a run interrupted by a stub probe that raises once after 2 transitions, resumed from its checkpoints, gives a final transition log and `Recommendation` equal to an uninterrupted run. The probe executor counter shows no action ran twice. Proves checkpoint-per-transition plus idempotency keys.
- [ ] `uv run pytest tests/test_investigation_machine.py -k recommendation_frozen` — assigning `recommended_rung` raises; `recommended_rung ∈ {ask, hand_to_person}` for every outcome; `recommendation_hash` changes if any field changes.
- [ ] `uv run pytest tests/test_investigation_machine.py -k checkpoint_chain` — every checkpoint's `prev_hash` equals the previous `hash`, and the JSONL file equals the in-memory list.

Integration:
- [ ] `uv run pytest tests/test_investigation.py -k catalog` — S04 to S08 at their catalog seeds through `ScenarioRunner.run` from the YAML on disk. Each `terminal_state` equals the scenario's `expected.terminal_state`, ignoring `passes_through` (HOLDING is Phase 4). `probes_used ≤ probes_per_contact`, paid probes ≤ 2, and interruptions ≤ the scenario's `max_human_interruptions`. S04 resolves with `rule_id == C_KNOWN_OPERATOR`. S05 has `threat_mass ≥ expected.min_threat_mass` (0.6) and P(UNAUTHORIZED_UUV) ≥ 0.6. S06 is `HANDED_OFF`. S01 and S02 still end as in WO-022 (regression).
- [ ] `uv run pytest tests/test_investigation.py -k investigation_no_fixtures` — S04 to S08 with `llm_roles=True`, an empty `tmp_path` fixture store, and `SOUNDINGS_CI` unset: the terminal states and transition logs equal the `llm_roles=False` run, every `PlannerChoice.source == "fallback"`, and the violations log stays empty. Proves CLAUDE.md §5's zero-fixture requirement end to end.
- [ ] `uv run pytest tests/test_investigation.py -k adversarial_writer_cannot_steer` — a spy client captures S05's real role inputs; the test saves hand-written outputs for those exact inputs into a `tmp_path` `FixtureStore`: a planner naming "hold" and a writer adding `"recommended_rung": "ask"`. Replaying them gives `HANDED_OFF`, `recommended_rung == hand_to_person`, the same ladder transitions as the deterministic run, and two violation rows in the `tmp_path` log.

Determinism:
- [ ] `uv run python -m soundings.runner --scenario S05 --seed 11 > /tmp/r1.jsonl; uv run python -m soundings.runner --scenario S05 --seed 11 > /tmp/r2.jsonl; diff /tmp/r1.jsonl /tmp/r2.jsonl` → empty, and `pytest tests/test_investigation.py -k s05_log_identical` asserts the same in-process. Proves CLAUDE.md §2.4 for scoring, guards, and the machine.

Guard / rule proof:
- [ ] CLAUDE.md §2.1 (the work stops at the handoff): `recommendation_frozen` (nothing above `hand_to_person`, no slow/hold from investigation) and the `transitions` property test (no state after STOPPED).
- [ ] CLAUDE.md §2.2 (no model output becomes a control): `adversarial_writer_cannot_steer` and `investigation_no_fixtures`.
- [ ] CLAUDE.md §2.5: `grep -iEf config/denylist.txt soundings/investigation/machine.py soundings/runner.py tests/test_investigation*.py tests/test_stopping_rule.py` → empty; `make check` green.

## Interfaces
Exposed by WO-033:
- `soundings.investigation.machine`: `StopOutcome`, `StoppingConfig`, `load_stopping_config`, `StopInputs`, `StopDecision`, `evaluate_stop`, `RECOMMENDED_RUNG`, `Recommendation`, `InvestigationState`, `InvestigationEvent`, `TRANSITIONS`, `IllegalTransition`, `InvestigationCheckpoint`, `action_key`, `InvestigationMachine` (`.run`, `.resume`), `InvestigationResult`
- `soundings.runner`: `ScenarioRunner.run(*, llm_roles=False, violations_path=..., fixture_store=None)`, `RunResult.recommendations`, `RunResult.investigation_checkpoints`, `RunResult.probe_calls`, `ProbeCall`, `RunResult.planner_choices`, `outcome_to_ladder_state` (the CLI stays WO-022's)

Consumed:
- WO-010: `soundings.schemas.LadderState`, `.Rung`, `.Hypothesis`, `soundings.schemas.config.load_ladder_config`, `soundings.canonical.canonical_json`, `.round6`
- WO-011: `soundings.sim.run.run_scenario`, `soundings.sim.scenario.resolve_scenario`
- WO-012: `soundings.investigation.evidence.EvidenceTable`, `soundings.investigation.features.build_evidence`, `.NOT_CLOSING_S`
- WO-022: `soundings.runner.ScenarioRunner` (v1), `soundings.runner.RunResult`, `soundings.runner.LostVehicleNotice`, `soundings.matcher.MatchOutcome`
- WO-030: `soundings.investigation.posterior.score_contact`, `.Posterior`, `.load_likelihood_tables`, `.load_prior_config`
- WO-031: `soundings.investigation.probes.run_probe`, `.ProbeContext`, `.ProbeCache`, `soundings.investigation.planner.DeterministicPlanner`, `.BudgetLedger`, `.load_budget_limits`, `.PlannerState`
- WO-032: `soundings.llm.planner.LlmPlanner`, `soundings.llm.roles.HypothesisWriter`, `soundings.llm.fallback.template_question`, `soundings.llm.guard.DEFAULT_VIOLATIONS_PATH`

Used by: WO-043 (`Recommendation` drives the INVESTIGATING exits and the SITREP's `question_for_human` and `uncertain` inputs), WO-051 (`RunResult`), WO-060 (the WS stream of transitions and posteriors).

## Cut line
If the WO runs more than an hour late, drop in this order (exit tests unchanged):
1. The `main()` CLI prints only the ladder-level transitions, without the nested investigation transitions. The two-run diff still runs on that output.
2. Simplify `silence_s` to "ticks since the last detection".
3. Drop the S01/S02 regression clause, since WO-022's own tests still cover them.

## Model calls
Roles: `probe_planner` and `hypothesis_writer`, through WO-032 only. They are off by default in runner v2 (`llm_roles=False`) and replay-only when on. Paid run needed? No. Recording S04 to S08 fixtures (about 40 calls) is a separate, later run the owner must approve and is not part of this WO.
Dependency adds needing owner approval (CLAUDE.md section 3): none.

## Rungs touched
ask (INVESTIGATING) and hand to a person (the `HANDOFF` recommendation and `HANDED_OFF` terminal). The slow/hold pass-through is deliberately left to Phase 4.

## Hard rules exercised
- §2.1: `recommendation_frozen` and the `transitions` property test.
- §2.2: `adversarial_writer_cannot_steer` and `investigation_no_fixtures`.
- §2.4: the two-run `diff` and `s05_log_identical`.
- §2.5: the denylist grep.
- §3: `thresholds_from_config`.

## Done
PR: #PRNUM (part B; part A #23) · Merged commit: squash of #PRNUM (see STATE) · Test summary: TESTSUMMARY · STATE.md updated: WO-033 row.

Part B (recorded 2026-10-17 07:30 PDT, branch `wo/033b-investigation-wiring`):
- `soundings/investigation_wiring.py` adapts the real collaborators to the part A Protocols: `PlannerAdapter` (WO-031 `DeterministicPlanner` or WO-032 `LlmPlanner`; cost/latency/rung from `ALLOWLIST`, args from `resolve_args` so the machine's probe key equals `run_probe`'s; `ran_probes` = probes with rows for the contact, so the ask hail and v1 registry lookups are not re-run), `RunProbeRunner` (`run_probe` + one shared `ProbeCache`; executor failures become `ProbeError`), `WriterAdapter` (`HypothesisWriter` with roles on, `template_question` off; only a HANDOFF gets a question), `make_kit(world, ..., llm_roles=False, fixture_store=None, violations_path=...)`.
- Machine additions: `InvestigationMachine.advance(until_tick)`, `.done`, `.pending_tick`, `.recommendation`; a probe checkpoint now carries every row the probe produced (a wired executor appends its own rows).
- Runner v2 (`soundings/runner.py`, block `# --- WO-033 begin: runner v2 ---`): `ScenarioRunnerV2(scenario, seed=...).run(*, llm_roles=False, violations_path=..., fixture_store=None) -> RunResultV2` (`recommendations` read-only mapping by case id, `investigation_checkpoints`, `probe_calls: tuple[ProbeCall]` with case ids, `planner_choices: tuple[CasePlannerChoice]`, `nested_log`, `nested_log_hash`; `probes_used` and `interruptions` include the investigation). `outcome_to_ladder_state`, `V2_TRANSITIONS`, `case_interruptions`. The v1 `ScenarioRunner.run()` and `python -m soundings.runner` are untouched (WO-022's tests pin them); the v2 CLI is `python -m soundings.runner_v2 --scenario S05 --seed 11` (nested canonical JSONL, then `transition_log_hash=` and `nested_log_hash=`).
- Interruptions: per case, eyes-on probes plus one for the handoff SITREP (one definition); a failed investigation also counts one (a person takes it).

Investigation handle for WO-043 part B and WO-085 (`soundings.investigation_wiring`):
- `kit = make_kit(ProbeWorld(run, scenario, harbor, current_field, pol, plans_at, hail=, match_cfg=, known=), tables=, prior_cfg=, stopping=, budgets=, llm_roles=False)`.
- `h = kit.handle(contact_id, table, max_tick=n_ticks-1, ledger=InvestigationLedger(paid_used=<ask hails>))`; `h.start(tick)` and `h.step(tick)` return the new `InvestigationCheckpoint`s (a probe lands at `sent + ceil(latency_s/tick_s)`); `h.done`, `h.state`, `h.recommendation` (frozen; `recommended_rung` computed), `h.result()`. `kit.choices` lists every planner decision. Tick-by-tick equals one-shot (`-k handle`).

Results (catalog seeds, deterministic planner; the scenario is graded on the ground-truth lineage end, WO-022's S02 convention):
- S01/S03 CLEARED_AT_ASK, S02 LOST_VEHICLE: v1 transitions and states identical under v2.
- S05: lineage end c-0008#1 HANDED_OFF by C_THREAT, threat_mass 0.789823, P(UNAUTHORIZED_UUV) 0.636107 (after `second_sensor_dwell`); the first two tracks of the same vehicle hand off at 0.223617 (C_BUDGET, top BIOLOGIC) and 0.350442 (C_THREAT, fires at the 0.35 threshold).
- S06 HANDED_OFF (0.887218, 0.768972); S07 RESOLVED_BENIGN (both tracks, top BIOLOGIC); S08 HAZARD_NOTICE (the re-acquired track; the first track resolves benign as BIOLOGIC).
- S04: the primary track resolves RESOLVED_BENIGN by C_KNOWN_OPERATOR at tick 23 with zero interruptions; the re-acquired track (c-0009#1, tick 366) ends HANDED_OFF by C_BUDGET (P(UNFILED_KNOWN_OPERATOR) 0.605072) because its registry rows carry confidence 0.57 instead of 0.91. The lineage-end and lineage-interruption checks for S04 are strict xfails naming this; no table or threshold changed (open question to the coordinator).
- Sim false alarms (biologic) mostly resolve benign; some near a critical asset hand off by C_BUDGET (benign mass >= 0.85 but inside `benign_time_guard_s`), so run-level interruptions exceed the per-scenario limits (S04 2, S05 6).

## Coordinator addenda (2026-10-16 23:05 PT, binding; see docs/log/DECISIONS.md)
- Approved: the `C_KNOWN_OPERATOR` stop clause (reuses `benign_mass_resolve` and `benign_time_guard_s`; no new threshold) so S04 ends RESOLVED_BENIGN.
- Budget scope confirmed: `probes_per_contact` counts planner-chosen probes during INVESTIGATING only; the ask-rung `acoustic_hail` counts toward `paid_probes_per_contact`.

## Part A done (coordinator split)
Recorded 2026-10-17 06:09 PT. Branch `wo/033a-investigation-core`. Everything is in `soundings/investigation/machine.py`; tests in `tests/test_stopping_rule.py`, `tests/test_investigation_machine.py`, `tests/test_investigation.py` (part A smoke).

Built:
- Stopping rule: `StopOutcome`, `StopRuleId` (+ `STOP_PRECEDENCE`), `StoppingConfig`, `load_stopping_config(path)`, `StopInputs`, `StopDecision`, `evaluate_stop(inputs, cfg)`, `stop_inputs_from(report, table, *, tick, tick_s, budget_exhausted=False)`, `evaluate_stop_at(...)` → `(inputs, decision)`, `last_heard_tick`. Section 7 clauses in the WO precedence incl. `C_KNOWN_OPERATOR`; thresholds only from `stopping` (grep test). `ttc_s` is None for `unknown`/`NOT_CLOSING_S`; `silence_s` counts from the latest `acoustic_detector:` fact row or `hail_reply=valid` row.
- Budgets: `BudgetLimits`, `load_budgets(path)` (the `budgets` block), `InvestigationLedger(probes_used, paid_used, interruptions, llm_calls)` with `.allows(choice, limits)` / `.after(choice)`, `BudgetView(limits, ledger, tick, ticks_left)`. Start the ledger at `paid_used=1` when the ask-rung hail ran (addendum).
- Protocols and doubles: `Planner.next_probe(table, posterior: PosteriorReport, budget: BudgetView, rung) -> ProbeChoice | None`; `ProbeRunner.run(probe_id, contact_id, tick, args) -> Sequence[EvidenceRow]` (rows returned, the machine appends; raise `ProbeError` to record `error`); `QuestionWriter.question_for_human(report, decision, table) -> str | None`; `ProbeChoice(probe_id, args, cost_usd, latency_s, interruption, rung_required, source, why)`; `NoProbePlanner`, `NoRowsProbeRunner`, `TemplateQuestionWriter`; `InvestigationError`, `ProbeError`.
- Machine: `InvestigationState` (SCORING, DECIDING, PROBING, STOPPED, ERRORED — ERRORED is the WO's FAILED, renamed off `LadderState`), `InvestigationEvent`, `TRANSITIONS`, `next_state`, `IllegalTransition`, `action_key`, `event_label`, `InvestigationCheckpoint` (hash chain, `evidence_rows` = rows the transition appended, `probe_key`, `recommendation` on the stop row), `verify_checkpoint_chain`, `load_checkpoints`, `checkpoint_line`, `latency_ticks`, `ProbeCall`, `InvestigationResult` (`events`, `transition_log_lines()`), `InvestigationMachine(contact_id, table, *, planner, probe_runner, tables, prior_cfg, stopping, budgets, tick, tick_s, max_tick, writer=None, ledger=None, rung=ask, rungs=None, checkpoint_path=None)` with `.run()` and `.resume(checkpoints)`.
- `RECOMMENDED_RUNG` and the frozen `Recommendation` (`recommended_rung` and `recommendation_hash` are computed fields; a supplied or edited rung is rejected; `tick` added).

Left for part B: wire WO-031 `DeterministicPlanner`/`run_probe`/`ProbeCache` and WO-032 `LlmPlanner`/`HypothesisWriter`/`template_question` behind these Protocols (adapters; or swap the part A budget types for WO-031's `BudgetLimits`/`BudgetLedger`); runner v2 block in `soundings/runner.py` (`outcome_to_ladder_state`, `RunResult.recommendations`/`investigation_checkpoints`/`probe_calls`/`planner_choices`, nested CLI log); the `-k catalog`, `investigation_no_fixtures`, `adversarial_writer_cannot_steer`, `s05_log_identical` exit tests and the two-run CLI diff.
