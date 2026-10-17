"""The deterministic probe planner (docs/SPEC-INVESTIGATION.md section 6).

For every allowlisted probe the contact's rung allows, the budget allows and that has not
already run in this tick bucket, the planner estimates the expected information gain
(expected entropy reduction of the posterior under the likelihood tables) and divides it
by the probe's cost::

    score = gain / max(DENOM_FLOOR,
                       cost_usd + latency_cost_weight_per_s * latency_s
                                + interruption_cost_weight * interruption)

and picks the highest score; ties break by allowlist order, and a probe whose gain is at
most ``GAIN_EPS`` is never chosen. The two weights come from ``config/ladder.yaml``
``costs`` and the three limits from ``budgets``; nothing here is hardcoded.

Budget scope (DECISIONS 2026-10-16 23:05, WO-033 addenda): ``probes_per_contact`` counts
the probes the planner chose during INVESTIGATING; the fixed watch sweep does not count.
Every probe with ``cost_usd > 0`` counts toward ``paid_probes_per_contact``, including the
ask-rung ``acoustic_hail`` runner v1 already sent (``BudgetLedger.record_ask_hail``).
Interruptions count ``ProbeSpec.interruption`` (``request_watch_officer_look``).

The planner only ranks typed ``ProbeId`` values from ``ALLOWLIST``; it never names a rung
or an action, and nothing it returns is a control (CLAUDE.md 2.1, 2.2). ``PlannerChoice``
carries every field WO-032's LLM planner sets (``source``, ``fallback_reason``, ``why``,
``question_for_human``), so that WO never edits this file. Pure and deterministic.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, Literal, Protocol

from pydantic import StrictStr

from soundings.canonical import round6
from soundings.investigation.evidence import EvidenceTable
from soundings.investigation.posterior import (
    LikelihoodTables,
    Posterior,
    PriorConfig,
    expected_entropy_after,
    score_contact,
)
from soundings.investigation.probes import (
    ALLOWLIST,
    ProbeSpec,
    as_probe_id,
    idempotency_key,
    rung_index,
    tick_bucket,
)
from soundings.schemas._base import NonNegFloat, NonNegInt, StrictModel
from soundings.schemas.config import LADDER_PATH, load_ladder_config
from soundings.schemas.denylist import FreeText
from soundings.schemas.enums import FeatureName, ProbeId, Rung
from soundings.schemas.evidence import UNKNOWN

DENOM_FLOOR: Final = 1e-3
"""Keeps the score of a free, zero-latency probe finite."""
GAIN_EPS: Final = 1e-9
"""A probe whose expected gain is at most this is never chosen."""
DEFAULT_TICK_S: Final = 2.0
"""The catalog's tick length; ``PlannerState.tick_s`` defaults to it."""

REASON_MAX_SCORE: Final = "max_gain_per_cost"
REASON_NO_PROBE: Final = "no_informative_probe_in_budget"

PlannerSource = Literal["deterministic", "llm", "fallback"]


# --- config ------------------------------------------------------------------------------


class BudgetLimits(StrictModel):
    """``config/ladder.yaml`` ``budgets`` (per contact)."""

    probes_per_contact: NonNegInt
    paid_probes_per_contact: NonNegInt
    human_interruptions_per_contact: NonNegInt


def load_budget_limits(path: str | Path = LADDER_PATH) -> BudgetLimits:
    """The three probe budgets from ``config/ladder.yaml`` (KeyError if one is missing)."""
    budgets = load_ladder_config(path).budgets
    return BudgetLimits(
        probes_per_contact=budgets["probes_per_contact"],
        paid_probes_per_contact=budgets["paid_probes_per_contact"],
        human_interruptions_per_contact=budgets["human_interruptions_per_contact"],
    )


class CostWeights(StrictModel):
    """``config/ladder.yaml`` ``costs``: lambda (USD per second) and mu (USD per interruption)."""

    latency_cost_weight_per_s: NonNegFloat
    interruption_cost_weight: NonNegFloat


def load_cost_weights(path: str | Path = LADDER_PATH) -> CostWeights:
    """The two planner weights from ``config/ladder.yaml`` ``costs``."""
    costs = load_ladder_config(path).costs
    return CostWeights(
        latency_cost_weight_per_s=float(costs["latency_cost_weight_per_s"]),
        interruption_cost_weight=float(costs["interruption_cost_weight"]),
    )


