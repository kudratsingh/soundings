"""Detection model properties (docs/SPEC-SIM.md section 2, SPEC-INVESTIGATION section 9.10).

Stage 1 covers the pure functions in ``soundings.sim.detection``. The tests that need a
full ``SimRun`` (the timeout re-acquisition case) join this file with ``run.py``.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from soundings.sim.acoustics import TONAL_BANDS, load_acoustic_defaults
from soundings.sim.detection import (
    DEFAULT_SIM_CONFIG_PATH,
    DetectionParams,
    GapState,
    detection_params_from_mapping,
    detection_probability,
    false_alarm_rate_per_tick,
    fuse_nearest,
    gap_step,
    load_detection_params,
    position_sigma_m,
    reading_confidence,
    sample_detected,
    sample_false_alarm,
    sample_false_alarm_arrivals,
    sample_position_noise,
    split_on_timeout,
)

PARAMS = DetectionParams()
R0 = PARAMS.r0_m


# --- config ---------------------------------------------------------------


def test_sim_yaml_matches_spec_defaults() -> None:
    params = load_detection_params()
    assert params == DetectionParams()
    assert params.p_max == 0.95
    assert params.r0_m == 400.0
    assert params.max_range_factor == 2.0
    assert params.noise_sigma_base_m == 5.0
    assert params.noise_sigma_per_m == 0.03
    assert params.gap_rate_per_tick == 0.08
    assert params.false_alarms_per_node_hour == 6.0
    assert params.false_alarm_click_prob == 0.7
    # DECISIONS.md 2026-10-16 22:25
    assert params.gap_mean_ticks == 5
    assert params.track_timeout_ticks == 15
    assert params.false_alarm_mean_ticks == 20


def test_detection_params_reject_bad_input() -> None:
    with pytest.raises(ValueError, match="unknown"):
        detection_params_from_mapping({"p_maxx": 0.9})
    with pytest.raises(ValueError, match="number"):
        detection_params_from_mapping({"p_max": True})
    with pytest.raises(ValueError, match="integer"):
        detection_params_from_mapping({"gap_mean_ticks": 5.5})
    with pytest.raises(ValueError):
        detection_params_from_mapping({"p_max": 1.5})


def test_load_detection_params_missing_block(tmp_path: Path) -> None:
    path = tmp_path / "sim.yaml"
    path.write_text("other: {}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="detection"):
        load_detection_params(path)


def test_acoustic_defaults_load_in_band_order() -> None:
    defaults = load_acoustic_defaults(DEFAULT_SIM_CONFIG_PATH)
    assert tuple(defaults) == TONAL_BANDS
    cav = [defaults[b].cavitation_index for b in TONAL_BANDS]
    level = [defaults[b].broadband_level_db for b in TONAL_BANDS]
    assert cav == sorted(cav) and level == sorted(level)
    assert all(0.0 <= c <= 1.0 for c in cav)


# --- (a) monotone range falloff ------------------------------------------


def test_detection_probability_monotone_grid() -> None:
    values = [detection_probability(float(r), R0, 1.0, PARAMS.p_max) for r in range(0, 1001, 10)]
    assert all(b <= a for a, b in zip(values, values[1:], strict=False))
    assert values[0] == pytest.approx(0.95)
    for r in range(810, 1001, 10):
        assert detection_probability(float(r), R0, 1.0, PARAMS.p_max) == 0.0
    assert detection_probability(800.0, R0, 1.0, PARAMS.p_max) > 0.0


@settings(derandomize=True, max_examples=300)
@given(
    a=st.floats(min_value=0.0, max_value=2000.0, allow_nan=False),
    b=st.floats(min_value=0.0, max_value=2000.0, allow_nan=False),
    health=st.floats(min_value=0.0, max_value=1.0, allow_nan=False),
)
def test_detection_probability_monotone_pairs(a: float, b: float, health: float) -> None:
    near, far = min(a, b), max(a, b)
    p_near = detection_probability(near, R0, health, PARAMS.p_max)
    p_far = detection_probability(far, R0, health, PARAMS.p_max)
    assert 0.0 <= p_far <= p_near <= 1.0
    if far > 2 * R0:
        assert p_far == 0.0


def test_empirical_detection_fraction_strictly_decreasing() -> None:
    rng = np.random.default_rng(101)
    fractions = []
    for r in (100.0, 300.0, 500.0, 700.0):
        hits = sum(sample_detected(rng, r, R0, 1.0, PARAMS) for _ in range(10_000))
        fractions.append(hits / 10_000)
        expected = detection_probability(r, R0, 1.0, PARAMS.p_max)
        assert abs(hits / 10_000 - expected) < 0.02
    assert all(b < a for a, b in zip(fractions, fractions[1:], strict=False))


# --- (b) biologic false alarms --------------------------------------------


def test_false_alarm_rate_over_10k_node_hours() -> None:
    tick_s = 2.0
    rate = false_alarm_rate_per_tick(PARAMS.false_alarms_per_node_hour, tick_s)
    assert rate == pytest.approx(6.0 * 2.0 / 3600.0)
    node_hours = 10_000
    ticks_per_hour = int(3600 / tick_s)
    rng = np.random.default_rng(202)
    total = 0
    chunk_hours = 1_000
    for _ in range(node_hours // chunk_hours):
        counts = sample_false_alarm_arrivals(rng, rate, size=chunk_hours * ticks_per_hour)
        assert isinstance(counts, np.ndarray)
        total += int(counts.sum())
    per_node_hour = total / node_hours
    assert abs(per_node_hour - 6.0) / 6.0 < 0.10
    assert total > 50_000


def test_false_alarm_click_fraction_and_shape() -> None:
    rng = np.random.default_rng(303)
    spawns = [sample_false_alarm(rng, 100.0, -50.0, R0, PARAMS) for _ in range(12_000)]
    clicks = sum(s.click for s in spawns) / len(spawns)
    assert abs(clicks - 0.70) <= 0.03
    for s in spawns:
        assert (s.x_m - 100.0) ** 2 + (s.y_m + 50.0) ** 2 <= R0**2 + 1e-6
        assert 0.0 <= s.depth_m <= 3.0
        assert s.lifetime_ticks >= 1
    mean_life = sum(s.lifetime_ticks for s in spawns) / len(spawns)
    assert abs(mean_life - PARAMS.false_alarm_mean_ticks) / PARAMS.false_alarm_mean_ticks < 0.05


def test_single_tick_arrival_is_int() -> None:
    rng = np.random.default_rng(1)
    assert isinstance(sample_false_alarm_arrivals(rng, 0.5), int)


# --- (c) track gaps ---------------------------------------------------------


def test_gap_rate_and_mean_length() -> None:
    rng = np.random.default_rng(404)
    state = GapState()
    eligible = 0
    starts = 0
    lengths: list[int] = []
    for _ in range(250_000):
        detected = sample_detected(rng, 0.0, R0, 1.0, PARAMS)
        was_free = state.remaining == 0
        emitted, state = gap_step(rng, state, detected, PARAMS)
        if was_free and detected:
            eligible += 1
            if not emitted:
                starts += 1
                lengths.append(state.remaining + 1)
    assert eligible >= 10_000
    assert abs(starts / eligible - 0.08) / 0.08 < 0.15
    assert abs(float(np.mean(lengths)) - 5.0) / 5.0 < 0.15


def test_gap_step_counts_down_and_never_emits_inside_gap() -> None:
    rng = np.random.default_rng(5)
    emitted, state = gap_step(rng, GapState(3), True, PARAMS)
    assert not emitted and state == GapState(2)
    emitted, state = gap_step(rng, GapState(0), False, PARAMS)
    assert not emitted and state == GapState(0)
    no_gaps = DetectionParams(gap_rate_per_tick=0.0)
    emitted, state = gap_step(rng, GapState(0), True, no_gaps)
    assert emitted and state == GapState(0)


def test_split_on_timeout() -> None:
    assert split_on_timeout([], 15) == []
    # 15 missing ticks (1..15) is not longer than the timeout; 16 missing ticks is.
    assert split_on_timeout([0, 16], 15) == [[0, 16]]
    assert split_on_timeout([0, 17], 15) == [[0], [17]]
    assert split_on_timeout([0, 1, 2, 40, 41, 100], 15) == [[0, 1, 2], [40, 41], [100]]
    with pytest.raises(ValueError):
        split_on_timeout([3, 3], 15)


# --- (d) position noise -----------------------------------------------------


def test_position_sigma_formula() -> None:
    assert position_sigma_m(0.0) == 5.0
    assert position_sigma_m(300.0) == pytest.approx(14.0)
    assert position_sigma_m(800.0) == pytest.approx(29.0)


def test_position_error_std_at_300m() -> None:
    rng = np.random.default_rng(505)
    samples = np.array([sample_position_noise(rng, 300.0, PARAMS) for _ in range(10_000)])
    for axis in (0, 1):
        std = float(samples[:, axis].std())
        assert abs(std - 14.0) / 14.0 < 0.10
        assert abs(float(samples[:, axis].mean())) < 1.0


# --- (e) reading confidence ---------------------------------------------------


def test_confidence_non_increasing_in_range() -> None:
    values = [reading_confidence(float(r), R0, 1.0) for r in range(0, 1001, 10)]
    assert all(b <= a for a, b in zip(values, values[1:], strict=False))
    assert values[0] == 1.0
    assert all(0.0 <= v <= 1.0 and v == round(v, 6) for v in values)


@settings(derandomize=True, max_examples=200)
@given(
    r=st.floats(min_value=0.0, max_value=1000.0, allow_nan=False),
    health=st.floats(min_value=0.0, max_value=1.0, allow_nan=False),
)
def test_confidence_linear_in_health(r: float, health: float) -> None:
    full = reading_confidence(r, R0, 1.0)
    assert reading_confidence(r, R0, health) == pytest.approx(health * full, abs=1.5e-6)
    assert reading_confidence(r, R0, 0.0) == 0.0


def test_health_zero_yields_no_detections() -> None:
    rng = np.random.default_rng(606)
    for r in (0.0, 100.0, 400.0):
        assert detection_probability(r, R0, 0.0, PARAMS.p_max) == 0.0
        assert not any(sample_detected(rng, r, R0, 0.0, PARAMS) for _ in range(10_000))


def test_bad_inputs_raise() -> None:
    with pytest.raises(ValueError):
        detection_probability(10.0, R0, 1.5, 0.95)
    with pytest.raises(ValueError):
        detection_probability(-1.0, R0, 1.0, 0.95)
    with pytest.raises(ValueError):
        reading_confidence(10.0, 0.0, 1.0)
    with pytest.raises(ValueError):
        false_alarm_rate_per_tick(6.0, 0.0)


# --- fusion -------------------------------------------------------------------


def test_fuse_nearest_with_node_id_tiebreak() -> None:
    assert fuse_nearest([]) is None
    assert fuse_nearest([("hyd_2", 300.0), ("hyd_1", 120.0)]) == ("hyd_1", 120.0)
    assert fuse_nearest([("hyd_3", 50.0), ("hyd_2", 50.0)]) == ("hyd_2", 50.0)
