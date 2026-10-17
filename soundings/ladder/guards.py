"""Boundary guards: pure, table-driven functions over one typed ``GuardInputs``.

SPEC-LADDER sections 3, 4, 5 and 7. Each guard is ``(GuardInputs, LadderCriteria) ->
GuardDecision`` and reads nothing else: no clock, no environment beyond the approval key,
no model (CLAUDE.md 2.2; ``tests/test_guards.py::test_no_model_imports``). Thresholds come
only from ``config/ladder.yaml`` through ``LadderCriteria`` (CLAUDE.md section 3).

Rules every guard keeps:

- Any ``None`` input a criterion needs blocks, with reason ``unknown_input:<name>``.
- Unsigned orders (``standing_orders_signed`` false or no scope) pre-authorize nothing.
- ``degraded_reasons`` only narrow the standing scope (``StandingScope.under``), so they
  can remove a permission and never add one.
- The never list is absolute (``boundary.ABSOLUTE_NEVER``): no token, signature or mode
  lets the platform execute it (CLAUDE.md 2.1).

``inputs_hash`` is ``stable_hash`` of the ``GuardInputs`` the guard read. ``BoundaryGuards``
adapts WO-040's ``GuardPort``: it builds ``GuardInputs`` from a ``GuardContext`` and leaves
the three evidence guards to ``guard_port.evidence_guard``.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Annotated, Any, Literal

from pydantic import Field, field_validator

from soundings.canonical import round6, stable_hash
from soundings.schemas import ToggleName
from soundings.schemas._base import HarborId, NonNegInt, Probability, StrictModel

from .approval import ApprovalToken, parse_token, token_hash, token_problem
from .boundary import ABSOLUTE_NEVER, BoundaryCell, BoundaryTable, LadderCriteria
from .guard_port import evidence_guard
from .standing_orders import StandingOrdersStatus, StandingScope, effective_scope
from .types import (
    EVIDENCE_GUARDS,
    ActionScope,
    GuardContext,
    GuardDecision,
    GuardName,
    GuardVerdict,
)

ContactId = Annotated[str, Field(pattern=r"^c-\d{4,6}$")]

UNSIGNED = "standing_orders_unsigned"


class GuardInputs(StrictModel):
    """The SPEC-LADDER section 4 guard inputs plus what the boundary checks need.

    No field holds free text: every string is a patterned id or an enum, so no model output
    can reach a guard. Floats are rounded to 6 dp and ``degraded_reasons`` is sorted and
    de-duplicated at construction, so equal inputs hash equal however they were built.
    """

    contact_id: ContactId
    now_s: float
    threat_mass: Probability | None = None
    benign_mass: Probability | None = None
    time_to_nearest_critical_asset_s: float | None = None
    zone_criticality: Probability | None = None
    standing_orders_signed: bool = False
    standing_orders_scope: StandingScope | None = None
    active_holds: NonNegInt | None = None
    approver_reachable: bool | None = None
    approval_token: ApprovalToken | None = None
    requested: ActionScope | None = None
    basin: HarborId | None = None
    nearest_asset_id: HarborId | None = None
    degraded_reasons: tuple[ToggleName, ...] = ()
    backup_paged: bool = False

    @field_validator(
        "now_s",
        "threat_mass",
        "benign_mass",
        "time_to_nearest_critical_asset_s",
        "zone_criticality",
        mode="after",
    )
    @classmethod
    def _round(cls, v: float | None) -> float | None:
        return None if v is None else round6(v)

    @field_validator("degraded_reasons", mode="after")
    @classmethod
    def _sorted(cls, v: tuple[ToggleName, ...]) -> tuple[ToggleName, ...]:
        return tuple(sorted(set(v)))


def inputs_hash(inputs: GuardInputs) -> str:
    return stable_hash(inputs)


def _decide(
    guard: GuardName,
    inputs: GuardInputs,
    permit: bool,
    reason: str,
    scope: ActionScope | None = None,
) -> GuardDecision:
    return GuardDecision(
        guard=guard,
        verdict=GuardVerdict.PERMIT if permit else GuardVerdict.BLOCK,
        reason=reason,
        inputs_hash=inputs_hash(inputs),
        granted_scope=scope if permit else None,
    )


def _scope(inputs: GuardInputs) -> StandingScope | None:
    """The standing scope in force: ``None`` unless signed, narrowed for degraded reasons."""
    if not inputs.standing_orders_signed or inputs.standing_orders_scope is None:
        return None
    return inputs.standing_orders_scope.under(inputs.degraded_reasons)


# --- WO-083 begin: _slow_scope_problem extraction (the one named edit outside the block) ---
def _slow_scope_problem(inputs: GuardInputs) -> tuple[str | None, StandingScope | None]:
    """The standing-orders half of the slow rung, shared by every slow guard.

    Signed orders, ``slow`` in the scope, the degraded-mode narrowing (``_scope``) and the
    basin in ``scope.slow.basins``. Returns the first problem, or ``None`` and the scope.
    """
    scope = _scope(inputs)
    if scope is None:
        return UNSIGNED, None
    if scope.slow is None:
        return "slow_not_in_standing_scope", scope
    if inputs.basin is None:
        return "unknown_input:basin", scope
    if inputs.basin not in scope.slow.basins:
        return "basin_out_of_standing_scope", scope
    return None, scope


def _slow_problem(inputs: GuardInputs, criteria: LadderCriteria) -> tuple[str | None, ActionScope]:
    c = criteria.slow
    granted = ActionScope(action=c.action)
    scope_problem, scope = _slow_scope_problem(inputs)
    if scope_problem is not None or scope is None or scope.slow is None:
        return scope_problem or UNSIGNED, granted
    # --- WO-083 end ---
    if inputs.threat_mass is None:
        return "unknown_input:threat_mass", granted
    if inputs.time_to_nearest_critical_asset_s is None:
        return "unknown_input:time_to_nearest_critical_asset_s", granted
    granted = ActionScope(action=c.action, basin=inputs.basin, max_minutes=scope.slow.max_minutes)
    if inputs.threat_mass < c.threat_mass_min:
        return "threat_mass_below_slow_min", granted
    if not inputs.time_to_nearest_critical_asset_s < c.time_to_critical_asset_max_s:
        return "time_to_critical_asset_not_below_slow_max", granted
    return None, granted


def _hold_problem(inputs: GuardInputs, criteria: LadderCriteria) -> tuple[str | None, ActionScope]:
    c = criteria.hold
    scope = _scope(inputs)
    granted = ActionScope(action=c.action)
    if scope is None:
        return UNSIGNED, granted
    if scope.hold is None or scope.hold.max_gates < 1:
        return "hold_not_in_standing_scope", granted
    if inputs.threat_mass is None:
        return "unknown_input:threat_mass", granted
    if inputs.time_to_nearest_critical_asset_s is None:
        return "unknown_input:time_to_nearest_critical_asset_s", granted
    if inputs.zone_criticality is None:
        return "unknown_input:zone_criticality", granted
    if inputs.active_holds is None:
        return "unknown_input:active_holds", granted
    if inputs.nearest_asset_id is None:
        return "unknown_input:nearest_asset_id", granted
    granted = ActionScope(
        action=c.action,
        asset_id=inputs.nearest_asset_id,
        gates=1,
        max_minutes=scope.hold.max_minutes,
    )
    if inputs.threat_mass < c.threat_mass_min:
        return "threat_mass_below_hold_min", granted
    if not inputs.time_to_nearest_critical_asset_s < c.time_to_critical_asset_max_s:
        return "time_to_critical_asset_not_below_hold_max", granted
    if inputs.zone_criticality < c.zone_criticality_min:
        return "zone_criticality_below_hold_min", granted
    if inputs.active_holds > c.active_holds_max:
        return "active_holds_above_max", granted
    return None, granted


def slow_permitted(inputs: GuardInputs, criteria: LadderCriteria) -> GuardDecision:
    """Signed orders cover ``slow`` in this basin and the slow criteria are met.

    ``threat_mass >= slow.threat_mass_min`` and ``time < slow.time_to_critical_asset_max_s``.
    The permit grants one basin for the orders' ``max_minutes``.
    """
    problem, granted = _slow_problem(inputs, criteria)
    if problem is not None:
        return _decide(GuardName.SLOW_PERMITTED, inputs, False, problem)
    return _decide(GuardName.SLOW_PERMITTED, inputs, True, "slow_criteria_met", granted)


def hold_permitted(inputs: GuardInputs, criteria: LadderCriteria) -> GuardDecision:
    """Signed orders cover ``hold`` and the hold criteria are met.

    ``threat_mass >= min``, ``time < max``, ``zone_criticality >= min`` and
    ``active_holds <= active_holds_max``. The permit grants one gate or berth (the nearest
    asset) for the orders' ``max_minutes``.
    """
    problem, granted = _hold_problem(inputs, criteria)
    if problem is not None:
        return _decide(GuardName.HOLD_PERMITTED, inputs, False, problem)
    return _decide(GuardName.HOLD_PERMITTED, inputs, True, "hold_criteria_met", granted)


def nothing_preauthorized(inputs: GuardInputs, criteria: LadderCriteria) -> GuardDecision:
    """Permits iff neither ``slow_permitted`` nor ``hold_permitted`` permits (literal reading)."""
    slow = slow_permitted(inputs, criteria)
    hold = hold_permitted(inputs, criteria)
    if slow.permits or hold.permits:
        which = "slow" if slow.permits else "hold"
        return _decide(GuardName.NOTHING_PREAUTHORIZED, inputs, False, f"{which}_preauthorized")
    reason = UNSIGNED if _scope(inputs) is None else "no_rung_preauthorized"
    return _decide(GuardName.NOTHING_PREAUTHORIZED, inputs, True, reason)


def _family(action: str, criteria: LadderCriteria) -> GuardName | None:
    if action == criteria.hold.action or action in criteria.hold.wider_actions:
        return GuardName.HOLD_PERMITTED
    if action == criteria.slow.action or action in criteria.slow.wider_actions:
        return GuardName.SLOW_PERMITTED
    return None


def _excess(inputs: GuardInputs, criteria: LadderCriteria, req: ActionScope) -> str | None:
    """Why ``req`` exceeds the standing scope in force, or ``None`` if it is within it."""
    family = _family(req.action, criteria)
    scope = _scope(inputs)
    if family is None:
        return "not_a_standing_orders_action"
    if scope is None:
        return "no_standing_orders"
    if req.action in criteria.hold.wider_actions or req.action in criteria.slow.wider_actions:
        return f"person_approves:{req.action}"
    if req.max_minutes is None:
        return "unbounded_minutes"
    if family is GuardName.HOLD_PERMITTED:
        if scope.hold is None:
            return "hold_not_in_standing_scope"
        if inputs.active_holds is None:
            return "unknown_input:active_holds"
        if inputs.active_holds > criteria.hold.active_holds_max:
            return "second_hold"
        if req.gates > scope.hold.max_gates:
            return "gates"
        if req.max_minutes > scope.hold.max_minutes:
            return "minutes"
        return None
    if scope.slow is None:
        return "slow_not_in_standing_scope"
    if req.basin is None or req.basin not in scope.slow.basins:
        return "basin"
    if req.max_minutes > scope.slow.max_minutes:
        return "minutes"
    return None


def scope_exceeded(inputs: GuardInputs, criteria: LadderCriteria) -> GuardDecision:
    """Do the standing orders cover ``inputs.requested``? BLOCK means scope exceeded.

    The decision is the rung's standing-orders guard (``hold_permitted`` for the hold
    family, ``slow_permitted`` otherwise) re-checked against the requested scope: it blocks
    with ``scope_exceeded:<why>`` for a second hold, more gates, more minutes, another
    basin, a ``person_approves`` action, no signed orders, or a missing request. The driver
    emits the ``scope_exceeded`` event while HOLDING when this blocks (``exceeds_scope``).
    """
    req = inputs.requested
    if req is None:
        return _decide(GuardName.HOLD_PERMITTED, inputs, False, "unknown_input:requested")
    guard = _family(req.action, criteria) or GuardName.HOLD_PERMITTED
    why = _excess(inputs, criteria, req)
    if why is not None:
        return _decide(guard, inputs, False, f"scope_exceeded:{why}")
    return _decide(guard, inputs, True, "within_standing_scope", req)


def exceeds_scope(inputs: GuardInputs, criteria: LadderCriteria) -> bool:
    """True iff ``scope_exceeded`` blocks: the driver then emits ``scope_exceeded``."""
    return not scope_exceeded(inputs, criteria).permits


def approval_token_valid_and_scope_matches(
    inputs: GuardInputs, criteria: LadderCriteria
) -> GuardDecision:
    """A valid token for this contact whose scope equals the request, unexpired, not never."""
    g = GuardName.APPROVAL_TOKEN_VALID_AND_SCOPE_MATCHES
    if inputs.approval_token is None:
        return _decide(g, inputs, False, "unknown_input:approval_token")
    if inputs.requested is None:
        return _decide(g, inputs, False, "unknown_input:requested")
    if inputs.requested.action in ABSOLUTE_NEVER:
        return _decide(g, inputs, False, f"never:{inputs.requested.action}")
    problem = token_problem(
        inputs.approval_token,
        contact_id=inputs.contact_id,
        requested=inputs.requested,
        now_s=inputs.now_s,
    )
    if problem is not None:
        return _decide(g, inputs, False, problem)
    return _decide(g, inputs, True, "token_valid_scope_matches", inputs.requested)


def _backup_channel(inputs: GuardInputs) -> str | None:
    scope = _scope(inputs)
    return None if scope is None else scope.backup_channel


def backup_exists(inputs: GuardInputs, criteria: LadderCriteria) -> GuardDecision:
    """Signed orders name a backup approver who has not been paged yet for this request."""
    g = GuardName.BACKUP_EXISTS
    if _backup_channel(inputs) is None:
        return _decide(g, inputs, False, UNSIGNED if _scope(inputs) is None else "no_backup")
    if inputs.backup_paged:
        return _decide(g, inputs, False, "backup_already_paged")
    return _decide(g, inputs, True, "backup_available")


def no_backup(inputs: GuardInputs, criteria: LadderCriteria) -> GuardDecision:
    """The complement of ``backup_exists``: release the hold at its limit, flag "unreachable"."""
    exists = backup_exists(inputs, criteria)
    if exists.permits:
        return _decide(GuardName.NO_BACKUP, inputs, False, "backup_available")
    return _decide(GuardName.NO_BACKUP, inputs, True, "unreachable")


class BoundaryDecision(StrictModel):
    """The boundary table's verdict on one action for one set of inputs."""

    action: str
    cell: BoundaryCell | None
    verdict: GuardVerdict
    reason: str
    inputs_hash: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    granted_scope: ActionScope | None = None

    @property
    def permits(self) -> bool:
        return self.verdict is GuardVerdict.PERMIT


