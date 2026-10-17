"""The guard port, the three evidence guards, and the unsigned-orders guard set.

CLAUDE.md 2.2: a guard is deterministic code over the typed ``GuardContext``. The evidence
guards read only the evidence rows the event payload carries (feature, value, probe id);
they never read free text. WO-041 plugs the standing-orders and approval guards in behind
the same ``GuardPort``.
"""

from __future__ import annotations

from typing import Protocol

from soundings.canonical import stable_hash
from soundings.schemas import EvidenceRow, FeatureName, HailReply, ProbeId, RegistryMatch

from .types import (
    EVIDENCE_GUARDS,
    GuardContext,
    GuardDecision,
    GuardName,
    GuardVerdict,
)

FREE_PROBES: tuple[ProbeId, ...] = (
    ProbeId.REGISTRY_LOOKUP,
    ProbeId.KINEMATICS_PROFILE,
    ProbeId.DRIFT_CHECK,
    ProbeId.PATTERN_OF_LIFE_CHECK,
    ProbeId.AIS_SURFACE_CORRELATE,
    ProbeId.ACOUSTIC_FEATURE_READ,
)
"""The watch-rung, zero-cost probes of SPEC-INVESTIGATION section 5."""

NO_SIGNED_ORDERS = "no_signed_standing_orders"


class GuardPort(Protocol):
    def decide(self, name: GuardName, ctx: GuardContext) -> GuardDecision: ...


def context_hash(ctx: GuardContext) -> str:
    """The ``inputs_hash`` every guard records: ``stable_hash`` of the whole context."""
    return stable_hash(ctx)


def _decision(name: GuardName, ctx: GuardContext, permit: bool, reason: str) -> GuardDecision:
    return GuardDecision(
        guard=name,
        verdict=GuardVerdict.PERMIT if permit else GuardVerdict.BLOCK,
        reason=reason,
        inputs_hash=context_hash(ctx),
    )


def _own_rows(ctx: GuardContext) -> list[EvidenceRow]:
    """The payload rows for this contact, in (t, row_id) order."""
    rows = [r for r in ctx.event.payload.rows if r.contact_id == ctx.contact_id]
    return sorted(rows, key=lambda r: (r.t, r.row_id))


def _latest_value(rows: list[EvidenceRow], feature: FeatureName) -> object | None:
    """The value of the latest row for ``feature``; ``None`` if absent or ``unknown``."""
    hits = [r for r in rows if r.feature is feature]
    if not hits:
        return None
    value = hits[-1].value
    return None if value == "unknown" else value


def evidence_guard(name: GuardName, ctx: GuardContext) -> GuardDecision:
    """Decide one of the three evidence guards from the event's evidence rows.

    - ``free_probes_complete``: permits iff a payload row for this contact comes from a
      free (watch-rung) probe.
    - ``registry_exact_and_hail_valid``: permits iff the latest ``registry_match`` row is
      ``exact`` and the latest ``hail_reply`` row is ``valid``.
    - ``not_cleared``: the negation of the previous guard over the same rows; a contact
      without positive registry and hail evidence is not cleared.
    """
    if name not in EVIDENCE_GUARDS:
        raise ValueError(f"{name.value} is not an evidence guard")
    rows = _own_rows(ctx)
    if name is GuardName.FREE_PROBES_COMPLETE:
        free = any(r.probe_id in FREE_PROBES for r in rows)
        return _decision(
            name, ctx, free, "free_probe_rows_present" if free else "no_free_probe_rows"
        )
    registry = _latest_value(rows, FeatureName.REGISTRY_MATCH)
    hail = _latest_value(rows, FeatureName.HAIL_REPLY)
    cleared = registry == RegistryMatch.EXACT.value and hail == HailReply.VALID.value
    if registry is None:
        why = "unknown_input:registry_match"
    elif hail is None:
        why = "unknown_input:hail_reply"
    elif cleared:
        why = "registry_exact_and_hail_valid"
    else:
        why = f"registry_{registry}_hail_{hail}"
    if name is GuardName.REGISTRY_EXACT_AND_HAIL_VALID:
        return _decision(name, ctx, cleared, why)
    return _decision(name, ctx, not cleared, why)


class NoStandingOrdersGuards:
    """The conservative port: real evidence guards, nothing pre-authorized, no approvals.

    This is the ``standing_orders.signed = false`` path: a handoff goes straight to
    HANDED_OFF, and every standing-order and approval guard blocks.
    """

    def decide(self, name: GuardName, ctx: GuardContext) -> GuardDecision:
        if name in EVIDENCE_GUARDS:
            return evidence_guard(name, ctx)
        if name is GuardName.NOTHING_PREAUTHORIZED:
            return _decision(name, ctx, True, NO_SIGNED_ORDERS)
        return _decision(name, ctx, False, NO_SIGNED_ORDERS)
