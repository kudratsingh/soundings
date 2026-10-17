"""Twist toggle plumbing (soundings.sim.toggles): all nine names parse; three change the run.

``sensor_dropout`` zeros a node's detections from ``at_tick``; ``contact_surge{n: 5}``
adds exactly five ground-truth contacts; ``contact_goes_quiet`` silences only the named
contact; the other six are reported as parsed-not-implemented and leave the hash as is.
"""

from __future__ import annotations

from typing import Any

import pytest

from soundings.schemas.enums import Hypothesis, ToggleName
from soundings.schemas.scenario import Scenario, Toggle, scenario_from_data
from soundings.sim.hashing import track_hash
from soundings.sim.run import run_scenario
from soundings.sim.scenario import resolve_scenario
from soundings.sim.toggles import IMPLEMENTED_TOGGLES, SURGE_BEHAVIORS, toggle_report

NOT_IMPLEMENTED = [n for n in ToggleName if n not in IMPLEMENTED_TOGGLES]


def _with_toggles(scenario: Scenario, toggles: list[Any]) -> Scenario:
    data = scenario.model_dump(mode="json")
    data["toggles"] = toggles
    return scenario_from_data(data)


@pytest.fixture(scope="module")
def s05() -> Scenario:
    return resolve_scenario("S05")


@pytest.fixture(scope="module")
def s03() -> Scenario:
    return resolve_scenario("S03")


def test_all_nine_names_parse() -> None:
    assert len(ToggleName) == 9
    for name in ToggleName:
        assert Toggle.model_validate(name.value).name is name
        assert Toggle.model_validate({"name": name.value}).name is name
    assert IMPLEMENTED_TOGGLES == {
        ToggleName.SENSOR_DROPOUT,
        ToggleName.CONTACT_SURGE,
        ToggleName.CONTACT_GOES_QUIET,
    }
    assert len(NOT_IMPLEMENTED) == 6


def test_sensor_dropout_zeros_node_after_at_tick(s05: Scenario) -> None:
    base = run_scenario(s05)
    busiest = max(
        sorted({d.node_id for d in base.detections}),
        key=lambda n: sum(d.node_id == n for d in base.detections if d.tick >= 200),
    )
    assert sum(d.node_id == busiest for d in base.detections if d.tick >= 200) > 0
    sc = _with_toggles(s05, [{"name": "sensor_dropout", "node_id": busiest, "at_tick": 200}])
    run = run_scenario(sc)
    after = [d for d in run.detections if d.node_id == busiest and d.tick >= 200]
    assert after == []
    for track in run.tracks.values():
        assert not any(p.sensor_id == busiest and p.tick >= 200 for p in track.points)
    before = [d for d in run.detections if d.node_id == busiest and d.tick < 200]
    assert before  # the node worked until the dropout
    assert run.toggle_report.implemented == (ToggleName.SENSOR_DROPOUT,)
    assert run.toggle_report.parsed_not_implemented == ()


def test_sensor_dropout_default_tick_is_zero(s05: Scenario) -> None:
    nodes = ["hyd_1", "hyd_2", "hyd_3", "hyd_4"]
    sc = _with_toggles(s05, [{"name": "sensor_dropout", "node_id": n} for n in nodes])
    run = run_scenario(sc)
    assert run.detections == [] and run.tracks == {}


def test_contact_surge_adds_exactly_n(s05: Scenario) -> None:
    base = run_scenario(s05)
    sc = _with_toggles(s05, [{"name": "contact_surge", "n": 5, "at_tick": 50}])
    run = run_scenario(sc)
    real = lambda r: {g for g in r.truth_paths if not g.startswith("fa-")}  # noqa: E731
    added = real(run) - real(base)
    assert len(added) == 5
    assert added == set(run.toggle_report.surge_contact_ids)
    for gt_id in sorted(added):
        assert run.truth_paths[gt_id][0].tick == 50
        assert len(run.truth_paths[gt_id]) == s05.duration_ticks - 50
    hyps = {ct.gt_id: ct.hypothesis for ct in run.contact_truth.values()}
    assert all(hyps[g] in SURGE_BEHAVIORS for g in added if g in hyps)
    # The original contact's truth path is untouched by the surge.
    assert run.truth_paths["gt-1"] == base.truth_paths["gt-1"]
    again = run_scenario(sc)
    assert track_hash(again) == track_hash(run)


def test_surge_hypotheses_cover_priors() -> None:
    sc = resolve_scenario("S05")
    sc = _with_toggles(sc, [{"name": "contact_surge", "n": 60}])
    run = run_scenario(sc, ticks=5)
    hyps = {ct.gt_id: ct.hypothesis for ct in run.contact_truth.values()}
    seen = {hyps[g] for g in run.toggle_report.surge_contact_ids if g in hyps}
    assert Hypothesis.FILED_ON_PLAN in seen and len(seen) >= 3


def test_contact_goes_quiet_silences_only_that_contact(s03: Scenario) -> None:
    base = run_scenario(s03)
    sc = _with_toggles(s03, [{"name": "contact_goes_quiet", "contact_id": "gt-2", "at_tick": 100}])
    run = run_scenario(sc)
    assert run.toggle_report.implemented == (ToggleName.CONTACT_GOES_QUIET,)
    gt2 = [d for d in run.detections if d.gt_id == "gt-2"]
    assert any(d.tick < 100 and d.breathing_or_click_present for d in gt2)
    for d in gt2:
        if d.tick >= 100:
            assert not d.propeller_tonal_present and not d.breathing_or_click_present
            assert d.tonal_band.value == "none"
    for ct in run.contact_truth.values():
        if ct.gt_id == "gt-2":
            assert ct.silent_after_tick == 100
        else:
            assert ct.silent_after_tick is None
    # gt-1 and everything else are exactly as before.
    keep = lambda r: [d for d in r.detections if d.gt_id != "gt-2"]  # noqa: E731
    assert keep(run) == keep(base)


@pytest.mark.parametrize("name", NOT_IMPLEMENTED, ids=lambda n: n.value)
def test_other_six_parsed_not_implemented(s05: Scenario, name: ToggleName) -> None:
    base = run_scenario(s05)
    run = run_scenario(_with_toggles(s05, [name.value]))
    assert run.toggle_report.parsed_not_implemented == (name,)
    assert run.toggle_report.implemented == ()
    assert track_hash(run) == track_hash(base)


def test_all_six_together_leave_hash_unchanged(s05: Scenario) -> None:
    base = run_scenario(s05)
    run = run_scenario(_with_toggles(s05, [n.value for n in NOT_IMPLEMENTED]))
    assert run.toggle_report.parsed_not_implemented == tuple(NOT_IMPLEMENTED)
    assert track_hash(run) == track_hash(base)


def test_toggle_report_dedupes_in_order() -> None:
    toggles = [Toggle.model_validate(v) for v in ("fog_no_camera", "network_down", "fog_no_camera")]
    report = toggle_report(toggles)
    assert report.parsed_not_implemented == (ToggleName.FOG_NO_CAMERA, ToggleName.NETWORK_DOWN)


def test_missing_fields_raise(s05: Scenario) -> None:
    with pytest.raises(ValueError, match="node_id"):
        run_scenario(_with_toggles(s05, ["sensor_dropout"]))
    with pytest.raises(ValueError, match="n"):
        run_scenario(_with_toggles(s05, ["contact_surge"]))
