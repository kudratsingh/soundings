"""WO-041: the boundary matrix, the criteria edges, purity, and the hard-rule proofs.

BUILD-PLAN Phase 4 boundary-matrix items, verbatim:
- a ``hold`` on one gate <= 15 min under signed standing orders executes without a human;
- a ``hold`` > 15 min or on > 1 gate blocks until approval;
- with ``standing_orders.signed=false`` nothing above ``ask`` executes;
- approver unreachable -> system stays at the highest pre-authorized rung and escalates
  to the backup approver, never auto-climbs.

Every threshold and scope number is read from ``config/ladder.yaml`` or
``config/standing_orders.yaml``; none is a literal here (CLAUDE.md section 3).
"""

from __future__ import annotations

import ast
import random
import re
import types
import typing
from collections.abc import Callable
from enum import Enum
from pathlib import Path
from typing import Any, Literal

import pytest
import yaml
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import BaseModel
from pydantic.fields import FieldInfo

from soundings.ladder.approval import ApprovalToken, issue_token
from soundings.ladder.boundary import ABSOLUTE_NEVER, NEVER_ACTIONS, BoundaryCell, load_boundary
from soundings.ladder.guards import (
    ApproverStatus,
    GuardInputs,
    approval_token_valid_and_scope_matches,
    backup_exists,
    boundary_decision,
    exceeds_scope,
    hold_permitted,
    no_backup,
    nothing_preauthorized,
    scope_exceeded,
    slow_permitted,
)
from soundings.ladder.standing_orders import (
    StandingOrders,
    StandingScope,
    read_document,
)
from soundings.ladder.types import ActionScope, GuardDecision, GuardVerdict
from soundings.schemas import Rung, ToggleName
from soundings.schemas.config import LADDER_PATH, STANDING_ORDERS_PATH
from soundings.schemas.denylist import DENYLIST

REPO = Path(__file__).resolve().parent.parent
LADDER_YAML: dict[str, Any] = yaml.safe_load(LADDER_PATH.read_text(encoding="utf-8"))
ORDERS_YAML: dict[str, Any] = yaml.safe_load(STANDING_ORDERS_PATH.read_text(encoding="utf-8"))
SLOW_C = LADDER_YAML["criteria"]["slow"]
HOLD_C = LADDER_YAML["criteria"]["hold"]
HOLD_SCOPE_Y = ORDERS_YAML["orders"]["scope"]["hold"]
SLOW_SCOPE_Y = ORDERS_YAML["orders"]["scope"]["slow"]

TABLE = load_boundary()
CRIT = TABLE.criteria
SCOPE = StandingScope.from_orders(StandingOrders.from_mapping(read_document()["orders"]))

CID = "c-0005"
BASIN = SLOW_SCOPE_Y["basins"][0]
ASSET = "fuel_berth_1"
NOW = 1000.0
EPS = 1e-6

HOLD_ACTION = LADDER_YAML["boundary"]["hold"]["standing_orders"][0]
SLOW_ACTION = LADDER_YAML["boundary"]["slow"]["standing_orders"][0]
HOLD_OK = ActionScope(
    action=HOLD_ACTION, asset_id=ASSET, gates=1, max_minutes=HOLD_SCOPE_Y["max_minutes"]
)
SLOW_OK = ActionScope(action=SLOW_ACTION, basin=BASIN, max_minutes=SLOW_SCOPE_Y["max_minutes"])

PureGuard = Callable[[GuardInputs, Any], GuardDecision]
PURE_GUARDS: tuple[PureGuard, ...] = (
    slow_permitted,
    hold_permitted,
    nothing_preauthorized,
    scope_exceeded,
    approval_token_valid_and_scope_matches,
    backup_exists,
    no_backup,
)
AUTHORIZING: tuple[PureGuard, ...] = (
    slow_permitted,
    hold_permitted,
    scope_exceeded,  # permit = within the standing scope
    approval_token_valid_and_scope_matches,
    backup_exists,
)
"""Guards whose permit grants something; ``nothing_preauthorized`` / ``no_backup`` are
their complements (a permit there means 'do not climb')."""


