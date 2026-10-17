"""WO-081 unit tests: the pure conflict detector and its rules.

Owner directive (verbatim): "table-driven tests (overlap in all three -> conflict; any one
disjoint -> none)". ``test_three_way_table`` proves it.

Hard rules exercised: CLAUDE.md 2.4 (no clock, sorted tree hits) and 2.2 (no model import)
by ``test_no_clock_no_model``.
"""

from __future__ import annotations

import ast
import itertools
import random
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from shapely import Polygon

from soundings.registry import conflicts as conflicts_mod
from soundings.registry.conflicts import (
    Conflict,
    ConflictReason,
    PlanFootprint,
    conflicts_among,
    detect_conflicts,
    footprint_of,
    zone_geometry,
)
from soundings.registry.models import (
    ConflictConfig,
    PlanRejected,
    PlanSource,
    PlanStatus,
    PlanView,
    RejectReason,
    load_conflict_config,
)
from soundings.registry.store import RegistryStore
from soundings.schemas import load_harbor
from soundings.schemas.geo import to_local_m

HARBOR = load_harbor()
CFG = load_conflict_config()
PDT = timezone(timedelta(hours=-7))
NOW = datetime(2026, 10, 17, 20, 0, tzinfo=UTC)
ALL = frozenset(ConflictReason)


def cfg_with(buffer_m: float) -> ConflictConfig:
    return CFG.model_copy(update={"area_buffer_m": buffer_m})


def at(hh: int, mm: int = 0, ss: int = 0, tz: timezone = PDT) -> datetime:
    return datetime(2026, 10, 17, hh, mm, ss, tzinfo=tz)


def fp(
    plan_id: str,
    area: str,
    start: datetime,
    end: datetime,
    depth: tuple[float, float] = (0.0, 12.0),
) -> PlanFootprint:
    return PlanFootprint(
        plan_id=plan_id,
        area=area,
        start_at=start,
        end_at=end,
        depth_min_m=depth[0],
        depth_max_m=depth[1],
    )


def view(plan_id: str, **overrides: Any) -> PlanView:
    data: dict[str, Any] = {
        "plan_id": plan_id,
        "operator": "Harbor Survey Co (demo)",
        "vehicle": "ROV-Blue-1",
        "modem_id": "MOD-1234",
        "area": "inner_basin_research",
        "start_tick": None,
        "end_tick": None,
        "start_at": at(13),
        "end_at": at(16),
        "contact_channel": "radio ch 16",
        "status": PlanStatus.ACTIVE,
        "source": PlanSource.API,
    }
    data.update(overrides)
    return PlanView(**data)


# --- the directive's table ---------------------------------------------------------------

A = fp("P-A", "inner_basin_research", at(13), at(16), (0.0, 12.0))
SAME_ZONE, FAR_ZONE = "inner_basin_research", "pier_400_approach"
OVER_WINDOW, APART_WINDOW = (at(14), at(18)), (at(17), at(18))
OVER_DEPTH, APART_DEPTH = (5.0, 20.0), (15.0, 25.0)

THREE_WAY: list[tuple[str, str, tuple[datetime, datetime], tuple[float, float], float, bool]] = [
    # name, zone, window, depth band, buffer, expect conflict
    ("all_three_overlap", SAME_ZONE, OVER_WINDOW, OVER_DEPTH, 50.0, True),
    ("area_disjoint", FAR_ZONE, OVER_WINDOW, OVER_DEPTH, 50.0, False),
    ("depth_disjoint", SAME_ZONE, OVER_WINDOW, APART_DEPTH, 50.0, False),
    ("window_disjoint", SAME_ZONE, APART_WINDOW, OVER_DEPTH, 50.0, False),
    ("area_and_depth_disjoint", FAR_ZONE, OVER_WINDOW, APART_DEPTH, 50.0, False),
    ("area_and_window_disjoint", FAR_ZONE, APART_WINDOW, OVER_DEPTH, 50.0, False),
    ("depth_and_window_disjoint", SAME_ZONE, APART_WINDOW, APART_DEPTH, 50.0, False),
    ("all_three_disjoint", FAR_ZONE, APART_WINDOW, APART_DEPTH, 50.0, False),
    # edge rows: half-open rules, touching is not overlapping
    ("windows_touch_at_1600", SAME_ZONE, (at(16), at(18)), OVER_DEPTH, 50.0, False),
    ("depths_touch_at_12m", SAME_ZONE, OVER_WINDOW, (12.0, 20.0), 50.0, False),
    ("zones_touch_unbuffered", "inner_harbor_west", OVER_WINDOW, OVER_DEPTH, 0.0, False),
]


