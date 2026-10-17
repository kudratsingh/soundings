"""WO-088 part B: the responder boundary rows, guards and the position-request violation."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from soundings.ladder.boundary import BoundaryCell, load_boundary
from soundings.ladder.guards import (
    RESPONDER_LEAVE_GEOFENCE,
    RESPONDER_LOOK_ACTION,
    RESPONDER_STANDOFF_BELOW_MIN,
    SECOND_RESPONDER,
    ResponderDispatchInputs,
    responder_dispatch_permitted,
    responder_request_needs_person,
)
from soundings.llm.client import FixtureStore, ModelClient, ModelMode, fixture_key
from soundings.llm.guard import (
    VIOLATIONS_HEADER,
    ViolationCode,
    check_hypothesis_set,
    check_probe_plan,
)
from soundings.llm.roles import ROLE_PROBE_PLANNER, ProbePlannerRole
from soundings.schemas.denylist import find_denylisted
from soundings.schemas.enums import Rung
from tests.llm_cases import asset, make_ctx, make_table, threat_report

REPO = Path(__file__).resolve().parent.parent
LADDER = yaml.safe_load((REPO / "config" / "ladder.yaml").read_text(encoding="utf-8"))
TABLE = load_boundary()


def _ok(**over: Any) -> ResponderDispatchInputs:
    base: dict[str, Any] = {
        "contact_id": "c-0001",
        "station_inside_geofence": True,
        "standoff_m": 25.0,
        "other_vehicles_dispatched": 0,
        "dispatches_for_contact": 0,
    }
    base.update(over)
    return ResponderDispatchInputs(**base)


# --- boundary rows ----------------------------------------------------------------------------


def test_boundary_rows_in_ladder_yaml() -> None:
    ask = LADDER["boundary"]["ask"]
    assert RESPONDER_LOOK_ACTION in ask["platform_alone"]
    for action in (RESPONDER_LEAVE_GEOFENCE, RESPONDER_STANDOFF_BELOW_MIN, SECOND_RESPONDER):
        assert action in ask["person_approves"]
        assert TABLE.cell_of(action) is BoundaryCell.PERSON_APPROVES
        assert TABLE.rung_of(action) is Rung.ASK
    assert TABLE.cell_of(RESPONDER_LOOK_ACTION) is BoundaryCell.PLATFORM_ALONE
    assert LADDER["budgets"]["responder_dispatches_per_contact"] == 1
    # nothing after the handoff is still a person's
    assert "anything_after_handoff" in LADDER["boundary"]["hand_to_person"]["person_approves"]


def test_vocabulary_is_look_station_standoff() -> None:
    names = [
        RESPONDER_LOOK_ACTION,
        RESPONDER_LEAVE_GEOFENCE,
        RESPONDER_STANDOFF_BELOW_MIN,
        SECOND_RESPONDER,
        "responder_look",
        "responder_search_box",
    ]
    for n in names:
        assert not find_denylisted(n) and not find_denylisted(n.replace("_", " "))
    src = (REPO / "soundings" / "ladder" / "guards.py").read_text(encoding="utf-8")
    block = src[src.index("# --- WO-088 begin ---") : src.index("# --- WO-088 end ---")]
    assert find_denylisted(block) == []
    for word in ("chase", "follow", "close with"):  # the owner's vocabulary rule (A2)
        assert word not in block.lower()


# --- the guards --------------------------------------------------------------------------------


def test_look_inside_geofence_at_standoff_is_platform_alone() -> None:
    d = responder_dispatch_permitted(_ok(), TABLE)
    assert d.permits and d.reason == "platform_alone" and d.needs_person == ()
    assert responder_request_needs_person(_ok()) == ()


@pytest.mark.parametrize(
    ("over", "action"),
    [
        ({"station_inside_geofence": False}, RESPONDER_LEAVE_GEOFENCE),
        ({"station_inside_geofence": None}, RESPONDER_LEAVE_GEOFENCE),
        ({"standoff_m": 24.999}, RESPONDER_STANDOFF_BELOW_MIN),
        ({"standoff_m": 10.0}, RESPONDER_STANDOFF_BELOW_MIN),
        ({"standoff_m": None}, RESPONDER_STANDOFF_BELOW_MIN),
        ({"other_vehicles_dispatched": 1}, SECOND_RESPONDER),
        ({"other_vehicles_dispatched": None}, SECOND_RESPONDER),
    ],
)
def test_person_approves_rows_block_the_platform(over: dict[str, Any], action: str) -> None:
    inputs = _ok(**over)
    assert action in responder_request_needs_person(inputs)
    d = responder_dispatch_permitted(inputs, TABLE)
    assert not d.permits and d.reason == f"person_approves:{action}"


def test_one_dispatch_per_contact_and_nothing_after_handoff() -> None:
    d = responder_dispatch_permitted(_ok(dispatches_for_contact=1), TABLE)
    assert not d.permits and d.reason == "dispatch_budget"
    d = responder_dispatch_permitted(_ok(dispatches_for_contact=None), TABLE)
    assert not d.permits and d.reason.startswith("unknown_input:")
    d = responder_dispatch_permitted(_ok(handed_off=True), TABLE)
    assert not d.permits and d.reason == "after_handoff"


def test_guard_is_deterministic() -> None:
    a = responder_dispatch_permitted(_ok(standoff_m=25.0000001), TABLE)
    b = responder_dispatch_permitted(_ok(standoff_m=25.0), TABLE)
    assert a == b  # 6 dp inputs hash equal


# --- RESPONDER_POSITION_REQUEST -----------------------------------------------------------------


def _plan(**over: Any) -> dict[str, Any]:
    raw: dict[str, Any] = {
        "next_probe": "camera_cue",
        "why": "A look separates the top two.",
        "hypotheses_to_discriminate": ["UNAUTHORIZED_UUV", "FILED_OFF_PLAN"],
        "question_for_human": None,
    }
    raw.update(over)
    return raw


@pytest.mark.parametrize(
    "raw",
    [
        _plan(standoff_m=10),
        _plan(position={"lat": 33.74, "lon": -118.27}),
        _plan(geofence_zone_ids=["outer_harbor"]),
        _plan(why="Send it to 33.741200, -118.269100 for a better view."),
        _plan(why="Reduce the standoff to 10 m for a sharper look."),
        _plan(why="Let it leave the geofence to look past the breakwater."),
    ],
)
def test_position_request_rejected(raw: dict[str, Any]) -> None:
    res = check_probe_plan(raw, make_ctx())
    assert not res.accepted and res.violation is ViolationCode.RESPONDER_POSITION_REQUEST


def test_hypothesis_writer_position_request_rejected() -> None:
    raw = {
        "hypotheses": [{"id": "UNAUTHORIZED_UUV", "why": "Closing on the berth."}],
        "question_for_human": "Can a person set the standoff to 5 m?",
    }
    res = check_hypothesis_set(raw)
    assert res.violation is ViolationCode.RESPONDER_POSITION_REQUEST


def test_plain_look_text_is_fine() -> None:
    raw = _plan(why="The responder looks from its station at standoff.")
    assert check_probe_plan(raw, make_ctx()).accepted


def test_hand_written_violating_fixture_rejected_and_logged(tmp_path: Path) -> None:
    """A recorded reply asking for a closer standoff is rejected and logged in the
    VIOLATIONS.md format; the deterministic fallback is used instead."""
    ctx = make_ctx(tick=4242)
    asset_context = asset(355.0)
    root = tmp_path / "fixtures"
    store = FixtureStore(root)
    from soundings.llm.roles import build_probe_planner_inputs

    inputs = build_probe_planner_inputs(make_table(), threat_report(), ctx, asset_context)
    store.save(
        ROLE_PROBE_PLANNER,
        inputs,
        {
            "next_probe": "camera_cue",
            "why": "Bring the responder to 10 m so the camera sees it.",
            "hypotheses_to_discriminate": ["UNAUTHORIZED_UUV", "UNAUTHORIZED_DIVER"],
            "question_for_human": None,
            "standoff_m": 10.0,
        },
    )
    log = tmp_path / "VIOLATIONS.md"
    role = ProbePlannerRole(ModelClient(mode=ModelMode.REPLAY, store=store), violations_path=log)
    outcome = role.propose(make_table(), threat_report(), ctx, asset_context, when="wo088/t=4242")
    assert outcome.source == "fallback"
    assert outcome.violation is ViolationCode.RESPONDER_POSITION_REQUEST
    text = log.read_text(encoding="utf-8")
    assert text.startswith(VIOLATIONS_HEADER)
    row = text[len(VIOLATIONS_HEADER) :].strip()
    cells = [c.strip() for c in row.strip("|").split(" | ")]
    assert cells[0] == "wo088/t=4242" and cells[1] == ROLE_PROBE_PLANNER
    assert cells[2] == fixture_key(ROLE_PROBE_PLANNER, inputs)
    assert cells[3].startswith("RESPONDER_POSITION_REQUEST")
