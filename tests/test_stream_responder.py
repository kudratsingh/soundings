"""WO-088 part C: the ``responder`` stream event (one per tick when a run has a responder).

The snapshots here come from a stub trail with the contract's ``ResponderSnapshot`` fields,
so the stream is tested without the sim; ``test_stream_responder_sim`` (when the sim is on
main) drives the real responder through the same path.
"""

from __future__ import annotations

import json
import math
from collections.abc import Iterator
from dataclasses import dataclass
from enum import StrEnum

import pytest
from pydantic import ValidationError

from soundings.runner import RunResult
from soundings.schemas.scenario import Scenario
from soundings.stream import events as ev
from soundings.stream.engine import batch_events, stream_events
from soundings.stream.events import ResponderPayload, event_json, parse_event
from soundings.stream.responder import (
    TRAIL_POINTS,
    ResponderSnapshotLike,
    register_responder_provider,
    responder_payloads,
    state_name,
    unregister_responder_provider,
)

# The contract's payload fields (WO-088 interface), plus the ones part C adds for the UI.
CONTRACT_FIELDS = {
    "id",
    "state",
    "lat",
    "lon",
    "heading_deg",
    "battery_min",
    "standoff_m",
    "target_contact_id",
    "trail",
}
UI_FIELDS = {"speed_mps", "battery_capacity_min", "distance_to_target_m", "dispatched"}


class StubState(StrEnum):
    DOCKED = "docked"
    TRANSIT = "transit"
    ON_STATION = "on_station"
    RETURNING = "returning"


@dataclass(frozen=True)
class StubSnapshot:
    tick: int
    t_s: float
    lat: float
    lon: float
    heading_deg: float
    speed_mps: float
    battery_min: float
    state: StubState
    target_contact_id: str | None
    standoff_m: float
    distance_to_target_m: float | None


DOCK = (33.7330, -118.2700)
CONTACT = (33.7360, -118.2620)
M_PER_DEG = 111_320.0


def stub_trail(n_ticks: int = 120, dispatch_tick: int = 5) -> tuple[StubSnapshot, ...]:
    """Docked, dispatched to look, on station at 25 m standoff (orbit), return to dock."""
    out: list[StubSnapshot] = []
    coslat = math.cos(math.radians(CONTACT[0]))
    battery = 90.0
    station_from = dispatch_tick + 20
    return_from = station_from + 60
    for tick in range(n_ticks):
        if tick < dispatch_tick:
            state, pos, target = StubState.DOCKED, DOCK, None
        elif tick < station_from:
            f = (tick - dispatch_tick) / (station_from - dispatch_tick)
            pos = (DOCK[0] + f * (CONTACT[0] - DOCK[0]), DOCK[1] + f * (CONTACT[1] - DOCK[1]))
            state, target = StubState.TRANSIT, "c-0001"
        elif tick < return_from:
            ang = 2 * math.pi * (tick - station_from) / 30
            pos = (
                CONTACT[0] + 25.0 * math.sin(ang) / M_PER_DEG,
                CONTACT[1] + 25.0 * math.cos(ang) / (M_PER_DEG * coslat),
            )
            state, target = StubState.ON_STATION, "c-0001"
        else:
            state, pos, target = StubState.RETURNING, DOCK, None
        if state is not StubState.DOCKED:
            battery -= 0.05
        dist = None
        if target is not None:
            dy = (pos[0] - CONTACT[0]) * M_PER_DEG
            dx = (pos[1] - CONTACT[1]) * M_PER_DEG * coslat
            dist = math.hypot(dx, dy)
        out.append(
            StubSnapshot(
                tick=tick,
                t_s=tick * 2.0,
                lat=pos[0],
                lon=pos[1],
                heading_deg=(tick * 7.0) % 360.0,
                speed_mps=0.0 if state is StubState.DOCKED else 1.5,
                battery_min=battery,
                state=state,
                target_contact_id=target,
                standoff_m=25.0,
                distance_to_target_m=dist,
            )
        )
    return tuple(out)


@pytest.fixture()
def stub_provider() -> Iterator[None]:
    def provider(result: RunResult, scenario: Scenario) -> tuple[StubSnapshot, ...] | None:
        return stub_trail() if scenario.id == "S05" else None

    register_responder_provider(provider)
    try:
        yield
    finally:
        unregister_responder_provider(provider)


def test_payload_fields_cover_the_contract() -> None:
    fields = set(ResponderPayload.model_fields)
    assert CONTRACT_FIELDS <= fields
    assert fields == CONTRACT_FIELDS | UI_FIELDS


