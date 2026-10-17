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
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, Literal, Protocol

import numpy as np
from numpy.typing import ArrayLike

from soundings.investigation.evidence import EvidenceTable
from soundings.investigation.kalman import (
    KalmanConfig,
    VelocitySource,
    kalman_state_xyv,
    load_tracking_config,
)
from soundings.investigation.velocity import (
    FIT_WINDOW_S,
    BoolArray,
    FloatArray,
    TrackArrays,
    WindowFits,
    fit_windows,
    wrap_deg,
)
from soundings.schemas.config import LikelihoodSpec, load_likelihood_spec
from soundings.schemas.enums import EvidenceKind, FeatureName, TonalBand, TransitPattern, Unit
from soundings.schemas.evidence import UNKNOWN, EvidenceRow, EvidenceValue
from soundings.schemas.geo import from_local_m, to_local_m
from soundings.schemas.harbor import Harbor, Zone
from soundings.schemas.track import Track

DECIMALS: Final = 6

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

TRANSIT_PATTERNS: Final[tuple[str, ...]] = tuple(p.value for p in TransitPattern)
"""The WO-010 ``TransitPattern`` values in vocabulary order (the bucket order)."""

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


BUCKETS: Final[dict[FeatureName, BucketSpec]] = {
    FeatureName.DRIFT_CONSISTENCY: BucketSpec(("low", "mid", "high"), (0.3, 0.7)),
    FeatureName.PROPELLER_TONAL_PRESENT: BucketSpec(("false", "true")),
    FeatureName.BREATHING_OR_CLICK_PRESENT: BucketSpec(("false", "true")),
    FeatureName.TRANSIT_PATTERN: BucketSpec(TRANSIT_PATTERNS),
    FeatureName.DEPTH: BucketSpec(("surface", "shallow", "mid", "deep"), (1.0, 4.0, 10.0)),
    FeatureName.SPEED_OVER_GROUND: BucketSpec(("drift", "slow", "cruise", "fast"), (0.3, 0.8, 2.0)),
    FeatureName.AIS_SURFACE_CORRELATION: BucketSpec(("none", "weak", "strong"), (0.3, 0.7)),
    FeatureName.REGISTRY_MATCH: BucketSpec(("exact", "area_mismatch", "window_mismatch", "none")),
    FeatureName.HAIL_REPLY: BucketSpec(("valid", "invalid", "none", "timeout")),
    FeatureName.PATTERN_OF_LIFE_ANOMALY: BucketSpec(
        ("normal", "unusual", "anomalous"), (0.5, 0.85)
    ),
    FeatureName.LOITER_SCORE: BucketSpec(("low", "mid", "high"), (0.3, 0.7)),
}

BOOL_BUCKETS: Final = ("false", "true")


def bucketize(feature: str, value: EvidenceValue) -> str | None:
    """Map a feature value to its bucket name; ``"unknown"`` maps to None (skipped).

    Raises KeyError for a feature with no buckets and ValueError for a value that does
    not fit the feature's bucket kind.
    """
    try:
        spec = BUCKETS[FeatureName(feature)]
    except ValueError as exc:  # not a feature name at all
        raise KeyError(feature) from exc
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


def likelihood_bucket_specs(spec: LikelihoodSpec | None = None) -> dict[FeatureName, BucketSpec]:
    """Bucket names and thresholds as the WO-010 loader reads ``config/likelihoods.yaml``.

    Used to check ``BUCKETS`` against the scorer's tables; the scorer itself reads the
    YAML through ``load_likelihood_spec``.
    """
    loaded = load_likelihood_spec() if spec is None else spec
    return {
        name: BucketSpec(tuple(body.buckets), body.thresholds)
        for name, body in loaded.features.items()
    }


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
    unit: Unit
    extractor: str
    min_points: int = 1
    min_span_s: float = 0.0
    unknown_source: str = INSUFFICIENT_POINTS
    unknown_rule: str = ""
    other_unknown_sources: tuple[str, ...] = ()


_FEWER_THAN_3 = "fewer than 3 points"

