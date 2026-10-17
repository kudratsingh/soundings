# WO-031 — probes-planner

Phase: 3 · Branch: `wo/031-probes-planner` · Worktree: `../soundings-worktrees/wo-031-probes-planner` · Est: 5 h · Owner: Kudrat
Depends on: WO-010 (`ProbeId`, `Rung`, `EvidenceRow`, `load_ladder_config`), WO-011 (`SimRun`, `Detection`, `ContactTruth`), WO-012 (`EvidenceTable`, the extractors, `PolTable`), WO-020 (`RegistryStore`), WO-021 (`HailSimulator`, `hail_evidence_rows`, `soundings.probe_keys`), WO-022 (`soundings.matcher.registry_lookup`, `registry_lookup_rows`), WO-030 (`Posterior`, `expected_entropy_after`)

## Goal
The only way new evidence enters the table during an investigation (SPEC-INVESTIGATION §5). When this WO lands:
- an allowlist registry holds every §5 probe with its exact cost, latency, and rung;
- each probe has an idempotency key and runs through a cache, so a repeated key never re-executes;
- each probe returns typed `EvidenceRow`s with `probe_id` set.

It also adds the deterministic probe planner (§6). The planner picks the allowlisted probe with the highest expected information gain per unit cost. It never exceeds the `config/ladder.yaml` budgets and never proposes a probe above the current rung. No probe exists at `slow` or above, and the allowlist is tested against the rung list in `config/ladder.yaml`.

Concurrency: steps 1 to 3 (allowlist, keys, cache, watch probes) need only WO-012 and can start alongside WO-030. Ask probes need WO-020/021/022 merged. The planner (steps 5 and 6) needs WO-030.

