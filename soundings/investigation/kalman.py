"""Projected tracks: a hand-rolled 2-D constant-velocity Kalman filter (WO-084).

State is (x, y, vx, vy) in local metres (east, north) and m/s. The process model is
constant velocity with white-noise acceleration of spectral density ``q`` per axis::

    F = [[I, dt*I], [0, I]]        Q = q * [[dt^3/3 I, dt^2/2 I], [dt^2/2 I, dt I]]

The measurement picks (x, y) with noise R = sigma^2 I, where sigma is the reading's
reported ``sigma_m`` or, when the track carries none,
``default_sigma_m / max(confidence, min_confidence)``. Updates use the Joseph form and P
is symmetrised after every step. The first reading starts the filter at rest with
P0 = diag(s0^2, s0^2, v^2, v^2) (s0 its sigma, v ``init_speed_sigma_mps``). Gaps are
crossed by ``predict`` with the real dt, so uncertainty grows across them.

The filter is causal (no backward smoother): the state at a reading only uses readings
up to it. A 60 s projection is a stress-test estimate (SPEC-INVESTIGATION section 8): it
assumes the contact holds its velocity, and its 95% ellipse only covers that model's
uncertainty, not a turn.

Pure numpy: no randomness, no clock, no model calls, and observed tracks only.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any, Literal

import numpy as np
import yaml
from pydantic import Field

from soundings.canonical import round6
from soundings.investigation.velocity import FloatArray, TrackArrays
from soundings.schemas._base import CONFIG_DIR, StrictModel, from_file_data
from soundings.schemas.geo import from_local_m, to_local_m
from soundings.schemas.track import Track

TRACKING_PATH: Path = CONFIG_DIR / "tracking.yaml"

VelocitySource = Literal["window", "kalman"]
"""Where the closing-rate rows take their velocity: the WO-012 window fit or this filter."""

_I2 = np.eye(2, dtype=np.float64)
_H = np.hstack([_I2, np.zeros((2, 2), dtype=np.float64)])


class KalmanConfig(StrictModel):
    """The ``kalman`` block of ``config/tracking.yaml`` (stress-test values)."""

    q_m2_s3: float = Field(default=0.002, gt=0.0)
    init_speed_sigma_mps: float = Field(default=2.0, gt=0.0)
    default_sigma_m: float = Field(default=15.0, gt=0.0)
    min_confidence: float = Field(default=0.1, gt=0.0, le=1.0)
    projection_s: float = Field(default=60.0, gt=0.0)
    ellipse_p: float = Field(default=0.95, gt=0.0, lt=1.0)


class TrackingConfig(StrictModel):
    """``config/tracking.yaml``: the filter settings and the closing-velocity flag."""

    kalman: KalmanConfig = KalmanConfig()
    closing_velocity_source: VelocitySource = "window"


def load_tracking_config(path: str | Path = TRACKING_PATH) -> TrackingConfig:
    """Load ``config/tracking.yaml`` (unknown keys are rejected)."""
    doc: Any = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    return TrackingConfig.model_validate(from_file_data(doc or {}))


def load_kalman_config(path: str | Path = TRACKING_PATH) -> KalmanConfig:
    """The ``kalman`` block of ``config/tracking.yaml``."""
    return load_tracking_config(path).kalman


@cache
def default_kalman_config() -> KalmanConfig:
    """The committed ``config/tracking.yaml`` filter settings, read once per process."""
    return load_kalman_config()


@dataclass(frozen=True)
class KalmanState:
    """Filter state at time ``t_s``: ``x`` = (x, y, vx, vy), ``P`` row-major 4x4."""

    t_s: float
    x: tuple[float, float, float, float]
    P: tuple[float, ...]

    def __post_init__(self) -> None:
        if len(self.P) != 16:
            raise ValueError("P must hold 16 values (4x4 row-major)")

    @classmethod
    def from_arrays(cls, t_s: float, x: FloatArray, p: FloatArray) -> KalmanState:
        xs = tuple(float(v) for v in x)
        return cls(
            t_s=float(t_s), x=(xs[0], xs[1], xs[2], xs[3]), P=tuple(float(v) for v in p.ravel())
        )

    def arrays(self) -> tuple[FloatArray, FloatArray]:
        x = np.array(self.x, dtype=np.float64)
        p = np.array(self.P, dtype=np.float64).reshape(4, 4)
        return x, p

    def position_sigma_m(self) -> float:
        """Per-axis RMS position sigma: sqrt((Pxx + Pyy) / 2)."""
        return math.sqrt(max(0.5 * (self.P[0] + self.P[5]), 0.0))


def _fq(dt: float, q: float) -> tuple[FloatArray, FloatArray]:
    f = np.eye(4, dtype=np.float64)
    f[0, 2] = dt
    f[1, 3] = dt
    q_block = q * np.array([[dt**3 / 3.0, dt**2 / 2.0], [dt**2 / 2.0, dt]], dtype=np.float64)
    qm = np.asarray(np.kron(q_block, _I2), dtype=np.float64)
    return f, qm


def _sym(p: FloatArray) -> FloatArray:
    return np.asarray(0.5 * (p + p.T), dtype=np.float64)


def _predict(x: FloatArray, p: FloatArray, dt: float, q: float) -> tuple[FloatArray, FloatArray]:
    if dt < 0.0:
        raise ValueError("predict needs dt >= 0")
    if dt == 0.0:
        return x, p
    f, qm = _fq(dt, q)
    return f @ x, _sym(f @ p @ f.T + qm)


def _update(
    x: FloatArray, p: FloatArray, z: FloatArray, sigma_m: float
) -> tuple[FloatArray, FloatArray]:
    if not sigma_m > 0.0:
        raise ValueError("measurement sigma must be positive")
    r = (sigma_m * sigma_m) * _I2
    s = _H @ p @ _H.T + r
    k = np.linalg.solve(s, _H @ p).T  # P H^T S^-1, with S and P symmetric
    innovation = z - _H @ x
    x_new = x + k @ innovation
    a = np.eye(4, dtype=np.float64) - k @ _H
    p_new = a @ p @ a.T + k @ r @ k.T  # Joseph form
    return x_new, _sym(p_new)


def predict(state: KalmanState, dt: float, cfg: KalmanConfig) -> KalmanState:
    """Advance the state ``dt`` seconds under constant velocity (P grows by Q)."""
    x, p = _predict(*state.arrays(), dt, cfg.q_m2_s3)
    return KalmanState.from_arrays(state.t_s + dt, x, p)


def update(
    state: KalmanState, z_xy: tuple[float, float], sigma_m: float, cfg: KalmanConfig
) -> KalmanState:
    """Fold in one (x, y) reading with per-axis noise ``sigma_m`` (Joseph form)."""
    del cfg  # the measurement noise is the reading's own sigma
    x, p = _update(*state.arrays(), np.array(z_xy, dtype=np.float64), sigma_m)
    return KalmanState.from_arrays(state.t_s, x, p)


def initial_state(
    t_s: float, z_xy: tuple[float, float], sigma_m: float, cfg: KalmanConfig
) -> KalmanState:
    """Start at the first reading, at rest: P0 = diag(s0^2, s0^2, v^2, v^2)."""
    v2 = cfg.init_speed_sigma_mps**2
    s2 = sigma_m * sigma_m
    p0 = np.diag([s2, s2, v2, v2]).astype(np.float64)
    return KalmanState.from_arrays(t_s, np.array([z_xy[0], z_xy[1], 0.0, 0.0]), p0)


def reading_sigmas(track: TrackArrays, cfg: KalmanConfig) -> FloatArray:
    """Per-reading measurement sigma: reported ``sigma_m``, else default / confidence."""
    if track.sigma_m is not None:
        return np.asarray(track.sigma_m, dtype=np.float64)
    conf = np.maximum(track.confidence, cfg.min_confidence)
    return np.asarray(cfg.default_sigma_m / conf, dtype=np.float64)


@dataclass(frozen=True)
class KalmanTrack:
    """The causal forward pass: per-reading filtered values (6 dp) and the last state."""

    t_s: tuple[float, ...]
    x_m: tuple[float, ...]
    y_m: tuple[float, ...]
    vx: tuple[float, ...]
    vy: tuple[float, ...]
    sigma_pos_m: tuple[float, ...]
    last: KalmanState

    def __len__(self) -> int:
        return len(self.t_s)

    def as_dict(self) -> dict[str, Any]:
        return {
            "t_s": list(self.t_s),
            "x_m": list(self.x_m),
            "y_m": list(self.y_m),
            "vx": list(self.vx),
            "vy": list(self.vy),
            "sigma_pos_m": list(self.sigma_pos_m),
            "last": {"t_s": self.last.t_s, "x": list(self.last.x), "P": list(self.last.P)},
        }


def filter_track(track: TrackArrays, cfg: KalmanConfig) -> KalmanTrack:
    """Run the filter forward over every reading of ``track`` (at least one reading)."""
    n = len(track)
    if n == 0:
        raise ValueError("filter_track needs at least one reading")
    sig = reading_sigmas(track, cfg)
    t = track.t_s
    first = initial_state(
        float(t[0]), (float(track.x_m[0]), float(track.y_m[0])), float(sig[0]), cfg
    )
    x, p = first.arrays()
    out: list[tuple[float, float, float, float, float, float]] = []
    for i in range(n):
        if i > 0:
            x, p = _predict(x, p, float(t[i] - t[i - 1]), cfg.q_m2_s3)
            z = np.array([track.x_m[i], track.y_m[i]], dtype=np.float64)
            x, p = _update(x, p, z, float(sig[i]))
        s_pos = math.sqrt(max(0.5 * (float(p[0, 0]) + float(p[1, 1])), 0.0))
        out.append((float(t[i]), float(x[0]), float(x[1]), float(x[2]), float(x[3]), s_pos))

    def col(k: int) -> tuple[float, ...]:
        return tuple(round6(r[k]) for r in out)

    return KalmanTrack(
        t_s=col(0),
        x_m=col(1),
        y_m=col(2),
        vx=col(3),
        vy=col(4),
        sigma_pos_m=col(5),
        last=KalmanState.from_arrays(float(t[-1]), x, p),
    )


def kalman_state_xyv(
    track: TrackArrays, cfg: KalmanConfig | None = None
) -> tuple[float, float, float, float] | None:
    """(x, y, vx, vy) of the filter at the last reading; None below 2 points.

    Same contract as ``features.last_fitted_state``; the filter reads the whole track.
    """
    if len(track) < 2:
        return None
    last = filter_track(track, default_kalman_config() if cfg is None else cfg).last
    return last.x


def chi2_2dof(p: float) -> float:
    """Chi-square quantile with 2 degrees of freedom: -2 ln(1 - p)."""
    return -2.0 * math.log(1.0 - p)


class ProjectionEllipse(StrictModel):
    """Position uncertainty ellipse: semi-axes in metres, compass bearing of the major
    axis in [0, 180), and the probability ``p`` it encloses."""

    semi_major_m: float
    semi_minor_m: float
    orientation_deg: float
    p: float


def cov_eigenvalues(a: float, b: float, c: float) -> tuple[float, float]:
    """(lambda_major, lambda_minor) of [[a, b], [b, c]] in closed form (no ``eigh``)."""
    half_sum = 0.5 * (a + c)
    root = math.sqrt((0.5 * (a - c)) ** 2 + b * b)
    return max(half_sum + root, 0.0), max(half_sum - root, 0.0)


def ellipse_from_cov(a: float, b: float, c: float, p: float) -> ProjectionEllipse:
    """Closed-form ellipse of the 2x2 covariance [[a, b], [b, c]] (x east, y north)."""
    lam_major, lam_minor = cov_eigenvalues(a, b, c)
    k = chi2_2dof(p)
    theta_deg = math.degrees(0.5 * math.atan2(2.0 * b, a - c))  # east-of-x, CCW
    orientation = (90.0 - theta_deg) % 180.0
    return ProjectionEllipse(
        semi_major_m=round6(math.sqrt(k * lam_major)),
        semi_minor_m=round6(math.sqrt(k * lam_minor)),
        orientation_deg=round6(orientation) % 180.0,
        p=round6(p),
    )


class Projection(StrictModel):
    """Where constant velocity puts the contact ``horizon_s`` after ``state``."""

    t_s: float
    x_m: float
    y_m: float
    vx: float
    vy: float
    ellipse: ProjectionEllipse


def project(state: KalmanState, horizon_s: float, cfg: KalmanConfig) -> Projection:
    """Predict ``horizon_s`` ahead (no update) and read the position ellipse."""
    ahead = predict(state, horizon_s, cfg)
    x, p = ahead.arrays()
    return Projection(
        t_s=round6(ahead.t_s),
        x_m=round6(float(x[0])),
        y_m=round6(float(x[1])),
        vx=round6(float(x[2])),
        vy=round6(float(x[3])),
        ellipse=ellipse_from_cov(float(p[0, 0]), float(p[0, 1]), float(p[1, 1]), cfg.ellipse_p),
    )


class ProjectionView(StrictModel):
    """JSON-ready projected track of one contact at its last reading (for WO-086)."""

    contact_id: str
    tick: int
    lat: float
    lon: float
    vx: float
    vy: float
    speed_mps: float
    heading_deg: float
    proj_lat: float
    proj_lon: float
    ellipse: ProjectionEllipse


def _track_columns(track: Track) -> TrackArrays:
    xy = [to_local_m(pt.lat, pt.lon) for pt in track.points]
    return TrackArrays.from_columns(
        [pt.tick * track.tick_s for pt in track.points],
        [x for x, _ in xy],
        [y for _, y in xy],
        [pt.depth_m for pt in track.points],
        [pt.confidence for pt in track.points],
        [pt.sigma_m for pt in track.points],
    )


def projection_view(track: Track, cfg: KalmanConfig | None = None) -> ProjectionView | None:
    """Filtered position, velocity and ``cfg.projection_s`` projection; None below 2
    readings."""
    if len(track.points) < 2:
        return None
    cfg = default_kalman_config() if cfg is None else cfg
    last = filter_track(_track_columns(track), cfg).last
    x, y, vx, vy = last.x
    proj = project(last, cfg.projection_s, cfg)
    lat, lon = from_local_m(x, y)
    proj_lat, proj_lon = from_local_m(proj.x_m, proj.y_m)
    speed = math.hypot(vx, vy)
    heading = round6(math.degrees(math.atan2(vx, vy)) % 360.0) % 360.0
    return ProjectionView(
        contact_id=track.contact_id,
        tick=track.points[-1].tick,
        lat=round6(lat),
        lon=round6(lon),
        vx=round6(vx),
        vy=round6(vy),
        speed_mps=round6(speed),
        heading_deg=heading,
        proj_lat=round6(proj_lat),
        proj_lon=round6(proj_lon),
        ellipse=proj.ellipse,
    )


__all__ = [
    "KalmanConfig",
    "KalmanState",
    "KalmanTrack",
    "Projection",
    "ProjectionEllipse",
    "ProjectionView",
    "TRACKING_PATH",
    "TrackingConfig",
    "VelocitySource",
    "chi2_2dof",
    "cov_eigenvalues",
    "default_kalman_config",
    "ellipse_from_cov",
    "filter_track",
    "initial_state",
    "kalman_state_xyv",
    "load_kalman_config",
    "load_tracking_config",
    "predict",
    "project",
    "projection_view",
    "reading_sigmas",
    "update",
]
