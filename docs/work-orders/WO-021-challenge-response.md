# WO-021 — challenge-response

Phase: 2 · Branch: `wo/021-challenge-response` · Worktree: `../soundings-worktrees/wo-021-challenge-response` · Est: 2.5 h · Owner: sim / data (unassigned); coordinator Kudrat
Depends on: WO-010 (`Scenario`, `ContactRegistrySpec`, `KnownOperator`, `EvidenceRow`, `HailReply`, `ProbeId`, `canonical_json`, `stable_hash`), WO-011 (`SimRun`, `ContactTruth`, `run_scenario`, `resolve_scenario`, `track_hash`). Not WO-012: rows are validated as WO-010 `EvidenceRow`s here, and WO-022 appends them to a real `EvidenceTable`. Runs in parallel with WO-012 and WO-041 (wave 3); WO-022 and WO-031 consume it.

## Goal
A simulated acoustic challenge-response: the `acoustic_hail` probe from SPEC-INVESTIGATION §5 (rung ask, cost $0.01, latency 20 s). Given a scenario, its `SimRun`, a seed, an observed contact id and the tick the hail is sent, it returns a typed `HailResult`. That result holds the reply tick (latency converted to ticks), the reply bucket (`valid`, `invalid`, `none`, `timeout`), the modem id if one was given, and whether the modem belongs to a known operator. It also returns the evidence rows that result writes. The simulation is deterministic: same scenario, seed, contact and tick give the same result, byte for byte. A known-operator table in `config/known_operators.yaml` lets the matcher in WO-022 recognise `UNFILED_KNOWN_OPERATOR`. The hail reads the world and never changes it.

## Scope
In:
- `soundings/challenge/__init__.py`, `soundings/challenge/hail.py`.
- `HailConfig`, loaded from `config/challenge.yaml` (`hail:` block):
  - `latency_s: 20` (SPEC-INVESTIGATION §5), `cost_usd: 0.01`.
  - `reply_loss_prob: 0.0`. This is an acoustic-modem loss rate; it stays 0.0 for the catalog and is seeded when it is non-zero.
  - `modem_id_pattern: "^MOD-[0-9]{4,6}$"`, the same pattern as WO-020's `config/registry.yaml`. A test asserts the two agree.
- `hail_latency_ticks(latency_s, tick_s) -> int` = `ceil(latency_s / tick_s)`. That is 10 ticks at `tick_s=2`.
- `KnownOperatorTable`, `KnownOperatorEntry(modem_id, operator, note)`, and `load_known_operators(path="config/known_operators.yaml", scenario=None)`.
  - The table is the union of the config file and `scenario.known_operators` (WO-010 field). A duplicate modem id with a different operator raises.
  - `lookup(modem_id) -> KnownOperatorEntry | None`.
  - Seed the file with S04's replying modem id (read it from WO-011's `scenarios/S04-*.yaml`). Add two or three demo operators, each labelled "(demo)" like S02's operator.
- `HailSimulator(scenario, run, *, seed, known, cfg)`:
  - `latency_ticks` attribute.
  - `send(contact_id, tick, *, previously_replied=False) -> HailResult`.
  - The idempotency key is `soundings.probe_keys.idempotency_key(contact_id, "acoustic_hail", {}, tick_bucket(tick, tick_s))`. A second `send` with the same key returns the cached result and does not draw again.
- `soundings/probe_keys.py` (new; the one SPEC-INVESTIGATION §5 key formula for every probe; WO-031 re-exports it and no WO defines a second one): `TICK_BUCKET_S = 30`, `tick_bucket(tick, tick_s) = (tick * tick_s) // TICK_BUCKET_S`, `idempotency_key(contact_id, probe_id, args, bucket)` = sha256 hex of `canonical_json([contact_id, probe_id, args or {}, bucket])`.
- **Reply rule.** Everything is evaluated at `sent_tick`; the reply lands at `reply_tick = sent_tick + latency_ticks`.
  1. Map the observed id to ground truth: `run.contact_truth[contact_id]`. An unknown id or a false alarm (`fa-*`) means no reply.
  2. The contact replies if `ContactTruth.replies_to_hail` is true and either `silent_after_tick` is None or `sent_tick < silent_after_tick`. Read `ContactTruth`, not the scenario file, so the `contact_goes_quiet` toggle is honoured.
  3. If `reply_loss_prob > 0`, draw a loss from `numpy.random.default_rng([seed, int(stable_hash([contact_id, sent_tick])[:8], 16)])`. There is no shared RNG.
  4. The modem id is the scenario contact's `registry.modem_id`, or else the modem id of the plan named by `registry.plan_id` in `scenario.registry_plans`, or else None.
  5. Pick the bucket:
     - Replied with a modem id that matches the pattern: `valid`.
     - Replied with no modem id or a malformed one: `invalid`.
     - No reply: `timeout` if `previously_replied`, else `none`.
     The `none`/`timeout` split uses only what an observer knows (did this lineage answer before?). Hidden truth never chooses the bucket.
  6. `operator_known = known.lookup(modem_id) is not None`.
  Filing status does not affect validity. An unfiled contact that answers with a well-formed modem id (S04) gets `valid`. Whether the modem matches a plan is the matcher's decision (WO-022). See the open question on the guidance wording.
