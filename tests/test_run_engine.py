"""WO-060 Part A: the run engine replays exactly what runner v1 decided, paced but not
changed by the clock, with every citation sent before it is used."""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

import pytest

from soundings.runner import ScenarioRunner
from soundings.schemas.enums import EvidenceKind, LadderState, ToggleName
from soundings.sim.scenario import catalog_paths
from soundings.stream.engine import (
    NoPendingApproval,
    RunEngine,
    UnknownRun,
    UnsupportedOption,
    stream_events,
)
from soundings.stream.events import StreamEvent, event_json

CATALOG = [p.name[:3] for p in catalog_paths()]


@dataclass
class FakeClock:
    t: float = 0.0
    step: float = 0.0  # seconds added on every read (a clock that moves on its own)

    def now_s(self) -> float:
        self.t += self.step
        return self.t


def _run(coro: Coroutine[Any, Any, list[int]]) -> list[int]:
    """asyncio.run in a fresh thread (a session browser fixture may hold this thread's loop)."""
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


def _by_type(events: list[StreamEvent], kind: str) -> list[StreamEvent]:
    return [e for e in events if e.type == kind]


@pytest.mark.parametrize("sid", CATALOG)
def test_engine_matches_batch_runner(sid: str) -> None:
    result = ScenarioRunner(sid, seed=1).run()
    engine = RunEngine(default_speed=0)
    events = engine.run_to_end(engine.start(sid, 1, speed=0).run_id)
    terminal: dict[str, LadderState] = {}
    for case in result.cases:  # sorted by (contact, case number): the last case wins
        terminal[case.contact_id] = case.state
    finished = events[-1]
    assert finished.type == "run_finished"
    assert finished.payload.terminal == terminal
    assert finished.payload.transition_log_hash == result.transition_log_hash
    streamed = [e.payload.row_id for e in _by_type(events, "evidence_row")]
    assert len(streamed) == len(set(streamed))
    assert set(streamed) == {r.row_id for r in result.evidence}
    trans = [e.payload for e in _by_type(events, "transition")]
    assert [(t.case_id, t.from_state, t.to_state, t.reason_code) for t in trans] == [
        (t.case_id, t.from_state, t.to_state, t.reason_code) for t in result.transitions
    ]


def test_demo_paths() -> None:
    s01 = stream_events("S01", 1)
    states = set(s01[-1].payload.terminal.values())
    assert LadderState.CLEARED_AT_ASK in states
    assert LadderState.LOST_VEHICLE not in states
    assert not _by_type(s01, "sitrep")

    s02 = stream_events("S02", 3)
    terminal = s02[-1].payload.terminal
    sitreps = _by_type(s02, "sitrep")
    assert sitreps, "S02 must stream its LOST_VEHICLE notice"
    for e in sitreps:
        p = e.payload
        assert p.kind == "lost_vehicle" and p.writer == "template"
        assert p.notice is not None
        assert terminal[p.contact_id] is LadderState.LOST_VEHICLE
        box = p.notice.search_box
        assert box.radius_m > 0 and box.min_lat <= box.center_lat <= box.max_lat
    # The rung indicator reaches ask on the way.
    rungs = [e.payload.rung.value for e in _by_type(s02, "rung")]
    assert "ask" in rungs


def test_stream_deterministic() -> None:
    a = [event_json(e) for e in stream_events("S02", 3)]
    b = [event_json(e) for e in stream_events("S02", 3)]
    assert a == b
    # Pacing never changes the stream: speed 20 on a clock gives the same events.
    clock = FakeClock(1e9)
    engine = RunEngine(clock_factory=lambda: clock)
    paced = engine.run_to_end(engine.start("S02", 3, speed=20).run_id)
    assert [e.model_dump(exclude={"run_id"}) for e in paced[1:]] == [
        e.model_dump(exclude={"run_id"}) for e in stream_events("S02", 3)[1:]
    ]


def test_sitrep_citations_resolve() -> None:
    sent: set[str] = set()
    kinds: dict[str, EvidenceKind] = {}
    seen_sitrep = False
    for e in stream_events("S02", 3):
        if e.type == "evidence_row":
            sent.add(e.payload.row_id)
            kinds[e.payload.row_id] = e.payload.row.kind
        elif e.type == "transition":
            assert set(e.payload.evidence_row_ids) <= sent
        elif e.type == "sitrep":
            seen_sitrep = True
            notice = e.payload.notice
            assert notice is not None
            assert set(notice.evidence_row_ids) <= sent
            for line in notice.lines:
                assert set(line.evidence_row_ids) <= sent
                cited = {kinds[i] for i in line.evidence_row_ids}
                if line.section == "What we know":
                    assert EvidenceKind.ASSUMPTION not in cited
                if line.section == "What we assume":
                    assert EvidenceKind.ASSUMPTION in cited
    assert seen_sitrep


def test_no_model_calls_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    from soundings.llm import client

    calls = {"n": 0}

    def counting(self: object, role: str, inputs: object) -> None:
        calls["n"] += 1

    monkeypatch.setattr(client.ModelClient, "call", counting)
    engine = RunEngine(default_speed=0)
    engine.run_to_end(engine.start("S02", 3).run_id)
    assert calls["n"] == 0
    with pytest.raises(UnsupportedOption):
        engine.start("S02", 3, use_llm=True)


def test_pacing_releases_by_sim_time() -> None:
    clock = FakeClock(0.0)
    engine = RunEngine(clock_factory=lambda: clock)
    h = engine.start("S01", 1, speed=20)
    assert 0 < h.available() < len(h.events)
    assert all(e.t_s == 0.0 for e in h.events[: h.available()])
    clock.t = 1.0  # 20 sim seconds = 10 ticks at tick_s 2
    n = h.available()
    assert h.events[n - 1].t_s <= 20.0 < h.events[n].t_s
    assert h.wait_s(n) == pytest.approx(h.events[n].t_s / 20 - 1.0)
    clock.t = 1e9
    assert h.finished


def test_subscribe_backlog_then_live_and_resume() -> None:
    clock = FakeClock(0.0, step=0.05)
    engine = RunEngine(clock_factory=lambda: clock)
    h = engine.start("S01", 1, speed=2000)

    async def collect(after: int, limit: int) -> list[int]:
        out: list[int] = []
        async for e in engine.subscribe(h.run_id, after):
            out.append(e.seq)
            if len(out) >= limit:
                break
        return out

    first = _run(collect(-1, 50))
    assert first == list(range(50))
    resumed = _run(collect(first[-1], 20))
    assert resumed == list(range(50, 70))
    rest = _run(collect(69, 10**9))
    assert rest == list(range(70, len(h.events)))


def test_eviction_and_lookup() -> None:
    clock = FakeClock(0.0)
    engine = RunEngine(max_runs=2, clock_factory=lambda: clock)
    a = engine.start("S01", 1, speed=0)  # finished at once
    b = engine.start("S01", 1, speed=1)  # still running
    c = engine.start("S01", 1, speed=1)
    ids = [h.run_id for h in engine.list()]
    assert ids == [c.run_id, b.run_id]  # newest first; the finished run went first
    with pytest.raises(UnknownRun):
        engine.get(a.run_id)
    with pytest.raises(NoPendingApproval):
        engine.approve(b.run_id, "req-1", "primary")
    with pytest.raises(UnsupportedOption):
        engine.start("S01", 1, toggles=[ToggleName.SENSOR_DROPOUT])
    with pytest.raises(UnsupportedOption):  # WO-061 registers "stepped"; any unknown name
        engine.start("S01", 1, source="no-such-source")
