"""Unit tests for the schema-independent feature mathematics (WO-012 stage 1).

Track-level tests (``Track`` in, evidence rows out) arrive with WO-010's schemas; these
cover the pure functions over ``TrackArrays`` that those extractors call.
"""

from __future__ import annotations

import math
from collections.abc import Callable

import numpy as np
import pytest
from synthetic_tracks import (
    FUEL_BERTHS_XY,
    OUTSIDE_ZONES_XY,
    TICK_S,
    calm,
    line_track,
    make_track,
    orbit,
    stationary,
    straight,
    to_track,
)

from soundings.investigation import features as F
from soundings.investigation.velocity import TrackArrays, fit_windows, wrap_deg
from soundings.schemas.config import load_likelihood_spec
from soundings.schemas.enums import EvidenceKind, FeatureName, Unit
from soundings.schemas.evidence import UNKNOWN, EvidenceRow
from soundings.schemas.geo import to_local_m
from soundings.schemas.harbor import Harbor, load_harbor
from soundings.schemas.track import Track

# SPEC-INVESTIGATION section 2, in section 2 order (32 features).
SPEC_FEATURES: tuple[str, ...] = (
    "speed_over_ground",
    "speed_variance",
    "depth",
    "depth_variance",
    "heading_persistence",
    "turn_rate",
    "closing_rate_to_nearest_critical_asset",
    "time_to_nearest_critical_asset",
    "dwell_time_in_zone",
    "track_gap_ratio",
    "drift_consistency",
    "loiter_score",
    "transit_pattern",
    "propeller_tonal_present",
    "tonal_band",
    "cavitation_index",
    "breathing_or_click_present",
    "broadband_level",
    "registry_match",
    "plan_id",
    "modem_id",
    "hail_sent_at",
    "hail_reply",
    "operator_known",
    "zone_id",
    "zone_criticality",
    "nearest_asset_id",
    "hour_of_day",
    "pattern_of_life_anomaly",
    "ais_surface_correlation",
    "sensor_health",
    "visibility_for_camera",
)
WO010_UNITS = {"m", "m/s", "m/s2", "deg/s", "s", "ratio", "bool", "category", "id", "hour", "dB"}


def line(n: int, *, tick_s: float = TICK_S, speed: float = 1.0, conf: float = 0.9) -> TrackArrays:
    t = np.arange(n, dtype=np.float64) * tick_s
    return make_track(t, speed * t, np.zeros(n), confidence=conf)


# --- buckets -----------------------------------------------------------------------


def test_buckets_match_yaml() -> None:
    spec = load_likelihood_spec()
    assert set(F.BUCKETS) == set(spec.features)
    for feature, body in spec.features.items():
        assert F.BUCKETS[feature].names == tuple(body.buckets), feature
        assert F.BUCKETS[feature].thresholds == body.thresholds, feature
    assert F.likelihood_bucket_specs(spec) == F.BUCKETS


def test_buckets_match_yaml_booleans_are_strings() -> None:
    yaml_specs = F.likelihood_bucket_specs(load_likelihood_spec())
    for feature in ("propeller_tonal_present", "breathing_or_click_present"):
        assert yaml_specs[feature].names == ("false", "true")
        assert all(isinstance(n, str) for n in yaml_specs[feature].names)


def _boundary_cases() -> list[tuple[str, float, str]]:
    cases: list[tuple[str, float, str]] = []
    for feature, spec in sorted(F.likelihood_bucket_specs(load_likelihood_spec()).items()):
        if spec.thresholds is None:
            continue
        for k, t in enumerate(spec.thresholds):
            cases.append((feature, t - 1e-6, spec.names[k]))
            cases.append((feature, t, spec.names[k + 1]))
            cases.append((feature, t + 1e-6, spec.names[k + 1]))
        cases.append((feature, spec.thresholds[0] - 1e6, spec.names[0]))
        cases.append((feature, spec.thresholds[-1] + 1e6, spec.names[-1]))
    return cases


@pytest.mark.parametrize(("feature", "value", "bucket"), _boundary_cases())
def test_buckets_match_yaml_boundaries(feature: str, value: float, bucket: str) -> None:
    assert F.bucketize(feature, value) == bucket


def test_buckets_match_yaml_left_closed_example() -> None:
    assert F.bucketize("drift_consistency", 0.3) == "mid"
    assert F.bucketize("drift_consistency", 0.7) == "high"
    assert F.bucketize("depth", 0) == "surface"


def test_buckets_match_yaml_every_bucket_reachable() -> None:
    for feature, spec in sorted(F.likelihood_bucket_specs(load_likelihood_spec()).items()):
        if spec.thresholds is None:
            for name in spec.names:
                value: bool | str = (name == "true") if spec.names == ("false", "true") else name
                assert F.bucketize(feature, value) == name
                assert F.bucketize(feature, name) == name
        else:
            edges = [spec.thresholds[0] - 1.0, *spec.thresholds]
            assert [F.bucketize(feature, v) for v in edges] == list(spec.names)


