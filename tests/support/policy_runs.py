"""Catalog baseline runs shared by the WO-050 tests, computed once per test process."""

from __future__ import annotations

from functools import lru_cache

from soundings.eval.policies import PolicyName, PolicyRun, make_policy
from soundings.schemas import Scenario
from soundings.sim.scenario import catalog_paths, resolve_scenario

SEEDS: tuple[int, ...] = (1, 2, 3, 4, 5)
BASELINES: tuple[PolicyName, ...] = (PolicyName.BANG, PolicyName.HUMAN_QUEUE)


@lru_cache(maxsize=1)
def catalog() -> tuple[Scenario, ...]:
    return tuple(resolve_scenario(p) for p in catalog_paths())


@lru_cache(maxsize=1)
def catalog_runs() -> dict[tuple[str, int, PolicyName], PolicyRun]:
    """(scenario id, seed, policy) -> PolicyRun for S01..S08 x seeds 1..5 x both baselines."""
    policies = {name: make_policy(name) for name in BASELINES}
    out: dict[tuple[str, int, PolicyName], PolicyRun] = {}
    for sc in catalog():
        for seed in SEEDS:
            for name in BASELINES:
                out[(sc.id, seed, name)] = policies[name].run(sc, seed)
    return out
