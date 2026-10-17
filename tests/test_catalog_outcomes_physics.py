"""WO-089: catalog outcomes under the passive-sonar-equation model.

The owner's exit tests: S07/S08 unchanged in outcome; catalog outcomes unchanged except
where the report explains why. ``GAUSSIAN_BASELINE`` is the lineage-end terminal state of
S01..S08 at the catalog seed and seeds 1-3 under ``detection.model: gaussian`` (WO-011),
recorded before the swap. Every difference under ``sonar_equation`` is listed in
``EXPLAINED`` with its DECISIONS line; all of them move a seed toward the scenario's own
``expected.terminal_state``.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from functools import cache

import pytest

import soundings.sim.run as sim_run
from soundings.runner import ScenarioRunnerV2
from soundings.sim.scenario import resolve_scenario

CATALOG = ("S01", "S02", "S03", "S04", "S05", "S06", "S07", "S08")

GAUSSIAN_BASELINE: dict[str, str] = {
    "S01/1": "CLEARED_AT_ASK",
    "S01/2": "CLEARED_AT_ASK",
    "S01/3": "CLEARED_AT_ASK",
    "S02/1": "LOST_VEHICLE",
    "S02/2": "LOST_VEHICLE",
    "S02/3": "LOST_VEHICLE",
    "S03/1": "CLEARED_AT_ASK",
    "S03/2": "CLEARED_AT_ASK",
    "S03/3": "CLEARED_AT_ASK",
    "S03/5": "CLEARED_AT_ASK",
    "S04/1": "HANDED_OFF",
    "S04/2": "HANDED_OFF",
    "S04/3": "HANDED_OFF",
    "S04/7": "HANDED_OFF",
    "S05/1": "HANDED_OFF",
    "S05/2": "HANDED_OFF",
    "S05/3": "HANDED_OFF",
    "S05/11": "HANDED_OFF",
    "S06/1": "HANDED_OFF",
    "S06/2": "HANDED_OFF",
    "S06/3": "HANDED_OFF",
    "S06/13": "HANDED_OFF",
    "S07/1": "RESOLVED_BENIGN",
    "S07/2": "RESOLVED_BENIGN",
    "S07/3": "RESOLVED_BENIGN",
    "S07/17": "RESOLVED_BENIGN",
    "S08/1": "HANDED_OFF",
    "S08/2": "HAZARD_NOTICE",
    "S08/3": "HAZARD_NOTICE",
    "S08/19": "HAZARD_NOTICE",
}

_WHY = (
    "DECISIONS 2026-10-17 (WO-089 code half): the logistic term keeps far ranges detected "
    "more often than the Gaussian (0.5 vs 0.37 at 400 m), so the track splits on the "
    "timeout less and the lineage ends on the scenario's expected state"
)
EXPLAINED: dict[str, tuple[str, str]] = {
    "S04/7": ("RESOLVED_BENIGN", _WHY + " (the S04 xfail's re-acquired track no longer forms)"),
    "S04/2": ("RESOLVED_BENIGN", _WHY),
    "S08/1": ("HAZARD_NOTICE", _WHY),
}


def _seeds(sid: str) -> list[int]:
    return [resolve_scenario(sid).seed, 1, 2, 3]


@contextmanager
def _model(model: str) -> Iterator[None]:
    original = sim_run.default_config
    base = original()
    cfg = replace(base, params=replace(base.params, model=model))
    sim_run.default_config = lambda: cfg  # type: ignore[assignment]
    try:
        yield
    finally:
        sim_run.default_config = original


def _lineage_end(sid: str, seed: int) -> str:
    result = ScenarioRunnerV2(sid, seed=seed).run()
    truth = result.sim.contact_truth
    cases = [c for c in result.cases if not truth[c.contact_id].gt_id.startswith("fa-")]
    cases.sort(key=lambda c: (c.opened_tick, c.case_id))
    return str(cases[-1].state.value)


@cache
def sonar_outcomes() -> dict[str, str]:
    assert sim_run.default_config().params.model == "sonar_equation"
    return {f"{sid}/{seed}": _lineage_end(sid, seed) for sid in CATALOG for seed in _seeds(sid)}


def test_baseline_covers_catalog_seed_and_seeds_1_to_3() -> None:
    assert set(GAUSSIAN_BASELINE) == {f"{s}/{n}" for s in CATALOG for n in _seeds(s)}
    for key, (state, why) in EXPLAINED.items():
        assert GAUSSIAN_BASELINE[key] != state and "DECISIONS" in why
        expected = resolve_scenario(key.split("/")[0]).expected.terminal_state.value
        assert state == expected, key  # every explained move goes to the expected state


@pytest.mark.parametrize("sid", CATALOG)
def test_gaussian_model_reproduces_the_recorded_baseline_at_the_catalog_seed(sid: str) -> None:
    seed = resolve_scenario(sid).seed
    with _model("gaussian"):
        assert _lineage_end(sid, seed) == GAUSSIAN_BASELINE[f"{sid}/{seed}"]


def test_catalog_outcomes_unchanged_except_explained() -> None:
    got = sonar_outcomes()
    rows = ["| run | gaussian | sonar_equation | note |", "|---|---|---|---|"]
    for key in GAUSSIAN_BASELINE:
        note = "explained" if key in EXPLAINED else ""
        rows.append(f"| {key} | {GAUSSIAN_BASELINE[key]} | {got[key]} | {note} |")
    print("\n".join(rows))
    for key, before in GAUSSIAN_BASELINE.items():
        want = EXPLAINED[key][0] if key in EXPLAINED else before
        assert got[key] == want, (key, before, got[key])


@pytest.mark.parametrize("sid", ("S07", "S08"))
def test_s07_s08_unchanged_in_outcome(sid: str) -> None:
    seed = resolve_scenario(sid).seed
    expected = resolve_scenario(sid).expected.terminal_state.value
    got = sonar_outcomes()[f"{sid}/{seed}"]
    assert got == GAUSSIAN_BASELINE[f"{sid}/{seed}"] == expected