FEATURE_CATALOG: Final[dict[FeatureName, FeatureSpec]] = {
    # kinematic (SPEC-INVESTIGATION section 2, in section 2 order)
    FeatureName.SPEED_OVER_GROUND: FeatureSpec(
        "kinematic",
        Unit.M_PER_S,
        "speed_over_ground",
        2,
        MIN_SPEED_SPAN_S,
        INSUFFICIENT_POINTS,
        "fewer than 2 points, or a span under 4 s",
    ),
    FeatureName.SPEED_VARIANCE: FeatureSpec(
        "kinematic", Unit.M_PER_S2, "speed_variance", 3, 0.0, INSUFFICIENT_POINTS, _FEWER_THAN_3
    ),
    FeatureName.DEPTH: FeatureSpec(
        "kinematic", Unit.M, "depth", 1, 0.0, INSUFFICIENT_POINTS, "no points"
    ),
    FeatureName.DEPTH_VARIANCE: FeatureSpec(
        "kinematic", Unit.M, "depth_variance", 3, 0.0, INSUFFICIENT_POINTS, _FEWER_THAN_3
    ),
    FeatureName.HEADING_PERSISTENCE: FeatureSpec(
        "kinematic",
        Unit.RATIO,
        "heading_persistence",
        3,
        0.0,
        INSUFFICIENT_POINTS,
        "fewer than 3 fitted steps whose displacement is at least 2 sigma position noise",
    ),
    FeatureName.TURN_RATE: FeatureSpec(
        "kinematic", Unit.DEG_PER_S, "turn_rate", 3, 0.0, INSUFFICIENT_POINTS, _FEWER_THAN_3
    ),
    FeatureName.CLOSING_RATE_TO_NEAREST_CRITICAL_ASSET: FeatureSpec(
        "kinematic",
        Unit.M_PER_S,
        "closing_to_asset",
        2,
        0.0,
        INSUFFICIENT_POINTS,
        "fewer than 2 points; no_critical_asset when no asset has criticality >= 0.7",
        (NO_CRITICAL_ASSET,),
    ),
    FeatureName.TIME_TO_NEAREST_CRITICAL_ASSET: FeatureSpec(
        "kinematic",
        Unit.S,
        "closing_to_asset",
        2,
        0.0,
        INSUFFICIENT_POINTS,
        "fewer than 2 points; no_critical_asset when no asset has criticality >= 0.7",
        (NO_CRITICAL_ASSET,),
    ),
    FeatureName.DWELL_TIME_IN_ZONE: FeatureSpec(
        "kinematic",
        Unit.S,
        "dwell_time_s",
        1,
        0.0,
        NO_SENSOR_COVERAGE,
        "the last point lies outside every zone",
    ),
    FeatureName.TRACK_GAP_RATIO: FeatureSpec(
        "kinematic", Unit.RATIO, "track_gap_ratio", 1, 0.0, INSUFFICIENT_POINTS, "no points"
    ),
    FeatureName.DRIFT_CONSISTENCY: FeatureSpec(
        "kinematic", Unit.RATIO, "drift_consistency", 3, 0.0, INSUFFICIENT_POINTS, _FEWER_THAN_3
    ),
    FeatureName.LOITER_SCORE: FeatureSpec(
        "kinematic",
        Unit.RATIO,
        "loiter_score",
        2,
        LOITER_WINDOW_S,
        INSUFFICIENT_POINTS,
        "a span under 300 s",
    ),
    FeatureName.TRANSIT_PATTERN: FeatureSpec(
        "kinematic",
        Unit.CATEGORY,
        "classify_transit_pattern",
        10,
        MIN_PATTERN_SPAN_S,
        INSUFFICIENT_POINTS,
        "fewer than 10 points, or a span under 60 s",
    ),
    # acoustic (passthrough of TrackPoint.acoustic)
    FeatureName.PROPELLER_TONAL_PRESENT: FeatureSpec(
        "acoustic",
        Unit.BOOL,
        "extract_acoustic",
        1,
        0.0,
        NOT_REPORTED_BY_DETECTOR,
        "the detector never reports the field",
    ),
    FeatureName.TONAL_BAND: FeatureSpec(
        "acoustic",
        Unit.CATEGORY,
        "extract_acoustic",
        1,
        0.0,
        NOT_REPORTED_BY_DETECTOR,
        "the detector never reports the field",
    ),
    FeatureName.CAVITATION_INDEX: FeatureSpec(
        "acoustic",
        Unit.RATIO,
        "extract_acoustic",
        1,
        0.0,
        NOT_REPORTED_BY_DETECTOR,
        "the detector never reports the field",
    ),
    FeatureName.BREATHING_OR_CLICK_PRESENT: FeatureSpec(
        "acoustic",
        Unit.BOOL,
        "extract_acoustic",
        1,
        0.0,
        NOT_REPORTED_BY_DETECTOR,
        "the detector never reports the field",
    ),
    FeatureName.BROADBAND_LEVEL: FeatureSpec(
        "acoustic",
        Unit.DB,
        "extract_acoustic",
        1,
        0.0,
        NOT_REPORTED_BY_DETECTOR,
        "the detector never reports the field",
    ),
    # registry / identity (Phase 2 placeholders)
    FeatureName.REGISTRY_MATCH: FeatureSpec(
        "registry", Unit.CATEGORY, PLACEHOLDER_PHASE2, 0, 0.0, PLACEHOLDER_PHASE2, "Phase 2"
    ),
    FeatureName.PLAN_ID: FeatureSpec(
        "registry", Unit.ID, PLACEHOLDER_PHASE2, 0, 0.0, PLACEHOLDER_PHASE2, "Phase 2"
    ),
    FeatureName.MODEM_ID: FeatureSpec(
        "registry", Unit.ID, PLACEHOLDER_PHASE2, 0, 0.0, PLACEHOLDER_PHASE2, "Phase 2"
    ),
    FeatureName.HAIL_SENT_AT: FeatureSpec(
        "registry", Unit.S, PLACEHOLDER_PHASE2, 0, 0.0, PLACEHOLDER_PHASE2, "Phase 2"
    ),
    FeatureName.HAIL_REPLY: FeatureSpec(
        "registry", Unit.CATEGORY, PLACEHOLDER_PHASE2, 0, 0.0, PLACEHOLDER_PHASE2, "Phase 2"
    ),
    FeatureName.OPERATOR_KNOWN: FeatureSpec(
        "registry", Unit.BOOL, PLACEHOLDER_PHASE2, 0, 0.0, PLACEHOLDER_PHASE2, "Phase 2"
    ),
    # context
    FeatureName.ZONE_ID: FeatureSpec(
        "context",
        Unit.ID,
        "extract_context",
        1,
        0.0,
        NO_SENSOR_COVERAGE,
        "the last point lies outside every zone",
    ),
    FeatureName.ZONE_CRITICALITY: FeatureSpec(
        "context",
        Unit.RATIO,
        "extract_context",
        1,
        0.0,
        NO_SENSOR_COVERAGE,
        "the last point lies outside every zone",
    ),
    FeatureName.NEAREST_ASSET_ID: FeatureSpec(
        "context",
        Unit.ID,
        "extract_context",
        1,
        0.0,
        NO_CRITICAL_ASSET,
        "the harbor map holds no asset at all",
    ),
    FeatureName.HOUR_OF_DAY: FeatureSpec(
        "context", Unit.HOUR, "extract_context", 0, 0.0, INSUFFICIENT_POINTS, "never"
    ),
    FeatureName.PATTERN_OF_LIFE_ANOMALY: FeatureSpec(
        "context",
        Unit.RATIO,
        "anomaly_score",
        1,
        0.0,
        POL_CELL_EMPTY,
        "the zone/hour/weekday cell is empty; no_pol_table when no table is loaded",
        (NO_POL_TABLE,),
    ),
    FeatureName.AIS_SURFACE_CORRELATION: FeatureSpec(
        "context",
        Unit.RATIO,
        "extract_context",
        0,
        0.0,
        NO_AIS_FEED,
        "always in Phase 1: no surface feed is simulated",
    ),
    FeatureName.SENSOR_HEALTH: FeatureSpec(
        "context",
        Unit.RATIO,
        "extract_context",
        1,
        0.0,
        NO_SENSOR_COVERAGE,
        "no sensor node covers the contact",
    ),
    FeatureName.VISIBILITY_FOR_CAMERA: FeatureSpec(
        "context",
        Unit.RATIO,
        "extract_context",
        0,
        0.0,
        INSUFFICIENT_POINTS,
        "never (assumption row from the hour-of-day default)",
    ),
    # support (not scored)
    FeatureName.TRACK_SEGMENT: FeatureSpec(
        "support", Unit.CATEGORY, "track_segment", 1, 0.0, INSUFFICIENT_POINTS, "no points"
    ),
}

