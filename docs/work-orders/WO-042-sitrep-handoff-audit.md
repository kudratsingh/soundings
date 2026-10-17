# WO-042 — sitrep-handoff-audit

Phase: 4 · Branch: `wo/042-sitrep-handoff-audit` · Worktree: `../soundings-worktrees/wo-042-sitrep-handoff-audit` · Est: 4.5 h · Owner: Kudrat (planner / ladder / investigation)
Depends on: WO-010 (`EvidenceRow.row_id`, `EvidenceKind`, `canonical_json`, denylist), WO-012 (`EvidenceTable`), WO-022 (runner v1, `LostVehicleNotice`, `soundings.matcher.SearchBox`), WO-030 (`PosteriorReport`, `score_contact`), WO-032 (`GuardResult`, `ViolationRow`, `append_violation`), WO-040 (`LadderMachine`, `LadderListener`, `LadderCheckpoint`, `LadderAction`, `GuardDecision`), WO-041 (`BoundaryGuards`, `GuardInputs`, `SimulatedApprover`, `ApprovalToken`, `token_hash`); WO-000 is merged (`ModelClient`, `FixtureStore`, `fixture_key`). Not WO-033 and not the driver: callers pass `question_for_human` and `uncertain` as plain fields, and WO-043 wires real runs. Merges in wave 7, in parallel with WO-033.

## Goal
Every handoff produces a SITREP with the fixed SPEC-LADDER §6 sections. Facts and assumptions sit in separate sections, and each line cites evidence row ids that exist. "What we don't know" is generated from the assumption rows and the missing features. Probabilities are labelled "stress-test estimate". The recommended rung is copied from the machine and no writer can edit it. A template writer always produces a valid SITREP with no model. The LLM writer is replayed from fixtures and runs behind a guard. A rejected output falls back to the template and is logged to the violations file. The LOST_VEHICLE SITREP carries the Phase 2 search box. Alongside, an append-only, hash-chained audit table records each of these: transition, probe, guard decision, approval, action, SITREP and model call. `python -m soundings.audit verify` recomputes the chain.

