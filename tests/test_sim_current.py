"""Current field and tick clock (soundings.sim.current, soundings.sim.clock)."""

from __future__ import annotations

import math
from pathlib import Path

import pytest
import yaml

from soundings.schemas.geo import to_local_m
from soundings.sim.clock import TickClock
from soundings.sim.current import (
    DEFAULT_CURRENT_FIELD_PATH,
    TIDE_PHASES,
    current_field_from_mapping,
    load_current_field,
    tide_phase_for_hour,
)


def test_field_shape_and_magnitudes() -> None:
    field = load_current_field()
    assert len(field.lats) >= 2 and len(field.lons) >= 2
    assert tuple(field.vectors) == TIDE_PHASES
    for phase in TIDE_PHASES:
        for row in field.vectors[phase]:
            for u, v in row:
                assert 0.05 <= math.hypot(u, v) <= 0.3
    # The grid covers the harbor bounding box used by the track schema.
    assert field.lats[0] <= 33.71 and field.lats[-1] >= 33.77
    assert field.lons[0] <= -118.29 and field.lons[-1] >= -118.23


def test_flood_sets_in_and_ebb_sets_out() -> None:
    field = load_current_field()
    for row in field.vectors["flood"]:
        assert all(v > 0 for _, v in row)
    for row in field.vectors["ebb"]:
        assert all(v < 0 for _, v in row)


def test_nearest_cell_lookup_without_interpolation() -> None:
    field = load_current_field()
    lat0, lat1 = field.lats[2], field.lats[3]
    lon0 = field.lons[4]
    assert field.vector_at(lat0, lon0, "flood") == field.vectors["flood"][2][4]
    # Just past the midpoint goes to the next cell; the exact midpoint goes to the lower one.
    mid = (lat0 + lat1) / 2
    assert field.vector_at(mid + 1e-6, lon0, "flood") == field.vectors["flood"][3][4]
    assert field.vector_at(mid - 1e-6, lon0, "flood") == field.vectors["flood"][2][4]
    # Outside the grid clamps to the edge cell.
    assert field.vector_at(10.0, -200.0, "ebb") == field.vectors["ebb"][0][0]
    assert field.vector_at(50.0, 0.0, "ebb") == field.vectors["ebb"][-1][-1]
    with pytest.raises(ValueError):
        field.vector_at(33.74, -118.26, "spring")  # type: ignore[arg-type]


def test_local_sampler_matches_latlon_lookup() -> None:
    field = load_current_field()
    sampler = field.sampler("slack")
    # The local origin (0, 0) is lat 33.74, lon -118.265.
    assert sampler(0.0, 0.0) == field.vector_at(33.74, -118.265, "slack")
    for lat, lon in ((33.752, -118.265), (33.718, -118.231), (33.776, -118.299)):
        x, y = to_local_m(lat, lon)
        assert sampler(x, y) == field.vector_at(lat, lon, "slack")


def test_tide_phase_for_hour() -> None:
    field = load_current_field()
    phases = [tide_phase_for_hour(h) for h in range(24)]
    assert phases == [field.phase_for_hour(h) for h in range(24)]
    assert set(phases) == set(TIDE_PHASES)
    assert tide_phase_for_hour(2) == "flood"  # S05, S06
    assert tide_phase_for_hour(14) == "flood"  # S02
    for bad in (-1, 24, True):
        with pytest.raises(ValueError):
            tide_phase_for_hour(bad)


def test_bad_field_files_raise(tmp_path: Path) -> None:
    raw = yaml.safe_load(DEFAULT_CURRENT_FIELD_PATH.read_text(encoding="utf-8"))
    dup = {**raw, "hours": {**raw["hours"], "slack": [*raw["hours"]["slack"], 0]}}
    with pytest.raises(ValueError, match="two phases"):
        current_field_from_mapping(dup)
    short = {**raw, "lats": raw["lats"][:-1]}
    with pytest.raises(ValueError, match="grid"):
        current_field_from_mapping(short)
    missing = {k: v for k, v in raw.items() if k != "hours"}
    with pytest.raises(ValueError):
        current_field_from_mapping(missing)
    path = tmp_path / "cf.yaml"
    path.write_text("- 1\n", encoding="utf-8")
    with pytest.raises(ValueError):
        load_current_field(path)


def test_tick_clock() -> None:
    clock = TickClock(tick_s=2.0)
    assert clock.tick == 0 and clock.t_s == 0.0
    assert clock.advance() == 1 and clock.t_s == 2.0
    assert clock.advance(9) == 10 and clock.t_s == 20.0
    with pytest.raises(ValueError):
        TickClock(tick_s=0.0)
    with pytest.raises(ValueError):
        clock.advance(-1)
