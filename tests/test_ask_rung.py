"""WO-022 integration: the ask rung on S01, S02 and S03 (BUILD-PLAN Phase 2 exit test).

S01 (filed on plan) and S03 (permitted divers) resolve at ask with zero human
interruptions; S02 (filed, gone silent) ends LOST_VEHICLE with a notice holding a
last-known position and a drift search box that contains the true position.
Ground truth is read here for grading only.
"""

from __future__ import annotations

import pytest

from soundings.runner import ScenarioRunner
from soundings.schemas import EvidenceKind, FeatureName, LadderState
from tests.runner_runs import gt_of, is_false_alarm, run, truth_at

SECTIONS = ("Contact", "What we know", "What we assume", "Search box")


@pytest.mark.parametrize(("scenario_id", "gt_ids"), [("S01", {"gt-1"}), ("S03", {"gt-1", "gt-2"})])
def test_filed_scenarios_resolve_at_ask(scenario_id: str, gt_ids: set[str]) -> None:
    result = run(scenario_id)
    gt_cases = [c for c in result.cases if not is_false_alarm(result, c.contact_id)]
    assert {gt_of(result, c.contact_id) for c in gt_cases} == gt_ids
    for case in gt_cases:
        assert case.state is LadderState.CLEARED_AT_ASK, (case.case_id, case.outcome)
        assert case.outcome is not None and case.outcome.plan_id is not None
        assert len(case.hail_keys) == 1
    assert result.interruptions == 0
    assert result.notices == []
    scenario = ScenarioRunner(scenario_id).scenario
    assert scenario.expected.max_human_interruptions == 0
    assert scenario.expected.terminal_state is LadderState.CLEARED_AT_ASK


def test_s02_lost_vehicle_sitrep() -> None:
    result = run("S02")
    assert result.notices, "S02 must produce a LOST_VEHICLE notice"
    notice = result.notices[0]
    assert gt_of(result, notice.contact_id) == "gt-1"
    assert result.case(notice.case_id).state is LadderState.LOST_VEHICLE
    assert notice.plan_id == "P-2026-1017-003"
    assert notice.contact_channel == "sim-phone-operator"
    assert notice.last_known.row_id in result.evidence
    assert notice.search_box.anchor == notice.last_known
    assert notice.silence_s > 600.0
    assert result.interruptions == 0
    scenario = ScenarioRunner("S02").scenario
    assert scenario.expected.sitrep_has_search_box
    # the lineage ends LOST_VEHICLE: every gt-1 case after the first notice is lost too
    later = [
        c
        for c in result.cases
        if gt_of(result, c.contact_id) == "gt-1" and c.opened_tick >= notice.tick
    ]
    assert all(c.state is LadderState.LOST_VEHICLE for c in later)


@pytest.mark.parametrize("seed", [3, 1, 2])
def test_s02_box_contains_truth(seed: int) -> None:
    result = run("S02", seed)
    assert result.notices
    for notice in result.notices:
        gt = gt_of(result, notice.contact_id)
        assert gt == "gt-1"
        assert notice.search_box.contains(*truth_at(result, gt, notice.tick)), notice.case_id
        sections = [line.section for line in notice.lines]
        for name in SECTIONS:
            assert sections.count(name) >= 1, name
    assert result.interruptions == 0


def test_s02_reacquisition_carry_over() -> None:
    result = run("S02")
    notice = result.notices[0]
    case = result.case(notice.case_id)
    assert case.reacquired_from is not None
    assert gt_of(result, case.reacquired_from) == "gt-1"
    rows = result.evidence.for_contact(case.contact_id)
    marker = [
        r
        for r in rows
        if r.feature is FeatureName.REACQUIRED_FROM and r.kind is EvidenceKind.ASSUMPTION
    ]
    assert len(marker) == 1  # older links in the lineage arrive as derived copies
    assert marker[0].value == case.reacquired_from and marker[0].assumes
    # the plan the lineage was filed on reaches the new id only through the carry-over
    plan_rows = [
        r for r in rows if r.feature is FeatureName.PLAN_ID and r.source.startswith("registry:")
    ]
    assert plan_rows and all(r.kind is EvidenceKind.DERIVED for r in plan_rows)
    assert all(marker[0].row_id in r.derived_from for r in plan_rows)
    contact_line = [
        ln for ln in notice.lines if ln.section == "Contact" and "re-acquired" in ln.text
    ]
    assert contact_line and marker[0].row_id in contact_line[0].evidence_row_ids
    # the old contact's rows are untouched
    old_rows = result.evidence.for_contact(case.reacquired_from)
    assert all(r.contact_id == case.reacquired_from for r in old_rows)


def test_s02_pending_silence_then_second_hail() -> None:
    result = run("S02")
    case = result.case(result.notices[0].case_id)
    codes = [o.code.value for o in case.outcomes]
    assert codes == ["PENDING_SILENCE", "LOST_VEHICLE"]
    assert len(case.hail_keys) == 2


def test_s02_plan_watch_rehails_cleared_contacts() -> None:
    result = run("S02")
    watch = [h for h in result.hails if h.purpose == "plan_watch"]
    assert watch, "the plan watch re-hails a cleared filed contact"
    for h in watch:
        cleared = [
            c
            for c in result.cases
            if c.contact_id == h.contact_id and c.state is LadderState.CLEARED_AT_ASK
        ]
        assert cleared and all(
            c.closed_tick is not None and c.closed_tick <= h.sent_tick for c in cleared
        )
    assert result.probes_used["acoustic_hail"] >= len(watch)


def test_plan_watch_opens_a_new_case_and_never_reopens_the_cleared_one() -> None:
    """S02 at seed 4 keeps one observed id through the silence, so the watch opens a case."""
    result = run("S02", 4)
    opened = [c for c in result.cases if c.opened_by == "plan_watch"]
    assert len(opened) == 1
    case = opened[0]
    assert case.state is LadderState.LOST_VEHICLE
    first = result.case(f"{case.contact_id}#1")
    assert first.state is LadderState.CLEARED_AT_ASK
    for_first = [t for t in result.transitions if t.case_id == first.case_id]
    assert for_first[-1].to_state is LadderState.CLEARED_AT_ASK
    assert all(t.tick <= first.closed_tick for t in for_first)  # type: ignore[operator]
    steps = [
        (t.from_state.value, t.to_state.value)
        for t in result.transitions
        if t.case_id == case.case_id
    ]
    assert steps == [
        ("DETECTED", "WATCHING"),
        ("WATCHING", "ASKING"),
        ("ASKING", "LOST_VEHICLE"),
    ]
    # the new case reuses the plan-watch hail through its idempotency key
    watch_keys = {h.idempotency_key for h in result.hails if h.purpose == "plan_watch"}
    assert case.hail_keys and case.hail_keys[0] in watch_keys
    reuse = [h for h in result.hails if h.case_id == case.case_id]
    assert reuse and reuse[0].cached
    notice = next(n for n in result.notices if n.case_id == case.case_id)
    assert notice.search_box.contains(*truth_at(result, "gt-1", notice.tick))
