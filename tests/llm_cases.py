"""Synthetic inputs for the LLM guard tests and the hand-written fixtures.

The allowlist below copies SPEC-INVESTIGATION section 5 by hand so these tests do not
depend on the probe module (WO-031). Every hand-written fixture under
``tests/fixtures/model_calls/{probe_planner,hypothesis_writer}/`` is ``<key>.json`` with
``key = fixture_key(role, inputs)``; ``PLANNER_CASES`` / ``WRITER_CASES`` name the case
each key belongs to. Only the fixture ``output`` is written by hand.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from soundings.investigation.evidence import EvidenceTable
from soundings.investigation.posterior import Posterior, PosteriorReport
from soundings.llm.guard import GuardContext, ProbeTerms, RemainingBudget, ViolationCode
from soundings.llm.roles import build_hypothesis_writer_inputs, build_probe_planner_inputs
from soundings.schemas import EvidenceKind, EvidenceRow, FeatureName, Hypothesis, ProbeId, Rung
from soundings.schemas.enums import Unit

FIXTURE_ROOT = Path(__file__).resolve().parent / "fixtures" / "model_calls"
CONTACT = "c-0001"

ALLOWLIST: dict[ProbeId, ProbeTerms] = {
    ProbeId.REGISTRY_LOOKUP: ProbeTerms(Rung.WATCH, 0.0, 0.0),
    ProbeId.KINEMATICS_PROFILE: ProbeTerms(Rung.WATCH, 0.0, 0.0),
    ProbeId.DRIFT_CHECK: ProbeTerms(Rung.WATCH, 0.0, 0.0),
    ProbeId.PATTERN_OF_LIFE_CHECK: ProbeTerms(Rung.WATCH, 0.0, 0.0),
    ProbeId.AIS_SURFACE_CORRELATE: ProbeTerms(Rung.WATCH, 0.0, 1.0),
    ProbeId.ACOUSTIC_FEATURE_READ: ProbeTerms(Rung.WATCH, 0.0, 2.0),
    ProbeId.ACOUSTIC_HAIL: ProbeTerms(Rung.ASK, 0.01, 20.0),
    ProbeId.CAMERA_CUE: ProbeTerms(Rung.ASK, 0.05, 30.0),
    ProbeId.SECOND_SENSOR_DWELL: ProbeTerms(Rung.ASK, 0.10, 60.0),
    ProbeId.ASK_OPERATOR_BY_PHONE: ProbeTerms(Rung.ASK, 0.0, 120.0),
    ProbeId.REQUEST_WATCH_OFFICER_LOOK: ProbeTerms(Rung.ASK, 0.0, 60.0, interruption=1),
}
"""SPEC-INVESTIGATION section 5, copied by hand (probe, rung, cost, latency)."""

FULL_BUDGET = RemainingBudget(probes=4, paid_probes=2, interruptions=1)
WATCH_SWEEP = frozenset(
    {
        ProbeId.REGISTRY_LOOKUP,
        ProbeId.KINEMATICS_PROFILE,
        ProbeId.DRIFT_CHECK,
        ProbeId.PATTERN_OF_LIFE_CHECK,
        ProbeId.AIS_SURFACE_CORRELATE,
        ProbeId.ACOUSTIC_FEATURE_READ,
    }
)


def make_ctx(
    current_rung: Rung = Rung.ASK,
    remaining: RemainingBudget = FULL_BUDGET,
    already_run: frozenset[ProbeId] = WATCH_SWEEP,
    tick: int = 120,
) -> GuardContext:
    return GuardContext(
        allowlist=ALLOWLIST,
        current_rung=current_rung,
        remaining=remaining,
        contact_id=CONTACT,
        tick=tick,
        already_run=already_run,
    )


def _row(feature: FeatureName, value: Any, unit: Unit, confidence: float, t: int) -> EvidenceRow:
    return EvidenceRow(
        contact_id=CONTACT,
        t=t,
        feature=feature,
        value=value,
        unit=unit,
        source="sim.detector:hyd_1",
        confidence=confidence,
        kind=EvidenceKind.FACT,
        probe_id=None,
    )


def make_table() -> EvidenceTable:
    table = EvidenceTable()
    table.extend(
        [
            _row(FeatureName.SPEED_OVER_GROUND, 1.2, Unit.M_PER_S, 0.8, 100),
            _row(FeatureName.SPEED_OVER_GROUND, 1.3000000004, Unit.M_PER_S, 0.8, 110),
            _row(FeatureName.PROPELLER_TONAL_PRESENT, True, Unit.BOOL, 0.7, 110),
            _row(FeatureName.REGISTRY_MATCH, "none", Unit.CATEGORY, 0.9, 112),
        ]
    )
    return table


def _report(weights: dict[Hypothesis, float]) -> PosteriorReport:
    full = {h: weights.get(h, 0.02) for h in Hypothesis}
    return PosteriorReport(
        contact_id=CONTACT, prior=Posterior.uniform(), posterior=Posterior.from_weights(full)
    )


def threat_report() -> PosteriorReport:
    """Top two UNAUTHORIZED_UUV / FILED_OFF_PLAN, margin well under 0.5."""
    return _report({Hypothesis.UNAUTHORIZED_UUV: 0.40, Hypothesis.FILED_OFF_PLAN: 0.30})


def benign_report() -> PosteriorReport:
    """Top two BIOLOGIC / DEBRIS_OR_DRIFT."""
    return _report({Hypothesis.BIOLOGIC: 0.45, Hypothesis.DEBRIS_OR_DRIFT: 0.35})


def settled_report() -> PosteriorReport:
    """FILED_ON_PLAN far ahead: top2 margin at least 0.5, so no template question."""
    return _report({Hypothesis.FILED_ON_PLAN: 0.9})


def asset(time_to_asset_s: float) -> dict[str, Any]:
    return {
        "nearest_asset_id": "fuel_berth_1",
        "zone_criticality": 0.9,
        "time_to_nearest_critical_asset_s": time_to_asset_s,
    }


@dataclass(frozen=True)
class PlannerCase:
    name: str
    ctx: GuardContext
    asset_context: dict[str, Any]
    expected: ViolationCode | None

    def inputs(self) -> dict[str, Any]:
        return build_probe_planner_inputs(
            make_table(), threat_report(), self.ctx, self.asset_context
        )


PLANNER_CASES: dict[str, PlannerCase] = {
    c.name: c
    for c in (
        PlannerCase("valid", make_ctx(), asset(410.0), None),
        PlannerCase("names_rung", make_ctx(), asset(400.0), ViolationCode.NAMES_RUNG),
        PlannerCase("names_tool_call", make_ctx(), asset(390.0), ViolationCode.NAMES_TOOL_CALL),
        PlannerCase("names_approval", make_ctx(), asset(380.0), ViolationCode.NAMES_APPROVAL),
        PlannerCase(
            "over_budget",
            make_ctx(remaining=RemainingBudget(probes=2, paid_probes=0, interruptions=1)),
            asset(370.0),
            ViolationCode.OVER_BUDGET,
        ),
        PlannerCase(
            "above_rung",
            make_ctx(current_rung=Rung.WATCH),
            asset(360.0),
            ViolationCode.ABOVE_CURRENT_RUNG,
        ),
        PlannerCase("free_text", make_ctx(), asset(350.0), ViolationCode.NOT_IN_ALLOWLIST),
    )
}
VIOLATING_PLANNER_CASES = tuple(n for n, c in PLANNER_CASES.items() if c.expected is not None)


@dataclass(frozen=True)
class WriterCase:
    name: str
    report: Callable[[], PosteriorReport]
    expected: ViolationCode | None

    def inputs(self) -> dict[str, Any]:
        return build_hypothesis_writer_inputs(self.report(), make_table())


WRITER_CASES: dict[str, WriterCase] = {
    c.name: c
    for c in (
        WriterCase("valid", threat_report, None),
        WriterCase("ninth_hypothesis", benign_report, ViolationCode.HYPOTHESIS_OUTSIDE_SET),
    )
}
