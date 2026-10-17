# WO-051 — eval-harness

Phase: 5 · Branch: `wo/051-eval-harness` · Worktree: `../soundings-worktrees/wo-051-eval-harness` · Est: 6.5 h · Owner: domain / eval / deck (unassigned); coordinator Kudrat
Depends on: WO-010 (`LikelihoodSpec`, `canonical_json`, `round6`), WO-011 (catalog, `catalog_paths`, `resolve_scenario`), WO-032 (`soundings.llm.planner.LlmPlanner`, violations-log path), WO-033 and WO-043 (`ScenarioRunner`, `RunResult` v2/v3 fields), WO-040 (`RUNG_OF_STATE`), WO-050 (`Policy`, `PolicyRun`, counting rules, `run_scenario(likelihoods=)`, `config/eval.yaml`); WO-000 is merged (Makefile `eval` target, `ModelClient`, `FixtureStore`). Wave 9.

> **Binding addendum (owner directive 2026-10-17 06:35 PDT):** see `WO-051-ADDENDUM-eval-directive.md` — mis-specified sim ±20%, toggle sweep, threshold sensitivity curve, BT-01 scale 40/100/200, seeds 1–20 (mean, min–max), planner ablation after the fixture run, `make eval` < 5 min. It extends the exit tests below.

## Goal
Running `make eval` produces `artifacts/eval/report.md` and `artifacts/eval/report.json`. It runs the catalog × seeds × 3 policies in replay mode with no network. Two runs give byte-identical output. The report has:
- the metric tables that `docs/DECK-INPUTS.md` pastes, with stable columns;
- a mis-specified variant, where the sim draws acoustics from perturbed likelihood tables but the scorer keeps the real ones, showing how outcomes shift;
- an auto-populated section "Where the LLM layer added nothing", built by diffing the LLM-planner and fallback-planner runs. With no fixtures, this section says so plainly.

Parallel start: `perturb.py`, `metrics.py` and `report.py` can be built and unit-tested against hand-built `PolicyRun` objects once WO-050 merges (wave 3). `SoundingsPolicy` and the end-to-end tests need runner v3 (WO-043).

## Scope
In:
- `soundings/eval/soundings_policy.py` (moved here from the WO-050 draft): `SoundingsPolicy(planner="fallback" | "llm", fixture_store=None, violations_log=None)` implements WO-050's `Policy`. It runs `ScenarioRunner(scenario, seed=seed, sim_likelihoods=...).run(llm_roles=(planner == "llm"), violations_path=violations_log, fixture_store=fixture_store)` and maps the `RunResult` (`transitions`, `ladder`, `handling_outcome`, `probe_calls`, `planner_choices`, `sim`) to a `PolicyRun` in one `_from_run_result`, using WO-050's counting helpers. `PlannerStats` comes from `planner_choices`. Nothing is re-decided in eval code.
- `soundings/runner.py`, only inside the block `# --- sim_likelihoods (WO-051) ---`: the constructor kwarg `sim_likelihoods: LikelihoodSpec | None = None`, passed only to `run_scenario(likelihoods=...)`. The scorer keeps loading `config/likelihoods.yaml`.
- `soundings/eval/perturb.py`:
  - `perturb_likelihoods(spec, magnitude, seed)`: multiplies each row by `exp(magnitude·z)`, with `z ~ N(0,1)` drawn from `numpy.random.default_rng(seed)` in sorted (feature, hypothesis) order. It then renormalizes, applies `round6`, and puts the rounding residual on the largest bucket so every row sums to exactly 1.
  - `unpin_acoustics(scenario)`: removes scenario acoustic pins for features that have a likelihood table. Without this, a pinned feature would ignore the perturbed table.
  - `table_shift(a, b)`: returns max and mean absolute difference.
