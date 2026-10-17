"""Integration: the evidence table built from real WO-011 runs of every catalog scenario.

Parametrized over ``catalog_paths()`` x seeds 1, 2, 3. The table is built by
``build_evidence`` from ``SimRun.tracks`` and ``tick_s`` only; this test file may read
the run's truth labels, but only to pick which observed contact a spot check looks at.
"""

from __future__ import annotations

from functools import cache
from pathlib import Path

import pytest

from soundings.investigation import features as F
from soundings.investigation.__main__ import main as cli_main
from soundings.investigation.evidence import EvidenceTable
from soundings.investigation.pol import PolTable, load_pol_table
from soundings.schemas.config import load_likelihood_spec
from soundings.schemas.enums import EvidenceKind, FeatureName, Unit
from soundings.schemas.evidence import UNKNOWN, EvidenceRow
from soundings.schemas.harbor import Harbor, load_harbor
from soundings.schemas.scenario import Scenario
from soundings.sim.current import CurrentField, load_current_field, tide_phase_for_hour
from soundings.sim.run import run_scenario, truncate_run
from soundings.sim.scenario import catalog_paths, resolve_scenario
from soundings.sim.types import SimRun

SEEDS = (1, 2, 3)
CATALOG = catalog_paths()
CASES = [pytest.param(p, s, id=f"{p.name[:3]}-seed{s}") for p in CATALOG for s in SEEDS]
SPEC_COLUMNS = ("contact_id", "t", "feature", "value", "unit", "source", "confidence", "kind")
ASSUMPTION_FEATURES = frozenset(
    {
        FeatureName.CURRENT_FIELD,
        FeatureName.WEEKDAY,
        FeatureName.POL_BASELINE,
        FeatureName.VISIBILITY_FOR_CAMERA,
    }
)
S05_DECISION_TICKS = 180
"""S05 spot check tick: the contact is inside 300 s of fuel_berth_1 from tick 109 and
reaches the fuel_berths zone near tick 185; after arriving at its berth it sits still."""


@cache
def _harbor() -> Harbor:
    return load_harbor()


@cache
def _field() -> CurrentField:
    return load_current_field()


@cache
def _pol() -> PolTable:
    return load_pol_table()


@cache
def _scenario(path: Path) -> Scenario:
    return resolve_scenario(path, _harbor())


@cache
def _run(path: Path, seed: int, ticks: int | None = None) -> SimRun:
    run = run_scenario(_scenario(path), seed=seed, harbor=_harbor())
    return run if ticks is None else truncate_run(run, ticks)


def _build(run: SimRun, scenario: Scenario) -> EvidenceTable:
    field = _field()
    return F.build_evidence(
        run,
        _harbor(),
        field,
        _pol(),
        hour_of_day=scenario.hour_of_day,
        tide_phase_for_hour=lambda hour: tide_phase_for_hour(hour, field),
    )


@cache
def _table(path: Path, seed: int, ticks: int | None = None) -> EvidenceTable:
    return _build(_run(path, seed, ticks), _scenario(path))


def _path(scenario_id: str) -> Path:
    (match,) = [p for p in CATALOG if p.name.startswith(f"{scenario_id}-")]
    return match


def _main_contact(path: Path, seed: int, ticks: int | None = None) -> str:
    """The longest observed track of the scenario's own contact (truth used to pick only)."""
    run = _run(path, seed, ticks)
    own = {c.id for c in _scenario(path).contacts}
    ids = [cid for cid, truth in run.contact_truth.items() if truth.gt_id in own]
    return max(sorted(ids), key=lambda cid: len(run.tracks[cid].points))


def _latest(table: EvidenceTable, contact_id: str, feature: FeatureName) -> EvidenceRow:
    row = table.latest(contact_id, feature)
    assert row is not None, (contact_id, feature.value)
    return row


def test_catalog_has_exactly_eight_scenarios() -> None:
    assert [p.name[:3] for p in CATALOG] == [f"S0{i}" for i in range(1, 9)]


# --- completeness ---------------------------------------------------------------------


@pytest.mark.parametrize(("path", "seed"), CASES)
def test_completeness_every_catalog_feature_every_column(path: Path, seed: int) -> None:
    table = _table(path, seed)
    run = _run(path, seed)
    assert run.tracks, "a catalog run with no observed contact proves nothing"
    assert table.contacts() == tuple(sorted(run.tracks))
    for contact_id in table.contacts():
        rows = table.for_contact(contact_id)
        present = {r.feature for r in rows}
        missing = [f.value for f in F.FEATURE_CATALOG if f not in present]
        assert not missing, (contact_id, missing)
        for row in rows:
            dumped = row.model_dump()
            for column in SPEC_COLUMNS:
                assert dumped[column] is not None, (contact_id, row.feature, column)
            assert dumped["probe_id"] is None  # no probes run in Phase 1
            assert row.unit is not Unit.UNKNOWN
            if row.value == UNKNOWN:
                assert row.confidence == 0.0, (contact_id, row.feature)
                assert row.source in F.UNKNOWN_SOURCES, (contact_id, row.feature, row.source)


# --- facts and assumptions ------------------------------------------------------------


@pytest.mark.parametrize(("path", "seed"), CASES)
def test_disjoint_facts_and_assumptions(path: Path, seed: int) -> None:
    table = _table(path, seed)
    for contact_id in table.contacts():
        rows = table.for_contact(contact_id)
        facts = {r.feature for r in rows if r.kind is EvidenceKind.FACT}
        assumptions = {r.feature for r in rows if r.kind is EvidenceKind.ASSUMPTION}
        assert not facts & assumptions, (contact_id, facts & assumptions)
        assert facts == table.facts(contact_id)
        assert assumptions == table.assumptions(contact_id) == ASSUMPTION_FEATURES
        for row in rows:
            if row.kind is EvidenceKind.ASSUMPTION:
                assert row.assumes.strip(), (contact_id, row.feature)
            else:
                assert row.assumes == ""


