"""Ground-truth kinematics on no-noise fixtures (soundings.sim.behaviors).

Proves the kinematics WO-012's features will measure: transit closes and stops,
lawnmower legs reverse, orbit holds its radius, drift follows the current-field cell,
stationary holds, lawnmower_then_drift switches at exactly fault_tick, erratic turns
more than transit, and silent_after_tick silences acoustics and hail replies.
"""

from __future__ import annotations

import math

import numpy as np
import numpy.typing as npt
import pytest

from soundings.sim.acoustics import SILENT, AcousticTruth, apply_silence, is_silent
from soundings.sim.behaviors import (
    BEHAVIOR_TYPES,
    Drift,
    Erratic,
    KinState,
    Lawnmower,
    LawnmowerThenDrift,
    Orbit,
    Stationary,
    Transit,
    heading_of,
    make_lawnmower,
    point_along,
    simulate_path,
    step,
)
from soundings.sim.current import Vector, load_current_field

DT = 2.0


def no_current(x_m: float, y_m: float) -> Vector:
    return (0.0, 0.0)


def steady_current(x_m: float, y_m: float) -> Vector:
    return (0.12, -0.05)


def rng(seed: int = 0) -> np.random.Generator:
    return np.random.default_rng(seed)


def headings(path: npt.NDArray[np.float64]) -> npt.NDArray[np.float64]:
    d = np.diff(path[:, :2], axis=0)
    moving = np.hypot(d[:, 0], d[:, 1]) > 1e-9
    return np.arctan2(d[moving, 0], d[moving, 1])


def turn_rates(path: npt.NDArray[np.float64]) -> npt.NDArray[np.float64]:
    h = headings(path)
    return (np.diff(h) + math.pi) % (2.0 * math.pi) - math.pi


def test_behavior_type_names() -> None:
    assert BEHAVIOR_TYPES == (
        "transit",
        "lawnmower",
        "orbit",
        "drift",
        "erratic",
        "stationary",
        "lawnmower_then_drift",
    )
    classes = (Transit, Lawnmower, Orbit, Drift, Erratic, Stationary, LawnmowerThenDrift)
    assert tuple(c.type for c in classes) == BEHAVIOR_TYPES


def test_transit_closes_monotonically_and_stops() -> None:
    target = (400.0, 300.0)
    p = Transit(target_x_m=target[0], target_y_m=target[1], speed_mps=1.0)
    path = simulate_path(
        p, KinState(0.0, 0.0, 6.0), 400, dt_s=DT, current=steady_current, rng=rng()
    )
    dist = np.hypot(path[:, 0] - target[0], path[:, 1] - target[1])
    assert np.all(np.diff(dist) <= 1e-9)
    arrived = int(np.argmax(dist < 1e-9))
    assert arrived == 250  # 500 m at 1 m/s and 2 s ticks
    assert np.all(np.diff(dist[:arrived]) < 0)
    assert np.allclose(path[arrived:, :2], target)
    assert np.all(path[arrived + 1 :, 4] == 0.0)
    assert np.allclose(path[1:arrived, 4], 1.0)
    assert np.all(path[:, 2] == 6.0)


def test_lawnmower_legs_reverse_inside_zone() -> None:
    rect = (-300.0, 300.0, -200.0, 200.0)
    p = make_lawnmower(0.0, 0.0, *rect, speed_mps=1.0, lane_spacing_m=50.0)
    path = simulate_path(p, KinState(0.0, 0.0, 8.0), 1000, dt_s=DT, current=no_current, rng=rng())
    x_min, x_max, y_min, y_max = rect
    assert np.all((path[:, 0] >= x_min - 1e-9) & (path[:, 0] <= x_max + 1e-9))
    assert np.all((path[:, 1] >= y_min - 1e-9) & (path[:, 1] <= y_max + 1e-9))
    # Compress per-step headings into legs: east-west runs of at least 10 ticks.
    h = headings(path)
    legs: list[float] = []
    run_dir, run_len = 0.0, 0
    for value in h:
        east_west = abs(math.sin(value)) > 0.999
        direction = math.copysign(1.0, math.sin(value)) if east_west else 0.0
        if direction == run_dir:
            run_len += 1
        else:
            if run_dir != 0.0 and run_len >= 10:
                legs.append(run_dir)
            run_dir, run_len = direction, 1
    if run_dir != 0.0 and run_len >= 10:
        legs.append(run_dir)
    assert len(legs) >= 3
    # Successive legs head in opposite directions: ~180 degree turns.
    assert all(a == -b for a, b in zip(legs, legs[1:], strict=False))
    assert np.allclose(path[1:, 4][path[1:, 4] > 0.99], 1.0)


