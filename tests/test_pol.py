"""Pattern of life (SPEC-INVESTIGATION section 4): the simulated normal month, its cells,
the anomaly score, and the committed ``data/pol_table.json``."""

from __future__ import annotations

import math
import os
import time
from functools import cache

import numpy as np
import pytest

from soundings.investigation import features as F
from soundings.investigation.pol import (
    ALL_WEEKDAYS,
    BENIGN_HYPOTHESES,
    DEFAULT_POL_PATH,
    MIN_CELL_CONTACTS,
    NUMERIC_FEATURES,
    POL_DAYS,
    POL_SEED,
    STD_FLOOR,
    FeatureStats,
    PolCell,
    PolTable,
    anomaly_score,
    load_pol_table,
    weekday_of_day,
)
from soundings.schemas.enums import FeatureName, Hypothesis
from soundings.schemas.evidence import UNKNOWN, EvidenceValue
from soundings.schemas.harbor import load_harbor
from soundings.sim.current import load_current_field, tide_phase_for_hour
from soundings.sim.normal_month import contact_rate, generate_normal_month, synthetic_scenario
from soundings.sim.run import run_scenario
from soundings.sim.scenario import catalog_paths, resolve_scenario

HARBOR = load_harbor()
ZONES = sorted(z.zone_id for z in HARBOR.zones)
_LOADED = bool(os.environ.get("PYTEST_XDIST_WORKER") or os.environ.get("SOUNDINGS_CI"))
GENERATION_BOUND_S = 60.0 * (4.0 if _LOADED else 1.0)
"""Generation bound on a quiet machine (about 49 s alone); a shared CI runner under
pytest-xdist gets a 4x load allowance (DECISIONS 2026-10-17 11:5x)."""
SPEED = FeatureName.SPEED_OVER_GROUND.value
DEPTH = FeatureName.DEPTH.value
DWELL = FeatureName.DWELL_TIME_IN_ZONE.value
PATTERN = FeatureName.TRANSIT_PATTERN.value
FAST_DEEP_ORBIT: dict[str, EvidenceValue] = {
    SPEED: 3.0,
    DEPTH: 25.0,
    DWELL: 1800.0,
    PATTERN: "orbit",
}


@cache
def _fresh() -> tuple[PolTable, float]:
    start = time.perf_counter()
    table = generate_normal_month(seed=POL_SEED)
    return table, time.perf_counter() - start


@cache
def _committed() -> PolTable:
    return load_pol_table()


def _benign_like(cell: PolCell) -> dict[str, EvidenceValue]:
    """Half a spread above the cell's means, with its most frequent pattern."""
    observed: dict[str, EvidenceValue] = {}
    for feature in NUMERIC_FEATURES:
        stats = cell.numeric[feature.value]
        observed[feature.value] = stats.mean + 0.5 * max(stats.std, STD_FLOOR[feature])
    observed[PATTERN] = max(sorted(cell.transit_pattern), key=lambda p: cell.transit_pattern[p])
    return observed


# --- the committed table is the seeded month ------------------------------------------


def test_persisted_table_equals_fresh_generation_within_bound() -> None:
    fresh, elapsed = _fresh()
    assert elapsed < GENERATION_BOUND_S, f"generation took {elapsed:.1f} s"
    assert fresh.seed == POL_SEED and fresh.days == POL_DAYS
    assert _committed() == fresh
    assert DEFAULT_POL_PATH.read_text(encoding="utf-8") == fresh.to_json()


def test_round_trip_and_canonical_json() -> None:
    table = _committed()
    assert PolTable.from_json(table.to_json()) == table
    assert table.to_json().endswith("\n") and "\n" not in table.to_json()[:-1]


def test_small_month_is_seeded() -> None:
    a = generate_normal_month(seed=7, days=2)
    b = generate_normal_month(seed=7, days=2)
    c = generate_normal_month(seed=8, days=2)
    assert a == b and a.to_json() == b.to_json()
    assert a != c
    with pytest.raises(ValueError):
        generate_normal_month(seed=7, days=0)


# --- cells ----------------------------------------------------------------------------


def test_every_populated_cell_has_density_and_a_full_mix() -> None:
    table = _committed()
    assert table.cells
    for key, cell in table.cells.items():
        zone_id, hour, weekday = key
        assert zone_id in ZONES and 0 <= hour <= 23
        assert weekday in (*F.WEEKDAYS, ALL_WEEKDAYS)
        assert cell.n_contacts > 0 and cell.density > 0.0, key
        assert set(cell.hypothesis_mix) == {h.value for h in BENIGN_HYPOTHESES}, key
        assert math.isclose(sum(cell.hypothesis_mix.values()), 1.0, abs_tol=1e-5), key
        if cell.transit_pattern:
            assert set(cell.transit_pattern) == set(F.TRANSIT_PATTERNS)
            assert math.isclose(sum(cell.transit_pattern.values()), 1.0, abs_tol=1e-5), key
        for stats in cell.numeric.values():
            assert 0 < stats.n <= cell.n_characterized <= cell.n_contacts
            assert stats.std >= 0.0