def base(**over: Any) -> GuardInputs:
    """Inputs that meet every slow and hold criterion with margin, under signed orders."""
    fields: dict[str, Any] = {
        "contact_id": CID,
        "now_s": NOW,
        "threat_mass": max(SLOW_C["threat_mass_min"], HOLD_C["threat_mass_min"]) * 2,
        "benign_mass": 0.1,
        "time_to_nearest_critical_asset_s": HOLD_C["time_to_critical_asset_max_s"] / 2,
        "zone_criticality": (1 + HOLD_C["zone_criticality_min"]) / 2,
        "standing_orders_signed": True,
        "standing_orders_scope": SCOPE,
        "active_holds": HOLD_C["active_holds_max"],
        "approver_reachable": True,
        "basin": BASIN,
        "nearest_asset_id": ASSET,
    }
    fields.update(over)
    return GuardInputs(**fields)


def token_for(scope: ActionScope, *, contact_id: str = CID, ttl: float = 600.0) -> ApprovalToken:
    return issue_token(
        contact_id=contact_id,
        scope=scope,
        approver="primary",
        channel="ui",
        issued_at=NOW - 10.0,
        expires_at=NOW - 10.0 + ttl,
    )


def scope_for(action: str) -> ActionScope:
    """A request for ``action`` inside the standing scope where the action has one."""
    if action == HOLD_ACTION:
        return HOLD_OK
    if action == SLOW_ACTION:
        return SLOW_OK
    rung = TABLE.rung_of(action)
    if rung is Rung.HOLD:
        return HOLD_OK.model_copy(update={"action": action})
    if rung is Rung.SLOW:
        return SLOW_OK.model_copy(update={"action": action})
    return ActionScope(action=action)


# --- the BUILD-PLAN boundary matrix ---------------------------------------------------------


def test_hold_one_gate_within_limit_under_signed_orders_executes_without_human() -> None:
    inputs = base()
    assert inputs.approval_token is None  # nobody asked, nobody approved
    d = hold_permitted(inputs, CRIT)
    assert d.permits, d.reason
    assert d.granted_scope == HOLD_OK
    assert d.granted_scope.gates == 1 == HOLD_SCOPE_Y["max_gates"]
    assert d.granted_scope.max_minutes == HOLD_SCOPE_Y["max_minutes"]
    bd = boundary_decision(HOLD_ACTION, base(requested=HOLD_OK), TABLE)
    assert bd.cell is BoundaryCell.STANDING_ORDERS
    assert bd.permits, bd.reason
    assert not exceeds_scope(base(requested=HOLD_OK), CRIT)


LONGER = HOLD_OK.model_copy(update={"max_minutes": HOLD_SCOPE_Y["max_minutes"] + 1})
WIDER = HOLD_OK.model_copy(update={"gates": HOLD_SCOPE_Y["max_gates"] + 1})


@pytest.mark.parametrize(
    ("excess", "person_action", "why"),
    [(LONGER, "hold_longer", "minutes"), (WIDER, "hold_wider", "gates")],
    ids=["longer", "wider"],
)
def test_hold_longer_or_wider_blocks_until_approval(
    excess: ActionScope, person_action: str, why: str
) -> None:
    # The standing-orders action cannot stretch past its scope, token or not.
    for token in (None, token_for(excess)):
        bd = boundary_decision(HOLD_ACTION, base(requested=excess, approval_token=token), TABLE)
        assert not bd.permits
        assert bd.reason == f"scope_exceeded:{why}"
    assert exceeds_scope(base(requested=excess), CRIT)
    # The person-approved action with the same numbers waits for a token...
    asked = excess.model_copy(update={"action": person_action})
    assert TABLE.cell_of(person_action) is BoundaryCell.PERSON_APPROVES
    blocked = boundary_decision(person_action, base(requested=asked), TABLE)
    assert not blocked.permits
    assert blocked.reason == "unknown_input:approval_token"
    # ...and executes once a person approves exactly that scope.
    ok = boundary_decision(
        person_action, base(requested=asked, approval_token=token_for(asked)), TABLE
    )
    assert ok.permits, ok.reason
    assert ok.granted_scope == asked


def test_second_hold_blocks_until_approval() -> None:
    busy = HOLD_C["active_holds_max"] + 1
    assert not hold_permitted(base(active_holds=busy), CRIT).permits
    assert scope_exceeded(base(active_holds=busy, requested=HOLD_OK), CRIT).reason == (
        "scope_exceeded:second_hold"
    )
    second = HOLD_OK.model_copy(update={"action": "second_hold"})
    assert not boundary_decision(
        "second_hold", base(active_holds=busy, requested=second), TABLE
    ).permits
    ok = boundary_decision(
        "second_hold",
        base(active_holds=busy, requested=second, approval_token=token_for(second)),
        TABLE,
    )
    assert ok.permits