def boundary_decision(action: str, inputs: GuardInputs, table: BoundaryTable) -> BoundaryDecision:
    """May the platform execute ``action`` now? Table-driven over ``config/ladder.yaml``.

    - ``never``: blocks, always (even with a valid token, signed orders, any mode).
    - ``platform_alone``: permits, always.
    - ``standing_orders``: permits only when signed, in scope and the criteria are met;
      a request for this action must also sit within the standing scope.
    - ``person_approves``: permits only with a valid token matching a request for it.
    - an action the table does not name blocks.
    """
    h = inputs_hash(inputs)
    if action in ABSOLUTE_NEVER:
        return BoundaryDecision(
            action=action,
            cell=BoundaryCell.NEVER,
            verdict=GuardVerdict.BLOCK,
            reason=f"never:{action}",
            inputs_hash=h,
        )
    if action not in table:
        return BoundaryDecision(
            action=action,
            cell=None,
            verdict=GuardVerdict.BLOCK,
            reason="unknown_action",
            inputs_hash=h,
        )
    cell = table.cell_of(action)
    crit = table.criteria
    decision: GuardDecision
    if cell is BoundaryCell.NEVER:
        return BoundaryDecision(
            action=action,
            cell=cell,
            verdict=GuardVerdict.BLOCK,
            reason=f"never:{action}",
            inputs_hash=h,
        )
    if cell is BoundaryCell.PLATFORM_ALONE:
        return BoundaryDecision(
            action=action,
            cell=cell,
            verdict=GuardVerdict.PERMIT,
            reason="platform_alone",
            inputs_hash=h,
        )
    if cell is BoundaryCell.STANDING_ORDERS:
        if action == crit.hold.action:
            decision = hold_permitted(inputs, crit)
        elif action == crit.slow.action:
            decision = slow_permitted(inputs, crit)
        else:
            return BoundaryDecision(
                action=action,
                cell=cell,
                verdict=GuardVerdict.BLOCK,
                reason="no_standing_orders_guard",
                inputs_hash=h,
            )
        if decision.permits and inputs.requested is not None:
            if inputs.requested.action != action:
                decision = _decide(decision.guard, inputs, False, "request_names_other_action")
            else:
                decision = scope_exceeded(inputs, crit)
    else:
        decision = approval_token_valid_and_scope_matches(inputs, crit)
        if decision.permits and inputs.requested is not None and inputs.requested.action != action:
            decision = _decide(decision.guard, inputs, False, "token_for_other_action")
    return BoundaryDecision(
        action=action,
        cell=cell,
        verdict=decision.verdict,
        reason=decision.reason,
        inputs_hash=h,
        granted_scope=decision.granted_scope,
    )


