"""WO-040: the ladder table is SPEC-LADDER section 3, every row is reachable, nothing else is.

Hard rules: 2.1 (HANDED_OFF only goes to WATCH_AFTER_HANDOFF; terminal states never move),
2.2 (no model import reaches a transition), 2.5 (no denylisted name anywhere in the ladder
vocabulary).
"""

from __future__ import annotations

import ast
import itertools
import re
from collections import deque
from collections.abc import Iterator
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from soundings.clock import TickClock
from soundings.ladder.actions import ActionLedger
from soundings.ladder.checkpoint import MemoryCheckpointStore
from soundings.ladder.guard_port import (
    FREE_PROBES,
    NO_SIGNED_ORDERS,
    NoStandingOrdersGuards,
    evidence_guard,
)
from soundings.ladder.machine import (
    GUARD_BLOCKED,
    HOLD_LONGER,
    NO_ROW,
    TIMER_EVENTS,
    LadderMachine,
    StepResult,
)
from soundings.ladder.table import (
    CLIMB_STATES,
    RUNG_OF_STATE,
    TRANSITIONS,
    TransitionRow,
    rows_for,
)
from soundings.ladder.types import (
    EVENT_ARGS,
    ActionKind,
    ActionScope,
    EventKind,
    EventPayload,
    GuardContext,
    GuardDecision,
    GuardName,
    GuardVerdict,
    LadderEvent,
    LadderTimings,
)
from soundings.schemas import (
    HANDLING_TERMINAL_STATES,
    TERMINAL_STATES,
    EvidenceKind,
    EvidenceRow,
    FeatureName,
    LadderState,
    ProbeId,
    Rung,
    Unit,
    find_denylisted,
    load_ladder_config,
)
from tests.support.scripted_guards import ScriptedGuards

REPO = Path(__file__).resolve().parent.parent
CID = "c-0001"
TIMINGS = LadderTimings(
    sitrep_update_interval_s=300.0,
    departed_after_s_outside_zone=120.0,
    primary_timeout_s=120.0,
    backup_timeout_s=180.0,
)

ALL_EVENTS: tuple[tuple[EventKind, str | None], ...] = tuple(
    (kind, arg) for kind in EventKind for arg in (EVENT_ARGS.get(kind) or (None,))
)
"""Every (kind, arg) pair the event vocabulary allows."""


def machine(guards: object, tick_s: float = 2.0) -> tuple[LadderMachine, TickClock]:
    clock = TickClock(tick_s=tick_s)
    m = LadderMachine(CID, guards, clock, ActionLedger(), MemoryCheckpointStore(), TIMINGS)  # type: ignore[arg-type]
    return m, clock


def event(kind: EventKind, arg: str | None = None, tick: int = 0) -> LadderEvent:
    return LadderEvent(kind=kind, arg=arg, tick=tick)


# --- the table is the spec ---------------------------------------------------------------

SPEC_GUARDS: dict[str, GuardName | None] = {
    "—": None,
    "free probes complete": GuardName.FREE_PROBES_COMPLETE,
    "registry exact ∧ hail valid": GuardName.REGISTRY_EXACT_AND_HAIL_VALID,
    "not cleared": GuardName.NOT_CLEARED,
    "standing orders permit slow ∧ criteria met": GuardName.SLOW_PERMITTED,
    "standing orders permit hold ∧ criteria met": GuardName.HOLD_PERMITTED,
    "nothing pre-authorized": GuardName.NOTHING_PREAUTHORIZED,
    "criteria met": GuardName.HOLD_PERMITTED,
    "approval token valid ∧ scope matches": GuardName.APPROVAL_TOKEN_VALID_AND_SCOPE_MATCHES,
    "backup approver exists": GuardName.BACKUP_EXISTS,
    "no backup": GuardName.NO_BACKUP,
}
"""Spec guard wording -> GuardName (the ADVISING_SLOW 'criteria met' row is the hold check)."""

SPEC_ARGS = {"limit": "hold_limit"}
"""Spec event arguments renamed in code: timer(limit) is the hold limit timer."""


