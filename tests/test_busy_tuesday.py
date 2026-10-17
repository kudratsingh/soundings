"""WO-080: Busy Tuesday (BT-01) through the runner with ``skip_idle_ticks=True``.

Exit tests: ``-k filed_cleared``, ``-k rogue_handed_off`` (gated: until WO-033 runner v2
is on main the rogue lineage stays INVESTIGATING and is never cleared), ``-k two_runs``,
``-k runtime``, ``-k false_alarm_rate``, ``-k no_show``, ``-k reacquire_precision``,
``-k ais_feature``, and the BT-01 citation check. Ground truth is read here for grading
only.
"""

from __future__ import annotations

import ast
import math
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest

from soundings.investigation.features import NO_AIS_FEED, SOURCE_SIM_AIS, extract_context
from soundings.matcher import PLAN_POSITION_SOURCE_PREFIX, PLAN_SOURCE_PREFIX
from soundings.registry.store import RegistryStore, plan_views_from_scenario
from soundings.runner import RunResult, ScenarioRunner
from soundings.schemas.enums import FeatureName, LadderState
from soundings.schemas.harbor import load_harbor
from soundings.schemas.scenario import Scenario
from soundings.sim.detection import load_detection_params
from soundings.sim.grading import check_expected_contacts, lineage_terminal, lineages
from soundings.sim.hashing import track_hash
from soundings.sim.scenario import REPO_ROOT, catalog_paths, resolve_scenario

S = LadderState
BT01: Scenario = resolve_scenario("BT-01")
PAST_ASK = {S.INVESTIGATING, S.ADVISING_SLOW, S.HOLDING, S.AWAITING_APPROVAL, S.HANDED_OFF}


RUNTIME_LIMIT_S = 60.0


@dataclass(frozen=True)
class _Timed:
    result: RunResult
    seconds: float


@pytest.fixture(scope="module")
def timed() -> _Timed:
    """Sim plus runner, headless (no UI pacing), 1x with tick skipping."""
    t0 = time.perf_counter()
    result = ScenarioRunner(BT01, skip_idle_ticks=True).run()
    return _Timed(result, time.perf_counter() - t0)


@pytest.fixture(scope="module")
def result(timed: _Timed) -> RunResult:
    return timed.result


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


def test_rogue_handed_off_gated(result: RunResult) -> None:
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
        from_db = ScenarioRunner(BT01, plans=store, skip_idle_ticks=True).run()
    finally:
        store.close()
    assert from_db.transition_log_hash == result.transition_log_hash


def test_runtime_under_60s(timed: _Timed) -> None:
    assert timed.seconds < RUNTIME_LIMIT_S, f"BT-01 took {timed.seconds:.1f} s"
    assert timed.result.ticks_stepped < BT01.duration_ticks


def test_two_runs_identical(result: RunResult) -> None:
    again = ScenarioRunner(BT01, skip_idle_ticks=True).run()
    assert again.transition_log_hash == result.transition_log_hash
    assert again.evidence.table_hash() == result.evidence.table_hash()
    assert track_hash(again.sim) == track_hash(result.sim)
    assert again.cases == result.cases


def _rogue_cases(result: RunResult) -> list[str]:
    return [c.contact_id for c in lineages(result)["rogue-1"]]


def test_reacquire_precision_rogue_never_laundered(result: RunResult) -> None:
    """No job plan reaches a rogue case as an identity, and no carry-over link into or out
    of the rogue's observed ids joins another ground-truth contact.

    A position-only registry row (source ``registry.position:``, the rogue crossing a
    dredge plan's main_channel area) is an area match, never an identity, and never makes
    a filed lineage (DECISIONS 2026-10-16 23:44); a ``registry:`` row would be laundering.
    """
    truth = result.sim.contact_truth
    rogue_ids = set(_rogue_cases(result))
    job_plans = {p.plan_id for p in BT01.registry_plans}
    for cid in sorted(rogue_ids):
        for row in result.evidence.for_contact(cid):
            if str(row.value) in job_plans:
                assert row.source.startswith(PLAN_POSITION_SOURCE_PREFIX), (cid, row.source)
                assert not row.source.startswith(PLAN_SOURCE_PREFIX)
    for case in lineages(result)["rogue-1"]:
        assert all(o.plan_id is None for o in case.outcomes), case.case_id
    for link in result.reacquisitions:
        if link.old_id in rogue_ids or link.new_id in rogue_ids:
            assert truth[link.old_id].gt_id == truth[link.new_id].gt_id == "rogue-1"


