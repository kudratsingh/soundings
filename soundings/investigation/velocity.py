"""Least-squares velocity fits over sliding windows of an observed track.

Reading noise is sigma ~ 10-20 m at 2 s ticks, so tick-to-tick differences are
meaningless (WO-012 "Velocity estimation"). Every velocity in the investigation package
comes from a least-squares line fit of local-metre positions over a sliding window of
``FIT_WINDOW_S`` = 60 s.

Window rule (deterministic, documented once here):

* A window starts at every observed point ``i`` and holds the points with
  ``t_i <= t <= t_i + w``.
* For a track whose span is at least ``FIT_WINDOW_S``, ``w = FIT_WINDOW_S`` and only
  full windows are kept (``t_i + w <= t_last``), so no window is a truncated tail.
* For a shorter track, ``w = span / 2`` and every start ``i <= n - 2`` is kept, so a
  short track still yields ``n - 1`` overlapping windows (3 points give 2 windows).
* A window needs at least 2 points. If no window qualifies, one window covers the
  whole track.

Each fit also carries the trace of the slope covariance (``var_v``), from the reported
per-reading sigma when it is known and from the fit residuals otherwise. Callers use it
to remove the positive bias that noise adds to a squared speed, so a still contact
does not read as moving at the noise floor.

The module is pure: numpy in, numpy out, no randomness, no clock.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from numpy.typing import ArrayLike, NDArray

FloatArray = NDArray[np.float64]
IntArray = NDArray[np.int64]
BoolArray = NDArray[np.bool_]

FIT_WINDOW_S = 60.0
MIN_FIT_POINTS = 2
TIME_EPS_S = 1e-9


def _as_float_array(values: ArrayLike, name: str) -> FloatArray:
    arr = np.array(values, dtype=np.float64, copy=True)
    if arr.ndim != 1:
        raise ValueError(f"{name} must be one-dimensional")
    if not np.all(np.isfinite(arr)):
        raise ValueError(f"{name} must be finite")
    arr.setflags(write=False)
    return arr


@dataclass(frozen=True, eq=False)
class TrackArrays:
    """Observed readings of one contact as parallel float64 columns.

    ``t_s`` is seconds (tick x tick_s), strictly increasing. ``x_m``/``y_m`` are local
    metres (east, north). ``confidence`` is per reading, in [0, 1]. ``sigma_m`` is the
    reported per-axis position noise of each reading, or None when unknown.
    """

    t_s: FloatArray
    x_m: FloatArray
    y_m: FloatArray
    depth_m: FloatArray
    confidence: FloatArray
    sigma_m: FloatArray | None = None

    @classmethod
    def from_columns(
        cls,
        t_s: ArrayLike,
        x_m: ArrayLike,
        y_m: ArrayLike,
        depth_m: ArrayLike,
        confidence: ArrayLike,
        sigma_m: ArrayLike | None = None,
    ) -> TrackArrays:
        return cls(
            t_s=_as_float_array(t_s, "t_s"),
            x_m=_as_float_array(x_m, "x_m"),
            y_m=_as_float_array(y_m, "y_m"),
            depth_m=_as_float_array(depth_m, "depth_m"),
            confidence=_as_float_array(confidence, "confidence"),
            sigma_m=None if sigma_m is None else _as_float_array(sigma_m, "sigma_m"),
        )

    def __post_init__(self) -> None:
        n = self.t_s.shape[0]
        columns = [self.x_m, self.y_m, self.depth_m, self.confidence]
        if self.sigma_m is not None:
            columns.append(self.sigma_m)
        for col in columns:
            if col.ndim != 1 or col.shape[0] != n:
                raise ValueError("all TrackArrays columns must be 1-D and equally long")
        if n > 1 and not bool(np.all(np.diff(self.t_s) > 0.0)):
            raise ValueError("t_s must be strictly increasing")
        if n and (self.confidence.min() < 0.0 or self.confidence.max() > 1.0):
            raise ValueError("confidence must lie in [0, 1]")
        if self.sigma_m is not None and n and self.sigma_m.min() <= 0.0:
            raise ValueError("sigma_m must be positive")

    def __len__(self) -> int:
        return int(self.t_s.shape[0])

    @property
    def span_s(self) -> float:
        if len(self) < 2:
            return 0.0
        return float(self.t_s[-1] - self.t_s[0])

    def select(self, index: slice | IntArray | BoolArray) -> TrackArrays:
        return TrackArrays.from_columns(
            self.t_s[index],
            self.x_m[index],
            self.y_m[index],
            self.depth_m[index],
            self.confidence[index],
            None if self.sigma_m is None else self.sigma_m[index],
        )

    def tail(self, window_s: float) -> TrackArrays:
        """The readings in the last ``window_s`` seconds, inclusive of the boundary."""
        if len(self) == 0:
            return self
        cutoff = float(self.t_s[-1]) - window_s - TIME_EPS_S
        start = int(np.searchsorted(self.t_s, cutoff, side="right"))
        if start == 0:
            return self
        return self.select(slice(start, None))


@dataclass(frozen=True, eq=False)
class WindowFits:
    """One least-squares line fit per window, as parallel arrays in window order."""

    t_start: FloatArray
    t_end: FloatArray
    n_points: IntArray
    x_c: FloatArray  # fitted position at the window's mid-time
    y_c: FloatArray
    vx: FloatArray  # fitted velocity, m/s
    vy: FloatArray
    var_v: FloatArray  # trace of the slope covariance, (m/s)^2
    sigma_m: FloatArray  # per-axis position noise used for the window

    def __len__(self) -> int:
        return int(self.t_start.shape[0])

    @property
    def t_mid(self) -> FloatArray:
        return np.asarray(0.5 * (self.t_start + self.t_end), dtype=np.float64)

    @property
    def speed(self) -> FloatArray:
        return np.asarray(np.hypot(self.vx, self.vy), dtype=np.float64)

    @property
    def heading_rad(self) -> FloatArray:
        """Compass heading of each fitted velocity: 0 = north, clockwise positive."""
        return np.asarray(np.arctan2(self.vx, self.vy), dtype=np.float64)

    @property
    def displacement_m(self) -> FloatArray:
        """Distance the fitted line moves across its window (the window's step)."""
        return np.asarray(self.speed * (self.t_end - self.t_start), dtype=np.float64)

    @property
    def resolved(self) -> BoolArray:
        """Windows whose step is at least 2 sigma of position noise (heading is real)."""
        disp = self.displacement_m
        return np.asarray((disp >= 2.0 * self.sigma_m) & (disp > 0.0), dtype=np.bool_)


def _fit_one(
    t: FloatArray,
    x: FloatArray,
    y: FloatArray,
    sigma: FloatArray | None,
    t_start: float,
    t_end: float,
) -> tuple[float, float, float, float, float, float]:
    n = t.shape[0]
    t_bar = float(np.mean(t))
    tc = t - t_bar
    sxx = float(np.dot(tc, tc))
    x_bar = float(np.mean(x))
    y_bar = float(np.mean(y))
    vx = float(np.dot(tc, x - x_bar)) / sxx
    vy = float(np.dot(tc, y - y_bar)) / sxx
    if sigma is not None:
        s2 = float(np.mean(sigma * sigma))
    elif n > 2:
        rx = x - (x_bar + vx * tc)
        ry = y - (y_bar + vy * tc)
        s2 = (float(np.dot(rx, rx)) + float(np.dot(ry, ry))) / (2.0 * (n - 2))
    else:
        s2 = 0.0
    t_mid = 0.5 * (t_start + t_end)
    x_c = x_bar + vx * (t_mid - t_bar)
    y_c = y_bar + vy * (t_mid - t_bar)
    var_v = 2.0 * s2 / sxx
    return x_c, y_c, vx, vy, var_v, float(np.sqrt(s2))


def fit_windows(track: TrackArrays, window_s: float = FIT_WINDOW_S) -> WindowFits:
    """Least-squares velocity fits over sliding windows (see the module docstring)."""
    n = len(track)
    rows: list[tuple[float, float, int, float, float, float, float, float, float]] = []
    if n >= MIN_FIT_POINTS:
        t = track.t_s
        span = track.span_s
        short = span < window_s
        w = span / 2.0 if short else window_s
        t_last = float(t[-1])
        bounds: list[tuple[int, int]] = []
        for i in range(n - 1):
            t_i = float(t[i])
            if not short and t_i + w > t_last + TIME_EPS_S:
                break
            j = int(np.searchsorted(t, t_i + w + TIME_EPS_S, side="right"))
            if j - i >= MIN_FIT_POINTS:
                bounds.append((i, j))
        if not bounds:
            bounds.append((0, n))
        for i, j in bounds:
            sig = None if track.sigma_m is None else track.sigma_m[i:j]
            t_start = float(t[i])
            t_end = float(t[j - 1])
            x_c, y_c, vx, vy, var_v, s = _fit_one(
                t[i:j], track.x_m[i:j], track.y_m[i:j], sig, t_start, t_end
            )
            rows.append((t_start, t_end, j - i, x_c, y_c, vx, vy, var_v, s))

    def col(k: int) -> FloatArray:
        return np.array([r[k] for r in rows], dtype=np.float64)

    return WindowFits(
        t_start=col(0),
        t_end=col(1),
        n_points=np.array([r[2] for r in rows], dtype=np.int64),
        x_c=col(3),
        y_c=col(4),
        vx=col(5),
        vy=col(6),
        var_v=col(7),
        sigma_m=col(8),
    )


def wrap_deg(angle_deg: FloatArray) -> FloatArray:
    """Wrap angles to (-180, 180]."""
    wrapped = -((-np.asarray(angle_deg, dtype=np.float64) + 180.0) % 360.0) + 180.0
    return np.asarray(wrapped, dtype=np.float64)
