"""WO-080: Busy Tuesday (BT-01) through the runner.

Part A exit tests: ``-k filed_cleared`` (through runner v1 as merged), ``-k rogue``
(gated: until WO-033 runner v2 is on main the rogue lineage stays INVESTIGATING and is
never cleared), ``-k false_alarm_rate``, ``-k no_show``, and the BT-01 citation check.
Ground truth is read here for grading only.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime
from pathlib import Path

import pytest

from soundings.registry.store import RegistryStore, plan_views_from_scenario
from soundings.runner import RunResult, ScenarioRunner
from soundings.schemas.enums import LadderState
from soundings.schemas.harbor import load_harbor
from soundings.schemas.scenario import Scenario
from soundings.sim.detection import load_detection_params
from soundings.sim.grading import check_expected_contacts, lineage_terminal, lineages
from soundings.sim.scenario import resolve_scenario

S = LadderState
BT01: Scenario = resolve_scenario("BT-01")
PAST_ASK = {S.INVESTIGATING, S.ADVISING_SLOW, S.HOLDING, S.AWAITING_APPROVAL, S.HANDED_OFF}


@pytest.fixture(scope="module")
def result() -> RunResult:
    return ScenarioRunner(BT01).run()


def test_filed_cleared_all_39(result: RunResult) -> None:
    problems = [p for p in check_expected_contacts(result, BT01) if p.startswith("job-")]
    assert problems == []
    lines = lineages(result)
    jobs = sorted(g for g in lines if g.startswith("job-"))
    assert len(jobs) == 39
    filed_case_ids = set()
    for g in jobs:
        for case in lines[g]:
            assert case.state is S.CLEARED_AT_ASK, (g, case.case_id, case.state)
            filed_case_ids.add(case.case_id)
    # No filed case moves past ASKING: zero interruptions under the WO-022 definition.
    for t in result.transitions:
        if t.case_id in filed_case_ids:
            assert t.to_state not in PAST_ASK
            assert t.to_state is not S.LOST_VEHICLE
    assert result.interruptions == 0
    recs = getattr(result, "recommendations", ())  # runner v2+ only
    assert not any(getattr(r, "case_id", None) in filed_case_ids for r in recs)
    assert len(plan_views_from_scenario(BT01)) == 40
    # The hail answered for every filed contact.
    answered = {
        result.sim.contact_truth[h.contact_id].gt_id
        for h in result.hails
        if h.hail_reply.value == "valid"
    }
    assert set(jobs) <= answered


def test_rogue_gated_v1_never_cleared(result: RunResult) -> None:
    """Gated: runner v2 (WO-033) is not on main, so the v1 end state is INVESTIGATING.

    WO-033 part B flips this to the HANDED_OFF assertion (DECISIONS 2026-10-17 06:37).
    """
    cases = lineages(result)["rogue-1"]
    assert cases
    assert lineage_terminal(cases) is S.INVESTIGATING
    for case in cases:
        assert case.state is S.INVESTIGATING and case.investigation_pending
        assert case.state is not S.CLEARED_AT_ASK
        assert not case.state.value.startswith("RESOLVED")


def test_false_alarm_rate() -> None:
    from soundings.sim.run import run_scenario

    sim = run_scenario(BT01)
    n = sum(1 for g in sim.truth_paths if g.startswith("fa-"))
    rate = load_detection_params().false_alarms_per_node_hour
    hours = BT01.duration_ticks * BT01.tick_s / 3600.0
    lam = len(load_harbor().sensor_nodes) * rate * hours
    assert abs(n - lam) <= 3 * math.sqrt(lam), (n, lam)


def test_no_show_opens_nothing(result: RunResult, tmp_path: Path) -> None:
    filed = {c.registry.plan_id for c in BT01.contacts if c.registry.plan_id}
    no_show = [p.plan_id for p in BT01.registry_plans if p.plan_id not in filed]
    assert no_show == ["P-BT01-040"]
    assert all(n.plan_id not in no_show for n in result.notices)
    gts = {result.sim.contact_truth[c.contact_id].gt_id for c in result.cases}
    assert not any(g.startswith("job-40") for g in gts)
    store = RegistryStore(
        tmp_path / "bt.db",
        harbor=load_harbor(),
        now=lambda: datetime(2026, 10, 17, 12, 0, tzinfo=UTC),
    )
    try:
        views = store.seed_from_scenario(BT01)
        assert len(views) == 40
        assert {v.plan_id for v in views} == {p.plan_id for p in BT01.registry_plans}
    finally:
        store.close()


def test_bt01_citations_present(result: RunResult) -> None:
    for t in result.transitions:
        assert t.evidence_row_ids
        for rid in t.evidence_row_ids:
            assert rid in result.evidence


def test_vessels_open_no_case(result: RunResult) -> None:
    gts = {result.sim.contact_truth[c.contact_id].gt_id for c in result.cases}
    assert not any(g.startswith("ais-") for g in gts)
    assert sorted(result.sim.ais_tracks) == ["ais-01", "ais-02", "ais-03"]
