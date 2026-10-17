"""WO-089 part 2: the live sensor range rings shrink when a vessel passes.

A ring is each node's passive-sonar p = 0.5 range at its current noise level (base + sea
state + vessel transits, distance-weighted). The stream sends ``sensor_ranges`` every
tick. The BT-01 case uses the WO-080 AIS feed; it skips only when that feed is not on this
branch yet.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path

import pytest

from soundings.physics_view import (
    VESSEL_FIX_MAX_AGE_S,
    SensorRange,
    node_ranges,
    physics_available,
    sensor_range_series,
    vessel_fixes_at,
)
from soundings.schemas.geo import from_local_m, to_local_m
from soundings.schemas.harbor import load_harbor
from soundings.stream.engine import stream_events
from soundings.stream.events import SensorRangesEvent

REPO = Path(__file__).resolve().parent.parent
needs_physics = pytest.mark.skipif(
    not physics_available(),
    reason="soundings/physics.py and config/physics.yaml land with WO-089 part 1",
)
NODES = tuple(load_harbor().sensor_nodes)
HYD = {n.node_id: n for n in NODES}


@dataclass(frozen=True)
class Fix:
    tick: int
    lat: float
    lon: float
    sog_mps: float


def _offset(node_id: str, dx_m: float, dy_m: float = 0.0) -> tuple[float, float]:
    n = HYD[node_id]
    x, y = to_local_m(n.lat, n.lon)
    return from_local_m(x + dx_m, y + dy_m)


def _ring(rings: list[SensorRange], node_id: str) -> float:
    return float(next(r.effective_range_m for r in rings if r.node_id == node_id))


@needs_physics
def test_a_vessel_nearby_raises_noise_and_shrinks_the_ring() -> None:
    calm = node_ranges(NODES)
    lat, lon = _offset("hyd_1", 150.0)
    busy = node_ranges(NODES, [(lat, lon, 6.0)])
    before = {r.node_id: r for r in calm}
    after = {r.node_id: r for r in busy}
    assert after["hyd_1"].nl_db > before["hyd_1"].nl_db
    assert after["hyd_1"].effective_range_m < before["hyd_1"].effective_range_m
    for nid in HYD:  # nothing grows when a vessel passes
        assert after[nid].effective_range_m <= before[nid].effective_range_m
    # The nearest node loses the most range (distance-weighted noise).
    loss = {nid: before[nid].effective_range_m - after[nid].effective_range_m for nid in HYD}
    assert max(loss, key=lambda k: loss[k]) == "hyd_1"


@needs_physics
def test_closer_vessels_shrink_the_ring_more() -> None:
    radii = []
    for d in (2000.0, 800.0, 300.0, 100.0):
        lat, lon = _offset("hyd_2", d)
        radii.append(_ring(node_ranges(NODES, [(lat, lon, 5.0)]), "hyd_2"))
    assert radii == sorted(radii, reverse=True)
    assert radii[-1] < radii[0]


def test_stale_fixes_stop_counting() -> None:
    lat, lon = _offset("hyd_1", 100.0)
    track = {"v1": [Fix(10, lat, lon, 5.0)]}
    assert vessel_fixes_at(track, 9, 5.0) == []
    assert vessel_fixes_at(track, 10, 5.0) == [(lat, lon, 5.0)]
    last_ok = 10 + int(VESSEL_FIX_MAX_AGE_S // 5.0)
    assert vessel_fixes_at(track, last_ok, 5.0) == [(lat, lon, 5.0)]
    assert vessel_fixes_at(track, last_ok + 1, 5.0) == []


@needs_physics
def test_synthetic_transit_shrinks_then_restores_the_ring() -> None:
    """A vessel crossing past hyd_1 at 6 m/s, one AIS report every two 5 s ticks."""
    reports = []
    for k in range(0, 41, 2):
        lat, lon = _offset("hyd_1", -600.0 + 30.0 * k, 80.0)
        reports.append(Fix(100 + k, lat, lon, 6.0))
    series = sensor_range_series({"v1": reports}, 220, 5.0, NODES)
    ticks = sorted(series)
    assert ticks[0] == 0
    before = _ring(series[0], "hyd_1")
    during = min(_ring(series[t], "hyd_1") for t in ticks if 100 <= t <= 140)
    after_t = max(ticks)
    assert during < before
    assert _ring(series[after_t], "hyd_1") == before  # the fix went stale; calm again
    assert sensor_range_series({"v1": reports}, 220, 5.0, NODES) == series


@needs_physics
def test_every_tick_carries_the_rings() -> None:
    stream = stream_events("S02", 3)
    ticks = [e.tick for e in stream if e.type == "tick"]
    rings = [e for e in stream if isinstance(e, SensorRangesEvent)]
    assert [e.tick for e in rings] == ticks
    for i, e in enumerate(stream):
        if isinstance(e, SensorRangesEvent):
            assert stream[i - 1].type == "tick" and stream[i - 1].tick == e.tick
    assert [r.node_id for r in rings[0].payload.nodes] == sorted(HYD)
    calm = node_ranges(NODES)
    assert [r.effective_range_m for r in rings[0].payload.nodes] == [
        r.effective_range_m for r in calm
    ]
    # No vessel feed in S02: every ring holds its base-noise radius all run.
    assert all(e.payload == rings[0].payload for e in rings)


def _bt01_feed() -> tuple[object, dict[str, list[object]]]:
    if not (REPO / "scenarios" / "BT-01.yaml").exists():
        pytest.skip("scenarios/BT-01.yaml lands with WO-080 part A (PR #33)")
    from soundings.sim.run import run_scenario
    from soundings.sim.scenario import resolve_scenario

    sim = run_scenario(resolve_scenario("BT-01"))
    ais = getattr(sim, "ais_tracks", None)
    if not ais:
        pytest.skip("SimRun.ais_tracks lands with WO-080 part A (PR #33)")
    return sim, {k: list(v) for k, v in ais.items()}


def _ring_at(series: dict[int, list[SensorRange]], tick: int, node_id: str) -> float:
    return _ring(series[max(t for t in series if t <= tick)], node_id)


@needs_physics
def test_bt01_rings_shrink_during_a_transit() -> None:
    """The owner's exit test: in BT-01 the ring of the node nearest a vessel transit is
    smaller at the transit's closest approach than ten minutes before it."""
    sim, ais = _bt01_feed()
    tick_s = float(sim.tick_s)  # type: ignore[attr-defined]
    n_ticks = int(sim.n_ticks)  # type: ignore[attr-defined]
    series = sensor_range_series(ais, n_ticks, tick_s, NODES)
    best: tuple[float, int, str] | None = None
    for vid in sorted(ais):
        for rep in ais[vid]:
            x, y = to_local_m(rep.lat, rep.lon)  # type: ignore[attr-defined]
            for n in NODES:
                nx, ny = to_local_m(n.lat, n.lon)
                key = (math.hypot(x - nx, y - ny), int(rep.tick), n.node_id)  # type: ignore[attr-defined]
                if best is None or key < best:
                    best = key
    assert best is not None
    dist, t_ca, nid = best
    t_before = t_ca - int(round(600.0 / tick_s))
    assert t_before >= 0
    r_before = _ring_at(series, t_before, nid)
    r_during = _ring_at(series, t_ca, nid)
    print(
        f"BT-01 {nid}: ring {r_before:.1f} m ten minutes before closest approach "
        f"(tick {t_before}), {r_during:.1f} m at closest approach (tick {t_ca}, {dist:.0f} m)"
    )
    assert r_during < r_before
    assert sensor_range_series(ais, n_ticks, tick_s, NODES) == series


def test_no_rings_without_part_one(monkeypatch: pytest.MonkeyPatch) -> None:
    import soundings.physics_view as pv

    monkeypatch.setattr(pv, "physics_available", lambda path=None: False)
    lat, lon = _offset("hyd_1", 100.0)
    assert pv.sensor_range_series({"v1": [Fix(0, lat, lon, 5.0)]}, 10, 5.0, NODES) == {}
    assert pv.physics_cards() == []
    stream = stream_events("S02", 3)
    assert not any(isinstance(e, SensorRangesEvent) for e in stream)
