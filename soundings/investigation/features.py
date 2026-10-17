"""Feature definitions, buckets and the schema-independent feature mathematics.

This is the canonical bucket definition named in the ``config/likelihoods.yaml`` header
(WO-012 "Buckets"). The kinematic functions here are pure functions over
:class:`~soundings.investigation.velocity.TrackArrays` (seconds, local metres, depth,
confidence); they never see ground truth, only observed readings.

A function returns ``None`` when the feature is ``unknown`` at that point; the reason
string to write in the row's ``source`` is ``FEATURE_CATALOG[feature].unknown_source``.
Every float returned is rounded to 6 dp.
"""

from __future__ import annotations

import math
from bisect import bisect_right
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal

import numpy as np
import yaml
from numpy.typing import ArrayLike

from soundings.investigation.velocity import (
    FIT_WINDOW_S,
    BoolArray,
    FloatArray,
    TrackArrays,
    WindowFits,
    fit_windows,
    wrap_deg,
)

DECIMALS: Final = 6
UNKNOWN: Final = "unknown"

# --- windows and thresholds (WO-012 binding definitions) -----------------------------

KIN_WINDOW_S: Final = 120.0  # speed, speed/depth variance, heading, turn, closing
PATTERN_WINDOW_S: Final = 600.0  # transit_pattern and loiter_score
LOITER_WINDOW_S: Final = 300.0  # minimum span for loiter_score
DRIFT_WINDOW_S: Final = 600.0  # drift_consistency pools the windows of this span
NOT_CLOSING_S: Final = 1e9  # time_to_nearest_critical_asset for a receding contact
CRITICAL_ASSET_MIN: Final = 0.7  # config/priors.yaml critical_zone.zone_criticality_min

MIN_SPEED_SPAN_S: Final = 4.0
MIN_PATTERN_SPAN_S: Final = 60.0
MIN_RESOLVED_STEPS: Final = 3  # heading_persistence needs 3 steps of >= 2 sigma

LOITER_ASSET_RADIUS_M: Final = 200.0  # loiter_score = 0.0 when no asset is this close
LOITER_RADIUS_M: Final = 75.0  # R: "within R metres of one point near an asset"
LOITER_SMOOTH_S: Final = 30.0  # centred moving mean before measuring loiter distance

DRIFT_EPS_MPS: Final = 1e-6
SPREAD_SIGNIFICANCE: Final = 2.0  # window spread must beat noise by 2 sampling sigmas
CLOSING_EPS_MPS: Final = 1e-6

# transit_pattern classifier constants (rules checked in this order)
STATIONARY_SPEED_MPS: Final = 0.1
ORBIT_MIN_TURN_DEG: Final = 360.0
ORBIT_SIGN_FRACTION: Final = 0.8
TURN_STEP_S: Final = 60.0  # spacing of heading steps for the orbit sign test (one window)
REVERSAL_MIN_DEG: Final = 150.0  # |turn| in [150, 210] deg  <=>  |wrapped turn| >= 150
LAWNMOWER_MIN_REVERSALS: Final = 2
LEG_TOL_DEG: Final = 30.0  # a window joins the current leg within this of its heading
MIN_LEG_S: Final = 30.0  # a leg must span this much window mid-time to count
MAX_TURN_GAP_S: Final = 90.0  # max time between two legs for their turn to count
STRAIGHT_PERSISTENCE: Final = 0.9

TransitPatternName = Literal["straight", "lawnmower", "orbit", "random", "stationary"]
TRANSIT_PATTERNS: Final[tuple[TransitPatternName, ...]] = (
    "straight",
    "lawnmower",
    "orbit",
    "random",
    "stationary",
)

CurrentAt = Callable[[float, float, float], tuple[float, float]]
"""``current_at(x_m, y_m, t_s) -> (u_east_mps, v_north_mps)`` of the configured field."""


def _r6(value: float) -> float:
    return round(float(value), DECIMALS)


# --- buckets ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BucketSpec:
    """Bucket names, plus left-closed thresholds for numeric features.

    ``v < thresholds[0]`` gives ``names[0]``; ``thresholds[k-1] <= v < thresholds[k]``
    gives ``names[k]``. Categorical and boolean features have ``thresholds=None``.
    """

    names: tuple[str, ...]
    thresholds: tuple[float, ...] | None = None

    def __post_init__(self) -> None:
        if len(set(self.names)) != len(self.names) or not self.names:
            raise ValueError("bucket names must be non-empty and unique")
        if self.thresholds is not None:
            if len(self.thresholds) != len(self.names) - 1:
                raise ValueError("a numeric bucket spec needs len(names) - 1 thresholds")
            if list(self.thresholds) != sorted(set(self.thresholds)):
                raise ValueError("thresholds must be strictly increasing")


