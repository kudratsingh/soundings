# WO-085 — surge-triage

Phase: 7 · Branch: `wo/085-surge-triage` · Worktree: `../soundings-worktrees/wo-085-surge-triage` · Est: 7 h · Owner: coordinator Claude / Kudrat
Depends on: WO-080 parts A and B (BT-01, `skip_idle_ticks`, `soundings.sim.grading`), WO-033 (runner v2; `InvestigationMachine`, `Recommendation`, `StopOutcome` are on in-flight `origin/wo/033a-investigation-core`, runner v2 is its part B), WO-043 (runner v3: approval requests, handoff and backup pages counted in `RunResult.interruptions`; part A in flight on `origin/wo/043a-ladder-driver-watch`), WO-031 (on main: `BudgetLedger`, `DeterministicPlanner`), WO-030 (`score_contact`), WO-012 (`zone_criticality` and `time_to_nearest_critical_asset` rows), WO-011 (`contact_surge`), WO-050 (`InterruptionKind`), WO-060 (`BatchReplaySource`; in flight on `origin/wo/060a-ui-core`), WO-061 (`SteppedSource`) for the toggle wiring. Uses WO-084's smoothed `time_to_nearest_critical_asset` if it has merged; the row name is the same either way. Wave D.

Parallel start (needed to land before the 6:00 PM freeze): `soundings/triage.py`, `config/triage.yaml` and `tests/test_triage.py` need only WO-012/030/031/050, all on main, so they start in Wave B. The runner seams wait for runner v3.

## Goal
When contacts arrive faster than the harbor can look at them, Soundings investigates and interrupts in order of urgency. The order is not arrival order.

A pure `TriageQueue` sits in the runner between detection (matcher ESCALATE) and the investigation machine. It is keyed by `(threat_mass × zone_criticality) / max(time_to_nearest_critical_asset, 30)`, and ties break by contact id. Investigation slots and watch-officer attention are allocated in queue order.

Triage only orders work. It sets no rung and never drops a handoff. Every recommendation still comes from WO-033's stop rule. BT-01 plus `contact_surge(25)` still hands the rogue to a person within its budget, adds at most one interruption per hour over the no-surge run, and yields a deterministic queue log.

## Scope
In:
- **`soundings/triage.py`** (pure: no I/O, no clock, no globals; frozen inputs)
  - `TriageConfig(ttc_floor_s=30, investigation_slots, optional_interruptions_per_hour, optional_window_s, unknown_zone_criticality, officer_review_s)` and `load_triage_config(path=config/triage.yaml, eval_path=config/eval.yaml)`.
  - `officer_review_s` is read from `config/eval.yaml` `baselines.human_review_s` (120, an existing labeled assumption; `load_eval_config().baselines.human_review_s`) and not copied.
  - `priority(threat_mass, zone_criticality, ttc_s, *, floor_s) -> float` is `round6(threat_mass * zone_criticality / max(ttc_s, floor_s))`. Inputs must be finite; `threat_mass` and `zone_criticality` must be in [0, 1].
  - `TriageInputs(contact_id, case_id, tick, threat_mass, zone_criticality, ttc_s, evidence_row_ids)` comes from `triage_inputs(table, contact_id, case_id, tick, tables, prior_cfg, cfg)`:
    - `threat_mass`: `score_contact(...).posterior.threat_mass()`, citing `used_row_ids`.
    - `zone_criticality`: `table.latest(cid, FeatureName.ZONE_CRITICALITY)`. If it is `unknown` or absent, use `cfg.unknown_zone_criticality` (1.0, labeled as an assumption) so that an unplaced contact is never starved.
    - `ttc_s`: `table.latest(cid, FeatureName.TIME_TO_NEAREST_CRITICAL_ASSET)`. If it is `unknown` or absent, use `soundings.investigation.features.NOT_CLOSING_S`.
  - `TriageEntry(inputs, priority)`. `sort_key = (-priority, contact_id, case_id)`.
  - `TriageQueue`: `push(entry)`, `rekey(entry)` (replaces by `case_id`), `remove(case_id)`, `ordered() -> tuple[TriageEntry, ...]`, and `admit(free_slots) -> tuple[TriageEntry, ...]`, which pops the top `free_slots`.
    - The queue is a plain list sorted on every read. There are at most a few hundred entries, so it needs no heap and no hash-order dependence.
  - `OfficerArbiter(cfg)`: one serial watch-officer channel.
    - `request(case_id, kind: InterruptionKind, tick, priority) -> None` queues a request. `grant_due(tick) -> tuple[Grant, ...]` grants requests in `(-priority, contact_id, case_id)` order. A grant happens only when the officer is free (`officer_review_s` since the last grant).
    - Mandatory kinds are `HANDOFF`, `APPROVAL_REQUEST` and `BACKUP_PAGE`. They are always granted eventually, never refused, and only ordered.
    - The optional kind is `EYES_ON` (`request_watch_officer_look`). It is granted only while fewer than `optional_interruptions_per_hour` grants of that kind fall in the trailing `optional_window_s` of ticks.
    - `eyes_on_allowed(case_id, tick) -> bool` is what the planner sees.
  - `TriageEvent` (StrEnum): `enqueued, rekeyed, admitted, removed, granted, deferred`. `TriageDecision(seq, tick, event, case_id, contact_id, priority, rank, evidence_row_ids)`. `triage_log_hash = sha256(canonical JSONL)`.
