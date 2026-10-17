"""WO-088 part B: the S02 search beat through runner v2 (opt-in: ``run(responder=...)``).

S05 and S06 gain the look beat: as the case enters INVESTIGATING the platform
dispatches the responder to look (rung ask, platform alone, guard-checked) and the
investigation starts once the look has landed, so the look comes before any hold or
handoff. S02 gains the search beat: the plan's operator gets a follow-up LOST_VEHICLE
notice with what the responder's search of the drift box found. Without a responder
(the catalog default) nothing changes. Deterministic.
"""

from __future__ import annotations

from functools import lru_cache

from soundings.investigation.probes import (
    RESPONDER_PROBES,
    SEARCH_DETECT_RADIUS_M,
    search_position,
)
from soundings.runner import RunResultV2, ScenarioRunnerV2
from soundings.schemas.enums import FeatureName, LadderState, ProbeId
from soundings.schemas.geo import distance_m, from_local_m, to_local_m
from soundings.schemas.harbor import Harbor, load_harbor
from soundings.sim.scenario import resolve_scenario
from tests.responder_double import FakeResponder

CASES = [("S05", 11), ("S06", 13)]


@lru_cache(maxsize=1)
def _harbor() -> Harbor:
    return load_harbor()


def _dock(sid: str) -> tuple[float, float]:
    """A pier point ~300 m from the scenario's first contact, inside a declared zone."""
    scenario = resolve_scenario(sid, _harbor())
    start = scenario.contacts[0].start
    x, y = to_local_m(start.lat, start.lon)
    for dx, dy in ((300, 0), (-300, 0), (0, 300), (0, -300), (200, 200), (-200, -200)):
        cand = from_local_m(x + dx, y + dy)
        if _harbor().zone_at(*cand) is not None:
            return cand
    raise AssertionError("no dock point inside a declared zone")


@lru_cache(maxsize=8)
def _run(sid: str, seed: int | None, responder: bool) -> tuple[RunResultV2, FakeResponder | None]:
    fake = FakeResponder(_harbor(), _dock(sid)) if responder else None
    runner = ScenarioRunnerV2(sid, seed=seed)
    return runner.run(responder=fake), fake


def _primary(result: RunResultV2) -> str:
    return sorted(result.cases, key=lambda k: (len(k), k))[0]


def test_no_responder_no_beat() -> None:
    without, _ = _run("S02", None, False)
    assert not [c for c in without.probe_calls if c.probe_id in RESPONDER_PROBES]
    assert not any("responder" in line.text for n in without.notices for line in n.lines)


def test_search_beat_keeps_the_catalog_outcome() -> None:
    with_search, _ = _run("S02", None, True)
    without, _ = _run("S02", None, False)
    lost = [c.case_id for c in without.cases if c.state is LadderState.LOST_VEHICLE]
    assert lost
    for case_id in lost:  # the glider's lineage still ends at LOST_VEHICLE
        assert with_search.case(case_id).state is LadderState.LOST_VEHICLE


def test_s02_search_beat_finds_the_glider() -> None:
    result, fake = _run("S02", None, True)
    notices = result.notices
    first = notices[0]
    follows = [n for n in notices[1:] if n.case_id == first.case_id]
    assert len(follows) == 1
    follow = follows[0]
    assert follow.search_box == first.search_box and follow.tick >= first.tick
    texts = [line.text for line in follow.lines if "responder searched" in line.text]
    assert len(texts) == 1
    searches = [c for c in result.probe_calls if c.probe_id is ProbeId.RESPONDER_SEARCH_BOX]
    assert len(searches) == 1
    rows = [r for r in result.evidence if r.probe_id is ProbeId.RESPONDER_SEARCH_BOX]
    found = search_position(rows)
    box = first.search_box
    # the scenario's own check: the drift box contains the vehicle's true position
    assert box.contains(*_truth_at_notice(result, first.contact_id, first.tick))
    assert found is not None and "found the vehicle at" in texts[0]
    assert any(r.feature is FeatureName.SEARCH_RESULT and r.value == "found" for r in rows)
    truth = _truth_at_notice(result, first.contact_id, follow.tick)
    assert distance_m(*found, *truth) <= 2 * SEARCH_DETECT_RADIUS_M
    assert fake is not None
    assert ("search", first.contact_id) in [(d[0], d[1]) for d in fake.dispatches]


def _truth_at_notice(result: RunResultV2, cid: str, tick: int) -> tuple[float, float]:
    gt = result.sim.contact_truth[cid].gt_id
    pts = [p for p in result.sim.truth_paths[gt] if p.tick <= tick]
    return pts[-1].lat, pts[-1].lon


def test_search_beat_is_deterministic() -> None:
    a = ScenarioRunnerV2("S02").run(responder=FakeResponder(_harbor(), _dock("S02")))
    b = ScenarioRunnerV2("S02").run(responder=FakeResponder(_harbor(), _dock("S02")))
    assert a.nested_log_hash == b.nested_log_hash
    assert a.transition_log_hash == b.transition_log_hash
