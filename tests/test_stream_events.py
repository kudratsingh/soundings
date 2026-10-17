"""WO-060: the WebSocket event schema round-trips, forbids unknown fields, and matches
the committed JSON Schema the UI mirrors."""

from __future__ import annotations

import json
import typing
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from soundings.schemas.enums import Hypothesis, LadderState, Rung
from soundings.stream import events as ev
from soundings.stream.engine import stream_events
from soundings.stream.events import (
    EVENT_TYPES,
    PosteriorEvent,
    StreamEvent,
    event_json,
    export_json_schema,
    parse_event,
)

REPO = Path(__file__).resolve().parent.parent
SCHEMA_FILE = REPO / "web" / "src" / "generated" / "stream-events.schema.json"


@pytest.fixture(scope="module")
def s02_stream() -> list[StreamEvent]:
    return stream_events("S02", 3)


def _sample(s02: list[StreamEvent]) -> dict[str, StreamEvent]:
    """One event of every type: real ones from S02, built ones for the Part B types."""
    out: dict[str, StreamEvent] = {}
    for e in s02:
        out.setdefault(e.type, e)
    env: dict[str, Any] = {"run_id": "r", "seq": 1, "tick": 2, "t_s": 4.0}
    out["posterior"] = ev.PosteriorEvent(
        **env,
        payload=ev.PosteriorPayload(
            contact_id="c-0001",
            probs={h: 0.125 for h in Hypothesis},
            entropy=3.0,
            top2_margin=0.0,
            threat_mass=0.25,
            benign_mass=0.75,
        ),
    )
    out["approval_request"] = ev.ApprovalRequestEvent(
        **env,
        payload=ev.ApprovalRequestPayload(
            contact_id="c-0001",
            request_id="req-1",
            action="hold_longer",
            action_label="Hold one berth longer",
            scope={"asset_id": "fuel_berth_1"},
            recommended_rung=Rung.HAND_TO_PERSON,
            expires_t_s=120.0,
            approver="primary",
        ),
    )
    out["approval_result"] = ev.ApprovalResultEvent(
        **env, payload=ev.ApprovalResultPayload(request_id="req-1", outcome="timeout")
    )
    out["error"] = ev.ErrorEvent(**env, payload=ev.ErrorPayload(message="x"))
    # --- WO-089 begin: built when the stream has no rings (WO-089 part 1 not on the branch) ---
    out.setdefault(
        "sensor_ranges",
        ev.SensorRangesEvent(
            **env,
            payload=ev.SensorRangesPayload(
                nodes=[ev.SensorRangeRow(node_id="hyd_1", effective_range_m=400.0, nl_db=70.0)]
            ),
        ),
    )
    # --- WO-089 end ---
    return out


def test_every_type_round_trips(s02_stream: list[StreamEvent]) -> None:
    samples = _sample(s02_stream)
    assert sorted(samples) == sorted(EVENT_TYPES)
    for name, event in samples.items():
        back = parse_event(event_json(event))
        assert back == event, name
        assert event_json(back) == event_json(event)


def test_whole_stream_round_trips(s02_stream: list[StreamEvent]) -> None:
    for event in s02_stream:
        if event.type in ("tick", "evidence_row"):
            continue  # sampled above; the rest are the decisions
        assert parse_event(event_json(event)) == event


def test_unknown_fields_rejected(s02_stream: list[StreamEvent]) -> None:
    event = next(e for e in s02_stream if e.type == "transition")
    data = json.loads(event_json(event))
    parse_event(json.dumps(data))
    with pytest.raises(ValidationError):
        parse_event(json.dumps({**data, "extra": 1}))
    with pytest.raises(ValidationError):
        parse_event(json.dumps({**data, "payload": {**data["payload"], "rung_override": "x"}}))
    with pytest.raises(ValidationError):
        parse_event(json.dumps({**data, "type": "set_rung"}))
    with pytest.raises(ValidationError):
        parse_event(json.dumps({**data, "v": 2}))


def test_posterior_label_is_literal() -> None:
    payload = ev.PosteriorPayload(
        contact_id="c-0001",
        probs={Hypothesis.BIOLOGIC: 1.0},
        entropy=0.0,
        top2_margin=1.0,
        threat_mass=0.0,
        benign_mass=1.0,
    )
    assert payload.label == "stress-test estimate"
    hint = typing.get_type_hints(ev.PosteriorPayload)["label"]
    assert typing.get_args(hint) == ("stress-test estimate",)
    data = json.loads(
        event_json(PosteriorEvent(run_id="r", seq=0, tick=0, t_s=0.0, payload=payload))
    )
    data["payload"]["label"] = "probability"
    with pytest.raises(ValidationError):
        parse_event(json.dumps(data))


def test_schema_matches_committed_file() -> None:
    committed = json.loads(SCHEMA_FILE.read_text(encoding="utf-8"))
    assert committed == json.loads(json.dumps(export_json_schema())), (
        "regenerate web/src/generated/stream-events.schema.json (see WO-060 Part A section)"
    )


def test_state_fields_are_ladder_states(s02_stream: list[StreamEvent]) -> None:
    hints = typing.get_type_hints(ev.TransitionPayload)
    assert hints["to_state"] is LadderState and hints["from_state"] is LadderState
    assert typing.get_type_hints(ev.RungPayload)["state"] is LadderState
    for e in s02_stream:
        if e.type == "transition":
            assert isinstance(e.payload.to_state, LadderState)
        if e.type == "rung":
            assert isinstance(e.payload.state, LadderState)
            assert isinstance(e.payload.rung, Rung)


def test_envelope_seq_and_sim_time(s02_stream: list[StreamEvent]) -> None:
    assert [e.seq for e in s02_stream] == list(range(len(s02_stream)))
    ticks = [e.tick for e in s02_stream]
    assert ticks == sorted(ticks)
    for e in s02_stream:
        assert e.v == 1
        assert e.t_s == round(e.tick * 2.0, 6)
    assert s02_stream[0].type == "run_started"
    assert s02_stream[-1].type == "run_finished"
    assert s02_stream[-1].payload.n_events == len(s02_stream)


def test_tick_events_carry_no_ground_truth(s02_stream: list[StreamEvent]) -> None:
    fields = set(ev.TickContact.model_fields)
    assert fields == {"contact_id", "lat", "lon", "depth_m", "confidence", "sensor_id"}
    text = "".join(event_json(e) for e in s02_stream[:2000])
    assert "truth" not in text and "ground_truth" not in text