BUCKETS: Final[dict[str, BucketSpec]] = {
    "drift_consistency": BucketSpec(("low", "mid", "high"), (0.3, 0.7)),
    "propeller_tonal_present": BucketSpec(("false", "true")),
    "breathing_or_click_present": BucketSpec(("false", "true")),
    "transit_pattern": BucketSpec(TRANSIT_PATTERNS),
    "depth": BucketSpec(("surface", "shallow", "mid", "deep"), (1.0, 4.0, 10.0)),
    "speed_over_ground": BucketSpec(("drift", "slow", "cruise", "fast"), (0.3, 0.8, 2.0)),
    "ais_surface_correlation": BucketSpec(("none", "weak", "strong"), (0.3, 0.7)),
    "registry_match": BucketSpec(("exact", "area_mismatch", "window_mismatch", "none")),
    "hail_reply": BucketSpec(("valid", "invalid", "none", "timeout")),
    "pattern_of_life_anomaly": BucketSpec(("normal", "unusual", "anomalous"), (0.5, 0.85)),
    "loiter_score": BucketSpec(("low", "mid", "high"), (0.3, 0.7)),
}

BOOL_BUCKETS: Final = ("false", "true")


def bucketize(feature: str, value: bool | int | float | str) -> str | None:
    """Map a feature value to its bucket name; ``"unknown"`` maps to None (skipped).

    Raises KeyError for a feature with no buckets and ValueError for a value that does
    not fit the feature's bucket kind.
    """
    spec = BUCKETS[feature]
    if isinstance(value, str) and value == UNKNOWN:
        return None
    if spec.names == BOOL_BUCKETS:
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, str) and value in BOOL_BUCKETS:
            return value
        raise ValueError(f"{feature} expects a boolean, got {value!r}")
    if spec.thresholds is None:
        if isinstance(value, str) and value in spec.names:
            return value
        raise ValueError(f"{feature} expects one of {spec.names}, got {value!r}")
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"{feature} expects a number, got {value!r}")
    v = float(value)
    if not math.isfinite(v):
        raise ValueError(f"{feature} expects a finite number, got {value!r}")
    return spec.names[bisect_right(spec.thresholds, v)]


DEFAULT_LIKELIHOODS_PATH: Final = (
    Path(__file__).resolve().parent.parent.parent / "config" / "likelihoods.yaml"
)


def _bucket_name(raw: object) -> str:
    # PyYAML reads the bucket names `false` / `true` as booleans; they are strings here.
    if isinstance(raw, bool):
        return "true" if raw else "false"
    return str(raw)


def load_yaml_bucket_specs(path: Path = DEFAULT_LIKELIHOODS_PATH) -> dict[str, BucketSpec]:
    """Bucket names and thresholds as written in ``config/likelihoods.yaml``.

    Only used to check ``BUCKETS`` against the YAML (the scorer reads the YAML through
    the WO-010 loader once it exists).
    """
    with path.open(encoding="utf-8") as fh:
        doc = yaml.safe_load(fh)
    out: dict[str, BucketSpec] = {}
    for name, body in doc["features"].items():
        names = tuple(_bucket_name(b) for b in body["buckets"])
        raw_thresholds = body.get("thresholds")
        thresholds = None if raw_thresholds is None else tuple(float(x) for x in raw_thresholds)
        out[str(name)] = BucketSpec(names, thresholds)
    return out


# --- feature catalog -------------------------------------------------------------------

FeatureGroup = Literal["kinematic", "acoustic", "registry", "context", "support"]

# Unknown reasons (the row's `source` when value == "unknown").
INSUFFICIENT_POINTS: Final = "insufficient_points"
NO_CRITICAL_ASSET: Final = "no_critical_asset"
NO_SENSOR_COVERAGE: Final = "no_sensor_coverage"
NOT_REPORTED_BY_DETECTOR: Final = "not_reported_by_detector"
NO_AIS_FEED: Final = "no_ais_feed"
PLACEHOLDER_PHASE2: Final = "placeholder:phase2"
POL_CELL_EMPTY: Final = "pol_cell_empty"
NO_POL_TABLE: Final = "no_pol_table"
UNKNOWN_SOURCES: Final = (
    INSUFFICIENT_POINTS,
    NO_CRITICAL_ASSET,
    NO_SENSOR_COVERAGE,
    NOT_REPORTED_BY_DETECTOR,
    NO_AIS_FEED,
    PLACEHOLDER_PHASE2,
    POL_CELL_EMPTY,
    NO_POL_TABLE,
)