@pytest.mark.parametrize(
    ("zone", "window", "depth", "buffer_m", "expect"),
    [row[1:] for row in THREE_WAY],
    ids=[row[0] for row in THREE_WAY],
)
def test_three_way_table(
    zone: str,
    window: tuple[datetime, datetime],
    depth: tuple[float, float],
    buffer_m: float,
    expect: bool,
) -> None:
    new = fp("P-B", zone, window[0], window[1], depth)
    found = detect_conflicts(new, [A], HARBOR, cfg_with(buffer_m))
    if expect:
        assert len(found) == 1
        assert found[0].reasons == {
            ConflictReason.AREA,
            ConflictReason.DEPTH,
            ConflictReason.WINDOW,
        }
        assert found[0].other_plan_id == "P-A"
    else:
        assert found == ()


def test_three_way_table_covers_every_disjoint_subset() -> None:
    core = THREE_WAY[:8]
    subsets = {
        (zone == FAR_ZONE, depth == APART_DEPTH, window == APART_WINDOW)
        for _, zone, window, depth, _, _ in core
    }
    assert subsets == set(itertools.product((False, True), repeat=3))
    assert [row[5] for row in THREE_WAY] == [True] + [False] * (len(THREE_WAY) - 1)


def test_windows_touch_at_1400_edge() -> None:
    a = fp("P-A", SAME_ZONE, at(12), at(14))
    b = fp("P-B", SAME_ZONE, at(14), at(16))
    assert detect_conflicts(b, [a], HARBOR, CFG) == ()


# --- exact overlap fields ----------------------------------------------------------------


def test_overlap_minutes_exact() -> None:
    b = fp("P-B", SAME_ZONE, at(14), at(18), (5.0, 20.0))
    (c,) = detect_conflicts(b, [A], HARBOR, CFG)
    assert c.overlap_minutes == 120
    assert c.overlap_start == at(14) and c.overlap_end == at(16)
    assert c.overlap_start.utcoffset() == timedelta(hours=-7)
    assert c.depth_overlap_m == 7.0
    assert c.area_overlap_m2 > 0


def test_mixed_offsets_compare_as_instants() -> None:
    # 14:00-18:00 PDT given in UTC (21:00-01:00Z)
    b = fp(
        "P-B",
        SAME_ZONE,
        datetime(2026, 10, 17, 21, 0, tzinfo=UTC),
        datetime(2026, 10, 18, 1, 0, tzinfo=UTC),
        (5.0, 20.0),
    )
    (c,) = detect_conflicts(b, [A], HARBOR, CFG)
    assert c.overlap_minutes == 120
    assert c.overlap_start == at(14) and c.overlap_end == at(16)
    assert c.overlap_start.utcoffset() == timedelta(0)  # in the new plan's offset


def test_seconds_are_floored() -> None:
    b = fp("P-B", SAME_ZONE, at(15, 57, 1), at(18), (5.0, 20.0))  # 2 min 59 s overlap
    (c,) = detect_conflicts(b, [A], HARBOR, CFG)
    assert c.overlap_minutes == 2
    short = fp("P-C", SAME_ZONE, at(15, 59, 1), at(18), (5.0, 20.0))  # 59 s overlap
    assert detect_conflicts(short, [A], HARBOR, CFG) == ()