def test_bucketize_unknown_is_none_for_every_feature() -> None:
    for feature in F.BUCKETS:
        assert F.bucketize(feature, F.UNKNOWN) is None


@pytest.mark.parametrize(
    ("feature", "value"),
    [
        ("speed_over_ground", True),
        ("speed_over_ground", "fast"),
        ("speed_over_ground", math.nan),
        ("propeller_tonal_present", 1),
        ("propeller_tonal_present", "yes"),
        ("transit_pattern", "zigzag"),
        ("hail_reply", 0.5),
    ],
)
def test_bucketize_rejects_wrong_kind(feature: str, value: bool | float | str) -> None:
    with pytest.raises(ValueError):
        F.bucketize(feature, value)


def test_bucketize_unbucketed_feature_raises() -> None:
    with pytest.raises(KeyError):
        F.bucketize("turn_rate", 1.0)


def test_bucket_spec_validates() -> None:
    with pytest.raises(ValueError):
        F.BucketSpec(("a", "b"), (1.0, 2.0))
    with pytest.raises(ValueError):
        F.BucketSpec(("a", "b", "c"), (2.0, 1.0))
    with pytest.raises(ValueError):
        F.BucketSpec(("a", "a"))


# --- catalog -----------------------------------------------------------------------


def test_catalog_covers_likelihoods() -> None:
    for feature in load_likelihood_spec().features:
        assert feature in F.FEATURE_CATALOG, feature
        assert F.FEATURE_CATALOG[feature].extractor, feature
    for feature in ("registry_match", "hail_reply"):
        assert F.FEATURE_CATALOG[FeatureName(feature)].extractor == F.PLACEHOLDER_PHASE2


def test_catalog_covers_likelihoods_keys_are_wo010_names() -> None:
    assert all(isinstance(k, FeatureName) for k in F.FEATURE_CATALOG)
    assert all(isinstance(s.unit, Unit) for s in F.FEATURE_CATALOG.values())
    assert all(isinstance(k, FeatureName) for k in F.BUCKETS)
    spec_order = [f for f in FeatureName if f.value in SPEC_FEATURES]
    assert list(F.FEATURE_CATALOG)[: len(spec_order)] == spec_order


def test_catalog_covers_likelihoods_and_every_spec_feature_in_order() -> None:
    assert tuple(F.FEATURE_CATALOG)[: len(SPEC_FEATURES)] == SPEC_FEATURES
    assert set(F.FEATURE_CATALOG) == {*SPEC_FEATURES, "track_segment"}


def test_catalog_extractors_exist_and_reasons_are_declared() -> None:
    extra = {"extract_acoustic", "extract_context", "anomaly_score", "track_segment"}
    for name, spec in F.FEATURE_CATALOG.items():
        assert spec.unit in WO010_UNITS, name
        assert spec.unknown_source in F.UNKNOWN_SOURCES, name
        assert all(s in F.UNKNOWN_SOURCES for s in spec.other_unknown_sources), name
        if spec.extractor != F.PLACEHOLDER_PHASE2 and spec.extractor not in extra:
            assert callable(getattr(F, spec.extractor)), name
        if spec.extractor == F.PLACEHOLDER_PHASE2:
            assert spec.unknown_source == F.PLACEHOLDER_PHASE2
    assert F.MIN_POINTS["speed_over_ground"] == (2, 4.0)
    assert F.MIN_POINTS["transit_pattern"] == (10, 60.0)
    assert F.MIN_POINTS["loiter_score"][1] == F.LOITER_WINDOW_S == 300.0
    for name in ("speed_variance", "depth_variance", "turn_rate", "drift_consistency"):
        assert F.MIN_POINTS[name] == (3, 0.0)


# --- unknown rules over arrays -----------------------------------------------------

ASSET_FAR_AHEAD = (5000.0, 0.0)


def _calm(_x: float, _y: float, _t: float) -> tuple[float, float]:
    return (0.0, 0.0)


ARRAY_FEATURES: dict[str, Callable[[TrackArrays], object]] = {
    "speed_over_ground": F.speed_over_ground,
    "speed_variance": F.speed_variance,
    "depth": F.depth,
    "depth_variance": F.depth_variance,
    "turn_rate": F.turn_rate,
    "drift_consistency": lambda tr: F.drift_consistency(tr, _calm),
    "closing_rate_to_nearest_critical_asset": lambda tr: F.closing_to_asset(tr, *ASSET_FAR_AHEAD),
    "transit_pattern": F.classify_transit_pattern,
    "loiter_score": lambda tr: F.loiter_score(tr, [ASSET_FAR_AHEAD]),
}