def test_lawnmower_path_shape() -> None:
    p = make_lawnmower(0.0, 0.0, -100.0, 100.0, -10.0, 90.0, speed_mps=1.0, lane_spacing_m=50.0)
    assert p.waypoints[0] == (0.0, 0.0)
    ys = sorted({y for _, y in p.waypoints})
    assert len(ys) >= 3  # spacing shrinks or lanes add so there are at least three legs
    assert point_along(p.waypoints, 0.0) == (0.0, 0.0)
    with pytest.raises(ValueError):
        make_lawnmower(0.0, 0.0, 1.0, 1.0, 0.0, 1.0, speed_mps=1.0)


def test_orbit_holds_radius() -> None:
    center = (50.0, -20.0)
    p = Orbit(center_x_m=center[0], center_y_m=center[1], radius_m=40.0, speed_mps=0.6)
    start = KinState(center[0] + 40.0, center[1], 8.0)
    path = simulate_path(p, start, 600, dt_s=DT, current=steady_current, rng=rng())
    r = np.hypot(path[:, 0] - center[0], path[:, 1] - center[1])
    assert np.all(np.abs(r - 40.0) <= 4.0)
    assert np.allclose(path[1:, 4], 0.6, atol=0.01)
    # It actually goes round: 600 ticks * 1.2 m = 720 m, about 2.9 laps.
    angles = np.unwrap(np.arctan2(path[:, 1] - center[1], path[:, 0] - center[0]))
    assert abs(angles[-1] - angles[0]) > 2 * 2 * math.pi


def test_orbit_converges_from_off_circle() -> None:
    p = Orbit(center_x_m=0.0, center_y_m=0.0, radius_m=40.0, speed_mps=0.6)
    path = simulate_path(p, KinState(150.0, 0.0, 8.0), 300, dt_s=DT, current=no_current, rng=rng())
    r = np.hypot(path[:, 0], path[:, 1])
    assert np.all(np.abs(r[150:] - 40.0) <= 4.0)


def test_drift_matches_current_field_cell() -> None:
    field = load_current_field()
    for phase in ("flood", "ebb", "slack"):
        sampler = field.sampler(phase)
        path = simulate_path(
            Drift(), KinState(-200.0, -500.0, 1.5), 300, dt_s=DT, current=sampler, rng=rng()
        )
        for k in range(len(path) - 1):
            u, v = sampler(float(path[k, 0]), float(path[k, 1]))
            vx = (path[k + 1, 0] - path[k, 0]) / DT
            vy = (path[k + 1, 1] - path[k, 1]) / DT
            assert abs(vx - u) <= 0.05 and abs(vy - v) <= 0.05
        assert np.all(path[:, 2] == 1.5)


def test_drift_jitter_is_seeded_noise_around_current() -> None:
    path = simulate_path(
        Drift(jitter_mps=0.05),
        KinState(0.0, 0.0, 1.5),
        2000,
        dt_s=DT,
        current=steady_current,
        rng=rng(9),
    )
    v = np.diff(path[:, :2], axis=0) / DT
    assert abs(float(v[:, 0].mean()) - 0.12) < 0.01
    assert abs(float(v[:, 1].mean()) + 0.05) < 0.01


