# WO-050 — baselines

Phase: 5 · Branch: `wo/050-baselines` · Worktree: `../soundings-worktrees/wo-050-baselines` · Est: 3 h · Owner: domain / eval / deck (unassigned); coordinator Kudrat
Depends on: WO-010 (`soundings.schemas`, `Harbor.nearest_asset`), WO-011 (`run_scenario`, `SimRun`, `track_hash`, `resolve_scenario`, `catalog_paths`, catalog S01..S08), WO-040 (`soundings.ladder.table.RUNG_OF_STATE`). The `soundings` policy and the runner kwarg are WO-051's, so this WO merges in wave 3 without waiting for runner v3.

## Goal
Two baselines run behind one `Policy` interface on the same seeded sim output: `bang` (close the berth on any unknown contact) and `human_queue` (every contact goes to a person). Each returns a typed `PolicyRun` with the same transition-log vocabulary and the same counting rules for interruptions, shutdown-minutes, missed threats and false shutdowns. The third policy, `soundings`, plugs into the same protocol in WO-051 (`soundings/eval/soundings_policy.py`), which then only aggregates and reports; it never re-derives an outcome.

## Scope
In:
- `soundings/eval/__init__.py`, `soundings/eval/policies.py`, `config/eval.yaml`.
- Counting rules, written once and shared by all three policies:
  - **Contact unit.** One `ContactOutcome` per observed contact id. Sim false alarms are kept and flagged `is_false_alarm=True`. A ground-truth contact seen under several ids (track re-acquisition) is rolled up by `outcome_for_gt`: max rung over its ids, plus the terminal state of its last id.
  - **Threat.** Ground-truth hypothesis in {`UNAUTHORIZED_UUV`, `UNAUTHORIZED_DIVER`}. Everything else is benign for scoring, including sim false alarms.
  - **Missed threat.** A threat ground-truth contact none of whose observed ids ever entered `HOLDING` or `HANDED_OFF`. A threat that was never detected counts as missed, with `reason="undetected"`.
  - **Shutdown.** `soundings`: `HOLDING` intervals. `bang`: closure intervals. `human_queue`: 0, labeled "person's decisions not simulated". `ADVISING_SLOW` is reported as `slow_minutes` and is not counted as shutdown. Overlapping intervals on the same berth are merged (interval union), so minutes are never counted twice.
  - **False shutdown.** Any shutdown interval caused by a benign contact.
  - **Interruption.** One count for each of: entering `AWAITING_APPROVAL`, entering `HANDED_OFF`, a `request_watch_officer_look` probe call, and each SITREP update in `WATCH_AFTER_HANDOFF`. The same function `count_interruptions(transitions)` is applied to every policy.
  - **Seconds seen to handled.** Time of entering a state in `HANDLING_TERMINAL_STATES` minus the contact's first detection time. `None` if the contact is never handled within the run. This is reported, never imputed.
- `bang` model. At each contact's first detection the log is `DETECTED → AWAITING_APPROVAL` (action `any_closure` on the nearest berth asset). That action comes from `config/ladder.yaml` boundary `hand_to_person.person_approves`. The person serves a single serial queue with `human_review_s` per request. Then `→ HANDED_OFF` with the approved action recorded; the baseline assumes approval, and that assumption is labeled. The closure lasts from approval until the contact's last detection plus `berth_reopen_after_s`, capped at the end of the run. A contact whose berth is already closed adds no new request. Reopening is one more interruption.
- `human_queue` model. At first detection: `DETECTED → HANDED_OFF`, where `HANDED_OFF` is stamped when the serial queue reaches the contact (`human_review_s` each). Then `WATCH_AFTER_HANDOFF` with one re-check every `watch_after_handoff.sitrep_update_interval_s` while the contact is still detected. No probes. No shutdown.
- `soundings` policy: WO-051. `make_policy("soundings", **kw)` imports `soundings.eval.soundings_policy` lazily, so this WO never imports `soundings.runner`.
- Plumbing for WO-051's mis-specified variant: add `likelihoods: LikelihoodSpec | None = None` to `soundings.sim.run.run_scenario`, used only for acoustic sampling. This is a one-keyword edit to WO-011's file; the preferred route is that WO-011 ships the keyword before it merges (open question to the coordinator), in which case this WO only tests it.

Out: the `soundings` policy and the runner's `sim_likelihoods` kwarg (WO-051); aggregation, medians, report and CLI (WO-051); perturbation (WO-051); any change to ladder thresholds (CLAUDE.md §3). Baselines never go through `LadderMachine`, because its guards would correctly refuse a closure on first contact. They are counterfactual logs that use the same `LadderState` names, validated against the enum.

## Spec references
`docs/BUILD-PLAN.md` Phase 5 · `docs/SPEC-LADDER.md` §1, §3, §4 (boundary: `any_closure` is person-approved) · `docs/SPEC-INVESTIGATION.md` §1 (hypothesis ids), §5 (`request_watch_officer_look` counts as an interruption) · `docs/SPEC-SIM.md` §2 (false alarms, gaps → new contact ids), §3 (expected outcomes) · `config/ladder.yaml` (`budgets`, `costs`, `watch_after_handoff`) · `docs/WORKFLOW.md` §8.