- `HailResult` (frozen Pydantic): `contact_id, sent_tick, reply_tick, replied: bool, hail_reply: HailReply, modem_id: str | None, operator_known: bool, operator: str | None, idempotency_key, cost_usd`.
- `hail_evidence_rows(result, *, tick_s) -> list[EvidenceRow]` returns four rows for the contact:
  - Features: `hail_sent_at` (seconds, unit `s`), `hail_reply` (category), `modem_id` (id, or `"unknown"` when there was no reply), `operator_known` (bool).
  - Common fields: `t` = the reply tick, `kind=fact`, `probe_id=acoustic_hail`, `source="probe:acoustic_hail"`, `confidence=1.0` when replied, and the detection-free constant `0.9` for a non-reply (stated in the module docstring as a stress-test setting).
  - Rows are returned in this fixed order; the caller appends them through `EvidenceTable.append`.
  - `t` is the integer tick (WO-010 `EvidenceRow`); `hail_sent_at` holds seconds (`sent_tick * tick_s`).
- Module docstring (honesty, SPEC-INVESTIGATION §8): "Simulated acoustic challenge-response for this demonstration. Replies come from scenario ground truth. No real modem protocol, authentication, or signal processing is modelled."

Out: the registry and plan matching (WO-020, WO-022); deciding any outcome from a hail (WO-022 matcher); the probe allowlist and planner wrapper (WO-031 wraps `HailSimulator.send` as the `acoustic_hail` probe); `ask_operator_by_phone` and `camera_cue` (Phase 3); hail retries and scheduling (WO-022 runner).

## Spec references
`docs/SPEC-INVESTIGATION.md §1` (`UNFILED_KNOWN_OPERATOR`), `§2` (`modem_id`, `hail_sent_at`, `hail_reply ∈ {valid, invalid, none, timeout}`, `operator_known`), `§5` (`acoustic_hail`: ask, $0.01, 20 s; idempotency key formula), `§8`; `docs/SPEC-SIM.md §3` (S02, S04), `§4` (`replies_to_hail`, `silent_after_tick`, `registry_plans`); `config/likelihoods.yaml` (`hail_reply` buckets); `docs/WORKFLOW.md §8`.

## Steps
1. Branch from `main` after WO-010 merges. Until WO-011 merges, unit tests use hand-built `SimRun` fixtures; rebase on WO-011 before writing the integration tests.
2. Write `config/challenge.yaml` and `config/known_operators.yaml`, then `HailConfig`, `hail_latency_ticks` and `-k latency`.
3. Write `KnownOperatorTable` and `-k known_operators`. Copy S04's replying modem id into the file.
4. Write `HailSimulator.send`, the reply rule and the cache. Then write `-k reply_table` and `-k idempotent` (test first, table first).
5. Write `hail_evidence_rows` and `-k evidence_rows`, validating against WO-010 `EvidenceRow` (WO-022 appends them to a real `EvidenceTable`).
6. Write the integration and determinism tests on real catalog runs. Then run `make check` and the denylist grep, update STATE.md, and open the PR (rung: ask; rules §2.2, §2.4, §2.5, §2.6).

## Files expected
- `soundings/challenge/__init__.py`, `soundings/challenge/hail.py`, `soundings/probe_keys.py`
- `config/challenge.yaml`, `config/known_operators.yaml`
- `tests/test_hail.py` (unit), `tests/test_hail_integration.py` (catalog)

## Exit tests (must all pass with `MODEL_MODE=replay`, no network)
BUILD-PLAN Phase 2 has no exit test specific to this WO. Its outputs feed `tests/test_matcher.py` and `tests/test_ask_rung.py` in WO-022. The tests below are this WO's own.