def _spec_rows() -> list[tuple[LadderState, EventKind, str | None, GuardName | None, LadderState]]:
    text = (REPO / "docs" / "SPEC-LADDER.md").read_text(encoding="utf-8")
    section = text.split("## 3.", 1)[1].split("## 4.", 1)[0]
    lines = [ln for ln in section.splitlines() if ln.startswith("|")]
    out = []
    for line in lines[2:]:  # skip header and separator
        cells = [c.strip().replace("`", "") for c in line.strip("|").split("|")]
        src_text, event_text, guard_text, dst_text = cells
        dst_match = re.match(r"[A-Z_]+", dst_text)
        assert dst_match, dst_text
        dst = LadderState(dst_match.group(0))
        if src_text == "any non-terminal":  # expansion 4
            srcs = [s for s in LadderState if s not in HANDLING_TERMINAL_STATES]
        else:
            srcs = [LadderState(src_text)]
        events: list[tuple[EventKind, str | None]] = []
        if event_text == "—":  # expansion 3
            events.append((EventKind.HANDOFF_COMPLETE, None))
        else:
            for part in event_text.split("∨"):  # expansion 2
                m = re.fullmatch(r"([a-z_ ]+?)\s*(?:\((.*)\))?", part.strip())
                assert m, part
                arg = m.group(2)
                kind = EventKind(m.group(1).strip().replace(" ", "_"))
                events.append((kind, SPEC_ARGS.get(arg, arg) if arg else None))
        guard = SPEC_GUARDS[guard_text]
        for src in srcs:
            for kind, arg in events:
                out.append((src, kind, arg, guard, dst))
    return out


def test_table_matches_spec() -> None:
    spec = _spec_rows()
    code = [(r.src, r.kind, r.arg, r.guard, r.dst) for r in TRANSITIONS]
    assert code == spec  # same rows, same order (table order decides the handoff rows)
    assert sorted((s, k, d) for s, k, _, _, d in spec) == sorted(
        (r.src, r.kind, r.dst) for r in TRANSITIONS
    )


def test_rung_of_state_and_climb_rows() -> None:
    assert set(RUNG_OF_STATE) == set(LadderState)
    assert len(RUNG_OF_STATE) == 15
    assert RUNG_OF_STATE[LadderState.AWAITING_APPROVAL] is None  # keeps the asking rung
    for row in TRANSITIONS:
        if row.dst in CLIMB_STATES:
            assert row.guard is not None, row
    # No row leaves a terminal state; HANDED_OFF has exactly one way out.
    assert not [r for r in TRANSITIONS if r.src in TERMINAL_STATES]
    out_of_handoff = [r for r in TRANSITIONS if r.src is LadderState.HANDED_OFF]
    assert [(r.kind, r.dst) for r in out_of_handoff] == [
        (EventKind.HANDOFF_COMPLETE, LadderState.WATCH_AFTER_HANDOFF)
    ]
    after_handoff = {LadderState.WATCH_AFTER_HANDOFF}
    for r in TRANSITIONS:
        if r.src in after_handoff:
            assert r.dst in after_handoff | {LadderState.RESOLVED_DEPARTED, LadderState.FAILED}


# --- every row reachable -----------------------------------------------------------------

Script = dict[GuardName, GuardVerdict]
Path_ = tuple[tuple[EventKind, str | None, tuple[tuple[GuardName, GuardVerdict], ...]], ...]


def _replay(path: Path_) -> tuple[LadderMachine, ScriptedGuards, StepResult | None]:
    guards = ScriptedGuards()
    m, clock = machine(guards)
    last = None
    for tick, (kind, arg, script) in enumerate(path):
        guards.set(dict(script))
        clock.tick = tick
        last = m.step(event(kind, arg, tick))
    return m, guards, last


def _settings(state: LadderState, kind: EventKind, arg: str | None) -> Iterator[Script]:
    guards = sorted({TRANSITIONS[i].guard for i in rows_for(state, kind, arg)} - {None})
    for verdicts in itertools.product(list(GuardVerdict), repeat=len(guards)):
        yield dict(zip(guards, verdicts, strict=True))  # type: ignore[arg-type]


