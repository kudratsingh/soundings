"""WO-060 part B: the runner v3 source streams the investigation, the posterior, the
recommendation, the ladder, approvals and SITREPs; toggles reach the engine; the stream is
deterministic; the approval hook for WO-061 takes only a person's token."""

from __future__ import annotations

import re
from collections import Counter
from typing import Any

import pytest

from soundings.runner import ScenarioRunnerV3
from soundings.schemas.enums import LadderState, Rung, ToggleName
from soundings.sim.scenario import resolve_scenario
from soundings.stream.engine import (
    NoPendingApproval,
    RunEngine,
    SourceRequest,
    v3_stream_events,
)
from soundings.stream.events import (
    InvestigationStepEvent,
    LadderActionEvent,
    RecommendationEvent,
    StreamEvent,
    event_json,
    parse_event,
)
from soundings.stream.v3 import TokenRefused, V3Source

POST_HANDOFF = {LadderState.WATCH_AFTER_HANDOFF, LadderState.RESOLVED_DEPARTED}


@pytest.fixture(scope="module")
def s05() -> list[StreamEvent]:
    return v3_stream_events("S05", 11)


@pytest.fixture(scope="module")
def s02() -> list[StreamEvent]:
    return v3_stream_events("S02")


def _of(events: list[StreamEvent], kind: str) -> list[Any]:
    return [e.payload for e in events if e.type == kind]


# --- event schema -----------------------------------------------------------------------


SAMPLES: dict[str, dict[str, Any]] = {
    "investigation_step": {
        "contact_id": "c-0004",
        "case_id": "c-0004#1",
        "probe_id": "second_sensor_dwell",
        "source": "fallback",
        "fallback_reason": "replay_miss",
        "why": "largest expected information gain per cost",
        "reason": "chosen",
        "sent_tick": 100,
        "landed_tick": 102,
        "row_ids": ["abc"],
    },
    "recommendation": {
        "contact_id": "c-0004",
        "case_id": "c-0004#1",
        "outcome": "HANDOFF",
        "recommended_rung": "hand_to_person",
        "stop_clause": "C_THREAT",
        "threat_mass": 0.7,
        "benign_mass": 0.2,
        "top": "UNAUTHORIZED_UUV",
        "uncertain": False,
        "question_for_human": None,
        "evidence_row_ids": ["abc"],
    },
    "ladder_action": {
        "contact_id": "c-0004",
        "state": "HOLDING",
        "action": "start_hold",
        "scope": {"action": "hold_one_gate_or_berth", "gates": 1, "max_minutes": 15},
        "clause": "standing_orders.hold.hold_one_gate_or_berth",
        "signed_by": "Watch Commander (demo)",
        "timeout_s": None,
        "expires_t_s": None,
    },
}


@pytest.mark.parametrize("kind", sorted(SAMPLES))
def test_new_event_round_trips_and_forbids_extras(kind: str) -> None:
    import json

    env = {"v": 1, "run_id": "r", "seq": 3, "tick": 7, "t_s": 14.0, "type": kind}
    event = parse_event(json.dumps({**env, "payload": SAMPLES[kind]}))
    assert type(event) in (InvestigationStepEvent, RecommendationEvent, LadderActionEvent)
    assert parse_event(event_json(event)) == event
    with pytest.raises(ValueError):
        parse_event(json.dumps({**env, "payload": {**SAMPLES[kind], "rung": "hold"}}))
    with pytest.raises(ValueError):
        parse_event(json.dumps({**env, "extra": 1, "payload": SAMPLES[kind]}))


def test_recommendation_label_is_fixed() -> None:
    import json

    env = {"v": 1, "run_id": "r", "seq": 0, "tick": 0, "t_s": 0.0, "type": "recommendation"}
    bad = {**SAMPLES["recommendation"], "label": "probability"}
    with pytest.raises(ValueError):
        parse_event(json.dumps({**env, "payload": bad}))


def test_every_streamed_event_round_trips(s05: list[StreamEvent]) -> None:
    kinds = Counter(e.type for e in s05)
    for kind in (
        "investigation_step",
        "posterior",
        "recommendation",
        "ladder_action",
        "approval_request",
        "approval_result",
        "sitrep",
        "transition",
        "rung",
    ):
        assert kinds[kind] > 0, kind
    seen: set[str] = set()
    for e in s05:
        if e.type not in seen:
            seen.add(e.type)
            assert parse_event(event_json(e)) == e
    assert [e.seq for e in s05] == list(range(len(s05)))


# --- the demo paths -----------------------------------------------------------------------


