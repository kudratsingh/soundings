# WO-087 — bounded-live-mode

Phase: 7 · Branch: `wo/087-bounded-live-mode` · Worktree: `../soundings-worktrees/wo-087-bounded-live-mode` · Est: 2.5 h · Owner: coordinator Claude / Kudrat
Depends on: WO-032 (part A merged; part B LlmPlanner), WO-042 (audit log), WO-061 (badge rendering)

## Goal
A stage-safe live mode: `MODEL_MODE=live` honors a hard call budget, times out per call, falls back deterministically on any error, logs every call, and the UI never overclaims which path produced an answer. Default stays replay; the owner enables it Sunday with `approve paid run stage live budget 40`. The model still only proposes probes, writes hypotheses/questions, and writes the SITREP (CLAUDE.md §2.2); guards are unchanged.

## Scope
In: `soundings/llm/client.py` live path (budget, timeout, fallback, logging), `LiveBudget` persisted to `artifacts/live_calls.json` (atomic write; restored across client instances), audit rows for every call (role, tokens in/out, latency, path ∈ {live, replay, fallback}, fixture key), `GET /api/model_mode` → `{mode, live_budget_total, live_budget_remaining, last_path}`, the judge-panel badge "model: live" / "model: fallback" (WO-061 renders from this endpoint), `scripts/paid-run-gate.py --role stage_live` wording for the Sunday enable step.
Out: any change to what the model may decide; any new role; live calls in tests or CI.

## Spec references
CLAUDE.md §2.2, §2.3, §5; docs/WORKFLOW.md §7, §8; SPEC-LADDER §8 (audit rows for LLM calls); this WO's owner directive (STATE.md → Phase 7 directives, item 4).

## Files expected
- `soundings/llm/client.py` (LiveBudget, timeout, fallback, LiveCallLog), `soundings/llm/live_budget.py`
- `soundings/api.py` (block `# --- WO-087 begin/end ---`: `GET /api/model_mode`)
- `web/src` badge (one small block in the judge panel, owned by WO-061; WO-087 ships the endpoint and the TS type)
- `tests/test_live_mode.py`, `tests/test_api_model_mode.py`
- `docs/log/DECISIONS.md` one line if anything deviates

## Exit tests (must all pass with `MODEL_MODE=replay`, no network)
- [ ] `pytest tests/test_live_mode.py -k budget` — with a stub transport and `LIVE_CALL_BUDGET=2`, the third call during an investigation returns the deterministic fallback; the investigation completes with the same terminal outcome as the replay run; `artifacts/live_calls.json` shows 2 spent (proves the hard cap and clean mid-investigation fallback).
- [ ] `pytest tests/test_live_mode.py -k timeout` — a stub transport that sleeps 7 s makes the call fall back within 6.5 s (per-call timeout 6 s) and logs `path=fallback reason=timeout`.
- [ ] `pytest tests/test_live_mode.py -k error` — a transport raising any exception falls back and logs the reason; nothing propagates to the caller.
- [ ] `pytest tests/test_live_mode.py -k persisted` — the budget counter survives a new `ModelClient` instance (restored from `artifacts/live_calls.json`); a corrupt file is treated as exhausted, not as fresh.
- [ ] `pytest tests/test_live_mode.py -k ci_refuses` — with `SOUNDINGS_CI=1`, `MODEL_MODE=live` raises before any transport call (CLAUDE.md §2.3).
- [ ] `pytest tests/test_live_mode.py -k guard_unchanged` — a live response naming a rung or a tool call is rejected by the WO-032 guard exactly as a fixture would be (CLAUDE.md §2.2).
- [ ] `pytest tests/test_sitrep.py -k path_recorded` or `tests/test_audit.py -k llm_path` — the audit log records which path (live | replay | fallback) produced each SITREP and each planner choice.
- [ ] `pytest tests/test_api_model_mode.py` — `GET /api/model_mode` returns `mode=replay`, remaining budget, and `last_path` after a fallback.
- [ ] determinism: two replay runs of S05 seed 11 with `LIVE_CALL_BUDGET=0` produce identical transition logs (budget 0 ⇒ always fallback).
- [ ] rule proof: §2.2 via `-k guard_unchanged`; §2.3 via `-k ci_refuses` and the gate wording; §2.4 via the determinism item.
- [ ] `make check` green; `bash scripts/denylist-check.sh` clean.

## Model calls
Roles touched: probe_planner, hypothesis_writer, sitrep_writer (live path only). Tests use a stub transport; zero network. Paid run needed? Not for this WO. Sunday enable: owner types `approve paid run stage live budget 40` → `MODEL_MODE=live LIVE_CALL_BUDGET=40` for the stage run; logged to `docs/log/PAID-RUNS.md` by the gate script.

## Rungs touched
None.

## Hard rules exercised
§2.2 (guard_unchanged), §2.3 (ci_refuses; budget cap; gate), §2.4 (determinism with budget 0), §2.6 (audit path rows).

## Interfaces
- Exposes: `soundings.llm.client.ModelClient(mode, store, model_name, *, live_budget: LiveBudget | None = None, timeout_s: float = 6.0, transport=None)`, `soundings.llm.live_budget.LiveBudget(path=ARTIFACTS/live_calls.json, total)` with `spend()`, `remaining`, `exhausted`; `ModelClient.last_path`; audit row kind `llm_call` with fields role, path, reason, tokens_in, tokens_out, latency_ms, fixture_key; `GET /api/model_mode`.
- Consumes: WO-032 guard (`soundings.llm.guard`), WO-042 `AuditLog`, WO-061 judge panel (badge).

## Cut line
1. The persisted JSON counter (keep an in-process counter; note it). 2. `last_path` on the endpoint (keep `mode` + remaining).

## Done
PR: see branch PR (wo/087-bounded-live-mode) · Merged commit: squash on merge · Test summary: 1866 passed, 1 skipped (new: tests/test_live_mode.py 44, tests/test_api_model_mode.py 5, tests/test_audit.py -k llm_path 3) · STATE.md updated: WO-087 row

Notes for downstream:
- `ModelClient(mode, store, model_name, *, live_budget=None, timeout_s=6.0, transport=None)`; `transport` is now keyword-only (every caller on main already passed it by name).
- Every call leaves an `LlmCallRecord` (role, path live|replay|fallback, reason, fixture_key, tokens_in/out, latency_ms) on `client.call_log`; `client.last_call` / `client.last_path`; process-wide `soundings.llm.client.last_path()` feeds `GET /api/model_mode`.
- Live fallback reasons: `budget_exhausted`, `timeout`, `error:<ExceptionType>`; replay miss stays `replay_miss`. `RoleOutcome.fallback_reason` and `PlannerChoice.fallback_reason` carry them.
- `RoleOutcome.call` and `SitrepOutcome.call` hold the record; `soundings.llm.roles.llm_call_fields(role, outcome)` + `soundings.ladder.audit.llm_call_payload(**fields)` is the planner/hypothesis `llm_call` row (WO-033 part B: append one per `LlmPlanner.choose` from `planner.last_outcome`). The handoff already writes SITREP rows with the path.
- Stage enable: `MODEL_MODE=live LIVE_CALL_BUDGET=40` and build the client with `live_client_from_env()` (refuses in CI, refuses without the budget, wires the vendor transport and the persisted cap). `make_planner` still pins replay.
- Judge panel (WO-061): `fetchModelMode()` and `modelBadge(info)` in web/src/api.ts (WO-087 block) give "model: live" / "model: fallback" / "model: replay".