def _above_ask(action: str) -> bool:
    return TABLE.rung_of(action) in (Rung.SLOW, Rung.HOLD)


def test_unsigned_nothing_above_ask_executes() -> None:
    for signed, scope in ((False, SCOPE), (True, None), (False, None)):
        inputs = base(standing_orders_signed=signed, standing_orders_scope=scope)
        assert not slow_permitted(inputs, CRIT).permits
        assert not hold_permitted(inputs, CRIT).permits
        nothing = nothing_preauthorized(inputs, CRIT)
        assert nothing.permits and nothing.reason == "standing_orders_unsigned"
        assert not backup_exists(inputs, CRIT).permits
        for action in TABLE.actions():
            req = scope_for(action)
            bd = boundary_decision(
                action,
                base(standing_orders_signed=signed, standing_orders_scope=scope, requested=req),
                TABLE,
            )
            if _above_ask(action) or action in ABSOLUTE_NEVER:
                assert not bd.permits, action
            if TABLE.rung_of(action) in (Rung.WATCH, Rung.ASK):
                assert bd.permits, action  # watch and ask stay with the platform


def test_approver_unreachable_stays_at_highest_preauthorized_rung_and_pages_backup() -> None:
    status = ApproverStatus.from_toggles([ToggleName.APPROVER_UNREACHABLE])
    assert not status.reachable
    reasons = status.degraded_reasons()
    assert reasons == (ToggleName.APPROVER_UNREACHABLE,)
    inputs = base(approver_reachable=False, degraded_reasons=reasons)
    # Hold is the highest pre-authorized rung and stays allowed in degraded mode.
    assert hold_permitted(inputs, CRIT).permits
    # Nothing a person must approve executes; nothing on the never list either.
    for action in TABLE.actions():
        cell = TABLE.cell_of(action)
        if cell in (BoundaryCell.PERSON_APPROVES, BoundaryCell.NEVER):
            req = scope_for(action)
            d = boundary_decision(
                action,
                base(approver_reachable=False, degraded_reasons=reasons, requested=req),
                TABLE,
            )
            assert not d.permits, action
    # Escalate to the backup first; once paged, release at the limit ("unreachable").
    assert backup_exists(inputs, CRIT).permits
    assert not no_backup(inputs, CRIT).permits
    paged = base(approver_reachable=False, degraded_reasons=reasons, backup_paged=True)
    assert not backup_exists(paged, CRIT).permits
    released = no_backup(paged, CRIT)
    assert released.permits and released.reason == "unreachable"
    # The pending request stays the hold's own scope: never auto-climbs to a wider one.
    longer = HOLD_OK.model_copy(update={"action": "hold_longer"})
    assert not approval_token_valid_and_scope_matches(
        base(degraded_reasons=reasons, requested=longer), CRIT
    ).permits


def test_degraded_mode_keeps_only_allowed_rungs() -> None:
    mapping = read_document()["orders"]
    narrowed = {
        **mapping,
        "degraded_mode": {**mapping["degraded_mode"], "allowed_rungs": ["watch", "ask"]},
    }
    scope = StandingScope.from_orders(StandingOrders.from_mapping(narrowed))
    for reason in scope.degraded_when:
        inputs = base(standing_orders_scope=scope, degraded_reasons=(reason,))
        assert not slow_permitted(inputs, CRIT).permits
        assert not hold_permitted(inputs, CRIT).permits
        assert nothing_preauthorized(inputs, CRIT).permits
    # A reason outside degraded_mode.when narrows nothing.
    other = base(standing_orders_scope=scope, degraded_reasons=(ToggleName.FOG_NO_CAMERA,))
    assert hold_permitted(other, CRIT).permits


# --- the matrix from config -----------------------------------------------------------------

YAML_CELLS = ("platform_alone", "standing_orders", "person_approves")
MATRIX = [
    (rung, action, cell)
    for rung, block in LADDER_YAML["boundary"].items()
    for cell in YAML_CELLS
    for action in block.get(cell, [])
]


def expected_cell(action: str, yaml_cell: str) -> BoundaryCell:
    """The YAML's cell, except the absolute never list (CLAUDE.md 2.1)."""
    return BoundaryCell.NEVER if action in ABSOLUTE_NEVER else BoundaryCell(yaml_cell)