def test_s05_reaches_awaiting_approval_under_standing_orders(s05: list[StreamEvent]) -> None:
    started = s05[0].payload
    assert started.runner == "v3" and started.source == "stepped"
    assert started.model_path == "off" and started.orders == "signed"
    trans = [p for p in _of(s05, "transition") if p.contact_id == "c-0004"]
    path = [p.to_state for p in trans]
    for state in (
        LadderState.ADVISING_SLOW,
        LadderState.HOLDING,
        LadderState.AWAITING_APPROVAL,
        LadderState.HANDED_OFF,
    ):
        assert state in path
    hold = [
        p
        for p in _of(s05, "ladder_action")
        if p.contact_id == "c-0004" and p.action == "start_hold"
    ]
    assert hold and hold[0].clause == "standing_orders.hold.hold_one_gate_or_berth"
    assert hold[0].signed_by == "Watch Commander (demo)"
    (req,) = _of(s05, "approval_request")
    assert req.contact_id == "c-0004" and req.scope["action"] == "hold_longer"
    assert req.action_label and req.approver == "Watch Officer"
    asked = next(e for e in s05 if e.type == "approval_request")
    assert req.expires_t_s == pytest.approx(asked.t_s + 120.0)
    (res,) = _of(s05, "approval_result")
    assert res.request_id == req.request_id and res.outcome == "granted" and res.token_hash
    statuses = [p.status for p in _of(s05, "sitrep") if p.contact_id == "c-0004"]
    assert statuses[:2] == ["draft", "final"] and "update" in statuses
    for p in _of(s05, "sitrep"):
        if p.kind == "sitrep":
            assert p.markdown and p.sitrep is not None
    # The lineage's investigation found threat mass >= 0.6 (stress-test estimate).
    posts = _of(s05, "posterior")
    assert max(p.threat_mass for p in posts) >= 0.6
    assert {p.label for p in posts} == {"stress-test estimate"}


def test_investigation_steps_carry_source_why_and_rows(s05: list[StreamEvent]) -> None:
    sent: set[str] = set()
    steps = 0
    for e in s05:
        if e.type == "evidence_row":
            sent.add(e.payload.row_id)
        elif e.type == "investigation_step":
            steps += 1
            p = e.payload
            assert p.source in ("deterministic", "llm", "fallback")
            assert set(p.row_ids) <= sent
            assert p.landed_tick >= p.sent_tick
            if p.probe_id is not None:
                assert p.why
        elif e.type == "recommendation":
            p = e.payload
            assert set(p.evidence_row_ids) <= sent
            assert p.stop_clause and p.label == "stress-test estimate"
    assert steps > 0


def test_s02_lost_vehicle_carries_search_box(s02: list[StreamEvent]) -> None:
    terminal = s02[-1].payload.terminal
    notices = [p for p in _of(s02, "sitrep") if p.kind == "lost_vehicle"]
    assert notices
    for p in notices:
        assert terminal[p.contact_id] is LadderState.LOST_VEHICLE
        assert p.notice is not None and p.notice.search_box.radius_m > 0


def test_s04_primary_resolves_benign() -> None:
    events = v3_stream_events("S04")
    result = ScenarioRunnerV3("S04").run()
    sim = result.sim
    primary = min(
        cid
        for cid, truth in sim.contact_truth.items()
        if truth.gt_id is not None and cid in result.ladder
    )
    assert events[-1].payload.terminal[primary] is LadderState.RESOLVED_BENIGN


def test_nothing_after_handoff_v3(s05: list[StreamEvent], s02: list[StreamEvent]) -> None:
    for events in (s05, s02):
        handed: set[str] = set()
        for p in _of(events, "transition"):
            if p.contact_id in handed:
                assert p.to_state in POST_HANDOFF
            if p.to_state is LadderState.HANDED_OFF:
                handed.add(p.contact_id)
        assert handed


def test_citations_resolve_v3(s05: list[StreamEvent]) -> None:
    from soundings.stream.v3 import _cited_ids

    sent: set[str] = set()
    for e in s05:
        if e.type == "evidence_row":
            sent.add(e.payload.row_id)
        elif e.type == "transition":
            assert set(e.payload.evidence_row_ids) <= sent
        elif e.type == "sitrep" and e.payload.sitrep is not None:
            assert _cited_ids(e.payload.sitrep) <= sent


def test_v3_stream_deterministic() -> None:
    a = [event_json(e) for e in v3_stream_events("S05", 11)]
    b = [event_json(e) for e in v3_stream_events("S05", 11)]
    assert a == b
    assert not any(re.search(r"\b20\d\d-\d\d-\d\dT", line) for line in a)


# --- toggles and pacing ---------------------------------------------------------------------


def test_toggles_reach_the_engine() -> None:
    events = v3_stream_events("S05", 11, toggles=[ToggleName.APPROVER_UNREACHABLE])
    assert events[0].payload.toggles == [ToggleName.APPROVER_UNREACHABLE]
    outcomes = [p.outcome for p in _of(events, "approval_result")]
    assert "granted" not in outcomes
    assert "backup_paged" in outcomes
    requests = _of(events, "approval_request")
    assert [r.approver for r in requests] == ["Watch Officer", "Duty Supervisor"]