@dataclass(frozen=True)
class FeatureSpec:
    """One catalog entry.

    ``unit`` uses the WO-010 ``Unit`` vocabulary. ``extractor`` is the name of the
    function in this module that computes the value, or ``"placeholder:phase2"``.
    ``min_points`` / ``min_span_s`` are the WO-012 ``MIN_POINTS`` rule; ``unknown_source``
    is the main reason written when the feature cannot be computed,
    ``other_unknown_sources`` the other reasons it may carry, and ``unknown_rule`` says
    when each happens in words.
    """

    group: FeatureGroup
    unit: str
    extractor: str
    min_points: int = 1
    min_span_s: float = 0.0
    unknown_source: str = INSUFFICIENT_POINTS
    unknown_rule: str = ""
    other_unknown_sources: tuple[str, ...] = ()


_FEWER_THAN_3 = "fewer than 3 points"

FEATURE_CATALOG: Final[dict[str, FeatureSpec]] = {
    # kinematic (SPEC-INVESTIGATION section 2, in section 2 order)
    "speed_over_ground": FeatureSpec(
        "kinematic",
        "m/s",
        "speed_over_ground",
        2,
        MIN_SPEED_SPAN_S,
        INSUFFICIENT_POINTS,
        "fewer than 2 points, or a span under 4 s",
    ),
    "speed_variance": FeatureSpec(
        "kinematic", "m/s2", "speed_variance", 3, 0.0, INSUFFICIENT_POINTS, _FEWER_THAN_3
    ),
    "depth": FeatureSpec("kinematic", "m", "depth", 1, 0.0, INSUFFICIENT_POINTS, "no points"),
    "depth_variance": FeatureSpec(
        "kinematic", "m", "depth_variance", 3, 0.0, INSUFFICIENT_POINTS, _FEWER_THAN_3
    ),
    "heading_persistence": FeatureSpec(
        "kinematic",
        "ratio",
        "heading_persistence",
        3,
        0.0,
        INSUFFICIENT_POINTS,
        "fewer than 3 fitted steps whose displacement is at least 2 sigma position noise",
    ),
    "turn_rate": FeatureSpec(
        "kinematic", "deg/s", "turn_rate", 3, 0.0, INSUFFICIENT_POINTS, _FEWER_THAN_3
    ),
    "closing_rate_to_nearest_critical_asset": FeatureSpec(
        "kinematic",
        "m/s",
        "closing_to_asset",
        2,
        0.0,
        INSUFFICIENT_POINTS,
        "fewer than 2 points; no_critical_asset when no asset has criticality >= 0.7",
        (NO_CRITICAL_ASSET,),
    ),
    "time_to_nearest_critical_asset": FeatureSpec(
        "kinematic",
        "s",
        "closing_to_asset",
        2,
        0.0,
        INSUFFICIENT_POINTS,
        "fewer than 2 points; no_critical_asset when no asset has criticality >= 0.7",
        (NO_CRITICAL_ASSET,),
    ),
    "dwell_time_in_zone": FeatureSpec(
        "kinematic",
        "s",
        "dwell_time_s",
        1,
        0.0,
        NO_SENSOR_COVERAGE,
        "the last point lies outside every zone",
    ),
    "track_gap_ratio": FeatureSpec(
        "kinematic", "ratio", "track_gap_ratio", 1, 0.0, INSUFFICIENT_POINTS, "no points"
    ),
    "drift_consistency": FeatureSpec(
        "kinematic", "ratio", "drift_consistency", 3, 0.0, INSUFFICIENT_POINTS, _FEWER_THAN_3
    ),
    "loiter_score": FeatureSpec(
        "kinematic",
        "ratio",
        "loiter_score",
        2,
        LOITER_WINDOW_S,
        INSUFFICIENT_POINTS,
        "a span under 300 s",
    ),
    "transit_pattern": FeatureSpec(
        "kinematic",
        "category",
        "classify_transit_pattern",
        10,
        MIN_PATTERN_SPAN_S,
        INSUFFICIENT_POINTS,
        "fewer than 10 points, or a span under 60 s",
    ),
    # acoustic (passthrough of TrackPoint.acoustic)
    "propeller_tonal_present": FeatureSpec(
        "acoustic",
        "bool",
        "extract_acoustic",
        1,
        0.0,
        NOT_REPORTED_BY_DETECTOR,
        "the detector never reports the field",
    ),
    "tonal_band": FeatureSpec(
        "acoustic",
        "category",
        "extract_acoustic",
        1,
        0.0,
        NOT_REPORTED_BY_DETECTOR,
        "the detector never reports the field",
    ),
    "cavitation_index": FeatureSpec(
        "acoustic",
        "ratio",
        "extract_acoustic",
        1,
        0.0,
        NOT_REPORTED_BY_DETECTOR,
        "the detector never reports the field",
    ),
    "breathing_or_click_present": FeatureSpec(
        "acoustic",
        "bool",
        "extract_acoustic",
        1,
        0.0,
        NOT_REPORTED_BY_DETECTOR,
        "the detector never reports the field",
    ),
    "broadband_level": FeatureSpec(
        "acoustic",
        "dB",
        "extract_acoustic",
        1,
        0.0,
        NOT_REPORTED_BY_DETECTOR,
        "the detector never reports the field",
    ),
    # registry / identity (Phase 2 placeholders)
    "registry_match": FeatureSpec(
        "registry", "category", PLACEHOLDER_PHASE2, 0, 0.0, PLACEHOLDER_PHASE2, "Phase 2"
    ),
    "plan_id": FeatureSpec(
        "registry", "id", PLACEHOLDER_PHASE2, 0, 0.0, PLACEHOLDER_PHASE2, "Phase 2"
    ),
    "modem_id": FeatureSpec(
        "registry", "id", PLACEHOLDER_PHASE2, 0, 0.0, PLACEHOLDER_PHASE2, "Phase 2"
    ),
    "hail_sent_at": FeatureSpec(
        "registry", "s", PLACEHOLDER_PHASE2, 0, 0.0, PLACEHOLDER_PHASE2, "Phase 2"
    ),
    "hail_reply": FeatureSpec(
        "registry", "category", PLACEHOLDER_PHASE2, 0, 0.0, PLACEHOLDER_PHASE2, "Phase 2"
    ),
    "operator_known": FeatureSpec(
        "registry", "bool", PLACEHOLDER_PHASE2, 0, 0.0, PLACEHOLDER_PHASE2, "Phase 2"
    ),
    # context
    "zone_id": FeatureSpec(
        "context",
        "id",
        "extract_context",
        1,
        0.0,
        NO_SENSOR_COVERAGE,
        "the last point lies outside every zone",
    ),
    "zone_criticality": FeatureSpec(
        "context",
        "ratio",
        "extract_context",
        1,
        0.0,
        NO_SENSOR_COVERAGE,
        "the last point lies outside every zone",
    ),
    "nearest_asset_id": FeatureSpec(
        "context", "id", "extract_context", 1, 0.0, INSUFFICIENT_POINTS, "no points"
    ),
    "hour_of_day": FeatureSpec(
        "context", "hour", "extract_context", 0, 0.0, INSUFFICIENT_POINTS, "never"
    ),
    "pattern_of_life_anomaly": FeatureSpec(
        "context",
        "ratio",
        "anomaly_score",
        1,
        0.0,
        POL_CELL_EMPTY,
        "the zone/hour/weekday cell is empty; no_pol_table when no table is loaded",
        (NO_POL_TABLE,),
    ),
    "ais_surface_correlation": FeatureSpec(
        "context",
        "ratio",
        "extract_context",
        0,
        0.0,
        NO_AIS_FEED,
        "always in Phase 1: no surface feed is simulated",
    ),
    "sensor_health": FeatureSpec(
        "context",
        "ratio",
        "extract_context",
        1,
        0.0,
        NO_SENSOR_COVERAGE,
        "no sensor node covers the contact",
    ),
    "visibility_for_camera": FeatureSpec(
        "context",
        "ratio",
        "extract_context",
        0,
        0.0,
        INSUFFICIENT_POINTS,
        "never (assumption row from the hour-of-day default)",
    ),
    # support (not scored)
    "track_segment": FeatureSpec(
        "support", "category", "track_segment", 1, 0.0, INSUFFICIENT_POINTS, "no points"
    ),
}

