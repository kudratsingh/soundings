# WO-032 — llm-planner-guard

Phase: 3 · Branch: `wo/032-llm-planner-guard` · Worktree: `../soundings-worktrees/wo-032-llm-planner-guard` · Est: 4.5 h · Owner: Kudrat
Depends on: WO-000 (merged; `ModelClient`, `FixtureStore`, `fixture_key`, `ReplayMiss`, `in_ci`), WO-010 (`Hypothesis`, `Rung`, `LadderState`, `ProbeId`, `FreeText`, `find_denylisted`), WO-030 (`PosteriorReport`), WO-031 (`ALLOWLIST`, `PlannerState`, `PlannerChoice`, `DeterministicPlanner`, `BudgetLedger`)

## Goal
Add the optional LLM layer for two roles, `probe_planner` and `hypothesis_writer` (SPEC-INVESTIGATION §6; CLAUDE.md §2.2 and §5). The layer sits behind a guard that makes "no model output becomes a control" a tested property.

What exists when this WO lands:
- Prompt builders produce canonical, hashable inputs.
- Strict output schemas: probe plan `{next_probe, why, hypotheses_to_discriminate, question_for_human}`; hypothesis set `{hypotheses: [{id, why}], question_for_human}`.
- A guard rejects any of the following and appends a violation row to a log whose path is a parameter:
  - a probe outside the allowlist, above the current rung, or over budget;
  - a rung, approval, action, or tool call;
  - free text where an id belongs;
  - a hypothesis outside the fixed eight;
  - denylisted text.