def test_job_type_default_depth_from_config() -> None:
    for job in sorted(CFG.depth_defaults_m):
        f = footprint_of(view("P-J", job_type=job), CFG)
        assert (f.depth_min_m, f.depth_max_m) == CFG.depth_band(job)
    given = footprint_of(view("P-K", job_type="dredge", depth_min_m=1.0, depth_max_m=2.0), CFG)
    assert (given.depth_min_m, given.depth_max_m) == (1.0, 2.0)


def test_job_type_defaults_decide_depth_overlap() -> None:
    hull = footprint_of(view("P-H", job_type="hull_cleaning"), CFG)
    dredge = footprint_of(view("P-D", job_type="dredge", start_at=at(14), end_at=at(18)), CFG)
    (c,) = detect_conflicts(dredge, [hull], HARBOR, CFG)
    h_lo, h_hi = CFG.depth_band("hull_cleaning")
    d_lo, d_hi = CFG.depth_band("dredge")
    assert c.depth_overlap_m == round(min(h_hi, d_hi) - max(h_lo, d_lo), 6)


def _touching_pair() -> tuple[str, str]:
    polys = {
        z.zone_id: Polygon([to_local_m(lat, lon) for lat, lon in z.polygon]) for z in HARBOR.zones
    }
    for a, b in itertools.combinations(sorted(polys), 2):
        if polys[a].touches(polys[b]) and polys[a].intersection(polys[b]).area == 0:
            return a, b
    raise AssertionError("no adjacent zone pair in config/harbor.geojson")


def test_area_buffer_makes_adjacent_zones_conflict() -> None:
    za, zb = _touching_pair()
    a = fp("P-A", za, at(13), at(16))
    b = fp("P-B", zb, at(14), at(18))
    assert CFG.area_buffer_m == 50
    assert len(detect_conflicts(b, [a], HARBOR, cfg_with(50.0))) == 1
    assert detect_conflicts(b, [a], HARBOR, cfg_with(0.0)) == ()


def test_nested_zone_conflicts_without_buffer() -> None:
    # config/harbor.geojson nests fuel_berths inside main_channel (not inner_harbor_west)
    outer = Polygon([to_local_m(*p) for p in HARBOR.zone("main_channel").polygon])
    inner = Polygon([to_local_m(*p) for p in HARBOR.zone("fuel_berths").polygon])
    assert outer.contains(inner)
    a = fp("P-A", "main_channel", at(13), at(16))
    b = fp("P-B", "fuel_berths", at(14), at(18))
    (c,) = detect_conflicts(b, [a], HARBOR, cfg_with(0.0))
    assert c.area_overlap_m2 == round(inner.area, 6)


def test_ordered_by_start_then_plan_id_even_if_shuffled() -> None:
    existing = [
        fp(f"P-{i:02d}", SAME_ZONE, at(10 + (i % 4)), at(18), (0.0, 30.0)) for i in range(12)
    ]
    new = fp("P-NEW", SAME_ZONE, at(9), at(20))
    expected = [f.plan_id for f in sorted(existing, key=lambda f: (f.start_at, f.plan_id))]
    rng = random.Random(7)
    for _ in range(5):
        shuffled = list(existing)
        rng.shuffle(shuffled)
        got = detect_conflicts(new, shuffled, HARBOR, CFG)
        assert [c.other_plan_id for c in got] == expected


def test_own_plan_id_is_skipped() -> None:
    assert detect_conflicts(A, [A], HARBOR, CFG) == ()


def test_zone_geometry_cached_and_buffered() -> None:
    g0 = zone_geometry(HARBOR, SAME_ZONE, 0.0)
    g50 = zone_geometry(HARBOR, SAME_ZONE, 50.0)
    assert zone_geometry(HARBOR, SAME_ZONE, 50.0) is g50
    assert g50.area > g0.area
    assert g50.contains(g0)


