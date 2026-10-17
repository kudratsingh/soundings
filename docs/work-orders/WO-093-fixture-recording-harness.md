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
PR: see the WO-093 PR (`wo/093-fixture-recording-harness`) · Merged commit: squash on merge · Test summary: `make check` exit 0, 2204 passed, 20 skipped, 3 xfailed (tests/test_fixture_harness.py 22, tests/test_ablation.py 6) · STATE.md updated: yes (WO-093 row)

Built 2026-10-17 (gate: runner v3, PR #50, merged 10:21 PDT). No model was called; every `record` in tests runs through an injected stub transport.
- `soundings/llm/fixtures.py`, `python -m soundings.llm.fixtures plan|record|verify [--scenarios S04,S05] [--fixture-root DIR]` (`plan --out`, `record --paid-runs`). The set: S02 (sitrep only), S04 and S07 (planner + hypothesis writer), S05, S06, S08 (all three), BT-01 (all three) when `scenarios/BT-01.yaml` exists, else "BT-01 pending" and the desc `record fixtures S04-S08 handoffs`. Runner v3 at each scenario's seed, signed orders, SITREP writer in `auto` through `sitrep_client`.
- Mechanism: runner v3 builds every role client over the `fixture_store` it is given, so a `FixtureStore` subclass sees every request. `CapturingStore` (plan, verify) records (scenario, role, key, prompt chars, hit). `RecordingStore` (record) serves hits from disk and sends each miss once through `ModelClient(mode=record, transport=...)` with the role's own system prompt (`SYSTEM_PROMPTS`, pinned by a spy test). No runner or wiring change was needed.
- `plan`: table per role (calls, distinct, recorded, to record, avg input tokens = ceil(chars/4) of system prompt + canonical inputs, stated output estimate 150 / 400 / 800), totals, per-scenario counts, the exact gate line and approval phrase, USD when `SOUNDINGS_USD_PER_MTOK_IN/_OUT` are set; `artifacts/fixtures/plan.json` (canonical, no clock). Real plan at 10:50 PDT: 74 distinct calls (probe_planner 41, hypothesis_writer 13, sitrep_writer 20), avg 1324 in / 370 out, BT-01 pending.
- `record` refusals (exit 3, before any call): `SOUNDINGS_CI`; `MODEL_MODE` not `record`; `SOUNDINGS_PAID_RUN_APPROVED` not exactly the gate phrase; no `MODEL_NAME`; no price env; worst case (`max_calls_for(n)` = n + 10 % calls at `max_tokens` 2048 out) above `PAID_RUN_CAP_USD` (15.0) minus `docs/log/PAID-RUNS.md` (a harness row replaces the gate's estimate row; this run's pending gate row is not counted). Stops at the first transport error (the billed call is logged at estimate in / max_tokens out) or before the call past plan + 10 %; after a stop no further call is possible. Appends one PAID-RUNS row (gate timestamp format, `fixture_run`, calls, actual cost from provider usage, model, `<desc>; N fixtures; tokens in/out`, `owner (SOUNDINGS_PAID_RUN_APPROVED); recorded by soundings.llm.fixtures`), copies guard rejections to `docs/log/VIOLATIONS.md`, writes `<fixture root>/fixture_run.json` (keys recorded, keys rejected), names any fixture holding a denylisted word. A second `record` makes zero calls and writes no row.
- `soundings/llm/client.py` WO-093 block: `anthropic_record_transport` (= `anthropic_transport` with `RECORD_TIMEOUT_S = 60`): a SITREP reply can outlast the 7 s stage timeout, and in record mode a timeout stops the run.
- `verify` (replay): zero replay misses, zero guard rejections not listed in the manifest, and no catalog `expected` key failing with the model roles that passes in the deterministic run (S04's known C_BUDGET failure stays a baseline failure, not a regression). Tests show it passing on stub-recorded fixtures (rejected and accepted replies) and failing on a recorded planner that spends eyes-on probes (S05 over `max_human_interruptions`).
- `soundings/eval/ablation.py`: `python -m soundings.eval.ablation --planners llm,deterministic,random --scenarios S04-S08,BT-01 --mode replay [--out JSON]`; arms deterministic (`llm_roles=False`), llm (recorded fixtures, replay), random (seeded `RandomPlannerStore`: a uniformly chosen probe the guard would accept, through the same guarded `LlmPlanner`); columns probes, median s seen->handled, missed threats (WO-050 rule), interruptions, llm picks; the "Where the LLM layer added nothing" sentence. With no fixtures: "every LLM planner call fell back ... equals the deterministic planner ... added nothing here".
- Exit test notes: the empty-fixture-root full suite has 5 failures that also fail on origin/main 31c0223 with the same root (tests/test_sitrep.py x3, tests/test_audit.py::test_sitrep_path_rows_and_chain, tests/test_audit_semantics.py::test_s05_stream_writes_one_row_per_mutation: they assert on the committed WO-042 SITREP fixtures); every WO-093 test passes with an empty root.
- Cut: none of the three cut-line items was dropped; BT-01 is pending only because WO-080 part A (#33) is not on main.
