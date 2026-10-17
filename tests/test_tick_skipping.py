"""WO-080: opt-in tick skipping never changes an outcome.

Exit test: ``uv run pytest tests/test_tick_skipping.py``. S01..S08 at seeds 1, 2, 3 and a
BT-01 cut to 2.5 h (1,800 ticks) run with ``skip_idle_ticks`` False and True; the
transition log text, cases and notices are identical, and skipping stepped fewer ticks.
The full BT-01 comparison lives in ``tests/test_busy_tuesday.py`` (``-k skip_equal``).
"""

from __future__ import annotations

import pytest

from soundings.runner import RunResult, ScenarioRunner, _next_wake_tick, transition_log_text
from soundings.schemas.enums import LadderState
from soundings.sim.generate import generate_busy_day
from soundings.sim.scenario import catalog_paths, resolve_scenario

CATALOG = {p.name[:3]: resolve_scenario(p) for p in catalog_paths()}


def assert_same(a: RunResult, b: RunResult) -> None:
    assert transition_log_text(a.transitions) == transition_log_text(b.transitions)
    assert a.transition_log_hash == b.transition_log_hash
    assert a.cases == b.cases
    assert a.notices == b.notices
    assert a.hails == b.hails
    assert a.reacquisitions == b.reacquisitions
    assert a.evidence.table_hash() == b.evidence.table_hash()


@pytest.mark.parametrize("seed", [1, 2, 3])
@pytest.mark.parametrize("sid", sorted(CATALOG))
def test_catalog_skip_equal(sid: str, seed: int) -> None:
    sc = CATALOG[sid]
    plain = ScenarioRunner(sc, seed=seed).run()
    skip = ScenarioRunner(sc, seed=seed, skip_idle_ticks=True).run()
    assert_same(plain, skip)
    assert plain.ticks_stepped >= sc.duration_ticks
    assert skip.ticks_stepped < plain.ticks_stepped


def test_bt01_truncated_skip_equal() -> None:
    sc = generate_busy_day(seed=7, hours=2.5)
    assert sc.duration_ticks == 1800
    plain = ScenarioRunner(sc).run()
    skip = ScenarioRunner(sc, skip_idle_ticks=True).run()
    assert_same(plain, skip)
    assert skip.ticks_stepped < plain.ticks_stepped


def test_default_is_unskipped() -> None:
    sc = CATALOG["S01"]
    assert ScenarioRunner(sc).skip_idle_ticks is False
    r = ScenarioRunner(sc).run()
    assert r.ticks_stepped >= sc.duration_ticks


def test_later_runner_states_force_every_tick() -> None:
    """A case past ASKING and not terminal makes the next wake tick ``tick + 1``."""
    from soundings.runner import _V1Run

    class _V2Like(_V1Run):
        pass

    runner = ScenarioRunner(CATALOG["S05"], skip_idle_ticks=True)
    v1 = _V1Run(runner)
    v2 = _V2Like(runner)
    for run in (v1, v2):
        case = run.open_case(next(iter(run.contacts.values())), 0, opened_by="new_track")
        case.state = LadderState.INVESTIGATING
    # v1 parks at INVESTIGATING (nothing acts on it); a later runner is stepped per tick.
    assert _next_wake_tick(v2, 0) == 1
    assert _next_wake_tick(v1, 0) >= 1
    for state in (
        LadderState.ADVISING_SLOW,
        LadderState.HOLDING,
        LadderState.AWAITING_APPROVAL,
        LadderState.WATCH_AFTER_HANDOFF,
    ):
        next(iter(v1.cases.values())).state = state
        assert _next_wake_tick(v1, 5) == 6


def test_next_wake_bounded() -> None:
    from soundings.runner import _V1Run

    runner = ScenarioRunner(CATALOG["S01"], skip_idle_ticks=True)
    run = _V1Run(runner)
    n = run.sim.n_ticks
    assert _next_wake_tick(run, n - 1) == n
    assert 1 <= _next_wake_tick(run, 0) <= n
