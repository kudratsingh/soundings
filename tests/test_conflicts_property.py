"""WO-081 property test: ``detect_conflicts`` agrees with a brute-force reference.

Random windows, depth bands and zone pairs: a conflict is returned iff shapely
``intersection().area > 0`` (no tree) and plain interval arithmetic say all three of area,
depth band and window overlap. Detection is symmetric with equal ``overlap_minutes``.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

from hypothesis import given, settings
from hypothesis import strategies as st
from shapely import Polygon

from soundings.registry.conflicts import PlanFootprint, detect_conflicts
from soundings.registry.models import load_conflict_config
from soundings.schemas import load_harbor
from soundings.schemas.geo import to_local_m

HARBOR = load_harbor()
CFG = load_conflict_config()
ZONES = sorted(z.zone_id for z in HARBOR.zones)
BASE = datetime(2026, 10, 17, 6, 0, tzinfo=timezone(timedelta(hours=-7)))
OFFSETS = [timezone(timedelta(hours=h)) for h in (-7, 0, 2)]


def _poly(zone_id: str, buffer_m: float) -> Polygon:
    base = Polygon([to_local_m(lat, lon) for lat, lon in HARBOR.zone(zone_id).polygon])
    return base.buffer(buffer_m) if buffer_m else base


@st.composite
def footprints(draw: st.DrawFn, plan_id: str) -> PlanFootprint:
    start_s = draw(st.integers(min_value=0, max_value=10 * 3600))
    length_s = draw(st.integers(min_value=0, max_value=6 * 3600))
    lo = draw(st.integers(min_value=0, max_value=30))
    hi = draw(st.integers(min_value=lo, max_value=40))
    tz = draw(st.sampled_from(OFFSETS))
    start = (BASE + timedelta(seconds=start_s)).astimezone(tz)
    return PlanFootprint(
        plan_id=plan_id,
        area=draw(st.sampled_from(ZONES)),
        start_at=start,
        end_at=start + timedelta(seconds=length_s),
        depth_min_m=float(lo),
        depth_max_m=float(hi),
    )


def reference(a: PlanFootprint, b: PlanFootprint, buffer_m: float) -> tuple[bool, int]:
    area = _poly(a.area, buffer_m).intersection(_poly(b.area, buffer_m)).area > 0
    depth = min(a.depth_max_m, b.depth_max_m) - max(a.depth_min_m, b.depth_min_m) > 0
    span = (min(a.end_at, b.end_at) - max(a.start_at, b.start_at)).total_seconds()
    minutes = math.floor(span / 60) if span > 0 else 0
    return area and depth and minutes > 0, minutes


@settings(max_examples=300, deadline=None)
@given(
    a=footprints("P-A"),
    b=footprints("P-B"),
    buffer_m=st.sampled_from([0.0, 50.0]),
)
def test_conflict_iff_all_three_overlap(
    a: PlanFootprint, b: PlanFootprint, buffer_m: float
) -> None:
    cfg = CFG.model_copy(update={"area_buffer_m": buffer_m})
    expect, minutes = reference(a, b, buffer_m)
    ab = detect_conflicts(a, [b], HARBOR, cfg)
    ba = detect_conflicts(b, [a], HARBOR, cfg)
    assert bool(ab) is expect
    assert bool(ba) is expect  # symmetry
    if expect:
        assert ab[0].overlap_minutes == ba[0].overlap_minutes == minutes
        assert ab[0].overlap_start == ba[0].overlap_start
        assert ab[0].overlap_end == ba[0].overlap_end


@settings(max_examples=100, deadline=None)
@given(
    new=footprints("P-NEW"),
    existing=st.lists(
        st.integers(min_value=0, max_value=999), min_size=0, max_size=12, unique=True
    ).flatmap(lambda ids: st.tuples(*[footprints(f"P-{i:03d}") for i in ids])),
)
def test_many_matches_pairwise_reference(
    new: PlanFootprint, existing: tuple[PlanFootprint, ...]
) -> None:
    found = detect_conflicts(new, list(existing), HARBOR, CFG)
    expected = sorted(
        (f for f in existing if reference(new, f, CFG.area_buffer_m)[0]),
        key=lambda f: (f.start_at, f.plan_id),
    )
    assert [c.other_plan_id for c in found] == [f.plan_id for f in expected]