# --- WO-083 begin: separation ---


SeparationId = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$")]
"""A vessel id (``ais-01``) or a plan id (``P-BT01-001``): patterned, never free text."""


class SeparationFacts(StrictModel):
    """The geometry one separation slow advisory rests on (SPEC-LADDER sections 3 and 8).

    ``own`` is the surface vessel (from its AIS-style report), ``other`` the in-water filed
    job (from its fitted track). Positions are local metres (x east, y north) at the same
    instant, velocities m/s. ``cpa_m``/``tcpa_s`` are ``cpa_tcpa`` of exactly these values;
    the thresholds come from ``config/advisories.yaml``. No field holds free text, so no
    model output can reach this guard (CLAUDE.md 2.2).
    """

    vessel_id: SeparationId
    plan_id: SeparationId
    own_x_m: float
    own_y_m: float
    own_vx_mps: float
    own_vy_mps: float
    other_x_m: float
    other_y_m: float
    other_vx_mps: float
    other_vy_mps: float
    velocity_source: Literal["window", "kalman"] = "window"
    cpa_m: Annotated[float, Field(ge=0.0)]
    tcpa_s: float
    separation_m: Annotated[float, Field(gt=0.0)]
    advisory_window_s: Annotated[float, Field(gt=0.0)]

    @field_validator(
        "own_x_m",
        "own_y_m",
        "own_vx_mps",
        "own_vy_mps",
        "other_x_m",
        "other_y_m",
        "other_vx_mps",
        "other_vy_mps",
        "cpa_m",
        "tcpa_s",
        "separation_m",
        "advisory_window_s",
        mode="after",
    )
    @classmethod
    def _round(cls, v: float) -> float:
        return round6(v)