MIN_POINTS: Final[dict[FeatureName, tuple[int, float]]] = {
    name: (spec.min_points, spec.min_span_s) for name, spec in FEATURE_CATALOG.items()
}
"""feature -> (minimum points, minimum span in seconds) below which it is unknown."""


def has_min_points(feature: str, track: TrackArrays) -> bool:
    """True when ``track`` meets the feature's point and span minimum."""
    points, span = MIN_POINTS[FeatureName(feature)]
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
    track: TrackArrays,
    asset_x_m: float,
    asset_y_m: float,
    *,
    velocity_source: VelocitySource = "window",
    kalman_config: KalmanConfig | None = None,
) -> tuple[float, float] | None:
    """(closing rate m/s, time to asset s) toward one asset point.

    Closing rate is the fitted velocity projected on the line of sight to the asset
    (positive = approaching). Time is range / closing rate, or ``NOT_CLOSING_S`` when the
    contact is not closing. None (unknown) below 2 points in the last ``KIN_WINDOW_S``.

    ``velocity_source="window"`` (the default) uses ``last_fitted_state``; ``"kalman"``
    uses the WO-084 filter state at the last reading (``kalman_state_xyv`` over the whole
    track), with the same None rule.
    """
    if velocity_source == "kalman":
        if len(track.tail(KIN_WINDOW_S)) < 2:
            return None
        state = kalman_state_xyv(track, kalman_config)
    elif velocity_source == "window":
        state = last_fitted_state(track)
    else:
        raise ValueError(f"unknown velocity_source {velocity_source!r}")
    if state is None:
        return None
    return closing_from_state(state, asset_x_m, asset_y_m)


def closing_from_state(
    state: tuple[float, float, float, float], asset_x_m: float, asset_y_m: float
) -> tuple[float, float]:
    """(closing rate, time to asset) of an (x, y, vx, vy) state toward one asset point."""
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


def classify_transit_pattern(track: TrackArrays) -> TransitPattern | None:
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
        return TransitPattern.STATIONARY
    if (
        abs(stats.cumulative_turn_deg) >= ORBIT_MIN_TURN_DEG
        and stats.turn_sign_fraction >= ORBIT_SIGN_FRACTION
    ):
        return TransitPattern.ORBIT
    if stats.reversals >= LAWNMOWER_MIN_REVERSALS:
        return TransitPattern.LAWNMOWER
    if stats.persistence is not None and stats.persistence >= STRAIGHT_PERSISTENCE:
        return TransitPattern.STRAIGHT
    return TransitPattern.RANDOM


# --- evidence rows from observed tracks ------------------------------------------------
#
# Everything below reads a WO-010 ``Track`` (observed readings only), the harbor map, the
# scenario clock and the configured current field. Nothing here can reach the
# simulator's hidden state: ``build_evidence`` takes ``run.tracks`` and ``run.tick_s``.

KINEMATIC_FEATURES: Final[tuple[FeatureName, ...]] = tuple(
    name for name, spec in FEATURE_CATALOG.items() if spec.group == "kinematic"
)
ACOUSTIC_FEATURES: Final[tuple[FeatureName, ...]] = tuple(
    name for name, spec in FEATURE_CATALOG.items() if spec.group == "acoustic"
)
REGISTRY_FEATURES: Final[tuple[FeatureName, ...]] = tuple(
    name for name, spec in FEATURE_CATALOG.items() if spec.group == "registry"
)

# Sources of rows that are not unknown.
SOURCE_TRACKER: Final = "tracker"
SOURCE_HARBOR_MAP: Final = "harbor_map"
SOURCE_SCENARIO_CLOCK: Final = "scenario_clock"
SOURCE_CURRENT_FIELD: Final = "config:current_field"
SOURCE_POL_TABLE: Final = "data:pol_table"
SOURCE_DEFAULT_WEEKDAY: Final = "default:weekday"
SOURCE_DEFAULT_VISIBILITY: Final = "default:hour_of_day"
ACOUSTIC_SOURCE_PREFIX: Final = "acoustic_detector:"
SENSOR_SOURCE_PREFIX: Final = "sensor:"
EXTRACTOR_SOURCE_PREFIX: Final = "extractor:"
KALMAN_CLOSING_EXTRACTOR: Final = "closing_to_asset_kalman"
"""Extractor named by closing rows built from the WO-084 filter (velocity_source=kalman)."""
CLOSING_FEATURES: Final = frozenset(
    {
        FeatureName.CLOSING_RATE_TO_NEAREST_CRITICAL_ASSET,
        FeatureName.TIME_TO_NEAREST_CRITICAL_ASSET,
    }
)

# Assumption rows (WO-012 "Kinds"): the configured value, exactly. Their uncertainty is
# stated in ``assumes``; ``confidence`` (SPEC-INV 9.10) is about reading quality, so an
# assumption row carries 1.0 and does not drag down the rows derived from it.
ASSUMPTION_CONFIDENCE: Final = 1.0
CLOCK_CONFIDENCE: Final = 1.0  # hour_of_day comes from the scenario clock
SENSOR_HEALTH_CONFIDENCE: Final = 1.0  # the node's configured health, read from the map
UNKNOWN_CONFIDENCE: Final = 0.0