def test_every_row_reachable() -> None:
    """BFS over (state, event, ScriptedGuards setting) from DETECTED fires every row."""
    fired: dict[int, Path_] = {}
    seen = {LadderState.DETECTED}
    queue: deque[tuple[LadderState, Path_]] = deque([(LadderState.DETECTED, ())])
    while queue:
        state, path = queue.popleft()
        for kind, arg in ALL_EVENTS:
            for script in _settings(state, kind, arg):
                step = (kind, arg, tuple(sorted(script.items())))
                m, _, result = _replay(path + (step,))
                assert result is not None
                if result.row_index is None:
                    assert m.state is state
                    continue
                assert result.transitioned
                fired.setdefault(result.row_index, path + (step,))
                if m.state not in seen:
                    seen.add(m.state)
                    queue.append((m.state, path + (step,)))
    assert set(fired) == set(range(len(TRANSITIONS)))
    assert seen == set(LadderState)
    for index, path in fired.items():  # each recorded path fires exactly that row
        _, _, result = _replay(path)
        assert result is not None and result.row_index == index


# --- nothing outside the table -----------------------------------------------------------


def _row_for(result: StepResult) -> TransitionRow:
    assert result.row_index is not None
    return TRANSITIONS[result.row_index]


@pytest.mark.parametrize("verdict", list(GuardVerdict))
def test_no_transition_outside_table_exhaustive(verdict: GuardVerdict) -> None:
    """Every (reachable state, event) pair under all-permit / all-block guards."""
    script = {g: verdict for g in GuardName}
    for state_path in _paths_to_states().values():
        for kind, arg in ALL_EVENTS:
            m, guards, _ = _replay(state_path)
            src = m.state
            guards.set(script)
            r = m.step(event(kind, arg, len(state_path)))
            if r.transitioned:
                row = _row_for(r)
                assert row.matches(src, kind, arg) and row.dst is m.state
            else:
                assert m.state is src
                assert r.reason in (NO_ROW, GUARD_BLOCKED)
                assert (r.reason == NO_ROW) == (not rows_for(src, kind, arg))


def _paths_to_states() -> dict[LadderState, Path_]:
    """A shortest path (all guards scripted as needed) to every state."""
    paths: dict[LadderState, Path_] = {LadderState.DETECTED: ()}
    queue = deque([LadderState.DETECTED])
    while queue:
        state = queue.popleft()
        for kind, arg in ALL_EVENTS:
            for script in _settings(state, kind, arg):
                path = paths[state] + ((kind, arg, tuple(sorted(script.items()))),)
                m, _, _ = _replay(path)
                if m.state not in paths:
                    paths[m.state] = path
                    queue.append(m.state)
    return paths


EVENTS = st.sampled_from(ALL_EVENTS)
SCRIPTS = st.dictionaries(st.sampled_from(list(GuardName)), st.sampled_from(list(GuardVerdict)))


@settings(max_examples=300, deadline=None, derandomize=True)
@given(st.lists(st.tuples(EVENTS, SCRIPTS), max_size=25))
def test_no_transition_outside_table_property(
    steps: list[tuple[tuple[EventKind, str | None], Script]],
) -> None:
    guards = ScriptedGuards()
    m, clock = machine(guards)
    for tick, ((kind, arg), script) in enumerate(steps):
        guards.set(script)
        clock.tick = tick
        src = m.state
        r = m.step(event(kind, arg, tick))
        assert m.state in set(LadderState)
        if r.transitioned:
            row = _row_for(r)
            assert row.matches(src, kind, arg) and row.dst is m.state
        else:
            assert m.state is src


# --- terminal for handling ---------------------------------------------------------------


@pytest.mark.parametrize("state", sorted(HANDLING_TERMINAL_STATES, key=lambda s: s.value))
@pytest.mark.parametrize("verdict", list(GuardVerdict))
def test_handling_terminal_states(state: LadderState, verdict: GuardVerdict) -> None:
    path = _paths_to_states()[state]
    for kind, arg in ALL_EVENTS:
        m, guards, _ = _replay(path)
        guards.set({g: verdict for g in GuardName})
        r = m.step(event(kind, arg, len(path)))
        if state is LadderState.HANDED_OFF and kind is EventKind.HANDOFF_COMPLETE:
            assert r.transitioned and m.state is LadderState.WATCH_AFTER_HANDOFF
        else:
            assert not r.transitioned and m.state is state


# --- vocabulary and model isolation --------------------------------------------------------