def test_conflicts_among_matches_sequential_detection() -> None:
    fps = [
        fp("P-1", SAME_ZONE, at(13), at(16)),
        fp("P-2", SAME_ZONE, at(14), at(18)),
        fp("P-3", FAR_ZONE, at(14), at(18)),
        fp("P-4", "inner_harbor_west", at(15), at(17)),
    ]
    found = conflicts_among(fps, HARBOR, CFG)
    pairs = [(c.plan_id, c.other_plan_id) for c in found]
    for i, new in enumerate(fps):
        for c in detect_conflicts(new, fps[:i], HARBOR, CFG):
            assert (c.plan_id, c.other_plan_id) in pairs
            assert (c.other_plan_id, c.plan_id) in pairs
    assert len(pairs) == len(set(pairs))
    assert ("P-3", "P-1") not in pairs


def test_depth_band_inverted_rejects_and_writes_nothing(tmp_path: Path) -> None:
    store = RegistryStore(tmp_path / "c.db", harbor=HARBOR, now=lambda: NOW)
    base: dict[str, Any] = {
        "operator": "Harbor Survey Co (demo)",
        "vehicle": "ROV-Blue-1",
        "modem_id": "MOD-1234",
        "area": SAME_ZONE,
        "start_at": at(13),
        "end_at": at(16),
        "contact_channel": "radio ch 16",
    }
    store.create({**base, "plan_id": "P-OK"})
    for bad in ({"depth_min_m": 20.0, "depth_max_m": 5.0}, {"depth_max_m": 5.0}):
        with pytest.raises(PlanRejected) as info:
            store.create({**base, "plan_id": "P-BAD", **bad})
        assert info.value.reason is RejectReason.DEPTH_BAND_INVERTED
    assert store.count() == 1
    assert store.conflicts_of("P-OK") == []
    assert store.get("P-OK") is not None and store.get("P-OK").status is PlanStatus.ACTIVE  # type: ignore[union-attr]
    # window inversion is reported first
    with pytest.raises(PlanRejected) as info:
        store.create(
            {**base, "start_at": at(17), "end_at": at(16), "depth_min_m": 9, "depth_max_m": 1}
        )
    assert info.value.reason is RejectReason.WINDOW_INVERTED
    store.close()


# --- rule proof 2.4 / 2.2 ------------------------------------------------------------------

FORBIDDEN_IMPORTS = ("random", "time", "soundings.llm", "anthropic")


def test_no_clock_no_model() -> None:
    src = Path(conflicts_mod.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert not alias.name.startswith(FORBIDDEN_IMPORTS), alias.name
        if isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            assert not mod.startswith(FORBIDDEN_IMPORTS), mod
        if isinstance(node, ast.Attribute):
            assert node.attr not in ("now", "utcnow", "today"), ast.unparse(node)
            if isinstance(node.value, ast.Name):
                assert node.value.id not in ("time", "random"), ast.unparse(node)
    assert "datetime.now" not in src
    assert "anthropic" not in src
    # STRtree hits are sorted before use: a tree-order-reversed input gives the same output
    existing = [
        fp(f"P-{i:02d}", z, at(12 + i % 3), at(18), (0.0, 30.0))
        for i, z in enumerate(sorted(zz.zone_id for zz in HARBOR.zones) * 3)
    ]
    new = fp("P-NEW", "main_channel", at(11), at(19), (0.0, 30.0))
    forward = detect_conflicts(new, existing, HARBOR, CFG)
    backward = detect_conflicts(new, list(reversed(existing)), HARBOR, CFG)
    assert forward == backward and len(forward) > 1


def test_conflict_model_is_frozen() -> None:
    c = Conflict(
        plan_id="a",
        other_plan_id="b",
        reasons=ALL,
        overlap_start=at(14),
        overlap_end=at(15),
        overlap_minutes=60,
        depth_overlap_m=1.0,
        area_overlap_m2=1.0,
    )
    with pytest.raises(ValueError):
        c.overlap_minutes = 3  # type: ignore[misc]