# (points, feature) -> computed? on a noise-free 1 m/s line at 2 s ticks
UNKNOWN_TABLE: dict[int, set[str]] = {
    1: {"depth"},
    2: {"depth", "closing_rate_to_nearest_critical_asset"},  # span 2 s < 4 s: no speed
    3: {
        "depth",
        "closing_rate_to_nearest_critical_asset",
        "speed_over_ground",
        "speed_variance",
        "depth_variance",
        "turn_rate",
        "drift_consistency",
    },
}
UNKNOWN_TABLE[9] = set(UNKNOWN_TABLE[3])
UNKNOWN_TABLE[10] = set(UNKNOWN_TABLE[3])  # 10 points at 2 s span 18 s < 60 s


@pytest.mark.parametrize("n", sorted(UNKNOWN_TABLE))
def test_unknown_rules_arrays_by_point_count(n: int) -> None:
    track = line(n)
    for feature, fn in ARRAY_FEATURES.items():
        computed = fn(track) is not None
        assert computed == (feature in UNKNOWN_TABLE[n]), (n, feature)
        assert computed == F.has_min_points(feature, track), (n, feature)


def test_unknown_rules_arrays_ten_points_spanning_a_minute() -> None:
    t = np.array([0, 2, 4, 6, 8, 10, 12, 14, 16, 60], dtype=np.float64)
    track = make_track(t, t * 1.0, np.zeros_like(t))
    assert F.classify_transit_pattern(track) is not None
    nine = track.select(slice(0, 9))
    assert F.classify_transit_pattern(nine) is None


def test_unknown_rules_arrays_loiter_needs_300_s() -> None:
    t_short = np.arange(0.0, 298.0 + 1e-9, 2.0)
    t_ok = np.arange(0.0, 300.0 + 1e-9, 2.0)
    still_short = make_track(t_short, np.zeros_like(t_short), np.zeros_like(t_short))
    still_ok = make_track(t_ok, np.zeros_like(t_ok), np.zeros_like(t_ok))
    assert F.loiter_score(still_short, [(0.0, 0.0)]) is None
    assert F.loiter_score(still_ok, [(0.0, 0.0)]) == 1.0


def test_unknown_rules_arrays_heading_persistence_needs_three_resolved_steps() -> None:
    assert F.heading_persistence(line(3)) is None  # 2 windows only
    assert F.heading_persistence(line(4)) is None  # 2 full windows of 3 s
    assert F.heading_persistence(line(5)) == 1.0  # 3 windows, all moving
    assert F.heading_persistence(stationary(sigma_m=0.0)) is None  # never moves
    # all-noise: unknown here, stationary as a pattern
    noisy = stationary(sigma_m=10.0, seed=3)
    assert F.heading_persistence(noisy) is None
    assert F.classify_transit_pattern(noisy) == "stationary"


def test_unknown_rules_arrays_receding_contact_is_not_closing() -> None:
    track = straight(speed=1.0, heading_deg=90.0, duration_s=120.0)  # heading east
    behind = (-200.0, 0.0)
    result = F.closing_to_asset(track, *behind)
    assert result is not None
    closing, time_s = result
    assert closing == pytest.approx(-1.0, abs=1e-6)
    assert time_s == F.NOT_CLOSING_S == 1e9


def test_unknown_rules_arrays_dwell_outside_every_zone_is_unknown() -> None:
    t = np.array([0.0, 2.0, 4.0])
    assert F.dwell_time_s(t, [True, True, False]) is None
    assert F.dwell_time_s(np.array([]), []) is None


def test_unknown_rules_arrays_confidence_is_min_point_confidence() -> None:
    conf = np.array([0.9, 0.4, 0.8, 0.95])
    t = np.arange(4, dtype=np.float64) * TICK_S
    track = make_track(t, t, np.zeros(4), confidence=conf)
    assert F.track_confidence(track) == 0.4
    assert F.track_confidence(track.select(slice(0, 0))) == 0.0


# --- velocity fit ------------------------------------------------------------------


@pytest.mark.parametrize("seed", [1, 2, 3])
@pytest.mark.parametrize("report_sigma", [False, True])
def test_velocity_fit_survives_reading_noise(seed: int, report_sigma: bool) -> None:
    clean = straight(speed=1.0, heading_deg=60.0, duration_s=600.0)
    rng_track = make_track(
        clean.t_s, clean.x_m, clean.y_m, sigma_m=15.0, seed=seed, report_sigma=report_sigma
    )
    speed = F.speed_over_ground(rng_track)
    persistence = F.heading_persistence(rng_track)
    assert speed is not None and abs(speed - 1.0) <= 0.1, speed
    assert persistence is not None and persistence >= 0.9, persistence