def test_vocabulary_clean() -> None:
    names: list[str] = []
    for enum_type in (LadderState, EventKind, GuardName, GuardVerdict, ActionKind, ProbeId, Rung):
        names += [m.value for m in enum_type]  # type: ignore[attr-defined]
        names += [m.name for m in enum_type]  # type: ignore[attr-defined]
    names += [arg for args in EVENT_ARGS.values() for arg in args]
    names += list(TIMER_EVENTS) + [HOLD_LONGER, NO_SIGNED_ORDERS, NO_ROW, GUARD_BLOCKED]
    names += [p.value for p in FREE_PROBES]
    cfg = load_ladder_config()
    for cell in cfg.boundary.values():
        names += [*cell.platform_alone, *cell.standing_orders, *cell.person_approves]
    names += list(LadderTimings.model_fields) + list(GuardContext.model_fields)
    assert len(names) > 100
    hits = {n: find_denylisted(n.replace("_", " ")) for n in names}
    hits |= {f"raw:{n}": find_denylisted(n) for n in names}
    assert {n: h for n, h in hits.items() if h} == {}


LADDER_MODULES = ("types", "table", "machine", "checkpoint", "actions", "guard_port", "ports")


def _imports(path: Path, package: str) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = package.split(".")[: len(package.split(".")) - node.level + 1]
                module = ".".join(base + ([node.module] if node.module else []))
            else:
                module = node.module or ""
            found.add(module)
            found |= {f"{module}.{a.name}" for a in node.names}
    return found


def test_no_model_imports() -> None:
    files = [
        (REPO / "soundings" / "ladder" / f"{m}.py", "soundings.ladder") for m in LADDER_MODULES
    ]
    files.append((REPO / "soundings" / "clock.py", "soundings"))
    for path, package in files:
        assert path.exists(), path
        for name in _imports(path, package):
            assert not name.startswith("soundings.llm"), (path.name, name)
            assert not name.startswith("anthropic"), (path.name, name)


# --- behaviour of individual rows ------------------------------------------------------------


def _to_investigating(m: LadderMachine) -> None:
    for i, (kind, arg) in enumerate(
        [
            (EventKind.CONTACT_DETECTED, None),
            (EventKind.PROBE_RESULT, None),
            (EventKind.PROBE_RESULT, None),
        ]
    ):
        assert m.step(event(kind, arg, i)).transitioned
    assert m.state is LadderState.INVESTIGATING


def test_handoff_rows_in_table_order_slow_then_hold() -> None:
    guards = ScriptedGuards()  # slow and hold both permit
    m, clock = machine(guards)
    _to_investigating(m)
    r = m.step(event(EventKind.STOP_RULE_FIRED, "HANDOFF", 3))
    assert m.state is LadderState.ADVISING_SLOW and m.rung is Rung.SLOW
    assert [d.guard for d in r.decisions] == [GuardName.SLOW_PERMITTED]
    assert [a.kind for a in r.actions] == [ActionKind.ISSUE_SLOW_ADVISORY]
    r = m.step(event(EventKind.STANDING_ORDER_CHECK, "hold_permitted", 4))
    assert m.state is LadderState.HOLDING and m.rung is Rung.HOLD
    assert [a.kind for a in r.actions] == [ActionKind.START_HOLD]
    assert m.ledger.active_holds() == 1
    assert [t.name for t in m.timers.pending()] == ["hold_limit"]  # slow timer cancelled

    guards2 = ScriptedGuards(blocked=[GuardName.SLOW_PERMITTED])
    m2, _ = machine(guards2)
    _to_investigating(m2)
    r2 = m2.step(event(EventKind.STOP_RULE_FIRED, "HANDOFF", 3))
    assert m2.state is LadderState.HOLDING
    assert [(d.guard, d.verdict) for d in r2.decisions] == [
        (GuardName.SLOW_PERMITTED, GuardVerdict.BLOCK),
        (GuardName.HOLD_PERMITTED, GuardVerdict.PERMIT),
    ]


class _UnboundedGuards(ScriptedGuards):
    def decide(self, name: GuardName, ctx: GuardContext) -> GuardDecision:
        return super().decide(name, ctx).model_copy(update={"granted_scope": None})


def test_permit_without_bounded_scope_never_climbs() -> None:
    m, _ = machine(_UnboundedGuards())
    _to_investigating(m)
    r = m.step(event(EventKind.STOP_RULE_FIRED, "HANDOFF", 3))
    assert m.state is LadderState.HANDED_OFF  # the nothing-pre-authorized row
    assert [d.guard for d in r.decisions] == [
        GuardName.SLOW_PERMITTED,
        GuardName.HOLD_PERMITTED,
        GuardName.NOTHING_PREAUTHORIZED,
    ]


