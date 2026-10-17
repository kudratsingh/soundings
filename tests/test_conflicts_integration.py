"""WO-081 integration tests: conflicts over real scenarios, filing time, determinism.

Owner directive (verbatim): "40-plan BT-01 filing runs < 200 ms".
``test_bt01_filing_under_200ms`` proves it. Until WO-080 part A puts
``scenarios/BT-01.yaml`` on main, ``tests.support.busy_plans.bt01_or_standin`` returns a
seeded, equivalent 40-plan day; the switch to the real BT-01 is automatic.

Hard rule exercised: CLAUDE.md 2.4 by ``-k deterministic`` (two in-process seedings and
two subprocesses under PYTHONHASHSEED 0 and 1 give byte-identical canonical JSON).
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import pytest

from soundings.canonical import canonical_json
from soundings.registry.conflicts import conflicts_among, footprint_of
from soundings.registry.models import (
    PlanCreate,
    PlanStatus,
    load_conflict_config,
    scenario_epoch,
)
from soundings.registry.store import (
    MEMORY,
    RegistryStore,
    plan_views_from_scenario,
    plans_from_scenario,
)
from soundings.runner import RunResult, ScenarioRunner
from soundings.schemas import LadderState, load_harbor
from soundings.schemas._base import REPO_ROOT
from soundings.schemas.scenario import Scenario
from tests.support.busy_plans import bt01_available, bt01_or_standin

HARBOR = load_harbor()
CFG = load_conflict_config()
OFF_CFG = CFG.model_copy(update={"area_buffer_m": -10_000.0})  # shrinks every zone to empty
BT01_SKIP = pytest.mark.skipif(
    not bt01_available(),
    reason="scenarios/BT-01.yaml lands with WO-080 part A; the BT-01 runner check waits for it",
)


def _no_clock() -> datetime:
    raise AssertionError("seeding a scenario must not read the clock")


def _store(db: Path | str, cfg=CFG) -> RegistryStore:  # type: ignore[no-untyped-def]
    return RegistryStore(db, harbor=HARBOR, now=_no_clock, conflict_config=cfg)


def _states(result: RunResult) -> list[tuple[str, str]]:
    return [(c.case_id, c.state.value) for c in result.cases]


# --- conflicted plans stay in force for the ask rung -------------------------------------


def test_conflicted_plan_still_clears(tmp_path: Path) -> None:
    baseline = ScenarioRunner("S01").run()
    scenario = ScenarioRunner("S01").scenario
    store = _store(tmp_path / "s01.db")
    try:
        (s01_plan,) = store.seed_from_scenario(scenario)
        assert s01_plan.status is PlanStatus.ACTIVE
        epoch = scenario_epoch(scenario)
        other = store.create(
            {
                "plan_id": "P-OTHER-MODEM",
                "operator": "Harbor Survey Co (demo)",
                "vehicle": "ROV-Blue-2",
                "modem_id": "MOD-3399",
                "area": s01_plan.area,
                "start_tick": s01_plan.start_tick,
                "end_tick": s01_plan.end_tick,
                "contact_channel": "radio ch 16",
                "source": "scenario",
                "created_at": epoch,
            },
            epoch=epoch,
            tick_s=scenario.tick_s,
        )
        assert other.status is PlanStatus.CONFLICTED
        after = store.get(s01_plan.plan_id)
        assert after is not None and after.status is PlanStatus.CONFLICTED
        assert [c.other_plan_id for c in after.conflicts] == ["P-OTHER-MODEM"]
        result = ScenarioRunner(scenario, plans=store).run()
    finally:
        store.close()
    filed = [c for c in result.cases if c.state is LadderState.CLEARED_AT_ASK]
    assert filed, "the S01 contact must still clear at ask"
    assert result.interruptions == 0
    assert _states(result) == _states(baseline)


@BT01_SKIP
def test_bt01_cases_unchanged_by_conflict_detection(tmp_path: Path) -> None:
    scenario = bt01_or_standin()
    results = []
    for name, cfg in (("on", CFG), ("off", OFF_CFG)):
        store = _store(tmp_path / f"bt01-{name}.db", cfg)
        try:
            store.seed_from_scenario(scenario)
            results.append(ScenarioRunner(scenario, plans=store).run())
        finally:
            store.close()
    assert _states(results[0]) == _states(results[1])


def test_detection_off_config_finds_nothing() -> None:
    views = plan_views_from_scenario(bt01_or_standin(), conflict_config=OFF_CFG)
    assert all(v.status is PlanStatus.ACTIVE and v.conflicts == () for v in views)


# --- the directive's filing-time clause --------------------------------------------------


def _bt01_plans() -> tuple[Scenario, list[PlanCreate]]:
    scenario = bt01_or_standin()
    plans = plans_from_scenario(scenario)
    assert len(plans) == 40
    return scenario, plans


def test_bt01_filing_under_200ms() -> None:
    scenario, plans = _bt01_plans()
    epoch = scenario_epoch(scenario)
    timings = []
    for _ in range(3):
        store = _store(MEMORY)  # construction excluded from the timing
        try:
            t0 = time.perf_counter()
            for plan in plans:
                store.create(plan, epoch=epoch, tick_s=scenario.tick_s)
            timings.append(time.perf_counter() - t0)
            assert store.count() == 40
        finally:
            store.close()
    assert min(timings) < 0.200, f"best of 3 filing passes took {min(timings):.3f} s"

    views = plan_views_from_scenario(scenario, conflict_config=OFF_CFG)
    fps = [footprint_of(v, CFG) for v in views]
    pure = []
    for _ in range(3):
        t0 = time.perf_counter()
        conflicts_among(fps, HARBOR, CFG)
        pure.append(time.perf_counter() - t0)
    assert min(pure) < 0.050, f"best of 3 conflicts_among passes took {min(pure):.3f} s"


# --- determinism (CLAUDE.md 2.4) ---------------------------------------------------------


def _seed_dump(db: Path | str) -> str:
    store = _store(db)
    try:
        store.seed_from_scenario(bt01_or_standin())
        return canonical_json([p.model_dump(mode="json") for p in store.list("all")])
    finally:
        store.close()


_SUBPROCESS_SCRIPT = """
import sys
from soundings.canonical import canonical_json
from soundings.registry.store import RegistryStore
from tests.support.busy_plans import bt01_or_standin