Unit (`tests/test_hail.py`; uses small hand-built `Scenario` + `SimRun` fixtures, no sim run):
- [ ] `uv run pytest tests/test_hail.py -k latency` — `hail_latency_ticks(20, 2) == 10`, `(20, 3) == 7`, `(20, 20) == 1`, and `reply_tick == sent_tick + 10` at `tick_s=2`. Proves SPEC §5's 20 s becomes ticks with ceiling rounding.
- [ ] `uv run pytest tests/test_hail.py -k reply_table` — table-driven on (replies_to_hail, silent_after_tick vs sent_tick, modem id well-formed, previously_replied, false alarm) → expected `hail_reply`, `replied` and `modem_id`, with ≥ 10 rows. It includes the boundary `sent_tick == silent_after_tick` → no reply, and a reply with the modem id coming from the plan rather than the contact. Proves the reply rule exactly as written above.
- [ ] `uv run pytest tests/test_hail.py -k known_operators` — the union of file and scenario entries; a conflicting duplicate raises; `operator_known` follows the table. Proves the table that drives `UNFILED_KNOWN_OPERATOR`.
- [ ] `uv run pytest tests/test_hail.py -k idempotent` — two `send`s in the same tick bucket return equal results with the same key and the RNG is drawn once (counted through a spy); a send in the next bucket gets a new key. Proves the SPEC §5 idempotency rule for this probe.
- [ ] `uv run pytest tests/test_hail.py -k evidence_rows` — exactly four rows in the fixed order, all `kind=fact`, all `probe_id=acoustic_hail`, no `None` in any column, and `modem_id == "unknown"` on a non-reply. The rows pass `EvidenceRow` validation and have distinct `row_id`s. Proves the hail writes the SPEC §2 registry/identity columns in the table's own types.
- [ ] `uv run pytest tests/test_hail.py -k probe_keys` — the same (contact, probe, args, bucket) gives the same 64-hex key; a changed arg or the next bucket gives a new one; `tick_bucket(14, 2) == 0` and `tick_bucket(15, 2) == 1`. Proves the shared SPEC §5 key formula.
- [ ] `uv run pytest tests/test_hail.py -k config_agree` — `HailConfig.modem_id_pattern` equals `registry.modem_id_pattern` in `config/registry.yaml` once WO-020 is merged (skipped with a reason before then), and `cost_usd == 0.01`, `latency_s == 20`. Proves this WO and the registry agree on what a valid modem id is.

