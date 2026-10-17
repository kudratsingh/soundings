"""WO-088 part C end to end: the real responder sim (part A) through the stream builder.

Skipped until ``soundings.sim.responder`` is on main; then it drives one dispatch to look
at a still contact in the main channel and checks the events the UI would receive.
"""

from __future__ import annotations

import pytest

from soundings.schemas.harbor import load_harbor
from soundings.stream.events import ResponderEvent, event_json, parse_event
from soundings.stream.responder import config_defaults, responder_payloads

sim = pytest.importorskip("soundings.sim.responder")

CONTACT = (33.7300, -118.2610)  # inside the main channel (a declared zone)
TICK_S = 2.0


def _trail() -> list[object]:
    responder = sim.Responder(sim.load_responder_config(), load_harbor(), tick_s=TICK_S)
    result = responder.dispatch_to_look("c-0001", CONTACT[0], CONTACT[1], 0)
    assert result.accepted, result.reason
    snaps = []
    for tick in range(1, 900):
        snaps.append(
            responder.step(
                tick, contact_lat=CONTACT[0], contact_lon=CONTACT[1], contact_speed_mps=0.0
            )
        )
    return snaps


def test_real_responder_streams_look_at_standoff() -> None:
    snaps = _trail()
    payloads = responder_payloads(snaps)
    rid, capacity = config_defaults()
    states = [p.state for _, p in sorted(payloads.items())]
    assert "TRANSIT" in states and "ON_STATION" in states
    for tick, p in sorted(payloads.items()):
        assert p.id == rid and p.battery_capacity_min == capacity
        assert p.standoff_m >= 25.0
        if p.state == "ON_STATION" and p.distance_to_target_m is not None:
            assert p.distance_to_target_m >= p.standoff_m - 0.5, tick
        event = ResponderEvent(run_id="r", seq=tick, tick=tick, t_s=tick * TICK_S, payload=p)
        assert parse_event(event_json(event)) == event


def test_real_responder_stream_is_deterministic() -> None:
    a = responder_payloads(_trail())
    b = responder_payloads(_trail())
    assert a == b