- Deterministic fallbacks: the planner falls back to `DeterministicPlanner`; the hypothesis writer falls back to template hypotheses and a template question.
- An `LlmPlanner` (new module `soundings/llm/planner.py`, so WO-031's `planner.py` stays single-owner) that always falls back on a miss or a rejection.

Tests use only hand-written fixtures. With no fixtures at all, every role falls back and the suite passes.

Recording fixtures for S04 to S08 is a later paid run that the owner must approve (about 40 calls, WORKFLOW §7). It is **not** part of this WO, and nothing here calls a hosted model.

Concurrency: steps 1 to 3 (schemas, guard, violation log) need only WO-010. They can start in parallel with WO-030 and WO-031, against the WO-031 Interfaces names. Steps 4 and 5 need WO-031 merged.

## Scope
In: `soundings/llm/guard.py`, `soundings/llm/roles.py`, `soundings/llm/fallback.py` (planner and hypothesis fallbacks; its module docstring names `soundings.llm.sitrep_writer.template_sitrep` (WO-042) as the SITREP fallback, so CLAUDE.md §5's list stays findable), `soundings/llm/planner.py` (`LlmPlanner`), a `SOUNDINGS_FIXTURE_ROOT` override in `FixtureStore`'s default root, hand-written fixtures, and tests.
Out:
- the record and live paths in `client.py` (they stay `NotImplementedError`; see open questions)
- the template SITREP and `sitrep_writer` (WO-042)
- `twist_parser`
- the investigation machine and runner wiring (WO-033)
- recording any fixture
- LLM-as-judge

## Spec references
`docs/SPEC-INVESTIGATION.md §1` (fixed eight; the LLM may not add hypotheses), `§5`, `§6` (LLM planner I/O and guard), `§7` (the recommended rung is not the writer's); CLAUDE.md §2.2, §2.3, §5; `docs/WORKFLOW.md §7`, `§8` (keys, replay-miss behaviour), `§9`; `docs/log/VIOLATIONS.md` (header and column order).

## Definitions (binding)
- **Schemas.** Pydantic v2 models with `extra="forbid"` and `strict=True`. No new dependency: no jsonschema.
  - `ProbePlanOutput`: `next_probe: str`, `why: FreeText` (≤ 200 chars, one line), `hypotheses_to_discriminate: list[Hypothesis]` (1 to 3, unique), `question_for_human: FreeText | None`.
  - `next_probe` is typed `str` on purpose. That lets the guard classify a bad value instead of failing at the schema stage with no reason given.
  - `HypothesisSetOutput`: `hypotheses: list[HypothesisEntry(id: str, why: FreeText)]` (1 to 8, unique ids), `question_for_human: FreeText | None`. There is no probability field: the posterior stays the scorer's.
- **Violation codes** (`ViolationCode`, checked in this order):
  - `SCHEMA_INVALID` (bad JSON shape)
  - `NAMES_TOOL_CALL` (keys such as `tool_use`, `tool_calls`, `function_call`, `action`, or a `next_probe` that looks like a call, e.g. containing `(`)
  - `NAMES_APPROVAL` (keys such as `approve`, `approval`, `approved`, `token`, or the value `approve`)
  - `NAMES_RUNG` (a key `rung` or `recommended_rung`, or `next_probe` equal to a `Rung` or `LadderState` value, or to a `config/ladder.yaml` boundary action)
  - `NOT_IN_ALLOWLIST` (free text or an unknown id)
  - `ABOVE_CURRENT_RUNG`
  - `OVER_BUDGET` (the probe's spec would break a `BudgetLedger` limit)
  - `ALREADY_RUN` (same idempotency key)
  - `HYPOTHESIS_OUTSIDE_SET`
  - `DENYLISTED_TEXT`
  - Forbidden keys are detected on the raw dict before schema validation, so the specific code wins over `SCHEMA_INVALID`.
- **Guard API.**
  - `check_probe_plan(raw: dict, ctx: GuardContext) -> GuardResult` and `check_hypothesis_set(raw) -> GuardResult`.
  - `GuardContext(allowlist, current_rung, ledger, run_keys, contact_id, tick)`.
  - `GuardResult(accepted: bool, value | None, violation: ViolationCode | None, detail: str)`.
  - Both are pure, with no I/O.
- **Violation log.**
  - `append_violation(row: ViolationRow, path: Path = DEFAULT_VIOLATIONS_PATH)`, with `DEFAULT_VIOLATIONS_PATH = docs/log/VIOLATIONS.md`.
  - `ViolationRow(when, role, fixture_key, violation, fallback_used)`.
  - The function appends one markdown row in the header's column order, escapes `|`, and writes the header if the file is missing.
  - `when` is supplied by the caller (for example `S05/seed11/t=212`). There is no wall-clock read in guard code. Tests always pass a `tmp_path` file and never touch the repo file.
- **Roles.**
  - `build_probe_planner_inputs(state, table, report, asset_context)` returns a plain dict: `prompt_version`, `role`, the latest row per feature (feature, value, kind, confidence), posterior (6 dp), the allowlist with costs, the remaining budget, the current rung, and the asset context.
  - `build_hypothesis_writer_inputs(report, table)` is similar and lists the eight ids.
  - Each input dict carries `prompt_version` (`PROMPT_VERSION = "p3.1"`), so a prompt edit changes fixture keys.
  - The system prompt texts are module constants that tell the model it may name only one allowlisted probe and must not name a rung or an approval. The guard does not trust the prompt.
- **LlmPlanner.**
  - `choose(state, tables)` calls `client.call("probe_planner", inputs)` at most once per decision.
  - It counts calls against `budgets.llm_calls_per_contact`. Once that limit is spent, it returns the deterministic choice without calling.
  - It never retries and never loops a model call.
  - On a `None` result (a replay miss outside CI), it returns the deterministic choice with `source="fallback"`, `fallback_reason="replay_miss"`, and **no** violation row, because a miss is not a violation.
  - On a guard rejection, it returns the deterministic choice with `source="fallback"`, `fallback_reason=<code>`, and appends a violation row.
  - On acceptance, it returns `source="llm"` with the model's `why` and question. In CI (`SOUNDINGS_CI`), `ReplayMiss` propagates by design (WORKFLOW §8).
- **HypothesisWriter** (in `roles.py`). One call per investigation, with the same accept, reject, and miss handling. The fallback `template_hypotheses(report)` gives:
  - the top 3 hypotheses by posterior, with fixed template `why` lines;
  - `template_question(report)`, a fixed question per top-2 pair (for example UNAUTHORIZED_UUV versus FILED_OFF_PLAN: "Is any operator running an unfiled vehicle in this basin tonight?"), or `None` when `top2_margin` ≥ 0.5.
- **Fixture override.** `FixtureStore()` with no argument reads `SOUNDINGS_FIXTURE_ROOT` when it is set; otherwise it uses `DEFAULT_FIXTURE_ROOT`. Guard tests always pass an explicit store.

## Steps
1. `guard.py`: schemas, `ViolationCode`, `GuardContext`, `GuardResult`, `check_probe_plan`, `check_hypothesis_set`, `ViolationRow`, `append_violation`.
2. `roles.py`: `ROLE_PROBE_PLANNER`, `ROLE_HYPOTHESIS_WRITER`, `PROMPT_VERSION`, `PROBE_PLANNER_PROMPT`, `HYPOTHESIS_WRITER_PROMPT`, `build_probe_planner_inputs`, `build_hypothesis_writer_inputs`.
3. `fallback.py`: `fallback_probe_plan(state, tables, planner) -> ProbePlanOutput`, which wraps the `DeterministicPlanner` choice in the output schema; `template_hypotheses`; `template_question`.
4. `LlmPlanner` in `soundings/llm/planner.py`, `HypothesisWriter` in `roles.py`, and the `FixtureStore` env override (a three-line change to `client.py`).
5. Hand-written fixtures. Under `tests/fixtures/model_calls/probe_planner/`: `valid`, `names_rung` (`"next_probe": "hold"`), `names_tool_call` (`"tool_use": {...}`), `names_approval`, `over_budget` (a second paid probe with paid limit 2 already spent), `above_rung` (`acoustic_hail` with `current_rung: watch`), and `free_text`. Under `.../hypothesis_writer/`: `valid` and `ninth_hypothesis` (`"id": "SUBMARINE_DRONE"`).
   - Each file is `{role, key, inputs, output}`. `inputs` comes from the synthetic builder in `tests/llm_cases.py`; `key = fixture_key(role, inputs)` is computed once with `uv run python -c` and pasted in. Only `output` is written by hand.
6. Tests, `make check`, a STATE.md line, and a PR.

## Files expected
- `soundings/llm/guard.py`, `soundings/llm/roles.py`, `soundings/llm/fallback.py`, `soundings/llm/planner.py`, `soundings/llm/client.py` (env override only; the one Phase 2–6 edit to this Phase 0 file)
- `tests/test_llm_guard.py`, `tests/test_llm_roles.py`, `tests/test_probe_planner_llm.py`, `tests/llm_cases.py`
- `tests/fixtures/model_calls/probe_planner/*.json` (7), `tests/fixtures/model_calls/hypothesis_writer/*.json` (2)

## Exit tests (must all pass with `MODEL_MODE=replay`, no network)
From BUILD-PLAN Phase 3 (verbatim):
- [ ] `pytest tests/test_llm_guard.py` — with `MODEL_MODE=replay` and no fixtures, every role uses its fallback and the full suite still passes; a model response containing a rung, an approval, or a tool call is rejected by the guard with a logged violation.
- [ ] `pytest tests/test_probe_planner.py tests/test_probe_planner_llm.py` — deterministic planner picks the max expected-information-gain-per-cost probe on a fixed evidence table (≥ 6 cases); LLM planner output outside the allowlist is rejected and the fallback is used; the planner never exceeds the probe budget. (Stronger form of the BUILD-PLAN line: WO-031's file carries the deterministic and budget clauses; `test_probe_planner_llm.py` adds `-k outside_allowlist_falls_back` (the `free_text` fixture is rejected with `NOT_IN_ALLOWLIST` and the choice equals `DeterministicPlanner`'s) and `-k llm_budget_property` (Hypothesis, `derandomize=True`: `LlmPlanner` fed any guard-accepted output never exceeds a budget).)

Unit:
- [ ] `uv run pytest tests/test_llm_guard.py -k violating_fixtures` — each of the six violating planner fixtures, replayed through `LlmPlanner` with an explicit `FixtureStore(tests/fixtures/model_calls)` and a `tmp_path` log, is rejected with its exact code: `NAMES_RUNG`, `NAMES_TOOL_CALL`, `NAMES_APPROVAL`, `OVER_BUDGET`, `ABOVE_CURRENT_RUNG`, `NOT_IN_ALLOWLIST`.
  - The returned choice equals `DeterministicPlanner`'s.
  - The log gains exactly one row with role, fixture key, code, and `fallback_used=deterministic_planner`.
  - The rejection is proven for each class.
- [ ] `uv run pytest tests/test_llm_guard.py -k valid_fixture_accepted` — `valid` is accepted with `source="llm"`, the log is unchanged, and the chosen probe is in the allowlist and within budget.
- [ ] `uv run pytest tests/test_llm_guard.py -k hypothesis_set` — `valid` is accepted; `ninth_hypothesis` is rejected with `HYPOTHESIS_OUTSIDE_SET`, and the template fallback lists only ids from the eight. A writer output carrying a `recommended_rung` key is rejected with `NAMES_RUNG`. Proves §1's "LLM may not add hypotheses".
- [ ] `uv run pytest tests/test_llm_guard.py -k fixture_keys_consistent` — every fixture file under `tests/fixtures/model_calls/probe_planner/` and `.../hypothesis_writer/` has `key == fixture_key(role, inputs)`, so hand-written fixtures cannot drift from their inputs.
- [ ] `uv run pytest tests/test_llm_guard.py -k no_fixtures_falls_back` — an empty `tmp_path` store with `SOUNDINGS_CI` unset: both roles return `source="fallback"`, `fallback_reason="replay_miss"`, and no violation row is written. With `SOUNDINGS_CI=1` the same call raises `ReplayMiss` (WORKFLOW §8).
- [ ] `uv run pytest tests/test_llm_guard.py -k never_loops` — a counting stub client, then 10 `choose` calls on one contact: the stub sees at most `llm_calls_per_contact` (4) calls, and the other decisions come from the deterministic planner. A rejected output is never retried.
- [ ] `uv run pytest tests/test_llm_guard.py -k violation_log_format` — appending to a missing file writes the exact `docs/log/VIOLATIONS.md` header and then a row; a `|` in the detail is escaped; two appends give two rows.
- [ ] `uv run pytest tests/test_llm_roles.py` — the input builders are deterministic (equal `fixture_key` for two builds), every input carries `prompt_version`, and the allowlist in the inputs equals `ALLOWLIST` with costs. The prompt constants pass `find_denylisted`.

Integration:
- [ ] `uv run pytest tests/test_llm_guard.py -k catalog_fallback` — the following runs against the real catalog with an empty fixture store. S05 at seed 11 and S07 at seed 17 each go through `run_scenario`, `build_evidence`, the WO-031 probe sweep, and `score_contact`. Then `LlmPlanner.choose` and `ledger.record` loop until the result is `None` or the budget is spent. The probe sequence is identical to a `DeterministicPlanner` loop. `HypothesisWriter` on the final report returns template hypotheses that are a subset of the eight. Proves the LLM layer adds nothing harmful when absent. (The S04 to S08 end-to-end check lands in WO-033.)
- [ ] `SOUNDINGS_FIXTURE_ROOT="$(mktemp -d)" SOUNDINGS_CI= MODEL_MODE=replay uv run pytest -q` → exit 0. Proves the whole suite passes with zero fixtures present (CLAUDE.md §5).

Determinism:
- [ ] `uv run pytest tests/test_llm_guard.py -k guard_deterministic` — each fixture checked twice gives an equal `GuardResult`, and the two log files are byte-identical when the same `when` strings are used. Proves CLAUDE.md §2.4 for guards.

Guard / rule proof:
- [ ] CLAUDE.md §2.2 (no model output becomes a control; the model may not set a rung, approve, or call a tool): `violating_fixtures` and `hypothesis_set`.
- [ ] CLAUDE.md §2.3 (no paid runs): `grep -rn "MODEL_MODE=live\|MODEL_MODE=record" tests .github` → empty, and `no_fixtures_falls_back` shows misses never reach a network path.
- [ ] CLAUDE.md §2.5: `grep -iEf config/denylist.txt soundings/llm tests/test_llm_*.py tests/fixtures/model_calls` → empty. The violating fixtures use non-denylisted words such as "hold" and "approve". `make check` green.

## Interfaces
Exposed by WO-032:
- `soundings.llm.guard`: `ProbePlanOutput`, `HypothesisEntry`, `HypothesisSetOutput`, `ViolationCode`, `GuardContext`, `GuardResult`, `check_probe_plan`, `check_hypothesis_set`, `ViolationRow`, `append_violation`, `DEFAULT_VIOLATIONS_PATH`
- `soundings.llm.roles`: `ROLE_PROBE_PLANNER`, `ROLE_HYPOTHESIS_WRITER`, `PROMPT_VERSION`, `PROBE_PLANNER_PROMPT`, `HYPOTHESIS_WRITER_PROMPT`, `build_probe_planner_inputs`, `build_hypothesis_writer_inputs`, `HypothesisWriter`
- `soundings.llm.fallback`: `fallback_probe_plan`, `template_hypotheses`, `template_question` (WO-042 adds the template SITREP)
- `soundings.llm.planner.LlmPlanner` (sets `PlannerChoice.source ∈ {deterministic, llm, fallback}` and `PlannerChoice.fallback_reason`, fields WO-031 defines)
- `soundings.llm.client.FixtureStore` default root honours `SOUNDINGS_FIXTURE_ROOT`

Consumed:
- WO-000: `soundings.llm.client.ModelClient`, `.FixtureStore`, `.fixture_key`, `.ReplayMiss`, `.in_ci`
- WO-010: `soundings.schemas.Hypothesis`, `.Rung`, `.LadderState`, `.ProbeId`, `soundings.schemas.denylist.FreeText`, `.find_denylisted`, `soundings.schemas.config.load_ladder_config`
- WO-030: `soundings.investigation.posterior.PosteriorReport`, `.LikelihoodTables`
- WO-031: `soundings.investigation.probes.ALLOWLIST`, `.rung_index`, `.idempotency_key`, `soundings.investigation.planner.PlannerState`, `.PlannerChoice`, `.DeterministicPlanner`, `.BudgetLedger`, `.load_budget_limits`

Used by: WO-033 (`LlmPlanner`, `HypothesisWriter`, `template_question`), WO-042 (`GuardResult`, `ViolationRow`, `append_violation`), WO-051 (the "Where the LLM layer added nothing" diff reads `PlannerChoice.source`).

## Cut line
If the WO runs more than an hour late, drop in this order (exit tests unchanged):
1. `ALREADY_RUN` and `DENYLISTED_TEXT` merge into `NOT_IN_ALLOWLIST` and `SCHEMA_INVALID` with a detail string.
2. `template_question` covers only the five most common top-2 pairs and returns a generic question otherwise.
3. `HypothesisWriter` makes no model call and always uses the template. The `hypothesis_set` guard tests still run against `check_hypothesis_set` directly.

## Model calls
Roles: `probe_planner` (≤ 4 per contact) and `hypothesis_writer` (1 per investigation), both replay by default and both tested only on hand-written fixtures. Paid run needed? No. The S04 to S08 fixture recording (about 40 calls) is a separate, later, owner-approved paid run under WORKFLOW §7 and is not in this WO.
Dependency adds needing owner approval (CLAUDE.md section 3): none. Schema checks use Pydantic, not jsonschema.

## Rungs touched
watch and ask (the planner may propose only allowlisted probes at or below the current rung). Every attempt to name slow, hold, hand to a person, or an approval is rejected.

## Hard rules exercised
- §2.2: `violating_fixtures` and `hypothesis_set`.
- §2.3: the grep and `no_fixtures_falls_back`.
- §2.4: `guard_deterministic` and `fixture_keys_consistent`.
- §2.5: the denylist grep.

## Done
PR: · Merged commit: · Test summary: · STATE.md updated:

## Coordinator addenda (2026-10-16 23:05 PT, binding; see docs/log/DECISIONS.md)
- Approved: `FixtureStore` default root reads `SOUNDINGS_FIXTURE_ROOT` (edit to Phase 0 client.py).
- WO-032 also implements the `record` and `live` paths of `soundings/llm/client.py`: the anthropic SDK is imported lazily inside the live call only; model name from `MODEL_NAME`; `record` writes the fixture then returns it; both raise if `SOUNDINGS_CI` is set; no test may exercise them against the network (unit-test with a stub transport). This prepares the later owner-approved fixture run; it is not a paid run.
- `llm_roles` defaults to False in the runner until recorded fixtures exist; eval and demo CLIs refuse `--mode record|live` (exit 3).