## Files expected
- `soundings/eval/__init__.py`, `soundings/eval/policies.py`
- `config/eval.yaml` (this WO owns the whole file). `baselines:` holds labeled assumptions, not ladder thresholds: `human_review_s: 120`, `berth_reopen_after_s: 600`, `assumption_note` text. `eval:` holds WO-051's CLI defaults: `seeds: [1, 2, 3, 4, 5]`, `perturb_magnitude: 0.5`, `perturb_seed: 17`.
- `soundings/sim/run.py` (the one `likelihoods` keyword, unless WO-011 already ships it)
- `tests/test_policies.py`, `tests/test_policies_integration.py`

## Exit tests (must all pass with `MODEL_MODE=replay`, no network)
Every BUILD-PLAN Phase 5 exit test is owned by WO-051. This WO's gate:

Unit:
- [ ] `pytest tests/test_policies.py::test_policyrun_roundtrip` — proves `PolicyRun` and `ContactOutcome` round-trip through JSON, and that floats come out through `round6`.
- [ ] `pytest tests/test_policies.py::test_count_interruptions_table` — proves the four interruption sources each count 1 and nothing else counts (≥ 6 hand-built logs).
- [ ] `pytest tests/test_policies.py::test_shutdown_interval_union` — proves overlapping closures on one berth are merged, while closures on two berths add up.
- [ ] `pytest tests/test_policies.py::test_missed_threat_rules` — proves an `UNAUTHORIZED_UUV` contact that reached `HANDED_OFF` under a re-acquired id is not missed, one that ends `RESOLVED_BENIGN` is missed, and an undetected one is missed with `reason="undetected"`.
- [ ] `pytest tests/test_policies.py::test_false_shutdown_rules` — proves a hold on a `BIOLOGIC` contact or a sim false alarm is a false shutdown, and a hold on `UNAUTHORIZED_UUV` is not.
- [ ] `pytest tests/test_policies.py::test_human_queue_serial` — proves that three simultaneous contacts are handed off at +120, +240 and +360 s with `human_review_s=120`.
- [ ] `pytest tests/test_policies.py::test_sim_likelihoods_kwarg` — proves `run_scenario(..., likelihoods=spec)` with an unpinned S05 copy and a `propeller_tonal_present` UUV row of `[1.0, 0.0]` yields no tonal detections. With `likelihoods=None`, the track hash equals the hash produced without the kwarg, so the default path is unchanged.