WEEKDAYS: Final[tuple[str, ...]] = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
DEFAULT_WEEKDAY: Final = "sat"  # the demo day (Sat 17 Oct 2026); scenarios carry none
DAYLIGHT_START_HOUR: Final = 6  # visibility default: 06:00 <= hour < 19:00 is daylight
DAYLIGHT_END_HOUR: Final = 19
VISIBILITY_DAYLIGHT: Final = 0.8
VISIBILITY_DARK: Final = 0.2

CURRENT_FIELD_ASSUMES: Final = "current field is the configured default; no live gauge"
VISIBILITY_ASSUMES: Final = "visibility from hour-of-day default; no visibility sensor"
WEEKDAY_ASSUMES: Final = f"weekday is the default ({DEFAULT_WEEKDAY}); scenarios carry no weekday"

SegmentName = Literal["kinematic", "pattern", "full"]
SEGMENT_WINDOWS_S: Final[dict[SegmentName, float | None]] = {
    "full": None,  # dwell_time_in_zone, track_gap_ratio
    "pattern": PATTERN_WINDOW_S,  # drift_consistency, loiter_score, transit_pattern
    "kinematic": KIN_WINDOW_S,  # speed, depth, heading, turn, closing
}
FEATURE_SEGMENT: Final[dict[FeatureName, SegmentName]] = {
    FeatureName.SPEED_OVER_GROUND: "kinematic",
    FeatureName.SPEED_VARIANCE: "kinematic",
    FeatureName.DEPTH: "kinematic",
    FeatureName.DEPTH_VARIANCE: "kinematic",
    FeatureName.HEADING_PERSISTENCE: "kinematic",
    FeatureName.TURN_RATE: "kinematic",
    FeatureName.CLOSING_RATE_TO_NEAREST_CRITICAL_ASSET: "kinematic",
    FeatureName.TIME_TO_NEAREST_CRITICAL_ASSET: "kinematic",
    FeatureName.DWELL_TIME_IN_ZONE: "full",
    FeatureName.TRACK_GAP_RATIO: "full",
    FeatureName.DRIFT_CONSISTENCY: "pattern",
    FeatureName.LOITER_SCORE: "pattern",
    FeatureName.TRANSIT_PATTERN: "pattern",
}
"""Which ``track_segment`` row each kinematic row derives from."""

POL_INPUT_FEATURES: Final[tuple[FeatureName, ...]] = (
    FeatureName.SPEED_OVER_GROUND,
    FeatureName.DEPTH,
    FeatureName.DWELL_TIME_IN_ZONE,
    FeatureName.TRANSIT_PATTERN,
)
"""The SPEC-INV section 4 feature vector scored against a pattern-of-life cell."""


class PolBaseline(Protocol):
    """What ``build_evidence`` needs from a pattern-of-life table (WO-012 ``pol.py``)."""

    @property
    def seed(self) -> int: ...

    def anomaly(
        self, zone_id: str, hour: int, weekday: str, observed: Mapping[str, EvidenceValue]
    ) -> float | None:
        """Anomaly in [0, 1] of ``observed`` in the cell, or None when the cell is empty."""
        ...


class ObservedRun(Protocol):
    """The two fields of a WO-011 ``SimRun`` the evidence table may read."""

    @property
    def tracks(self) -> Mapping[str, Track]: ...

    @property
    def tick_s(self) -> float: ...


class _VectorField(Protocol):
    def vector_at(self, lat: float, lon: float, tide_phase: Any) -> tuple[float, float]: ...


def pol_assumes(seed: int) -> str:
    """The ``assumes`` text of a ``pol_baseline`` row."""
    return f"pattern of life is a simulated normal month (seed {seed}); not observed history"


def hour_at(hour_of_day: float, t_s: float) -> int:
    """Whole clock hour (0-23) at ``t_s`` seconds after a scenario starting at ``hour_of_day``."""
    return int(math.floor(hour_of_day + t_s / 3600.0)) % 24


def visibility_default(hour: int) -> float:
    """The hour-of-day visibility default (an assumption, never a reading)."""
    return (
        VISIBILITY_DAYLIGHT if DAYLIGHT_START_HOUR <= hour < DAYLIGHT_END_HOUR else VISIBILITY_DARK
    )


def current_at_from_field(
    vector_at: Callable[[float, float, Any], tuple[float, float]],
    tide_phase_for_hour: Callable[[int], Any],
    *,
    hour_of_day: float,
) -> CurrentAt:
    """Wrap a lat/lon/tide-phase current lookup as a local-metre ``CurrentAt``.

    The tide phase is read at the clock hour of each queried time (``hour_at``).
    """

    def current_at(x_m: float, y_m: float, t_s: float) -> tuple[float, float]:
        lat, lon = from_local_m(x_m, y_m)
        u, v = vector_at(lat, lon, tide_phase_for_hour(hour_at(hour_of_day, t_s)))
        return float(u), float(v)

    return current_at


def track_arrays(track: Track) -> TrackArrays:
    """A WO-010 ``Track`` as feature-ready columns: seconds, local metres, reported sigma."""
    xy = [to_local_m(p.lat, p.lon) for p in track.points]
    return TrackArrays.from_columns(
        [p.tick * track.tick_s for p in track.points],
        [x for x, _ in xy],
        [y for _, y in xy],
        [p.depth_m for p in track.points],
        [p.confidence for p in track.points],
        [p.sigma_m for p in track.points],
    )


def _row_value(value: EvidenceValue | TransitPattern) -> EvidenceValue:
    if isinstance(value, TransitPattern):
        return value.value
    if isinstance(value, float):
        return _r6(value)
    return value