# --- the budget ledger -------------------------------------------------------------------


class BudgetExceeded(ValueError):
    """Recording this planner-chosen probe would push a counter past its limit."""


class BudgetCounts(StrictModel):
    """One contact's counters."""

    probes: NonNegInt = 0
    paid: NonNegInt = 0
    interruptions: NonNegInt = 0


class BudgetLedger:
    """Per-contact probe counters against ``BudgetLimits``.

    ``record`` mutates the ledger and returns it, so both ``ledger.record(...)`` and
    ``ledger = ledger.record(...)`` work. ``copy`` gives an independent ledger.
    """

    def __init__(self, limits: BudgetLimits | None = None) -> None:
        self.limits: BudgetLimits = limits if limits is not None else load_budget_limits()
        self._counts: dict[str, BudgetCounts] = {}

    def counts(self, contact_id: str) -> BudgetCounts:
        return self._counts.get(contact_id, BudgetCounts())

    def remaining(self, contact_id: str) -> BudgetCounts:
        """What is left of each budget (never negative)."""
        c, lim = self.counts(contact_id), self.limits
        return BudgetCounts(
            probes=max(0, lim.probes_per_contact - c.probes),
            paid=max(0, lim.paid_probes_per_contact - c.paid),
            interruptions=max(0, lim.human_interruptions_per_contact - c.interruptions),
        )

    def allows(self, contact_id: str, probe_id: ProbeId | str) -> bool:
        """True when choosing this probe keeps all three counters within their limits."""
        spec = ALLOWLIST[as_probe_id(probe_id)]
        c, lim = self.counts(contact_id), self.limits
        if c.probes + 1 > lim.probes_per_contact:
            return False
        if spec.paid and c.paid + 1 > lim.paid_probes_per_contact:
            return False
        return c.interruptions + spec.interruption <= lim.human_interruptions_per_contact

    def record(
        self, contact_id: str, probe_id: ProbeId | str, *, planner_chosen: bool = True
    ) -> BudgetLedger:
        """Count one probe. A planner-chosen probe that does not fit raises
        ``BudgetExceeded`` and changes nothing; ``planner_chosen=False`` (a probe the
        runner sent itself) counts toward paid and interruptions only, never raises."""
        pid = as_probe_id(probe_id)
        spec = ALLOWLIST[pid]
        if planner_chosen and not self.allows(contact_id, pid):
            raise BudgetExceeded(f"{contact_id}: {pid.value} does not fit the remaining budget")
        c = self.counts(contact_id)
        self._counts[contact_id] = BudgetCounts(
            probes=c.probes + (1 if planner_chosen else 0),
            paid=c.paid + (1 if spec.paid else 0),
            interruptions=c.interruptions + spec.interruption,
        )
        return self

    def record_ask_hail(self, contact_id: str) -> BudgetLedger:
        """The ask-rung ``acoustic_hail`` runner v1 sent: a paid probe, not a planner pick."""
        return self.record(contact_id, ProbeId.ACOUSTIC_HAIL, planner_chosen=False)

    def copy(self) -> BudgetLedger:
        other = BudgetLedger(self.limits)
        other._counts = dict(self._counts)
        return other

    def to_dict(self) -> dict[str, dict[str, int]]:
        """``contact_id -> counters``, contacts sorted."""
        return {cid: self._counts[cid].model_dump() for cid in sorted(self._counts)}


# --- information gain and score ----------------------------------------------------------