MIN_POINTS: Final[dict[str, tuple[int, float]]] = {
    name: (spec.min_points, spec.min_span_s) for name, spec in FEATURE_CATALOG.items()
}
"""feature -> (minimum points, minimum span in seconds) below which it is unknown."""


def has_min_points(feature: str, track: TrackArrays) -> bool:
    """True when ``track`` meets the feature's point and span minimum."""
    points, span = MIN_POINTS[feature]
    return len(track) >= points and (span <= 0.0 or track.span_s >= span)


# --- kinematic features ----------------------------------------------------------------


def track_confidence(track: TrackArrays) -> float:
    """Minimum reading confidence (a derived row's ceiling); 0.0 for an empty track."""
    if len(track) == 0:
        return 0.0
    return _r6(float(track.confidence.min()))


def _debiased_mean_speed(fits: WindowFits) -> float:
    """RMS fitted speed with the mean slope-noise variance removed before the floor.

    Averaging before flooring at 0 keeps the estimate unbiased for a still contact; a
    per-window floor would leave a positive bias of about 0.37 x the noise variance.
    """
    if len(fits) == 0:
        return 0.0
    raw = float(np.mean(fits.vx * fits.vx + fits.vy * fits.vy - fits.var_v))
    return math.sqrt(max(raw, 0.0))