def test_stationary_stays_within_2m() -> None:
    p = Stationary(anchor_x_m=10.0, anchor_y_m=20.0, jitter_mps=0.2)
    path = simulate_path(
        p, KinState(10.0, 20.0, 3.0), 600, dt_s=DT, current=steady_current, rng=rng(3)
    )
    off = np.hypot(path[:, 0] - 10.0, path[:, 1] - 20.0)
    assert float(off.max()) <= 2.0
    assert float(off.max()) > 0.0  # the jitter is real
    still = simulate_path(
        Stationary(10.0, 20.0),
        KinState(10.0, 20.0, 3.0),
        50,
        dt_s=DT,
        current=no_current,
        rng=rng(),
    )
    assert np.all(still[:, :2] == (10.0, 20.0))


def test_lawnmower_then_drift_switches_at_fault_tick() -> None:
    mower = make_lawnmower(0.0, 0.0, -300.0, 300.0, -200.0, 200.0, speed_mps=1.0)
    fault = 300
    p = LawnmowerThenDrift(lawnmower=mower, fault_tick=fault)
    start = KinState(0.0, 0.0, 8.0)
    path = simulate_path(p, start, 400, dt_s=DT, current=steady_current, rng=rng())
    pure = simulate_path(mower, start, 400, dt_s=DT, current=steady_current, rng=rng())
    # Rows 0..fault come from steps at ticks < fault: identical to the pure lawnmower.
    assert np.array_equal(path[: fault + 1, :2], pure[: fault + 1, :2])
    # The step that starts at fault_tick is already drift, and so is every later step.
    v = np.diff(path[:, :2], axis=0) / DT
    assert not np.allclose(v[fault - 1], (0.12, -0.05))
    assert np.allclose(v[fault:], (0.12, -0.05))


def test_erratic_turns_more_than_transit() -> None:
    erratic = Erratic(speed_mps=1.0, drift_weight=0.6, leash_m=300.0)
    e_path = simulate_path(
        erratic, KinState(0.0, 0.0, 2.0), 600, dt_s=DT, current=steady_current, rng=rng(17)
    )
    t_path = simulate_path(
        Transit(2000.0, 500.0, 1.0),
        KinState(0.0, 0.0, 2.0),
        600,
        dt_s=DT,
        current=steady_current,
        rng=rng(17),
    )
    assert float(np.var(turn_rates(e_path))) > 0.05
    assert float(np.var(turn_rates(e_path))) > 100 * float(np.var(turn_rates(t_path)))
    assert np.all((e_path[:, 2] >= 0.0) & (e_path[:, 2] <= 6.0))
    # The leash keeps the animal near home.
    assert float(np.hypot(e_path[:, 0], e_path[:, 1]).max()) < 400.0


def test_silent_after_tick_modifier() -> None:
    loud = AcousticTruth(propeller_tonal=True, tonal_band="low", biologic_signature=False)
    for tick in range(0, 300):
        assert apply_silence(tick, 300, loud, True) == (loud, True)
    for tick in range(300, 600):
        acoustic, replies = apply_silence(tick, 300, loud, True)
        assert acoustic == SILENT
        assert acoustic.propeller_tonal is False and acoustic.tonal_band == "none"
        assert replies is False
    assert apply_silence(10_000, None, loud, True) == (loud, True)
    assert is_silent(5, 5) and not is_silent(4, 5) and not is_silent(4, None)
    with pytest.raises(ValueError):
        AcousticTruth(propeller_tonal=True, tonal_band="loud", biologic_signature=False)


def test_heading_of_compass_convention() -> None:
    assert heading_of(0.0, 1.0, 9.0) == 0.0
    assert heading_of(1.0, 0.0, 9.0) == pytest.approx(math.pi / 2)
    assert heading_of(0.0, 0.0, 9.0) == 9.0


def test_step_rejects_bad_dt() -> None:
    with pytest.raises(ValueError):
        step(Drift(), KinState(0.0, 0.0, 1.0), tick=0, dt_s=0.0, current=no_current, rng=rng())