@pytest.mark.parametrize("with_token", [False, True], ids=["no_token", "token"])
@pytest.mark.parametrize("signed", [False, True], ids=["unsigned", "signed"])
@pytest.mark.parametrize(
    ("rung", "action", "yaml_cell"), MATRIX, ids=[f"{r}:{a}" for r, a, _ in MATRIX]
)
def test_matrix_from_config(
    rung: str, action: str, yaml_cell: str, signed: bool, with_token: bool
) -> None:
    cell = expected_cell(action, yaml_cell)
    assert TABLE.cell_of(action) is cell
    assert TABLE.rung_of(action) is Rung(rung)
    req = scope_for(action)
    inputs = base(
        standing_orders_signed=signed,
        requested=req,
        approval_token=token_for(req) if with_token else None,
    )
    d = boundary_decision(action, inputs, TABLE)
    assert d.cell is cell
    if cell is BoundaryCell.PLATFORM_ALONE:
        expected = True
    elif cell is BoundaryCell.STANDING_ORDERS:
        expected = signed  # base() is in scope and meets every criterion
    elif cell is BoundaryCell.PERSON_APPROVES:
        expected = with_token
    else:
        expected = False
    assert d.permits is expected, d.reason


def test_matrix_covers_every_yaml_action() -> None:
    assert len(MATRIX) == len(TABLE.actions()) == len(set(TABLE.actions()))
    assert [a for _, a, _ in MATRIX] == list(TABLE.actions())
    rows = TABLE.rows()
    assert {r["action"]: r["yaml_cell"] for r in rows} == {a: c for _, a, c in MATRIX}


def test_standing_orders_cell_needs_criteria_and_scope() -> None:
    low = base(threat_mass=0.0, requested=HOLD_OK)
    assert not boundary_decision(HOLD_ACTION, low, TABLE).permits
    away = base(basin="outer_harbor", requested=None)
    assert not boundary_decision(SLOW_ACTION, away, TABLE).permits
    assert boundary_decision(SLOW_ACTION, base(), TABLE).permits
    assert not boundary_decision("not_an_action", base(), TABLE).permits


# --- criteria edges (thresholds from config, never literals) ---------------------------------


def _slow(**over: Any) -> bool:
    fields = {
        "threat_mass": SLOW_C["threat_mass_min"],
        "time_to_nearest_critical_asset_s": SLOW_C["time_to_critical_asset_max_s"] - 1,
    }
    fields.update(over)
    return slow_permitted(base(**fields), CRIT).permits


def _hold(**over: Any) -> bool:
    fields = {
        "threat_mass": HOLD_C["threat_mass_min"],
        "time_to_nearest_critical_asset_s": HOLD_C["time_to_critical_asset_max_s"] - 1,
        "zone_criticality": HOLD_C["zone_criticality_min"],
        "active_holds": HOLD_C["active_holds_max"],
    }
    fields.update(over)
    return hold_permitted(base(**fields), CRIT).permits


def test_criteria_edges() -> None:
    # slow: threat_mass >= min (0.25)
    t = SLOW_C["threat_mass_min"]
    assert _slow(threat_mass=t) and _slow(threat_mass=t + EPS) and not _slow(threat_mass=t - EPS)
    # slow: time < max (600 s), strict
    m = SLOW_C["time_to_critical_asset_max_s"]
    key = "time_to_nearest_critical_asset_s"
    assert _slow(**{key: m - EPS}) and not _slow(**{key: m}) and not _slow(**{key: m + EPS})
    # hold: threat_mass >= min (0.35)
    t = HOLD_C["threat_mass_min"]
    assert _hold(threat_mass=t) and _hold(threat_mass=t + EPS) and not _hold(threat_mass=t - EPS)
    # hold: time < max (300 s), strict
    m = HOLD_C["time_to_critical_asset_max_s"]
    assert _hold(**{key: m - EPS}) and not _hold(**{key: m}) and not _hold(**{key: m + EPS})
    # hold: zone_criticality >= min (0.7)
    c = HOLD_C["zone_criticality_min"]
    assert _hold(zone_criticality=c) and _hold(zone_criticality=c + EPS)
    assert not _hold(zone_criticality=c - EPS)
    # hold: active_holds <= max (0): 0 permits, 1 blocks
    a = HOLD_C["active_holds_max"]
    assert _hold(active_holds=a) and not _hold(active_holds=a + 1)
    # The loaded criteria are exactly the YAML numbers.
    assert CRIT.slow.threat_mass_min == SLOW_C["threat_mass_min"]
    assert CRIT.slow.time_to_critical_asset_max_s == SLOW_C["time_to_critical_asset_max_s"]
    assert CRIT.hold.threat_mass_min == HOLD_C["threat_mass_min"]
    assert CRIT.hold.time_to_critical_asset_max_s == HOLD_C["time_to_critical_asset_max_s"]
    assert CRIT.hold.zone_criticality_min == HOLD_C["zone_criticality_min"]
    assert CRIT.hold.active_holds_max == HOLD_C["active_holds_max"]