- `soundings/eval/metrics.py`, the per-policy aggregates over a list of `PolicyRun`:
  - rung histogram (max rung per ground-truth contact);
  - `% at or below ask`, where a ground-truth contact counts if and only if:
    - its rolled-up terminal state is in `AT_OR_BELOW_ASK_TERMINALS = {CLEARED_AT_ASK, RESOLVED_BENIGN, HAZARD_NOTICE, LOST_VEHICLE}`, and
    - its max rung is `watch` or `ask`.
  - Denominator: ground-truth contacts in the catalog (S03 contributes two divers), × seeds. Sim false alarms are reported in their own row and are not in the denominator. Expected nominal result: S01, S02, S03×2, S04, S07, S08 = 7 of 9 = 77.8%. S05 and S06 correctly hand off.
  - missed threats, false shutdowns, median seconds seen to handled (over handled contacts, with the unhandled count shown beside it), interruptions per simulated hour, shutdown-minutes, shutdown USD (`shutdown_minutes × costs.shutdown_usd_per_min_default`), probes used.
  - `diff_planners`: matches runs by (scenario, seed, gt contact) and compares terminal state, max rung, probe sequence, probes used and seconds seen to handled. It sums `PlannerStats`.
- `soundings/eval/report.py`:
  - `EvalReport` (pydantic); `render_markdown` with fixed column constants; `write_report`, which writes `canonical_json` for the JSON and `\n` line endings for the Markdown.
  - No timestamps, hostnames or absolute paths. The header carries `inputs_sha256` over `config/*.yaml`, the suite's scenario files and the CLI arguments.
- `soundings/eval/harness.py`:
  - `run_campaign(suite, seeds, *, policies, perturb_magnitude, perturb_seed, include_misspecified, include_llm_diff, fixture_store=None, violations_log)`;
  - `resolve_suite`: `catalog` resolves to `catalog_paths()` (S01..S08, sorted). `twist0N` resolves to `twists/0N/suite.yaml` (a list of scenario paths) if present, else `twists/0N/scenarios/*.yaml`, sorted. An unknown suite exits 2;
  - `parse_seeds`: accepts `1,2,3,4,5` and `1..5`, with sorted unique output.
- `soundings/eval/__main__.py`: a typer CLI.
  - It accepts exactly the Makefile line `--suite catalog --seeds 1,2,3,4,5 --mode replay --out artifacts/eval`, plus the BUILD-PLAN form with `--seeds 1..5` and no `--out`.
  - Default `--out` is `artifacts/eval` for `catalog` and `artifacts/eval/<suite>` otherwise.
  - Options: `--perturb-magnitude` (default 0.5), `--perturb-seed` (default 17), `--no-misspecified`, `--no-llm-diff`. Defaults are read from `config/eval.yaml` `eval:` (WO-050 owns the file).
  - It sets `MODEL_MODE` from `--mode`. `--mode record|live` prints the estimated call count (contacts × `llm_calls_per_contact`) and exits 3 with "eval campaigns are paid runs; needs `approve paid run`". It makes no client call.
  - The violations log for eval runs goes to `<out>/violations.md`, never to `docs/log/VIOLATIONS.md`.
- Report sections, in order:
  1. Header: suite, seeds, inputs hash, "stress-test estimate" label.
  2. `## Outcomes by policy` with columns `policy | gt_contacts | at_or_below_ask_pct | watch | ask | slow | hold | hand_to_person | missed_threats | false_shutdowns | median_s_seen_to_handled | unhandled | interruptions_per_hour | shutdown_minutes | shutdown_usd | probes_used`.
  3. `## Sim false alarms by policy`.
  4. `## Per scenario` with columns `scenario | policy | expected_terminal | terminal_states | matches_expected | missed | false_shutdown | shutdown_minutes | interruptions`.
  5. `## Mis-specified world` (perturbation magnitude, seed, table shift; then `policy | metric | nominal | misspecified | delta`, and the scenarios whose terminal state changed).
  6. `## Where the LLM layer added nothing`.
  7. `## Assumptions`, covering:
     - the shared-tables note;
     - the baseline modeling choices from `config/eval.yaml`;
     - shutdown USD, written as "$0: no mentor-validated figure yet (`config/ladder.yaml` `costs.shutdown_usd_per_min_default = 0`)";
     - human_queue shutdown shown as 0 because the person's decisions are not simulated;
     - the interruption definition: the eval counts every person-facing event (approval request, handoff, eyes-on probe, watch update; WO-050 `count_interruptions`), while a scenario's `max_human_interruptions` counts approval requests and backup pages (WO-040 `ActionLedger.interruptions`).