def _fact(
    track: Track,
    feature: FeatureName,
    value: EvidenceValue,
    *,
    source: str,
    confidence: float,
    t: int | None = None,
    unit: Unit | None = None,
) -> EvidenceRow:
    return EvidenceRow(
        contact_id=track.contact_id,
        t=track.points[-1].tick if t is None else t,
        feature=feature,
        value=_row_value(value),
        unit=FEATURE_CATALOG[feature].unit if unit is None else unit,
        source=source,
        confidence=_r6(confidence),
        kind=EvidenceKind.FACT,
        probe_id=None,
    )


def _unknown_fact(track: Track, feature: FeatureName, reason: str) -> EvidenceRow:
    return _fact(track, feature, UNKNOWN, source=reason, confidence=UNKNOWN_CONFIDENCE)


def _assumption(
    track: Track,
    feature: FeatureName,
    value: EvidenceValue,
    *,
    unit: Unit,
    source: str,
    assumes: str,
) -> EvidenceRow:
    return EvidenceRow(
        contact_id=track.contact_id,
        t=track.points[-1].tick,
        feature=feature,
        value=_row_value(value),
        unit=unit,
        source=source,
        confidence=ASSUMPTION_CONFIDENCE,
        kind=EvidenceKind.ASSUMPTION,
        probe_id=None,
        assumes=assumes,
    )


def _row_confidence(row: EvidenceRow) -> float:
    return row.confidence if isinstance(row.confidence, float) else 0.0


def _derived(
    track: Track,
    feature: FeatureName,
    value: EvidenceValue | TransitPattern | None,
    sources: Sequence[EvidenceRow],
    *,
    unknown_reason: str,
    extractor: str | None = None,
) -> EvidenceRow:
    """A derived row; ``value=None`` writes ``unknown`` with ``unknown_reason`` as source.

    A known value's confidence is the minimum of its sources' (the table's ceiling).
    ``extractor`` overrides the catalog extractor named in the source (WO-084 kalman rows).
    """
    spec = FEATURE_CATALOG[feature]
    extractor = spec.extractor if extractor is None else extractor
    ids = tuple(dict.fromkeys(s.row_id for s in sources))
    known = value is not None
    return EvidenceRow(
        contact_id=track.contact_id,
        t=track.points[-1].tick,
        feature=feature,
        value=_row_value(value) if value is not None else UNKNOWN,
        unit=spec.unit,
        source=f"{EXTRACTOR_SOURCE_PREFIX}{extractor}" if known else unknown_reason,
        confidence=min(_row_confidence(s) for s in sources) if known else UNKNOWN_CONFIDENCE,
        kind=EvidenceKind.DERIVED,
        probe_id=None,
        derived_from=ids,
    )


def track_segments(track: Track) -> dict[SegmentName, EvidenceRow]:
    """The ``track_segment`` fact rows the kinematic rows derive from.

    One row per window in ``SEGMENT_WINDOWS_S``; its value names the ticks and reading
    count it covers and its confidence is the minimum reading confidence in it (WO-012
    "Confidence"). Windows holding the same readings share one identical row.
    """
    arrays = track_arrays(track)
    out: dict[SegmentName, EvidenceRow] = {}
    for name, window_s in SEGMENT_WINDOWS_S.items():
        n = len(arrays) if window_s is None else len(arrays.tail(window_s))
        points = track.points[len(track.points) - n :]
        out[name] = _fact(
            track,
            FeatureName.TRACK_SEGMENT,
            f"ticks={points[0].tick}..{points[-1].tick};readings={n}",
            source=SOURCE_TRACKER,
            confidence=min(p.confidence for p in points),
        )
    return out


def current_field_row(track: Track, current_at: CurrentAt) -> EvidenceRow:
    """The ``current_field`` assumption: configured current speed at the last reading."""
    last = track.points[-1]
    x, y = to_local_m(last.lat, last.lon)
    u, v = current_at(x, y, last.tick * track.tick_s)
    return _assumption(
        track,
        FeatureName.CURRENT_FIELD,
        math.hypot(u, v),
        unit=Unit.M_PER_S,
        source=SOURCE_CURRENT_FIELD,
        assumes=CURRENT_FIELD_ASSUMES,
    )


def weekday_row(track: Track) -> EvidenceRow:
    """The ``weekday`` assumption (``DEFAULT_WEEKDAY``)."""
    return _assumption(
        track,
        FeatureName.WEEKDAY,
        DEFAULT_WEEKDAY,
        unit=Unit.CATEGORY,
        source=SOURCE_DEFAULT_WEEKDAY,
        assumes=WEEKDAY_ASSUMES,
    )


def pol_baseline_row(track: Track, pol: PolBaseline) -> EvidenceRow:
    """The ``pol_baseline`` assumption naming the simulated month's seed."""
    return _assumption(
        track,
        FeatureName.POL_BASELINE,
        f"normal_month:seed={pol.seed}",
        unit=Unit.ID,
        source=SOURCE_POL_TABLE,
        assumes=pol_assumes(pol.seed),
    )