@pytest.mark.parametrize(
    ("guard", "field"),
    [
        (slow_permitted, "threat_mass"),
        (slow_permitted, "time_to_nearest_critical_asset_s"),
        (slow_permitted, "basin"),
        (hold_permitted, "threat_mass"),
        (hold_permitted, "time_to_nearest_critical_asset_s"),
        (hold_permitted, "zone_criticality"),
        (hold_permitted, "active_holds"),
        (hold_permitted, "nearest_asset_id"),
        (approval_token_valid_and_scope_matches, "approval_token"),
        (approval_token_valid_and_scope_matches, "requested"),
        (scope_exceeded, "requested"),
    ],
)
def test_criteria_edges_none_input_blocks(guard: PureGuard, field: str) -> None:
    complete = base(requested=HOLD_OK, approval_token=token_for(HOLD_OK))
    assert guard(complete, CRIT).permits
    d = guard(complete.model_copy(update={field: None}), CRIT)
    assert d.verdict is GuardVerdict.BLOCK
    assert d.reason == f"unknown_input:{field}"


# --- purity -----------------------------------------------------------------------------------

REQUESTS = [None, HOLD_OK, LONGER, WIDER, SLOW_OK, ActionScope(action="hold_longer")]
TOKENS = [None, token_for(HOLD_OK), token_for(LONGER), token_for(HOLD_OK, contact_id="c-0009")]
unit = st.one_of(st.none(), st.floats(0.0, 1.0, allow_nan=False))


@st.composite
def guard_inputs(draw: st.DrawFn) -> dict[str, Any]:
    return {
        "contact_id": CID,
        "now_s": draw(st.floats(0.0, 2000.0, allow_nan=False)),
        "threat_mass": draw(unit),
        "benign_mass": draw(unit),
        "time_to_nearest_critical_asset_s": draw(
            st.one_of(st.none(), st.floats(0.0, 1200.0, allow_nan=False))
        ),
        "zone_criticality": draw(unit),
        "standing_orders_signed": draw(st.booleans()),
        "standing_orders_scope": draw(st.sampled_from([None, SCOPE])),
        "active_holds": draw(st.one_of(st.none(), st.integers(0, 2))),
        "approver_reachable": draw(st.one_of(st.none(), st.booleans())),
        "approval_token": draw(st.sampled_from(TOKENS)),
        "requested": draw(st.sampled_from(REQUESTS)),
        "basin": draw(st.sampled_from([None, BASIN, "outer_harbor"])),
        "nearest_asset_id": draw(st.sampled_from([None, ASSET])),
        "degraded_reasons": tuple(draw(st.lists(st.sampled_from(list(ToggleName)), max_size=3))),
        "backup_paged": draw(st.booleans()),
    }


@settings(max_examples=150, deadline=None)
@given(
    fields=guard_inputs(), seed=st.integers(0, 2**32 - 1), extra=st.sampled_from(list(ToggleName))
)
def test_purity(fields: dict[str, Any], seed: int, extra: ToggleName) -> None:
    inputs = GuardInputs(**fields)
    keys = list(fields)
    random.Random(seed).shuffle(keys)
    shuffled = {k: fields[k] for k in keys}
    shuffled["degraded_reasons"] = tuple(reversed(fields["degraded_reasons"]))
    again = GuardInputs(**shuffled)
    assert again == inputs
    degraded = inputs.model_copy(
        update={"degraded_reasons": tuple(sorted(set(inputs.degraded_reasons) | {extra}))}
    )
    plain = inputs.model_copy(update={"degraded_reasons": ()})
    for guard in PURE_GUARDS:
        first = guard(inputs, CRIT)
        assert guard(inputs, CRIT) == first
        assert guard(again, CRIT) == first
        assert first.inputs_hash == guard(again, CRIT).inputs_hash
    # degraded_reasons only remove permissions: a permit with more reasons implies a permit
    # with fewer.
    for guard in AUTHORIZING:
        if guard(degraded, CRIT).permits:
            assert guard(inputs, CRIT).permits, guard.__name__
        if guard(inputs, CRIT).permits:
            assert guard(plain, CRIT).permits, guard.__name__
    for action in TABLE.actions():
        if boundary_decision(action, degraded, TABLE).permits:
            assert boundary_decision(action, inputs, TABLE).permits, action
        assert boundary_decision(action, inputs, TABLE) == boundary_decision(action, again, TABLE)
    # The complements stay complements.
    assert nothing_preauthorized(inputs, CRIT).permits is not (
        slow_permitted(inputs, CRIT).permits or hold_permitted(inputs, CRIT).permits
    )
    assert no_backup(inputs, CRIT).permits is not backup_exists(inputs, CRIT).permits