def no_clock():
    raise AssertionError("clock read")

store = RegistryStore(sys.argv[1], now=no_clock)
store.seed_from_scenario(bt01_or_standin())
print(canonical_json([v.model_dump(mode="json") for v in store.list("all")]))
"""


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def test_deterministic_seed_with_conflicts(tmp_path: Path) -> None:
    first = _seed_dump(tmp_path / "a.db")
    second = _seed_dump(MEMORY)
    assert _sha(first) == _sha(second)
    assert '"conflicted"' in first and '"overlap_minutes"' in first
    hashes = []
    for seed in ("0", "1"):
        env = {**os.environ, "PYTHONHASHSEED": seed, "MODEL_MODE": "replay"}
        env.pop("SOUNDINGS_DB", None)
        proc = subprocess.run(
            [sys.executable, "-c", _SUBPROCESS_SCRIPT, str(tmp_path / f"h{seed}.db")],
            cwd=REPO_ROOT,
            env=env,
            capture_output=True,
            text=True,
            check=True,
            timeout=120,
        )
        hashes.append(_sha(proc.stdout.strip()))
    assert hashes[0] == hashes[1] == _sha(first)


def test_deterministic_views_equal_store_readback(tmp_path: Path) -> None:
    scenario = bt01_or_standin()
    store = _store(tmp_path / "v.db")
    try:
        store.seed_from_scenario(scenario)
        read_back = sorted(store.list("all"), key=lambda v: (v.start_tick or 0, v.plan_id))
    finally:
        store.close()
    views = plan_views_from_scenario(scenario)
    assert canonical_json([v.model_dump(mode="json") for v in views]) == canonical_json(
        [v.model_dump(mode="json") for v in read_back]
    )
    assert any(v.status is PlanStatus.CONFLICTED for v in views)


def test_deterministic_conflict_lists_two_runs() -> None:
    scenario = bt01_or_standin()
    fps = [footprint_of(v, CFG) for v in plan_views_from_scenario(scenario)]
    a = conflicts_among(fps, HARBOR, CFG)
    b = conflicts_among(list(fps), HARBOR, CFG)
    assert canonical_json([c.model_dump(mode="json") for c in a]) == canonical_json(
        [c.model_dump(mode="json") for c in b]
    )


def test_recompute_and_resolution_round_trip(tmp_path: Path) -> None:
    store = _store(tmp_path / "r.db")
    try:
        store.seed_from_scenario(bt01_or_standin())
        conflicted = store.list("conflicted")
        assert conflicted
        pid = conflicted[0].plan_id
        partners = [c.other_plan_id for c in store.conflicts_of(pid)]
        before = canonical_json([v.model_dump(mode="json") for v in store.list("all")])
        store.recompute_conflicts([pid, *partners])
        after = canonical_json([v.model_dump(mode="json") for v in store.list("all")])
        assert before == after  # nothing moved, so nothing changes
        # resolve every conflict of the whole store: everything returns to active
        everyone = [v.plan_id for v in store.list("all")]
        store.set_conflict_resolution(everyone, "PROP-TEST-1")
        assert store.list("conflicted") == []
        assert all(c.resolved_by == "PROP-TEST-1" for v in store.list("all") for c in v.conflicts)
    finally:
        store.close()


def test_runner_default_plans_include_conflicted() -> None:
    # plan_views_from_scenario marks overlapping plans conflicted; the runner still reads them
    views = plan_views_from_scenario(bt01_or_standin())
    assert {v.status for v in views} <= {PlanStatus.ACTIVE, PlanStatus.CONFLICTED}


def test_bt01_standin_switches_automatically() -> None:
    scenario = bt01_or_standin()
    assert scenario.id == "BT-01"
    assert len(scenario.registry_plans) == 40
    if not bt01_available():
        assert not (REPO_ROOT / "scenarios" / "BT-01.yaml").exists()
