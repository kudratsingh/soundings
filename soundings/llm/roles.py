"""Prompt builders and guarded calls for the ``probe_planner`` and ``hypothesis_writer``.

Input builders return plain, canonical dicts (floats at 6 dp, fixed key sets, fixed
order), so ``fixture_key(role, inputs)`` is stable and every prompt edit shows up as a
new ``PROMPT_VERSION`` and therefore a new key.

``run_guarded_role`` is the one accept / reject / miss path:

* a replay miss (``None`` from the client outside CI) uses the fallback with
  ``fallback_reason="replay_miss"`` and writes **no** violation row (a miss is not a
  violation); in CI the client raises ``ReplayMiss`` instead (WORKFLOW section 8);
* a guard rejection uses the fallback, ``fallback_reason=<ViolationCode>``, and appends
  one ``ViolationRow``;
* an accepted reply is returned with ``source="llm"``.

A model call is made at most once per decision and never retried. ``ProbePlannerRole``
also stops calling once ``llm_calls_per_contact`` is spent for a contact. The system
prompts tell the model its limits; the guard does not trust them.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast

from soundings.canonical import round6
from soundings.investigation.evidence import EvidenceTable
from soundings.investigation.posterior import PosteriorReport
from soundings.llm.client import ModelClient, fixture_key
from soundings.llm.fallback import fallback_probe_plan, template_hypotheses
from soundings.llm.guard import (
    DEFAULT_VIOLATIONS_PATH,
    GuardContext,
    GuardResult,
    HypothesisSetOutput,
    ProbePlanOutput,
    ViolationCode,
    ViolationRow,
    append_violation,
    check_hypothesis_set,
    check_probe_plan,
)
from soundings.schemas.config import load_ladder_config
from soundings.schemas.enums import FeatureName, Hypothesis

__all__ = [
    "FALLBACK_DETERMINISTIC_PLANNER",
    "FALLBACK_RULE_ORDER",
    "FALLBACK_TEMPLATE_HYPOTHESES",
    "HYPOTHESIS_WRITER_PROMPT",
    "LLM_BUDGET_SPENT",
    "PROBE_PLANNER_PROMPT",
    "PROMPT_VERSION",
    "REPLAY_MISS",
    "ROLE_HYPOTHESIS_WRITER",
    "ROLE_PROBE_PLANNER",
    "HypothesisWriter",
    "ProbePlannerRole",
    "RoleOutcome",
    "build_hypothesis_writer_inputs",
    "build_probe_planner_inputs",
    "evidence_summary",
    "run_guarded_role",
]

ROLE_PROBE_PLANNER = "probe_planner"
ROLE_HYPOTHESIS_WRITER = "hypothesis_writer"
PROMPT_VERSION = "p3.1"

REPLAY_MISS = "replay_miss"
LLM_BUDGET_SPENT = "llm_budget_spent"
FALLBACK_DETERMINISTIC_PLANNER = "deterministic_planner"
FALLBACK_RULE_ORDER = "rule_based_probe_order"
FALLBACK_TEMPLATE_HYPOTHESES = "template_hypotheses"

PROBE_PLANNER_PROMPT = (
    "You help a harbor watch officer investigate one unknown underwater contact. "
    "You receive the latest evidence rows, the posterior over eight fixed hypotheses, "
    "the probe allowlist with costs and required rungs, the remaining budget, the current "
    "rung and the asset context. Reply with one JSON object and nothing else, with exactly "
    'these keys: "next_probe" (one probe id copied from the allowlist, permitted at the '
    'current rung and within the remaining budget), "why" (one line, at most 200 '
    'characters), "hypotheses_to_discriminate" (one to three ids from the eight), and '
    '"question_for_human" (one line, or null). You may name only one allowlisted probe. '
    "Do not name a rung, a ladder state, an approval, an action or a tool call, and do not "
    "add keys. Deterministic code decides the rung and every transition; your reply is a "
    "suggestion that a guard checks and may discard."
)

HYPOTHESIS_WRITER_PROMPT = (
    "You help a harbor watch officer reason about one unknown underwater contact. You "
    "receive the latest evidence rows, the posterior over eight fixed hypotheses and the "
    "list of those eight ids. Reply with one JSON object and nothing else, with exactly "
    'these keys: "hypotheses" (one to eight objects, each with "id" copied from the eight '
    'and a one-line "why") and "question_for_human" (one line a person on watch could '
    "answer, or null). Never invent a hypothesis outside the eight, never give a "
    "probability, and never name a rung, an approval, an action or a tool call. "
    "Deterministic code owns the posterior and the recommended rung."
)


# --- input builders ----------------------------------------------------------------------


def _plain(value: Any) -> Any:
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, float):
        return round6(value)
    if isinstance(value, list | tuple):
        return [_plain(v) for v in value]
    if isinstance(value, Mapping):
        return {str(k): _plain(v) for k, v in value.items()}
    if hasattr(value, "value") and isinstance(value.value, str):
        return value.value
    return value


def evidence_summary(table: EvidenceTable, contact_id: str) -> list[dict[str, Any]]:
    """The latest row per feature for one contact, in ``FeatureName`` order."""
    rows: list[dict[str, Any]] = []
    for feature in FeatureName:
        row = table.latest(contact_id, feature)
        if row is None:
            continue
        rows.append(
            {
                "feature": feature.value,
                "value": _plain(row.value),
                "kind": row.kind.value,
                "confidence": _plain(row.confidence),
            }
        )
    return rows


def build_probe_planner_inputs(
    table: EvidenceTable,
    report: PosteriorReport,
    ctx: GuardContext,
    asset_context: Mapping[str, Any],
) -> dict[str, Any]:
    """Canonical planner inputs: evidence, posterior, allowlist with costs, budget, rung."""
    return {
        "prompt_version": PROMPT_VERSION,
        "role": ROLE_PROBE_PLANNER,
        "evidence": evidence_summary(table, report.contact_id),
        "posterior": report.posterior.to_dict(),
        "allowlist": [
            {
                "probe_id": probe.value,
                "rung_required": terms.rung_required.value,
                "cost_usd": round6(terms.cost_usd),
                "latency_s": round6(terms.latency_s),
                "interruption": terms.interruption,
            }
            for probe, terms in ctx.allowlist.items()
        ],
        "already_run": sorted(p.value for p in ctx.already_run),
        "remaining_budget": {
            "probes": ctx.remaining.probes,
            "paid_probes": ctx.remaining.paid_probes,
            "interruptions": ctx.remaining.interruptions,
        },
        "current_rung": ctx.current_rung.value,
        "asset_context": _plain(dict(asset_context)),
    }


def build_hypothesis_writer_inputs(report: PosteriorReport, table: EvidenceTable) -> dict[str, Any]:
    """Canonical writer inputs: evidence, posterior and the eight hypothesis ids."""
    return {
        "prompt_version": PROMPT_VERSION,
        "role": ROLE_HYPOTHESIS_WRITER,
        "evidence": evidence_summary(table, report.contact_id),
        "posterior": report.posterior.to_dict(),
        "hypotheses": [h.value for h in Hypothesis],
    }


# --- guarded calls -----------------------------------------------------------------------


@dataclass(frozen=True)
class RoleOutcome[T]:
    """What a role produced and where it came from."""

    source: Literal["llm", "fallback"]
    value: T
    fallback_reason: str | None
    violation: ViolationCode | None
    fixture_key: str
    detail: str = ""


def run_guarded_role[T](
    client: ModelClient,
    role: str,
    inputs: Mapping[str, Any],
    check: Callable[[Any], GuardResult],
    fallback: Callable[[], T],
    *,
    when: str,
    fallback_name: str,
    violations_path: Path = DEFAULT_VIOLATIONS_PATH,
    system: str | None = None,
) -> RoleOutcome[T]:
    """One model call through the guard; the fallback on a miss or a rejection."""
    key = fixture_key(role, inputs)
    raw = client.call(role, inputs, system=system)
    if raw is None:
        return RoleOutcome("fallback", fallback(), REPLAY_MISS, None, key)
    result = check(raw)
    if not result.accepted or result.violation is not None:
        code = result.violation or ViolationCode.SCHEMA_INVALID
        append_violation(
            ViolationRow(when, role, key, code, fallback_name, result.detail), violations_path
        )
        return RoleOutcome("fallback", fallback(), code.value, code, key, result.detail)
    return RoleOutcome("llm", cast(T, result.value), None, None, key)


def _llm_calls_per_contact() -> int:
    return int(load_ladder_config().budgets["llm_calls_per_contact"])


class ProbePlannerRole:
    """The guarded ``probe_planner`` call with a per-contact call budget.

    ``propose`` makes at most one model call; once ``call_limit`` calls are spent for a
    contact it returns the fallback with ``fallback_reason="llm_budget_spent"`` and no
    call. ``fallback`` overrides the rule-based plan (the investigation layer passes the
    deterministic planner's plan) and ``fallback_name`` labels it in the violation log.
    """

    def __init__(
        self,
        client: ModelClient,
        *,
        violations_path: Path = DEFAULT_VIOLATIONS_PATH,
        call_limit: int | None = None,
    ) -> None:
        self.client = client
        self.violations_path = violations_path
        self.call_limit = call_limit if call_limit is not None else _llm_calls_per_contact()
        self._calls: dict[str, int] = {}

    def calls_made(self, contact_id: str) -> int:
        return self._calls.get(contact_id, 0)

    def propose(
        self,
        table: EvidenceTable,
        report: PosteriorReport,
        ctx: GuardContext,
        asset_context: Mapping[str, Any],
        *,
        when: str,
        fallback: Callable[[], ProbePlanOutput | None] | None = None,
        fallback_name: str | None = None,
    ) -> RoleOutcome[ProbePlanOutput | None]:
        if fallback is None:

            def fallback() -> ProbePlanOutput | None:
                return fallback_probe_plan(ctx, report)

            name = fallback_name or FALLBACK_RULE_ORDER
        else:
            name = fallback_name or FALLBACK_DETERMINISTIC_PLANNER
        inputs = build_probe_planner_inputs(table, report, ctx, asset_context)
        if self.calls_made(ctx.contact_id) >= self.call_limit:
            key = fixture_key(ROLE_PROBE_PLANNER, inputs)
            return RoleOutcome("fallback", fallback(), LLM_BUDGET_SPENT, None, key)
        self._calls[ctx.contact_id] = self.calls_made(ctx.contact_id) + 1
        return run_guarded_role(
            self.client,
            ROLE_PROBE_PLANNER,
            inputs,
            lambda raw: check_probe_plan(raw, ctx),
            fallback,
            when=when,
            fallback_name=name,
            violations_path=self.violations_path,
            system=PROBE_PLANNER_PROMPT,
        )


class HypothesisWriter:
    """The guarded ``hypothesis_writer`` call: one per investigation, template fallback."""

    def __init__(
        self, client: ModelClient, *, violations_path: Path = DEFAULT_VIOLATIONS_PATH
    ) -> None:
        self.client = client
        self.violations_path = violations_path

    def write(
        self, report: PosteriorReport, table: EvidenceTable, *, when: str
    ) -> RoleOutcome[HypothesisSetOutput]:
        return run_guarded_role(
            self.client,
            ROLE_HYPOTHESIS_WRITER,
            build_hypothesis_writer_inputs(report, table),
            check_hypothesis_set,
            lambda: template_hypotheses(report),
            when=when,
            fallback_name=FALLBACK_TEMPLATE_HYPOTHESES,
            violations_path=self.violations_path,
            system=HYPOTHESIS_WRITER_PROMPT,
        )