def separation_inputs_hash(inputs: GuardInputs, facts: SeparationFacts) -> str:
    """``stable_hash`` over ``{"guard_inputs": inputs, "separation": facts}``.

    The same mapping is the audit row's ``inputs`` (WO-042 ``guard_decision_payload``), so
    ``stable_hash(row.payload["inputs"]) == decision.inputs_hash``.
    """
    return stable_hash({"guard_inputs": inputs, "separation": facts})


def separation_slow_permitted(
    inputs: GuardInputs, criteria: LadderCriteria, facts: SeparationFacts
) -> GuardDecision:
    """May the platform propose a separation slow advisory for ``inputs.basin``?

    The slow rung's standing-orders checks (``_slow_scope_problem``: signed and unexpired
    orders, ``slow`` in scope, degraded-mode narrowing, basin in ``scope.slow.basins``) with
    CPA/TCPA in place of the threat_mass and time criteria (DECISIONS 2026-10-17 06:37):
    ``cpa_m < separation_m`` and ``0 < tcpa_s < advisory_window_s``, strict on every edge.
    The granted scope (one basin, the orders' ``max_minutes``) is then re-checked with
    ``scope_exceeded``. The decision is ``GuardName.SLOW_PERMITTED``.
    """
    g = GuardName.SLOW_PERMITTED
    h = separation_inputs_hash(inputs, facts)

    def block(reason: str) -> GuardDecision:
        return GuardDecision(guard=g, verdict=GuardVerdict.BLOCK, reason=reason, inputs_hash=h)

    problem, scope = _slow_scope_problem(inputs)
    if problem is not None or scope is None or scope.slow is None:
        return block(problem or UNSIGNED)
    if not facts.cpa_m < facts.separation_m:
        return block("cpa_not_below_separation_m")
    if not 0.0 < facts.tcpa_s < facts.advisory_window_s:
        return block("tcpa_outside_advisory_window")
    granted = ActionScope(
        action=criteria.slow.action, basin=inputs.basin, max_minutes=scope.slow.max_minutes
    )
    check = scope_exceeded(inputs.model_copy(update={"requested": granted}), criteria)
    if not check.permits:
        return block(check.reason)
    return GuardDecision(
        guard=g,
        verdict=GuardVerdict.PERMIT,
        reason="separation_criteria_met",
        inputs_hash=h,
        granted_scope=granted,
    )


