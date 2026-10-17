"""Seeded synthetic tracks for the feature and transit-pattern tests.

Every generator builds a noise-free path in local metres at ``tick_s`` spacing, then adds
independent Gaussian position noise from ``numpy.random.default_rng(seed)``. Headings are
compass headings (0 = north, clockwise positive), matching the feature code.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

import numpy as np
from numpy.typing import NDArray

from soundings.canonical import round6
from soundings.investigation.velocity import TrackArrays
from soundings.schemas.geo import from_local_m
from soundings.schemas.track import AcousticReading, Track, TrackGap, TrackPoint

TICK_S = 2.0
FArr = NDArray[np.float64]


def _times(duration_s: float, tick_s: float = TICK_S) -> FArr:
    n = int(round(duration_s / tick_s)) + 1
    return np.arange(n, dtype=np.float64) * tick_s


def make_track(
    t: FArr,
    x: FArr,
    y: FArr,
    *,
    sigma_m: float = 0.0,
    seed: int = 0,
    depth_m: float | FArr = 8.0,
    confidence: float | FArr = 0.9,
    report_sigma: bool = False,
) -> TrackArrays:
    rng = np.random.default_rng(seed)
    n = t.shape[0]
    if sigma_m > 0.0:
        x = x + rng.normal(0.0, sigma_m, n)
        y = y + rng.normal(0.0, sigma_m, n)
    d = np.broadcast_to(np.asarray(depth_m, dtype=np.float64), (n,))
    c = np.broadcast_to(np.asarray(confidence, dtype=np.float64), (n,))
    sig = np.full(n, sigma_m) if (report_sigma and sigma_m > 0.0) else None
    return TrackArrays.from_columns(t, x, y, d, c, sig)


def straight(
    *,
    speed: float = 1.0,
    heading_deg: float = 60.0,
    duration_s: float = 600.0,
    sigma_m: float = 0.0,
    seed: int = 0,
    x0: float = 0.0,
    y0: float = 0.0,
) -> TrackArrays:
    t = _times(duration_s)
    h = math.radians(heading_deg)
    return make_track(
        t, x0 + speed * t * math.sin(h), y0 + speed * t * math.cos(h), sigma_m=sigma_m, seed=seed
    )


def orbit(
    *,
    radius: float = 40.0,
    speed: float = 0.6,
    turns: float = 1.5,
    clockwise: bool = True,
    sigma_m: float = 0.0,
    seed: int = 0,
    cx: float = 0.0,
    cy: float = 0.0,
) -> TrackArrays:
    duration = turns * 2.0 * math.pi * radius / speed
    t = _times(duration)
    omega = speed / radius * (1.0 if clockwise else -1.0)
    ang = omega * t  # bearing from the centre, compass sense
    return make_track(
        t, cx + radius * np.sin(ang), cy + radius * np.cos(ang), sigma_m=sigma_m, seed=seed
    )


def lawnmower(
    *,
    legs: int = 4,
    leg_m: float = 110.0,
    spacing_m: float = 30.0,
    speed: float = 1.0,
    sigma_m: float = 0.0,
    seed: int = 0,
) -> TrackArrays:
    """Legs run north/south, joined by semicircular turns stepping east."""
    r = spacing_m / 2.0
    pieces_x: list[float] = []
    pieces_y: list[float] = []
    ds = speed * TICK_S / 4.0
    x, y = 0.0, 0.0
    for k in range(legs):
        direction = 1.0 if k % 2 == 0 else -1.0
        steps = int(round(leg_m / ds))
        for _ in range(steps):
            pieces_x.append(x)
            pieces_y.append(y)
            y += direction * ds
        if k == legs - 1:
            break
        # semicircle centred east of the leg end, turning toward the next leg
        cx, cy = x + r, y
        n_turn = int(round(math.pi * r / ds))
        for j in range(n_turn):
            a = math.pi * j / n_turn
            pieces_x.append(cx - r * math.cos(a))
            pieces_y.append(cy + direction * r * math.sin(a))
        x, y = cx + r, cy
    pieces_x.append(x)
    pieces_y.append(y)
    fine_x = np.array(pieces_x)
    fine_y = np.array(pieces_y)
    # resample the fine path (ds apart) at one point per tick
    stride = int(round(speed * TICK_S / ds))
    px = fine_x[::stride]
    py = fine_y[::stride]
    t = np.arange(px.shape[0], dtype=np.float64) * TICK_S
    return make_track(t, px, py, sigma_m=sigma_m, seed=seed)


def random_walk(
    *,
    speed: float = 0.8,
    turn_sigma_deg: float = 50.0,
    turn_every_s: float = 20.0,
    duration_s: float = 600.0,
    sigma_m: float = 0.0,
    seed: int = 0,
    path_seed: int = 2,
) -> TrackArrays:
    """Constant speed; every ``turn_every_s`` the heading jumps by N(0, turn_sigma).

    ``path_seed`` draws the path, ``seed`` the reading noise.
    """
    rng = np.random.default_rng(path_seed)
    t = _times(duration_s)
    per = int(round(turn_every_s / TICK_S))
    heading = float(rng.uniform(0.0, 360.0))
    xs = np.zeros_like(t)
    ys = np.zeros_like(t)
    for i in range(1, t.shape[0]):
        if i % per == 0:
            heading += float(rng.normal(0.0, turn_sigma_deg))
        h = math.radians(heading)
        xs[i] = xs[i - 1] + speed * TICK_S * math.sin(h)
        ys[i] = ys[i - 1] + speed * TICK_S * math.cos(h)
    return make_track(t, xs, ys, sigma_m=sigma_m, seed=seed)


def stationary(*, duration_s: float = 600.0, sigma_m: float = 5.0, seed: int = 0) -> TrackArrays:
    t = _times(duration_s)
    return make_track(t, np.zeros_like(t), np.zeros_like(t), sigma_m=sigma_m, seed=seed)


# --- WO-010 Track builders -----------------------------------------------------------

# Local-metre anchors on the real config/harbor.geojson map.
FUEL_BERTHS_XY = (90.0, 300.0)  # inside fuel_berths (criticality 0.9), near hyd_1
MAIN_CHANNEL_XY = (400.0, -1500.0)  # inside main_channel, near hyd_4
OUTSIDE_ZONES_XY = (-2000.0, -2000.0)  # inside the bounding box, in no zone


def to_track(
    arrays: TrackArrays,
    *,
    contact_id: str = "c-0001",
    origin_xy: tuple[float, float] = MAIN_CHANNEL_XY,
    sigma_m: float = 1.0,
    sensor_id: str = "hyd_4",
    tick_s: float = TICK_S,
    acoustic: AcousticReading | Sequence[AcousticReading] | None = None,
    gaps: Sequence[tuple[int, int]] = (),
) -> Track:
    """A WO-010 ``Track`` from local-metre columns shifted by ``origin_xy``.

    Ticks are ``round(t_s / tick_s)``; lat/lon are rounded to 6 dp like the sim output.
    """
    points = []
    for i in range(len(arrays)):
        lat, lon = from_local_m(
            float(arrays.x_m[i]) + origin_xy[0], float(arrays.y_m[i]) + origin_xy[1]
        )
        if acoustic is None:
            reading = AcousticReading()
        elif isinstance(acoustic, AcousticReading):
            reading = acoustic
        else:
            reading = acoustic[i]
        points.append(
            TrackPoint(
                tick=int(round(float(arrays.t_s[i]) / tick_s)),
                lat=round6(lat),
                lon=round6(lon),
                depth_m=round6(float(arrays.depth_m[i])),
                sigma_m=sigma_m,
                sensor_id=sensor_id,
                confidence=round6(float(arrays.confidence[i])),
                acoustic=reading,
            )
        )
    return Track(
        contact_id=contact_id,
        tick_s=tick_s,
        points=tuple(points),
        gaps=tuple(TrackGap(start_tick=a, end_tick=b) for a, b in gaps),
    )


def line_track(
    n: int,
    *,
    speed: float = 1.0,
    heading_deg: float = 90.0,
    confidence: float | FArr = 0.9,
    **kwargs: object,
) -> Track:
    """A noise-free ``n``-point line at ``speed`` m/s, 2 s ticks."""
    t = np.arange(n, dtype=np.float64) * TICK_S
    h = math.radians(heading_deg)
    arrays = make_track(t, speed * t * math.sin(h), speed * t * math.cos(h), confidence=confidence)
    return to_track(arrays, **kwargs)  # type: ignore[arg-type]


@dataclass(frozen=True)
class FakeRun:
    """The two ``SimRun`` fields the evidence table reads (WO-011 is not merged yet)."""

    tracks: Mapping[str, Track] = field(default_factory=dict)
    tick_s: float = TICK_S


def calm(_x: float, _y: float, _t: float) -> tuple[float, float]:
    return (0.0, 0.0)