def speed_over_ground(track: TrackArrays) -> float | None:
    """Mean fitted speed (m/s) over the last ``KIN_WINDOW_S``, noise bias removed."""
    window = track.tail(KIN_WINDOW_S)
    if not has_min_points("speed_over_ground", window):
        return None
    return _r6(_debiased_mean_speed(fit_windows(window)))


def speed_variance(track: TrackArrays) -> float | None:
    """Population variance of the fitted window speeds over the last ``KIN_WINDOW_S``."""
    window = track.tail(KIN_WINDOW_S)
    if not has_min_points("speed_variance", window):
        return None
    return _r6(float(np.var(fit_windows(window).speed)))


def depth(track: TrackArrays) -> float | None:
    """Mean reported depth (m) over the last ``KIN_WINDOW_S``."""
    window = track.tail(KIN_WINDOW_S)
    if not has_min_points("depth", window):
        return None
    return _r6(float(np.mean(window.depth_m)))


def depth_variance(track: TrackArrays) -> float | None:
    """Population variance of reported depth over the last ``KIN_WINDOW_S``."""
    window = track.tail(KIN_WINDOW_S)
    if not has_min_points("depth_variance", window):
        return None
    return _r6(float(np.var(window.depth_m)))


def _persistence(fits: WindowFits, mask: BoolArray) -> float | None:
    """Mean resultant length of the resolved window headings, or None below 3 steps."""
    if int(np.count_nonzero(mask)) < MIN_RESOLVED_STEPS:
        return None
    h = fits.heading_rad[mask]
    return float(np.hypot(np.mean(np.sin(h)), np.mean(np.cos(h))))


def heading_persistence(track: TrackArrays, window_s: float = KIN_WINDOW_S) -> float | None:
    """How straight (0-1): mean resultant length of resolved fitted headings.

    A step is one fitted window; it is resolved when the fitted line moves at least
    2 sigma of position noise across the window. Fewer than 3 resolved steps gives
    unknown, so an all-noise track is unknown here (and ``stationary`` as a pattern).
    """
    fits = fit_windows(track.tail(window_s))
    value = _persistence(fits, fits.resolved)
    return None if value is None else _r6(value)


def _cumulative_turn_deg(fits: WindowFits, mask: BoolArray) -> tuple[float, float]:
    """Signed turn (deg, clockwise positive) over resolved windows and its time span."""
    idx = np.flatnonzero(mask)
    if idx.shape[0] < 2:
        return 0.0, 0.0
    h_deg = np.degrees(fits.heading_rad[idx])
    turn = float(np.sum(wrap_deg(np.diff(h_deg))))
    span = float(fits.t_mid[idx[-1]] - fits.t_mid[idx[0]])
    return turn, span


def turn_rate(track: TrackArrays) -> float | None:
    """Signed turn rate (deg/s, clockwise positive) over the last ``KIN_WINDOW_S``.

    Net heading change across the resolved fitted windows divided by the time between
    the first and last of them; 0.0 when fewer than two windows resolve a heading.
    """
    window = track.tail(KIN_WINDOW_S)
    if not has_min_points("turn_rate", window):
        return None
    fits = fit_windows(window)
    turn, span = _cumulative_turn_deg(fits, fits.resolved)
    if span <= 0.0:
        return 0.0
    return _r6(turn / span)


def track_gap_ratio(t_s: ArrayLike, tick_s: float) -> float | None:
    """Fraction of ticks between the first and last reading that have no reading.

    Array fallback; the evidence builder uses WO-010 ``Track.gap_ratio`` when present.
    """
    t = np.asarray(t_s, dtype=np.float64)
    n = int(t.shape[0])
    if n == 0:
        return None
    if n == 1:
        return 0.0
    expected = int(round(float(t[-1] - t[0]) / tick_s)) + 1
    return _r6(max(0.0, 1.0 - n / expected))


