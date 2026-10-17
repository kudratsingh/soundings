"""WO-022 re-acquisition matching (SPEC-INVESTIGATION 9.9) on synthetic lost tracks."""

from __future__ import annotations

from soundings.matcher import LostTrack, PositionFix, load_match_config, match_reacquisition
from soundings.schemas.geo import from_local_m, to_local_m

CFG = load_match_config()
TICK_S = 2.0
ORIGIN = (33.7405, -118.27)


class Uniform:
    def __init__(self, u: float, v: float) -> None:
        self.u, self.v = u, v

    def vector_at(self, lat: float, lon: float, tide_phase: object) -> tuple[float, float]:
        return self.u, self.v


def fix(dx: float, dy: float, tick: int, sigma: float = 10.0) -> PositionFix:
    x, y = to_local_m(*ORIGIN)
    lat, lon = from_local_m(x + dx, y + dy)
    return PositionFix(lat=lat, lon=lon, tick=tick, sigma_m=sigma)


def lost(
    cid: str, dx: float, dy: float, tick: int, v: tuple[float, float] = (0.0, 0.0)
) -> LostTrack:
    return LostTrack(contact_id=cid, last_fix=fix(dx, dy, tick), velocity_mps=v, last_tick=tick)


def test_drifting_track_reappears_inside_the_gate() -> None:
    field = Uniform(0.2, 0.0)
    old = lost("c-0003", 0.0, 0.0, 100, v=(0.2, 0.0))
    first = fix(0.2 * 40 + 5.0, 3.0, 120)  # 20-tick gap = 40 s of 0.2 m/s drift, plus noise
    got = match_reacquisition(first, 0.2, [old], 125, TICK_S, field, "flood", CFG, new_id="c-0006")
    assert got is not None
    assert (got.old_id, got.new_id) == ("c-0003", "c-0006")
    assert got.gate_m == CFG.reacquire.gate_base_m + CFG.reacquire.gate_growth_mps * 40.0
    assert 0.0 < got.match_confidence <= 1.0
    assert got.match_confidence == round(1.0 - got.distance_m / got.gate_m, 6)
    assert "c-0003" in got.reason


def test_drift_prediction_used_when_dead_reckoning_is_wrong() -> None:
    field = Uniform(0.0, 0.25)
    old = lost("c-0003", 0.0, 0.0, 100, v=(1.0, 0.0))  # was under way east; now drifting north
    first = fix(0.0, 0.25 * 60, 130)
    got = match_reacquisition(first, None, [old], 130, TICK_S, field, "flood", CFG, new_id="c-0007")
    assert got is not None and "drift" in got.reason


def test_dead_reckoning_used_for_a_vehicle_under_way() -> None:
    old = lost("c-0001", 0.0, 0.0, 100, v=(1.0, 0.0))
    first = fix(60.0, 0.0, 130)
    got = match_reacquisition(
        first, 1.0, [old], 130, TICK_S, Uniform(0.0, 0.0), "slack", CFG, new_id="c-0004"
    )
    assert got is not None and "dead-reckoning" in got.reason and got.distance_m < 1.0


def test_outside_the_gate_is_rejected() -> None:
    old = lost("c-0003", 0.0, 0.0, 100)
    first = fix(200.0, 0.0, 120)
    assert (
        match_reacquisition(
            first, None, [old], 120, TICK_S, Uniform(0, 0), "slack", CFG, new_id="c-0009"
        )
        is None
    )


def test_inconsistent_speed_is_rejected() -> None:
    old = lost("c-0003", 0.0, 0.0, 100, v=(0.2, 0.0))
    first = fix(8.0, 0.0, 120)
    tol = CFG.reacquire.speed_tol_mps
    ok = match_reacquisition(
        first, 0.2 + tol, [old], 120, TICK_S, Uniform(0.2, 0), "flood", CFG, new_id="c-0006"
    )
    bad = match_reacquisition(
        first, 0.2 + tol + 0.01, [old], 120, TICK_S, Uniform(0.2, 0), "flood", CFG, new_id="c-0006"
    )
    assert ok is not None
    assert bad is None


def test_past_max_gap_there_is_no_match() -> None:
    gap_ticks = int(CFG.reacquire.max_gap_s / TICK_S)
    old = lost("c-0003", 0.0, 0.0, 100)
    at_limit = fix(0.0, 0.0, 100 + gap_ticks)
    past = fix(0.0, 0.0, 100 + gap_ticks + 1)
    field = Uniform(0.0, 0.0)
    assert (
        match_reacquisition(
            at_limit, None, [old], at_limit.tick, TICK_S, field, "slack", CFG, new_id="c-0005"
        )
        is not None
    )
    assert (
        match_reacquisition(
            past, None, [old], past.tick, TICK_S, field, "slack", CFG, new_id="c-0005"
        )
        is None
    )


def test_two_candidates_resolve_by_ratio_then_id() -> None:
    field = Uniform(0.0, 0.0)
    first = fix(0.0, 0.0, 120)
    near = lost("c-0009", 10.0, 0.0, 100)
    far = lost("c-0002", 30.0, 0.0, 100)
    got = match_reacquisition(
        first, None, [far, near], 120, TICK_S, field, "slack", CFG, new_id="c-0010"
    )
    assert got is not None and got.old_id == "c-0009"
    # gap 80 s -> gate 87 m, ratio 0.172; near: gap 40 s -> gate 81 m, ratio 0.123
    wide = lost("c-0004", 15.0, 0.0, 80)
    got2 = match_reacquisition(
        first, None, [near, wide], 120, TICK_S, field, "slack", CFG, new_id="c-0010"
    )
    assert got2 is not None and got2.old_id == "c-0009"
    tie_a = lost("c-0008", 10.0, 0.0, 100)
    tie_b = lost("c-0006", 0.0, 10.0, 100)
    got3 = match_reacquisition(
        first, None, [tie_a, tie_b], 120, TICK_S, field, "slack", CFG, new_id="c-0010"
    )
    assert got3 is not None and got3.old_id == "c-0006"


def test_ratio_beats_raw_distance() -> None:
    field = Uniform(0.0, 0.0)
    first = fix(0.0, 0.0, 150)
    short_gap = lost("c-0001", 40.0, 0.0, 140)  # gap 20 s, gate 78 m, ratio 0.513
    long_gap = lost("c-0002", 45.0, 0.0, 110)  # gap 80 s, gate 87 m, ratio 0.517
    got = match_reacquisition(
        first, None, [long_gap, short_gap], 150, TICK_S, field, "slack", CFG, new_id="c-0003"
    )
    assert got is not None and got.old_id == "c-0001"
    closer_long = lost("c-0002", 42.0, 0.0, 110)  # ratio 0.483 now wins despite 42 > 40 m
    got = match_reacquisition(
        first, None, [closer_long, short_gap], 150, TICK_S, field, "slack", CFG, new_id="c-0003"
    )
    assert got is not None and got.old_id == "c-0002"


def test_tracks_still_alive_or_later_are_not_candidates() -> None:
    field = Uniform(0.0, 0.0)
    first = fix(0.0, 0.0, 120)
    later = lost("c-0002", 0.0, 0.0, 125)
    same = lost("c-0010", 0.0, 0.0, 100)
    assert (
        match_reacquisition(
            first, None, [later, same], 130, TICK_S, field, "slack", CFG, new_id="c-0010"
        )
        is None
    )