## Scope
In:
- `soundings/ladder/sitrep.py`:
  - `SitrepLine(text, evidence_ids: tuple[str, ...])`, `HypothesisLine(hypothesis, p, label="stress-test estimate", evidence_ids)`, `ActionLine(rung, t_s, clause, evidence_ids)`, `SearchBoxSection(last_known, box: soundings.matcher.SearchBox, drift_basis, evidence_ids)` (wraps WO-022's box; no second box type).
  - `Sitrep(contact_id, status ∈ {draft, final, update}, contact, facts, assumptions, unknowns, hypotheses, actions_taken, recommended_rung: Rung, person_must_decide, search_box: SearchBoxSection | None, writer ∈ {template, llm})`. `recommended_rung` is set by `build_sitrep` from `LadderMachine.rung` and is never read from writer output.
  - `SitrepContext` is built by `context_from(machine, evidence, posterior_report, checkpoints, lost_vehicle_notice=None, *, question_for_human=None, uncertain=False)`. `sitrep_inputs(ctx)` returns the canonical dict that is hashed into the fixture key.
  - `render_markdown(sitrep)` prints the nine §6 headings in fixed order. `Search box` prints only for LOST_VEHICLE, where it is required.
  - Line-to-row rules:
    - Facts cite `fact` or `derived` rows. Assumptions cite `assumption` rows, and each one quotes the row's `assumes`.
    - Hypotheses cite the rows the posterior used: the three most recent per feature, chosen deterministically.
    - Each action line cites the evidence rows behind its guard inputs (the `ladder_monitor` rows) and the standing-order clause.
    - "What a person must decide" cites the rows that put the contact over the handoff threshold.
    - In "What we don't know", a missing-feature line names the feature with the prefix `missing:` instead of a row id, because an absent feature has no row. This is the only exception, and the tests assert it exactly.
- `soundings/llm/sitrep_writer.py` (new; this WO owns it and adds nothing to WO-032's `guard.py`, `roles.py` or `fallback.py`):
  - `ROLE_SITREP_WRITER`, `SITREP_WRITER_PROMPT`, and `sitrep_writer_prompt(inputs)`. The model writes prose only.
  - `SitrepWriterOutput`, the strict output schema `{contact_summary, facts[{text, evidence_ids}], assumptions[{text, evidence_ids}], unknowns[{text, evidence_ids}], person_must_decide[str], recommended_rung?}`, with extra keys forbidden.
  - `template_sitrep(ctx) -> Sitrep`, which is deterministic, uses no model, and always passes `validate_sitrep`.
  - `guard_sitrep(raw, ctx) -> GuardResult` (WO-032's result type; `value` is the accepted `Sitrep`). `SitrepViolation` (StrEnum) holds the reason codes. It rejects:
  - a schema failure or extra key (for example `approve`, `next_state`, `tool`, `rung_override`): `schema`;
  - `recommended_rung` present and not equal to the computed rung: `recommended_rung_mismatch`;
  - a fact or assumption line with no ids, an id not in the table, or an id of the wrong kind: `citation`;
  - a probability or percentage in prose: `probability_in_prose`;
  - any denylist hit: `denylist`.

  On rejection it appends a WO-032 `ViolationRow` through `append_violation` (the path is a parameter, default `docs/log/VIOLATIONS.md`).
- `soundings/ladder/handoff.py`:
  - `HandoffService(client, audit, violations_path, writer_mode ∈ {auto, template})` is a WO-040 `LadderListener`.
    - On `deliver_sitrep` it builds the final SITREP. It makes one `ModelClient.call("sitrep_writer", inputs)` per handoff, never retried and never looped. A `None` (replay miss) gives the template.
    - On `request_approval` it builds a template draft, so the approver sees what they are approving.
    - On `update_sitrep` it builds a template update. WO-043 emits these.
  - Each delivery is a `HandoffPacket(contact_id, sitrep, markdown, audit_seq)`.
- `soundings/ladder/audit.py`:
  - SQLModel `AuditRow` (table `audit_row`): `seq` (pk, contiguous from 1), `run_id`, `contact_id`, `kind ∈ {transition, probe, guard_decision, approval, action, sitrep, llm_call}`, `t_s` (run clock, never wall clock), `payload_json`, `prev_hash`, `row_hash`, and `retention_label="30 days (demo)"`.
  - `row_hash = sha256(prev_hash + canonical_json({seq, run_id, contact_id, kind, t_s, payload}))`. The genesis `prev_hash` is 64 zeros.
  - `AuditLog(db_url)` exposes `append` and `verify -> VerifyResult(ok, rows, first_bad_seq)` and no update or delete method. SQLite `BEFORE UPDATE` and `BEFORE DELETE` triggers abort with "append-only".
  - The DB path comes from env `SOUNDINGS_DB`, the same file as the registry.
  - `AuditSink(audit, run_id)` is a WO-040 `LadderListener`. It writes:
    - probe rows from event payloads (probe_id, idempotency_key, cost_usd), before the transition row;
    - one guard row per `GuardDecision`, with its full `GuardInputs` dump;
    - transition rows;
    - action rows with idempotency keys;
    - approval rows carrying `token_hash`, never the token;
    - SITREP rows (sha256 of the markdown, writer, status);
    - LLM-call rows (role, fixture key, `accepted|rejected|fallback_no_fixture`, violation reason).
- `soundings/audit/__init__.py` and `soundings/audit/__main__.py` provide `python -m soundings.audit verify [--db PATH]`, which prints `ok <n> rows` and exits 0, or prints `broken at seq <k>` and exits 1.
- WO-043's `python -m soundings.ladder` CLI exposes `--sitrep-out` and `--audit-db` through `HandoffService` and `AuditSink`; this WO adds no CLI besides `soundings.audit`.
- Hand-written response bodies live in `tests/fixtures/sitrep_bodies/` (outside `model_calls/`, so WO-032's key-consistency test never reads them): `s05_good.json`, `s05_rung_change.json` (claims `hold` while the machine says `hand_to_person`), `s05_bad_citation.json`, `s05_extra_control_key.json`, `s05_probability_in_prose.json`. `tests/support/fixtures.py::install(store, role, inputs, body)` writes each body into a tmp `FixtureStore` under the computed `fixture_key`. The denylist case builds its word from the pattern at test time, so no denylisted literal is ever stored in the repo.

Out: watch-after-handoff updates and runner v3 (WO-043); the SITREP drawer UI (WO-060); recording real fixtures (a paid run after Phase 4 only if the owner approves); any SITREP wording about what a person does after the handoff beyond "What a person must decide".

Parallel start: `sitrep.py`, the template and `audit.py` need only WO-010, WO-012 and WO-030 types and can start against stub contexts. The guard waits for WO-032's `GuardResult`; `handoff.py`, `AuditSink` and the integration test for WO-040 and WO-041.

## Spec references
`docs/SPEC-LADDER.md §6` (SITREP), `§7` (token hash), `§8` (audit) · `docs/SPEC-INVESTIGATION.md §7` (computed rung), `§8` (honesty: stress-test label, what we don't know) · `CLAUDE.md §2.2, §2.3, §2.6, §5` (role `sitrep_writer`, 1 per handoff) · `docs/WORKFLOW.md §7, §9`.

## Files expected
- `soundings/ladder/sitrep.py`, `handoff.py`, `audit.py`
- `soundings/audit/__init__.py`, `__main__.py`
- `soundings/llm/sitrep_writer.py`
- `tests/test_sitrep.py`, `tests/test_sitrep_guard.py`, `tests/test_audit.py`, `tests/test_handoff_integration.py`
- `tests/support/fixtures.py`
- `tests/fixtures/sitrep_bodies/*.json`

## Exit tests (must all pass with `MODEL_MODE=replay`, no network)
- [ ] `pytest tests/test_sitrep.py` — every SITREP line cites ≥ 1 evidence row id; facts and assumptions are in separate sections; the recommended rung equals the state machine's rung (the LLM cannot change it); template fallback produces a valid SITREP with no model.
- [ ] `pytest tests/test_sitrep.py::test_sections_and_honesty` — proves:
  - the nine headings appear in §6 order;
  - every `HypothesisLine` and the rendered Hypotheses section carry "stress-test estimate";
  - every assumption row in the table appears in "What we assume" or "What we don't know";
  - each feature missing from the table shows up as a `missing:` line;
  - no fact line cites an assumption row.
- [ ] `pytest tests/test_sitrep_guard.py` — each hand-written body is installed under its computed key. Proves:
  - `s05_good` is accepted and the result has `writer=llm`;
  - `s05_rung_change`, `s05_bad_citation`, `s05_extra_control_key`, `s05_probability_in_prose` and the generated-denylist body are each rejected with their reason code;
  - each rejection appends exactly one row to a tmp violations file, and the delivered SITREP equals `template_sitrep(ctx)`;
  - `recommended_rung` is identical in all six outcomes.
- [ ] `pytest tests/test_audit.py` — every transition, probe, guard decision, and approval writes an append-only audit row with a hash chain; tampering with one row breaks verification.
- [ ] `pytest tests/test_audit.py::test_append_only_and_tamper` — proves:
  - `UPDATE` and `DELETE` through SQLModel raise;
  - after a raw `sqlite3` connection drops the triggers and edits one payload, `verify()` reports that seq;
  - deleting a row or swapping two rows is also detected;
  - `uv run python -m soundings.audit verify --db <tmp>` exits 1 on the tampered DB and 0 on a clean one;
  - every row carries `retention_label == "30 days (demo)"`.
- [ ] integration: `pytest tests/test_handoff_integration.py` — real catalog evidence, zero fixtures, a tmp DB. Proves:
  - S02 at seed 3 through runner v1 (WO-022): `build_sitrep` over its `LostVehicleNotice` and evidence table gives a LOST_VEHICLE SITREP whose search box contains the true position at the notice tick (`SimRun.truth_paths`);
  - S05 at seed 11: runner v1 stops the case at INVESTIGATING with a real evidence table, which `score_contact` scores. A `LadderMachine` with `BoundaryGuards`, signed temp orders and `SimulatedApprover`, with `HandoffService` and `AuditSink` attached, is fed the S05-shaped handoff stream citing those real rows. It delivers one draft (at AWAITING_APPROVAL) and one final SITREP with `recommended_rung == hand_to_person`;
  - the audit log verifies and has at least one row of each kind exercised, an approval row holding a 64-hex `token_hash` with no token fields, and exactly one `llm_call` row per final SITREP, marked `fallback_no_fixture`.
  The same checks on full runner v3 runs are WO-043's.
- [ ] determinism: `uv run pytest tests/test_handoff_integration.py -k deterministic` — the S05 case run twice into two tmp DBs gives byte-identical SITREP markdown and an equal last `row_hash`, and a `PYTHONHASHSEED=0`/`=1` subprocess pair prints the same chain head (CLAUDE.md §2.4).
- [ ] rule proof §2.2: `test_sitrep_guard.py::test_rung_change_rejected` (above), plus `test_sitrep.py::test_rung_not_from_writer`, which shows `build_sitrep` ignores any `recommended_rung` field even when it matches.
- [ ] rule proof §2.3: `test_handoff_integration.py::test_no_live_client` — with `anthropic.Anthropic` monkeypatched to raise on construction, the full S05 and S02 runs pass.
- [ ] rule proof §2.6: `test_sitrep.py` citations plus the stress-test label. Also `grep -riEf config/denylist.txt soundings/ladder soundings/audit soundings/llm tests/test_sitrep*.py tests/test_audit.py tests/fixtures/sitrep_bodies` → empty, and `make check` green.

## Interfaces
Exposed:
- WO-042 `soundings.ladder.sitrep.Sitrep`, `.SitrepLine`, `.HypothesisLine`, `.ActionLine`, `.SearchBoxSection`, `.SitrepContext`, `.context_from`, `.sitrep_inputs`, `.build_sitrep`, `.validate_sitrep`, `.render_markdown`
- WO-042 `soundings.llm.sitrep_writer.ROLE_SITREP_WRITER`, `.SITREP_WRITER_PROMPT`, `.sitrep_writer_prompt`, `.SitrepWriterOutput`, `.SitrepViolation`, `.guard_sitrep`, `.template_sitrep`
- WO-042 `soundings.ladder.handoff.HandoffService`, `.HandoffPacket`
- WO-042 `soundings.ladder.audit.AuditRow`, `.AuditKind`, `.AuditLog`, `.AuditSink`, `.VerifyResult`, `.GENESIS_HASH`
- WO-042 `soundings.audit` (CLI `python -m soundings.audit verify [--db PATH]`)

Consumed:
- WO-040: `soundings.ladder.ports.LadderListener`, `LadderMachine` (`.rung`), `LadderCheckpoint`, `LadderAction`, `ActionKind.{deliver_sitrep, request_approval, update_sitrep}`, `LadderEvent.payload["probes"]`.
- WO-041: `GuardInputs` (the dump for guard rows), `ApprovalToken`, `token_hash`, `BoundaryGuards`, `SimulatedApprover` (integration test).
- WO-000: `soundings.llm.client.ModelClient`, `FixtureStore`, `fixture_key`.
- WO-010: `EvidenceRow.row_id`, `EvidenceKind`, `soundings.canonical.canonical_json`, `soundings.schemas.denylist.find_denylisted`.
- WO-012: `EvidenceTable`.
- WO-022: `soundings.runner.ScenarioRunner` (v1, integration test), `soundings.runner.LostVehicleNotice`, `soundings.matcher.SearchBox`.
- WO-030: `soundings.investigation.posterior.PosteriorReport` (`used_row_ids`), `score_contact`.
- WO-032: `soundings.llm.guard.GuardResult`, `ViolationRow`, `append_violation`.

## Cut line
If this WO runs more than an hour late, drop in this order: (1) the draft SITREP at `request_approval` (the final remains); (2) the `s05_probability_in_prose` check and fixture; (3) the hypothesis-line citations, which shrink to the single most recent row per feature. Never an exit test.

## Model calls
Role `sitrep_writer`, default replay, at most 1 call per handoff, never retried. Drafts and updates are template only. Tests use hand-written bodies in a tmp `FixtureStore` and the template fallback. Paid run needed? No. The optional SITREP recording run (about 10 calls, per WORKFLOW §7) is not part of this WO.
Dependency adds needing owner approval (CLAUDE.md section 3): none.

## Rungs touched
hand to a person (the SITREP and the handoff packet); slow and hold (cited as actions already taken); none climbed.

## Hard rules exercised
- §2.2: `test_rung_change_rejected` and `test_rung_not_from_writer`.
- §2.3: `test_no_live_client`; the fixtures are hand-written.
- §2.4: the SITREP `diff` and the equal chain head.
- §2.5: the generated-denylist rejection and the grep.
- §2.6: the citation and section tests.

## Done
PR: · Merged commit: · Test summary: · STATE.md updated: yes/no

## Coordinator addenda (2026-10-16 23:05 PT, binding; see docs/log/DECISIONS.md)
- A 'What we don't know' line for a missing feature carries a `missing:` prefix and is exempt from the evidence-id citation rule; every other line cites ≥ 1 existing row id.
- A draft SITREP is issued on entering AWAITING_APPROVAL; the final SITREP at HANDED_OFF.

## Part A done (coordinator split)
Recorded 2026-10-17 05:58 PDT. Branch `wo/042a-audit-sitrep-template`. `make check` exit 0; full suite green after rebase on main with WO-031, WO-032 part A, WO-062 part A and WO-043 part A (45 new: tests/test_audit.py 21, tests/test_sitrep.py 24). Determinism: S05 draft + S02 LOST_VEHICLE markdown and the audit chain head printed under `PYTHONHASHSEED=0` and `=1` diff empty (sha256 `e9ff134c…a93f`). The denylist grep over `soundings/ladder soundings/audit tests/test_sitrep*.py tests/test_audit.py` is empty.

Built:
- `soundings/ladder/audit.py`: `AuditRow` (table `audit_row`: seq, run_id, contact_id, kind, t_s, payload_json, prev_hash, row_hash, retention_label "30 days (demo)"), `AuditKind` (transition, probe, guard_decision, approval, action, sitrep, llm_call), typed payloads `TransitionPayload`, `ProbePayload` (idempotency_key required), `GuardDecisionPayload` (full `inputs` dump), `ApprovalPayload` (`token_hash` only; extra keys refused), `ActionPayload`, `SitrepPayload` (markdown_sha256, writer, status, recommended_rung), `LlmCallPayload` (role, fixture_key, accepted|rejected|fallback_no_fixture, violation_reason iff rejected); `PAYLOAD_MODELS`, `guard_decision_payload(decision, inputs)`, `approval_payload(token, outcome=, action=)`, `chain_hash`, `GENESIS_HASH`, `RETENTION_LABEL`, `AuditError`. `AuditLog(db=None)` (path, `sqlite:` URL or `:memory:`; default `$SOUNDINGS_DB`) with `append(kind, payload, *, run_id, t_s, contact_id="")`, `rows(run_id=, kind=)`, `head()`, `verify() -> VerifyResult(ok, rows, first_bad_seq, head)`, `close()`; no update/delete; SQLite BEFORE UPDATE/DELETE triggers abort "append-only".
- `soundings/audit/__init__.py`, `__main__.py`: `python -m soundings.audit verify [--db PATH]` prints `ok <n> rows` (exit 0) or `broken at seq <k>` (exit 1); exit 2 when the file is absent.
- `soundings/ladder/sitrep.py`: `SITREP_HEADINGS` (nine, section 6 order), `MISSING_PREFIX`, `SitrepLine`, `HypothesisLine`, `ActionLine`, `SearchBoxSection` (wraps `soundings.matcher.SearchBox`), `Sitrep` (`to_dict()`), `SitrepStatus`, `SitrepWriter`, `SitrepInputs` (+ `from_report(table, report, *, recommended_rung, ...)`), `sitrep_inputs(inputs)` (canonical dict for the fixture key), `build_sitrep(inputs)` (the template writer), `validate_sitrep(sitrep, inputs) -> list[SitrepIssue]` (codes citation, sections, label, recommended_rung_mismatch, search_box, denylist), `render_markdown`, `headings_for`, `sitrep_json`.
- Exit tests covered here: `tests/test_sitrep.py` (incl. `test_sections_and_honesty`, the S02 seed 3 LOST_VEHICLE SITREP whose box contains the truth at the notice tick, the S05 seed 11 draft at the 180-tick prefix) and `tests/test_audit.py` (incl. `test_append_only_and_tamper`, delete and swap detection, CLI exit codes).

Remaining for part B: `soundings/llm/sitrep_writer.py` (ROLE_SITREP_WRITER, prompt, `SitrepWriterOutput`, `SitrepViolation`, `guard_sitrep`, `template_sitrep(ctx)` = `build_sitrep`), `tests/fixtures/sitrep_bodies/*.json` + `tests/support/fixtures.py`, `tests/test_sitrep_guard.py` (incl. `test_rung_change_rejected`), `test_sitrep.py::test_rung_not_from_writer`, `SitrepContext`/`context_from(machine, ...)` building `SitrepInputs`, `soundings/ladder/handoff.py` (`HandoffService`, `HandoffPacket`), `AuditSink` (LadderListener over `AuditLog`), an adapter from WO-043's `soundings.ladder.sitrep_port.SitrepInputs` (driver port, same class name) to `soundings.ladder.sitrep.SitrepInputs` behind `SitrepWriterLike`, `tests/test_handoff_integration.py` (incl. `test_no_live_client` and `-k deterministic`).