def dwell_time_s(t_s: ArrayLike, in_zone: ArrayLike) -> float | None:
    """Seconds the contact has been continuously observed in its current zone.

    ``in_zone[i]`` says whether reading ``i`` lies in the zone of the last reading.
    None (unknown) when the last reading lies in no zone or there are no readings.
    """
    t = np.asarray(t_s, dtype=np.float64)
    inside = np.asarray(in_zone, dtype=np.bool_)
    if t.shape != inside.shape:
        raise ValueError("t_s and in_zone must have the same shape")
    if t.shape[0] == 0 or not bool(inside[-1]):
        return None
    outside = np.flatnonzero(~inside)
    first = 0 if outside.shape[0] == 0 else int(outside[-1]) + 1
    return _r6(float(t[-1] - t[first]))


def _smoothed_xy(track: TrackArrays, half_s: float) -> tuple[FloatArray, FloatArray]:
    t = track.t_s
    lo = np.searchsorted(t, t - half_s - 1e-9, side="left")
    hi = np.searchsorted(t, t + half_s + 1e-9, side="right")
    cx = np.concatenate(([0.0], np.cumsum(track.x_m)))
    cy = np.concatenate(([0.0], np.cumsum(track.y_m)))
    count = (hi - lo).astype(np.float64)
    xs = np.asarray((cx[hi] - cx[lo]) / count, dtype=np.float64)
    ys = np.asarray((cy[hi] - cy[lo]) / count, dtype=np.float64)
    return xs, ys


def _as_points(points_xy: ArrayLike) -> FloatArray:
    arr = np.asarray(points_xy, dtype=np.float64)
    if arr.size == 0:
        return np.zeros((0, 2), dtype=np.float64)
    arr = arr.reshape(-1, 2)
    return np.asarray(arr, dtype=np.float64)


def loiter_score(track: TrackArrays, assets_xy: ArrayLike) -> float | None:
    """Fraction (0-1) of the last ``PATTERN_WINDOW_S`` spent within ``LOITER_RADIUS_M``
    of one point near an asset.

    Positions are smoothed with a centred ``LOITER_SMOOTH_S`` moving mean. Candidate
    points are the assets within ``LOITER_ASSET_RADIUS_M`` of the smoothed track and
    the smoothed positions within that radius of an asset; the score is the largest
    fraction of readings within ``LOITER_RADIUS_M`` of one candidate. 0.0 when no asset
    is within ``LOITER_ASSET_RADIUS_M``; unknown when the window spans under 300 s.
    """
    window = track.tail(PATTERN_WINDOW_S)
    if not has_min_points("loiter_score", window):
        return None
    assets = _as_points(assets_xy)
    if assets.shape[0] == 0:
        return 0.0
    xs, ys = _smoothed_xy(window, LOITER_SMOOTH_S / 2.0)
    pos = np.stack([xs, ys], axis=1)
    d_pos_asset = np.hypot(
        pos[:, None, 0] - assets[None, :, 0], pos[:, None, 1] - assets[None, :, 1]
    )
    near = d_pos_asset <= LOITER_ASSET_RADIUS_M
    if not bool(near.any()):
        return 0.0
    candidates = np.concatenate([assets[near.any(axis=0)], pos[near.any(axis=1)]], axis=0)
    d = np.hypot(pos[:, None, 0] - candidates[None, :, 0], pos[:, None, 1] - candidates[None, :, 1])
    fraction = np.mean(d <= LOITER_RADIUS_M, axis=0)
    return _r6(float(fraction.max()))


def nearest_point_index(x_m: float, y_m: float, points_xy: ArrayLike) -> int | None:
    """Index of the nearest point (ties go to the lowest index); None if no points."""
    pts = _as_points(points_xy)
    if pts.shape[0] == 0:
        return None
    d = np.hypot(pts[:, 0] - x_m, pts[:, 1] - y_m)
    return int(np.argmin(d))


def last_fitted_state(track: TrackArrays) -> tuple[float, float, float, float] | None:
    """(x, y, vx, vy) at the last reading from the latest fitted window in the last
    ``KIN_WINDOW_S``; None below 2 points."""
    window = track.tail(KIN_WINDOW_S)
    if len(window) < 2:
        return None
    fits = fit_windows(window)
    k = len(fits) - 1
    t_last = float(window.t_s[-1])
    vx = float(fits.vx[k])
    vy = float(fits.vy[k])
    dt = t_last - float(fits.t_mid[k])
    return float(fits.x_c[k]) + vx * dt, float(fits.y_c[k]) + vy * dt, vx, vy