@pytest.mark.parametrize(
    "toggle",
    [
        ToggleName.NETWORK_DOWN,
        ToggleName.STANDING_ORDERS_EXPIRED,
        ToggleName.FOG_NO_CAMERA,
        ToggleName.SENSOR_DROPOUT,
        ToggleName.CONTACT_SURGE,
    ],
)
def test_each_toggle_is_accepted(toggle: ToggleName) -> None:
    engine = RunEngine(default_speed=0)
    h = engine.start("S01", 1, speed=0, toggles=[toggle], source="v3")
    events = engine.run_to_end(h.run_id)
    assert toggle in events[0].payload.toggles
    assert events[-1].type == "run_finished"


def test_standing_orders_expired_never_holds() -> None:
    events = v3_stream_events("S05", 11, toggles=[ToggleName.STANDING_ORDERS_EXPIRED])
    states = {p.to_state for p in _of(events, "transition")}
    assert LadderState.HOLDING not in states and LadderState.ADVISING_SLOW not in states


class FakeClock:
    def __init__(self, t: float = 0.0) -> None:
        self.t = t

    def now_s(self) -> float:
        return self.t


def test_pacing_drops_to_1x_while_awaiting_approval() -> None:
    clock = FakeClock()
    engine = RunEngine(clock_factory=lambda: clock)
    h = engine.start("S05", 11, speed=20, source="v3")
    assert h.lazy and not h.exhausted
    assert 0 < h.available() < 50  # only tick 0 is out; the rest is not even stepped yet
    clock.t = 1294.0 / 20 + 0.01  # the approval request lands at t_s 1294
    n = h.available()
    asked = next(i for i, e in enumerate(h.events[:n]) if e.type == "approval_request")
    t_ask = h.events[asked].t_s
    # While AWAITING_APPROVAL the pace is 1x: 30 sim seconds later need 30 wall seconds.
    clock.t += 1.0
    released = h.events[: h.available()]
    assert released[-1].t_s <= t_ask + 1.0 + 1e-6
    clock.t += 60.0  # the simulated approver grants 60 s after the request
    assert any(e.type == "approval_result" for e in h.events[: h.available()])
    clock.t = 1e9
    assert h.finished and h.events[-1].type == "run_finished"
    paced = [e.model_dump(exclude={"run_id"}) for e in h.events[1:]]
    unpaced = [e.model_dump(exclude={"run_id"}) for e in v3_stream_events("S05", 11)[1:]]
    assert paced == unpaced  # pacing never changes the stream


def test_deliver_approval_hook() -> None:
    from soundings.ladder.approval import issue_token
    from soundings.ladder.types import ActionScope

    clock = FakeClock()
    engine = RunEngine(clock_factory=lambda: clock)
    sim_run = engine.start("S05", 11, speed=20, source="v3")
    with pytest.raises(NoPendingApproval):
        engine.deliver_approval(sim_run.run_id, {"contact_id": "c-0004"})
    h = engine.start("S05", 11, speed=20, source="v3-manual")
    with pytest.raises(NoPendingApproval):  # nothing asked yet
        engine.deliver_approval(h.run_id, {"contact_id": "c-0004"})
    clock.t = 1294.0 / 20 + 0.01
    h.available()
    req = next(e.payload for e in h.events if e.type == "approval_request")
    asked_at = req.expires_t_s - 120.0

    def token(scope: dict[str, Any]) -> dict[str, Any]:
        return issue_token(
            contact_id=req.contact_id,
            scope=ActionScope.model_validate(scope),
            approver="primary",
            channel="ui",
            issued_at=asked_at,
            expires_at=req.expires_t_s,
        ).model_dump(mode="json")

    with pytest.raises(TokenRefused):  # a token for another scope is not forwarded
        engine.deliver_approval(h.run_id, token({**req.scope, "max_minutes": 60}))
    assert engine.deliver_approval(h.run_id, token(req.scope)) == "c-0004"
    clock.t = 1e9
    h.available()
    results = [e.payload for e in h.events if e.type == "approval_result"]
    assert [r.outcome for r in results] == ["granted"]
    states = [
        e.payload.to_state
        for e in h.events
        if e.type == "transition" and e.payload.contact_id == "c-0004"
    ]
    assert LadderState.HANDED_OFF in states
    with pytest.raises(NoPendingApproval):
        engine.approve(h.run_id, req.request_id, "primary")


def test_v3_source_rejects_nothing_the_batch_rejects() -> None:
    sc = resolve_scenario("S01")
    req = SourceRequest(scenario=sc, seed=1, run_id="t", speed=0, use_llm=False)
    events = V3Source(req).events()
    assert events[-1].type == "run_finished"
    assert all(e.payload.rung in set(Rung) for e in events if e.type == "rung")