# --- WO-083 end ---


# --- the GuardPort adapter -----------------------------------------------------------------


@dataclass(frozen=True)
class ApproverStatus:
    """Who can be reached right now, and the network. Feeds ``degraded_reasons``."""

    primary_reachable: bool = True
    backup_reachable: bool = True
    network_up: bool = True

    @classmethod
    def from_toggles(cls, toggles: Iterable[ToggleName | str]) -> ApproverStatus:
        """``approver_unreachable``: nobody answers; ``network_down``: the primary is silent."""
        names = {ToggleName(t) for t in toggles}
        if ToggleName.APPROVER_UNREACHABLE in names:
            return cls(primary_reachable=False, backup_reachable=False, network_up=True)
        if ToggleName.NETWORK_DOWN in names:
            return cls(primary_reachable=False, backup_reachable=True, network_up=False)
        return cls()

    @property
    def reachable(self) -> bool:
        return self.primary_reachable or self.backup_reachable

    def degraded_reasons(self) -> tuple[ToggleName, ...]:
        out: set[ToggleName] = set()
        if not self.network_up:
            out.add(ToggleName.NETWORK_DOWN)
        if not self.reachable:
            out.add(ToggleName.APPROVER_UNREACHABLE)
        return tuple(sorted(out))


_PURE = {
    GuardName.SLOW_PERMITTED: slow_permitted,
    GuardName.HOLD_PERMITTED: hold_permitted,
    GuardName.NOTHING_PREAUTHORIZED: nothing_preauthorized,
    GuardName.APPROVAL_TOKEN_VALID_AND_SCOPE_MATCHES: approval_token_valid_and_scope_matches,
    GuardName.BACKUP_EXISTS: backup_exists,
    GuardName.NO_BACKUP: no_backup,
}