- LLM section contents: calls, fixture hits, guard rejections, fallbacks, and a table of differing runs. When `fixture_hits == 0` it prints `NO_LLM_EFFECT_SENTENCE` verbatim: "No recorded probe_planner fixtures were used in this run; every LLM-planner call fell back to the deterministic planner, so outcomes are identical by construction. The LLM layer added nothing measurable here." When there are hits and no differences, it says "LLM planner chose the same probes as the fallback on every run". When there are differences, it lists them and says whether each one helped: fewer probes, a lower rung, or a faster outcome for the same terminal state.

Out: plots, an LLM-as-judge pass on SITREPs, live or record campaigns, threshold tuning to make numbers look better (CLAUDE.md §3), and `docs/DECK-INPUTS.md` edits. The deck seat pastes from the report.

## Spec references
`docs/BUILD-PLAN.md` Phase 5, Phase 7 (`--suite twist0N`) · `docs/SPEC-SIM.md` §2 (mis-specified runs), §3 (expected outcomes) · `docs/SPEC-INVESTIGATION.md` §6 (eval reports where the LLM planner differed), §8 (honesty labels) · `docs/DECK-INPUTS.md` "Slide: eval" · `docs/TWIST-PLAYBOOK.md` step 5 · `config/ladder.yaml` `costs`, `budgets` · `docs/WORKFLOW.md` §7, §8 · `docs/log/DECISIONS.md` (USD 0 until a mentor figure).

