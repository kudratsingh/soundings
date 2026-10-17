"""Ground-truth kinematics: seven behavior types as pure step functions.

State is ``KinState`` in local metres (x east, y north), depth in metres, heading in
compass radians (0 = north, pi/2 = east) and speed over ground in m/s. ``step`` maps
(state, tick) to the state one tick later. Functions that draw take an explicit
``numpy.random.Generator``; behaviors that never draw ignore it, so the draw order for a
contact is fixed by its behavior alone (docs/WORKFLOW.md section 8).

Self-propelled behaviors (``transit``, ``lawnmower``, ``orbit``, ``stationary``, and
``lawnmower_then_drift`` before its fault) hold their commanded ground track: the
vehicle's autopilot or the diver compensates for the current. Unpowered motion
(``drift``, the drifting part of ``lawnmower_then_drift``) moves with the current, and
``erratic`` blends its own swimming with the current by ``drift_weight``.

The ``silent_after_tick`` modifier is acoustic, not kinematic: see
``soundings.sim.acoustics.apply_silence``.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import ClassVar

import numpy as np
import numpy.typing as npt

from soundings.sim.current import CurrentSampler

BEHAVIOR_TYPES: tuple[str, ...] = (
    "transit",
    "lawnmower",
    "orbit",
    "drift",
    "erratic",
    "stationary",
    "lawnmower_then_drift",
)

Point = tuple[float, float]
_ARRIVE_TOL_M = 1e-6  # snap onto a transit target once within one step plus float noise


@dataclass(frozen=True, slots=True)
class KinState:
    """Kinematic state of one ground-truth contact at one tick."""

    x_m: float
    y_m: float
    depth_m: float
    heading_rad: float = 0.0
    speed_mps: float = 0.0
    path_s_m: float = 0.0  # distance along a precomputed path (lawnmower); 0 otherwise


def heading_of(dx: float, dy: float, fallback: float) -> float:
    """Compass heading of a displacement in [0, 2*pi); ``fallback`` if it is zero."""
    if dx == 0.0 and dy == 0.0:
        return fallback
    return math.atan2(dx, dy) % (2.0 * math.pi)


def _moved(state: KinState, x: float, y: float, dt_s: float, **kw: float) -> KinState:
    dx, dy = x - state.x_m, y - state.y_m
    return replace(
        state,
        x_m=x,
        y_m=y,
        heading_rad=heading_of(dx, dy, state.heading_rad),
        speed_mps=math.hypot(dx, dy) / dt_s,
        **kw,
    )


# ---------------------------------------------------------------------------
# Behavior parameter types (plain, immutable, local metres)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Transit:
    """Head straight for a target point at ``speed_mps`` and stop on arrival."""

    type: ClassVar[str] = "transit"
    target_x_m: float
    target_y_m: float
    speed_mps: float


@dataclass(frozen=True, slots=True)
class Lawnmower:
    """Survey legs along a precomputed boustrophedon path, ping-ponging at its ends.

    Build it with ``make_lawnmower`` so the path starts at the contact's start point.
    """

    type: ClassVar[str] = "lawnmower"
    waypoints: tuple[Point, ...]
    speed_mps: float


@dataclass(frozen=True, slots=True)
class Orbit:
    """Circle a centre point at ``radius_m``; a contact off the circle converges onto it."""

    type: ClassVar[str] = "orbit"
    center_x_m: float
    center_y_m: float
    radius_m: float
    speed_mps: float
    clockwise: bool = True


@dataclass(frozen=True, slots=True)
class Drift:
    """Move with the current, plus optional Gaussian velocity jitter (m/s per axis)."""

    type: ClassVar[str] = "drift"
    jitter_mps: float = 0.0


@dataclass(frozen=True, slots=True)
class Erratic:
    """Random-heading swimming blended with the current: ``v = (1-w) v_self + w v_current``.

    If ``leash_m`` is set, a contact farther than that from home turns back toward home,
    which keeps an animal near the sensors without making its path regular.
    """

    type: ClassVar[str] = "erratic"
    speed_mps: float
    drift_weight: float = 0.0
    turn_sigma_rad: float = 0.6
    speed_sigma_mps: float = 0.1
    depth_sigma_m: float = 0.1
    depth_min_m: float = 0.0
    depth_max_m: float = 6.0
    home_x_m: float = 0.0
    home_y_m: float = 0.0
    leash_m: float | None = None


@dataclass(frozen=True, slots=True)
class Stationary:
    """Hold near an anchor, wandering at up to ``jitter_mps`` within ``max_offset_m``."""

    type: ClassVar[str] = "stationary"
    anchor_x_m: float
    anchor_y_m: float
    jitter_mps: float = 0.0
    max_offset_m: float = 1.5


@dataclass(frozen=True, slots=True)
class LawnmowerThenDrift:
    """Lawnmower until ``fault_tick``; from the step that starts at ``fault_tick``, drift."""

    type: ClassVar[str] = "lawnmower_then_drift"
    lawnmower: Lawnmower
    fault_tick: int
    drift: Drift = Drift()


BehaviorParams = Transit | Lawnmower | Orbit | Drift | Erratic | Stationary | LawnmowerThenDrift


# ---------------------------------------------------------------------------
# Lawnmower path
# ---------------------------------------------------------------------------


def make_lawnmower(
    start_x_m: float,
    start_y_m: float,
    x_min_m: float,
    x_max_m: float,
    y_min_m: float,
    y_max_m: float,
    speed_mps: float,
    lane_spacing_m: float = 50.0,
) -> Lawnmower:
    """Boustrophedon path inside a rectangle, starting at the (clamped) start point.

    Legs run east-west. The first leg goes to the farther x edge; lanes step toward the
    side of the rectangle with more room. Lane spacing shrinks if needed so the path has
    at least three legs.
    """
    if x_max_m <= x_min_m or y_max_m <= y_min_m:
        raise ValueError("lawnmower rectangle must have positive width and height")
    if lane_spacing_m <= 0.0 or speed_mps <= 0.0:
        raise ValueError("lane_spacing_m and speed_mps must be > 0")
    sx = min(max(start_x_m, x_min_m), x_max_m)
    sy = min(max(start_y_m, y_min_m), y_max_m)
    direction = 1.0 if (y_max_m - sy) >= (sy - y_min_m) else -1.0
    room = (y_max_m - sy) if direction > 0 else (sy - y_min_m)
    spacing = min(lane_spacing_m, room / 2.0) if room > 0 else lane_spacing_m
    n_lanes = int(room // spacing) + 1 if room > 0 else 1
    east_first = (x_max_m - sx) >= (sx - x_min_m)
    edges = (x_max_m, x_min_m) if east_first else (x_min_m, x_max_m)
    waypoints: list[Point] = [(sx, sy)]
    for k in range(n_lanes):
        y = sy + direction * spacing * k
        if k > 0:
            waypoints.append((waypoints[-1][0], y))
        waypoints.append((edges[k % 2], y))
    return Lawnmower(waypoints=tuple(waypoints), speed_mps=speed_mps)


def _path_length(waypoints: Sequence[Point]) -> float:
    return sum(
        math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in zip(waypoints, waypoints[1:], strict=False)
    )


def point_along(waypoints: Sequence[Point], s_m: float) -> Point:
    """Position at distance ``s_m`` along the path, ping-ponging back and forth."""
    total = _path_length(waypoints)
    if total == 0.0:
        return waypoints[0]
    s = s_m % (2.0 * total)
    if s > total:
        s = 2.0 * total - s
    for a, b in zip(waypoints, waypoints[1:], strict=False):
        seg = math.hypot(b[0] - a[0], b[1] - a[1])
        if s <= seg:
            f = s / seg if seg > 0 else 0.0
            return a[0] + f * (b[0] - a[0]), a[1] + f * (b[1] - a[1])
        s -= seg
    return waypoints[-1]


# ---------------------------------------------------------------------------
# Step functions
# ---------------------------------------------------------------------------


def step_transit(p: Transit, s: KinState, dt_s: float) -> KinState:
    dx, dy = p.target_x_m - s.x_m, p.target_y_m - s.y_m
    dist = math.hypot(dx, dy)
    reach = p.speed_mps * dt_s
    if dist <= reach + _ARRIVE_TOL_M:
        return _moved(s, p.target_x_m, p.target_y_m, dt_s)
    f = reach / dist
    return _moved(s, s.x_m + f * dx, s.y_m + f * dy, dt_s)


def step_lawnmower(p: Lawnmower, s: KinState, dt_s: float) -> KinState:
    new_s = s.path_s_m + p.speed_mps * dt_s
    x, y = point_along(p.waypoints, new_s)
    return _moved(s, x, y, dt_s, path_s_m=new_s)


def step_orbit(p: Orbit, s: KinState, dt_s: float) -> KinState:
    rx, ry = s.x_m - p.center_x_m, s.y_m - p.center_y_m
    r = math.hypot(rx, ry)
    reach = p.speed_mps * dt_s
    if r == 0.0:
        return _moved(s, p.center_x_m, p.center_y_m + min(reach, p.radius_m), dt_s)
    if abs(r - p.radius_m) > reach:
        f = reach / r * (1.0 if r < p.radius_m else -1.0)
        return _moved(s, s.x_m + f * rx, s.y_m + f * ry, dt_s)
    # On (or within one step of) the circle: advance by arc length ``reach`` along it.
    theta = math.atan2(ry, rx)
    dtheta = reach / p.radius_m
    theta += -dtheta if p.clockwise else dtheta
    x = p.center_x_m + p.radius_m * math.cos(theta)
    y = p.center_y_m + p.radius_m * math.sin(theta)
    return _moved(s, x, y, dt_s)


def step_drift(
    p: Drift, s: KinState, dt_s: float, current: CurrentSampler, rng: np.random.Generator
) -> KinState:
    u, v = current(s.x_m, s.y_m)
    if p.jitter_mps > 0.0:
        ju, jv = rng.normal(0.0, p.jitter_mps, size=2)
        u, v = u + float(ju), v + float(jv)
    return _moved(s, s.x_m + u * dt_s, s.y_m + v * dt_s, dt_s)


def step_erratic(
    p: Erratic, s: KinState, dt_s: float, current: CurrentSampler, rng: np.random.Generator
) -> KinState:
    turn, dspeed, ddepth = rng.normal(0.0, 1.0, size=3)
    swim_heading = (s.heading_rad + p.turn_sigma_rad * float(turn)) % (2.0 * math.pi)
    if p.leash_m is not None:
        hx, hy = p.home_x_m - s.x_m, p.home_y_m - s.y_m
        if math.hypot(hx, hy) > p.leash_m:
            swim_heading = heading_of(hx, hy, swim_heading)
    swim_speed = min(max(p.speed_mps + p.speed_sigma_mps * float(dspeed), 0.0), 3.0 * p.speed_mps)
    u, v = current(s.x_m, s.y_m)
    w = p.drift_weight
    vx = (1.0 - w) * swim_speed * math.sin(swim_heading) + w * u
    vy = (1.0 - w) * swim_speed * math.cos(swim_heading) + w * v
    depth = min(max(s.depth_m + p.depth_sigma_m * float(ddepth), p.depth_min_m), p.depth_max_m)
    out = _moved(s, s.x_m + vx * dt_s, s.y_m + vy * dt_s, dt_s, depth_m=depth)
    # Keep the swimming heading as state so turns accumulate as a random walk.
    return replace(out, heading_rad=swim_heading)


def step_stationary(p: Stationary, s: KinState, dt_s: float, rng: np.random.Generator) -> KinState:
    if p.jitter_mps <= 0.0:
        return _moved(s, p.anchor_x_m, p.anchor_y_m, dt_s)
    jx, jy = rng.normal(0.0, p.jitter_mps * dt_s, size=2)
    ox = s.x_m - p.anchor_x_m + float(jx)
    oy = s.y_m - p.anchor_y_m + float(jy)
    off = math.hypot(ox, oy)
    if off > p.max_offset_m:
        ox, oy = ox * p.max_offset_m / off, oy * p.max_offset_m / off
    return _moved(s, p.anchor_x_m + ox, p.anchor_y_m + oy, dt_s)


def step(
    params: BehaviorParams,
    state: KinState,
    *,
    tick: int,
    dt_s: float,
    current: CurrentSampler,
    rng: np.random.Generator,
) -> KinState:
    """Advance one contact from ``tick`` to ``tick + 1``."""
    if dt_s <= 0.0:
        raise ValueError("dt_s must be > 0")
    match params:
        case Transit():
            return step_transit(params, state, dt_s)
        case Lawnmower():
            return step_lawnmower(params, state, dt_s)
        case Orbit():
            return step_orbit(params, state, dt_s)
        case Drift():
            return step_drift(params, state, dt_s, current, rng)
        case Erratic():
            return step_erratic(params, state, dt_s, current, rng)
        case Stationary():
            return step_stationary(params, state, dt_s, rng)
        case LawnmowerThenDrift():
            if tick < params.fault_tick:
                return step_lawnmower(params.lawnmower, state, dt_s)
            return step_drift(params.drift, state, dt_s, current, rng)
    raise TypeError(f"unknown behavior {type(params).__name__}")


PATH_COLUMNS: tuple[str, ...] = ("x_m", "y_m", "depth_m", "heading_rad", "speed_mps")


def simulate_path(
    params: BehaviorParams,
    start: KinState,
    n_ticks: int,
    *,
    dt_s: float,
    current: CurrentSampler,
    rng: np.random.Generator,
) -> npt.NDArray[np.float64]:
    """Rows of ``PATH_COLUMNS`` for ticks 0..n_ticks-1 (row 0 is ``start``)."""
    if n_ticks < 0:
        raise ValueError("n_ticks must be >= 0")
    out = np.empty((n_ticks, len(PATH_COLUMNS)), dtype=np.float64)
    state = start
    for tick in range(n_ticks):
        out[tick] = (state.x_m, state.y_m, state.depth_m, state.heading_rad, state.speed_mps)
        state = step(params, state, tick=tick, dt_s=dt_s, current=current, rng=rng)
    return out