def extract_kinematics(
    track: Track,
    harbor: Harbor,
    current_at: CurrentAt,
    *,
    segments: Mapping[SegmentName, EvidenceRow],
    current_row: EvidenceRow,
    velocity_source: VelocitySource = "window",
) -> list[EvidenceRow]:
    """One derived row per kinematic catalog feature, in catalog order.

    Each derives from the ``track_segment`` row of its window (``FEATURE_SEGMENT``);
    ``drift_consistency`` also derives from ``current_row``. With
    ``velocity_source="kalman"`` the two closing rows come from the WO-084 filter, name
    ``extractor:closing_to_asset_kalman`` and derive from the ``full`` segment (the
    filter reads the whole track); the default ``"window"`` rows are unchanged.
    """
    arrays = track_arrays(track)
    last = track.points[-1]
    values: dict[FeatureName, EvidenceValue | TransitPattern | None] = {
        FeatureName.SPEED_OVER_GROUND: speed_over_ground(arrays),
        FeatureName.SPEED_VARIANCE: speed_variance(arrays),
        FeatureName.DEPTH: depth(arrays),
        FeatureName.DEPTH_VARIANCE: depth_variance(arrays),
        FeatureName.HEADING_PERSISTENCE: heading_persistence(arrays),
        FeatureName.TURN_RATE: turn_rate(arrays),
        FeatureName.TRACK_GAP_RATIO: track.gap_ratio,
        FeatureName.DRIFT_CONSISTENCY: drift_consistency(arrays, current_at),
        FeatureName.TRANSIT_PATTERN: classify_transit_pattern(arrays),
    }
    reasons: dict[FeatureName, str] = {}

    asset = harbor.nearest_asset(last.lat, last.lon, CRITICAL_ASSET_MIN)
    closing = None
    if asset is None:
        reasons[FeatureName.CLOSING_RATE_TO_NEAREST_CRITICAL_ASSET] = NO_CRITICAL_ASSET
        reasons[FeatureName.TIME_TO_NEAREST_CRITICAL_ASSET] = NO_CRITICAL_ASSET
    else:
        closing = closing_to_asset(
            arrays, *to_local_m(asset.lat, asset.lon), velocity_source=velocity_source
        )
    values[FeatureName.CLOSING_RATE_TO_NEAREST_CRITICAL_ASSET] = (
        None if closing is None else closing[0]
    )
    values[FeatureName.TIME_TO_NEAREST_CRITICAL_ASSET] = None if closing is None else closing[1]

    zone = harbor.zone_at(last.lat, last.lon)
    values[FeatureName.DWELL_TIME_IN_ZONE] = (
        None if zone is None else dwell_time_s(arrays.t_s, _inside(track, zone))
    )

    assets = sorted(harbor.assets, key=lambda a: a.asset_id)
    values[FeatureName.LOITER_SCORE] = loiter_score(
        arrays, [to_local_m(a.lat, a.lon) for a in assets]
    )

    rows: list[EvidenceRow] = []
    for feature in KINEMATIC_FEATURES:
        sources = [segments[FEATURE_SEGMENT[feature]]]
        extractor: str | None = None
        if velocity_source == "kalman" and feature in CLOSING_FEATURES:
            sources = [segments["full"]]
            extractor = KALMAN_CLOSING_EXTRACTOR
        if feature is FeatureName.DRIFT_CONSISTENCY:
            sources.append(current_row)
        reason = reasons.get(feature, FEATURE_CATALOG[feature].unknown_source)
        rows.append(
            _derived(
                track,
                feature,
                values[feature],
                sources,
                unknown_reason=reason,
                extractor=extractor,
            )
        )
    return rows


def _inside(track: Track, zone: Zone) -> list[bool]:
    return [zone.contains(p.lat, p.lon) for p in track.points]


def extract_acoustic(track: Track) -> list[EvidenceRow]:
    """Passthrough of every reading's acoustic fields as fact rows, in catalog order.

    Each reported field of each reading becomes a row at the reading's tick with
    ``source="acoustic_detector:<sensor_id>"`` and the reading's confidence. A field no
    reading reports becomes one ``unknown`` row (``not_reported_by_detector``).
    """
    rows: list[EvidenceRow] = []
    for feature in ACOUSTIC_FEATURES:
        reported = False
        for point in track.points:
            raw = getattr(point.acoustic, feature.value)
            if isinstance(raw, str) and raw == UNKNOWN:
                continue
            value: EvidenceValue
            if isinstance(raw, bool):
                value = raw
            elif isinstance(raw, TonalBand):
                value = raw.value
            else:
                value = float(raw)
            rows.append(
                _fact(
                    track,
                    feature,
                    value,
                    source=f"{ACOUSTIC_SOURCE_PREFIX}{point.sensor_id}",
                    confidence=point.confidence,
                    t=point.tick,
                )
            )
            reported = True
        if not reported:
            rows.append(_unknown_fact(track, feature, NOT_REPORTED_BY_DETECTOR))
    return rows


def extract_registry_placeholders(track: Track) -> list[EvidenceRow]:
    """Registry/identity features as ``unknown`` facts (``placeholder:phase2``)."""
    return [_unknown_fact(track, feature, PLACEHOLDER_PHASE2) for feature in REGISTRY_FEATURES]


def covering_nodes(track: Track, harbor: Harbor) -> list[str]:
    """Sensor nodes covering the contact, sorted: nodes that reported it in the last
    ``KIN_WINDOW_S``, plus nodes whose ``range_m`` reaches its last reading."""
    last = track.points[-1]
    t_cut = (last.tick * track.tick_s) - KIN_WINDOW_S
    known = {n.node_id for n in harbor.sensor_nodes}
    ids = {p.sensor_id for p in track.points if p.tick * track.tick_s >= t_cut} & known
    for node in harbor.sensor_nodes:
        x, y = to_local_m(last.lat, last.lon)
        nx, ny = to_local_m(node.lat, node.lon)
        if math.hypot(x - nx, y - ny) <= node.range_m:
            ids.add(node.node_id)
    return sorted(ids)


