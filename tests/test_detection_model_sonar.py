"""WO-089: the WO-011 detection properties re-run under ``detection.model: sonar_equation``.

Monotone falloff, the gap rate and length, the false-alarm rate (10k node-hours) and the
per-tick ceiling are unchanged; NL per node rises with synthetic vessel fixes (WO-080 part A
supplies real ones) and lowers detection there.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

import numpy as np
import pytest

from soundings.physics import ACOUSTIC_CLASSES, detection_probability_at
from soundings.schemas.geo import from_local_m, to_local_m
from soundings.schemas.harbor import load_harbor
from soundings.sim.detection import (
    DETECTION_MODELS,
    DetectionParams,
    GapState,
    acoustic_class,
    detection_params_from_mapping,
    false_alarm_rate_per_tick,
    gap_step,
    load_detection_params,
    sample_detected_sonar,
    sample_false_alarm_arrivals,
    sonar_detection_probability,
    sonar_reading_confidence,
    sonar_setup,
    sonar_term,
)
from soundings.sim.hashing import track_hash
from soundings.sim.run import NodeNoise, run_scenario
from soundings.sim.scenario import resolve_scenario

PARAMS = replace(load_detection_params(), model="sonar_equation")
SETUP = sonar_setup()
NL0 = SETUP.base_nl_db
NODES = sorted(load_harbor().sensor_nodes, key=lambda n: n.node_id)


@dataclass(frozen=True)
class Fix:
    tick: int
    lat: float
    lon: float
    sog_mps: float


def test_sonar_is_the_configured_default() -> None:
    assert load_detection_params().model in DETECTION_MODELS
    assert DetectionParams().model == "gaussian"  # WO-011 stays the default (DECISIONS 10:30)
    assert DETECTION_MODELS == ("sonar_equation", "gaussian")
    assert detection_params_from_mapping({"model": "gaussian"}).model == "gaussian"
    with pytest.raises(ValueError):
        detection_params_from_mapping({"model": "rays"})
    with pytest.raises(ValueError):
        DetectionParams(model="rays")


def test_acoustic_classes() -> None:
    assert acoustic_class(True, False) == "propeller_tonal_present"
    assert acoustic_class(True, True) == "propeller_tonal_present"
    assert acoustic_class(False, True) == "biologic"
    assert acoustic_class(False, False) == "propeller_tonal_absent"


@pytest.mark.parametrize("cls", ACOUSTIC_CLASSES)
def test_probability_monotone_grid_and_ceiling(cls: str) -> None:
    for health in (1.0, 0.7, 0.3):
        p = [
            sonar_detection_probability(float(r), cls, NL0, health, PARAMS.p_max, SETUP)
            for r in np.arange(0.0, 1200.0, 1.0)
        ]
        assert all(b <= a for a, b in zip(p, p[1:], strict=False))
        assert max(p) <= PARAMS.p_max * health
        assert p[0] == pytest.approx(PARAMS.p_max * health, abs=1e-3)
    cut = SETUP.cutoff(cls)
    assert sonar_detection_probability(cut + 0.01, cls, NL0, 1.0, PARAMS.p_max, SETUP) == 0.0
    assert sonar_detection_probability(cut - 0.01, cls, NL0, 1.0, PARAMS.p_max, SETUP) > 0.0
    assert sonar_detection_probability(400.0, cls, NL0, 1.0, 1.0, SETUP) == pytest.approx(0.5)


def test_empirical_detection_fraction_strictly_decreasing() -> None:
    rng = np.random.default_rng(101)
    fractions = []
    for r in (50.0, 250.0, 400.0, 550.0, 750.0):
        hits = sum(
            sample_detected_sonar(rng, r, "propeller_tonal_present", NL0, 1.0, PARAMS, SETUP)
            for _ in range(4000)
        )
        fractions.append(hits / 4000)
    assert all(b < a for a, b in zip(fractions, fractions[1:], strict=False)), fractions


def test_health_zero_yields_no_detections() -> None:
    rng = np.random.default_rng(3)
    assert not any(
        sample_detected_sonar(rng, 10.0, "biologic", NL0, 0.0, PARAMS, SETUP) for _ in range(500)
    )


def test_gap_rate_and_mean_length_unchanged() -> None:
    rng = np.random.default_rng(404)
    state = GapState()
    eligible = starts = 0
    lengths: list[int] = []
    for _ in range(250_000):
        detected = sample_detected_sonar(
            rng, 0.0, "propeller_tonal_present", NL0, 1.0, PARAMS, SETUP
        )
        was_free = state.remaining == 0
        emitted, state = gap_step(rng, state, detected, PARAMS)
        if was_free and detected:
            eligible += 1
            if not emitted:
                starts += 1
                lengths.append(state.remaining + 1)
    assert eligible >= 10_000
    assert abs(starts / eligible - PARAMS.gap_rate_per_tick) / PARAMS.gap_rate_per_tick < 0.15
    assert abs(float(np.mean(lengths)) - PARAMS.gap_mean_ticks) / PARAMS.gap_mean_ticks < 0.15


def test_false_alarm_rate_over_10k_node_hours_unchanged() -> None:
    tick_s = 2.0
    rate = false_alarm_rate_per_tick(PARAMS.false_alarms_per_node_hour, tick_s)
    rng = np.random.default_rng(202)
    total = 0
    for _ in range(10):
        counts = sample_false_alarm_arrivals(rng, rate, size=1_000 * int(3600 / tick_s))
        assert isinstance(counts, np.ndarray)
        total += int(counts.sum())
    assert abs(total / 10_000 - PARAMS.false_alarms_per_node_hour) / 6.0 < 0.10


def test_false_alarm_arrivals_identical_across_models() -> None:
    sc = resolve_scenario("S07")
    sonar = run_scenario(sc, seed=2, params=PARAMS)
    gauss = run_scenario(sc, seed=2, params=replace(PARAMS, model="gaussian"))
    fa = sorted(k for k in sonar.truth_paths if k.startswith("fa-"))
    assert fa == sorted(k for k in gauss.truth_paths if k.startswith("fa-"))
    for k in fa:
        assert sonar.truth_paths[k] == gauss.truth_paths[k]


def test_confidence_is_health_times_the_logistic_term() -> None:
    sc = resolve_scenario("S05")
    run = run_scenario(sc, seed=11, params=PARAMS)
    for d in run.detections[:400]:
        cls = "biologic" if d.is_false_alarm else None
        if cls is None:
            continue
        term = sonar_term(d.range_m, cls, NL0, SETUP)
        assert d.confidence == pytest.approx(
            sonar_reading_confidence(term, d.sensor_health), abs=2e-6
        )
    confs = [
        sonar_reading_confidence(sonar_term(float(r), "biologic", NL0, SETUP), 1.0)
        for r in range(0, 900, 5)
    ]
    assert all(b <= a for a, b in zip(confs, confs[1:], strict=False))
    assert sonar_reading_confidence(0.5, 0.5) == 0.25


def test_node_noise_rises_near_a_synthetic_vessel_and_lowers_detection() -> None:
    hyd = NODES[0]
    x, y = to_local_m(hyd.lat, hyd.lon)
    lat, lon = from_local_m(x + 60.0, y)
    tracks = {"v1": [Fix(10, lat, lon, 5.0)]}
    noise = NodeNoise(NODES, SETUP, 2.0, tracks)
    assert noise.levels(9) == tuple(NL0 for _ in NODES)
    loud = noise.levels(10)
    assert loud[0] > NL0
    assert all(lv >= NL0 for lv in loud)
    assert loud[0] == max(loud)
    assert noise.levels(10 + 16) == tuple(NL0 for _ in NODES)  # the fix went stale (30 s)
    p_calm = detection_probability_at(300.0, SETUP.sl("propeller_tonal_present"), NL0, SETUP.cfg)
    p_loud = detection_probability_at(
        300.0, SETUP.sl("propeller_tonal_present"), loud[0], SETUP.cfg
    )
    assert p_loud < p_calm


def test_run_with_synthetic_vessel_fixes_is_deterministic_and_quieter() -> None:
    sc = resolve_scenario("S05")
    base = run_scenario(sc, seed=11, params=PARAMS)
    # a vessel parked beside every node for the whole run raises NL everywhere
    tracks = {}
    for i, n in enumerate(NODES):
        nx, ny = to_local_m(n.lat, n.lon)
        lat, lon = from_local_m(nx + 40.0, ny)
        tracks[f"v{i}"] = [Fix(t, lat, lon, 1.0) for t in range(0, sc.duration_ticks, 5)]
    noisy = run_scenario(sc, seed=11, ais_feed=tracks, params=PARAMS)
    again = run_scenario(sc, seed=11, ais_feed=tracks, params=PARAMS)
    assert track_hash(noisy) == track_hash(again)
    real = [d for d in base.detections if not d.is_false_alarm]
    real_noisy = [d for d in noisy.detections if not d.is_false_alarm]
    assert len(real_noisy) < len(real)


def test_detection_rows_match_track_points_with_a_short_node() -> None:
    """The WO-011 row/point test under sonar_equation: a node whose harbor range_m is 100 m
    keeps 100 m as its calm-noise effective range (cut-off 200 m), and every reading's
    confidence is health x the logistic term at that node."""
    from test_detection_model import _one_short_node, _orbit_scenario

    from soundings.schemas.scenario import scenario_from_data
    from soundings.sim.detection import node_gain_db

    scenario = scenario_from_data(_orbit_scenario())
    run = run_scenario(scenario, harbor=_one_short_node(), ticks=400, params=PARAMS)
    gain = node_gain_db(100.0, PARAMS.r0_m, SETUP)
    assert gain < 0.0
    sl = SETUP.sl("propeller_tonal_present") + gain
    assert detection_probability_at(100.0, sl, NL0, SETUP.cfg) == pytest.approx(0.5, abs=1e-4)
    by_key = {(d.observed_contact_id, d.tick): d for d in run.detections}
    assert len(by_key) == sum(len(t.points) for t in run.tracks.values()) > 0
    for cid, track in run.tracks.items():
        for pt in track.points:
            d = by_key[(cid, pt.tick)]
            assert (d.lat, d.lon, d.confidence, d.node_id) == (
                pt.lat,
                pt.lon,
                pt.confidence,
                pt.sensor_id,
            )
            term = sonar_term(d.range_m, "propeller_tonal_present", NL0, SETUP, gain)
            assert d.confidence == pytest.approx(
                sonar_reading_confidence(term, d.sensor_health), abs=2e-6
            )
            assert d.range_m <= 200.0 + 1e-3