def closing_to_asset(
    track: TrackArrays, asset_x_m: float, asset_y_m: float
) -> tuple[float, float] | None:
    """(closing rate m/s, time to asset s) toward one asset point.

    Closing rate is the fitted velocity projected on the line of sight to the asset
    (positive = approaching). Time is range / closing rate, or ``NOT_CLOSING_S`` when the
    contact is not closing. None (unknown) below 2 points in the last ``KIN_WINDOW_S``.
    """
    state = last_fitted_state(track)
    if state is None:
        return None
    x, y, vx, vy = state
    dx, dy = asset_x_m - x, asset_y_m - y
    rng = math.hypot(dx, dy)
    if rng < 1e-9:
        return 0.0, 0.0
    closing = (vx * dx + vy * dy) / rng
    time_s = rng / closing if closing > CLOSING_EPS_MPS else NOT_CLOSING_S
    return _r6(closing), _r6(time_s)


def _rms_with_noise_removed(
    ex: FloatArray, ey: FloatArray, noise_var: float, threshold: float
) -> float:
    """RMS of the window vectors (ex, ey): |mean|^2 plus the spread in excess of noise.

    The spread of the window vectors around their mean only counts beyond ``threshold``
    (the slope-noise variance plus a significance margin), so reading noise is not
    mistaken for real wiggle. With no noise (``noise_var == 0``) this is the plain RMS.
    """
    mx, my = float(np.mean(ex)), float(np.mean(ey))
    spread = float(np.mean((ex - mx) ** 2 + (ey - my) ** 2))
    excess = spread - threshold if noise_var > 0.0 else spread
    return math.sqrt(mx * mx + my * my + max(excess, 0.0))


def drift_consistency(track: TrackArrays, current_at: CurrentAt) -> float | None:
    """How well the track moves with the configured current (0-1; high = drifting).

    The WO-012 score ``clip(1 - |v_obs - v_cur| / (|v_obs| + |v_cur| + eps), 0, 1)`` over
    the 60 s fitted-velocity windows of the last ``DRIFT_WINDOW_S``, with the current read
    at each window's fitted mid-position and mid-time. The window terms are pooled
    before the ratio: ``|v_obs - v_cur|`` and ``|v_obs|`` are root-mean-squares over the
    windows (``_rms_with_noise_removed``) and ``|v_cur|`` is the RMS current. On noise-free
    input with a steady velocity this equals the per-window mean exactly; under 10-20 m
    reading noise it stops the noise alone from marking a drifting object as
    self-propelled, or a still object as drifting (a per-window ratio of noisy
    magnitudes does both at currents of 0.05-0.3 m/s).
    """
    window = track.tail(DRIFT_WINDOW_S)
    if not has_min_points("drift_consistency", window):
        return None
    fits = fit_windows(window)
    t_mid = fits.t_mid
    cur = np.array(
        [
            current_at(float(fits.x_c[k]), float(fits.y_c[k]), float(t_mid[k]))
            for k in range(len(fits))
        ],
        dtype=np.float64,
    ).reshape(-1, 2)
    noise_var = float(np.mean(fits.var_v))
    independent = 2.0 * max(1.0, float(t_mid[-1] - t_mid[0]) / FIT_WINDOW_S + 1.0)
    threshold = noise_var * (1.0 + SPREAD_SIGNIFICANCE * math.sqrt(2.0 / independent))
    diff = _rms_with_noise_removed(fits.vx - cur[:, 0], fits.vy - cur[:, 1], noise_var, threshold)
    obs = _rms_with_noise_removed(fits.vx, fits.vy, noise_var, threshold)
    cur_rms = math.sqrt(float(np.mean(cur[:, 0] ** 2 + cur[:, 1] ** 2)))
    score = 1.0 - diff / (obs + cur_rms + DRIFT_EPS_MPS)
    return _r6(min(max(score, 0.0), 1.0))


# --- transit_pattern -------------------------------------------------------------------


@dataclass(frozen=True)
class TransitStats:
    """The heading statistics the classifier decides on (exposed for tests and the UI)."""

    mean_speed_mps: float
    cumulative_turn_deg: float
    turn_sign_fraction: float
    reversals: int
    persistence: float | None


def _spaced_indices(t_start: FloatArray, idx: Sequence[int], spacing_s: float) -> list[int]:
    out: list[int] = []
    for i in idx:
        if not out or float(t_start[i] - t_start[out[-1]]) >= spacing_s - 1e-9:
            out.append(int(i))
    return out


def _sign_fraction(fits: WindowFits, mask: BoolArray, cumulative: float) -> float:
    idx = _spaced_indices(fits.t_start, list(np.flatnonzero(mask)), TURN_STEP_S)
    if len(idx) < 2 or cumulative == 0.0:
        return 0.0
    steps = wrap_deg(np.diff(np.degrees(fits.heading_rad[idx])))
    held = np.count_nonzero(np.sign(steps) == math.copysign(1.0, cumulative))
    return float(held) / float(steps.shape[0])


