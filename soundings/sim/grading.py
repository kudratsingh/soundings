"""Grading helpers over ground truth (WO-080). Tests and eval only.

These read ``SimRun.contact_truth`` (ground truth), so the runner never imports this
module (``tests/test_busy_tuesday_rules.py`` checks it). A lineage is every case whose
observed id maps to one ground-truth contact, in open order.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from soundings.schemas.enums import LadderState
from soundings.schemas.scenario import Scenario

INTERRUPTION_STATES: frozenset[LadderState] = frozenset(
    {LadderState.AWAITING_APPROVAL, LadderState.HANDED_OFF}
)
"""A transition into one of these needs the watch officer or approver (DECISIONS 23:05)."""


def _case_order(case: Any) -> tuple[int, str, int]:
    cid, _, n = str(case.case_id).partition("#")
    return int(case.opened_tick), cid, int(n or 0)


def lineages(result: Any) -> dict[str, tuple[Any, ...]]:
    """Ground-truth id -> its cases (``CaseResult``) in open order, keys sorted."""
    truth = result.sim.contact_truth
    out: dict[str, list[Any]] = {}
    for case in result.cases:
        gt = truth[case.contact_id].gt_id
        out.setdefault(gt, []).append(case)
    return {gt: tuple(sorted(out[gt], key=_case_order)) for gt in sorted(out)}


def lineage_terminal(cases: Sequence[Any]) -> LadderState:
    """The state of the latest-opened case of a lineage."""
    if not cases:
        raise ValueError("an empty lineage has no terminal state")
    state: LadderState = max(cases, key=_case_order).state
    return state


def lineage_interruptions(result: Any, cases: Sequence[Any]) -> int:
    """Transitions of the lineage's cases into a state that needs a person."""
    ids = {c.case_id for c in cases}
    return sum(
        1 for t in result.transitions if t.case_id in ids and t.to_state in INTERRUPTION_STATES
    )


def check_expected_contacts(result: Any, scenario: Scenario) -> list[str]:
    """Problems with ``scenario.expected.contacts`` in ``result`` (empty means pass).

    A filed contact's lineage passes only if every one of its cases is CLEARED_AT_ASK.
    """
    lines = lineages(result)
    by_id = {c.id: c for c in scenario.contacts}
    problems: list[str] = []
    for exp in scenario.expected.contacts:
        cases = lines.get(exp.id, ())
        if not cases:
            problems.append(f"{exp.id}: no case opened")
            continue
        contact = by_id[exp.id]
        if contact.registry.filed and exp.terminal_state is LadderState.CLEARED_AT_ASK:
            off = [
                f"{c.case_id}={c.state.value}" for c in cases if c.state is not exp.terminal_state
            ]
            if off:
                problems.append(f"{exp.id}: filed lineage has cases not CLEARED_AT_ASK: {off}")
        else:
            term = lineage_terminal(cases)
            if term is not exp.terminal_state:
                problems.append(
                    f"{exp.id}: terminal {term.value}, expected {exp.terminal_state.value}"
                )
        if exp.max_interruptions is not None:
            n = lineage_interruptions(result, cases)
            if n > exp.max_interruptions:
                problems.append(f"{exp.id}: {n} interruptions > {exp.max_interruptions}")
    return problems