def test_pooled_cell_counts_every_weekday() -> None:
    table = _committed()
    for zone_id in ZONES:
        for hour in range(24):
            pooled = table.cell(zone_id, hour, ALL_WEEKDAYS)
            parts = [table.cell(zone_id, hour, w) for w in F.WEEKDAYS]
            n = sum(c.n_contacts for c in parts if c is not None)
            if pooled is None:
                assert n == 0
                continue
            assert pooled.n_contacts == n
            assert pooled.n_characterized == sum(c.n_characterized for c in parts if c is not None)
            assert math.isclose(pooled.density, n / POL_DAYS, abs_tol=1e-6)


def test_hypothesis_mix_follows_the_rate_model() -> None:
    """Filed surveys are a working-day pattern; at 02:00 they are rare."""
    table = _committed()
    day = table.cell("inner_basin_research", 10, ALL_WEEKDAYS)
    night = table.cell("inner_basin_research", 2, ALL_WEEKDAYS)
    assert day is not None and night is not None
    filed = Hypothesis.FILED_ON_PLAN.value
    assert day.hypothesis_mix[filed] > night.hypothesis_mix[filed]
    assert contact_rate(Hypothesis.FILED_ON_PLAN, "inner_basin_research", 10, "tue") > (
        contact_rate(Hypothesis.FILED_ON_PLAN, "inner_basin_research", 10, "sat")
    )
    with pytest.raises(ValueError):
        contact_rate(Hypothesis.UNAUTHORIZED_UUV, "fuel_berths", 2, "sat")
    with pytest.raises(ValueError):
        synthetic_scenario(
            Hypothesis.UNAUTHORIZED_UUV, HARBOR.zone("fuel_berths"), 2, np.random.default_rng(0)
        )
    assert [weekday_of_day(d) for d in range(8)] == [*F.WEEKDAYS, "mon"]


# --- anomaly --------------------------------------------------------------------------


def test_anomaly_in_unit_interval() -> None:
    table = _committed()
    rng = np.random.default_rng(3)
    for key, cell in sorted(table.cells.items()):
        for _ in range(3):
            observed: dict[str, EvidenceValue] = {
                SPEED: float(rng.uniform(0.0, 20.0)),
                DEPTH: float(rng.uniform(0.0, 60.0)),
                DWELL: float(rng.uniform(0.0, 5000.0)),
                PATTERN: str(rng.choice(list(F.TRANSIT_PATTERNS))),
            }
            score = anomaly_score(cell, observed)
            if cell.n_characterized == 0:
                assert score is None, key  # nothing to compare against
            else:
                assert score is not None and 0.0 <= score <= 1.0, key
        partial = anomaly_score(cell, {PATTERN: "straight"})
        assert partial is None or 0.0 <= partial <= 1.0


def test_benign_like_vector_in_populated_cell_scores_low() -> None:
    table = _committed()
    checked = 0
    for key, cell in sorted(table.cells.items()):
        if key[2] != ALL_WEEKDAYS or len(cell.numeric) < len(NUMERIC_FEATURES):
            continue
        if not cell.transit_pattern:
            continue
        score = anomaly_score(cell, _benign_like(cell))
        assert score is not None and score < 0.5, (key, score)
        checked += 1
    assert checked >= 100


def test_fast_deep_orbit_at_0200_scores_high() -> None:
    table = _committed()
    scored = 0
    for zone_id in ZONES:
        for weekday in F.WEEKDAYS:
            score = table.anomaly(zone_id, 2, weekday, FAST_DEEP_ORBIT)
            if score is None:
                continue
            assert score >= 0.85, (zone_id, weekday, score)
            scored += 1
    assert scored >= len(ZONES) * len(F.WEEKDAYS) - 7
    assert table.anomaly("fuel_berths", 2, F.DEFAULT_WEEKDAY, FAST_DEEP_ORBIT) is not None