def extract_context(track: Track, harbor: Harbor, *, hour_of_day: float) -> list[EvidenceRow]:
    """Context rows except ``pattern_of_life_anomaly``, in catalog order.

    ``zone_id``, ``zone_criticality``, ``nearest_asset_id`` and ``hour_of_day`` are facts
    at the last reading; ``ais_surface_correlation`` is ``unknown`` (``no_ais_feed``);
    one ``sensor_health`` fact per covering node; ``visibility_for_camera`` is an
    assumption from the hour-of-day default.
    """
    last = track.points[-1]
    hour = hour_at(hour_of_day, last.tick * track.tick_s)
    rows: list[EvidenceRow] = []
    zone = harbor.zone_at(last.lat, last.lon)
    if zone is None:
        rows.append(_unknown_fact(track, FeatureName.ZONE_ID, NO_SENSOR_COVERAGE))
        rows.append(_unknown_fact(track, FeatureName.ZONE_CRITICALITY, NO_SENSOR_COVERAGE))
    else:
        conf = last.confidence
        rows.append(
            _fact(
                track, FeatureName.ZONE_ID, zone.zone_id, source=SOURCE_HARBOR_MAP, confidence=conf
            )
        )
        rows.append(
            _fact(
                track,
                FeatureName.ZONE_CRITICALITY,
                zone.criticality,
                source=SOURCE_HARBOR_MAP,
                confidence=conf,
            )
        )
    asset = harbor.nearest_asset(last.lat, last.lon)
    if asset is None:
        rows.append(_unknown_fact(track, FeatureName.NEAREST_ASSET_ID, NO_CRITICAL_ASSET))
    else:
        rows.append(
            _fact(
                track,
                FeatureName.NEAREST_ASSET_ID,
                asset.asset_id,
                source=SOURCE_HARBOR_MAP,
                confidence=last.confidence,
            )
        )
    rows.append(
        _fact(
            track,
            FeatureName.HOUR_OF_DAY,
            hour,
            source=SOURCE_SCENARIO_CLOCK,
            confidence=CLOCK_CONFIDENCE,
        )
    )
    rows.append(_unknown_fact(track, FeatureName.AIS_SURFACE_CORRELATION, NO_AIS_FEED))
    nodes = covering_nodes(track, harbor)
    if not nodes:
        rows.append(_unknown_fact(track, FeatureName.SENSOR_HEALTH, NO_SENSOR_COVERAGE))
    for node_id in nodes:
        rows.append(
            _fact(
                track,
                FeatureName.SENSOR_HEALTH,
                harbor.node(node_id).health,
                source=f"{SENSOR_SOURCE_PREFIX}{node_id}",
                confidence=SENSOR_HEALTH_CONFIDENCE,
            )
        )
    rows.append(
        _assumption(
            track,
            FeatureName.VISIBILITY_FOR_CAMERA,
            visibility_default(hour),
            unit=FEATURE_CATALOG[FeatureName.VISIBILITY_FOR_CAMERA].unit,
            source=SOURCE_DEFAULT_VISIBILITY,
            assumes=VISIBILITY_ASSUMES,
        )
    )
    return rows


def extract_pattern_of_life(
    track: Track,
    rows_by_feature: Mapping[FeatureName, Sequence[EvidenceRow]],
    *,
    weekday: EvidenceRow,
    pol: PolBaseline | None,
    pol_row: EvidenceRow | None,
) -> EvidenceRow:
    """``pattern_of_life_anomaly``, derived from zone, hour, weekday, the section 4
    feature vector and (when a table is loaded) ``pol_baseline``.

    ``pol=None`` gives ``unknown`` (``no_pol_table``); an empty cell, or a contact in no
    zone, gives ``unknown`` (``pol_cell_empty``).
    """
    feature = FeatureName.PATTERN_OF_LIFE_ANOMALY
    zone_row = rows_by_feature[FeatureName.ZONE_ID][0]
    hour_row = rows_by_feature[FeatureName.HOUR_OF_DAY][0]
    inputs = [rows_by_feature[f][0] for f in POL_INPUT_FEATURES]
    context = [zone_row, hour_row, weekday]
    if pol is None or pol_row is None:
        return _derived(track, feature, None, [*context, *inputs], unknown_reason=NO_POL_TABLE)
    known = [r for r in inputs if not (isinstance(r.value, str) and r.value == UNKNOWN)]
    if isinstance(zone_row.value, str) and zone_row.value == UNKNOWN:
        score = None
    else:
        observed = {r.feature.value: r.value for r in known}
        hour = hour_row.value
        if not isinstance(hour, int) or isinstance(hour, bool):
            raise TypeError("hour_of_day row must hold an int")
        score = pol.anomaly(str(zone_row.value), hour, str(weekday.value), observed)
    if score is None:
        return _derived(
            track, feature, None, [*context, *inputs, pol_row], unknown_reason=POL_CELL_EMPTY
        )
    return _derived(
        track,
        feature,
        min(max(float(score), 0.0), 1.0),
        [*context, *known, pol_row],
        unknown_reason=POL_CELL_EMPTY,
    )


@dataclass(frozen=True, slots=True)
class PolObservation:
    """What a pattern-of-life cell is keyed by and scored on, for one observed track.

    ``zone_id`` and ``hour`` are the ``zone_id`` and ``hour_of_day`` row values at the
    last reading (``zone_id`` is None outside every zone); ``values`` holds the known
    ``POL_INPUT_FEATURES`` exactly as the evidence rows hold them (6 dp, pattern names).
    ``characterized`` is True when the track meets the ``transit_pattern`` minimum (10
    readings over 60 s), the shortest track on which the whole vector is defined.
    """

    zone_id: str | None
    hour: int
    values: dict[str, EvidenceValue]
    characterized: bool


def pol_observation(track: Track, harbor: Harbor, *, hour_of_day: float) -> PolObservation:
    """The section 4 feature vector of a track without building its whole evidence.

    ``soundings.investigation.pol`` builds the normal month from these, so the baseline
    and ``pattern_of_life_anomaly`` read the same feature definitions.
    """
    arrays = track_arrays(track)
    last = track.points[-1]
    zone = harbor.zone_at(last.lat, last.lon)
    raw: dict[FeatureName, EvidenceValue | TransitPattern | None] = {
        FeatureName.SPEED_OVER_GROUND: speed_over_ground(arrays),
        FeatureName.DEPTH: depth(arrays),
        FeatureName.DWELL_TIME_IN_ZONE: (
            None if zone is None else dwell_time_s(arrays.t_s, _inside(track, zone))
        ),
        FeatureName.TRANSIT_PATTERN: classify_transit_pattern(arrays),
    }
    return PolObservation(
        zone_id=None if zone is None else zone.zone_id,
        hour=hour_at(hour_of_day, last.tick * track.tick_s),
        values={f.value: _row_value(v) for f, v in raw.items() if v is not None},
        characterized=has_min_points(FeatureName.TRANSIT_PATTERN.value, arrays),
    )