Integration (real catalog scenarios end to end):
- [ ] `pytest tests/test_policies_integration.py::test_s05_baselines` — S05 at seed 1: `bang` closes the berth nearest the contact after an `AWAITING_APPROVAL` step, has `shutdown_minutes > 0`, and is not a missed threat; `human_queue` has `shutdown_minutes == 0` and is not a missed threat.
- [ ] `pytest tests/test_policies_integration.py::test_catalog_baseline_facts` — over S01..S08 × seeds 1..5: `bang` has 0 missed threats and positive shutdown-minutes on every scenario with a detected ground-truth contact; `human_queue` has ≥ 1 interruption per detected contact. (The three-policy comparisons are WO-051's.)
- [ ] `pytest tests/test_policies_integration.py::test_identical_world` — for every (scenario, seed), both baselines see the same sim output as a plain `run_scenario` call: equal `track_hash`, which proves the baselines are replayed on identical scenarios.

Determinism:
- [ ] `pytest tests/test_policies_integration.py::test_policy_runs_byte_identical` — runs each policy twice on S02, S05 and S07 at seeds 1 and 2; `canonical_json(PolicyRun)` is byte-identical.
- [ ] Shell: `for i in 1 2; do uv run python -c "from soundings.eval.policies import make_policy; from soundings.sim.scenario import resolve_scenario; from soundings.canonical import canonical_json; print(canonical_json(make_policy('bang').run(resolve_scenario('S06'), 3)))" > /tmp/bang$i.json; done; diff /tmp/bang1.json /tmp/bang2.json` → empty. Proves the CLI-free path is deterministic.

Rule proofs (CLAUDE.md §2):
- [ ] §2.1 `pytest tests/test_policies_integration.py::test_nothing_after_handoff` — every log from both baselines over the catalog only goes from `HANDED_OFF` to `WATCH_AFTER_HANDOFF`, and from there only to itself or `RESOLVED_DEPARTED`.
- [ ] §2.1 / §3 boundary `pytest tests/test_policies.py::test_bang_closure_is_person_approved` — every `bang` closure is preceded by `AWAITING_APPROVAL`, and its action string is a member of `boundary.hand_to_person.person_approves` read from `config/ladder.yaml`. The baseline cannot invent a platform-alone closure.
- [ ] §2.2 `pytest tests/test_policies.py::test_baselines_import_no_llm` — an AST scan of `soundings/eval/policies.py` shows it imports nothing from `soundings.llm` or `soundings.runner`.
- [ ] §2.4: the determinism items above.
- [ ] §2.5 `pytest tests/test_policies.py::test_policy_vocabulary_clean` — `find_denylisted` returns nothing over policy names, action strings, every serialized `PolicyRun` from the catalog run, and `config/eval.yaml`. Also `grep -iEf config/denylist.txt soundings/eval config/eval.yaml` is empty.

## Interfaces
Exposed by WO-050 (consumed by WO-051 and Phase 7 twist proofs):
- `soundings.eval.policies.PolicyName` (StrEnum: `soundings`, `bang`, `human_queue`; this order is the column order everywhere)
- `soundings.eval.policies.POLICIES: tuple[PolicyName, ...]`
- `soundings.eval.policies.Policy` (Protocol: `name: PolicyName`; `run(scenario, seed, *, sim_likelihoods: LikelihoodSpec | None = None) -> PolicyRun`)
- `soundings.eval.policies.PolicyRun` with fields:
  - `policy, scenario_id, seed, variant ("nominal" | "misspecified"), planner ("fallback" | "llm" | "none")`
  - `transition_log: tuple[Transition, ...], terminal_state: LadderState` (of the scenario's first ground-truth contact)
  - `rung_histogram: dict[Rung, int], human_interruptions: int, shutdown_minutes: float, slow_minutes: float, probes_used: int`
  - `seconds_seen_to_handled: float | None, missed_threat: bool, false_shutdown: bool`
  - `contacts: tuple[ContactOutcome, ...], sim_hours: float, track_hash: str, planner_stats: PlannerStats | None`
- `soundings.eval.policies.ContactOutcome` with fields: `observed_contact_id, gt_id | None, truth: Hypothesis | None, is_false_alarm, first_seen_s, handled_s | None, terminal_state, max_rung, interruptions, shutdown_minutes, false_shutdown, probes_used, probe_sequence: tuple[ProbeId, ...]`
- `soundings.eval.policies.GtOutcome`, `soundings.eval.policies.outcome_for_gt(run, gt_id) -> GtOutcome`
- `soundings.eval.policies.PlannerStats` (`calls, fixture_hits, guard_rejections, fallbacks`)
- `soundings.eval.policies.BangPolicy(cfg)`, `soundings.eval.policies.HumanQueuePolicy(cfg)`, `soundings.eval.policies.make_policy(name, **kw) -> Policy` (`soundings` resolves lazily to WO-051's `SoundingsPolicy`)
- `soundings.eval.policies.THREAT_HYPOTHESES`, `soundings.eval.policies.is_threat`, `soundings.eval.policies.count_interruptions`, `soundings.eval.policies.max_rung` (over WO-040's `RUNG_OF_STATE`; no second state-to-rung table), `soundings.eval.policies.merge_intervals`
- `soundings.eval.policies.EvalConfig`, `soundings.eval.policies.load_eval_config(path="config/eval.yaml")`
- `soundings.sim.run.run_scenario(..., likelihoods=None)` (kwarg added)
- File: `config/eval.yaml` (`baselines:` and `eval:` blocks)

Consumed:
- WO-010: `soundings.schemas` names `Hypothesis`, `Rung`, `LadderState`, `HANDLING_TERMINAL_STATES`, `ProbeId`, `Scenario`, `LikelihoodSpec`, `load_ladder_config`, `find_denylisted`; also `soundings.canonical.canonical_json` and `round6`.
- WO-010: `soundings.schemas.harbor.Harbor.nearest_asset` (berth chosen by `bang`).
- WO-011: `soundings.sim.run.run_scenario`, `soundings.sim.types.SimRun` (`detections`, `contact_truth`, `tick_s`, `n_ticks`), `soundings.sim.types.Detection.is_false_alarm`, `soundings.sim.hashing.track_hash`, `soundings.sim.scenario.resolve_scenario`, `soundings.sim.scenario.catalog_paths`.
- WO-040: `soundings.ladder.table.RUNG_OF_STATE`.

## Cut line
If more than an hour late, drop in this order:
1. The serial human queue: use a constant `human_review_s` per contact instead.
2. Merging the closure request when the berth is already closed: count one request per contact. `human_queue` re-checks still keep it strictly highest.
3. `slow_minutes` (report it as 0, labeled).

Never cut an exit test.

## Model calls
None. Paid run needed? No.
Dependency adds needing owner approval (CLAUDE.md §3): none.

## Rungs touched
None changed. The code reads all five rungs to build histograms. `bang` models a person-approved closure at hand to a person.

## Hard rules exercised
§2.1, §2.2, §2.4, §2.5: see Rule proofs above.

## Done
PR: · Merged commit: · Test summary: · STATE.md updated: yes/no

## Coordinator addenda (2026-10-16 23:05 PT, binding; see docs/log/DECISIONS.md)
- Approved: baselines are counterfactual logs outside LadderMachine with an assumed person approval; config/eval.yaml holds `human_review_s: 120`, `berth_reopen_after_s: 600`, labeled assumptions; human_queue shutdown-minutes reported as 0 and labeled.