def test_payload_rejects_unknown_fields_and_states() -> None:
    good = responder_payloads(stub_trail(10))[9].model_dump(mode="json")
    ResponderPayload.model_validate(good)
    with pytest.raises(ValidationError):
        ResponderPayload.model_validate({**good, "goto_lat": 1.0})
    with pytest.raises(ValidationError):
        ResponderPayload.model_validate({**good, "state": "CLOSING"})


def test_state_names() -> None:
    assert state_name(StubState.ON_STATION) == "ON_STATION"
    assert state_name("transit") == "TRANSIT"
    assert state_name("ResponderState.RETURNING") == "RETURNING"
    with pytest.raises(ValueError):
        state_name("ELSEWHERE")


def test_one_payload_per_tick_with_a_bounded_trail() -> None:
    snaps = stub_trail()
    payloads = responder_payloads(snaps)
    assert sorted(payloads) == [s.tick for s in snaps]
    for snap in snaps:
        p = payloads[snap.tick]
        assert 1 <= len(p.trail) <= TRAIL_POINTS
        assert len(p.trail) == min(snap.tick + 1, TRAIL_POINTS)
        assert (p.trail[-1].tick, p.trail[-1].lat, p.trail[-1].lon) == (snap.tick, p.lat, p.lon)
        assert [pt.tick for pt in p.trail] == sorted(pt.tick for pt in p.trail)
        assert p.id == "resp_1" and p.standoff_m == 25.0
        assert 0.0 <= p.heading_deg < 360.0


def test_dispatch_cue_once_per_dispatch() -> None:
    payloads = responder_payloads(stub_trail(dispatch_tick=5))
    cues = [t for t, p in sorted(payloads.items()) if p.dispatched]
    assert cues == [5]
    assert payloads[5].state == "TRANSIT" and payloads[5].target_contact_id == "c-0001"


def test_stub_holds_standoff() -> None:
    # The stub is a fair stand-in for the sim: on station it never comes inside 25 m.
    for p in responder_payloads(stub_trail()).values():
        if p.state == "ON_STATION":
            assert p.distance_to_target_m is not None
            assert p.distance_to_target_m >= p.standoff_m - 1e-3


def test_no_responder_no_events() -> None:
    events = stream_events("S02", 3)
    assert not any(e.type == "responder" for e in events)


def test_batch_events_interleave_after_each_tick(stub_provider: None) -> None:
    events = stream_events("S05", 1)
    assert [e.seq for e in events] == list(range(len(events)))
    assert events[-1].type == "run_finished" and events[-1].payload.n_events == len(events)
    responders = [e for e in events if e.type == "responder"]
    assert [e.tick for e in responders] == list(range(len(stub_trail())))
    by_seq = {e.seq: e for e in events}
    for r in responders:
        # The tick's own ``tick`` event comes first (per-tick overlays such as WO-089's
        # ``sensor_ranges`` may sit between), and nothing from a later tick.
        k = r.seq - 1
        while by_seq[k].type == "sensor_ranges":
            k -= 1
        before = by_seq[k]
        assert before.type == "tick" and before.tick == r.tick
        assert r.t_s == round(r.tick * 2.0, 6)
        assert parse_event(event_json(r)) == r


def test_stream_is_deterministic(stub_provider: None) -> None:
    a = [event_json(e) for e in stream_events("S05", 1)]
    b = [event_json(e) for e in stream_events("S05", 1)]
    assert a == b


def test_batch_events_keyword_is_optional() -> None:
    # Existing callers (no responder) get the same stream as before WO-088.
    from soundings.runner import ScenarioRunner
    from soundings.sim.scenario import resolve_scenario

    sc = resolve_scenario("S02")
    result = ScenarioRunner(sc, seed=3).run()
    plain = batch_events(result, sc, run_id="x", speed=0)
    explicit = batch_events(result, sc, run_id="x", speed=0, responder=None)
    assert [event_json(e) for e in plain] == [event_json(e) for e in explicit]


def test_protocol_matches_the_stub() -> None:
    snap: ResponderSnapshotLike = stub_trail(1)[0]
    assert snap.tick == 0


def test_payload_json_has_no_ground_truth() -> None:
    text = json.dumps(
        [p.model_dump(mode="json") for p in responder_payloads(stub_trail()).values()]
    )
    assert "truth" not in text
    assert ev.ResponderEvent.model_fields["type"].default == "responder"