def expected_information_gain(
    posterior: Posterior,
    spec: ProbeSpec,
    observed: Mapping[FeatureName, float],
    tables: LikelihoodTables,
    *,
    probe_confidence: float | None = None,
) -> float:
    """Sum over the probe's likelihood features f of ``H(post) - E[H(post | f)]``.

    ``E[H(post | f)]`` is WO-030 ``expected_entropy_after(post, f, tables, delta_c)`` with
    ``delta_c = max(0, c_probe - observed[f])``: only the confidence the probe would add
    beyond what the table already holds counts (a feature with no row has observed 0).
    ``c_probe`` is ``probe_confidence`` or ``spec.nominal_confidence``. Features with no
    likelihood table add 0.

    Approximation: features are treated independently (a per-feature sum, not the joint
    expectation over all of a probe's features). A tempered update can make a term
    slightly negative; each term is floored at 0. Unrounded; round at output only.
    """
    c_probe = spec.nominal_confidence if probe_confidence is None else float(probe_confidence)
    h_now = posterior.entropy()
    terms: list[float] = []
    for feature in spec.returns:
        if not tables.has(feature):
            continue
        delta_c = max(0.0, c_probe - float(observed.get(feature, 0.0)))
        if delta_c <= 0.0:
            continue
        after = expected_entropy_after(posterior, feature, tables, min(delta_c, 1.0))
        terms.append(max(0.0, h_now - after))
    return math.fsum(terms)


def probe_cost(spec: ProbeSpec, weights: CostWeights) -> float:
    """``cost_usd + lambda * latency_s + mu * interruption`` (before the floor)."""
    return (
        spec.cost_usd
        + weights.latency_cost_weight_per_s * spec.latency_s
        + weights.interruption_cost_weight * spec.interruption
    )


def probe_score(gain: float, spec: ProbeSpec, weights: CostWeights) -> float:
    """``gain / max(DENOM_FLOOR, probe_cost(spec, weights))``."""
    return gain / max(DENOM_FLOOR, probe_cost(spec, weights))


# --- state and choice --------------------------------------------------------------------


@dataclass(frozen=True)
class PlannerState:
    """What the planner sees for one contact at one tick.

    ``observed_confidence`` maps a likelihood feature to the confidence of the row the
    posterior reads for it (0 or absent when unknown or missing; see
    ``observed_confidence``). ``run_keys`` holds the idempotency keys of probes already
    run; a probe whose argument-free key for the current tick bucket is in it is skipped.
    ``probe_confidence`` overrides ``ProbeSpec.nominal_confidence`` per probe (for
    example the contact's track confidence for the watch re-reads).
    """

    contact_id: str
    posterior: Posterior
    observed_confidence: Mapping[FeatureName, float]
    current_rung: Rung
    ledger: BudgetLedger
    tick: int
    run_keys: frozenset[str] = frozenset()
    tick_s: float = DEFAULT_TICK_S
    probe_confidence: Mapping[ProbeId, float] = field(default_factory=dict)

    def key_for(self, probe_id: ProbeId) -> str:
        """The argument-free idempotency key of ``probe_id`` in this tick bucket."""
        return idempotency_key(
            self.contact_id, probe_id.value, None, tick_bucket(self.tick, self.tick_s)
        )

    def already_ran(self, probe_id: ProbeId) -> bool:
        return self.key_for(probe_id) in self.run_keys


class RankedProbe(StrictModel):
    """One candidate in a ``PlannerChoice.ranked`` list (6 dp)."""

    probe_id: ProbeId
    gain: NonNegFloat
    score: NonNegFloat


class PlannerChoice(StrictModel):
    """The planner's pick, or ``probe_id=None`` with ``reason`` when nothing qualifies.

    ``ranked`` lists every eligible candidate (rung, budget and not yet run), best first.
    ``source``/``fallback_reason``/``why``/``question_for_human`` are set by WO-032's LLM
    planner; the deterministic planner sets ``source="deterministic"`` and a ``why``.
    """

    probe_id: ProbeId | None
    gain: NonNegFloat = 0.0
    score: NonNegFloat = 0.0
    ranked: tuple[RankedProbe, ...] = ()
    reason: StrictStr
    source: PlannerSource = "deterministic"
    fallback_reason: StrictStr | None = None
    why: FreeText | None = None
    question_for_human: FreeText | None = None


class Planner(Protocol):
    """Anything that picks the next probe (deterministic here, LLM in WO-032)."""

    def choose(self, state: PlannerState, tables: LikelihoodTables) -> PlannerChoice: ...


def _why(probe_id: ProbeId, gain: float, score: float) -> str:
    return (
        f"{probe_id.value}: highest expected information gain per unit cost "
        f"({round6(gain)} bits, score {round6(score)})"
    )


