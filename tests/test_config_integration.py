"""Integration tests over the real files on disk: harbor map, scenarios, and config (WO-010).

Every scenario file under ``scenarios/`` (and ``scenarios/live/``) is one parametrized case,
so files added later are covered automatically.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from soundings.canonical import canonical_json
from soundings.schemas import (
    Harbor,
    Scenario,
    distance_m,
    load_harbor,
    load_ladder_config,
    load_likelihood_spec,
    load_priors,
    load_scenario,
    load_scenarios,
    load_standing_orders,
)
from soundings.schemas.scenario import scenario_paths

REPO_ROOT = Path(__file__).resolve().parent.parent
SCENARIO_ROOT = REPO_ROOT / "scenarios"
SCENARIO_FILES = scenario_paths(SCENARIO_ROOT)


@pytest.fixture(scope="module")
def harbor() -> Harbor:
    return load_harbor()


@pytest.fixture(scope="module")
def scenarios(harbor: Harbor) -> dict[str, Scenario]:
    return {s.id: s for s in load_scenarios(SCENARIO_ROOT, harbor)}


def test_harbor_required_ids(harbor: Harbor) -> None:
    zone_ids = {z.zone_id for z in harbor.zones}
    assert {
        "main_channel",
        "inner_harbor_west",
        "inner_basin_research",
        "fuel_berths",
        "pier_400_approach",
        "vincent_thomas_bridge",
    } <= zone_ids
    assert "inner_harbor_west" in harbor.basins()
    asset_ids = {a.asset_id for a in harbor.assets}
    assert {"fuel_berth_1", "fuel_berth_2", "pier_400_gate"} <= asset_ids
    pilings = [a for a in harbor.assets if a.asset_kind == "piling"]
    assert {"vtb_piling_west", "vtb_piling_east"} <= {a.asset_id for a in pilings}
    assert 3 <= len(harbor.sensor_nodes) <= 4
    for node in harbor.sensor_nodes:
        assert node.range_m == 400.0 and node.health == 1.0
    assert harbor.name == "Port of Los Angeles (simplified for demonstration)"
    crit = {z.zone_id: z.criticality for z in harbor.zones}
    assert crit["fuel_berths"] == 0.9 and crit["inner_basin_research"] == 0.3
    assert harbor.asset("fuel_berth_2").criticality == 0.95


@pytest.mark.parametrize("path", SCENARIO_FILES, ids=[p.name for p in SCENARIO_FILES])
def test_every_scenario_parses(path: Path, harbor: Harbor) -> None:
    sc = load_scenario(path, harbor)
    assert isinstance(sc, Scenario)
    back = Scenario.model_validate_json(sc.model_dump_json())
    assert back == sc
    assert canonical_json(back) == canonical_json(sc)


def test_scenario_files_found() -> None:
    names = {p.name for p in SCENARIO_FILES}
    assert "S02-filed-vehicle-gone-silent.yaml" in names
    assert "S05-unfiled-closing-fuel-berth.yaml" in names
    assert SCENARIO_FILES == sorted(SCENARIO_FILES)


def test_scenario_refs_resolve(harbor: Harbor, scenarios: dict[str, Scenario]) -> None:
    assert scenarios
    for sc in scenarios.values():
        for plan in sc.registry_plans:
            harbor.zone(plan.area)
        for c in sc.contacts:
            if c.behavior.target_asset is not None:
                harbor.asset(c.behavior.target_asset)
            if c.behavior.area is not None:
                harbor.zone(c.behavior.area)
            lat, lon = c.start.lat, c.start.lon
            assert harbor.zone_at(lat, lon) is not None, (sc.id, c.id)
            assert any(
                distance_m(lat, lon, n.lat, n.lon) <= 2 * n.range_m for n in harbor.sensor_nodes
            ), (sc.id, c.id)
    s02 = scenarios["S02"].contacts[0].start
    assert harbor.zone("inner_basin_research").contains(s02.lat, s02.lon)
    zone = harbor.zone_at(s02.lat, s02.lon)
    assert zone is not None and zone.zone_id == "inner_basin_research"
    s05 = scenarios["S05"].contacts[0].start
    zone = harbor.zone_at(s05.lat, s05.lon)
    assert zone is not None and zone.zone_id == "main_channel"


def test_s05_geometry_reachable(harbor: Harbor, scenarios: dict[str, Scenario]) -> None:
    s05 = scenarios["S05"]
    c = s05.contacts[0]
    assert c.behavior.target_asset == "fuel_berth_2" and c.behavior.speed_mps is not None
    berth = harbor.asset("fuel_berth_2")
    d = distance_m(c.start.lat, c.start.lon, berth.lat, berth.lon)
    assert 700.0 <= d <= 1100.0
    assert d / c.behavior.speed_mps < s05.duration_ticks * s05.tick_s
    assert any(distance_m(berth.lat, berth.lon, n.lat, n.lon) <= 400.0 for n in harbor.sensor_nodes)


def test_every_config_validates(harbor: Harbor) -> None:
    spec = load_likelihood_spec()
    assert len(spec.features) >= 10
    priors = load_priors()
    assert len(priors.base) == 8
    ladder = load_ladder_config()
    assert [r.value for r in ladder.lives_on] == ["ask", "hand_to_person"]
    orders = load_standing_orders(harbor)
    assert set(orders.orders.scope.slow.basins) <= set(harbor.basins())
