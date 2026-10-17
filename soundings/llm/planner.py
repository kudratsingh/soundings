"""The LLM probe planner: WO-031's deterministic planner behind the WO-032 guard.

``LlmPlanner`` implements WO-031's ``Planner`` protocol (``choose(state, tables)``). Per
decision it:

1. computes the ``DeterministicPlanner`` choice first (always available);
2. builds a ``GuardContext`` from the state: WO-031 ``ALLOWLIST`` terms, the
   ``BudgetLedger``'s remaining budget, and every probe ``PlannerState.already_ran``;
3. makes at most one ``probe_planner`` call through ``ProbePlannerRole`` (no retry, no
   loop; at most ``budgets.llm_calls_per_contact`` calls per contact);
4. returns ``source="llm"`` with the model's ``why`` and question when the guard accepts
   the reply, and otherwise the deterministic choice with ``source="fallback"``:

   * ``fallback_reason="replay_miss"`` on a replay miss outside CI, with no violation
     row (a miss is not a violation; in CI ``ReplayMiss`` propagates, WORKFLOW 8);
   * ``fallback_reason=<ViolationCode>`` on a rejection, with one row appended to the
     violations log (``fallback used = deterministic_planner``);
   * ``fallback_reason="llm_budget_spent"`` once the per-contact call budget is spent
     (no call is made).

An accepted reply is re-checked against the ledger, the rung and the probes already run
(``post_guard_recheck``) before it is returned, so even a guard defect cannot make the
planner pick a probe the deterministic rules would refuse. Nothing returned is a
control: a ``PlannerChoice`` names one allowlisted ``ProbeId`` or none (CLAUDE.md 2.2).

``make_planner(llm_roles)`` is the hook runner v2 (WO-033) uses: ``llm_roles`` defaults
to ``LLM_ROLES_DEFAULT = False`` until recorded fixtures exist, and when on the client is
pinned to ``MODEL_MODE=replay`` with no transport, so no code path here can reach a
hosted model (CLAUDE.md 2.3).
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any, Final

from soundings.investigation.evidence import EvidenceTable
from soundings.investigation.planner import (
    CostWeights,
    DeterministicPlanner,
    Planner,
    PlannerChoice,
    PlannerState,
)
from soundings.investigation.posterior import LikelihoodTables, PosteriorReport
from soundings.investigation.probes import ALLOWLIST, ProbeSpec, rung_index
from soundings.llm.client import FixtureStore, ModelClient, ModelMode
from soundings.llm.fallback import fallback_probe_plan
from soundings.llm.guard import (
    DEFAULT_VIOLATIONS_PATH,
    GuardContext,
    ProbePlanOutput,
    ProbeTerms,
    RemainingBudget,
)
from soundings.llm.roles import (
    FALLBACK_DETERMINISTIC_PLANNER,
    ProbePlannerRole,
    RoleOutcome,
    build_probe_planner_inputs,
)
from soundings.schemas.enums import ProbeId

__all__ = [
    "LLM_ROLES_DEFAULT",
    "POST_GUARD_RECHECK",
    "REASON_LLM_PROPOSAL",
    "LlmPlanner",
    "default_when",
    "guard_allowlist",
    "guard_context",
    "make_planner",
    "planner_inputs",
    "planner_report",
    "probe_terms",
    "remaining_budget",
]

LLM_ROLES_DEFAULT: Final = False
"""Runner default for the model roles (DECISIONS 2026-10-16 23:05): off until the
owner-approved S04-S08 fixture run exists."""

REASON_LLM_PROPOSAL: Final = "llm_proposal_accepted"
"""``PlannerChoice.reason`` for a guard-accepted model proposal."""

POST_GUARD_RECHECK: Final = "post_guard_recheck"
"""``fallback_reason`` when an accepted reply fails the deterministic re-check."""


# --- state -> guard context --------------------------------------------------------------


def probe_terms(spec: ProbeSpec) -> ProbeTerms:
    """The guard's view of one WO-031 ``ProbeSpec`` (rung, cost, latency, interruption)."""
    return ProbeTerms(
        rung_required=spec.rung_required,
        cost_usd=float(spec.cost_usd),
        latency_s=float(spec.latency_s),
        interruption=int(spec.interruption),
    )


def guard_allowlist(
    allowlist: Mapping[ProbeId, ProbeSpec] = ALLOWLIST,
) -> dict[ProbeId, ProbeTerms]:
    """WO-031 ``ALLOWLIST`` as guard terms, in allowlist order."""
    return {pid: probe_terms(spec) for pid, spec in allowlist.items()}


def remaining_budget(state: PlannerState) -> RemainingBudget:
    """The ledger's remaining per-contact budget as the guard's ``RemainingBudget``."""
    left = state.ledger.remaining(state.contact_id)
    return RemainingBudget(
        probes=left.probes, paid_probes=left.paid, interruptions=left.interruptions
    )


def guard_context(state: PlannerState) -> GuardContext:
    """Everything ``check_probe_plan`` needs, derived from one ``PlannerState``.

    ``already_run`` holds every allowlisted probe ``state.already_ran`` reports (its
    ``ran_probes`` plus any whose argument-free key is in ``run_keys`` for this bucket),
    so the guard and the deterministic planner skip the same probes.
    """
    return GuardContext(
        allowlist=guard_allowlist(),
        current_rung=state.current_rung,
        remaining=remaining_budget(state),
        contact_id=state.contact_id,
        tick=state.tick,
        already_run=frozenset(pid for pid in ALLOWLIST if state.already_ran(pid)),
    )


def planner_report(state: PlannerState) -> PosteriorReport:
    """A posterior-only report for a caller that has no WO-030 report at hand.

    The input builders read only ``contact_id`` and ``posterior``; ``prior`` repeats the
    posterior because the state carries no prior.
    """
    return PosteriorReport(
        contact_id=state.contact_id, prior=state.posterior, posterior=state.posterior
    )