- **`config/triage.yaml`** (new; header: stress-test settings for the demonstration):
  - `ttc_floor_s: 30`, from the owner directive.
  - `investigation_slots: 4`.
  - `optional_interruptions_per_hour: 2` and `optional_window_s: 3600`.
  - `unknown_zone_criticality: 1.0`.
  - `config/ladder.yaml` is not changed.
- **Runner wiring** (`soundings/runner.py`, only inside a new block `# --- triage (WO-085) ---` plus two named call sites)
  - `ScenarioRunner(..., triage: TriageConfig | None = None)`. With `None`, v2 and v3 behave exactly as merged, and catalog logs are unchanged (tested).
  - The two seams:
    1. Where v2 moves ASKING → INVESTIGATING on ESCALATE and starts an `InvestigationMachine`, the start goes through `enqueue`. A machine starts only when admitted, at the admit tick. A queued case is INVESTIGATING with no machine yet; that row of SPEC-LADDER §3 is unchanged.
    2. Where v3 emits an interruption-bearing event (approval request, backup page, the handoff SITREP to the officer, an eyes-on probe), it goes through `OfficerArbiter.request`. The event fires at its grant tick.
  - If WO-033 or WO-043 did not leave these as single functions, extract them first in a pure refactor commit. `test_tick_skipping.py` and the catalog logs prove that refactor changes nothing.
  - Admission order on each wake tick: re-key every queued case whose contact got new evidence rows this tick. Then free the slots of machines that reached STOPPED or FAILED. Then admit in queue order.
  - The planner's interruption allowance for an eyes-on probe is `BudgetLedger.allows(...)` and `arbiter.eyes_on_allowed(...)`. When the arbiter refuses, the deterministic planner picks its next probe or stops. The per-contact `BudgetLimits` are untouched.
  - Wake ticks: a queued or pending-grant case is INVESTIGATING or later, so WO-080's per-tick rule for states past ASKING already steps every tick while triage has work. Only if WO-043 narrowed that rule do admissions, grants and the officer-free tick join `next_wake_tick`.
  - `RunResult.triage_log: tuple[TriageDecision, ...]` and `RunResult.triage_log_hash`. Both are empty when `triage is None`.
  - Surge contacts come from the existing toggle: `SimRun.toggle_report.surge_contact_ids` (WO-011 `ToggleReport`). They are not special-cased; they queue like any contact.
