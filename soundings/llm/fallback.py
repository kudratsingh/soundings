"""Deterministic fallbacks for every model role (CLAUDE.md section 5).

The system runs end to end with ``MODEL_MODE=replay`` and zero fixtures: whenever a role
has no fixture or its reply is rejected by ``soundings.llm.guard``, the caller uses one
of these instead.

* Probe planner: ``rule_based_probe_order`` (cheapest permitted, affordable, not yet run
  allowlisted probe first) and ``fallback_probe_plan``, which wraps a probe choice in the
  planner's output schema. The investigation layer passes the deterministic planner's
  choice as ``probe``; with none given, the rule-based order decides.
* Hypothesis writer: ``template_hypotheses`` (the top three of the fixed eight by
  posterior, fixed ``why`` lines) and ``template_question`` (a fixed question per top-two
  pair, or ``None`` once the top two are well separated).
* SITREP writer: ``soundings.llm.sitrep_writer.template_sitrep`` (WO-042).

Nothing here reads a clock or an rng, and nothing here can add a hypothesis.
"""

from __future__ import annotations

from soundings.investigation.posterior import PosteriorReport
from soundings.llm.guard import (
    GuardContext,
    HypothesisEntry,
    HypothesisSetOutput,
    ProbePlanOutput,
)
from soundings.schemas.enums import Hypothesis, ProbeId

__all__ = [
    "GENERIC_QUESTION",
    "HYPOTHESIS_WHY",
    "PAIR_QUESTIONS",
    "QUESTION_MARGIN_MAX",
    "fallback_probe_plan",
    "rule_based_probe_order",
    "template_hypotheses",
    "template_question",
]

QUESTION_MARGIN_MAX = 0.5
"""``template_question`` returns ``None`` when ``top2_margin`` is at least this."""

TEMPLATE_HYPOTHESES_COUNT = 3

HYPOTHESIS_WHY: dict[Hypothesis, str] = {
    Hypothesis.FILED_ON_PLAN: "A filed plan covers this area and window.",
    Hypothesis.FILED_OFF_PLAN: "A filed vehicle may be outside its planned area or window.",
    Hypothesis.UNFILED_KNOWN_OPERATOR: "A known operator may be running without a filing.",
    Hypothesis.BIOLOGIC: "Track and acoustics fit marine life.",
    Hypothesis.DEBRIS_OR_DRIFT: "Motion follows the current like drifting debris.",
    Hypothesis.SURFACE_VESSEL_AIS_OFF: "A surface vessel with its transponder off may explain it.",
    Hypothesis.UNAUTHORIZED_UUV: "Powered, purposeful motion with no filing or valid hail reply.",
    Hypothesis.UNAUTHORIZED_DIVER: "Slow, shallow motion with breathing sounds and no filing.",
}
"""Fixed one-line reasons, one per hypothesis of the eight."""

GENERIC_QUESTION = "Can anyone on watch identify this contact or the operator running it?"

_H = Hypothesis
PAIR_QUESTIONS: dict[frozenset[Hypothesis], str] = {
    frozenset({_H.UNAUTHORIZED_UUV, _H.FILED_OFF_PLAN}): (
        "Is any operator running an unfiled vehicle in this basin tonight?"
    ),
    frozenset({_H.FILED_ON_PLAN, _H.FILED_OFF_PLAN}): (
        "Has the filed operator changed their survey area or window today?"
    ),
    frozenset({_H.UNAUTHORIZED_UUV, _H.UNFILED_KNOWN_OPERATOR}): (
        "Did a known operator launch a vehicle here without filing a plan?"
    ),
    frozenset({_H.FILED_OFF_PLAN, _H.UNFILED_KNOWN_OPERATOR}): (
        "Which operator owns the modem heard on this contact, and did they file?"
    ),
    frozenset({_H.BIOLOGIC, _H.DEBRIS_OR_DRIFT}): (
        "Has anyone seen marine life or floating debris near this position?"
    ),
    frozenset({_H.BIOLOGIC, _H.UNAUTHORIZED_DIVER}): (
        "Is any dive team working near this position, or is marine life reported here?"
    ),
    frozenset({_H.UNAUTHORIZED_UUV, _H.UNAUTHORIZED_DIVER}): (
        "Can the watch officer get eyes on the water near this position?"
    ),
    frozenset({_H.UNAUTHORIZED_UUV, _H.SURFACE_VESSEL_AIS_OFF}): (
        "Is a surface vessel without a transponder signal transiting near this position?"
    ),
    frozenset({_H.DEBRIS_OR_DRIFT, _H.FILED_OFF_PLAN}): (
        "Has a filed vehicle gone silent and started drifting in this basin?"
    ),
    frozenset({_H.UNAUTHORIZED_UUV, _H.BIOLOGIC}): (
        "Does anyone on watch hear propeller noise or marine life on this bearing?"
    ),
}
"""Fixed questions per unordered top-two pair; any other pair gets ``GENERIC_QUESTION``."""


def rule_based_probe_order(ctx: GuardContext) -> tuple[ProbeId, ...]:
    """Allowlisted probes the guard would accept now, cheapest first.

    Order: cost, then interruptions, then latency, then allowlist order. A probe above the
    current rung, over a remaining budget, or already run in this bucket is left out.
    """
    current = ctx.rung_index(ctx.current_rung)
    eligible: list[tuple[float, int, float, int, ProbeId]] = []
    for i, (probe, terms) in enumerate(ctx.allowlist.items()):
        if probe in ctx.already_run:
            continue
        if ctx.rung_index(terms.rung_required) > current:
            continue
        if ctx.remaining.allows(terms) is not None:
            continue
        eligible.append((terms.cost_usd, terms.interruption, terms.latency_s, i, probe))
    return tuple(e[-1] for e in sorted(eligible))


def _top(report: PosteriorReport, n: int) -> list[Hypothesis]:
    return [h for h, _ in report.posterior.ranked()[:n]]


def template_question(report: PosteriorReport) -> str | None:
    """The fixed question for the top-two pair, or ``None`` when they are well separated."""
    if report.posterior.top2_margin() >= QUESTION_MARGIN_MAX:
        return None
    pair = frozenset(_top(report, 2))
    return PAIR_QUESTIONS.get(pair, GENERIC_QUESTION)


def template_hypotheses(report: PosteriorReport) -> HypothesisSetOutput:
    """The top three of the fixed eight by posterior, with fixed reasons and question."""
    entries = [
        HypothesisEntry(id=h.value, why=HYPOTHESIS_WHY[h])
        for h in _top(report, TEMPLATE_HYPOTHESES_COUNT)
    ]
    return HypothesisSetOutput(hypotheses=entries, question_for_human=template_question(report))


def fallback_probe_plan(
    ctx: GuardContext, report: PosteriorReport, probe: ProbeId | None = None
) -> ProbePlanOutput | None:
    """Wrap a probe choice in the planner's output schema.

    ``probe`` is the deterministic planner's choice when the caller has one; otherwise the
    first of ``rule_based_probe_order``. ``None`` when no probe qualifies.
    """
    if probe is None:
        order = rule_based_probe_order(ctx)
        if not order:
            return None
        probe = order[0]
        why = f"Fallback order: {probe.value} is the cheapest permitted probe left."
    else:
        why = f"Deterministic planner choice: {probe.value}."
    return ProbePlanOutput(
        next_probe=probe.value,
        why=why,
        hypotheses_to_discriminate=_top(report, 2),
        question_for_human=template_question(report),
    )
