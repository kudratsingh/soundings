"""Cached runner v1 results shared by the WO-022 integration tests (one run per key)."""

from __future__ import annotations

from functools import cache

from soundings.runner import RunResult, ScenarioRunner

CATALOG = ("S01", "S02", "S03", "S04", "S05", "S06", "S07", "S08")
SWEEP_SEEDS = (1, 2, 3)


@cache
def run(scenario_id: str, seed: int | None = None) -> RunResult:
    """``ScenarioRunner(scenario_id, seed=seed).run()``, computed once per process."""
    return ScenarioRunner(scenario_id, seed=seed).run()


def gt_of(result: RunResult, contact_id: str) -> str:
    """Ground-truth id behind an observed id. Grading only; the runner never reads it."""
    return result.sim.contact_truth[contact_id].gt_id


def is_false_alarm(result: RunResult, contact_id: str) -> bool:
    return gt_of(result, contact_id).startswith("fa-")


def truth_at(result: RunResult, gt_id: str, tick: int) -> tuple[float, float]:
    point = {p.tick: p for p in result.sim.truth_paths[gt_id]}[tick]
    return point.lat, point.lon