def planner_inputs(
    state: PlannerState,
    *,
    table: EvidenceTable | None = None,
    report: PosteriorReport | None = None,
    asset_context: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """The exact ``probe_planner`` inputs ``LlmPlanner.choose`` sends for this state."""
    return build_probe_planner_inputs(
        table if table is not None else EvidenceTable(),
        report if report is not None else planner_report(state),
        guard_context(state),
        dict(asset_context or {}),
    )


def default_when(state: PlannerState) -> str:
    """The violation log's ``when`` cell when the caller passes none (no clock read)."""
    return f"{state.contact_id}/t={state.tick}"


# --- the planner -------------------------------------------------------------------------


class LlmPlanner:
    """``Planner`` that asks the ``probe_planner`` role and always falls back safely.

    ``client`` is any ``ModelClient`` (replay by default). ``deterministic`` defaults to a
    ``DeterministicPlanner`` with the ``config/ladder.yaml`` weights. ``call_limit``
    defaults to ``budgets.llm_calls_per_contact``. ``last_outcome`` keeps the most recent
    ``RoleOutcome`` (fixture key, violation, detail) for audit and tests.
    """

    def __init__(
        self,
        client: ModelClient,
        *,
        deterministic: DeterministicPlanner | None = None,
        weights: CostWeights | None = None,
        violations_path: Path = DEFAULT_VIOLATIONS_PATH,
        call_limit: int | None = None,
    ) -> None:
        self.deterministic = (
            deterministic if deterministic is not None else DeterministicPlanner(weights)
        )
        self.role = ProbePlannerRole(client, violations_path=violations_path, call_limit=call_limit)
        self.last_outcome: RoleOutcome[ProbePlanOutput | None] | None = None

    @property
    def call_limit(self) -> int:
        return self.role.call_limit

    def calls_made(self, contact_id: str) -> int:
        """Model calls made so far for this contact (misses and rejections count)."""
        return self.role.calls_made(contact_id)

    def choose(
        self,
        state: PlannerState,
        tables: LikelihoodTables,
        *,
        table: EvidenceTable | None = None,
        report: PosteriorReport | None = None,
        asset_context: Mapping[str, Any] | None = None,
        when: str | None = None,
    ) -> PlannerChoice:
        """One decision: at most one model call, the deterministic choice on any doubt.

        ``table``, ``report`` and ``asset_context`` only shape the model's inputs (and so
        the fixture key); ``report.posterior`` should be ``state.posterior``. ``when``
        labels a violation row (for example ``S05/seed11/t=212``).
        """
        det = self.deterministic.choose(state, tables)
        ctx = guard_context(state)
        rep = report if report is not None else planner_report(state)

        def fallback() -> ProbePlanOutput | None:
            if det.probe_id is None:
                return None
            return fallback_probe_plan(ctx, rep, det.probe_id)

        outcome = self.role.propose(
            table if table is not None else EvidenceTable(),
            rep,
            ctx,
            dict(asset_context or {}),
            when=when if when is not None else default_when(state),
            fallback=fallback,
            fallback_name=FALLBACK_DETERMINISTIC_PLANNER,
        )
        self.last_outcome = outcome
        if outcome.source == "llm" and isinstance(outcome.value, ProbePlanOutput):
            accepted = self._accepted(state, det, outcome.value)
            if accepted is not None:
                return accepted
            return self._fallback(det, POST_GUARD_RECHECK)
        return self._fallback(det, outcome.fallback_reason)

    @staticmethod
    def _fallback(det: PlannerChoice, reason: str | None) -> PlannerChoice:
        return det.model_copy(update={"source": "fallback", "fallback_reason": reason})

    @staticmethod
    def _accepted(
        state: PlannerState, det: PlannerChoice, plan: ProbePlanOutput
    ) -> PlannerChoice | None:
        """The model's pick as a ``PlannerChoice``, or ``None`` if the re-check fails."""
        try:
            probe = ProbeId(plan.next_probe)
        except ValueError:
            return None
        spec = ALLOWLIST.get(probe)
        if (
            spec is None
            or rung_index(spec.rung_required) > rung_index(state.current_rung)
            or not state.ledger.allows(state.contact_id, probe)
            or state.already_ran(probe)
        ):
            return None
        scored = {r.probe_id: r for r in det.ranked}
        ranked = scored.get(probe)
        return PlannerChoice(
            probe_id=probe,
            gain=ranked.gain if ranked is not None else 0.0,
            score=ranked.score if ranked is not None else 0.0,
            ranked=det.ranked,
            reason=REASON_LLM_PROPOSAL,
            source="llm",
            fallback_reason=None,
            why=plan.why,
            question_for_human=plan.question_for_human,
        )


def make_planner(
    llm_roles: bool = LLM_ROLES_DEFAULT,
    *,
    fixture_store: FixtureStore | None = None,
    violations_path: Path = DEFAULT_VIOLATIONS_PATH,
    weights: CostWeights | None = None,
) -> Planner:
    """The planner a runner should use.

    ``llm_roles=False`` (the default) gives the plain ``DeterministicPlanner``.
    ``llm_roles=True`` gives an ``LlmPlanner`` over a replay-only ``ModelClient`` (no
    transport), reading ``fixture_store`` or the default store
    (``SOUNDINGS_FIXTURE_ROOT`` when set).
    """
    deterministic = DeterministicPlanner(weights)
    if not llm_roles:
        return deterministic
    client = ModelClient(mode=ModelMode.REPLAY, store=fixture_store)
    return LlmPlanner(client, deterministic=deterministic, violations_path=violations_path)
