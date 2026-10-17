"""WO-031 integration: every allowlisted probe on real simulator output (S05, S07).

Scenarios load from the YAML on disk; the registry is a real ``RegistryStore`` in a temp
``SOUNDINGS_DB``; the hail is the WO-021 simulator. The probe sweep runs at the main
contact's last tick, at rung ask.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest

from soundings.investigation.evidence import EvidenceTable
from soundings.investigation.features import build_evidence
from soundings.investigation.planner import (
    BudgetLedger,
    DeterministicPlanner,
    PlannerChoice,
    state_from_table,
)
from soundings.investigation.pol import load_pol_table
from soundings.investigation.posterior import (
    load_likelihood_tables,
    load_prior_config,
    score_contact,
)
from soundings.investigation.probes import (
    ALLOWLIST,
    ProbeCache,
    ProbeContext,
    ProbeResult,
    run_probe,
)
from soundings.registry.store import RegistryStore
from soundings.schemas._base import from_file_data
from soundings.schemas.enums import FeatureName, ProbeId, Rung
from soundings.schemas.evidence import EvidenceRow
from soundings.schemas.harbor import load_harbor
from soundings.sim.current import load_current_field
from soundings.sim.run import run_scenario
from soundings.sim.scenario import resolve_scenario
from soundings.sim.types import SimRun

FIXED_NOW = datetime(2026, 10, 17, 9, 0, tzinfo=UTC)
CATALOG = [("S05", 11), ("S07", 17)]


@dataclass
class Sweep:
    contact_id: str
    tick: int
    run: SimRun
    table: EvidenceTable
    results: dict[ProbeId, ProbeResult]
    cache: ProbeCache


def _main_contact(run: SimRun) -> str:
    ids = [c for c, t in run.contact_truth.items() if t.gt_id == "gt-1"]
    return sorted(ids, key=lambda c: (-len(run.tracks[c].points), c))[0]


def _sweep(scenario_id: str, seed: int, db: Path) -> Sweep:
    harbor = load_harbor()
    scenario = resolve_scenario(scenario_id, harbor)
    run = run_scenario(scenario, seed=seed, harbor=harbor)
    field = load_current_field()
    pol = load_pol_table()
    table = build_evidence(
        run,
        harbor,
        field,
        pol,
        hour_of_day=scenario.hour_of_day,
        tide_phase_for_hour=field.phase_for_hour,
    )
    store = RegistryStore(harbor=harbor, now=lambda: FIXED_NOW)  # path from SOUNDINGS_DB
    assert Path(store.db_path) == db
    store.seed_from_scenario(scenario, skip_existing=True)
    cid = _main_contact(run)
    tick = run.tracks[cid].points[-1].tick
    ctx = ProbeContext(
        run=run,
        scenario=scenario,
        harbor=harbor,
        current_field=field,
        pol=pol,
        registry=store,
        tick=tick,
        current_rung=Rung.ASK,
        contact_id=cid,
    )
    cache = ProbeCache()
    try:
        results = {pid: run_probe(pid, ctx, table, cache=cache) for pid in ALLOWLIST}
    finally:
        store.close()
    return Sweep(cid, tick, run, table, results, cache)


@pytest.fixture
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "registry.db"
    monkeypatch.setenv("SOUNDINGS_DB", str(path))
    return path


def _value(sweep: Sweep, probe: ProbeId, feature: FeatureName) -> object:
    rows = [r for r in sweep.results[probe].rows if r.feature is feature]
    assert rows, (probe, feature)
    return rows[-1].value


@pytest.mark.parametrize(("scenario_id", "seed"), CATALOG)
def test_catalog_sweep_rows_are_typed(scenario_id: str, seed: int, db: Path) -> None:
    sweep = _sweep(scenario_id, seed, db)
    assert list(sweep.results) == list(ProbeId)
    for pid, result in sweep.results.items():
        assert not result.cached and sweep.cache.count(pid) == 1
        assert result.rows, pid
        for row in result.rows:
            again = EvidenceRow.model_validate(from_file_data(row.model_dump(mode="json")))
            assert again.row_id == row.row_id
            assert row.probe_id is pid and row.row_id in sweep.table
            assert row.t == result.landed_tick >= sweep.tick
    # the table, including every probe row, still re-validates as a whole
    assert EvidenceTable.from_json(sweep.table.to_json()).table_hash() == sweep.table.table_hash()


def test_catalog_s05_hail_gets_no_reply(db: Path) -> None:
    sweep = _sweep("S05", 11, db)
    assert _value(sweep, ProbeId.ACOUSTIC_HAIL, FeatureName.HAIL_REPLY) in ("none", "timeout")
    assert _value(sweep, ProbeId.REGISTRY_LOOKUP, FeatureName.REGISTRY_MATCH) == "none"


def test_catalog_s07_clicks_heard(db: Path) -> None:
    sweep = _sweep("S07", 17, db)
    value = _value(sweep, ProbeId.ACOUSTIC_FEATURE_READ, FeatureName.BREATHING_OR_CLICK_PRESENT)
    assert value is True


def test_catalog_s05_planner_same_choice_twice(db: Path) -> None:
    sweep = _sweep("S05", 11, db)
    tables, prior_cfg = load_likelihood_tables(), load_prior_config()
    report = score_contact(sweep.table, sweep.contact_id, tables, prior_cfg)
    assert report.posterior.threat_mass() > report.prior.threat_mass()

    def choose(tick: int) -> PlannerChoice:
        ledger = BudgetLedger()
        ledger.record_ask_hail(sweep.contact_id)
        state = state_from_table(
            sweep.table,
            sweep.contact_id,
            tables,
            prior_cfg,
            current_rung=Rung.ASK,
            ledger=ledger,
            tick=tick,
            tick_s=sweep.run.tick_s,
            run_keys=frozenset(sweep.cache.keys()),
        )
        return DeterministicPlanner().choose(state, tables)

    # In the sweep's own bucket every probe has run, so nothing is proposed.
    assert choose(sweep.tick).probe_id is None
    # One bucket later the same table gives the same, non-empty choice twice.
    first, second = choose(sweep.tick + 15), choose(sweep.tick + 15)
    assert first == second and first.model_dump_json() == second.model_dump_json()
    assert first.ranked and first.probe_id is not None
    assert ALLOWLIST[first.probe_id].rung_required in (Rung.WATCH, Rung.ASK)


def test_table_hash_stable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    hashes = []
    for i in range(2):
        path = tmp_path / f"run{i}.db"
        monkeypatch.setenv("SOUNDINGS_DB", str(path))
        hashes.append(_sweep("S05", 11, path).table.table_hash())
    assert hashes[0] == hashes[1]
