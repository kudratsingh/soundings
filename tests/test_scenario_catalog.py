"""The scenario catalog matches docs/SPEC-SIM.md section 3 and the WO-011 catalog table.

Schema validity and harbor references are WO-010's tests/test_config_integration.py;
this file reads the YAML as plain data.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from soundings.schemas.scenario import Scenario, scenario_paths
from soundings.sim.scenario import (
    SCENARIO_ROOT,
    SMOKE_PATH,
    UnknownScenarioError,
    catalog_paths,
    resolve_scenario,
    scenario_path,
)

# SPEC-SIM section 3: id -> (ground truth, expected terminal state).
SPEC_TABLE: dict[str, tuple[str, str]] = {
    "S01": ("FILED_ON_PLAN", "CLEARED_AT_ASK"),
    "S02": ("FILED_OFF_PLAN", "LOST_VEHICLE"),
    "S03": ("FILED_ON_PLAN", "CLEARED_AT_ASK"),
    "S04": ("UNFILED_KNOWN_OPERATOR", "RESOLVED_BENIGN"),
    "S05": ("UNAUTHORIZED_UUV", "HANDED_OFF"),
    "S06": ("UNAUTHORIZED_UUV", "HANDED_OFF"),
    "S07": ("BIOLOGIC", "RESOLVED_BENIGN"),
    "S08": ("DEBRIS_OR_DRIFT", "HAZARD_NOTICE"),
}

# WO-011 catalog table: id -> (seed, behavior types, top_hypothesis, max_human_interruptions).
WO_TABLE: dict[str, tuple[int, tuple[str, ...], str | None, int]] = {
    "S01": (1, ("lawnmower",), None, 0),
    "S03": (5, ("stationary", "stationary"), None, 0),
    "S04": (7, ("transit",), "UNFILED_KNOWN_OPERATOR", 0),
    "S06": (13, ("orbit",), None, 1),
    "S07": (17, ("erratic",), "BIOLOGIC", 0),
    "S08": (19, ("drift",), "DEBRIS_OR_DRIFT", 0),
}


def _load(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def test_catalog_paths_finds_exactly_s01_to_s08() -> None:
    paths = catalog_paths()
    assert [p.name[:3] for p in paths] == list(SPEC_TABLE)
    assert SMOKE_PATH not in paths
    assert all(p.parent == SCENARIO_ROOT for p in paths)


@pytest.mark.parametrize("path", catalog_paths(), ids=lambda p: p.name[:3])
def test_catalog_matches_spec_table(path: Path) -> None:
    data = _load(path)
    sid = path.name[:3]
    assert data["id"] == sid
    ground_truth, terminal = SPEC_TABLE[sid]
    assert data["ground_truth"] == ground_truth
    assert data["expected"]["terminal_state"] == terminal


@pytest.mark.parametrize("sid", sorted(WO_TABLE))
def test_new_files_match_wo_table(sid: str) -> None:
    (path,) = [p for p in catalog_paths() if p.name.startswith(sid)]
    data = _load(path)
    seed, behaviors, top, interruptions = WO_TABLE[sid]
    assert data["seed"] == seed
    assert data["duration_ticks"] == 600
    assert data["tick_s"] == 2
    assert data["toggles"] == []
    assert tuple(c["behavior"]["type"] for c in data["contacts"]) == behaviors
    assert data["expected"].get("top_hypothesis") == top
    assert data["expected"]["max_human_interruptions"] == interruptions
    assert (data["hour_of_day"] == 2) == (sid == "S06")  # S06 at night, others daytime
    if sid != "S06":
        assert 6 <= data["hour_of_day"] <= 18


def test_registry_rows_match_wo_table() -> None:
    by_id = {p.name[:3]: _load(p) for p in catalog_paths()}
    s01 = by_id["S01"]
    (plan,) = s01["registry_plans"]
    assert plan["area"] == "inner_basin_research"
    assert s01["contacts"][0]["registry"]["plan_id"] == plan["plan_id"]
    s03 = by_id["S03"]
    assert len(s03["registry_plans"]) == 1
    assert {c["registry"]["plan_id"] for c in s03["contacts"]} == {
        s03["registry_plans"][0]["plan_id"]
    }
    s04 = by_id["S04"]["contacts"][0]
    assert s04["registry"]["filed"] is False and s04["registry"]["replies_to_hail"] is True
    known = {k["modem_id"] for k in by_id["S04"]["known_operators"]}
    assert s04["registry"]["modem_id"] in known
    assert s04["behavior"]["speed_mps"] == 0.5 and "waypoint" in s04["behavior"]
    for sid in ("S06", "S07", "S08"):
        reg = by_id[sid]["contacts"][0]["registry"]
        assert reg == {"filed": False, "replies_to_hail": False}
    orbit = by_id["S06"]["contacts"][0]["behavior"]
    assert (orbit["target_asset"], orbit["radius_m"], orbit["speed_mps"]) == (
        "fuel_berth_1",
        40,
        0.6,
    )
    assert by_id["S07"]["contacts"][0]["behavior"]["drift_weight"] == 0.6
    assert by_id["S08"]["contacts"][0]["behavior"]["jitter_mps"] == 0.05


def test_smoke_file_shape() -> None:
    data = _load(SMOKE_PATH)
    assert data["id"] == "smoke"
    assert data["ground_truth"] == "FILED_ON_PLAN"
    assert data["expected"] == {"terminal_state": "CLEARED_AT_ASK"}
    assert sorted(c["behavior"]["type"] for c in data["contacts"]) == ["stationary", "transit"]


@pytest.mark.parametrize("path", scenario_paths(SCENARIO_ROOT), ids=lambda p: p.name)
def test_every_scenario_file_validates_via_resolver(path: Path) -> None:
    by_path = resolve_scenario(path)
    assert isinstance(by_path, Scenario)
    ref = "smoke" if path == SMOKE_PATH else path.name[:3]
    assert resolve_scenario(ref) == by_path
    assert scenario_path(ref) == path


def test_resolver_rejects_unknown_refs(tmp_path: Path) -> None:
    for ref in ("nope", "S99", "s04", str(tmp_path / "missing.yaml")):
        with pytest.raises(UnknownScenarioError):
            resolve_scenario(ref)