@pytest.mark.xfail(
    strict=True,
    reason=(
        "runner v1's re-acquisition (WO-022 matcher) links co-working filed jobs that share "
        "one survey or dredge box (job-02/job-07, job-17/job-37) and a few biologic false "
        "alarms to jobs; see DECISIONS 2026-10-17 WO-080 part B and the open question"
    ),
)
def test_reacquire_precision_every_link_same_gt(result: RunResult) -> None:
    truth = result.sim.contact_truth
    wrong = [
        (r.old_id, r.new_id)
        for r in result.reacquisitions
        if truth[r.old_id].gt_id != truth[r.new_id].gt_id
    ]
    assert wrong == []


def test_ais_feature_reading_under_vessel(result: RunResult) -> None:
    sim = result.sim
    harbor = load_harbor()
    found = None
    for cid in sorted(sim.tracks):
        track = sim.tracks[cid]
        for k, point in enumerate(track.points):
            upto = track.model_copy(update={"points": track.points[: k + 1], "gaps": ()})
            rows = extract_context(upto, harbor, hour_of_day=BT01.hour_of_day, ais=sim.ais_tracks)
            row = next(r for r in rows if r.feature is FeatureName.AIS_SURFACE_CORRELATION)
            if row.value == 1.0:
                found = (cid, point.tick, row)
                break
        if found:
            break
    assert found is not None, "no BT-01 reading under a vessel"
    assert found[2].source == SOURCE_SIM_AIS
    # Without a feed the same reading stays unknown, exactly as before WO-080.
    cid, tick = found[0], found[1]
    track = sim.tracks[cid]
    upto = track.model_copy(
        update={"points": tuple(p for p in track.points if p.tick <= tick), "gaps": ()}
    )
    plain = extract_context(upto, harbor, hour_of_day=BT01.hour_of_day)
    ais_row = next(r for r in plain if r.feature is FeatureName.AIS_SURFACE_CORRELATION)
    assert ais_row.source == NO_AIS_FEED


def test_ais_feature_catalog_evidence_unchanged() -> None:
    """The runner never passes a feed, and without one extract_context is unchanged."""
    tree = ast.parse((REPO_ROOT / "soundings" / "runner.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "extract_context":
            assert all(kw.arg != "ais" for kw in node.keywords)
    from soundings.sim.run import run_scenario

    harbor = load_harbor()
    for path in catalog_paths():
        sc = resolve_scenario(path)
        sim = run_scenario(sc, seed=1)
        for cid in sorted(sim.tracks)[:3]:
            rows = extract_context(sim.tracks[cid], harbor, hour_of_day=sc.hour_of_day)
            assert rows == extract_context(
                sim.tracks[cid], harbor, hour_of_day=sc.hour_of_day, ais=None
            )
            ais_row = next(r for r in rows if r.feature is FeatureName.AIS_SURFACE_CORRELATION)
            assert ais_row.source == NO_AIS_FEED


def test_bt01_citations_present(result: RunResult) -> None:
    for t in result.transitions:
        assert t.evidence_row_ids
        for rid in t.evidence_row_ids:
            assert rid in result.evidence


def test_vessels_open_no_case(result: RunResult) -> None:
    gts = {result.sim.contact_truth[c.contact_id].gt_id for c in result.cases}
    assert not any(g.startswith("ais-") for g in gts)
    assert sorted(result.sim.ais_tracks) == ["ais-01", "ais-02", "ais-03"]