def test_blocked_guard_and_no_row_change_nothing() -> None:
    seen: list[tuple[object, int, int]] = []

    def listener(cp: object, actions: object, decisions: object) -> None:
        seen.append((cp, len(actions), len(decisions)))  # type: ignore[arg-type]

    guards = ScriptedGuards(blocked=[GuardName.FREE_PROBES_COMPLETE])
    clock = TickClock(tick_s=2.0)
    store = MemoryCheckpointStore()
    m = LadderMachine(CID, guards, clock, ActionLedger(), store, TIMINGS, listeners=[listener])
    m.step(event(EventKind.CONTACT_DETECTED))
    head = m.head_hash
    r = m.step(event(EventKind.PROBE_RESULT, tick=1))
    assert (r.transitioned, r.reason, m.state) == (False, GUARD_BLOCKED, LadderState.WATCHING)
    assert [d.verdict for d in r.decisions] == [GuardVerdict.BLOCK]
    r = m.step(event(EventKind.APPROVAL, "granted", 2))
    assert (r.transitioned, r.reason, r.decisions) == (False, NO_ROW, ())
    assert m.head_hash == head and len(store.load()) == 1
    assert seen[0][0] is not None and seen[1] == (None, 0, 1) and len(seen) == 2


def test_hold_timer_approval_backup_and_interruptions() -> None:
    guards = ScriptedGuards(blocked=[GuardName.SLOW_PERMITTED])
    m, clock = machine(guards, tick_s=1.0)
    _to_investigating(m)
    clock.tick = 3
    m.step(event(EventKind.STOP_RULE_FIRED, "HANDOFF", 3))
    assert m.state is LadderState.HOLDING
    clock.tick = 3 + 15 * 60 - 1
    assert m.advance() == []
    clock.advance()
    (r,) = m.advance()
    assert r.event.kind is EventKind.TIMER and r.event.arg == "hold_limit"
    assert m.state is LadderState.AWAITING_APPROVAL and m.rung is Rung.HOLD
    (req,) = r.actions
    assert req.kind is ActionKind.REQUEST_APPROVAL
    assert req.scope == ActionScope(
        action=HOLD_LONGER, asset_id="fuel_berth_1", gates=1, max_minutes=15
    )
    clock.advance(120)
    (r,) = m.advance()
    assert r.event.kind is EventKind.APPROVAL and r.event.arg == "timeout"
    assert [a.kind for a in r.actions] == [ActionKind.PAGE_BACKUP] and m.backup_paged
    assert m.state is LadderState.AWAITING_APPROVAL
    assert m.ledger.interruptions(CID) == 2
    guards.set({GuardName.BACKUP_EXISTS: GuardVerdict.BLOCK})
    clock.advance(179)
    assert m.advance() == []
    clock.advance()
    (r,) = m.advance()
    assert m.state is LadderState.HANDED_OFF
    assert [a.kind for a in r.actions] == [ActionKind.RELEASE_HOLD, ActionKind.DELIVER_SITREP]
    assert m.ledger.active_holds() == 0
    assert m.timers.pending() == ()


def test_scope_exceeded_and_granted_approval() -> None:
    guards = ScriptedGuards(blocked=[GuardName.SLOW_PERMITTED])
    m, _ = machine(guards)
    _to_investigating(m)
    m.step(event(EventKind.STOP_RULE_FIRED, "HANDOFF", 3))
    wider = ActionScope(action="second_hold", asset_id="fuel_berth_2", gates=1, max_minutes=15)
    r = m.step(
        LadderEvent(
            kind=EventKind.SCOPE_EXCEEDED, tick=4, payload=EventPayload(requested_scope=wider)
        )
    )
    assert m.state is LadderState.AWAITING_APPROVAL
    assert r.actions[0].scope == wider
    r = m.step(
        LadderEvent(
            kind=EventKind.APPROVAL,
            arg="granted",
            tick=5,
            payload=EventPayload(approval_token={"scope": wider.model_dump(mode="json")}),
        )
    )
    assert m.state is LadderState.HANDED_OFF
    assert [a.kind for a in r.actions] == [
        ActionKind.RECORD_APPROVED_ACTION,
        ActionKind.DELIVER_SITREP,
    ]
    assert r.actions[0].scope == wider