class DeterministicPlanner:
    """Max expected-information-gain-per-cost over the allowlist (SPEC section 6)."""

    def __init__(self, weights: CostWeights | None = None) -> None:
        self.weights: CostWeights = weights if weights is not None else load_cost_weights()

    def candidates(self, state: PlannerState, tables: LikelihoodTables) -> list[RankedProbe]:
        """Eligible probes with unrounded gain and score, best first (ties: allowlist order)."""
        order = {pid: i for i, pid in enumerate(ALLOWLIST)}
        scored: list[tuple[float, int, ProbeId, float]] = []
        current = rung_index(state.current_rung)
        for pid, spec in ALLOWLIST.items():
            if rung_index(spec.rung_required) > current:
                continue
            if not state.ledger.allows(state.contact_id, pid):
                continue
            if state.already_ran(pid):
                continue
            gain = expected_information_gain(
                state.posterior,
                spec,
                state.observed_confidence,
                tables,
                probe_confidence=state.probe_confidence.get(pid),
            )
            scored.append((probe_score(gain, spec, self.weights), order[pid], pid, gain))
        scored.sort(key=lambda s: (-s[0], s[1]))
        return [RankedProbe(probe_id=pid, gain=gain, score=score) for score, _, pid, gain in scored]

    def choose(self, state: PlannerState, tables: LikelihoodTables) -> PlannerChoice:
        """The best eligible probe with gain > ``GAIN_EPS``, or ``probe_id=None``."""
        cands = self.candidates(state, tables)
        ranked = tuple(
            RankedProbe(probe_id=c.probe_id, gain=round6(c.gain), score=round6(c.score))
            for c in cands
        )
        for c in cands:
            if c.gain > GAIN_EPS:
                return PlannerChoice(
                    probe_id=c.probe_id,
                    gain=round6(c.gain),
                    score=round6(c.score),
                    ranked=ranked,
                    reason=REASON_MAX_SCORE,
                    why=_why(c.probe_id, c.gain, c.score),
                )
        return PlannerChoice(probe_id=None, ranked=ranked, reason=REASON_NO_PROBE)


# --- building a state from the evidence table --------------------------------------------


def observed_confidence(
    table: EvidenceTable, contact_id: str, tables: LikelihoodTables
) -> dict[FeatureName, float]:
    """Per likelihood feature, the confidence of the row the posterior reads
    (``EvidenceTable.latest``); 0.0 when the row is missing, ``unknown`` or carries an
    ``unknown`` confidence."""
    out: dict[FeatureName, float] = {}
    for feature in tables.features():
        row = table.latest(contact_id, feature)
        if row is None or (isinstance(row.value, str) and row.value == UNKNOWN):
            out[feature] = 0.0
        elif isinstance(row.confidence, float):
            out[feature] = float(row.confidence)
        else:
            out[feature] = 0.0
    return out


def state_from_table(
    table: EvidenceTable,
    contact_id: str,
    tables: LikelihoodTables,
    prior_cfg: PriorConfig,
    *,
    current_rung: Rung,
    ledger: BudgetLedger,
    tick: int,
    tick_s: float = DEFAULT_TICK_S,
    run_keys: frozenset[str] = frozenset(),
    probe_confidence: Mapping[ProbeId, float] | None = None,
) -> PlannerState:
    """A ``PlannerState`` from the table: WO-030 ``score_contact`` posterior plus
    ``observed_confidence``."""
    report = score_contact(table, contact_id, tables, prior_cfg)
    return PlannerState(
        contact_id=contact_id,
        posterior=report.posterior,
        observed_confidence=observed_confidence(table, contact_id, tables),
        current_rung=current_rung,
        ledger=ledger,
        tick=tick,
        run_keys=run_keys,
        tick_s=tick_s,
        probe_confidence=dict(probe_confidence or {}),
    )


__all__ = [
    "DENOM_FLOOR",
    "GAIN_EPS",
    "REASON_MAX_SCORE",
    "REASON_NO_PROBE",
    "BudgetCounts",
    "BudgetExceeded",
    "BudgetLedger",
    "BudgetLimits",
    "CostWeights",
    "DeterministicPlanner",
    "Planner",
    "PlannerChoice",
    "PlannerSource",
    "PlannerState",
    "RankedProbe",
    "expected_information_gain",
    "load_budget_limits",
    "load_cost_weights",
    "observed_confidence",
    "probe_cost",
    "probe_score",
    "state_from_table",
]
