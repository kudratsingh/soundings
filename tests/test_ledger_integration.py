"""WO-086 integration: the streamed ledger equals the eval harness numbers for the same run.

The run engine streams a scenario; the final ``ledger`` event must equal
``ledger_from_run`` of the same run assembled with the eval's counting rules
(``assemble_run``, the hook WO-051's soundings adapter calls) and a ``Counter`` of
``ContactOutcome.max_rung``; the blunt counter must equal ``closure_count`` of
``BangPolicy().run(sc, seed)``. The engine on main streams runner v1 through the batch
source (WO-061's stepped source is not on main), so "the same run" is that runner's
result. Runner v3's ladder log goes through the same helper in ``test_runner_v3_buckets``.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path

import pytest

from soundings.canonical import round6
from soundings.eval.policies import (
    LEDGER_BUCKET_OF_RUNG,
    BangPolicy,
    LedgerBucket,
    PolicyEvent,
    PolicyName,
    PolicyRun,
    Transition,
    assemble_run,
    closure_count,
    closure_episodes,
    ledger_from_run,
    rung_ledger,
)
from soundings.runner import RunResult, ScenarioRunner, ScenarioRunnerV3
from soundings.schemas.scenario import Scenario
from soundings.sim.scenario import resolve_scenario
from soundings.stream.engine import RunEngine, source_names
from soundings.stream.events import LedgerEvent, StreamEvent
from soundings.stream.ledger import TICK_OVERLAYS

REPO = Path(__file__).resolve().parents[1]
BT01 = REPO / "scenarios" / "BT-01.yaml"
needs_bt01 = pytest.mark.skipif(
    not BT01.exists(), reason="BT-01 comes from WO-080 part A (not on main yet)"
)
RUNS = [pytest.param("BT-01", 7, marks=needs_bt01, id="BT-01"), pytest.param("S05", 11, id="S05")]


def _policy_run(result: RunResult, sc: Scenario) -> PolicyRun:
    """The streamed run as the eval harness counts it (runner v1 steps -> eval steps)."""
    tick_s = float(result.sim.tick_s)
    steps = tuple(
        Transition(
            seq=i,
            contact_id=t.contact_id,
            tick=t.tick,
            t_s=round6(t.tick * tick_s),
            from_state=t.from_state,
            to_state=t.to_state,
            event=PolicyEvent.STATE_CHANGE,
        )
        for i, t in enumerate(sorted(result.transitions, key=lambda t: t.seq))
    )
    return assemble_run(
        policy=PolicyName.SOUNDINGS,
        scenario=sc,
        sim=result.sim,
        transitions=steps,
        shutdowns=(),
        variant="nominal",
        planner="fallback",
    )


def _stream(sid: str, seed: int, source: str = "batch") -> list[StreamEvent]:
    """The engine's stream; ``batch`` is runner v1, which ``_policy_run`` adapts."""
    engine = RunEngine(default_speed=0)
    return engine.run_to_end(engine.start(sid, seed, speed=0, source=source).run_id)


@pytest.mark.parametrize(("sid", "seed"), RUNS)
def test_matches_eval_harness(sid: str, seed: int) -> None:
    sc = resolve_scenario(sid)
    events = _stream(sid, seed)
    assert events[-1].type == "run_finished"
    ledgers = [e for e in events if isinstance(e, LedgerEvent)]
    assert ledgers, "the stream must carry ledger events"
    final = ledgers[-1].payload

    result = ScenarioRunner(sc, seed=seed).run()
    assert result.transition_log_hash == events[-1].payload.transition_log_hash
    run = _policy_run(result, sc)
    by_max_rung = Counter(LEDGER_BUCKET_OF_RUNG[c.max_rung] for c in run.contacts)
    expected = {b: by_max_rung.get(b, 0) for b in LedgerBucket}
    assert final.counts == ledger_from_run(run) == expected
    assert final.contacts_seen == len(run.contacts)

    bang = BangPolicy().run(sc, seed)
    assert final.blunt.available
    assert final.blunt.berth_closures == closure_count(bang.transition_log)
    assert final.blunt.episodes == closure_episodes(bang.transition_log)
    assert final.blunt.closure_minutes == bang.shutdown_minutes
    assert bang.track_hash == events[-1].payload.track_hash


def test_runner_v3_buckets() -> None:
    """Runner v3's ladder log through the same helper: S05 at seed 11 reaches the handoff
    (the counts the ledger will show once a v3 source streams it)."""
    r = ScenarioRunnerV3("S05", seed=11).run()
    counts = rung_ledger(r.ladder_transitions, seen=r.sim.tracks)
    assert counts[LedgerBucket.HANDED_OFF] >= 1
    assert sum(counts.values()) == len(r.sim.tracks)


@needs_bt01
@pytest.mark.skipif(
    "stepped" not in source_names(), reason="the stepped source (WO-061) is not on main yet"
)
def test_bt01_story() -> None:
    events = _stream("BT-01", 7, source="stepped")
    ledgers = [e for e in events if isinstance(e, LedgerEvent)]
    final = ledgers[-1].payload
    assert final.counts[LedgerBucket.HANDED_OFF] == 1
    assert final.counts[LedgerBucket.CLEARED_AT_ASK] >= 39
    assert final.blunt.berth_closures >= 40
    # No ledger event precedes the event it reflects.
    for e in ledgers:
        k = e.seq - 1
        while events[k].type in TICK_OVERLAYS:
            k -= 1
        assert events[k].type in ("tick", "transition")