class BoundaryGuards:
    """WO-040's ``GuardPort`` over the boundary table and the verified standing orders.

    ``epoch``, when given, is the wall-time instant of run-clock zero (``scenario_now``):
    the orders' validity window is then re-checked at ``epoch + now_s`` on every decision,
    so orders that expire mid-run stop pre-authorizing at that instant.
    """

    def __init__(
        self,
        standing: StandingOrdersStatus,
        boundary: BoundaryTable,
        approver_status: ApproverStatus | None = None,
        *,
        epoch: dt.datetime | None = None,
    ) -> None:
        self.standing = standing
        self.boundary = boundary
        self.approver_status = approver_status or ApproverStatus()
        self.epoch = epoch

    def status_at(self, now_s: float) -> StandingOrdersStatus:
        if self.epoch is None:
            return self.standing
        return self.standing.at(self.epoch + dt.timedelta(seconds=now_s))

    def inputs(self, ctx: GuardContext) -> GuardInputs:
        """``GuardInputs`` from a ``GuardContext``. A malformed token reads as no token."""
        status = self.status_at(ctx.now_s)
        reasons = self.approver_status.degraded_reasons()
        return GuardInputs(
            contact_id=ctx.contact_id,
            now_s=ctx.now_s,
            threat_mass=ctx.threat_mass,
            benign_mass=ctx.benign_mass,
            time_to_nearest_critical_asset_s=ctx.time_to_nearest_critical_asset_s,
            zone_criticality=ctx.zone_criticality,
            standing_orders_signed=status.signed,
            standing_orders_scope=effective_scope(status, reasons),
            active_holds=ctx.active_holds,
            approver_reachable=self.approver_status.reachable,
            approval_token=parse_token(ctx.approval_token),
            requested=ctx.requested_scope,
            basin=ctx.basin,
            nearest_asset_id=ctx.nearest_asset_id,
            degraded_reasons=reasons,
            backup_paged=ctx.backup_paged,
        )

    # --- WO-042 audit semantics ---
    observer: GuardObserver | None = None
    """Told the inputs of every decision (the audit's guard rows); never changes a verdict."""

    def decide(self, name: GuardName, ctx: GuardContext) -> GuardDecision:
        if name in EVIDENCE_GUARDS:
            decision = evidence_guard(name, ctx)
            if self.observer is not None:
                self.observer(decision.inputs_hash, evidence_guard_read(ctx))
            return decision
        inputs = self.inputs(ctx)
        decision = _PURE[name](inputs, self.boundary.criteria)
        if self.observer is not None:
            self.observer(decision.inputs_hash, guard_inputs_read(inputs))
        return decision

    # --- WO-042 audit semantics end ---

    # --- WO-083 begin: separation ---
    def separation_decision(
        self, facts: SeparationFacts, *, contact_id: str, basin: str, now_s: float
    ) -> tuple[GuardDecision, GuardInputs]:
        """``separation_slow_permitted`` for one advisory; the orders are re-checked at
        ``now_s`` (``status_at``), so an expired epoch blocks, and the degraded reasons
        narrow the scope through ``effective_scope``. Returns the decision and its inputs.
        """
        status = self.status_at(now_s)
        reasons = self.approver_status.degraded_reasons()
        inputs = GuardInputs(
            contact_id=contact_id,
            now_s=now_s,
            standing_orders_signed=status.signed,
            standing_orders_scope=effective_scope(status, reasons),
            approver_reachable=self.approver_status.reachable,
            basin=basin,
            degraded_reasons=reasons,
        )
        return separation_slow_permitted(inputs, self.boundary.criteria, facts), inputs

    # --- WO-083 end ---


# --- WO-042 audit semantics ---
GuardObserver = Callable[[str, dict[str, Any]], None]


def guard_inputs_read(inputs: GuardInputs) -> dict[str, Any]:
    """``GuardInputs`` as the audit stores it: the approval token becomes its hash."""
    dumped: dict[str, Any] = inputs.model_dump(mode="json")
    token = inputs.approval_token
    dumped["approval_token"] = None if token is None else {"token_hash": token_hash(token)}
    return dumped


def evidence_guard_read(ctx: GuardContext) -> dict[str, Any]:
    """What an evidence guard reads: the event and its rows for this contact (ids only)."""
    rows = sorted(
        (r for r in ctx.event.payload.rows if r.contact_id == ctx.contact_id),
        key=lambda r: (r.t, r.row_id),
    )
    return {
        "contact_id": ctx.contact_id,
        "state": ctx.state.value,
        "event": ctx.event.kind.value,
        "arg": ctx.event.arg,
        "tick": ctx.event.tick,
        "evidence_row_ids": [r.row_id for r in rows],
    }


# --- WO-042 audit semantics end ---