def contact_evidence(
    track: Track,
    harbor: Harbor,
    current_at: CurrentAt,
    pol: PolBaseline | None,
    *,
    hour_of_day: float,
    velocity_source: VelocitySource = "window",
) -> list[EvidenceRow]:
    """One extraction pass at the contact's last tick, as rows in append order.

    Order: the assumption rows other rows cite (``current_field``, ``weekday``,
    ``pol_baseline``), then the ``track_segment`` facts, then every other catalog
    feature in catalog order. Every catalog feature gets at least one row.
    """
    current_row = current_field_row(track, current_at)
    week_row = weekday_row(track)
    pol_row = None if pol is None else pol_baseline_row(track, pol)
    segments = track_segments(track)

    by_feature: dict[FeatureName, list[EvidenceRow]] = {}
    produced = (
        extract_kinematics(
            track,
            harbor,
            current_at,
            segments=segments,
            current_row=current_row,
            velocity_source=velocity_source,
        )
        + extract_acoustic(track)
        + extract_registry_placeholders(track)
        + extract_context(track, harbor, hour_of_day=hour_of_day)
    )
    for row in produced:
        by_feature.setdefault(row.feature, []).append(row)
    by_feature[FeatureName.PATTERN_OF_LIFE_ANOMALY] = [
        extract_pattern_of_life(track, by_feature, weekday=week_row, pol=pol, pol_row=pol_row)
    ]

    ordered: list[EvidenceRow] = [current_row, week_row]
    if pol_row is not None:
        ordered.append(pol_row)
    ordered.extend(dict.fromkeys(segments.values()))  # identical windows share one row
    for feature in FEATURE_CATALOG:
        if feature is FeatureName.TRACK_SEGMENT:
            continue
        rows = by_feature.get(feature)
        if not rows:
            raise RuntimeError(f"catalog gap: no row for {feature.value}")  # never silent
        ordered.extend(rows)
    return ordered


def build_evidence(
    run: ObservedRun,
    harbor: Harbor,
    current_field: CurrentAt | _VectorField,
    pol: PolBaseline | None = None,
    *,
    hour_of_day: float,
    tide_phase_for_hour: Callable[[int], Any] | None = None,
    velocity_source: VelocitySource | None = None,
) -> EvidenceTable:
    """The evidence table of a run, from its observed tracks only.

    Reads ``run.tracks`` and ``run.tick_s`` and nothing else. Contacts go in sorted id
    order. ``current_field`` is either a ``CurrentAt`` callable in local metres or an
    object with ``vector_at(lat, lon, tide_phase)``; the latter needs
    ``tide_phase_for_hour``. ``pol=None`` makes ``pattern_of_life_anomaly`` unknown with
    source ``no_pol_table``. ``velocity_source=None`` reads ``closing_velocity_source``
    from ``config/tracking.yaml`` (WO-084; ``window`` keeps the WO-012 rows).
    """
    if velocity_source is None:
        velocity_source = load_tracking_config().closing_velocity_source
    if hasattr(current_field, "vector_at"):
        if tide_phase_for_hour is None:
            raise TypeError("a current field object needs tide_phase_for_hour")
        current_at = current_at_from_field(
            current_field.vector_at, tide_phase_for_hour, hour_of_day=hour_of_day
        )
    elif callable(current_field):
        current_at = current_field
    else:
        raise TypeError("current_field must be a CurrentAt callable or have vector_at")
    table = EvidenceTable()
    for contact_id in sorted(run.tracks):
        track = run.tracks[contact_id]
        if track.contact_id != contact_id:
            raise ValueError(f"run.tracks[{contact_id!r}] holds {track.contact_id!r}")
        if not math.isclose(track.tick_s, run.tick_s, rel_tol=0.0, abs_tol=1e-12):
            raise ValueError(f"{contact_id}: track tick_s differs from run.tick_s")
        table.extend(
            contact_evidence(
                track,
                harbor,
                current_at,
                pol,
                hour_of_day=hour_of_day,
                velocity_source=velocity_source,
            )
        )
    return table


__all__ = [
    "ACOUSTIC_FEATURES",
    "BUCKETS",
    "BucketSpec",
    "CLOSING_FEATURES",
    "CurrentAt",
    "DEFAULT_WEEKDAY",
    "FEATURE_CATALOG",
    "FEATURE_SEGMENT",
    "FIT_WINDOW_S",
    "FeatureSpec",
    "KALMAN_CLOSING_EXTRACTOR",
    "KINEMATIC_FEATURES",
    "MIN_POINTS",
    "NOT_CLOSING_S",
    "ObservedRun",
    "PolBaseline",
    "PolObservation",
    "REGISTRY_FEATURES",
    "TRANSIT_PATTERNS",
    "TrackArrays",
    "TransitStats",
    "UNKNOWN",
    "UNKNOWN_SOURCES",
    "VelocitySource",
    "WEEKDAYS",
    "bucketize",
    "build_evidence",
    "classify_transit_pattern",
    "closing_from_state",
    "closing_to_asset",
    "contact_evidence",
    "covering_nodes",
    "current_at_from_field",
    "current_field_row",
    "depth",
    "depth_variance",
    "drift_consistency",
    "dwell_time_s",
    "extract_acoustic",
    "extract_context",
    "extract_kinematics",
    "extract_pattern_of_life",
    "extract_registry_placeholders",
    "has_min_points",
    "heading_persistence",
    "hour_at",
    "likelihood_bucket_specs",
    "loiter_score",
    "nearest_point_index",
    "pol_assumes",
    "pol_baseline_row",
    "pol_observation",
    "speed_over_ground",
    "speed_variance",
    "track_arrays",
    "track_confidence",
    "track_gap_ratio",
    "track_segments",
    "transit_stats",
    "turn_rate",
    "visibility_default",
    "weekday_row",
]