def test_velocity_fit_noise_free_is_exact() -> None:
    track = straight(speed=1.3, heading_deg=200.0)
    fits = fit_windows(track)
    assert np.allclose(fits.speed, 1.3)
    assert np.allclose(np.degrees(fits.heading_rad) % 360.0, 200.0)
    assert F.speed_over_ground(track) == pytest.approx(1.3, abs=1e-6)
    assert F.heading_persistence(track) == pytest.approx(1.0, abs=1e-6)
    assert F.turn_rate(track) == pytest.approx(0.0, abs=1e-6)
    assert F.speed_variance(track) == pytest.approx(0.0, abs=1e-6)


def test_velocity_fit_windows_are_sixty_seconds_on_long_tracks() -> None:
    fits = fit_windows(straight(duration_s=600.0))
    assert np.allclose(fits.t_end - fits.t_start, F.FIT_WINDOW_S)
    assert int(fits.n_points.min()) == 31


def test_velocity_fit_noise_bias_removed_for_still_contact() -> None:
    speeds = [F.speed_over_ground(stationary(sigma_m=5.0, seed=s)) for s in range(1, 6)]
    assert all(v is not None and v < F.BUCKETS["speed_over_ground"].thresholds[0] for v in speeds)  # type: ignore[index]


def test_turn_rate_orbit_sign_and_size() -> None:
    expected = math.degrees(0.6 / 40.0)
    cw = F.turn_rate(orbit(clockwise=True))
    ccw = F.turn_rate(orbit(clockwise=False))
    assert cw is not None and ccw is not None
    assert cw == pytest.approx(expected, rel=0.02)
    assert ccw == pytest.approx(-expected, rel=0.02)


def test_wrap_deg_range() -> None:
    out = wrap_deg(np.array([-540.0, -180.0, 0.0, 180.0, 190.0, 359.0]))
    assert list(out) == [180.0, 180.0, 0.0, 180.0, -170.0, -1.0]


# --- depth, gaps, dwell, closing ---------------------------------------------------


def test_depth_and_depth_variance_over_last_window() -> None:
    t = np.arange(0.0, 300.0 + 1e-9, 2.0)
    depth = np.where(t >= 300.0 - F.KIN_WINDOW_S, 8.0, 2.0)
    depth[-1] = 10.0
    track = make_track(t, t, np.zeros_like(t), depth_m=depth)
    window = depth[t >= 300.0 - F.KIN_WINDOW_S]
    assert F.depth(track) == round(float(np.mean(window)), 6)
    assert F.depth_variance(track) == round(float(np.var(window)), 6)


def test_track_gap_ratio_counts_missing_ticks() -> None:
    assert F.track_gap_ratio([0.0, 2.0, 4.0, 8.0], TICK_S) == 0.2
    assert F.track_gap_ratio([0.0, 2.0, 4.0], TICK_S) == 0.0
    assert F.track_gap_ratio([4.0], TICK_S) == 0.0
    assert F.track_gap_ratio([], TICK_S) is None


def test_dwell_time_counts_trailing_run_in_zone() -> None:
    t = np.array([0.0, 2.0, 4.0, 6.0, 10.0])
    assert F.dwell_time_s(t, [True, False, True, True, True]) == 6.0
    assert F.dwell_time_s(t, [True] * 5) == 10.0
    assert F.dwell_time_s(t[:1], [True]) == 0.0
    with pytest.raises(ValueError):
        F.dwell_time_s(t, [True])


def test_closing_rate_and_time_to_asset() -> None:
    track = straight(speed=1.0, heading_deg=90.0, duration_s=120.0)  # ends at x=120
    result = F.closing_to_asset(track, 620.0, 0.0)
    assert result is not None
    closing, time_s = result
    assert closing == pytest.approx(1.0, abs=1e-6)
    assert time_s == pytest.approx(500.0, abs=1e-3)
    # abeam: zero closing rate is not closing
    side = F.closing_to_asset(track, 120.0, 300.0)
    assert side is not None and side[1] == F.NOT_CLOSING_S
    assert F.closing_to_asset(track, 120.0, 0.0) == (0.0, 0.0)


def test_nearest_point_index_ties_go_low() -> None:
    assert F.nearest_point_index(0.0, 0.0, [(10.0, 0.0), (0.0, 10.0), (5.0, 0.0)]) == 2
    assert F.nearest_point_index(0.0, 0.0, [(10.0, 0.0), (-10.0, 0.0)]) == 0
    assert F.nearest_point_index(0.0, 0.0, []) is None


# --- loiter: limits and monotonicity -----------------------------------------------


