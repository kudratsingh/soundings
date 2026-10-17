"""WO-022 drift search box: advection and the radius growth formula (exit test)."""

from __future__ import annotations

import math

import pytest

from soundings.matcher import PositionFix, drift_search_box, load_match_config
from soundings.schemas.geo import distance_m, from_local_m, to_local_m
from soundings.sim.current import load_current_field

CFG = load_match_config()
ROW = "00000000000000c1"
ANCHOR = PositionFix(lat=33.7405, lon=-118.27, tick=100, sigma_m=12.0, row_id="00000000000000b1")


class Uniform:
    """A uniform current (u east, v north) in m/s."""

    def __init__(self, u: float, v: float) -> None:
        self.u, self.v = u, v

    def vector_at(self, lat: float, lon: float, tide_phase: object) -> tuple[float, float]:
        return self.u, self.v


def test_zero_current_keeps_the_centre_and_grows_the_radius() -> None:
    box = drift_search_box(ANCHOR, 400, 2.0, Uniform(0.0, 0.0), "slack", CFG, assumption_row_id=ROW)
    elapsed = 300 * 2.0
    sb = CFG.search_box
    assert distance_m(box.center_lat, box.center_lon, ANCHOR.lat, ANCHOR.lon) < 0.2
    assert box.radius_m == pytest.approx(
        sb.base_radius_m + ANCHOR.sigma_m + sb.growth_mps * elapsed
    )
    assert box.assumption_row_id == ROW and box.anchor == ANCHOR and box.to_tick == 400


def test_uniform_east_current_moves_the_centre_120_m_east() -> None:
    box = drift_search_box(ANCHOR, 400, 2.0, Uniform(0.2, 0.0), "flood", CFG, assumption_row_id=ROW)
    ax, ay = to_local_m(ANCHOR.lat, ANCHOR.lon)
    cx, cy = to_local_m(box.center_lat, box.center_lon)
    assert cx - ax == pytest.approx(120.0, abs=1.0)
    assert cy - ay == pytest.approx(0.0, abs=1.0)


def test_contains_centre_but_not_one_metre_past_the_radius() -> None:
    box = drift_search_box(ANCHOR, 250, 2.0, Uniform(0.1, -0.05), "ebb", CFG, assumption_row_id=ROW)
    assert box.contains(box.center_lat, box.center_lon)
    cx, cy = to_local_m(box.center_lat, box.center_lon)
    for angle in (0.0, 1.0, 2.5, 4.0):
        inside = from_local_m(
            cx + (box.radius_m - 1.0) * math.cos(angle), cy + (box.radius_m - 1.0) * math.sin(angle)
        )
        outside = from_local_m(
            cx + (box.radius_m + 1.0) * math.cos(angle), cy + (box.radius_m + 1.0) * math.sin(angle)
        )
        assert box.contains(*inside)
        assert not box.contains(*outside)


def test_bounding_box_encloses_the_circle() -> None:
    box = drift_search_box(ANCHOR, 300, 2.0, Uniform(0.0, 0.1), "flood", CFG, assumption_row_id=ROW)
    assert box.min_lat < box.center_lat < box.max_lat
    assert box.min_lon < box.center_lon < box.max_lon
    assert distance_m(box.center_lat, box.min_lon, box.center_lat, box.max_lon) == pytest.approx(
        2 * box.radius_m, abs=0.5
    )


def test_no_elapsed_time_is_base_plus_sigma() -> None:
    box = drift_search_box(ANCHOR, 100, 2.0, Uniform(0.3, 0.3), "flood", CFG, assumption_row_id=ROW)
    assert box.radius_m == CFG.search_box.base_radius_m + ANCHOR.sigma_m
    assert (box.center_lat, box.center_lon) == (ANCHOR.lat, ANCHOR.lon)


def test_real_current_field_is_deterministic() -> None:
    field = load_current_field()
    a = drift_search_box(ANCHOR, 400, 2.0, field, "flood", CFG, assumption_row_id=ROW)
    b = drift_search_box(ANCHOR, 400, 2.0, field, "flood", CFG, assumption_row_id=ROW)
    assert a == b
    # flood sets north in the inner harbor
    assert a.center_lat > ANCHOR.lat
