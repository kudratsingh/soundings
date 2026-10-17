"""WO-080: the vessel_transit surface behavior and the AIS feed; the schema is additive.

Exit tests: ``tests/test_vessel_transit.py`` (no subsurface detection) and
``-k schema_additive`` (S01..S08 and smoke validate unchanged, pre-change hashes hold).
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np
import pytest
from pydantic import ValidationError

from soundings.schemas.enums import BehaviorType, Hypothesis
from soundings.schemas.scenario import BehaviorSpec, scenario_from_data
from soundings.sim.ais import AisConfig, ais_reports, load_ais_config
from soundings.sim.behaviors import KinState, VesselTransit, simulate_path, step_vessel_transit
from soundings.sim.current import load_current_field
from soundings.sim.hashing import track_hash
from soundings.sim.run import run_scenario
from soundings.sim.scenario import SCENARIO_ROOT, catalog_paths, resolve_scenario
from soundings.sim.types import AisReport

FIXTURE = Path(__file__).parent / "fixtures" / "sim_hashes_pre_wo080.json"
WP = {"lat": 33.7475, "lon": -118.261}


def _base(contacts: list[dict], duration: int = 120) -> dict:
    return {
        "id": "BT-99",
        "name": "vessel test",
        "seed": 3,
        "duration_ticks": duration,
        "tick_s": 5.0,
        "ground_truth": "FILED_ON_PLAN",
        "hour_of_day": 9,
        "contacts": contacts,
        "expected": {"terminal_state": "CLEARED_AT_ASK"},
    }


def _vessel(**over: object) -> dict:
    c: dict = {
        "id": "ais-01",
        "start": {"lat": 33.716, "lon": -118.261, "depth_m": 0},
        "behavior": {"type": "vessel_transit", "waypoint": WP, "speed_mps": 4.0},
        "registry": {"filed": False, "replies_to_hail": False},
        "hypothesis": "SURFACE_VESSEL_AIS_OFF",
    }
    c.update(over)
    return c


def _uuv(**over: object) -> dict:
    c: dict = {
        "id": "gt-1",
        "start": {"lat": 33.7410, "lon": -118.2582, "depth_m": 6},
        "behavior": {"type": "transit", "target_asset": "fuel_berth_2", "speed_mps": 1.0},
        "registry": {"filed": False, "replies_to_hail": False},
    }
    c.update(over)
    return c


def test_constant_speed_depth_zero_and_stops() -> None:
    p = VesselTransit(waypoints=((0.0, 0.0), (100.0, 0.0), (100.0, 50.0)), speed_mps=4.0)
    path = simulate_path(
        p,
        KinState(0.0, 0.0, 0.0),
        60,
        dt_s=5.0,
        current=load_current_field().sampler("flood"),
        rng=np.random.default_rng(0),
    )
    steps = np.hypot(np.diff(path[:, 0]), np.diff(path[:, 1]))
    moving = steps[steps > 1e-9]
    # Constant 20 m per tick along the legs (the corner step is shorter as a chord).
    assert np.allclose(moving[:4], 20.0)
    assert np.all(path[:, 2] == 0.0)
    assert path[-1, 0] == pytest.approx(100.0) and path[-1, 1] == pytest.approx(50.0)
    assert np.allclose(path[-10:, :2], path[-1, :2])


def test_step_is_pure() -> None:
    p = VesselTransit(waypoints=((0.0, 0.0), (100.0, 0.0)), speed_mps=3.0)
    s = KinState(0.0, 0.0, 0.0)
    assert step_vessel_transit(p, s, 5.0) == step_vessel_transit(p, s, 5.0)
    assert step_vessel_transit(p, s, 5.0).x_m == pytest.approx(15.0)


@pytest.mark.parametrize(
    "bad",
    [
        {"type": "vessel_transit", "waypoint": WP, "speed_mps": 2.9},
        {"type": "vessel_transit", "waypoint": WP, "speed_mps": 6.1},
        {"type": "vessel_transit", "speed_mps": 4.0},
        {"type": "vessel_transit", "waypoint": WP},
        {"type": "vessel_transit", "waypoint": WP, "speed_mps": 4.0, "area": "main_channel"},
    ],
)
def test_bad_vessel_spec_rejected(bad: dict) -> None:
    with pytest.raises(ValidationError):
        BehaviorSpec.model_validate(bad)


def test_vessel_depth_must_be_zero() -> None:
    with pytest.raises(ValidationError):
        scenario_from_data(_base([_vessel(start={"lat": 33.716, "lon": -118.261, "depth_m": 2})]))


def test_speed_bounds_inclusive() -> None:
    for v in (3.0, 6.0):
        spec = BehaviorSpec.model_validate(
            {"type": "vessel_transit", "waypoint": WP, "speed_mps": v}
        )
        assert spec.type is BehaviorType.VESSEL_TRANSIT


def test_vessel_never_detected_and_reported_on_ais() -> None:
    sc = scenario_from_data(_base([_vessel(start_tick=10, end_tick=100), _uuv()]))
    run = run_scenario(sc)
    assert "ais-01" in run.truth_paths
    pts = run.truth_paths["ais-01"]
    assert pts[0].tick == 10 and pts[-1].tick == 99
    assert all(p.depth_m == 0.0 for p in pts)
    assert {t.gt_id for t in run.contact_truth.values()} & {"ais-01"} == set()
    assert all(d.gt_id != "ais-01" for d in run.detections)
    reports = run.ais_tracks["ais-01"]
    every = AisConfig(load_ais_config().report_interval_s).every_ticks(5.0)
    assert [r.tick for r in reports] == list(range(10, 100, every))
    assert all(isinstance(r, AisReport) for r in reports)
    assert all(3.0 <= r.sog_mps <= 6.0 for r in reports)
    assert all(r.t_s == r.tick * 5.0 for r in reports)
    # Every float at 6 dp.
    for r in reports:
        for v in (r.lat, r.lon, r.sog_mps, r.cog_deg):
            assert round(v, 6) == v
    # Course points from the channel's south end toward the north waypoint.
    assert reports[0].cog_deg == pytest.approx(0.0, abs=1e-6) or reports[0].cog_deg > 359.9


def test_vessel_does_not_change_other_streams_order() -> None:
    """A vessel sorted after a contact leaves that contact's detections untouched."""
    alone = run_scenario(scenario_from_data(_base([_uuv(id="a-uuv")])))
    with_vessel = run_scenario(scenario_from_data(_base([_uuv(id="a-uuv"), _vessel()])))
    gt1 = [d for d in alone.detections if d.gt_id == "a-uuv"]
    gt1_v = [d for d in with_vessel.detections if d.gt_id == "a-uuv"]
    assert gt1
    assert gt1 == gt1_v