def test_empty_cell_gives_unknown() -> None:
    table = _committed()
    assert table.anomaly("no_such_zone", 2, "sat", FAST_DEEP_ORBIT) is None
    assert table.density("no_such_zone", 2, "sat") == 0.0
    empty = PolCell(
        n_contacts=0,
        n_characterized=0,
        density=0.0,
        hypothesis_mix={},
        numeric={},
        transit_pattern={},
    )
    assert anomaly_score(empty, FAST_DEEP_ORBIT) is None
    with pytest.raises(ValueError):
        table.anomaly("fuel_berths", 2, "someday", FAST_DEEP_ORBIT)

    # Through build_evidence: an empty table gives unknown with source pol_cell_empty.
    scenario = resolve_scenario(catalog_paths()[4], HARBOR)  # S05
    run = run_scenario(scenario, seed=1, ticks=120, harbor=HARBOR)
    field = load_current_field()
    evidence = F.build_evidence(
        run,
        HARBOR,
        field,
        PolTable(seed=POL_SEED, days=POL_DAYS, cells={}),
        hour_of_day=scenario.hour_of_day,
        tide_phase_for_hour=lambda hour: tide_phase_for_hour(hour, field),
    )
    for contact_id in evidence.contacts():
        row = evidence.latest(contact_id, FeatureName.PATTERN_OF_LIFE_ANOMALY)
        assert row is not None and row.value == UNKNOWN and row.source == F.POL_CELL_EMPTY


def test_thin_weekday_cell_falls_back_to_pooled() -> None:
    def cell(n: int, speed_mean: float) -> PolCell:
        return PolCell(
            n_contacts=n,
            n_characterized=n,
            density=n / 4.0,
            hypothesis_mix={
                h.value: (1.0 if i == 0 else 0.0) for i, h in enumerate(BENIGN_HYPOTHESES)
            },
            numeric={SPEED: FeatureStats(n=n, mean=speed_mean, std=0.1)},
            transit_pattern={},
        )

    thin = cell(MIN_CELL_CONTACTS - 1, 5.0)
    full = cell(MIN_CELL_CONTACTS, 5.0)
    pooled = cell(40, 0.5)
    table = PolTable(
        seed=1,
        days=28,
        cells={("z", 3, "sat"): thin, ("z", 3, "sun"): full, ("z", 3, ALL_WEEKDAYS): pooled},
    )
    assert table.baseline_cell("z", 3, "sat") is pooled
    assert table.baseline_cell("z", 3, "sun") is full
    assert table.baseline_cell("z", 3, "mon") is pooled
    assert table.anomaly("z", 3, "sat", {SPEED: 0.5}) == 0.0
    assert table.anomaly("z", 3, "sun", {SPEED: 5.0}) == 0.0


def test_anomaly_formula() -> None:
    cell = PolCell(
        n_contacts=10,
        n_characterized=10,
        density=2.5,
        hypothesis_mix={h.value: 0.25 for h in BENIGN_HYPOTHESES},
        numeric={
            SPEED: FeatureStats(n=10, mean=1.0, std=0.5),
            DEPTH: FeatureStats(n=10, mean=2.0, std=0.0),  # floored to 1.5
        },
        transit_pattern={"straight": 0.6, "random": 0.3, "orbit": 0.1, "lawnmower": 0.0},
    )
    observed: dict[str, EvidenceValue] = {SPEED: 2.0, DEPTH: 5.0, DWELL: 99.0, PATTERN: "random"}
    expected = (
        math.erf(2.0 / math.sqrt(2.0)) + math.erf(2.0 / math.sqrt(2.0)) + (1.0 - 0.3 / 0.6)
    ) / 3.0  # dwell has no stats in this cell, so it is skipped
    assert anomaly_score(cell, observed) == pytest.approx(expected, abs=1e-6)
    assert anomaly_score(cell, {PATTERN: "straight"}) == 0.0
    assert anomaly_score(cell, {}) is None


# --- the month reads the evidence table's feature definitions ---------------------------


@pytest.mark.parametrize("path", catalog_paths(), ids=lambda p: p.name[:3])
def test_pol_observation_matches_evidence_rows(path: object) -> None:
    scenario = resolve_scenario(path, HARBOR)  # type: ignore[arg-type]
    run = run_scenario(scenario, seed=1, harbor=HARBOR)
    field = load_current_field()
    table = F.build_evidence(
        run,
        HARBOR,
        field,
        None,
        hour_of_day=scenario.hour_of_day,
        tide_phase_for_hour=lambda hour: tide_phase_for_hour(hour, field),
    )
    for contact_id in table.contacts():
        obs = F.pol_observation(run.tracks[contact_id], HARBOR, hour_of_day=scenario.hour_of_day)
        zone = table.latest(contact_id, FeatureName.ZONE_ID)
        hour = table.latest(contact_id, FeatureName.HOUR_OF_DAY)
        assert zone is not None and hour is not None
        assert obs.zone_id == (None if zone.value == UNKNOWN else zone.value)
        assert obs.hour == hour.value
        rows = {f.value: table.latest(contact_id, f) for f in F.POL_INPUT_FEATURES}
        known = {k: r.value for k, r in rows.items() if r is not None and r.value != UNKNOWN}
        assert obs.values == known, contact_id
