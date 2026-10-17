"""WO-022 runner v1 over the catalog: end states, re-acquisition, hails, registry, determinism.

Exit tests: ``-k catalog``, ``-k reacquire_sweep``, ``-k hail_rows_append``,
``-k registry_catalog``, ``-k registry_db``, ``-k deterministic``.
Ground truth is read here for grading only.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from soundings.probe_keys import idempotency_key, tick_bucket
from soundings.registry.store import RegistryStore
from soundings.runner import (
    ASK_TRANSITIONS,
    V1_END_STATES,
    ScenarioRunner,
    main,
    write_transition_log,
)
from soundings.schemas import LadderState, ProbeId, load_harbor
from soundings.schemas._base import REPO_ROOT
from tests.runner_runs import CATALOG, SWEEP_SEEDS, gt_of, is_false_alarm, run

HARBOR = load_harbor()
FIXED_NOW = datetime(2026, 10, 17, 12, 0, tzinfo=UTC)


def _store(path: Path) -> RegistryStore:
    return RegistryStore(path, harbor=HARBOR, now=lambda: FIXED_NOW)


@pytest.mark.parametrize("scenario_id", CATALOG)
def test_catalog_end_states(scenario_id: str) -> None:
    result = run(scenario_id)
    assert result.cases
    for case in result.cases:
        assert case.state in V1_END_STATES, (case.case_id, case.state, case.failure)
        assert case.investigation_pending == (case.state is LadderState.INVESTIGATING)
        assert case.outcome is not None
        assert len(set(case.hail_keys)) <= 2
    for t in result.transitions:
        assert (t.from_state, t.event, t.to_state) in ASK_TRANSITIONS
    assert [t.seq for t in result.transitions] == list(range(len(result.transitions)))
    assert result.interruptions == 0


def test_catalog_s05_is_never_cleared_at_ask() -> None:
    result = run("S05")
    gt_cases = [c for c in result.cases if gt_of(result, c.contact_id) == "gt-1"]
    assert gt_cases
    for case in gt_cases:
        assert case.state is LadderState.INVESTIGATING
        assert case.outcome is not None and case.outcome.code.value == "ESCALATE"


def test_catalog_s04_unfiled_known_operator() -> None:
    result = run("S04")
    gt_cases = [c for c in result.cases if gt_of(result, c.contact_id) == "gt-1"]
    assert gt_cases
    for case in gt_cases:
        assert case.outcome is not None
        assert case.outcome.code.value == "UNFILED_KNOWN_OPERATOR"
        assert case.state is LadderState.INVESTIGATING and case.investigation_pending


def test_catalog_nothing_unfiled_is_cleared() -> None:
    for scenario_id in ("S04", "S05", "S06", "S07", "S08"):
        result = run(scenario_id)
        assert all(c.state is not LadderState.CLEARED_AT_ASK for c in result.cases)
        assert result.notices == []


def test_catalog_false_alarms_never_clear() -> None:
    for scenario_id in CATALOG:
        result = run(scenario_id)
        for case in result.cases:
            if is_false_alarm(result, case.contact_id):
                assert case.state is LadderState.INVESTIGATING


def test_reacquire_sweep_precision() -> None:
    links = 0
    for scenario_id in CATALOG:
        for seed in SWEEP_SEEDS:
            result = run(scenario_id, seed)
            for link in result.reacquisitions:
                links += 1
                assert gt_of(result, link.old_id) == gt_of(result, link.new_id), (
                    scenario_id,
                    seed,
                    link,
                )
                assert result.case(f"{link.new_id}#1").reacquired_from == link.old_id
    assert links >= 1


@pytest.mark.parametrize("scenario_id", ["S02", "S04"])
def test_hail_rows_append(scenario_id: str) -> None:
    result = run(scenario_id)
    assert result.hails
    tick_s = result.sim.tick_s
    for hail in result.hails:
        assert len(hail.row_ids) == 4
        for row_id in hail.row_ids:
            row = result.evidence.get(row_id)
            assert row.probe_id is ProbeId.ACOUSTIC_HAIL and row.t == hail.reply_tick
        expected = idempotency_key(
            hail.contact_id, "acoustic_hail", {}, tick_bucket(hail.sent_tick, tick_s)
        )
        assert hail.idempotency_key == expected
    sent = {h.idempotency_key for h in result.hails if not h.cached}
    assert result.probes_used["acoustic_hail"] == len(sent)


@pytest.mark.parametrize("scenario_id", ["S01", "S02", "S03"])
def test_registry_catalog(tmp_path: Path, scenario_id: str) -> None:
    scenario = ScenarioRunner(scenario_id).scenario
    store = _store(tmp_path / "reg.db")
    try:
        seeded = store.seed_from_scenario(scenario)
        assert [p.plan_id for p in seeded] == [p.plan_id for p in scenario.registry_plans]
        for plan in scenario.registry_plans:
            found = store.by_modem(plan.modem_id)
            assert plan.plan_id in {p.plan_id for p in found}
    finally:
        store.close()


def test_registry_db_same_hash(tmp_path: Path) -> None:
    scenario = ScenarioRunner("S01").scenario
    store = _store(tmp_path / "reg.db")
    try:
        store.seed_from_scenario(scenario)
        from_db = ScenarioRunner(scenario, plans=store).run()
    finally:
        store.close()
    default = run("S01")
    assert from_db.transition_log_hash == default.transition_log_hash
    assert from_db.evidence.table_hash() == default.evidence.table_hash()


def test_registry_db_empty_store_clears_nothing(tmp_path: Path) -> None:
    store = _store(tmp_path / "empty.db")
    try:
        result = ScenarioRunner("S01", plans=store).run()
    finally:
        store.close()
    assert all(c.state is not LadderState.CLEARED_AT_ASK for c in result.cases)


@pytest.mark.parametrize("scenario_id", ["S01", "S02", "S05"])
def test_deterministic_in_process(scenario_id: str) -> None:
    a = ScenarioRunner(scenario_id).run()
    b = ScenarioRunner(scenario_id).run()
    assert a.transition_log_hash == b.transition_log_hash
    assert a.evidence.table_hash() == b.evidence.table_hash()
    assert [n.model_dump() for n in a.notices] == [n.model_dump() for n in b.notices]


def test_deterministic_cli_across_hash_seeds() -> None:
    outputs = []
    for hash_seed in ("0", "1"):
        env = {**os.environ, "PYTHONHASHSEED": hash_seed, "MODEL_MODE": "replay"}
        proc = subprocess.run(
            [sys.executable, "-m", "soundings.runner", "--scenario", "S02", "--seed", "3"],
            cwd=REPO_ROOT,
            env=env,
            capture_output=True,
            text=True,
            check=True,
        )
        outputs.append(proc.stdout)
    assert outputs[0] == outputs[1]
    assert outputs[0].splitlines()[-1] == f"transition_log_hash={run('S02').transition_log_hash}"


def test_transition_log_file_hash(tmp_path: Path) -> None:
    result = run("S02")
    path = write_transition_log(result, tmp_path / "log" / "transitions.jsonl")
    data = path.read_bytes()
    assert hashlib.sha256(data).hexdigest() == result.transition_log_hash
    lines = data.decode().splitlines()
    assert len(lines) == len(result.transitions)
    assert json.loads(lines[0])["seq"] == 0


def test_cli_prints_transitions_then_hash(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out_dir = tmp_path / "S01-1"
    assert main(["--scenario", "S01", "--seed", "1", "--out", str(out_dir)]) == 0
    lines = capsys.readouterr().out.splitlines()
    result = run("S01", 1)
    assert len(lines) == len(result.transitions) + 1
    assert lines[-1] == f"transition_log_hash={result.transition_log_hash}"
    assert "CLEARED_AT_ASK" in "\n".join(lines)
    assert (out_dir / "transitions.jsonl").is_file() and (out_dir / "notices.jsonl").is_file()


def test_cli_plans_db(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    db = tmp_path / "s01.db"
    store = _store(db)
    store.seed_from_scenario(ScenarioRunner("S01").scenario)
    store.close()
    monkeypatch.setenv("SOUNDINGS_DB", str(db))
    assert main(["--scenario", "S01", "--seed", "1", "--plans", "db"]) == 0
    last = capsys.readouterr().out.splitlines()[-1]
    assert last == f"transition_log_hash={run('S01', 1).transition_log_hash}"