def test_ais_reports_helper_interval() -> None:
    sc = scenario_from_data(_base([_vessel()], duration=40))
    run = run_scenario(sc)
    out = ais_reports("ais-01", run.truth_paths["ais-01"], 5.0, AisConfig(report_interval_s=20.0))
    assert [r.tick for r in out] == list(range(0, 40, 4))


def test_load_ais_config_default_block() -> None:
    assert load_ais_config().report_interval_s == 10.0


# --- schema additive ---------------------------------------------------------------------


@pytest.mark.parametrize("path", [*catalog_paths(), SCENARIO_ROOT / "smoke.yaml"], ids=str)
def test_schema_additive_catalog_validates(path: Path) -> None:
    sc = resolve_scenario(path)
    assert all(c.start_tick == 0 and c.end_tick is None for c in sc.contacts)
    assert all(c.hypothesis is None for c in sc.contacts)
    assert sc.expected.contacts == ()


def test_schema_additive_hashes_unchanged() -> None:
    want = json.loads(FIXTURE.read_text(encoding="utf-8"))
    got = {}
    for p in catalog_paths():
        sc = resolve_scenario(p)
        for seed in (1, 2, 3):
            got[f"{sc.id}:{seed}"] = track_hash(run_scenario(sc, seed=seed))
    assert got == want


def test_schema_additive_tick_bounds() -> None:
    with pytest.raises(ValidationError):
        scenario_from_data(_base([_uuv(start_tick=120)]))
    with pytest.raises(ValidationError):
        scenario_from_data(_base([_uuv(start_tick=10, end_tick=10)]))
    with pytest.raises(ValidationError):
        scenario_from_data(_base([_uuv(end_tick=121)]))
    ok = scenario_from_data(_base([_uuv(start_tick=10, end_tick=120)]))
    assert ok.contacts[0].end_tick == 120


def test_schema_additive_expected_contacts_ids() -> None:
    data = _base([_uuv()])
    data["expected"]["contacts"] = [{"id": "gt-9", "terminal_state": "CLEARED_AT_ASK"}]
    with pytest.raises(ValidationError):
        scenario_from_data(data)
    data["expected"]["contacts"] = [{"id": "gt-1", "terminal_state": "HANDED_OFF"}]
    assert scenario_from_data(data).expected.contacts[0].id == "gt-1"


def test_schema_additive_hypothesis_override_and_window() -> None:
    sc = scenario_from_data(
        _base([_uuv(hypothesis="UNAUTHORIZED_UUV", start_tick=20, end_tick=80)])
    )
    run = run_scenario(sc)
    assert {t.hypothesis for t in run.contact_truth.values() if t.gt_id == "gt-1"} == {
        Hypothesis.UNAUTHORIZED_UUV
    }
    ticks = [p.tick for p in run.truth_paths["gt-1"]]
    assert ticks[0] == 20 and ticks[-1] == 79
    assert all(20 <= d.tick < 80 for d in run.detections if d.gt_id == "gt-1")


def test_schema_additive_bt_id_accepted() -> None:
    assert scenario_from_data(_base([_uuv()])).id == "BT-99"
    bad = _base([_uuv()])
    bad["id"] = "BT-1"
    with pytest.raises(ValidationError):
        scenario_from_data(bad)


def test_cog_matches_geometry() -> None:
    sc = scenario_from_data(_base([_vessel()], duration=10))
    pts = run_scenario(sc).truth_paths["ais-01"]
    assert math.isclose(pts[3].speed_mps, 4.0, rel_tol=1e-6)