# --- hard-rule proofs -------------------------------------------------------------------------

GUARD_MODULES = ("boundary.py", "standing_orders.py", "approval.py", "guards.py", "approver_sim.py")


def _imports(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out.extend(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if node.level:  # relative: resolve against soundings.ladder
                parent = ["soundings", "ladder"][: max(0, 3 - node.level)]
                mod = ".".join([*parent, mod] if mod else parent)
            out.append(mod)
            out.extend(f"{mod}.{a.name}" for a in node.names)
    return out


def _free_text(annotation: Any, meta: list[Any], seen: set[type]) -> list[str]:
    """Every string-typed spot in ``annotation`` that carries no pattern (free text)."""
    origin = typing.get_origin(annotation)
    if origin is typing.Annotated:
        inner, *extra = typing.get_args(annotation)
        return _free_text(inner, [*meta, *extra], seen)
    if origin in (typing.Union, types.UnionType, tuple, list):
        return [
            p
            for a in typing.get_args(annotation)
            if a is not Ellipsis
            for p in _free_text(a, [], seen)
        ]
    if origin is Literal:
        return []
    if origin is not None:  # dict and the like: arbitrary content
        return [repr(annotation)]
    if annotation is str:
        patterned = any(
            getattr(m, "pattern", None)
            or (isinstance(m, FieldInfo) and any(getattr(x, "pattern", None) for x in m.metadata))
            for m in meta
        )
        return [] if patterned else ["str"]
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        if annotation in seen:
            return []
        seen.add(annotation)
        return [
            f"{annotation.__name__}.{name}:{p}"
            for name, f in annotation.model_fields.items()
            for p in _free_text(f.annotation, list(f.metadata), seen)
        ]
    if isinstance(annotation, type) and issubclass(annotation, (Enum, bool, int, float)):
        return []
    if annotation is type(None):
        return []
    import datetime as dt

    if annotation is dt.datetime:
        return []
    return [repr(annotation)]


def test_no_model_imports() -> None:
    for name in GUARD_MODULES:
        path = REPO / "soundings" / "ladder" / name
        for mod in _imports(path):
            assert not mod.startswith("soundings.llm"), (name, mod)
            assert not mod.startswith("anthropic"), (name, mod)
    # GuardInputs carries no free text, so no model output can reach a guard.
    assert _free_text(GuardInputs, [], set()) == []


NEVER = sorted(ABSOLUTE_NEVER)


@pytest.mark.parametrize("action", NEVER)
def test_never_list_absolute(action: str) -> None:
    assert set(NEVER) == {"any_closure", "external_call_uscg_eod", "anything_after_handoff"}
    assert set(NEVER_ACTIONS.values()) <= ABSOLUTE_NEVER
    assert TABLE.cell_of(action) is BoundaryCell.NEVER
    req = ActionScope(action=action)
    token = token_for(req)
    for reasons in ((), tuple(ToggleName)):
        inputs = base(requested=req, approval_token=token, degraded_reasons=reasons)
        assert inputs.standing_orders_signed
        d = boundary_decision(action, inputs, TABLE)
        assert not d.permits and d.reason == f"never:{action}"
        t = approval_token_valid_and_scope_matches(inputs, CRIT)
        assert not t.permits and t.reason == f"never:{action}"


def test_never_list_denylist_clean() -> None:
    """``grep -riEf config/denylist.txt`` over the WO-041 files finds nothing."""
    paths = [
        *sorted((REPO / "soundings" / "ladder").glob("*.py")),
        *sorted((REPO / "tests").glob("test_guards*.py")),
        REPO / "tests" / "test_standing_orders.py",
        REPO / "tests" / "test_approval.py",
        REPO / "tests" / "support" / "guard_streams.py",
    ]
    for path in paths:
        text = path.read_text(encoding="utf-8")
        for pattern in DENYLIST:
            assert not re.search(pattern.pattern, text, re.IGNORECASE), path.name