def _approach_then_hold(
    hold_fraction: float, *, sigma_m: float = 0.0, seed: int = 0
) -> TrackArrays:
    """600 s: travel in from 1 km at a steady pace, then hold at the asset (origin)."""
    t = np.arange(0.0, 600.0 + 1e-9, 2.0)
    t_arrive = 600.0 * (1.0 - hold_fraction)
    x = np.where(t < t_arrive, 1000.0 * (1.0 - t / max(t_arrive, 1e-9)), 0.0)
    return make_track(t, x, np.zeros_like(t), sigma_m=sigma_m, seed=seed)


def test_loiter_limits() -> None:
    at_asset = stationary(sigma_m=0.0)
    assert F.loiter_score(at_asset, [(0.0, 0.0)]) == 1.0
    assert F.loiter_score(at_asset, [(250.0, 0.0)]) == 0.0  # no asset within 200 m
    assert F.loiter_score(at_asset, []) == 0.0
    assert F.loiter_score(at_asset, [(250.0, 0.0), (0.0, 30.0)]) == 1.0


def test_loiter_orbit_around_asset_is_high() -> None:
    for seed in (1, 2, 3):
        score = F.loiter_score(orbit(radius=40.0, sigma_m=10.0, seed=seed), [(0.0, 0.0)])
        assert score is not None and F.bucketize("loiter_score", score) == "high"


def test_loiter_straight_pass_is_low() -> None:
    track = straight(speed=1.0, heading_deg=90.0, x0=-300.0, y0=20.0)
    score = F.loiter_score(track, [(0.0, 0.0)])
    assert score is not None and F.bucketize("loiter_score", score) == "low"


@pytest.mark.parametrize("sigma_m", [0.0, 10.0])
def test_loiter_monotone_in_time_held(sigma_m: float) -> None:
    fractions = [0.0, 0.2, 0.4, 0.6, 0.8, 1.0]
    scores = [
        F.loiter_score(_approach_then_hold(f, sigma_m=sigma_m, seed=4), [(0.0, 0.0)])
        for f in fractions
    ]
    values = [s for s in scores if s is not None]
    assert len(values) == len(fractions)
    assert values == sorted(values)
    assert values[-1] == 1.0 or sigma_m > 0.0
    assert values[0] < 0.3


# --- drift consistency: limits and monotonicity ------------------------------------


def _steady(vx: float, vy: float, *, sigma_m: float = 0.0, seed: int = 0) -> TrackArrays:
    t = np.arange(0.0, 600.0 + 1e-9, 2.0)
    return make_track(t, vx * t, vy * t, sigma_m=sigma_m, seed=seed)


CURRENT = (0.12, 0.09)  # 0.15 m/s


def _current(_x: float, _y: float, _t: float) -> tuple[float, float]:
    return CURRENT


def test_drift_consistency_limits() -> None:
    assert F.drift_consistency(_steady(*CURRENT), _current) == 1.0
    still = F.drift_consistency(_steady(0.0, 0.0), _current)
    assert still is not None and still <= 1e-5  # 1 - |c| / (|c| + eps)
    # noise-free steady velocity: exactly the WO-012 per-window formula
    v = (1.0, 0.0)
    expected = 1.0 - math.hypot(v[0] - CURRENT[0], v[1] - CURRENT[1]) / (
        math.hypot(*v) + math.hypot(*CURRENT) + F.DRIFT_EPS_MPS
    )
    assert F.drift_consistency(_steady(*v), _current) == round(expected, 6)
    # straight against the current: |v - c| = |v| + |c| -> 0
    against = F.drift_consistency(_steady(-0.24, -0.18), _current)
    assert against is not None and against <= 1e-5


def test_drift_consistency_monotone_in_self_propulsion() -> None:
    perp = (-CURRENT[1] / 0.15, CURRENT[0] / 0.15)  # unit vector across the current
    scores = []
    for alpha in (0.0, 0.05, 0.1, 0.2, 0.4, 0.8, 1.6):
        v = (CURRENT[0] + alpha * perp[0], CURRENT[1] + alpha * perp[1])
        score = F.drift_consistency(_steady(*v), _current)
        assert score is not None
        scores.append(score)
    assert scores == sorted(scores, reverse=True)
    assert scores[0] == 1.0 and scores[-1] < 0.3


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_drift_consistency_under_noise(seed: int) -> None:
    drifting = F.drift_consistency(_steady(*CURRENT, sigma_m=15.0, seed=seed), _current)
    propelled = F.drift_consistency(_steady(1.0, 0.2, sigma_m=15.0, seed=seed), _current)
    still = F.drift_consistency(_steady(0.0, 0.0, sigma_m=15.0, seed=seed), _current)
    assert drifting is not None and F.bucketize("drift_consistency", drifting) == "high"
    assert propelled is not None and F.bucketize("drift_consistency", propelled) == "low"
    assert still is not None and F.bucketize("drift_consistency", still) == "low"