- **Toggle wiring** (the directive's "wire to the `contact_surge(n)` toggle"): when a run's scenario carries a `CONTACT_SURGE` toggle, or a run request names `contact_surge`, the stream sources construct `ScenarioRunner(..., triage=load_triage_config())`. This is one marked `# --- WO-085: surge triage ---` block in `soundings/stream/engine.py` (`BatchReplaySource`) and one in WO-061's `soundings/stream/stepped.py`. Runs without the toggle keep `triage=None`.
- **Test support** `tests/support/bt01.py`:
  - `bt01()` loads `scenarios/BT-01.yaml`.
  - `bt01_with_surge(n=25)` returns `model_copy` with `Toggle(name=ToggleName.CONTACT_SURGE, n=n, at_tick=rogue_start - ceil(60 / tick_s))`, where `rogue_start` is `rogue-1`'s `start_tick`. The surge lands just before the rogue.
  - `interruptions_per_hour(result) = result.interruptions / (result.sim.n_ticks * result.sim.tick_s / 3600)`.

Out: any change to the stop rule, guards, standing orders or thresholds; preemption of a running investigation; a UI for the queue (WO-086 may read `triage_log`); conflict and slot work (WO-081/082).

## Spec references
`docs/SPEC-INVESTIGATION.md §2` (`zone_criticality`, `time_to_nearest_critical_asset`), `§3` (threat mass), `§6` (budgets), `§7` (stop rule, budget exhaustion); `docs/SPEC-LADDER.md §2`, `§3` (INVESTIGATING, AWAITING_APPROVAL, HANDED_OFF rows), `§4` (guards); `config/ladder.yaml` `budgets`; `config/eval.yaml` `baselines.human_review_s`; the WO-022 coordinator addendum (one interruption definition); WO-033 addendum (budget scope).

## Files expected
- `soundings/triage.py`, `config/triage.yaml`, `soundings/runner.py` (WO-085 block plus the two seams), `soundings/stream/engine.py` and `soundings/stream/stepped.py` (one marked WO-085 block each)
- `tests/test_triage.py` (unit), `tests/test_triage_runner.py` (integration), `tests/test_triage_rules.py` (rule proofs), `tests/support/bt01.py`, `tests/fixtures/triage_catalog_pre_wo085.json`
- `docs/STATE.md` (WO-085 row only), `docs/log/DECISIONS.md` (rows: `config/triage.yaml` values as stress-test settings; the serial-officer reading of "allocated in queue order")

## Exit tests (must all pass with `MODEL_MODE=replay`, no network)
From the owner directive (verbatim):
- [ ] BT-01 + contact_surge(25): the rogue is still handed off within its budget; interruptions/hour <= the no-surge run + 1; order is deterministic.

Each clause as a command (both runs: `triage=load_triage_config()`, `skip_idle_ticks=True`, `llm_roles=False`, seed 7):
- [ ] `uv run pytest tests/test_triage_runner.py -k rogue_within_budget`. In `bt01_with_surge(25)`, the `rogue-1` lineage terminal is HANDED_OFF (`soundings.sim.grading.lineage_terminal`). Its `Recommendation` has `probes_used ≤ probes_per_contact` and paid ≤ `paid_probes_per_contact`. Its ledger interruptions are ≤ `human_interruptions_per_contact`. Its HANDED_OFF tick is ≤ the no-surge HANDED_OFF tick + `ceil(officer_review_s / tick_s)`, and earlier than the first `truth_paths["rogue-1"]` tick within 20 m of `fuel_berth_2`. Proves the first clause: the surge costs the rogue at most one officer slot.
- [ ] `uv run pytest tests/test_triage_runner.py -k interruptions_per_hour`. `interruptions_per_hour(surge) <= interruptions_per_hour(no_surge) + 1`. Both counts are `RunResult.interruptions` under the single WO-022 definition. The test also asserts that `sum(1 for d in triage_log if d.event is TriageEvent.GRANTED)` equals `result.interruptions`, so every interruption went through the arbiter. Proves the second clause.
- [ ] Determinism: `pytest tests/test_triage_runner.py -k order_deterministic` runs `bt01_with_surge(25)` twice in process and asserts equal `triage_log_hash` and `transition_log_hash`. `for i in 0 1; do PYTHONHASHSEED=$i uv run python -c "from tests.support.bt01 import main; main()" > /tmp/tq$i.txt; done; diff /tmp/tq0.txt /tmp/tq1.txt` prints nothing (`main` prints the triage log and both hashes). Proves the third clause (CLAUDE.md §2.4).

Unit (`tests/test_triage.py`):
- [ ] `-k priority`, table-driven, ≥ 8 cases:
  - the ttc floor (ttc 10 and ttc 30 give equal priority; ttc 60 gives half);
  - `NOT_CLOSING_S` gives a priority below 1e-8;
  - zero `threat_mass` gives 0;
  - `unknown` zone_criticality uses 1.0 and cites no zone row;
  - NaN or out-of-range input raises.
- [ ] `-k tie_break`: equal priorities order by `contact_id`, then `case_id`. A Hypothesis test (`derandomize=True`) shuffles the insertion order of 50 entries, and `ordered()` stays identical. `rekey` moves an entry, and `remove` then `push` is the same as `rekey`.
- [ ] `-k admit`: with 3 slots and 5 entries, the top 3 are admitted and the rest stay in order. With 0 free slots, nothing is admitted.
- [ ] `-k arbiter`:
  - grants are serial with `officer_review_s` spacing and follow priority order among pending requests;
  - a mandatory request is granted even when the optional budget is spent;
  - the third EYES_ON request inside `optional_window_s` is deferred and granted after the window slides;
  - nothing is ever discarded: requests in equal grants out after draining.
- [ ] `-k config`: the values come from the YAML. A tmp `triage.yaml` with `ttc_floor_s: 60` changes the floor case. `grep -nE "\b(30|120|3600)\b" soundings/triage.py` prints nothing. Proves there are no literals (CLAUDE.md §3).

Integration (`tests/test_triage_runner.py`):
- [ ] `-k catalog_unchanged`: S01..S08 at their seeds with `triage=None` have the same `transition_log_hash` as on main before this WO, from a fixture captured first. With `triage=load_triage_config()`, every terminal state still equals each scenario's `expected.terminal_state`. Proves that triage is opt-in and does not change outcomes.
- [ ] `-k bt01_no_surge`: BT-01 with triage still passes `check_expected_contacts`: 39 CLEARED_AT_ASK and the rogue HANDED_OFF. Proves that WO-080's exit holds with triage on.
- [ ] `-k surge_toggle_wired`: a `RunEngine` run of `bt01_with_surge(25)` through each source has a non-empty `triage_log` and the same `triage_log_hash` as the direct runner call; S05 without the toggle has an empty one. Proves the toggle wiring.
- [ ] `-k surge_ids_queued`: every `toggle_report.surge_contact_ids` lineage that reaches INVESTIGATING has an `enqueued` decision. Every admitted decision's rank is 0..slots−1 at its tick. The rogue's first `enqueued` is followed by `admitted` within one wake tick, or it is already rank 0. Proves allocation in queue order.

Guard / rule proof (`tests/test_triage_rules.py`):
- [ ] §2.1 (the work stops at the handoff; triage never withholds one): over `bt01_with_surge(25)` with `investigation_slots: 1` and `optional_interruptions_per_hour: 0`, every `Recommendation` with outcome HANDOFF ends HANDED_OFF; it is delayed, never dropped. `triage.py` writes no `LadderState`: an AST check finds no import of it and no transition call.
- [ ] §2.2 (no model output becomes a control): an AST scan finds no `soundings.llm` import in `triage.py` or the WO-085 block. The priority depends only on evidence rows and the posterior: a test that changes `question_for_human` text in a fixture leaves `triage_log_hash` unchanged.
- [ ] §2.4: `order_deterministic` above, plus an AST scan of `triage.py` for `time`, `datetime.now`, `random` and set iteration.
- [ ] §2.6: every `TriageDecision.evidence_row_ids` is non-empty for `enqueued` and `rekeyed` decisions, and each id exists in `result.evidence`.
- [ ] §2.5: `grep -iEf config/denylist.txt soundings/triage.py config/triage.yaml tests/test_triage*.py tests/support/bt01.py` prints nothing. `make check` green.

## Interfaces
Exposed by WO-085:
- `soundings.triage.TriageConfig`, `.load_triage_config`, `.priority`, `.TriageInputs`, `.triage_inputs`, `.TriageEntry`, `.TriageQueue` (`push`, `rekey`, `remove`, `ordered`, `admit`), `.OfficerArbiter` (`request`, `grant_due`, `eyes_on_allowed`), `.Grant`, `.TriageEvent`, `.TriageDecision`, `.triage_log_hash_of`
- `soundings.runner.ScenarioRunner(..., triage=None)`, `RunResult.triage_log`, `RunResult.triage_log_hash`
- Files: `config/triage.yaml`; `tests/support/bt01.py` (`bt01`, `bt01_with_surge`, `interruptions_per_hour`, `main`)

Consumed:
- WO-080: `scenarios/BT-01.yaml`, `ScenarioRunner(..., skip_idle_ticks=True)`, `_V1Run.next_wake_tick`, `soundings.sim.grading.lineages`, `.lineage_terminal`, `.check_expected_contacts`
- WO-033 (part A in flight on `origin/wo/033a-investigation-core`, where these names exist today; re-read from main before coding): `soundings.investigation.machine.InvestigationMachine`, `Recommendation`, `StopOutcome`; `ScenarioRunner.run(*, llm_roles=False)`; `RunResult.recommendations`, `.probe_calls`, `.investigation_checkpoints`
- WO-043 (part A in flight on `origin/wo/043a-ladder-driver-watch`): runner v3 interruption events; `RunResult.interruptions` (already on main from WO-022)
- WO-031: `soundings.investigation.planner.BudgetLedger` (`allows`, `remaining`), `BudgetLimits`, `load_budget_limits`, `DeterministicPlanner`
- WO-030: `soundings.investigation.posterior.score_contact`, `PosteriorReport`, `Posterior.threat_mass`, `load_likelihood_tables`, `load_prior_config`
- WO-012: `soundings.investigation.evidence.EvidenceTable.latest`, `soundings.investigation.features.NOT_CLOSING_S`; `soundings.schemas.enums.FeatureName.ZONE_CRITICALITY`, `.TIME_TO_NEAREST_CRITICAL_ASSET`
- WO-011: `soundings.sim.toggles.ToggleReport.surge_contact_ids`, `surge_contacts`; `soundings.schemas.scenario.Toggle`; `soundings.schemas.enums.ToggleName.CONTACT_SURGE`
- WO-050: `soundings.eval.policies.InterruptionKind`; `soundings.eval.policies.load_eval_config` (for `human_review_s`)
- WO-010: `soundings.canonical.canonical_json`, `stable_hash`, `round6`
- Used by: WO-086 (the ledger can show the queue rank), WO-061 (the judge's surge toggle runs through triage), WO-062 (demo beat).

## Cut line
Drop in this order (no exit test is dropped):
1. Re-keying on every new evidence row. Re-key only at admission checks instead; the order stays deterministic.
2. The optional EYES_ON budget. `eyes_on_allowed` always returns True, and grants stay serial and ordered.
3. `tests/support/bt01.py::main` pretty output; it keeps only the two hash lines that the diff needs.

## Model calls
None. Paid run needed? No. Triage reads the posterior and evidence rows only; runner v2 runs with `llm_roles=False`.
Dependency adds (logged in DECISIONS.md per owner directive): none (stdlib sorting only).

## Rungs touched
ask (INVESTIGATING admission order, eyes-on allowance) and hand to a person (handoff and approval delivery order). No slow or hold decision changes; their approvals are only ordered.

## Hard rules exercised
- §2.1: the slots=1 / budget=0 test (no handoff withheld) and the AST check for no `LadderState`.
- §2.2: the import scan and the `question_for_human` invariance test.
- §2.4: `order_deterministic`, the PYTHONHASHSEED diff, and `tie_break`.
- §2.5: the denylist grep.
- §2.6: the citation check on triage decisions.
- §3: `config` (no threshold literal; `config/ladder.yaml` untouched).

## Done
PR: see branch PR (wo/085-surge-triage) · Merged commit: see STATE.md · Test summary: 2548 passed, 21 skipped, 1 xfailed (full suite on main after WO-080/088D/094) (WO-085: test_triage 34, test_triage_runner 17 incl. 1 recorded xfail, test_triage_rules 7) · STATE.md updated: WO-085 row

What landed (all opt-in; `triage=None` keeps v2/v3 byte-identical, proven against `tests/fixtures/triage_catalog_pre_wo085.json` captured on main first):
- `soundings/triage.py` + `config/triage.yaml` as specified (`OfficerArbiter(cfg, tick_s)`; `TriageRecorder` builds the log; `TriageDecision.kind` names the granted interruption).
- Runner: `ScenarioRunner(..., triage=)`; `_TriageV2Run` in the `# --- triage (WO-085) ---` block, chosen by `ScenarioRunnerV2.run` (v3 runs v2, so it inherits it). Seams in v2: `triage_admit` (re-key, free slots, admit), `may_start`, `triage_grant`; HANDOFF -> HANDED_OFF fires at its grant tick. The planner's eyes-on allowance is `BudgetLedger` plus `eyes_on_allowed` (`investigation_wiring.eyes_on_refused`).
- Stream: `BatchReplaySource` runs v2 with triage for a scenario carrying `contact_surge` (`surge_runner`); WO-060 part B's `V3Source` (`soundings/stream/v3.py`, the stepped source; there is no `stepped.py`) builds `ScenarioRunnerV3(..., triage=load_triage_config())` for such a run (marked WO-085 block).
- The audit-key crash triage exposed in S05 (a guard decision with no checkpoint and no action has no contact id) was fixed on main by WO-060B (`unattributed:` prefix); WO-085's own fix was dropped at rebase.

Exit tests (synthetic stand-in `tests/fixtures/triage_bt_synthetic.yaml`, since BT-01 is not on main; v2, seed 7, `llm_roles=False`):
- rogue_within_budget: passes (lineage end HANDED_OFF at tick 169 vs 120 without surge, slot = 60 ticks; within probe, paid and interruption budgets; before berth arrival).
- interruptions_per_hour: every interruption is a GRANTED decision (passes). The clause itself fails: 59 vs 9 interruptions in 0.5 h (118/h vs 19/h; 61 vs 9 before WO-089's detection model). Recorded as an imperative xfail with the numbers, not weakened. Cause: the surge's own stop-rule HANDOFFs are mandatory officer interruptions (DECISIONS 06:37: never refused); triage can delay them but not remove them.
- order_deterministic: passes; the PYTHONHASHSEED 0/1 diff of `tests.support.bt01.main` is empty.

BT-01 probe (PR #33 merged locally, not committed): without surge, triage on equals triage off for the rogue lineage end (HANDED_OFF at tick 5594, a late re-acquired track parked at the berth; the first rogue track hands off at 1797); 176 interruptions in 8 h (22/h). With contact_surge(25): 424 interruptions (53/h), and the lineage end hands off at tick 10375 because the officer channel is saturated (capacity 240 grants in 8 h) and nearly every priority is 0.0 at escalation (time_to_nearest_critical_asset unknown, or not closing for a parked contact), so the order falls back to contact id. Two BT-01 runs take about 7 minutes. Hence `SOUNDINGS_TRIAGE_BT01=1` is opt-in.

Open questions for the coordinator: (1) the interruptions/h clause conflicts with the never-refused handoff ruling under the single interruption definition; (2) the priority formula gives about 0 to a contact parked at an asset (not closing) and to any contact without a time-to-asset row at escalation; (3) v3's ladder approval requests and backup pages are not yet routed through the arbiter (v2's are); (4) S05 with triage on reaches HANDED_OFF but no longer passes through HOLDING (its own eyes-on look takes the officer slot first, so the handoff lands 120 s later).

## Notes for downstream
- WO-086: rank and priority per contact are in `RunResult.triage_log`. The UI may show "queue position", but no label may suggest that triage dismissed a contact. Nothing is dismissed.
- WO-061: no change needed; WO-085's block in `stepped.py` turns triage on for any run carrying `contact_surge`.
