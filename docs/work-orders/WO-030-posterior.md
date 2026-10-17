# WO-030 — posterior

Phase: 3 · Branch: `wo/030-posterior` · Worktree: `../soundings-worktrees/wo-030-posterior` · Est: 3.5 h · Owner: Kudrat
Depends on: WO-000 (scaffold, `soundings.llm.client.canonical_json`), WO-010 (enums, `EvidenceRow`, config loaders), WO-011 (`run_scenario`, used only by the integration test), WO-012 (`EvidenceTable`, `bucketize`, `build_evidence`)

## Goal
A deterministic naive-Bayes posterior over the eight fixed hypotheses (SPEC-INVESTIGATION §1, §3), computed only from the WO-012 evidence table. When this WO lands, likelihood and prior loaders exist and reject any row that does not sum to 1 within 1e-6. Priors are conditioned by the `adjustments` block in `config/priors.yaml`. Rows are confidence-weighted per §9.10. The posterior reports Shannon entropy, top-2 margin, `threat_mass`, and `benign_mass`. There is also a one-feature expected-entropy helper; WO-031's planner builds its information gain on it. Every number can be traced to a YAML cell a mentor can edit. Nothing here can set a rung, and no model output enters.

Concurrency: this WO needs only WO-010 and WO-012 merged. It can run alongside WO-031 steps 1 to 3 (the probe allowlist does not read the posterior) and alongside the WO-032 guard work.

## Scope
In: `soundings/investigation/posterior.py`, its tests, and a dump CLI.
Out:
- probes and the planner (WO-031)
- the stopping rule (WO-033)
- pattern-of-life conditioning of the prior beyond the `adjustments` block: pattern of life enters only as the `pattern_of_life_anomaly` feature
- multi-contact correlation (§9.6)
- sensor-health re-weighting beyond what WO-012 already folds into `confidence` (§9.7)
- any change to the threshold or likelihood values in `config/*.yaml`

## Spec references
`docs/SPEC-INVESTIGATION.md §1` (hypotheses, priors), `§2` (row columns, `unknown`), `§3` (posterior, aggregates, required tests), `§9.10` (confidence weighting); `config/likelihoods.yaml`; `config/priors.yaml`; `docs/WORKFLOW.md §8` (6 dp at output boundaries); CLAUDE.md §2.2, §2.4, §2.6.