def _row(feature: FeatureName, value: str, probe: ProbeId | None, cid: str = CID) -> EvidenceRow:
    return EvidenceRow(
        contact_id=cid,
        t=1,
        feature=feature,
        value=value,
        unit=Unit.CATEGORY,
        source="probe:test",
        confidence=1.0,
        kind=EvidenceKind.FACT,
        probe_id=probe,
    )


def _ctx(*rows: EvidenceRow) -> GuardContext:
    ev = LadderEvent(kind=EventKind.PROBE_RESULT, tick=1, payload=EventPayload(rows=rows))
    return GuardContext(contact_id=CID, state=LadderState.ASKING, event=ev, now_s=2.0)


@pytest.mark.parametrize(
    ("registry", "hail", "cleared"),
    [
        ("exact", "valid", True),
        ("exact", "invalid", False),
        ("exact", "timeout", False),
        ("none", "valid", False),
        ("area_mismatch", "valid", False),
        (None, "valid", False),
        ("exact", None, False),
        ("exact", "unknown", False),
    ],
)
def test_evidence_guards_read_rows(registry: str | None, hail: str | None, cleared: bool) -> None:
    rows = []
    if registry is not None:
        rows.append(_row(FeatureName.REGISTRY_MATCH, registry, ProbeId.REGISTRY_LOOKUP))
    if hail is not None:
        rows.append(_row(FeatureName.HAIL_REPLY, hail, ProbeId.ACOUSTIC_HAIL))
    rows.append(_row(FeatureName.REGISTRY_MATCH, "exact", ProbeId.REGISTRY_LOOKUP, "c-0999"))
    ctx = _ctx(*rows)
    ok = evidence_guard(GuardName.REGISTRY_EXACT_AND_HAIL_VALID, ctx)
    nc = evidence_guard(GuardName.NOT_CLEARED, ctx)
    assert ok.permits is cleared and nc.permits is not cleared
    assert ok.inputs_hash == nc.inputs_hash


def test_free_probes_guard_and_unsigned_guards() -> None:
    assert not evidence_guard(GuardName.FREE_PROBES_COMPLETE, _ctx()).permits
    assert not evidence_guard(
        GuardName.FREE_PROBES_COMPLETE,
        _ctx(_row(FeatureName.HAIL_REPLY, "none", ProbeId.ACOUSTIC_HAIL)),
    ).permits
    ctx = _ctx(_row(FeatureName.REGISTRY_MATCH, "none", ProbeId.REGISTRY_LOOKUP))
    assert evidence_guard(GuardName.FREE_PROBES_COMPLETE, ctx).permits
    with pytest.raises(ValueError):
        evidence_guard(GuardName.SLOW_PERMITTED, ctx)
    port = NoStandingOrdersGuards()
    for g in GuardName:
        d = port.decide(g, ctx)
        if g is GuardName.NOTHING_PREAUTHORIZED:
            assert d.permits
        elif g in (
            GuardName.SLOW_PERMITTED,
            GuardName.HOLD_PERMITTED,
            GuardName.APPROVAL_TOKEN_VALID_AND_SCOPE_MATCHES,
            GuardName.BACKUP_EXISTS,
            GuardName.NO_BACKUP,
        ):
            assert not d.permits and d.reason == NO_SIGNED_ORDERS


def test_event_args_validated() -> None:
    with pytest.raises(ValueError):
        LadderEvent(kind=EventKind.STOP_RULE_FIRED, arg="SOMETHING_ELSE", tick=0)
    with pytest.raises(ValueError):
        LadderEvent(kind=EventKind.STOP_RULE_FIRED, tick=0)
    with pytest.raises(ValueError):
        LadderEvent(kind=EventKind.ERROR, arg="x", tick=0)
    assert LadderEvent(kind=EventKind.TIMER, arg="hold_limit", tick=0).arg == "hold_limit"


@settings(
    max_examples=50, deadline=None, derandomize=True, suppress_health_check=[HealthCheck.too_slow]
)
@given(st.integers(min_value=0, max_value=10))
def test_restart_from_detected_is_deterministic(n: int) -> None:
    heads = []
    for _ in range(2):
        m, _ = machine(ScriptedGuards())
        for i, (kind, arg) in enumerate(ALL_EVENTS[: n + 1]):
            m.step(event(kind, arg, i))
        heads.append(m.head_hash)
    assert heads[0] == heads[1]
