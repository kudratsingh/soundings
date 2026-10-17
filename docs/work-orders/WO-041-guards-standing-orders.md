# WO-041 — guards-standing-orders

Phase: 4 · Branch: `wo/041-guards-standing-orders` · Worktree: `../soundings-worktrees/wo-041-guards-standing-orders` · Est: 4 h · Owner: Kudrat (planner / ladder / investigation)
Depends on: WO-010 (config loaders, `Harbor.basins()`, `stable_hash`, `canonical_json`, `ToggleName`), WO-040 (`GuardPort`, `GuardContext`, `GuardDecision`, `ActionScope`, `LadderMachine`, `soundings.ladder.ports.ApproverPort`, `ApprovalReply`, `soundings.clock.TickClock`). No sim or runner dependency, so it merges in wave 3; the real-catalog S05 proof runs in WO-043.

## Goal
The boundary table in `config/ladder.yaml` is what decides, through pure, table-driven guard functions over one typed `GuardInputs`. Standing orders are verified at load time. The signature is a sha256 over the canonical YAML of the `orders` mapping, and a sign CLI writes it. An unsigned, mismatched or expired file means nothing above `ask` executes. The `degraded_mode` block is honoured: rungs are allowed only within scope, and the `never` list is absolute. Approval tokens are signed and tied to one contact, one scope and one expiry. An approval can never widen scope. Plugged into a `LadderMachine`, an S05-shaped event stream under signed orders passes through HOLDING and is then handed off; unsigned, it goes straight to HANDED_OFF. WO-043 repeats this on the real S05 run.

