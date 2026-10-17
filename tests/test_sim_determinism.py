"""Determinism of the sim building blocks (CLAUDE.md section 2.4, WORKFLOW.md section 8).

Stage 1: the behavior and detection functions, driven by one seeded generator split
with ``SeedSequence.spawn`` in a fixed order, give identical arrays for the same seed
and different arrays for different seeds. The catalog-wide track-hash tests join this
file with ``run_scenario``.
"""

from __future__ import annotations

import numpy as np
import numpy.typing as npt

from soundings.sim.behaviors import (
    BehaviorParams,
    Drift,
    Erratic,
    KinState,
    LawnmowerThenDrift,
    Orbit,
    Stationary,
    Transit,
    make_lawnmower,
    simulate_path,
)
from soundings.sim.current import load_current_field
from soundings.sim.detection import (
    DetectionParams,
    GapState,
    false_alarm_rate_per_tick,
    gap_step,
    sample_detected,
    sample_false_alarm,
    sample_false_alarm_arrivals,
    sample_position_noise,
)

DT = 2.0
N_TICKS = 300
NODE = (0.0, 0.0)


def _contacts() -> dict[str, tuple[BehaviorParams, KinState]]:
    mower = make_lawnmower(-100.0, -100.0, -300.0, 300.0, -200.0, 200.0, speed_mps=1.0)
    return {
        "gt-1": (Transit(400.0, 300.0, 1.0), KinState(-200.0, 0.0, 6.0)),
        "gt-2": (mower, KinState(-100.0, -100.0, 8.0)),
        "gt-3": (Orbit(50.0, 50.0, 40.0, 0.6), KinState(90.0, 50.0, 8.0)),
        "gt-4": (Drift(jitter_mps=0.05), KinState(10.0, -40.0, 1.5)),
        "gt-5": (Erratic(1.0, drift_weight=0.6, leash_m=300.0), KinState(0.0, 0.0, 2.0)),
        "gt-6": (Stationary(30.0, 30.0, jitter_mps=0.2), KinState(30.0, 30.0, 3.0)),
        "gt-7": (LawnmowerThenDrift(mower, fault_tick=150), KinState(-100.0, -100.0, 8.0)),
    }


def run_blocks(seed: int) -> dict[str, npt.NDArray[np.float64]]:
    """A miniature tick loop over the pure blocks, with SeedSequence-spawned streams."""
    field = load_current_field()
    current = field.sampler(field.phase_for_hour(2))
    params = DetectionParams()
    contacts = _contacts()
    ids = sorted(contacts)
    streams = np.random.SeedSequence(seed).spawn(len(ids) + 2)
    out: dict[str, npt.NDArray[np.float64]] = {}
    for gt_id, ss in zip(ids, streams, strict=False):
        behavior, start = contacts[gt_id]
        rng = np.random.default_rng(ss)
        path = simulate_path(behavior, start, N_TICKS, dt_s=DT, current=current, rng=rng)
        det_rng = np.random.default_rng(ss.spawn(1)[0])
        gap = GapState()
        rows = []
        for tick in range(N_TICKS):
            rng_range = float(np.hypot(path[tick, 0] - NODE[0], path[tick, 1] - NODE[1]))
            detected = sample_detected(det_rng, rng_range, params.r0_m, 1.0, params)
            emitted, gap = gap_step(det_rng, gap, detected, params)
            if emitted:
                dx, dy = sample_position_noise(det_rng, rng_range, params)
                rows.append((tick, path[tick, 0] + dx, path[tick, 1] + dy))
        out[gt_id] = np.round(path, 6)
        out[f"{gt_id}/obs"] = np.round(np.array(rows, dtype=np.float64), 6)
    fa_rng = np.random.default_rng(streams[-2])
    rate = false_alarm_rate_per_tick(params.false_alarms_per_node_hour, DT)
    arrivals = sample_false_alarm_arrivals(fa_rng, rate * 50, size=N_TICKS)
    spawns = [
        sample_false_alarm(fa_rng, NODE[0], NODE[1], params.r0_m, params)
        for _ in range(int(np.asarray(arrivals).sum()))
    ]
    out["fa"] = np.round(
        np.array([(s.x_m, s.y_m, s.depth_m, s.lifetime_ticks, s.click) for s in spawns]), 6
    )
    return out


def test_same_seed_identical_arrays() -> None:
    a, b = run_blocks(1), run_blocks(1)
    assert list(a) == list(b)
    for key in a:
        assert a[key].tobytes() == b[key].tobytes(), key


def test_different_seed_differs() -> None:
    a, b = run_blocks(1), run_blocks(2)
    assert any(a[k].shape != b[k].shape or not np.array_equal(a[k], b[k]) for k in a)


def test_seeds_1_2_3_each_reproduce() -> None:
    for seed in (1, 2, 3):
        first, second = run_blocks(seed), run_blocks(seed)
        assert all(np.array_equal(first[k], second[k]) for k in first)
