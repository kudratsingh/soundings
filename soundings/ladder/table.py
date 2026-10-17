"""SPEC-LADDER section 3 as data. This tuple is the only source of ladder transitions.

Rows are copied line for line from the spec table, in spec order. Four places go beyond a
literal one-row-per-line copy, each tied to a section 3 line:

1. ``INVESTIGATING x stop_rule_fired(HANDOFF)``: three rows evaluated in table order
   (slow, then hold, then nothing pre-authorized; DECISIONS.md 23:05).
2. ``HOLDING x timer(limit) or scope exceeded``: one row per event (``timer``, then
   ``scope_exceeded``), both to AWAITING_APPROVAL.
3. ``HANDED_OFF x (dash)``: the dash is the ``handoff_complete`` event (DECISIONS.md 23:05).
4. ``any non-terminal x error -> FAILED``: one row per state outside
   ``HANDLING_TERMINAL_STATES``, in ``LadderState`` order.

``tests/test_ladder.py::test_table_matches_spec`` parses the spec table and checks these
rows against it, so the two cannot drift.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from soundings.schemas import HANDLING_TERMINAL_STATES, LadderState, Rung

from .types import ActionKind, EventKind, GuardName

S = LadderState
E = EventKind
G = GuardName
A = ActionKind


@dataclass(frozen=True)
class TransitionRow:
    """(from, event kind, event arg or None for any, guard or None, to, actions)."""

    src: LadderState
    kind: EventKind
    arg: str | None
    guard: GuardName | None
    dst: LadderState
    actions: tuple[ActionKind, ...] = ()

    def matches(self, state: LadderState, kind: EventKind, arg: str | None) -> bool:
        return self.src is state and self.kind is kind and (self.arg is None or self.arg == arg)


_SITREP = (A.DELIVER_SITREP,)

_SPEC_ROWS: tuple[TransitionRow, ...] = (
    TransitionRow(S.DETECTED, E.CONTACT_DETECTED, None, None, S.WATCHING, (A.OPEN_EVIDENCE_TABLE,)),
    TransitionRow(S.WATCHING, E.PROBE_RESULT, None, G.FREE_PROBES_COMPLETE, S.ASKING),
    TransitionRow(
        S.ASKING,
        E.STOP_RULE_FIRED,
        "CLEARED_AT_ASK",
        G.REGISTRY_EXACT_AND_HAIL_VALID,
        S.CLEARED_AT_ASK,
    ),
    TransitionRow(S.ASKING, E.STOP_RULE_FIRED, "LOST_VEHICLE", None, S.LOST_VEHICLE, _SITREP),
    TransitionRow(S.ASKING, E.PROBE_RESULT, None, G.NOT_CLEARED, S.INVESTIGATING),
    TransitionRow(S.INVESTIGATING, E.STOP_RULE_FIRED, "RESOLVED_BENIGN", None, S.RESOLVED_BENIGN),
    TransitionRow(
        S.INVESTIGATING, E.STOP_RULE_FIRED, "HAZARD_NOTICE", None, S.HAZARD_NOTICE, _SITREP
    ),
    # Expansion 1: the three handoff rows, in table order.
    TransitionRow(
        S.INVESTIGATING,
        E.STOP_RULE_FIRED,
        "HANDOFF",
        G.SLOW_PERMITTED,
        S.ADVISING_SLOW,
        (A.ISSUE_SLOW_ADVISORY,),
    ),
    TransitionRow(
        S.INVESTIGATING, E.STOP_RULE_FIRED, "HANDOFF", G.HOLD_PERMITTED, S.HOLDING, (A.START_HOLD,)
    ),
    TransitionRow(
        S.INVESTIGATING,
        E.STOP_RULE_FIRED,
        "HANDOFF",
        G.NOTHING_PREAUTHORIZED,
        S.HANDED_OFF,
        _SITREP,
    ),
    TransitionRow(
        S.ADVISING_SLOW,
        E.STANDING_ORDER_CHECK,
        "hold_permitted",
        G.HOLD_PERMITTED,
        S.HOLDING,
        (A.START_HOLD,),
    ),
    TransitionRow(
        S.ADVISING_SLOW, E.STANDING_ORDER_CHECK, "not_permitted", None, S.HANDED_OFF, _SITREP
    ),
    # Expansion 2: timer(limit) or scope exceeded.
    TransitionRow(
        S.HOLDING, E.TIMER, "hold_limit", None, S.AWAITING_APPROVAL, (A.REQUEST_APPROVAL,)
    ),
    TransitionRow(
        S.HOLDING, E.SCOPE_EXCEEDED, None, None, S.AWAITING_APPROVAL, (A.REQUEST_APPROVAL,)
    ),
    TransitionRow(
        S.HOLDING, E.CONTACT_DEPARTED, None, None, S.RESOLVED_DEPARTED, (A.RELEASE_HOLD,)
    ),
    TransitionRow(
        S.AWAITING_APPROVAL,
        E.APPROVAL,
        "granted",
        G.APPROVAL_TOKEN_VALID_AND_SCOPE_MATCHES,
        S.HANDED_OFF,
        (A.RECORD_APPROVED_ACTION, A.DELIVER_SITREP),
    ),
    TransitionRow(
        S.AWAITING_APPROVAL,
        E.APPROVAL,
        "denied",
        None,
        S.HANDED_OFF,
        (A.RELEASE_HOLD, A.DELIVER_SITREP),
    ),
    TransitionRow(
        S.AWAITING_APPROVAL,
        E.APPROVAL,
        "timeout",
        G.BACKUP_EXISTS,
        S.AWAITING_APPROVAL,
        (A.PAGE_BACKUP,),
    ),
    TransitionRow(
        S.AWAITING_APPROVAL,
        E.APPROVAL,
        "timeout",
        G.NO_BACKUP,
        S.HANDED_OFF,
        (A.RELEASE_HOLD, A.DELIVER_SITREP),
    ),
    # Expansion 3: the dash row is handoff_complete.
    TransitionRow(
        S.HANDED_OFF, E.HANDOFF_COMPLETE, None, None, S.WATCH_AFTER_HANDOFF, (A.START_WATCH,)
    ),
    TransitionRow(
        S.WATCH_AFTER_HANDOFF, E.CONTACT_DEPARTED, None, None, S.RESOLVED_DEPARTED, (A.CLOSE_WATCH,)
    ),
    TransitionRow(
        S.WATCH_AFTER_HANDOFF,
        E.CONTACT_PERSISTED,
        None,
        None,
        S.WATCH_AFTER_HANDOFF,
        (A.UPDATE_SITREP,),
    ),
)

# Expansion 4: any non-terminal x error -> FAILED.
_ERROR_ROWS: tuple[TransitionRow, ...] = tuple(
    TransitionRow(s, E.ERROR, None, None, S.FAILED)
    for s in LadderState
    if s not in HANDLING_TERMINAL_STATES
)

TRANSITIONS: tuple[TransitionRow, ...] = _SPEC_ROWS + _ERROR_ROWS
"""Every legal transition; a (state, event) pair with no row here is impossible."""

RUNG_OF_STATE: Mapping[LadderState, Rung | None] = MappingProxyType(
    {
        S.DETECTED: Rung.WATCH,
        S.WATCHING: Rung.WATCH,
        S.ASKING: Rung.ASK,
        S.INVESTIGATING: Rung.ASK,
        S.ADVISING_SLOW: Rung.SLOW,
        S.HOLDING: Rung.HOLD,
        S.AWAITING_APPROVAL: None,
        S.HANDED_OFF: Rung.HAND_TO_PERSON,
        S.WATCH_AFTER_HANDOFF: Rung.HAND_TO_PERSON,
        S.RESOLVED_BENIGN: Rung.ASK,
        S.CLEARED_AT_ASK: Rung.ASK,
        S.HAZARD_NOTICE: Rung.ASK,
        S.LOST_VEHICLE: Rung.ASK,
        S.RESOLVED_DEPARTED: None,
        S.FAILED: None,
    }
)
"""The rung of each of the 15 states. ``None`` keeps the rung of the state it came from:
AWAITING_APPROVAL stays at the rung that asked, and RESOLVED_DEPARTED and FAILED record the
rung the contact ended on."""

CLIMB_STATES: frozenset[LadderState] = frozenset({S.ADVISING_SLOW, S.HOLDING})
"""States entered only on a guard ``permit`` that carries a bounded ``granted_scope``."""


def rung_after(previous: Rung, dst: LadderState) -> Rung:
    """The rung after entering ``dst`` from a state whose rung was ``previous``."""
    rung = RUNG_OF_STATE[dst]
    return previous if rung is None else rung


def rows_for(state: LadderState, kind: EventKind, arg: str | None) -> tuple[int, ...]:
    """Indices of the candidate rows for (state, event), in table order."""
    return tuple(i for i, row in enumerate(TRANSITIONS) if row.matches(state, kind, arg))