## Scope
In: `soundings/investigation/probes.py`, the deterministic half of `soundings/investigation/planner.py`, budget and cost loaders, and tests.
Out:
- the LLM planner and guard (WO-032)
- the stopping rule, the machine, and runner wiring (WO-033)
- camera "visual class" output: it has no likelihood column, so it would be stretch item 2
- any threshold, budget, or cost change in `config/ladder.yaml`
- persistence of the cache beyond the process (WO-042's audit log records the keys)

## Spec references
`docs/SPEC-INVESTIGATION.md §5` (probe table, idempotency key), `§6` (deterministic planner, budgets), `§2` (row kinds); `docs/SPEC-LADDER.md §1` (INVESTIGATING is rung ask), `§4` (boundary: what is decided alone at ask); `config/ladder.yaml` (`rungs`, `budgets`, `costs`); CLAUDE.md §2.1, §2.2, §2.4.

## Definitions (binding)
- **Allowlist.** `ALLOWLIST: dict[ProbeId, ProbeSpec]` in §5 order. `ProbeSpec(probe_id, rung_required: Rung, cost_usd, latency_s, interruption: int, returns: tuple[FeatureName, ...])`. Values are copied exactly from §5:

  | probe | rung | cost_usd | latency_s | interruption |
  |---|---|---|---|---|
  | registry_lookup | watch | 0 | 0 | 0 |
  | kinematics_profile | watch | 0 | 0 | 0 |
  | drift_check | watch | 0 | 0 | 0 |
  | pattern_of_life_check | watch | 0 | 0 | 0 |
  | ais_surface_correlate | watch | 0 | 1 | 0 |
  | acoustic_feature_read | watch | 0 | 2 | 0 |
  | acoustic_hail | ask | 0.01 | 20 | 0 |
  | camera_cue | ask | 0.05 | 30 | 0 |
  | second_sensor_dwell | ask | 0.10 | 60 | 0 |
  | ask_operator_by_phone | ask | 0 | 120 | 0 |
  | request_watch_officer_look | ask | 0 | 60 | 1 |

- **What each probe returns** (rows carry `source="probe:<id>"`, `probe_id=<id>`, and `t` = the tick when the result lands):
  - Watch probes call the WO-012 extractors on the track truncated at that tick.
  - `registry_lookup` calls `soundings.matcher.registry_lookup` over `RegistryStore.overlapping_ticks` and appends `soundings.matcher.registry_lookup_rows` (`registry_match`, `plan_id`).
  - `acoustic_hail` calls `soundings.challenge.hail.HailSimulator.send` and appends `hail_evidence_rows` (`hail_sent_at`, `hail_reply`, `modem_id`, `operator_known`).
  - `camera_cue` returns `visibility_for_camera` (a fact; `fog_no_camera` forces 0).
  - `second_sensor_dwell` returns `depth` and `speed_over_ground` from the two best nodes over the latency window. Confidence is 1 − (1 − c1)(1 − c2).
  - `ask_operator_by_phone` returns `operator_known`, matched from the scenario's `known_operators` by `modem_id`. When there is no `modem_id` it returns `unknown`.
  - `request_watch_officer_look` returns `depth` and `transit_pattern` from the simulated eyes-on, with confidence equal to the `visibility_for_camera` value.
  - A feature a probe cannot compute becomes an `unknown` row, never a missing one.
- **Idempotency key.** Re-exported from `soundings.probe_keys` (WO-021), so the hail and every other probe share one formula: `idempotency_key(contact_id, probe_id, args, tick_bucket)` = sha256 hex of `canonical_json([contact_id, probe_id, args or {}, tick_bucket])`, `tick_bucket = (tick * tick_s) // TICK_BUCKET_S`, `TICK_BUCKET_S = 30`.
- **Cache.** `ProbeCache` maps key to `ProbeResult(key, probe_id, rows, landed_tick)`. `run_probe` on a cached key returns the stored result with `cached=True`. It appends nothing and does not call the executor; an execution counter proves this.
- **Rung check.** `run_probe` raises `ProbeNotAllowed` when `rung_index(spec.rung_required) > rung_index(ctx.current_rung)`. The rung order comes from `load_ladder_config().rungs`, never from a literal list.
- **Information gain.**
  - `expected_information_gain(posterior, spec, observed, tables)` = Σ over the spec's likelihood features of (H(post) − `expected_entropy_after(post, f, tables, delta_c)`), where `delta_c = max(0, c_probe − c_observed[f])`.
  - The independent per-feature sum is an approximation; state it in the docstring.
  - Features with no likelihood table add 0.
- **Score.**
  - `score = gain / max(DENOM_FLOOR, cost_usd + latency_cost_weight_per_s * latency_s + interruption_cost_weight * interruption)`.
  - The weights come from `config/ladder.yaml` `costs`. `DENOM_FLOOR = 1e-3` keeps free zero-latency probes finite.
  - Ties break by allowlist order.
  - A probe with gain ≤ 1e-9 is never chosen.
- **Budgets** (`config/ladder.yaml` `budgets`).
  - `BudgetLedger` counts, per contact, the probes the planner chose (`probes_per_contact`), the probes with cost > 0 (`paid_probes_per_contact`), and interruptions (`human_interruptions_per_contact`).
  - The ask-rung `acoustic_hail` that runner v1 already ran counts as paid. The fixed watch sweep does not count toward `probes_per_contact`; see open questions.
  - The planner filters out any probe that would push any counter past its limit.
  - `PlannerChoice(probe_id=None, reason="no_informative_probe_in_budget")` is returned when nothing qualifies.

## Steps
1. `ProbeSpec`, `ALLOWLIST`, `rung_index`, `ProbeNotAllowed`, `idempotency_key`, `tick_bucket`, `TICK_BUCKET_S`.
2. `ProbeContext(run: SimRun, scenario, harbor, current_field, pol, registry, tick, current_rung, contact_id)`, `ProbeResult`, `ProbeCache`, and `run_probe(probe_id, ctx, table, *, args=None, cache) -> ProbeResult`. `run_probe` appends rows to the table and is the single executor entry point.
3. The six watch-probe executors, which wrap WO-012 extractors.
4. The five ask-probe executors (they need WO-020/021/022).
5. `planner.py`: `BudgetLimits`, `load_budget_limits`, `BudgetLedger`, `CostWeights`, `load_cost_weights`, `DENOM_FLOOR`, `expected_information_gain`, `probe_score`, `PlannerState(contact_id, posterior, observed_confidence, current_rung, ledger, tick, run_keys)`, `PlannerChoice(probe_id, gain, score, ranked, reason, source: Literal["deterministic", "llm", "fallback"] = "deterministic", fallback_reason: str | None = None, why: str | None = None, question_for_human: str | None = None)` (all fields defined here so WO-032 never edits this file), the `Planner` Protocol, and `DeterministicPlanner.choose(state, tables)`.
6. Tests, `make check`, a STATE.md line, and a PR.

## Files expected
- `soundings/investigation/probes.py`, `soundings/investigation/planner.py`
- `tests/test_probes.py`, `tests/test_probe_planner.py`, `tests/test_probes_catalog.py`

## Exit tests (must all pass with `MODEL_MODE=replay`, no network)
From BUILD-PLAN Phase 3 (verbatim):
- [ ] `pytest tests/test_probes.py` — every probe is in the allowlist, has a cost, a latency, an idempotency key, and returns typed evidence rows; a probe called twice with the same key does not re-execute.

The BUILD-PLAN `pytest tests/test_probe_planner.py` line is owned by WO-032, which adds its LLM clause in its own file `tests/test_probe_planner_llm.py`. This WO creates and alone owns `tests/test_probe_planner.py` (deterministic and budget clauses, below).

Unit:
- [ ] `uv run pytest tests/test_probes.py -k allowlist_exact` — the 11 ids equal `ProbeId`, and each cost, latency, rung, and interruption equals the table above. Proves §5 is copied, not paraphrased.
- [ ] `uv run pytest tests/test_probes.py -k nothing_at_or_above_slow` — reads `rungs` from `config/ladder.yaml` and asserts every `rung_required` index < index("slow"). Also asserts no probe id equals a rung, a `LadderState`, or a boundary action name. Proves investigation cannot climb (CLAUDE.md §2.1).
- [ ] `uv run pytest tests/test_probes.py -k idempotency` — the same (contact, probe, args, tick in the same 30 s bucket) gives the same key. A changed arg or the next bucket gives a new key. The second `run_probe` returns `cached=True`, the executor counter stays at 1, and the table row count does not change.
- [ ] `uv run pytest tests/test_probes.py -k rung_enforced` — `run_probe("acoustic_hail", ctx(current_rung=watch))` raises `ProbeNotAllowed` and appends nothing.
- [ ] `uv run pytest tests/test_probe_planner.py -k eig_cases` — 7 table-driven cases on hand-built tables, each asserting the chosen id and that its score is the max in `ranked`:
  - (1) everything unobserved: a zero-latency watch probe wins;
  - (2) only paid probes remain with `hail_reply` unobserved: `acoustic_hail` beats `camera_cue` (gain 0);
  - (3) `depth` observed at c = 0.2: `second_sensor_dwell` beats `request_watch_officer_look` because of the interruption weight;
  - (4) all likelihood features observed at c = 1: the result is `None`;
  - (5) a tie between two free probes breaks by allowlist order;
  - (6) the interruption limit is reached, so `request_watch_officer_look` is filtered;
  - (7) the paid limit is reached, so only free probes rank.
- [ ] `uv run pytest tests/test_probe_planner.py -k budget_property` — for random tables and ledgers (`derandomize=True`), a loop of choose and record never exceeds any of the three budgets and never returns a probe above `current_rung`.
- [ ] `uv run pytest tests/test_probe_planner.py -k weights_from_config` — doubling `interruption_cost_weight` in a tmp copy of `ladder.yaml` changes case 3's ranking as predicted. Proves the weights are read from config, not hardcoded.

Integration:
- [ ] `uv run pytest tests/test_probes_catalog.py` — S05 at seed 11 and S07 at seed 17, run from the YAML on disk:
  - `run_scenario` and `build_evidence` run first. Then every allowlisted probe goes through `run_probe` at the contact's last tick against the real `SimRun`, `RegistryStore` (temp `SOUNDINGS_DB`), and hail sim.
  - Every returned row validates as `EvidenceRow` with `probe_id` set.
  - S05 `hail_reply` is `none` or `timeout`, and S07 `breathing_or_click_present` is `true`.
  - `DeterministicPlanner` on the post-sweep S05 table returns the same `PlannerChoice` twice.
  - This proves probes and the planner work on real simulator output.

Determinism:
- [ ] `uv run pytest tests/test_probes_catalog.py -k table_hash_stable` — the S05 table after the full probe sweep has an equal `EvidenceTable.table_hash()` across two fresh runs. Proves CLAUDE.md §2.4 for probe execution and for the planner's input.

Guard / rule proof:
- [ ] CLAUDE.md §2.1 (the work stops at the handoff; nothing climbs): `nothing_at_or_above_slow` and `rung_enforced`.
- [ ] CLAUDE.md §2.2 (no model output becomes a control): `grep -n "soundings.llm" soundings/investigation/probes.py` → empty, because probes take only typed ids from `ALLOWLIST`; `budget_property` proves the deterministic chooser respects every limit by itself.
- [ ] CLAUDE.md §2.5: `grep -iEf config/denylist.txt soundings/investigation/probes.py soundings/investigation/planner.py tests/test_probe*.py` → empty; `make check` green.

## Interfaces
Exposed by WO-031:
- `soundings.investigation.probes`: `ProbeSpec`, `ALLOWLIST`, `TICK_BUCKET_S`, `tick_bucket`, `idempotency_key` (these three re-exported from `soundings.probe_keys`), `rung_index`, `ProbeNotAllowed`, `ProbeContext`, `ProbeResult`, `ProbeCache`, `run_probe`
- `soundings.investigation.planner`: `BudgetLimits`, `load_budget_limits`, `BudgetLedger`, `CostWeights`, `load_cost_weights`, `DENOM_FLOOR`, `expected_information_gain`, `probe_score`, `PlannerState`, `PlannerChoice`, `Planner`, `DeterministicPlanner`

Consumed:
- WO-010: `soundings.schemas.ProbeId`, `soundings.schemas.Rung`, `soundings.schemas.LadderState`, `soundings.schemas.EvidenceRow`, `soundings.schemas.UNKNOWN`, `soundings.schemas.config.load_ladder_config`, `soundings.canonical.canonical_json`
- WO-011: `soundings.sim.types.SimRun`, `soundings.sim.types.Detection`, `soundings.sim.types.ContactTruth`, `soundings.sim.run.run_scenario`
- WO-012: `soundings.investigation.evidence.EvidenceTable`, `soundings.investigation.features.extract_kinematics`, `.extract_acoustic`, `.extract_context`, `.build_evidence`, `soundings.investigation.pol.anomaly_score`
- WO-020: `soundings.registry.store.RegistryStore` (`overlapping_ticks`)
- WO-021: `soundings.challenge.hail.HailSimulator`, `.hail_evidence_rows`, `.load_known_operators`; `soundings.probe_keys.TICK_BUCKET_S`, `.tick_bucket`, `.idempotency_key`
- WO-022: `soundings.matcher.registry_lookup`, `.registry_lookup_rows`, `.RegistryLookup`
- WO-030: `soundings.investigation.posterior.Posterior`, `.expected_entropy_after`, `.LikelihoodTables`

Used by: WO-032 (`PlannerState`, `PlannerChoice`, `DeterministicPlanner`, `ALLOWLIST`, `BudgetLedger`), WO-033 (all of it), WO-051 (`PlannerChoice.source` for the LLM diff).

## Cut line
If the WO runs more than an hour late, drop in this order (exit tests unchanged):
1. `request_watch_officer_look` returns only `depth` (drop `transit_pattern`).
2. `second_sensor_dwell` uses the single best node at c + 0.2, capped at 1, instead of combining two nodes.
3. `ask_operator_by_phone` returns `unknown` unless `modem_id` is present.

## Model calls
None. Paid run needed? No.
Dependency adds needing owner approval (CLAUDE.md section 3): none.

## Rungs touched
watch (six free probes) and ask (five probes, including the one human interruption). Nothing at slow, hold, or hand to a person.

## Hard rules exercised
- §2.1: `nothing_at_or_above_slow` and `rung_enforced`.
- §2.2: the `soundings.llm` grep and `budget_property`.
- §2.4: `table_hash_stable` and the idempotency tests.
- §2.5: the denylist grep.

## Done
PR: (see `wo/031-probes-planner` PR) · Merged commit: squash commit on main (see PR) · Test summary: `make check` exit 0, full suite 1310 passed after rebase onto WO-032 part A; WO-031 tests 87 (test_probes 55, test_probe_planner 26, test_probes_catalog 6) · STATE.md updated: yes (WO-031 row)

- Every exit test passes with `MODEL_MODE=replay`; the `soundings.llm` grep and the denylist grep print nothing. Determinism: two separate processes running the S05/S07 sweep plus a planner choice give an empty diff (S05 `table_hash` `3682c432…756dcc`, S07 `26b17259…89c453`).
- Interface as implemented (differences from the text above):
  - `ProbeSpec` adds `nominal_confidence` (the planner's `c_probe`; section 5 gives none) and a `paid` property. Values in `NOMINAL_CONFIDENCE`: 0.5 for track-derived reads (`TRACK_DERIVED_CONFIDENCE`), 0.0 for `ais_surface_correlate` (no surface feed), 0.9 hail, 0.75 dwell, 1.0 camera and phone, 0.5 eyes-on.
  - `ProbeContext` adds optional `hail`, `match_cfg`, `known`, `tide_phase_for_hour`. `ProbeResult` adds `contact_id`, `sent_tick`, `args`. `ProbeCache.count(probe_id=None)` is the execution counter. Extra exports: `probe_key`, `resolve_args`, `latency_ticks`, `as_probe_id`, `restamp`, `with_provenance`, `track_upto`, `fog_active`, `camera_visibility`.
  - With no args, `registry_lookup` and `ask_operator_by_phone` take the contact's latest known `modem_id` row (`{"modem_id": ...}`, runner v1's key shape); the hail key equals `HailSimulator.key`. `acoustic_hail` reads `previously_replied` from the table's `hail_reply` rows unless args give it.
  - `PlannerState` adds `tick_s` (default 2.0), `probe_confidence` (per-probe `c_probe` override) and `ran_probes` (skipped at any tick). `PlannerChoice.ranked` is a tuple of `RankedProbe(probe_id, gain, score)`; `why`/`question_for_human` are `FreeText`. `BudgetLedger(limits)` with `allows`, `record(..., planner_chosen=True)` (raises `BudgetExceeded`), `record_ask_hail`, `counts`, `remaining`, `copy`. Helpers `observed_confidence` and `state_from_table`.
- Choices beyond the text (DECISIONS.md WO-031 row): `camera_cue` writes a `derived` row (from the WO-012 visibility assumption) instead of a fact, because the table never mixes fact and assumption on one feature; `acoustic_feature_read` keeps the latest reading per acoustic feature; watch probes keep their provenance rows (`track_segment`, assumptions, context), re-stamped as probe rows; `registry_lookup` keeps WO-022's row sources; `request_watch_officer_look` reads the simulator path like the hail does; `nothing_at_or_above_slow` checks boundary actions at rungs >= slow only, because ask's `platform_alone` lists four probe ids. No cut-line item was used.