def test_drift_consistency_reads_current_at_window_positions() -> None:
    seen: list[tuple[float, float, float]] = []

    def spy(x: float, y: float, t: float) -> tuple[float, float]:
        seen.append((x, y, t))
        return (0.0, 0.0)

    track = _steady(1.0, 0.0)
    F.drift_consistency(track, spy)
    fits = fit_windows(track.tail(F.DRIFT_WINDOW_S))
    assert len(seen) == len(fits)
    assert seen[0] == pytest.approx((float(fits.x_c[0]), 0.0, float(fits.t_mid[0])))


# --- determinism -------------------------------------------------------------------


def _all_outputs(track: TrackArrays) -> list[object]:
    assets = [(0.0, 0.0), (150.0, -40.0)]
    return [
        F.speed_over_ground(track),
        F.speed_variance(track),
        F.depth(track),
        F.depth_variance(track),
        F.heading_persistence(track),
        F.turn_rate(track),
        F.closing_to_asset(track, 150.0, -40.0),
        F.drift_consistency(track, _current),
        F.loiter_score(track, assets),
        F.classify_transit_pattern(track),
        F.transit_stats(track),
        F.track_gap_ratio(track.t_s, TICK_S),
        F.dwell_time_s(track.t_s, track.x_m > -1e9),
        F.track_confidence(track),
    ]


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_determinism_every_function(seed: int) -> None:
    first = orbit(sigma_m=12.0, seed=seed)
    again = orbit(sigma_m=12.0, seed=seed)
    copy = TrackArrays.from_columns(
        first.t_s.copy(),
        first.x_m.copy(),
        first.y_m.copy(),
        first.depth_m.copy(),
        first.confidence.copy(),
    )
    a, b, c = _all_outputs(first), _all_outputs(again), _all_outputs(copy)
    assert repr(a) == repr(b) == repr(c)
    assert repr(_all_outputs(first)) == repr(a)  # no hidden state between calls


def test_determinism_outputs_rounded_to_six_dp() -> None:
    track = orbit(sigma_m=12.0, seed=5)
    for value in _all_outputs(track):
        items = value if isinstance(value, tuple) else (value,)
        for item in items:
            if isinstance(item, float):
                assert item == round(item, 6)


def test_track_arrays_validate_and_are_read_only() -> None:
    track = line(5)
    with pytest.raises(ValueError):
        track.x_m[0] = 1.0
    with pytest.raises(ValueError):
        TrackArrays.from_columns([0.0, 0.0], [0, 0], [0, 0], [1, 1], [0.5, 0.5])
    with pytest.raises(ValueError):
        TrackArrays.from_columns([0.0, 2.0], [0, 0], [0, 0], [1, 1], [0.5, 1.5])
    with pytest.raises(ValueError):
        TrackArrays.from_columns([0.0, 2.0], [0, 0], [0], [1, 1], [0.5, 0.5])
    assert len(track.tail(4.0)) == 3


# --- Track-based rules (WO-010 Track + Harbor in, evidence rows out) ---------------

HARBOR = load_harbor()
KIN = set(F.KINEMATIC_FEATURES)


def _rows(track: Track, harbor: Harbor = HARBOR) -> dict[FeatureName, list[EvidenceRow]]:
    out: dict[FeatureName, list[EvidenceRow]] = {}
    for row in F.contact_evidence(track, harbor, calm, None, hour_of_day=10):
        out.setdefault(row.feature, []).append(row)
    return out


def _one(rows: dict[FeatureName, list[EvidenceRow]], feature: FeatureName) -> EvidenceRow:
    assert len(rows[feature]) == 1, feature
    return rows[feature][0]


# points -> kinematic features with a value (the rest are unknown: insufficient_points).
# Noise-free 1 m/s line at 2 s ticks, heading east in main_channel toward no asset
# ahead; the nearest critical asset lies north-west, so closing is computed, not None.
TRACK_UNKNOWN_TABLE: dict[int, set[FeatureName]] = {
    1: {FeatureName.DEPTH, FeatureName.TRACK_GAP_RATIO, FeatureName.DWELL_TIME_IN_ZONE},
    2: {
        FeatureName.DEPTH,
        FeatureName.TRACK_GAP_RATIO,
        FeatureName.DWELL_TIME_IN_ZONE,
        FeatureName.CLOSING_RATE_TO_NEAREST_CRITICAL_ASSET,
        FeatureName.TIME_TO_NEAREST_CRITICAL_ASSET,
    },
}
TRACK_UNKNOWN_TABLE[3] = TRACK_UNKNOWN_TABLE[2] | {
    FeatureName.SPEED_OVER_GROUND,
    FeatureName.SPEED_VARIANCE,
    FeatureName.DEPTH_VARIANCE,
    FeatureName.TURN_RATE,
    FeatureName.DRIFT_CONSISTENCY,
}
TRACK_UNKNOWN_TABLE[9] = TRACK_UNKNOWN_TABLE[3] | {FeatureName.HEADING_PERSISTENCE}
TRACK_UNKNOWN_TABLE[10] = set(TRACK_UNKNOWN_TABLE[9])  # span 18 s < 60 s: no pattern


