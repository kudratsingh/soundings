"""Seeded straight synthetic track for the WO-084 Kalman tests.

A noise-free constant-velocity path in local metres at ``tick_s`` spacing, plus
independent N(0, sigma) noise per axis drawn from the ``numpy.random.Generator`` the
caller passes in (built with ``numpy.random.default_rng(seed)``).
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray

from soundings.investigation.velocity import TrackArrays

FArr = NDArray[np.float64]

TICK_S = 2.0
DURATION_S = 600.0
SPEED_MPS = 1.5
HEADING_DEG = 45.0
START_XY = (100.0, -200.0)
SIGMA_M = 15.0


@dataclass(frozen=True)
class StraightTrack:
    """The observed readings plus the truth they were drawn from (tests only)."""

    readings: TrackArrays
    true_x: FArr
    true_y: FArr
    vx: float
    vy: float

    def truth_at(self, t_s: float) -> tuple[float, float]:
        t0 = float(self.readings.t_s[0])
        return START_XY[0] + self.vx * (t_s - t0), START_XY[1] + self.vy * (t_s - t0)


def straight_track(
    rng: np.random.Generator,
    *,
    duration_s: float = DURATION_S,
    tick_s: float = TICK_S,
    speed_mps: float = SPEED_MPS,
    heading_deg: float = HEADING_DEG,
    sigma_m: float = SIGMA_M,
    noise: bool = True,
    report_sigma: bool = True,
    confidence: float = 0.9,
) -> StraightTrack:
    n = int(round(duration_s / tick_s)) + 1
    t = np.arange(n, dtype=np.float64) * tick_s
    h = math.radians(heading_deg)
    vx, vy = speed_mps * math.sin(h), speed_mps * math.cos(h)
    tx = START_XY[0] + vx * t
    ty = START_XY[1] + vy * t
    if noise:
        ox = tx + rng.normal(0.0, sigma_m, n)
        oy = ty + rng.normal(0.0, sigma_m, n)
    else:
        ox, oy = tx.copy(), ty.copy()
    readings = TrackArrays.from_columns(
        t,
        ox,
        oy,
        np.full(n, 8.0),
        np.full(n, confidence),
        np.full(n, sigma_m) if report_sigma else None,
    )
    return StraightTrack(readings=readings, true_x=tx, true_y=ty, vx=vx, vy=vy)