def _circular_mean_deg(h_deg: FloatArray) -> float:
    r = np.radians(h_deg)
    return float(np.degrees(np.arctan2(np.mean(np.sin(r)), np.mean(np.cos(r)))))


def _count_reversals(fits: WindowFits, mask: BoolArray) -> int:
    """Reversals of 150-210 deg between consecutive straight legs."""
    idx = np.flatnonzero(mask)
    if idx.shape[0] == 0:
        return 0
    h_deg = np.degrees(fits.heading_rad)
    t_mid = fits.t_mid
    legs: list[list[int]] = [[int(idx[0])]]
    for i in idx[1:]:
        ref = _circular_mean_deg(h_deg[legs[-1]])
        if abs(float(wrap_deg(np.array([h_deg[i] - ref]))[0])) <= LEG_TOL_DEG:
            legs[-1].append(int(i))
        else:
            legs.append([int(i)])
    straight: list[tuple[float, float, float]] = []  # (t_first, t_last, mean heading)
    for leg in legs:
        t0, t1 = float(t_mid[leg[0]]), float(t_mid[leg[-1]])
        if t1 - t0 < MIN_LEG_S:
            continue
        leg_mask = np.zeros(len(fits), dtype=np.bool_)
        leg_mask[leg] = True
        persistence = _persistence(fits, leg_mask)
        if persistence is None or persistence < STRAIGHT_PERSISTENCE:
            continue
        straight.append((t0, t1, _circular_mean_deg(h_deg[leg])))
    reversals = 0
    for (_, prev_end, prev_h), (next_start, _, next_h) in zip(straight, straight[1:], strict=False):
        turn = abs(float(wrap_deg(np.array([next_h - prev_h]))[0]))
        if turn >= REVERSAL_MIN_DEG and next_start - prev_end <= MAX_TURN_GAP_S:
            reversals += 1
    return reversals


def transit_stats(track: TrackArrays) -> TransitStats:
    """Heading statistics over the last ``PATTERN_WINDOW_S`` (no minimum check)."""
    fits = fit_windows(track.tail(PATTERN_WINDOW_S))
    mask = fits.resolved
    cumulative, _ = _cumulative_turn_deg(fits, mask)
    return TransitStats(
        mean_speed_mps=_debiased_mean_speed(fits),
        cumulative_turn_deg=cumulative,
        turn_sign_fraction=_sign_fraction(fits, mask, cumulative),
        reversals=_count_reversals(fits, mask),
        persistence=_persistence(fits, mask),
    )


def classify_transit_pattern(track: TrackArrays) -> TransitPatternName | None:
    """straight / lawnmower / orbit / random / stationary over the last 600 s.

    Rules, in order: mean speed < 0.1 m/s is ``stationary``; |cumulative signed turn|
    >= 360 deg with one turn sign held for >= 80% of steps is ``orbit``; at least 2
    reversals of 150-210 deg between legs with heading persistence >= 0.9 is
    ``lawnmower``; heading persistence >= 0.9 is ``straight``; anything else is
    ``random``. None (unknown) below 10 points or a 60 s span.
    """
    window = track.tail(PATTERN_WINDOW_S)
    if not has_min_points("transit_pattern", window):
        return None
    stats = transit_stats(window)
    if stats.mean_speed_mps < STATIONARY_SPEED_MPS:
        return "stationary"
    if (
        abs(stats.cumulative_turn_deg) >= ORBIT_MIN_TURN_DEG
        and stats.turn_sign_fraction >= ORBIT_SIGN_FRACTION
    ):
        return "orbit"
    if stats.reversals >= LAWNMOWER_MIN_REVERSALS:
        return "lawnmower"
    if stats.persistence is not None and stats.persistence >= STRAIGHT_PERSISTENCE:
        return "straight"
    return "random"


__all__ = [
    "BUCKETS",
    "BucketSpec",
    "CurrentAt",
    "FEATURE_CATALOG",
    "FIT_WINDOW_S",
    "FeatureSpec",
    "MIN_POINTS",
    "NOT_CLOSING_S",
    "TRANSIT_PATTERNS",
    "TrackArrays",
    "TransitPatternName",
    "TransitStats",
    "bucketize",
    "classify_transit_pattern",
    "closing_to_asset",
    "depth",
    "depth_variance",
    "drift_consistency",
    "dwell_time_s",
    "has_min_points",
    "heading_persistence",
    "load_yaml_bucket_specs",
    "loiter_score",
    "nearest_point_index",
    "speed_over_ground",
    "speed_variance",
    "track_confidence",
    "track_gap_ratio",
    "transit_stats",
    "turn_rate",
]
