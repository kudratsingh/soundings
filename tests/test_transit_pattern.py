"""transit_pattern classifier: table-driven synthetic tracks under reading noise.

Each class is generated noise-free and then under three noise seeds at sigma 10 m
(the stationary track uses 5 m, per WO-012). The classifier must be deterministic and
separate the five classes; two edge cases pin the rule boundaries.
"""

from __future__ import annotations

from collections.abc import Callable

import numpy as np
import pytest
from synthetic_tracks import lawnmower, make_track, orbit, random_walk, stationary, straight

from soundings.investigation.features import (
    TRANSIT_PATTERNS,
    classify_transit_pattern,
    transit_stats,
)
from soundings.investigation.velocity import TrackArrays

NOISE_SEEDS = (1, 2, 3)

GENERATORS: dict[str, Callable[[float, int], TrackArrays]] = {
    "straight": lambda sigma, seed: straight(speed=1.0, heading_deg=60.0, sigma_m=sigma, seed=seed),
    "lawnmower": lambda sigma, seed: lawnmower(legs=4, sigma_m=sigma, seed=seed),
    "orbit": lambda sigma, seed: orbit(radius=40.0, speed=0.6, turns=1.5, sigma_m=sigma, seed=seed),
    "random": lambda sigma, seed: random_walk(sigma_m=sigma, seed=seed),
    "stationary": lambda sigma, seed: stationary(sigma_m=5.0 if sigma else 0.0, seed=seed),
}

CASES = [
    (pattern, sigma, seed)
    for pattern in GENERATORS
    for sigma, seeds in ((0.0, (0,)), (10.0, NOISE_SEEDS))
    for seed in seeds
]


def test_every_pattern_is_generated() -> None:
    assert set(GENERATORS) == set(TRANSIT_PATTERNS)


@pytest.mark.parametrize(("pattern", "sigma", "seed"), CASES)
def test_classifies_synthetic_track(pattern: str, sigma: float, seed: int) -> None:
    track = GENERATORS[pattern](sigma, seed)
    assert classify_transit_pattern(track) == pattern, transit_stats(track)


@pytest.mark.parametrize("seed", NOISE_SEEDS)
@pytest.mark.parametrize("pattern", ["straight", "lawnmower", "orbit"])
def test_classifies_under_heavier_noise(pattern: str, seed: int) -> None:
    track = GENERATORS[pattern](15.0, seed)
    assert classify_transit_pattern(track) == pattern, transit_stats(track)


@pytest.mark.parametrize("seed", NOISE_SEEDS)
def test_counterclockwise_orbit(seed: int) -> None:
    track = orbit(clockwise=False, sigma_m=10.0, seed=seed)
    stats = transit_stats(track)
    assert stats.cumulative_turn_deg <= -360.0
    assert classify_transit_pattern(track) == "orbit"


@pytest.mark.parametrize("seed", (0, *NOISE_SEEDS))
def test_half_orbit_is_random(seed: int) -> None:
    track = orbit(turns=0.5, sigma_m=10.0 if seed else 0.0, seed=seed)
    stats = transit_stats(track)
    assert abs(stats.cumulative_turn_deg) < 360.0
    assert classify_transit_pattern(track) == "random"


@pytest.mark.parametrize("seed", (0, *NOISE_SEEDS))
def test_single_reversal_is_not_lawnmower(seed: int) -> None:
    track = lawnmower(legs=2, leg_m=250.0, sigma_m=10.0 if seed else 0.0, seed=seed)
    stats = transit_stats(track)
    assert stats.reversals == 1
    assert classify_transit_pattern(track) != "lawnmower"


def test_lawnmower_counts_reversals() -> None:
    stats = transit_stats(lawnmower(legs=4))
    assert stats.reversals == 3
    # three alternating U-turns: +180 - 180 + 180, far from an orbit's 360
    assert stats.cumulative_turn_deg == pytest.approx(180.0, abs=1.0)


def test_orbit_stats() -> None:
    stats = transit_stats(orbit())
    assert stats.cumulative_turn_deg >= 360.0
    assert stats.turn_sign_fraction == 1.0
    assert stats.persistence is not None and stats.persistence < 0.5


def test_uses_last_600_seconds_only() -> None:
    """A straight run after a long orbit is classified on the recent window."""
    o = orbit(turns=2.0)
    t_tail = o.t_s[-1] + np.arange(1, 400, dtype=np.float64) * 2.0
    run = (t_tail - o.t_s[-1]) * 1.0
    x = np.concatenate([o.x_m, o.x_m[-1] + run])
    y = np.concatenate([o.y_m, np.full_like(run, o.y_m[-1])])
    track = make_track(np.concatenate([o.t_s, t_tail]), x, y)
    assert classify_transit_pattern(track) == "straight"


def test_unknown_below_minimums() -> None:
    long = straight()
    assert classify_transit_pattern(long.select(slice(0, 9))) is None
    assert classify_transit_pattern(long.select(slice(0, 10))) is None  # span 18 s
    assert classify_transit_pattern(long.select(slice(0, 31))) == "straight"  # span 60 s


@pytest.mark.parametrize(("pattern", "sigma", "seed"), CASES)
def test_deterministic(pattern: str, sigma: float, seed: int) -> None:
    a = GENERATORS[pattern](sigma, seed)
    b = GENERATORS[pattern](sigma, seed)
    assert classify_transit_pattern(a) == classify_transit_pattern(b)
    assert transit_stats(a) == transit_stats(b)
    assert classify_transit_pattern(a) == classify_transit_pattern(a)