Integration (`tests/test_hail_integration.py`; real scenarios through WO-011's `run_scenario` at their own seeds):
- [ ] `uv run pytest tests/test_hail_integration.py -k s02` — take every observed id whose `contact_truth.gt_id == "gt-1"`. A hail sent at the first detection tick below 300 returns `valid` with `MOD-4471` (the modem comes from plan P-2026-1017-003). A hail at tick 400 returns `timeout` with `previously_replied=True` and `none` without it. Proves the gone-silent story the matcher needs: the vehicle answers before `silent_after_tick` and does not answer after it.
- [ ] `uv run pytest tests/test_hail_integration.py -k s04_s05` — the S04 ground-truth contact returns `valid` with `operator_known=True`; the S05 contact returns `none`; every `fa-*` observed id in S01 returns `none`. Proves the known-operator path and the non-responsive path on real catalog runs.
- [ ] `uv run pytest tests/test_hail_integration.py -k read_only` — compute `track_hash(run)` before and after 100 hails across all S01..S08 contacts; the hash is unchanged. Proves the hail is information only: it cannot move, stop or alter a contact.

Determinism:
- [ ] `uv run pytest tests/test_hail_integration.py -k deterministic` — with `reply_loss_prob=0.5`, hailing every S02 observed contact every 10 ticks gives an identical `canonical_json` result list across two simulators. A subprocess with `PYTHONHASHSEED=0` and one with `=1` print identical output, and seeds 3 and 4 differ. Proves CLAUDE.md §2.4: the hail depends on scenario, seed, contact and tick only.

Guard / rule proof:
- [ ] CLAUDE.md §2.2: `-k read_only` plus `grep -rn "soundings.llm" soundings/challenge` printing nothing. No model output reaches the hail, and the hail cannot act on the world.
- [ ] CLAUDE.md §2.5: `grep -riEf config/denylist.txt soundings/challenge config/challenge.yaml config/known_operators.yaml tests/test_hail*.py` prints nothing.
- [ ] CLAUDE.md §2.6: `-k evidence_rows`. Every hail output enters only as typed fact rows tagged with their probe, and the simulated nature is in the docstring the About panel quotes.
- [ ] `make check` green.

No manual check for this WO; the phase manual check is in WO-022.

## Interfaces
Exposed by WO-021:
- `soundings.challenge.hail.HailConfig`, `.load_hail_config`, `.hail_latency_ticks`
- `soundings.challenge.hail.KnownOperatorEntry`, `.KnownOperatorTable` (`lookup`), `.load_known_operators`
- `soundings.challenge.hail.HailResult`, `.HailSimulator` (`latency_ticks`, `send`), `.hail_evidence_rows`
- `soundings.probe_keys.TICK_BUCKET_S`, `.tick_bucket`, `.idempotency_key`
- Files: `config/challenge.yaml`, `config/known_operators.yaml`

Consumed:
- WO-010: `soundings.schemas.Scenario`, `ScenarioContact`, `ContactRegistrySpec`, `RegistryPlan`, `KnownOperator`, `EvidenceRow`, `EvidenceKind`, `HailReply`, `ProbeId`, `UNKNOWN`; `soundings.canonical.stable_hash`, `canonical_json`.
- WO-011: `soundings.sim.run.run_scenario`, `soundings.sim.types.SimRun`, `ContactTruth` (`gt_id`, `replies_to_hail`, `silent_after_tick`), `soundings.sim.hashing.track_hash`, `soundings.sim.scenario.resolve_scenario`.

## Cut line
If more than an hour late, drop these in order (no exit test is dropped):
1. `reply_loss_prob` becomes a hard-coded 0. The `deterministic` test then runs with loss 0, and the different-seeds assertion drops with the feature.
2. Scenario-level `known_operators`; the config file only.
3. `operator` name on `HailResult`; keep only `operator_known`.

## Model calls
None. Paid run needed? No.
Dependency adds needing owner approval (CLAUDE.md section 3): none (numpy is in the Phase 0 set).

## Rungs touched
ask (the `acoustic_hail` probe). Nothing above ask.

## Hard rules exercised
- §2.2: `-k read_only` and the `soundings.llm` grep.
- §2.4: `-k deterministic` (two runs, PYTHONHASHSEED subprocess diff, seed sensitivity) and `-k idempotent`.
- §2.5: the denylist grep.
- §2.6: `-k evidence_rows` and the honesty docstring.

## Done
PR: [#5](https://github.com/kudratsingh/soundings/pull/5) (branch `wo/021-challenge-response`) · Merged commit: squash commit on main (see PR) · Test summary: `make check` exit 0; full suite 395 passed after rebase on WO-020; hail 65 passed (unit `tests/test_hail.py` 55, integration `tests/test_hail_integration.py` 10); `-k config_agree` runs unskipped against WO-020's `config/registry.yaml` · STATE.md updated: yes (WO-021 row)

- Every exit test above passes with `MODEL_MODE=replay`; the `soundings.llm` grep and the denylist grep print nothing.
- Determinism: hailing every observed contact every 10 ticks on S01..S08 at loss 0.0 and 0.5 gives identical row-id and result hashes under `PYTHONHASHSEED=0` and `=1` (empty diff); seeds 3 and 4 differ at loss 0.5.
- Code on main absorbed: false alarms are observed `c-NNNN` ids whose `contact_truth[...].gt_id` starts with `fa-` (there are no `fa-*` observed ids; `ContactId` is `^c-\d{4,6}$`). `HailSimulator(..., known=None, cfg=None)` default to `load_known_operators(scenario=scenario)` and `load_hail_config()`. The tick bucket uses `run.tick_s`.
- Extras beyond the WO: `HONESTY_NOTE` constant (the docstring's first sentence, for the About panel), `HailSimulator.key(contact_id, tick)`, `KnownOperatorTable.from_entries`, module-level `loss_rng(seed, contact_id, sent_tick)` (the spy seam), `NO_REPLY_CONFIDENCE = 0.9`, `HAIL_SOURCE`.
- No cut-line item was dropped.

## Coordinator addenda (2026-10-16 23:05 PT, binding; see docs/log/DECISIONS.md)
- Hail validity does not depend on filing status: a well-formed reply with a modem id is `valid` even for an unfiled contact (SPEC-SIM S04). The matcher judges filing.