@pytest.mark.parametrize("n", sorted(TRACK_UNKNOWN_TABLE))
def test_unknown_rules_track_by_point_count(n: int) -> None:
    rows = _rows(line_track(n))
    for feature in F.KINEMATIC_FEATURES:
        row = _one(rows, feature)
        assert row.kind is EvidenceKind.DERIVED
        if feature in TRACK_UNKNOWN_TABLE[n]:
            assert row.value != UNKNOWN, (n, feature)
            assert row.source == f"extractor:{F.FEATURE_CATALOG[feature].extractor}"
        else:
            assert row.value == UNKNOWN, (n, feature, row.value)
            assert row.source == F.INSUFFICIENT_POINTS, (n, feature)
            assert row.confidence == 0.0
        assert row.unit == F.FEATURE_CATALOG[feature].unit


def test_unknown_rules_track_ten_points_over_a_minute_has_a_pattern() -> None:
    t = np.array([0, 2, 4, 6, 8, 10, 12, 14, 16, 60], dtype=np.float64)
    track = to_track(make_track(t, t * 1.0, np.zeros_like(t)))
    assert _one(_rows(track), FeatureName.TRANSIT_PATTERN).value == "straight"
    nine = to_track(make_track(t[:9], t[:9] * 1.0, np.zeros(9)))
    assert _one(_rows(nine), FeatureName.TRANSIT_PATTERN).value == UNKNOWN


def test_unknown_rules_track_loiter_needs_300_s() -> None:
    short = to_track(stationary(duration_s=298.0, sigma_m=0.0), origin_xy=FUEL_BERTHS_XY)
    full = to_track(stationary(duration_s=300.0, sigma_m=0.0), origin_xy=FUEL_BERTHS_XY)
    assert _one(_rows(short), FeatureName.LOITER_SCORE).value == UNKNOWN
    assert _one(_rows(full), FeatureName.LOITER_SCORE).value == 1.0


def test_unknown_rules_track_last_point_outside_every_zone() -> None:
    rows = _rows(line_track(40, origin_xy=OUTSIDE_ZONES_XY))
    for feature in (
        FeatureName.ZONE_ID,
        FeatureName.ZONE_CRITICALITY,
        FeatureName.DWELL_TIME_IN_ZONE,
    ):
        row = _one(rows, feature)
        assert row.value == UNKNOWN and row.source == F.NO_SENSOR_COVERAGE, feature
    # the rest of the context still resolves
    assert _one(rows, FeatureName.NEAREST_ASSET_ID).value != UNKNOWN


def test_unknown_rules_track_zone_rows_inside_a_zone() -> None:
    rows = _rows(line_track(40, origin_xy=FUEL_BERTHS_XY, heading_deg=0.0, speed=0.2))
    assert _one(rows, FeatureName.ZONE_ID).value == "fuel_berths"  # highest criticality
    assert _one(rows, FeatureName.ZONE_CRITICALITY).value == 0.9
    assert _one(rows, FeatureName.DWELL_TIME_IN_ZONE).value == 78.0


def test_unknown_rules_track_no_critical_asset() -> None:
    low = tuple(a.model_copy(update={"criticality": 0.5}) for a in HARBOR.assets)
    harbor = Harbor(
        name=HARBOR.name, zones=HARBOR.zones, assets=low, sensor_nodes=HARBOR.sensor_nodes
    )
    rows = _rows(line_track(40), harbor)
    for feature in (
        FeatureName.CLOSING_RATE_TO_NEAREST_CRITICAL_ASSET,
        FeatureName.TIME_TO_NEAREST_CRITICAL_ASSET,
    ):
        row = _one(rows, feature)
        assert row.value == UNKNOWN and row.source == F.NO_CRITICAL_ASSET, feature
    assert _one(rows, FeatureName.NEAREST_ASSET_ID).value != UNKNOWN  # any criticality
    empty = Harbor(name=HARBOR.name, zones=HARBOR.zones, assets=(), sensor_nodes=())
    rows = _rows(line_track(40), empty)
    assert _one(rows, FeatureName.NEAREST_ASSET_ID).source == F.NO_CRITICAL_ASSET
    assert _one(rows, FeatureName.LOITER_SCORE).value == UNKNOWN  # span 78 s < 300 s
    assert _one(rows, FeatureName.SENSOR_HEALTH).source == F.NO_SENSOR_COVERAGE