# --- provenance -----------------------------------------------------------------------


@pytest.mark.parametrize(("path", "seed"), CASES)
def test_provenance_derived_rows_cite_earlier_rows(path: Path, seed: int) -> None:
    table = _table(path, seed)
    seen: dict[str, EvidenceRow] = {}
    for row in table.rows():
        if row.kind is EvidenceKind.DERIVED:
            assert row.derived_from, row.feature
            sources = [seen[s] for s in row.derived_from]  # KeyError: not earlier
            ceiling = min(float(s.confidence) for s in sources)
            assert float(row.confidence) <= ceiling + 1e-9, (row.contact_id, row.feature)
            assert all(s.contact_id == row.contact_id for s in sources)
        seen[row.row_id] = row
    for contact_id in table.contacts():
        current = _latest(table, contact_id, FeatureName.CURRENT_FIELD)
        assert current.assumes == "current field is the configured default; no live gauge"
        drift = _latest(table, contact_id, FeatureName.DRIFT_CONSISTENCY)
        assert current.row_id in drift.derived_from
        pol_row = _latest(table, contact_id, FeatureName.POL_BASELINE)
        assert pol_row.assumes == F.pol_assumes(_pol().seed)
        anomaly = _latest(table, contact_id, FeatureName.PATTERN_OF_LIFE_ANOMALY)
        assert anomaly.kind is EvidenceKind.DERIVED
        if anomaly.value != UNKNOWN:
            assert pol_row.row_id in anomaly.derived_from
            assert anomaly.source == "extractor:anomaly_score"


# --- buckets --------------------------------------------------------------------------


@pytest.mark.parametrize(("path", "seed"), CASES)
def test_buckets_land_in_likelihood_names(path: Path, seed: int) -> None:
    spec = load_likelihood_spec()
    table = _table(path, seed)
    for row in table.rows():
        if row.feature not in spec.features:
            continue
        bucket = F.bucketize(row.feature.value, row.value)
        if row.value == UNKNOWN:
            assert bucket is None
        else:
            names = spec.features[row.feature].buckets
            assert bucket in names, (row.contact_id, row.feature.value, row.value, bucket)


# --- signatures -----------------------------------------------------------------------


def _seeds_for(scenario_id: str) -> list[int]:
    return [*SEEDS, _scenario(_path(scenario_id)).seed]


@pytest.mark.parametrize("seed", _seeds_for("S08"))
def test_signatures_s08_drift_consistency_is_high(seed: int) -> None:
    path = _path("S08")
    table = _table(path, seed)
    cid = _main_contact(path, seed)
    value = _latest(table, cid, FeatureName.DRIFT_CONSISTENCY).value
    assert F.bucketize("drift_consistency", value) == "high", (cid, value)


@pytest.mark.parametrize("seed", _seeds_for("S05"))
def test_signatures_s05_closing_and_straight(seed: int) -> None:
    path = _path("S05")
    table = _table(path, seed, S05_DECISION_TICKS)
    cid = _main_contact(path, seed, S05_DECISION_TICKS)
    closing = _latest(table, cid, FeatureName.CLOSING_RATE_TO_NEAREST_CRITICAL_ASSET).value
    assert isinstance(closing, float) and closing > 0.0, (cid, closing)
    assert _latest(table, cid, FeatureName.TRANSIT_PATTERN).value == "straight", cid
    time_to = _latest(table, cid, FeatureName.TIME_TO_NEAREST_CRITICAL_ASSET).value
    assert isinstance(time_to, float) and time_to < F.NOT_CLOSING_S


@pytest.mark.parametrize("seed", _seeds_for("S06"))
def test_signatures_s06_orbit(seed: int) -> None:
    path = _path("S06")
    cid = _main_contact(path, seed)
    assert _latest(_table(path, seed), cid, FeatureName.TRANSIT_PATTERN).value == "orbit", cid


@pytest.mark.parametrize("seed", _seeds_for("S07"))
def test_signatures_s07_breathing_or_click_true_row(seed: int) -> None:
    path = _path("S07")
    table = _table(path, seed)
    cid = _main_contact(path, seed)
    rows = table.feature_rows(cid, FeatureName.BREATHING_OR_CLICK_PRESENT)
    assert any(r.value is True for r in rows), cid
    assert all(r.source.startswith(F.ACOUSTIC_SOURCE_PREFIX) for r in rows)


# --- determinism ----------------------------------------------------------------------


def test_determinism_s05_seed11_table_hash_twice() -> None:
    path = _path("S05")
    scenario = _scenario(path)
    first = _build(run_scenario(scenario, seed=11, harbor=_harbor()), scenario)
    second = _build(run_scenario(scenario, seed=11, harbor=_harbor()), scenario)
    assert first.to_json() == second.to_json()
    assert first.table_hash() == second.table_hash()
    assert EvidenceTable.from_json(first.to_json()).table_hash() == first.table_hash()


def test_cli_prints_the_same_bytes_twice(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli_main(["--scenario", "S05", "--seed", "11"]) == 0
    first = capsys.readouterr().out
    assert cli_main(["--scenario", "S05", "--seed", "11"]) == 0
    second = capsys.readouterr().out
    assert first == second
    table = EvidenceTable.from_json(first)
    scenario = _scenario(_path("S05"))
    expected = _build(run_scenario(scenario, seed=11, harbor=_harbor()), scenario)
    assert table.table_hash() == expected.table_hash()


def test_cli_unknown_scenario_exits_2(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli_main(["--scenario", "S99"]) == 2
    assert "S99" in capsys.readouterr().err