## Scope
In:
- `soundings/ladder/boundary.py`:
  - `BoundaryCell` (`platform_alone`, `standing_orders`, `person_approves`, `never`).
  - `BoundaryTable` with `cell_of(action)`, `rung_of(action)` and `rows()` (the latter for WO-060's `GET /api/boundary`).
  - `SlowCriteria`, `HoldCriteria`, `LadderCriteria`, and `load_boundary(path="config/ladder.yaml") -> BoundaryTable`, which reads the `boundary` and `criteria` blocks.
  - `NEVER_ACTIONS` maps the standing-orders `never` names onto boundary actions: `harbor_closure` to `any_closure`, and `external_call` to `external_call_uscg_eod`. `anything_after_handoff` is always `never` for the platform.
- `soundings/ladder/standing_orders.py`:
  - `StandingOrders` (strict Pydantic over `orders`) and `StandingScope`.
  - `StandingOrdersStatus(signed, reason ∈ {ok, missing, unsigned, mismatch, expired, not_yet_valid}, orders, digest)`.
  - `canonical_orders_yaml(mapping)` is `yaml.safe_dump(sort_keys=True, default_flow_style=False, allow_unicode=True)` of the loaded mapping. `orders_digest(mapping)` is the sha256 hex of that string.
  - `load_verified(path, *, now: datetime) -> StandingOrdersStatus` never raises on a bad signature: it returns `signed=False` with the reason.
  - `effective_scope(status, degraded_reasons) -> StandingScope | None` returns `None` when unsigned. While a `degraded_mode.when` reason is active, it keeps only `allowed_rungs`.
  - `scenario_now(scenario, now_s) -> datetime` anchors sim time at 2026-10-17 `hour_of_day`:00 -07:00 plus `now_s`. It is deterministic, and `datetime.now` is never called.
- `soundings/ladder/sign.py`: `python -m soundings.ladder.sign [--path config/standing_orders.yaml] [--check]`.
  - It rewrites only the `^signature:` line, so comments and order survive.
  - `--check` exits 1 on a mismatch and writes nothing.
  - The result is idempotent: signing twice gives a byte-identical file.
- `soundings/ladder/approval.py`:
  - `ApprovalToken(contact_id, action, scope: ActionScope, approver, channel, issued_at, expires_at, mac)`. Times are run-clock seconds.
  - `issue_token(...)` accepts `channel ∈ {ui, sms_sim}` only. The mac is HMAC-SHA256 (stdlib `hmac`) over `canonical_json` of the other fields. The key comes from the env var `SOUNDINGS_APPROVAL_KEY`; otherwise a fixed demo key is used, and the UI labels it "demo key".
  - `verify_token(token, *, contact_id, requested, now_s) -> GuardDecision` blocks on a bad mac, another contact, `token.scope != requested` (any difference, wider or narrower), `now_s >= expires_at`, or `issued_at > now_s`.
  - `token_hash(token)` is what the audit log stores.
- `soundings/ladder/approver_sim.py`:
  - `ApproverScript(primary, backup)`. Each side is a `Reply(decision ∈ {granted, denied}, after_s)` or `None` for no reply. `ApproverScript.default()` has the primary grant after 60 s.
  - `ApproverScript.from_toggles(toggles)`: `approver_unreachable` makes both sides `None`; `network_down` makes the primary `None`.
  - `SimulatedApprover(script, clock)` implements WO-040's `ApproverPort`. It issues tokens through `issue_token` on the channel of the side that replied: the primary is a headless stand-in for the UI button, and the backup is `sms_sim`.
- `soundings/ladder/guards.py`:
  - `GuardInputs` (frozen) holds the SPEC-LADDER §4 inputs: threat_mass, benign_mass, time_to_nearest_critical_asset_s, zone_criticality, standing_orders_signed, standing_orders_scope, active_holds, approver_reachable, approval_token. It adds requested (ActionScope), basin, degraded_reasons, backup_paged and now_s.
  - Pure functions, each `(GuardInputs, LadderCriteria) -> GuardDecision`: `slow_permitted`, `hold_permitted`, `nothing_preauthorized` (permits iff neither of the other two permits), `scope_exceeded` (a second hold, more gates, or more minutes than scope), `approval_token_valid_and_scope_matches`, `backup_exists`, `no_backup`, and `boundary_decision(action, inputs)`.
  - Any `None` input that a criterion needs blocks, with reason `unknown_input:<name>`.
  - `BoundaryGuards(standing, boundary, approver_status)` implements `GuardPort`. It builds `GuardInputs` from WO-040's `GuardContext`, delegates the evidence guards to `evidence_guard`, and sets `granted_scope` from the standing scope (slow: basin, 30 min; hold: one gate or berth at `nearest_asset_id`, 15 min).
- `config/standing_orders.yaml`: the real `signature` value, written by the sign CLI. The owner must approve committing it (see open questions).

Out: SITREP and audit (WO-042); the driver over real runs, the `python -m soundings.ladder` CLI with `--orders`/`--unsigned`, departure events and runner v3 (WO-043); the UI approval path (WO-061's `soundings/stream/ladder_adapter.py` calls `issue_token` with `channel="ui"`); changing any threshold (CLAUDE.md §3).

Parallel start: the pure guards, standing orders, sign CLI and tokens need nothing from WO-040 and can start now. Only `BoundaryGuards` and the integration test wait for WO-040's merged types.

## Spec references
`docs/SPEC-LADDER.md §3` (guarded rows), `§4` (boundary and criteria), `§5` (standing orders, degraded mode), `§7` (approval) · `docs/BUILD-PLAN.md` Phase 4 · `CLAUDE.md §2.1, §2.2, §2.4, §3` · `config/ladder.yaml`, `config/standing_orders.yaml`.

## Files expected
- `soundings/ladder/boundary.py`, `standing_orders.py`, `sign.py`, `approval.py`, `approver_sim.py`, `guards.py`
- `config/standing_orders.yaml` (signature line only)
- `tests/test_guards.py`, `tests/test_standing_orders.py`, `tests/test_approval.py`, `tests/test_guards_integration.py`

## Exit tests (must all pass with `MODEL_MODE=replay`, no network)
- [ ] `pytest tests/test_guards.py` — table-driven over the boundary matrix: a `hold` on one gate ≤ 15 min under signed standing orders executes without a human; a `hold` > 15 min or on > 1 gate blocks until approval; with `standing_orders.signed=false` nothing above `ask` executes; approver unreachable → system stays at the highest pre-authorized rung and escalates to the backup approver, never auto-climbs.
- [ ] `pytest tests/test_guards.py::test_matrix_from_config` — proves one parametrized case per (rung, action) in `config/ladder.yaml` `boundary`, crossed with signed/unsigned and token/no-token, expects exactly the cell the YAML names. Expectations:
  - `platform_alone` permits always;
  - `standing_orders` permits only when signed, in scope and criteria are met;
  - `person_approves` permits only with a valid matching token;
  - `never` blocks even with a valid token.
- [ ] `pytest tests/test_guards.py::test_criteria_edges` — proves the §4 thresholds at, just above and just below each edge (threat 0.25 and 0.35; time 600 s and 300 s; criticality 0.7; `active_holds` 0 and 1), read from config, never literals in the test; a `None` input blocks.
- [ ] `pytest tests/test_guards.py::test_purity` — hypothesis over random `GuardInputs`. Proves each guard returns an equal `GuardDecision` and `inputs_hash` on repeat calls and under shuffled field construction, and that `degraded_reasons` can only remove permissions, never add one.
- [ ] `pytest tests/test_standing_orders.py` — on a temp copy, proves:
  - the placeholder signature gives `unsigned`;
  - signing gives `ok`;
  - editing one character inside `orders` gives `mismatch`;
  - `now` after `valid_until` gives `expired`, and before `signed_at` gives `not_yet_valid`;
  - `sign --check` exits 1 on a mismatch and 0 after `sign`;
  - signing twice is byte-identical;
  - comments survive.
- [ ] `pytest tests/test_approval.py` — proves:
  - a valid token permits;
  - a flipped mac byte, another contact, an expired token, a future `issued_at`, or a token whose scope is wider (2 gates, 30 min) or narrower than the request each block;
  - `issue_token` rejects any channel outside {ui, sms_sim};
  - an AST scan shows `issue_token` is called only from `approver_sim.py` and `soundings/stream/ladder_adapter.py` (WO-061, the UI button path), so no other path can create an approval.
- [ ] integration: `pytest tests/test_guards_integration.py` — a WO-040 `LadderMachine` with `BoundaryGuards`, `SimulatedApprover(ApproverScript.default())`, a `TickClock`, the real `config/ladder.yaml`, and signed temp orders, fed an S05-shaped event stream whose payload rows carry `threat_mass` 0.7, `time_to_nearest_critical_asset` falling below 300 s, and `zone_criticality` 0.9. Proves:
  - the checkpoints contain HOLDING, then AWAITING_APPROVAL (event `timer(hold_limit)`), then HANDED_OFF;
  - hold time is ≤ 15 min of run clock and interruptions are ≤ 1;
  - with `signed=False`, or `now` past `valid_until`, the same stream goes INVESTIGATING → HANDED_OFF with no ADVISING_SLOW or HOLDING;
  - with `ApproverScript.from_toggles([approver_unreachable])`, the backup is paged once, the run ends HANDED_OFF with reason `unreachable`, and no checkpoint ever climbs above hold.
  The same checks on the real S05 run are WO-043's `test_runner_v3.py`.
- [ ] `uv run python -m soundings.ladder.sign --check` exits 0 on the committed `config/standing_orders.yaml`, which proves the shipped orders are signed. This runs once the owner approves the signature commit.
- [ ] determinism: `uv run pytest tests/test_guards_integration.py -k deterministic` — the signed S05-shaped stream run twice in-process, and once each in subprocesses under `PYTHONHASHSEED=0` and `=1`, gives identical checkpoint hashes and guard `inputs_hash`es (CLAUDE.md §2.4 for guards).
- [ ] rule proof §2.2: `pytest tests/test_guards.py::test_no_model_imports` — an AST scan of `boundary.py`, `standing_orders.py`, `approval.py`, `guards.py` and `approver_sim.py` finds no `soundings.llm` or `anthropic` import. `GuardInputs` has no free-text field, so no model output can reach a guard.
- [ ] rule proof §2.1: `pytest tests/test_guards.py::test_never_list_absolute` — `any_closure`, `external_call_uscg_eod` and `anything_after_handoff` block with a valid token, signed orders and every degraded reason. `grep -riEf config/denylist.txt soundings/ladder tests/test_guards*.py tests/test_standing_orders.py tests/test_approval.py` → empty; `make check` green.

## Interfaces
Exposed:
- WO-041 `soundings.ladder.boundary.BoundaryCell`, `.BoundaryTable`, `.SlowCriteria`, `.HoldCriteria`, `.LadderCriteria`, `.load_boundary`, `.NEVER_ACTIONS`
- WO-041 `soundings.ladder.standing_orders.StandingOrders`, `.StandingScope`, `.StandingOrdersStatus`, `.canonical_orders_yaml`, `.orders_digest`, `.load_verified`, `.effective_scope`, `.scenario_now`
- WO-041 `soundings.ladder.sign` (CLI `python -m soundings.ladder.sign [--path] [--check]`)
- WO-041 `soundings.ladder.approval.ApprovalToken`, `.issue_token`, `.verify_token`, `.token_hash`
- WO-041 `soundings.ladder.approver_sim.ApproverScript`, `.Reply`, `.SimulatedApprover`
- WO-041 `soundings.ladder.guards.GuardInputs`, `.ApproverStatus`, `.slow_permitted`, `.hold_permitted`, `.nothing_preauthorized`, `.scope_exceeded`, `.approval_token_valid_and_scope_matches`, `.backup_exists`, `.no_backup`, `.boundary_decision`, `.BoundaryGuards`

Consumed:
- WO-040: `soundings.ladder.types.{GuardName, GuardDecision, GuardVerdict, GuardContext, ActionScope, LadderEvent}`, `soundings.ladder.guard_port.{GuardPort, evidence_guard}`, `soundings.ladder.ports.{ApproverPort, ApprovalReply}`, `soundings.ladder.machine.LadderMachine` (integration test), `soundings.clock.{Clock, TickClock}`.
- WO-010: `soundings.schemas.config.load_ladder_config`, `load_standing_orders` (structure only), `soundings.schemas.load_harbor`, `ToggleName`, `soundings.canonical.canonical_json`, `stable_hash`.

## Cut line
If this WO runs more than an hour late, drop in this order: (1) the `network_down` branch of `ApproverScript.from_toggles` (`approver_unreachable` stays); (2) `BoundaryTable.rows()` (WO-060's GET /api/boundary reads the YAML directly); (3) the comment-preserving rewrite in `sign.py`, replaced by a full dump with a header comment. Never an exit test.

## Model calls
None. Paid run needed? No.
Dependency adds needing owner approval (CLAUDE.md section 3): none (`hmac` and `hashlib` are stdlib; `pyyaml` is in the Phase 0 set).

## Rungs touched
slow, hold (standing orders), hand to a person (approval rows), ask (the ceiling when unsigned).

## Hard rules exercised
- §2.1: `test_never_list_absolute`.
- §2.2: `test_no_model_imports` and `test_purity`.
- §2.4: the two-run `diff`, and `scenario_now`, which has no wall clock.
- §3: thresholds come only from config (`test_criteria_edges`).

## Done
PR: [#9](https://github.com/kudratsingh/soundings/pull/9) · Merged commit: squash commit on main (see PR) · Test summary: `make check` exit 0, 950 passed after rebase on main (141 new: test_guards 95 incl. test_matrix_from_config 68, test_standing_orders 14, test_approval 22, test_guards_integration 10) · STATE.md updated: yes

- Every exit test passes with `MODEL_MODE=replay`; `uv run python -m soundings.ladder.sign --check` exits 0 on the committed, signed `config/standing_orders.yaml` (sha256 `0df7260f...a70`, tamper-evident, not authenticated; `standing_orders.SIGNATURE_LABEL` is the About-text wording).
- Determinism: the signed S05-shaped stream printed under `PYTHONHASHSEED=0` and `=1` diffs empty (13 lines of checkpoint hashes and guard `inputs_hash`es, both sha256 `3e752c87...`).
- Choices beyond the text (DECISIONS.md, WO-041 row): reason `invalid`; never cell overrides the YAML's `person_approves` for the three never actions; `boundary_decision(action, inputs, table) -> BoundaryDecision`; `scope_exceeded` BLOCK = exceeded (`exceeds_scope` bool); `GuardInputs` adds `contact_id`, `nearest_asset_id`; token `approver` is `primary`/`backup`.
- Cut line: nothing dropped.

## Coordinator addenda (2026-10-16 23:05 PT, binding; see docs/log/DECISIONS.md)
- The agent may run the sign CLI and commit the real sha256 signature for 'Watch Commander (demo)'. The check is tamper-evident, not authenticated; label it so in the About text; keyed HMAC/PKI is a production step, out of scope.
- Default headless approver in sim/catalog runs: the primary grants 60 s after the request (S05 stays at 1 interruption). Provide `denied` and `unreachable` options for the `approver_unreachable` toggle and tests.

## Notes for downstream (relayed by the coordinator from the implementer, 2026-10-16 23:25 PT)
- `boundary_decision(action, inputs, table: BoundaryTable) -> BoundaryDecision(action, cell, verdict, reason, inputs_hash, granted_scope)`. `BoundaryTable.rows()` returns rung, action, yaml_cell and cell (for WO-060 `GET /api/boundary`). `load_boundary()` returns a BoundaryTable with `.criteria` (LadderCriteria); `load_criteria()` is a shortcut.
- `scope_exceeded(inputs, criteria)`: BLOCK means exceeded; `exceeds_scope()` is the bool. Call it only while HOLDING with a requested scope in hand (a missing request also counts as exceeded).
- `BoundaryGuards(standing, boundary, approver_status=None, *, epoch=None)`; pass `epoch=scenario_now(scenario, 0)` to re-check `valid_until` on every decision. `ApproverStatus.from_toggles(toggles)`; `ApproverScript.default()/.denied()/.unreachable()/.from_toggles()`; `SimulatedApprover(script, clock).request(cid, scope, channel, now)` (primary for `ui`, backup for `sms_sim`).
- The machine does not call ApproverPort: the driver forwards REQUEST_APPROVAL to the primary channel and PAGE_BACKUP to the backup, polls each tick, and steps `reply_event(reply, tick)`. `tests/support/guard_streams.py::run_s05` is a working reference driver.
- A hold permit needs a `nearest_asset_id` row plus zone_criticality, time_to_nearest_critical_asset and threat_mass; a slow permit needs `payload.basin` in the orders' basins. Under signed orders S05 passes ADVISING_SLOW before HOLDING; the driver then emits `standing_order_check(hold_permitted)` once time-to-asset drops below 300 s.
- Unreachable approvers: the no_backup decision's reason is `unreachable` (SITREP flags on it). `ApprovalToken.approver` holds the approvers key (`primary`/`backup`). UI: `issue_token(contact_id=..., scope=<pending request scope exactly>, approver='primary', channel='ui', issued_at, expires_at)`; `key_label()`; `token_hash(token)` is what the audit stores.
- `standing_orders.SIGNATURE_LABEL` is the About-panel wording. `config/standing_orders.yaml` is signed: any edit to `orders` needs `uv run python -m soundings.ladder.sign` or `test_committed_orders_are_signed` fails.
- Files under `soundings/ladder` must use `import datetime as dt` (WO-040's wall-clock AST scan flags `from datetime import datetime`).
- Coordinator rulings on the open items (DECISIONS.md 02:20): approval token TTL moves to a new additive `approval: {token_ttl_s: 600}` block in `config/ladder.yaml` (WO-043); the hold is released at its limit even while AWAITING_APPROVAL (SPEC-LADDER §3 'hold stays within limit'), with the state staying AWAITING_APPROVAL until the approval resolves (WO-043 driver); `ToggleName.STANDING_ORDERS_EXPIRED` is wired by WO-043's driver into BoundaryGuards via an expired epoch.