## Definitions (binding)
- **Hypothesis order** follows `soundings.schemas.Hypothesis` (spec order). Every dict output uses this order.
- **Aggregates.**
  - `threat_mass` = P(UNAUTHORIZED_UUV) + P(UNAUTHORIZED_DIVER).
  - `benign_mass` = P(FILED_ON_PLAN) + P(BIOLOGIC) + P(DEBRIS_OR_DRIFT) + P(SURFACE_VESSEL_AIS_OFF).
  - FILED_OFF_PLAN and UNFILED_KNOWN_OPERATOR are in neither. This is the spec as written (WO-033's `C_KNOWN_OPERATOR` clause handles S04 without changing these sums).
- **Row-sum validation.** `load_likelihood_tables` wraps WO-010's `load_likelihood_spec`, which checks shape only.
  - It raises `LikelihoodTableError(feature, hypothesis, sum)` when a row is off by more than `ROW_SUM_TOL = 1e-6`, or has a negative cell.
  - `load_prior_config` raises when `base` does not sum to 1 within 1e-6.
- **Prior conditioning.** `conditioned_prior(cfg, hour_of_day, zone_criticality)`:
  - starts from `base`;
  - multiplies by each `adjustments` entry whose condition holds: `night_hours` when `hour_of_day ∈ hours`, `critical_zone` when `zone_criticality ≥ zone_criticality_min`. Entries are applied in YAML order;
  - then renormalizes.
  - An `unknown` or absent condition means that adjustment is skipped. It is never assumed true.
- **Which rows count.** One row per likelihood feature: `EvidenceTable.latest(contact_id, feature)`.
  - A feature's rows are repeated readings of the same quantity. Multiplying them all in would double-count.
  - The row is skipped when `bucketize(feature, value)` returns `None`, which covers `unknown` and absent. Skipped means never imputed.
  - Features without a likelihood table (for example `zone_id`) are ignored.
  - Every skip is recorded with a reason: `unknown`, `absent`, or `no_table`.
- **Confidence weighting (§9.10).** A row with confidence `c ∈ [0,1]` contributes `P(bucket | h) ** c`.
  - c = 1 is a full naive-Bayes factor. c = 0 contributes nothing.
  - A confidence of `"unknown"` is treated as c = 0, and the row is recorded as skipped with reason `unknown_confidence`.
  - This rule goes in the module docstring word for word.
- **Degenerate update.** When every hypothesis gets weight 0 (for example, depth `mid` after only SURFACE_VESSEL_AIS_OFF survives):
  - the posterior stays unchanged;
  - the row id goes into `degenerate_row_ids`.
  - No probability floor is applied, so the tables stay exactly as a judge reads them.
- **Entropy** is Shannon entropy in bits (log2), with 0·log 0 = 0. `top2_margin` = p(1st) − p(2nd).
- **Expected entropy after a feature.** `expected_entropy_after(posterior, feature, tables, delta_c=1.0)` = Σ_b P(b)·H(post | b, weight delta_c), where P(b) = Σ_h P(h)·P(b|h).
- **Output.**
  - `to_dict()` rounds to 6 dp (`round6`).
  - It carries `"label": "stress-test estimate"` (`STRESS_TEST_LABEL`).
  - Its keys are `posterior, entropy_bits, top2_margin, threat_mass, benign_mass, top`.

## Steps
1. Loaders:
   - `LikelihoodTables`, built from WO-010 `LikelihoodSpec`, with `.features()`, `.buckets(f)`, `.likelihood(f, bucket, h)`, and `.row(f, h)`.
   - `load_likelihood_tables(path=config/likelihoods.yaml)`.
   - `PriorConfig` and `load_prior_config(path=config/priors.yaml)`.
2. `Posterior`: a frozen Pydantic model holding `probs: dict[Hypothesis, float]`. It has:
   - `update(feature, bucket, confidence, tables) -> Posterior`, which returns a new object;
   - `entropy()`, `top2_margin()`, `threat_mass()`, `benign_mass()`, `top()`, and `to_dict()`.
   - Constructor check: probabilities are non-negative and sum to 1 within 1e-9.
3. `conditioned_prior(...)`, then `score_contact(table, contact_id, tables, prior_cfg) -> PosteriorReport`.
   - `PosteriorReport` holds `prior`, `posterior`, `used_row_ids`, `skipped: dict[str, str]`, and `degenerate_row_ids`.
   - Features are applied in `tables.features()` order. The result is order-independent mathematically; the fixed order keeps floats byte-stable.
4. `expected_entropy_after(...)`, a pure function. WO-031 is its only consumer.
5. CLI: `python -m soundings.investigation.posterior --scenario S05 --seed 11` prints `canonical_json(report.to_dict())`. The chain is `run_scenario`, then `build_evidence`, then `score_contact` for every contact in sorted order.
6. Tests (below), `make check`, a STATE.md line, and a PR.

## Files expected
- `soundings/investigation/posterior.py`
- `tests/test_posterior.py`, `tests/test_likelihood_tables.py`, `tests/test_posterior_catalog.py`
- `tests/fixtures/config/likelihoods_bad_rowsum.yaml`, `tests/fixtures/config/priors_bad_sum.yaml` (deliberately broken copies)

## Exit tests (must all pass with `MODEL_MODE=replay`, no network)
From BUILD-PLAN Phase 3 (verbatim):
- [ ] `pytest tests/test_posterior.py` — posterior sums to 1; adding a strongly-diagnostic evidence row moves mass in the expected direction (table-driven, ≥ 12 cases); conflicting evidence widens entropy rather than collapsing.

Unit (`tests/test_posterior.py`; property tests use `@settings(derandomize=True, max_examples=200)`):
- [ ] `uv run pytest tests/test_posterior.py -k sums_to_one` — for random subsets of features and buckets with random confidences, the posterior sums to 1 within 1e-9 and every p ≥ 0. Proves normalization.
- [ ] `uv run pytest tests/test_posterior.py -k direction` — table-driven, 14 cases. Each case starts from `base` (no adjustments), adds one row at c = 1, and asserts the named mass strictly rises. Proves every §3 direction claim plus one per hypothesis family:
  - (1) `hail_reply=valid` raises P(FILED_ON_PLAN)+P(FILED_OFF_PLAN)+P(UNFILED_KNOWN_OPERATOR).
  - (2) `drift_consistency=high` raises DEBRIS_OR_DRIFT.
  - (3) `propeller_tonal_present=true` and `drift_consistency=low` raise UNAUTHORIZED_UUV + FILED_*.
  - (4) `breathing_or_click_present=true` raises BIOLOGIC.
  - (5) `registry_match=exact` raises FILED_ON_PLAN.
  - (6) `registry_match=area_mismatch` raises FILED_OFF_PLAN.
  - (7) `ais_surface_correlation=strong` raises SURFACE_VESSEL_AIS_OFF.
  - (8) `depth=deep` raises UNAUTHORIZED_UUV.
  - (9) `transit_pattern=orbit` raises UNAUTHORIZED_UUV.
  - (10) `pattern_of_life_anomaly=anomalous` raises `threat_mass`.
  - (11) `speed_over_ground=drift` raises DEBRIS_OR_DRIFT.
  - (12) `transit_pattern=lawnmower` raises FILED_ON_PLAN.
  - (13) `hail_reply=none` lowers FILED_ON_PLAN.
  - (14) `loiter_score=high` raises `threat_mass`.
- [ ] `uv run pytest tests/test_posterior.py -k conflict_widens_entropy` — `drift_consistency=high` alone, then the same plus `propeller_tonal_present=true`: entropy rises and `top2_margin` falls. A second pair (`breathing_or_click_present=true` then `+depth=deep`) asserts the same. Proves conflict widens and never collapses.
- [ ] `uv run pytest tests/test_posterior.py -k missing_skipped` — a table where `hail_reply` is `unknown` and `registry_match` is absent gives the same posterior as a table without those rows, and `skipped` names both with reasons. Proves the "skipped, never imputed" rule.
- [ ] `uv run pytest tests/test_posterior.py -k confidence_weighting` — c = 0 leaves the posterior equal to the prior; c = 1 equals the plain naive-Bayes product computed by hand for one case; c = 0.5 lands strictly between them in KL distance. Proves the `likelihood ** c` rule stated in the docstring.
- [ ] `uv run pytest tests/test_posterior.py -k degenerate` — SURFACE-only posterior plus `depth=mid` is unchanged and lists the row in `degenerate_row_ids`. Proves no silent floor.
- [ ] `uv run pytest tests/test_posterior.py -k prior_adjustments` — hour 2 with criticality 0.8 applies both adjustments and renormalizes; the expected vector is hand-computed in the test. Hour 12 with an `unknown` criticality applies neither.
- [ ] `uv run pytest tests/test_posterior.py -k expected_entropy` — `expected_entropy_after` ≤ current entropy for every feature on 5 fixed posteriors (information never hurts in expectation), and it equals the current entropy when delta_c = 0.
- [ ] `uv run pytest tests/test_likelihood_tables.py` — every row in `config/likelihoods.yaml` sums to 1 within 1e-6; `config/priors.yaml` base sums to 1; the two broken fixtures raise `LikelihoodTableError` with the feature and hypothesis named. Proves the loader guards the catalog.

Integration:
- [ ] `uv run pytest tests/test_posterior_catalog.py` — runs WO-011 `run_scenario` and WO-012 `build_evidence` on the real catalog files. S05 at seed 11 gives top hypothesis UNAUTHORIZED_UUV with `threat_mass` ≥ 0.6. S07 at seed 17 gives top BIOLOGIC. S08 at seed 19 gives top DEBRIS_OR_DRIFT. Every catalog scenario's report sums to 1 and carries the stress-test label. Proves the scorer reads real evidence end to end. Registry and hail are still Phase 2 placeholders, so they are skipped here.

Determinism:
- [ ] `uv run python -m soundings.investigation.posterior --scenario S05 --seed 11 > /tmp/p1.json; uv run python -m soundings.investigation.posterior --scenario S05 --seed 11 > /tmp/p2.json; diff /tmp/p1.json /tmp/p2.json` → empty. Proves CLAUDE.md §2.4 for scoring.

Guard / rule proof:
- [ ] CLAUDE.md §2.2 (no model output becomes a control): `grep -n "soundings.llm" soundings/investigation/posterior.py` matches nothing except a `canonical_json` import, and `test_posterior.py::test_no_rung_in_output` asserts that `to_dict()` has no key or value that is a `Rung` member. The scorer cannot express a rung.
- [ ] CLAUDE.md §2.6 (probabilities are stress-test numbers): `test_posterior.py::test_stress_test_label` asserts the label is on every report.
- [ ] CLAUDE.md §2.5: `grep -iEf config/denylist.txt soundings/investigation/posterior.py tests/test_posterior*.py tests/test_likelihood_tables.py` → empty; `make check` green.

## Interfaces
Exposed by WO-030 (`soundings.investigation.posterior`):
- `HYPOTHESES`, `THREAT_HYPOTHESES`, `BENIGN_HYPOTHESES`, `STRESS_TEST_LABEL`, `ROW_SUM_TOL`
- `LikelihoodTableError`, `LikelihoodTables`, `load_likelihood_tables`, `PriorConfig`, `load_prior_config`, `conditioned_prior`
- `Posterior` (`.update`, `.entropy`, `.top2_margin`, `.threat_mass`, `.benign_mass`, `.top`, `.to_dict`), `PosteriorReport`, `score_contact`, `expected_entropy_after`
- CLI: `python -m soundings.investigation.posterior --scenario <id> --seed <n>`

Consumed:
- WO-010: `soundings.schemas.Hypothesis`, `soundings.schemas.Rung`, `soundings.schemas.config.load_likelihood_spec`, `soundings.schemas.config.LikelihoodSpec`, `soundings.schemas.config.load_priors`, `soundings.canonical.canonical_json`, `soundings.canonical.round6`
- WO-012: `soundings.investigation.evidence.EvidenceTable` (`latest`, `for_contact`), `soundings.investigation.features.bucketize`, `soundings.investigation.features.build_evidence`
- WO-011: `soundings.sim.run.run_scenario`, `soundings.sim.scenario.resolve_scenario` (CLI and integration only)

Used by: WO-031 (`Posterior`, `expected_entropy_after`, `score_contact`), WO-032 (`PosteriorReport` for the role inputs), WO-033 (`threat_mass`, `benign_mass`, `top`), WO-042 (`PosteriorReport.used_row_ids` for SITREP hypotheses), WO-043 (`score_contact` in the driver), WO-060 (posterior panel).

## Cut line
If the WO runs more than an hour late, drop in this order (exit tests unchanged):
1. Drop the dump CLI's multi-contact loop and print only the first contact. The determinism diff still runs.
2. Drop the 200-example property test to 50 examples.
3. Drop `PosteriorReport.skipped` reasons down to a plain list of feature names.

## Model calls
None. Paid run needed? No.
Dependency adds needing owner approval (CLAUDE.md section 3): none. The math uses `math` and plain floats; `numpy` is optional and already in the Phase 0 set.

## Rungs touched
None executed. The output feeds the ask-rung investigation and the hand-to-a-person recommendation in WO-033.

## Hard rules exercised
- §2.2: `test_no_rung_in_output` and the `soundings.llm` grep.
- §2.4: the two-run `diff`.
- §2.6: `test_stress_test_label`, plus the `missing_skipped` test (nothing is imputed, so "what we don't know" stays honest).
- §2.5: the denylist grep.

## Done
PR: see the WO-030 PR on GitHub (`feat(wo-030): hypothesis set, likelihood tables, naive-Bayes posterior`) · Merged commit: squash commit on main (see PR) · Test summary: `873 passed` (full suite; tests/test_posterior.py 43, test_likelihood_tables.py 8, test_posterior_catalog.py 13) · STATE.md updated: yes (WO-030 row)

- Exit tests: all pass with `MODEL_MODE=replay`; `make check` exits 0. The S05 seed 11 CLI dump is byte-identical across two runs (sha256 `e93c2528…f0a4`, empty diff). The `soundings.llm` grep matches nothing (`canonical_json` comes from `soundings.canonical` on main); the denylist grep is empty.
- Interface as implemented (all in `soundings.investigation.posterior`): the WO names plus `PRIOR_BASE`, `PROB_SUM_TOL`, `SKIP_*` reason constants, `DegenerateUpdateError` (raised by `Posterior.update` when every weight is 0; `score_contact` catches it and records the row), `applied_adjustments(cfg, hour, crit)`, `Posterior.from_weights/uniform/p/mass/ranked/rounded`, `LikelihoodTables.has`, `score_run_table(table, tables, prior_cfg)`. `conditioned_prior` returns a `Posterior`. `score_contact` reads `hour_of_day` and `zone_criticality` from the contact's latest rows. `PosteriorReport` also carries `contact_id`, `prior_adjustments`, `buckets` (feature -> bucket used) and `label`; its `to_dict()` is the posterior's `to_dict()` plus `contact_id, prior, prior_adjustments, used_row_ids, buckets, skipped, degenerate_row_ids`.
- The CLI prints one canonical JSON document `{label, scenario, seed, contacts: [report...]}` (contacts sorted); `--ticks`, `--pol`, `--no-pol` as in the WO-012 CLI.
- Deviations (DECISIONS 2026-10-16 23:40): the S05 catalog check uses the closing prefix (240 and 300 ticks) plus the merged WO-021 hail; the strict entropy-rise conflict pair is ordered tonal-then-drift (the WO's literal order is asserted on the margin collapse and entropy > 1.9 bits); the prior-sum error names `priors.base` with no hypothesis. No cut-line item was used.

