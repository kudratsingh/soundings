"""WO-061: the stepped source (runner v3, a person approves through the UI)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import soundings.stream.engine as engine_mod
from soundings.ladder.approval import issue_token
from soundings.ladder.audit import AuditLog
from soundings.ladder.types import ActionScope
from soundings.llm.guard import DEFAULT_VIOLATIONS_PATH
from soundings.runner import ManualApprover, ScenarioRunnerV3
from soundings.schemas.enums import LadderState
from soundings.sim.scenario import resolve_scenario
from soundings.stream.engine import NoPendingApproval, RunEngine, SourceRequest, source_names
from soundings.stream.ledger import BluntCounter, LedgerAnnotator
from soundings.stream.stage import client_for_run
from soundings.stream.stepped import (
    AlreadyAnswered,
    DeferredSteppedSource,
    SteppedSource,
    UiApprover,
    stepped_stream_events,
)
from soundings.stream.v3 import V3Source


class FakeClock:
    def __init__(self) -> None:
        self.t = 0.0

    def now_s(self) -> float:
        return self.t


def _open_request(engine: RunEngine, run_id: str, clock: FakeClock) -> Any:
    h = engine.get(run_id)
    src = h.source
    assert isinstance(src, DeferredSteppedSource)
    assert src.wait_ready(120)
    while True:
        clock.t += 5.0
        h.available()
        req = next((e.payload for e in h.events if e.type == "approval_request"), None)
        if req is not None:
            return req
        assert not h.exhausted, "S05 should ask for approval"


def test_registered_and_approver_is_ui() -> None:
    assert "stepped" in source_names()
    req = SourceRequest(scenario=resolve_scenario("S05"), seed=11, run_id="t", speed=0)
    src = SteppedSource(req)
    assert isinstance(src.approver, UiApprover) and src.name == "stepped"


def test_s05_approval_flow(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    live_db = tmp_path / "live.db"
    monkeypatch.setitem(
        engine_mod._SOURCES, "stepped", lambda r: DeferredSteppedSource(r, audit_db=live_db)
    )
    clock = FakeClock()
    engine = RunEngine(clock_factory=lambda: clock)
    h = engine.start("S05", 11, speed=20, source="stepped")
    req = _open_request(engine, h.run_id, clock)
    states = [e.payload.to_state for e in h.events if e.type == "transition"]
    assert LadderState.HOLDING in states and LadderState.AWAITING_APPROVAL in states
    asked_tick = next(e.tick for e in h.events if e.type == "approval_request")
    src = h.source
    assert isinstance(src, DeferredSteppedSource)
    approve_tick = src.inner._tick
    answer = engine.approve(h.run_id, req.request_id, "Watch Officer")
    assert answer.payload.outcome == "granted" and answer.payload.token_hash
    with pytest.raises(AlreadyAnswered):
        engine.approve(h.run_id, req.request_id, "Watch Officer")
    clock.t = 1e9
    h.available()
    assert h.finished and h.events[-1].type in ("run_finished", "ledger")
    mine = [
        e.payload.to_state
        for e in h.events
        if e.type == "transition" and e.payload.contact_id == req.contact_id
    ]
    assert LadderState.HANDED_OFF in mine
    assert mine.index(LadderState.WATCH_AFTER_HANDOFF) > mine.index(LadderState.HANDED_OFF)
    results = [e.payload for e in h.events if e.type == "approval_result"]
    assert [r.outcome for r in results] == ["granted"]
    assert results[0].token_hash == answer.payload.token_hash
    finished = next(e.payload for e in h.events if e.type == "run_finished")
    live_log = AuditLog(live_db)
    live_head = live_log.head()
    assert live_log.verify().ok
    live_log.close()

    # The batch runner, with a scripted approver granting the same token at the same tick.
    approver = ManualApprover()
    rc = client_for_run(False)
    session = ScenarioRunnerV3(resolve_scenario("S05"), seed=11).start(
        llm_roles=rc.llm_roles,
        violations_path=DEFAULT_VIOLATIONS_PATH,
        toggles=(),
        approver=approver,
        writer_mode="template",
        sitrep_client=None,
        run_id=h.run_id,
        db_url=tmp_path / "batch.db",
    )
    while not session.done:
        session.step()
        if session.tick == approve_tick:
            token = issue_token(
                contact_id=req.contact_id,
                scope=ActionScope.model_validate(req.scope),
                approver="primary",
                channel="ui",
                issued_at=round(approve_tick * 2.0, 6),
                expires_at=req.expires_t_s,
            ).model_dump(mode="json")
            session.approve(req.contact_id, "granted", token=token)
    batch = session.result()
    assert asked_tick <= approve_tick
    assert batch.ladder[req.contact_id].final_state is finished.terminal[req.contact_id]
    assert batch.ladder_log_hash == finished.transition_log_hash
    assert batch.audit_head == live_head


def test_nothing_pending_before_a_request() -> None:
    clock = FakeClock()
    engine = RunEngine(clock_factory=lambda: clock)
    h = engine.start("S05", 11, speed=20, source="stepped")
    with pytest.raises(NoPendingApproval):
        engine.approve(h.run_id, "apr-c-0004-1-primary", "Watch Officer")


def test_decline_is_a_persons_no() -> None:
    clock = FakeClock()
    engine = RunEngine(clock_factory=lambda: clock)
    h = engine.start("S05", 11, speed=20, source="stepped")
    req = _open_request(engine, h.run_id, clock)
    src = h.source
    assert isinstance(src, DeferredSteppedSource)
    src.decline(req.request_id, "Watch Officer")
    clock.t = 1e9
    h.available()
    results = [e.payload.outcome for e in h.events if e.type == "approval_result"]
    assert results[0] == "denied"
    acts = [
        e.payload.action
        for e in h.events
        if e.type == "ladder_action" and e.payload.contact_id == req.contact_id
    ]
    assert "record_approved_action" not in acts  # a no records nothing approved


def test_deferred_stream_equals_stepped_stream() -> None:
    sc = resolve_scenario("S01")
    req = SourceRequest(scenario=sc, seed=1, run_id="cli", speed=0)
    src = DeferredSteppedSource(req)
    src.annotate(LedgerAnnotator(BluntCounter(sc, 1).start()))
    deferred = src.events()
    direct = stepped_stream_events("S01", 1)
    assert [e.model_dump() for e in deferred] == [e.model_dump() for e in direct]


def test_stepped_matches_v3_without_approvals() -> None:
    """With nobody answering, the stepped stream is the v3-manual stream."""
    sc = resolve_scenario("S01")
    req = SourceRequest(scenario=sc, seed=1, run_id="cli", speed=0)
    a = [e.model_dump() for e in SteppedSource(req).events()]
    b = [e.model_dump() for e in V3Source(req, manual=True).events()]
    assert a == b


def test_cli_approve_first_is_deterministic() -> None:
    a = stepped_stream_events("S05", 11, approve_first=True)
    b = stepped_stream_events("S05", 11, approve_first=True)
    assert [e.model_dump() for e in a] == [e.model_dump() for e in b]
    outcomes = [e.payload.outcome for e in a if e.type == "approval_result"]
    assert outcomes == ["granted"]