## Files expected
- `soundings/eval/soundings_policy.py`, `soundings/eval/perturb.py`, `soundings/eval/metrics.py`, `soundings/eval/report.py`, `soundings/eval/harness.py`, `soundings/eval/__main__.py`
- `soundings/runner.py` (`# --- sim_likelihoods (WO-051) ---` block only)
- `tests/test_eval.py`, `tests/test_soundings_policy.py`, `tests/test_perturb.py`, `tests/test_metrics.py`, `tests/test_eval_cli.py`, `tests/test_eval_llm_diff.py`
- `tests/fixtures/eval_static/eval_valid_camera_first.json`, `tests/fixtures/eval_static/eval_violating_rung.json` (hand-written, outside `model_calls/` so WO-032's key-consistency test never reads them; the second proposes a rung and an approval instead of a probe), and `tests/helpers/static_client.py` (captures S04's real `probe_planner` inputs with a spy, as WO-033's adversarial test does, then installs these outputs into a `tmp_path` `FixtureStore` under the computed keys; the runner reaches them through `fixture_store=`, with no network)
- Outputs (gitignored): `artifacts/eval/report.md`, `artifacts/eval/report.json`, `artifacts/eval/violations.md`

## Exit tests (must all pass with `MODEL_MODE=replay`, no network)
From BUILD-PLAN (verbatim):
- [ ] `python -m soundings.eval --suite catalog --seeds 1..5 --mode replay` runs all scenarios × 3 policies and writes `artifacts/eval/report.md` + `report.json`.
- [ ] Metrics present per policy: where each contact resolved (rung histogram), missed threats (must be 0 for Soundings on the catalog), false shutdowns, median seconds from seen to handled, human interruptions per hour, shutdown-minutes × configured $/min, probes used.
- [ ] `pytest tests/test_eval.py` — report is deterministic; Soundings resolves ≥ 70% of catalog contacts at or below `ask`; `bang` has 0 missed threats and the highest shutdown-minutes; `human_queue` has the highest interruptions.
- [ ] The report has a section titled "Where the LLM layer added nothing" that is auto-populated by diffing LLM-planner vs fallback-planner outcomes.

Unit:
- [ ] `pytest tests/test_perturb.py` — proves:
  - every perturbed row sums to 1.000000 and every bucket is > 0;
  - `magnitude=0` returns a table equal to the input;
  - the same seed gives an equal table and a different seed gives a different one;
  - mean absolute shift increases with magnitude over 0.1, 0.5 and 1.0;
  - `unpin_acoustics(S05)` leaves no pinned feature that has a table;
  - the input spec is not mutated.
- [ ] `pytest tests/test_metrics.py` — on hand-built `PolicyRun`s, proves:
  - the at-or-below-ask rule: `LOST_VEHICLE` at max rung ask counts; `CLEARED_AT_ASK` after an `ADVISING_SLOW` does not; a false alarm is excluded from the denominator;
  - the median ignores unhandled contacts and reports them;
  - interruptions per hour use summed `sim_hours`;
  - USD is 0 with the default config and equals minutes × rate with a monkeypatched rate.
- [ ] `pytest tests/test_eval_cli.py::test_seed_forms` — proves `1,2,3,4,5`, `1..5` and `5,1..4` all parse to `(1,2,3,4,5)`, and that `1..`, `a` and `0..-1` exit 2.
- [ ] `pytest tests/test_eval_cli.py::test_suite_resolution` — proves `catalog` resolves to eight sorted paths, and that `twist01` resolves from a temporary `twists/01/suite.yaml` and from a temporary `twists/01/scenarios/` folder, each giving an output folder under `artifacts/eval/twist01`.

Integration (real catalog end to end):
- [ ] `pytest tests/test_soundings_policy.py` (moved from the WO-050 draft) — S05 at seed 1: `soundings` ends `HANDED_OFF`, passes through `HOLDING`, is not a missed threat, and `bang.shutdown_minutes > soundings.shutdown_minutes`. For seeds 1..5, S01 and both S03 divers end `CLEARED_AT_ASK` and S04 `RESOLVED_BENIGN`, at max rung ≤ `ask` with 0 interruptions. All three policies see equal `track_hash` per (scenario, seed). With no fixtures, `planner="llm"` gives outcomes identical to `planner="fallback"`. `sim_likelihoods=None` leaves the runner's `transition_log_hash` unchanged.
- [ ] `make eval` exits 0 and writes `artifacts/eval/report.md` and `report.json`. This proves the exact Makefile invocation is accepted.
- [ ] `pytest tests/test_eval.py::test_catalog_claims` — one session-scoped campaign (catalog × seeds 1..5) asserts:
  - Soundings `at_or_below_ask_pct ≥ 70.0`, and S01, S03 and S04 are counted for every seed;
  - Soundings has 0 missed threats;
  - `bang` has 0 missed threats and strictly the highest `shutdown_minutes`;
  - `human_queue` has strictly the highest `interruptions_per_hour`;
  - every metric column named in BUILD-PLAN is present in both the JSON and the Markdown.
- [ ] `pytest tests/test_eval.py::test_columns_stable` — proves the Markdown table headers equal the `REPORT_COLUMNS` constants exactly, so the deck paste never breaks silently.
- [ ] `pytest tests/test_eval.py::test_misspecified_section` — proves the mis-specified section is present and lists magnitude, seed and table shift. With `--perturb-magnitude 0` every delta is 0 and no scenario changes terminal state, which shows the variant only differs by the tables. The scorer's likelihoods equal `config/likelihoods.yaml` in both variants.
- [ ] `pytest tests/test_eval_llm_diff.py::test_no_fixtures_honest` — with an empty fixture directory, the section contains `NO_LLM_EFFECT_SENTENCE` verbatim, 0 differing runs, `fixture_hits == 0`, and `fallbacks == calls`.
- [ ] `pytest tests/test_eval_llm_diff.py::test_valid_fixture_diff` — with the static client returning `eval_valid_camera_first.json` on S04 at seed 1, the section lists S04 with the differing probe sequence and a verdict. The terminal state is still produced by the stopping rule.

Determinism:
- [ ] `pytest tests/test_eval.py::test_report_deterministic` — two campaigns into two `tmp_path` folders give byte-identical `report.md` and `report.json`.
- [ ] Shell: `uv run python -m soundings.eval --suite catalog --seeds 1,2,3,4,5 --mode replay --out /tmp/e1 && uv run python -m soundings.eval --suite catalog --seeds 1..5 --mode replay --out /tmp/e2 && diff -r /tmp/e1 /tmp/e2` → empty. This proves both seed forms and two processes give identical output, including the mis-specified run, whose perturbation is seeded.

Rule proofs (CLAUDE.md §2):
- [ ] §2.2 `pytest tests/test_eval_llm_diff.py::test_violating_fixture_rejected` — the static client returns `eval_violating_rung.json`. The guard rejects it, and one line is appended to `<tmp>/violations.md` naming role and reason. The contact's outcome is identical to the fallback run, and the LLM section counts 1 guard rejection. This proves model output cannot set a rung, even inside the eval.
- [ ] §2.3 `pytest tests/test_eval_cli.py::test_live_mode_refused` — `--mode live` and `--mode record` exit 3, print the estimated call count, and never construct a live client (it is monkeypatched to raise). `test_no_network` runs the full CLI on S01 and S05 with seed 1 while `socket.socket` is monkeypatched to raise, and it exits 0.
- [ ] §2.4: the determinism items above.
- [ ] §2.6 `pytest tests/test_eval.py::test_honesty_labels` — proves the report contains "stress-test estimate", the shared-tables note, the USD "$0: no mentor-validated figure yet" label, and every `config/eval.yaml` assumption by name.
- [ ] §2.5 `grep -iEf config/denylist.txt artifacts/eval/report.md soundings/eval` → empty. `test_eval.py::test_report_vocabulary_clean` runs `find_denylisted` over both artifacts.

## Interfaces
Exposed by WO-051 (consumed by WO-062, Phase 7 twist proofs, and the deck seat):
- `soundings.eval.soundings_policy.SoundingsPolicy`; `soundings.runner.ScenarioRunner(..., sim_likelihoods=None)` (constructor kwarg)
- `soundings.eval.perturb.perturb_likelihoods(spec, magnitude, seed) -> LikelihoodSpec`, `soundings.eval.perturb.unpin_acoustics(scenario) -> Scenario`, `soundings.eval.perturb.table_shift(a, b) -> TableShift`
- `soundings.eval.metrics.PolicyMetrics`, `soundings.eval.metrics.compute_metrics(runs, *, ladder_cfg) -> dict[PolicyName, PolicyMetrics]`, `soundings.eval.metrics.AT_OR_BELOW_ASK_TERMINALS`, `soundings.eval.metrics.at_or_below_ask(gt_outcome) -> bool`, `soundings.eval.metrics.median_seconds`, `soundings.eval.metrics.LlmDiff`, `soundings.eval.metrics.diff_planners(fallback_runs, llm_runs) -> LlmDiff`
- `soundings.eval.report.EvalReport`, `soundings.eval.report.build_report(...)`, `soundings.eval.report.render_markdown(report) -> str`, `soundings.eval.report.write_report(report, out_dir) -> tuple[Path, Path]`, `soundings.eval.report.REPORT_COLUMNS`, `soundings.eval.report.NO_LLM_EFFECT_SENTENCE`
- `soundings.eval.harness.run_campaign(...) -> EvalReport`, `soundings.eval.harness.resolve_suite(name) -> list[Path]`, `soundings.eval.harness.parse_seeds(text) -> tuple[int, ...]`
- CLI: `python -m soundings.eval --suite {catalog|twist0N} --seeds {1,2,3,4,5|1..5} --mode replay [--out DIR] [--perturb-magnitude F] [--perturb-seed N] [--no-misspecified] [--no-llm-diff]`
- Files: `artifacts/eval/report.md`, `artifacts/eval/report.json`

Consumed:
- WO-050: `soundings.eval.policies.{PolicyName, POLICIES, Policy, PolicyRun, ContactOutcome, GtOutcome, outcome_for_gt, PlannerStats, make_policy, count_interruptions, max_rung, merge_intervals, is_threat, EvalConfig, load_eval_config}`, `soundings.sim.run.run_scenario(likelihoods=)`.
- WO-040: `soundings.ladder.table.RUNG_OF_STATE`.
- WO-033 / WO-043: `soundings.runner.ScenarioRunner` (`run(llm_roles=, violations_path=, fixture_store=)`), `RunResult.{transitions, ladder, handling_outcome, probe_calls, planner_choices, sim}`.
- WO-032: `soundings.llm.planner.LlmPlanner` (reached through runner v3), `soundings.llm.guard.append_violation` (violations path is a parameter).
- WO-011: `soundings.sim.scenario.{catalog_paths, resolve_scenario}`.
- WO-010: `soundings.schemas.{LikelihoodSpec, load_likelihood_spec, load_ladder_config, Scenario, LadderState, Rung, find_denylisted}`, `soundings.canonical.{canonical_json, round6}`.
- WO-000: `soundings.llm.client.{ModelClient, FixtureStore}`.

## Cut line
If more than an hour late, drop in this order:
1. The per-scenario list of terminal-state changes in the mis-specified section; keep the aggregate deltas.
2. The "helped or not" verdict per LLM difference; keep the diff list.
3. `twists/0N/suite.yaml` support; keep the `scenarios/` folder form.
4. The sim false-alarm table.

Never cut an exit test.

## Model calls
Roles reached through runner v3 in the LLM-planner arm are `probe_planner` and `hypothesis_writer`, both in replay. Tests use hand-written fixtures through a static stub client; there are no recorded fixtures and no live calls. Each contact makes at most `budgets.llm_calls_per_contact` planner calls and none are retried in a loop. Paid run needed? No. A live LLM-arm campaign would be a paid run and is refused by the CLI.
Dependency adds needing owner approval (CLAUDE.md §3): none (typer, numpy, pydantic and pytest are in the Phase 0 set; network blocking uses monkeypatch, not pytest-socket).

## Rungs touched
None changed. The code reports on watch, ask, slow, hold and hand to a person.

## Hard rules exercised
§2.2, §2.3, §2.4, §2.5, §2.6: see Rule proofs above.

## Done
PR: · Merged commit: · Test summary: · STATE.md updated: yes/no

## Coordinator addenda (2026-10-16 23:05 PT, binding; see docs/log/DECISIONS.md)
- 70% rule: a ground-truth contact counts as resolved at or below ask when its terminal state ∈ {CLEARED_AT_ASK, RESOLVED_BENIGN, HAZARD_NOTICE, LOST_VEHICLE} and its max rung ≤ ask. Report false-alarm contacts on a separate line, not in the denominator. Expected 7 of 9 on the catalog.
- Use the single interruption definition from WO-022's addenda.
- If main lacks `run_scenario(..., likelihoods=None)` when WO-051 starts, WO-051 adds that keyword (one small commit in soundings/sim).

## Part A done (coordinator split)
Built on main without runner v3 (2026-10-17 06:10 PDT). Branch `wo/051a-eval-core`; 42 new tests; `make check` green, 1454 passed after rebasing on WO-031, WO-032, WO-062 part A and WO-043 part A.

Built:
- `soundings/eval/perturb.py`: `perturb_likelihoods(spec, magnitude, seed)`, `unpin_acoustics(scenario, spec=None)`, `table_shift(a, b) -> TableShift(max_abs, mean_abs, cells)`, `ACOUSTIC_PIN_FEATURES`, `ZERO_FLOOR` (1e-4; structural zeros are floored so every perturbed bucket is > 0), `MIN_BUCKET` (1e-6).
- `soundings/eval/metrics.py`: `PolicyMetrics`, `FalseAlarmMetrics`, `compute_metrics(runs, *, ladder_cfg)`, `compute_false_alarms(runs)`, `policy_metrics`, `rolled_outcomes(run)`, `AT_OR_BELOW_ASK_TERMINALS`, `at_or_below_ask(gt)`, `median_seconds(values) -> (median, unhandled)`, `LlmDiff`, `PlannerDifference`, `diff_planners(fallback_runs, llm_runs)` (with the "helped or not" verdict), `DIFF_FIELDS`.
- `soundings/eval/report.py`: `EvalReport`, `build_report(...)`, `render_markdown`, `write_report(report, out_dir) -> (md, json)`, `REPORT_COLUMNS` (outcomes, false_alarms, per_scenario, misspecified, misspecified_changes, llm_diff), `NO_LLM_EFFECT_SENTENCE`, `SAME_PROBES_SENTENCE`, `PENDING_RUNNER_V3`, `inputs_sha256`, `misspecified_section`, `llm_section`.
- `soundings/eval/harness.py`: `run_campaign(suite, seeds, *, policies, perturb_magnitude, perturb_seed, include_misspecified, include_llm_diff, fixture_store, violations_log, mode, soundings_factory, use_default_soundings, twists_root, ladder_cfg, eval_cfg)`, `resolve_suite`, `parse_seeds`, `default_out`, `SuiteError`, `SeedsError`, and the Protocol `SoundingsFactory.__call__(*, planner, fixture_store, violations_log) -> Policy`. `default_soundings_factory()` returns None until `soundings.eval.soundings_policy` exists, then builds it through WO-050 `make_policy("soundings", planner=, fixture_store=, violations_log=)`; with None the report lists soundings as `pending runner v3` in every table.
- `soundings/eval/__main__.py`: typer CLI; the Makefile line, `--seeds 1..5`, `--suite twist0N`, default `--out`, `--perturb-magnitude/--perturb-seed` (defaults from `config/eval.yaml`), `--no-misspecified`, `--no-llm-diff`; `--mode record|live` prints `estimated model calls: N` and exits 3; bad suite/seeds/mode exit 2.
- Tests: `tests/test_perturb.py`, `tests/test_metrics.py`, `tests/test_eval_cli.py` (`test_seed_forms`, `test_suite_resolution`, `test_live_mode_refused`, `test_no_network`), `tests/test_eval.py` (baselines over S01..S08 x seeds 1,2; `test_columns_stable`, `test_misspecified_section`, magnitude 0, `test_honesty_labels`, `test_report_vocabulary_clean`, `test_report_deterministic` as two CLI processes).

Mis-specified reading: both columns of that section run the suite with acoustic pins cleared; `nominal` samples from config/likelihoods.yaml, `misspecified` from the perturbed copy (so magnitude 0 gives zero deltas). The baselines ignore acoustics, so their deltas are 0 by construction.

Remains for Part B:
- `soundings/eval/soundings_policy.py` (`SoundingsPolicy(planner=, fixture_store=, violations_log=)` over runner v3) and the runner's `# --- sim_likelihoods (WO-051) ---` block; `tests/test_soundings_policy.py`.
- `tests/test_eval.py::test_catalog_claims` (catalog x seeds 1..5, soundings >= 70% at or below ask, S01/S03/S04 counted every seed, 0 missed threats; bang strictly highest shutdown-minutes and human_queue strictly highest interruptions over all three policies); the scorer-likelihood check in `test_misspecified_section`.
- `tests/test_eval_llm_diff.py`, `tests/helpers/static_client.py`, `tests/fixtures/eval_static/*.json`.
- The owner addendum `WO-051-ADDENDUM-eval-directive.md` items 2a-2e and 3 (perturbation at ±20%: pass the magnitude that maps to it, toggle sweep, threshold sweep, BT-01 scale, seeds 1-20 mean/min-max, planner ablation); part A does none of them.
- `make eval` with all three policies; drop the `SOUNDINGS_PENDING` branches in `tests/test_eval.py` once soundings runs.

## Part B1 done (coordinator split, 2026-10-17 08:11 PDT)
Branch `wo/051b1-eval-archives-traps`. Built on main over the baselines (the soundings policy adapter is still part B).

Built:
- `soundings/eval/archive.py`: every CLI run writes `<out>/runs/<stamp>-<key>/` (`stamp` = `YYYYMMDDTHHMMSSZ` UTC, `key` = first 12 hex of the report's `inputs_sha256`). The folder is created with `mkdir(exist_ok=False)` and every file with `O_CREAT|O_EXCL`; order `config.json` (resolved args + `config/eval.yaml` + `config/ladder.yaml`), `manifest.json` (git sha, mode, suite, seeds, per-file input sha256, report hashes), `report.md`, then `report.json` last as the completion marker. Only then are `<out>/report.md` and `<out>/report.json` refreshed as the latest copies. `newest_archive` orders by the stamp in the name, never mtime. CLI: `--stamp` (bad stamp exits 2); an existing folder exits 4 and nothing is overwritten.
- `soundings/eval/families.py`: `FAMILIES` (tuesday S01 S03 S04 S07 S08; harbor_night S05 S06; lost_vehicle S02; busy BT-01, skipped until `scenarios/BT-01.yaml` exists; traps TR-01..04); `resolve_suite` accepts a family name; default out `artifacts/eval/<family>`.
- `soundings/eval/grader.py`: `grade(run, expected, *, observations=None, gt_id=None) -> GradeResult`; one `KeyGrade` (pass/fail/not_graded, expected, observed, reason) per declared `Expected` key; `max_human_interruptions` uses the one interruption definition (CLAUDE.md §8 A5, WO-050 `interruption_kind`); `top_hypothesis`, `min_threat_mass` and the SITREP keys are `not_graded` unless supplied in `Observations`; `RULES` covers every `Expected` field (tested).
- `scenarios/traps/` (committed): TR-01 sea lion charging the fuel berth at night (no hand-off), TR-02 slow-closing unfiled vehicle in daylight (hand-off), TR-03 loiterer beside the fuel berth (hand-off), TR-04 debris cluster at night (no hand-off); `traps.yaml` states each answer and a threat_mass band per checkpoint, written before any run. `ScenarioId` accepts `TR-NN`.
- `soundings/eval/traps.py`: `run_traps(seeds)` scores each trap's observed prefix at every checkpoint (sim -> evidence -> ask-rung hail -> posterior) and reports threat_mass against the band; `threshold_sweep` replays the readings at `stopping.threat_mass_handoff` 0.25..0.45 step 0.05 (scorer-level: hand-off when any checkpoint reaches the threshold; other stop rules not applied; config never edited). `--suite traps` writes the trap report through the same archive.

First reading (seeds 1..3, bands untouched): 0 false shutdowns at every threshold; missed threats 1/4/4/5/5 of 6 threat-trap runs at 0.25/0.30/0.35/0.40/0.45. TR-02 and TR-03 often top out as FILED_OFF_PLAN because registry rows are still the Phase 2 `unknown` placeholder in the evidence table, so the scorer-level threat_mass stays below the band. This is a calibration finding for part B and the sensitivity curve, not a reason to move the bands or the threshold.

Shell determinism note: with archives, `diff -r /tmp/e1 /tmp/e2` is empty when both runs pass the same `--stamp`; without it the two `runs/` folder names differ by stamp while every `report.json` is byte-identical.
