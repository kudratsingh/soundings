"""WO-095: BT-01 filing-window flexibility (the registry's start bounds per job type).

Exit test: ``uv run pytest tests/test_bt01_window_flex.py``.

- every generated plan carries ``earliest_start_tick``/``latest_start_tick`` from
  ``WINDOW_FLEX_MIN``, clipped to the scenario day;
- nothing else in BT-01 moved: with the bounds stripped, a fresh generation is
  byte-identical to the ``scenarios/BT-01.yaml`` of PR #33 (sha256 pinned below);
- the schema rejects bounds that do not bracket the start or that end past the run, and
  every catalog scenario (no bounds) validates unchanged;
- the bounds reach the registry: ``plan_views_from_scenario`` and a seeded store agree.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from soundings.registry.models import scenario_epoch, tick_to_datetime
from soundings.registry.store import RegistryStore, plan_views_from_scenario
from soundings.schemas.enums import JobType
from soundings.schemas.harbor import load_harbor
from soundings.schemas.scenario import PlanWindow, Scenario
from soundings.sim.generate import WINDOW_FLEX_MIN, generate_busy_day, scenario_yaml
from soundings.sim.scenario import catalog_paths, resolve_scenario

HARBOR = load_harbor()
CMD = "python -m soundings.sim.generate --jobs 40 --vessels 3 --seed 7 --out scenarios/BT-01.yaml"
PRE_WO095_BT01_SHA256 = "668d9f13f8d400e8a02bf51f47d10e27e8d4e6f9d73b6c6693f74401bbe1a8c0"
"""``sha256`` of ``scenarios/BT-01.yaml`` as merged in PR #33 (368d00f), before WO-095."""


def _strip_bounds(sc: Scenario) -> Scenario:
    plans = tuple(
        p.model_copy(
            update={
                "window": p.window.model_copy(
                    update={"earliest_start_tick": None, "latest_start_tick": None}
                )
            }
        )
        for p in sc.registry_plans
    )
    return sc.model_copy(update={"registry_plans": plans})


def test_flex_values_declared_per_generated_type() -> None:
    assert set(WINDOW_FLEX_MIN) == {
        JobType.SURVEY,
        JobType.HULL_CLEANING,
        JobType.PILE_INSPECTION,
        JobType.DREDGE,
    }
    assert WINDOW_FLEX_MIN == {
        JobType.SURVEY: 90,
        JobType.HULL_CLEANING: 120,
        JobType.PILE_INSPECTION: 180,
        JobType.DREDGE: 45,
    }


def test_bounds_per_type_clipped_to_the_day() -> None:
    sc = generate_busy_day(seed=7)
    n = sc.duration_ticks
    widened = 0
    for p in sc.registry_plans:
        w = p.window
        assert p.job_type is not None
        flex = int(round(WINDOW_FLEX_MIN[p.job_type] * 60 / sc.tick_s))
        length = w.end_tick - w.start_tick
        assert w.earliest_start_tick == max(w.start_tick - flex, 0), p.plan_id
        assert w.latest_start_tick == min(w.start_tick + flex, n - length), p.plan_id
        assert 0 <= w.earliest_start_tick <= w.start_tick <= w.latest_start_tick
        assert w.latest_start_tick + length <= n
        if w.latest_start_tick > w.start_tick or w.earliest_start_tick < w.start_tick:
            widened += 1
    assert widened == len(sc.registry_plans) == 40


def test_only_the_bounds_changed() -> None:
    """Same seed, same placement: stripping the bounds gives PR #33's BT-01 byte for byte."""
    text = scenario_yaml(_strip_bounds(generate_busy_day(seed=7)), CMD)
    assert hashlib.sha256(text.encode("utf-8")).hexdigest() == PRE_WO095_BT01_SHA256


def test_committed_bt01_carries_the_bounds() -> None:
    sc = resolve_scenario("BT-01")
    assert sc == generate_busy_day(seed=7)
    assert all(p.window.latest_start_tick is not None for p in sc.registry_plans)


def test_schema_rejects_bad_bounds() -> None:
    with pytest.raises(ValidationError, match="earliest_start_tick"):
        PlanWindow(start_tick=10, end_tick=20, earliest_start_tick=11)
    with pytest.raises(ValidationError, match="latest_start_tick"):
        PlanWindow(start_tick=10, end_tick=20, latest_start_tick=9)
    sc = resolve_scenario("BT-01")
    first = sc.registry_plans[0]
    late = first.window.model_copy(update={"latest_start_tick": sc.duration_ticks})
    data = sc.model_dump(mode="json")
    data["registry_plans"][0]["window"] = late.model_dump(mode="json")
    with pytest.raises(ValidationError, match="latest start ends after duration_ticks"):
        Scenario.model_validate_json(json.dumps(data))


def test_catalog_has_no_bounds_and_validates() -> None:
    paths = catalog_paths()
    assert paths
    for path in paths:
        sc = resolve_scenario(path.name[:3])
        for p in sc.registry_plans:
            assert p.window.earliest_start_tick is None and p.window.latest_start_tick is None
        for v in plan_views_from_scenario(sc):
            assert v.earliest_start_at is None and v.latest_start_at is None


def test_bounds_reach_the_registry(tmp_path: Path) -> None:
    sc = resolve_scenario("BT-01")
    epoch = scenario_epoch(sc)
    plans = {p.plan_id: p for p in sc.registry_plans}
    views = plan_views_from_scenario(sc)
    assert len(views) == 40
    for v in views:
        w = plans[v.plan_id].window
        assert w.earliest_start_tick is not None and w.latest_start_tick is not None
        assert v.earliest_start_at == tick_to_datetime(epoch, w.earliest_start_tick, sc.tick_s)
        assert v.latest_start_at == tick_to_datetime(epoch, w.latest_start_tick, sc.tick_s)
    store = RegistryStore(tmp_path / "bt01.db", harbor=HARBOR, now=lambda: epoch)
    try:
        seeded = {v.plan_id: v for v in store.seed_from_scenario(sc)}
    finally:
        store.close()
    assert len(seeded) == 40
    for v in views:
        assert seeded[v.plan_id].earliest_start_at == v.earliest_start_at
        assert seeded[v.plan_id].latest_start_at == v.latest_start_at
