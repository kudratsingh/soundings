# WO-093 — fixture-recording-harness

Phase: 7 · Branch: `wo/093-fixture-recording-harness` · Worktree: `../soundings-worktrees/wo-093-fixture-recording-harness` · Est: 1.5 h · Owner: coordinator Claude / Kudrat

Depends on: WO-032 (FixtureStore, ModelClient, roles, guard), WO-033 (runner v2, `make_kit`, `llm_roles`), WO-042 (SitrepWriterRole, HandoffService `writer_mode="auto"`), WO-043 (runner v3: `ScenarioRunnerV3`, `sitrep_client`, `catalog_verify`). Reads WO-087 (bounded live mode) for `TransportReply` usage tokens.

## Goal
The one planned paid run (CLAUDE.md §8 A4, WO-051 addendum item 1: probe_planner + hypothesis_writer over S04–S08 and the BT-01 rogue, sitrep_writer over every handoff scenario) becomes three commands: `plan` prints the real call count and token estimate with the exact `scripts/paid-run-gate.py` line, `record` (refused unless every gate is satisfied) records each distinct fixture key once, and `verify` proves the recorded fixtures replay with zero misses and change no catalog outcome. The planner ablation entry point the addendum's item 3 needs exists and says plainly when the LLM layer added nothing.

## Scope
In:
- `soundings/llm/fixtures.py`, `python -m soundings.llm.fixtures plan | record | verify`.
- `plan`: replay mode, `llm_roles=True`, a capturing fixture store (never calls out) that records every (role, inputs, fixture key, prompt) the system would send; de-duplicated by key; per-role table (calls, distinct keys, already recorded, to record, avg input tokens = ceil(chars/4) of system prompt + canonical inputs, stated output-token estimate); total; the gate command; `artifacts/fixtures/plan.json` (gitignored).
- `record`: refuses under `SOUNDINGS_CI`, unless `MODEL_MODE=record`, unless `SOUNDINGS_PAID_RUN_APPROVED` equals the gate's exact phrase, without `MODEL_NAME` or the price env, and when the plan's worst-case USD exceeds the remaining budget (`$15` cap minus `docs/log/PAID-RUNS.md`). Then replays the same scenario set with a `ModelClient(mode=record, transport=anthropic_transport)` behind the store, so each distinct key is called once; stops at the first transport error or before the call that would exceed the plan by more than 10 %; appends the PAID-RUNS row; prints a summary.
- `verify`: replay, recorded fixtures present: zero replay misses, zero guard violations beyond those recorded as rejected, and every scenario's catalog expectation failures no worse than the deterministic run's.
- `soundings/eval/ablation.py`, `python -m soundings.eval.ablation --planners llm,deterministic,random --scenarios S04-S08,BT-01 --mode replay`.
- `docs/WORKFLOW.md` §7 "How to run it" block (commands only); one DECISIONS row.
Out:
- Any live model call (the record path runs in tests only through an injected stub transport).
- Changing a role prompt, a fixture key, a guard, a threshold or a catalog expected block.
- Running the paid run itself (owner, ~12:30, after `approve paid run ...`).

## Spec references
CLAUDE.md §2.3, §2.4, §5, §8 A4; `docs/WORKFLOW.md` §7; `docs/work-orders/WO-051-ADDENDUM-eval-directive.md` items 1 and 3; WO-032/033/042/043 Done sections.

## Interfaces
- `soundings.llm.fixtures`: `RUN_SET` (scenario id -> `RunConfig(llm_roles, writer_mode)`), `CapturingStore(FixtureStore)`, `RecordingStore(FixtureStore)`, `run_scenario_set(store, *, scenarios=None, violations_path) -> ScenarioSetRun`, `build_plan(...) -> FixturePlan`, `gate_command(plan) -> str`, `approval_phrase(plan) -> str`, `remaining_budget(paid_runs_path, *, cap, desc) -> float`, `record(...) -> RecordSummary`, `verify(...) -> VerifyReport`, `main(argv) -> int`; constants `PAID_RUN_CAP_USD = 15.0` (DECISIONS 2026-10-17, WO-093), `APPROVAL_ENV = "SOUNDINGS_PAID_RUN_APPROVED"`, `OVERRUN_FRACTION = 0.10`, `OUT_TOKENS_ESTIMATE` per role.
- `soundings.eval.ablation`: `run_ablation(planners, scenarios, *, mode="replay") -> AblationReport`, `ablation_sentence(report) -> str`, `main(argv) -> int`; the random arm is a seeded `RandomPlannerStore` that answers `probe_planner` with a uniformly chosen allowlisted, rung-permitted, in-budget, not-yet-run probe through the same guarded `LlmPlanner` path.

## Files expected
- `soundings/llm/fixtures.py` (module CLI: `python -m soundings.llm.fixtures`)
- `soundings/eval/ablation.py`
- `tests/test_fixture_harness.py`, `tests/test_ablation.py`
- `docs/WORKFLOW.md` (§7 block), `docs/log/DECISIONS.md` (one row), `docs/STATE.md` (WO-093 row)

## Exit tests (must all pass with `MODEL_MODE=replay`)
- [ ] `pytest tests/test_fixture_harness.py -k plan` — `plan` on S04 and S05 is deterministic (two runs, identical JSON), counts > 0 per role, the gate command line is well-formed.
- [ ] `pytest tests/test_fixture_harness.py -k refuses` — `record` refuses without `MODEL_MODE=record`, without the approval env, under `SOUNDINGS_CI`, and when the plan exceeds the remaining budget; no transport call in any refusal.
- [ ] `pytest tests/test_fixture_harness.py -k stub_record` — `record` with a stub transport writes exactly the planned distinct keys, appends the PAID-RUNS row, and a second `record` makes zero transport calls; a transport error stops the run after one call.
- [ ] `pytest tests/test_fixture_harness.py -k verify` — `verify` passes against stub-recorded fixtures.
- [ ] `pytest tests/test_ablation.py` — the ablation table and sentence; with no fixtures llm == deterministic, reported plainly.
- [ ] `SOUNDINGS_FIXTURE_ROOT=$(mktemp -d) uv run pytest -q` — the full suite passes with an empty fixture root.
- [ ] `uv run python -m soundings.llm.fixtures plan` — the real plan table (zero network), pasted in the PR.

## Model calls
None in this WO. The harness prepares the owner's fixture run: `plan` prints the count and estimate; the run needs `approve paid run record fixtures S04-S08 BT-01 handoffs` (or the BT-01-pending wording the plan prints).

## Rungs touched
ask (probe planner, hypothesis writer) and hand to a person (SITREP writer); none changed.

## Hard rules exercised
- §2.2: `verify` asserts the recorded replies change no catalog outcome beyond the deterministic run (`-k verify`).
- §2.3: `record` refusals (`-k refuses`); the stub transport is the only transport any test injects.
- §2.4: plan determinism (`-k plan`); each distinct key is called once and a second record makes zero calls.

## Cut line
1. BT-01 rogue (if `scenarios/BT-01*.yaml` is not on main the harness prints "BT-01 pending" and runs without it).
2. The ablation's random arm (deterministic + llm only).
3. `verify`'s catalog comparison (keep zero misses and zero unexpected violations).

## Done
PR: · Merged commit: · Test summary: · STATE.md updated: yes/no
