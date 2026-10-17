"""WO-020 integration tests over the real scenario catalog and harbor map on disk.

Scenario files with a ``registry_plans`` block are discovered at collection time (S02 on
``main`` today; S01 and S03 join automatically when WO-011 merges).

Hard rule exercised: CLAUDE.md 2.4 (determinism) by ``-k deterministic_seed``: seeded plan
ids, ``created_at`` and ordering never depend on the wall clock or the hash seed.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from soundings.api import app
from soundings.canonical import canonical_json
from soundings.registry.models import PlanRejected, PlanSource, PlanStatus, RejectReason
from soundings.registry.router import get_store
from soundings.registry.store import RegistryStore, plan_views_from_scenario, plans_from_scenario
from soundings.schemas import load_harbor
from soundings.schemas._base import REPO_ROOT
from soundings.schemas.scenario import Scenario, load_scenario

SCENARIO_DIR = REPO_ROOT / "scenarios"
S02_PATH = SCENARIO_DIR / "S02-filed-vehicle-gone-silent.yaml"
HARBOR = load_harbor()
PDT = timezone(timedelta(hours=-7))


def _wall_clock_forbidden() -> datetime:
    raise AssertionError("seeding a scenario must not read the clock")


def plan_bearing_paths() -> list[Path]:
    out = []
    for path in sorted(SCENARIO_DIR.glob("S*.yaml")):
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and data.get("registry_plans"):
            out.append(path)
    return out


PLAN_BEARING = plan_bearing_paths()


def _store(path: Path) -> RegistryStore:
    return RegistryStore(path, harbor=HARBOR, now=_wall_clock_forbidden)


@pytest.fixture
def api_store(tmp_path: Path) -> Iterator[RegistryStore]:
    store = RegistryStore(
        tmp_path / "int.db", harbor=HARBOR, now=lambda: datetime(2026, 10, 17, tzinfo=UTC)
    )
    app.dependency_overrides[get_store] = lambda: store
    try:
        yield store
    finally:
        app.dependency_overrides.pop(get_store, None)
        store.close()


def test_s02_scenario_rest_sqlite_hot_path(api_store: RegistryStore) -> None:
    scenario = load_scenario(S02_PATH, HARBOR)
    client = TestClient(app)
    for plan in plans_from_scenario(scenario):
        resp = client.post("/api/plans", json=plan.model_dump(mode="json"))
        assert resp.status_code == 201, resp.text
    hits = api_store.overlapping_ticks(450, 450)
    assert [p.plan_id for p in hits] == ["P-2026-1017-003"]
    plan = hits[0]
    assert plan.modem_id == "MOD-4471"
    assert plan.area == "inner_basin_research"
    assert plan.start_at == datetime(2026, 10, 17, 14, 0, tzinfo=PDT)
    assert plan.start_at.isoformat() == "2026-10-17T14:00:00-07:00"
    assert plan.end_at == datetime(2026, 10, 17, 14, 30, tzinfo=PDT)  # 900 ticks x 2 s
    assert (plan.start_tick, plan.end_tick) == (0, 900)
    assert plan.contact_channel == "sim-phone-operator"
    assert plan.source is PlanSource.SCENARIO
    assert plan.created_at == datetime(2026, 10, 17, 14, 0, tzinfo=PDT)
    # The REST round trip and the direct, database-free view agree.
    assert hits == [v for v in plan_views_from_scenario(scenario) if v.plan_id == plan.plan_id]


def test_s02_is_plan_bearing() -> None:
    assert S02_PATH in PLAN_BEARING


@pytest.mark.parametrize("path", PLAN_BEARING, ids=[p.stem.split("-")[0] for p in PLAN_BEARING])
def test_catalog_seed_and_by_modem(tmp_path: Path, path: Path) -> None:
    scenario = load_scenario(path, HARBOR)
    store = _store(tmp_path / "cat.db")
    try:
        seeded = store.seed_from_scenario(scenario)
        assert [p.plan_id for p in seeded] == [p.plan_id for p in scenario.registry_plans]
        assert store.count() == len(scenario.registry_plans)
        zone_ids = {z.zone_id for z in HARBOR.zones}
        for rp in scenario.registry_plans:
            assert rp.area in zone_ids
            found = store.by_modem(rp.modem_id)
            assert rp.plan_id in [p.plan_id for p in found]
            view = store.get(rp.plan_id)
            assert view is not None and view.status is PlanStatus.ACTIVE
            assert (view.start_tick, view.end_tick) == (rp.window.start_tick, rp.window.end_tick)
        # Re-seeding is refused unless asked to skip what is already there.
        with pytest.raises(PlanRejected) as info:
            store.seed_from_scenario(scenario)
        assert info.value.reason is RejectReason.DUPLICATE_PLAN_ID
        assert store.seed_from_scenario(scenario, skip_existing=True) == seeded
    finally:
        store.close()


def _seed_dump(scenarios: list[Scenario], db: Path) -> str:
    store = _store(db)
    try:
        for scenario in scenarios:
            store.seed_from_scenario(scenario)
        return canonical_json([p.model_dump(mode="json") for p in store.list()])
    finally:
        store.close()


_SUBPROCESS_SCRIPT = """
import sys
from pathlib import Path
from soundings.canonical import canonical_json
from soundings.registry.store import RegistryStore
from soundings.schemas.scenario import load_scenario

def no_clock():
    raise AssertionError("clock read")

store = RegistryStore(sys.argv[1], now=no_clock)
for p in sys.argv[2:]:
    store.seed_from_scenario(load_scenario(Path(p)))
print(canonical_json([v.model_dump(mode="json") for v in store.list()]))
"""


def test_deterministic_seed(tmp_path: Path) -> None:
    scenarios = [load_scenario(p, HARBOR) for p in PLAN_BEARING]
    first = _seed_dump(scenarios, tmp_path / "a.db")
    second = _seed_dump(scenarios, tmp_path / "b.db")
    assert first == second
    assert "P-2026-1017-003" in first

    outputs = []
    for seed in ("0", "1"):
        env = {**os.environ, "PYTHONHASHSEED": seed, "MODEL_MODE": "replay"}
        env.pop("SOUNDINGS_DB", None)
        proc = subprocess.run(
            [
                sys.executable,
                "-c",
                _SUBPROCESS_SCRIPT,
                str(tmp_path / f"h{seed}.db"),
                *map(str, PLAN_BEARING),
            ],
            cwd=REPO_ROOT,
            env=env,
            capture_output=True,
            text=True,
            check=True,
            timeout=120,
        )
        outputs.append(proc.stdout.strip())
    assert outputs[0] == outputs[1] == first