def test_unknown_rules_track_receding_contact_is_not_closing() -> None:
    # starts 150 m south of fuel_berth_1 and heads south: the nearest critical asset
    # stays fuel_berth_1 and the range grows
    rows = _rows(line_track(60, heading_deg=180.0, origin_xy=(138.7, 128.0)))
    closing = _one(rows, FeatureName.CLOSING_RATE_TO_NEAREST_CRITICAL_ASSET)
    time_to = _one(rows, FeatureName.TIME_TO_NEAREST_CRITICAL_ASSET)
    assert isinstance(closing.value, float) and closing.value < 0.0
    assert time_to.value == F.NOT_CLOSING_S == 1e9


def test_unknown_rules_track_approaching_contact_closes() -> None:
    # 600 m south of fuel_berth_1, heading north straight at it
    start = (FUEL_BERTHS_XY[0] + 48.0, FUEL_BERTHS_XY[1] - 600.0)
    rows = _rows(line_track(60, heading_deg=0.0, origin_xy=start))
    closing = _one(rows, FeatureName.CLOSING_RATE_TO_NEAREST_CRITICAL_ASSET)
    time_to = _one(rows, FeatureName.TIME_TO_NEAREST_CRITICAL_ASSET)
    assert isinstance(closing.value, float) and closing.value > 0.9
    assert isinstance(time_to.value, float) and 0.0 < time_to.value < 1000.0


def test_unknown_rules_track_kinematic_confidence_is_min_point_confidence() -> None:
    n = 400  # 798 s: the 120 s, 600 s and full windows hold different readings
    conf = np.full(n, 0.9)
    conf[10] = 0.2  # only in the full window
    conf[150] = 0.5  # in the 600 s window, not the 120 s one
    conf[-5] = 0.7  # in every window
    rows = _rows(line_track(n, confidence=conf))
    expected = {"full": 0.2, "pattern": 0.5, "kinematic": 0.7}
    segments = {r.row_id: r for r in rows[FeatureName.TRACK_SEGMENT]}
    assert sorted(r.confidence for r in segments.values()) == [0.2, 0.5, 0.7]  # type: ignore[type-var]
    for feature in F.KINEMATIC_FEATURES:
        row = _one(rows, feature)
        source = segments[row.derived_from[0]]
        assert source.confidence == expected[F.FEATURE_SEGMENT[feature]], feature
        if row.value != UNKNOWN:
            assert row.confidence == source.confidence, feature


def test_unknown_rules_track_short_track_shares_one_segment_row() -> None:
    rows = _rows(line_track(5))
    assert len(rows[FeatureName.TRACK_SEGMENT]) == 1
    assert rows[FeatureName.TRACK_SEGMENT][0].value == "ticks=0..4;readings=5"


def test_unknown_rules_track_gap_ratio_is_wo010_gap_ratio() -> None:
    t = np.array([0.0, 2.0, 4.0, 12.0, 14.0])
    track = to_track(make_track(t, t, np.zeros_like(t)), gaps=[(3, 5)])
    assert _one(_rows(track), FeatureName.TRACK_GAP_RATIO).value == track.gap_ratio == 0.375


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_velocity_fit_track_survives_reading_noise(seed: int) -> None:
    clean = straight(speed=1.0, heading_deg=60.0, duration_s=600.0)
    noisy = make_track(clean.t_s, clean.x_m, clean.y_m, sigma_m=15.0, seed=seed)
    rows = _rows(to_track(noisy, sigma_m=15.0, origin_xy=(100.0, -2500.0)))
    speed = _one(rows, FeatureName.SPEED_OVER_GROUND).value
    persistence = _one(rows, FeatureName.HEADING_PERSISTENCE).value
    assert isinstance(speed, float) and abs(speed - 1.0) <= 0.1, speed
    assert isinstance(persistence, float) and persistence >= 0.9, persistence
    assert _one(rows, FeatureName.TRANSIT_PATTERN).value == "straight"


def test_velocity_fit_track_arrays_use_ticks_metres_and_sigma() -> None:
    track = line_track(5, sigma_m=12.0)
    arrays = F.track_arrays(track)
    assert list(arrays.t_s) == [0.0, 2.0, 4.0, 6.0, 8.0]
    assert arrays.sigma_m is not None and set(arrays.sigma_m) == {12.0}
    x0, y0 = to_local_m(track.points[0].lat, track.points[0].lon)
    assert (float(arrays.x_m[0]), float(arrays.y_m[0])) == (x0, y0)
