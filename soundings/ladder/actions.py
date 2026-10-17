"""Ladder actions and the idempotency ledger (CLAUDE.md section 4: key per action).

An action is what a transition asks the outside world to do. The ledger is the only door
an action goes through: a key it has seen returns the first record and executes nothing
again, so a re-delivered event after a restore never starts a second hold.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from soundings.canonical import stable_hash
from soundings.schemas import LadderState
from soundings.schemas._base import NonNegInt, StrictModel

from .types import ActionKind, ActionScope, LadderEvent

INTERRUPTION_KINDS: frozenset[ActionKind] = frozenset(
    {ActionKind.REQUEST_APPROVAL, ActionKind.PAGE_BACKUP}
)
"""Actions that ask a person to act (WO-040; WO-043 adds the handoff SITREP count)."""


class LadderAction(StrictModel):
    kind: ActionKind
    contact_id: str
    scope: ActionScope | None = None
    tick: NonNegInt
    idempotency_key: str


def action_key(
    contact_id: str,
    kind: ActionKind,
    scope: ActionScope | None,
    from_state: LadderState,
    to_state: LadderState,
    event: LadderEvent,
) -> str:
    """``stable_hash([contact_id, kind, scope, from_state, to_state, event.tick])``."""
    return stable_hash([contact_id, kind, scope, from_state, to_state, event.tick])


def make_action(
    contact_id: str,
    kind: ActionKind,
    scope: ActionScope | None,
    from_state: LadderState,
    to_state: LadderState,
    event: LadderEvent,
) -> LadderAction:
    return LadderAction(
        kind=kind,
        contact_id=contact_id,
        scope=scope,
        tick=event.tick,
        idempotency_key=action_key(contact_id, kind, scope, from_state, to_state, event),
    )


class ActionRecord(StrictModel):
    """The ledger's record of the first execution of a key. ``seq`` counts from 0."""

    seq: NonNegInt
    action: LadderAction


@dataclass
class ActionLedger:
    """Executes each idempotency key once. One ledger may serve every contact of a run."""

    _records: dict[str, ActionRecord] = field(default_factory=dict)
    hits: int = 0

    def execute(self, action: LadderAction) -> ActionRecord:
        """Record ``action`` unless its key is known; a known key returns the first record."""
        known = self._records.get(action.idempotency_key)
        if known is not None:
            self.hits += 1
            return known
        record = ActionRecord(seq=len(self._records), action=action)
        self._records[action.idempotency_key] = record
        return record

    def __contains__(self, key: object) -> bool:
        return key in self._records

    def records(self, contact_id: str | None = None) -> tuple[ActionRecord, ...]:
        """Records in execution order, optionally for one contact."""
        return tuple(
            r
            for r in self._records.values()
            if contact_id is None or r.action.contact_id == contact_id
        )

    def keys(self, contact_id: str | None = None) -> tuple[str, ...]:
        return tuple(r.action.idempotency_key for r in self.records(contact_id))

    def active_holds(self) -> int:
        """Contacts, across the whole run, with more ``start_hold`` than ``release_hold``."""
        balance: dict[str, int] = {}
        for r in self._records.values():
            if r.action.kind is ActionKind.START_HOLD:
                balance[r.action.contact_id] = balance.get(r.action.contact_id, 0) + 1
            elif r.action.kind is ActionKind.RELEASE_HOLD:
                balance[r.action.contact_id] = balance.get(r.action.contact_id, 0) - 1
        return sum(1 for v in balance.values() if v > 0)

    def interruptions(self, contact_id: str) -> int:
        """``request_approval`` plus ``page_backup`` records for one contact."""
        return sum(1 for r in self.records(contact_id) if r.action.kind in INTERRUPTION_KINDS)
