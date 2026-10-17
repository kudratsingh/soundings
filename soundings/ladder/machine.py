"""The ladder: a hand-rolled, typed state machine over ``table.TRANSITIONS``.

CLAUDE.md 2.1: no row leaves HANDED_OFF except to WATCH_AFTER_HANDOFF, and no row reaches
a state after it. CLAUDE.md 2.2: a transition fires only from the table and only on
``GuardDecision`` values computed by a ``GuardPort``; nothing here reads model output.
CLAUDE.md 2.4: time comes from the injected ``Clock``; every float is rounded to 6 dp at
the checkpoint boundary.

A step that fires no row (no row for the pair, or every guard blocked) leaves the machine
exactly as it was, so the machine is a pure function of its checkpoints: ``restore``
rebuilds state, rung, pending timers and ledger keys by re-applying each checkpointed
transition with its recorded guard decisions (no guard is called again).
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass

from soundings.canonical import round6
from soundings.clock import Clock, TickClock, Timer, TimerSet
from soundings.schemas import (
    HANDLING_TERMINAL_STATES,
    EvidenceRow,
    FeatureName,
    LadderState,
    Rung,
)

from .actions import ActionLedger, ActionRecord, LadderAction, make_action
from .checkpoint import (
    GENESIS_HASH,
    CheckpointError,
    CheckpointStore,
    LadderCheckpoint,
    verify_chain,
)
from .guard_port import GuardPort
from .ports import LadderListener
from .table import CLIMB_STATES, TRANSITIONS, TransitionRow, rows_for, rung_after
from .types import (
    ActionKind,
    ActionScope,
    EventKind,
    EventPayload,
    GuardContext,
    GuardDecision,
    GuardName,
    LadderEvent,
    LadderTimings,
)

HOLD_LIMIT = "hold_limit"
SLOW_LIMIT = "slow_limit"
APPROVAL_TIMEOUT = "approval_timeout"
SITREP_UPDATE = "sitrep_update"

TIMER_EVENTS: dict[str, tuple[EventKind, str]] = {
    HOLD_LIMIT: (EventKind.TIMER, "hold_limit"),
    SLOW_LIMIT: (EventKind.TIMER, "slow_limit"),
    APPROVAL_TIMEOUT: (EventKind.APPROVAL, "timeout"),
    SITREP_UPDATE: (EventKind.CONTACT_PERSISTED, "interval"),
}
"""The event each timer fires when it comes due (``LadderMachine.advance``)."""

HOLD_LONGER = "hold_longer"
"""The boundary action an approval request names when a hold reaches its limit."""

TRANSITIONED = "transitioned"
NO_ROW = "no_row"
GUARD_BLOCKED = "guard_blocked"

Decide = Callable[[GuardName, GuardContext], GuardDecision]


@dataclass(frozen=True)
class StepResult:
    transitioned: bool
    reason: str
    from_state: LadderState
    to_state: LadderState
    event: LadderEvent
    decisions: tuple[GuardDecision, ...] = ()
    actions: tuple[LadderAction, ...] = ()
    records: tuple[ActionRecord, ...] = ()
    checkpoint: LadderCheckpoint | None = None
    row_index: int | None = None


def _bounded(decision: GuardDecision) -> bool:
    scope = decision.granted_scope
    return scope is not None and scope.max_minutes is not None


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


class LadderMachine:
    """One contact's ladder. ``guards`` decide, ``ledger`` executes, ``store`` remembers."""

    def __init__(
        self,
        contact_id: str,
        guards: GuardPort,
        clock: Clock,
        ledger: ActionLedger,
        store: CheckpointStore,
        timings: LadderTimings,
        listeners: Sequence[LadderListener] = (),
    ) -> None:
        self.contact_id = contact_id
        self._guards = guards
        self._clock = clock
        self._ledger = ledger
        self._store = store
        self._timings = timings
        self._listeners = tuple(listeners)
        self.timers = TimerSet()
        self._state = LadderState.DETECTED
        self._rung = Rung.WATCH
        self._seq = 0
        self._head = GENESIS_HASH
        self._last_tick = 0
        self._checkpoints: list[LadderCheckpoint] = []
        # Memory built only from checkpointed events.
        self._latest_rows: dict[FeatureName, EvidenceRow] = {}
        self._threat_mass: float | None = None
        self._benign_mass: float | None = None
        self._basin: str | None = None
        self._hold_scope: ActionScope | None = None
        self._requested: ActionScope | None = None
        self._backup_paged = False

    # --- read-only views -----------------------------------------------------------------

    @property
    def state(self) -> LadderState:
        return self._state

    @property
    def rung(self) -> Rung:
        return self._rung

    @property
    def head_hash(self) -> str:
        """Hash of the latest checkpoint (``GENESIS_HASH`` before the first)."""
        return self._head

    @property
    def checkpoints(self) -> tuple[LadderCheckpoint, ...]:
        return tuple(self._checkpoints)

    @property
    def ledger(self) -> ActionLedger:
        return self._ledger

    @property
    def backup_paged(self) -> bool:
        return self._backup_paged

    # --- stepping ------------------------------------------------------------------------

    def step(self, event: LadderEvent) -> StepResult:
        """Apply one event. No row, or every guard blocking, changes nothing."""
        now_s = round6(self._clock.now_s())
        ctx = self._context(event, now_s)
        result, permit = self._select(event, ctx, self._guards.decide)
        if result.row_index is None:
            if result.decisions:
                self._notify(None, (), result.decisions)
            return result
        return self._fire(event, result.row_index, result.decisions, permit, now_s, write=True)

    def advance(self, tick: int | None = None) -> list[StepResult]:
        """Fire every due timer of this contact, earliest first, as its event.

        A timer is consumed only by the transition its event fires (``_fire``), so the
        pending timers stay a function of the checkpoints. A due timer whose event fires no
        row stays pending and is tried again on the next call. ``tick`` defaults to the
        ``TickClock`` tick, or the latest event tick otherwise.
        """
        if tick is None:
            tick = self._clock.tick if isinstance(self._clock, TickClock) else self._last_tick
        results: list[StepResult] = []
        tried: set[Timer] = set()
        while True:
            due = [
                t
                for t in self.timers.due(self._clock)
                if t.contact_id == self.contact_id and t not in tried
            ]
            if not due:
                return results
            timer = due[0]
            tried.add(timer)
            kind, arg = TIMER_EVENTS[timer.name]
            event = LadderEvent(
                kind=kind,
                arg=arg,
                tick=tick,
                payload=EventPayload(extra={"timer": timer.name, "deadline_s": timer.deadline_s}),
            )
            results.append(self.step(event))

    # --- restore -------------------------------------------------------------------------

    @classmethod
    def restore(
        cls,
        checkpoints: Sequence[LadderCheckpoint],
        *,
        guards: GuardPort,
        clock: Clock,
        ledger: ActionLedger,
        store: CheckpointStore,
        timings: LadderTimings,
        listeners: Sequence[LadderListener] = (),
        contact_id: str | None = None,
    ) -> LadderMachine:
        """Rebuild a machine from its checkpoints without calling a guard.

        Each checkpoint is re-applied with its recorded decisions; the rebuilt checkpoint
        must equal the stored one (same actions, keys, rung and hash), or ``CheckpointError``.
        Actions go through ``ledger`` again: a ledger that already holds them records hits,
        a fresh ledger is refilled. Nothing is appended to ``store``.
        """
        verify_chain(checkpoints)
        if contact_id is None:
            if not checkpoints:
                raise CheckpointError("contact_id is required to restore from no checkpoints")
            contact_id = checkpoints[0].contact_id
        machine = cls(contact_id, guards, clock, ledger, store, timings, listeners)
        for cp in checkpoints:
            if cp.contact_id != contact_id or cp.from_state is not machine.state:
                raise CheckpointError(f"checkpoint {cp.seq} does not follow the machine state")
            ctx = machine._context(cp.event, cp.now_s)
            result, permit = machine._select(cp.event, ctx, _recorded(cp.guard_decisions))
            if result.row_index is None or result.decisions != cp.guard_decisions:
                raise CheckpointError(f"checkpoint {cp.seq} does not replay through the table")
            rebuilt = machine._fire(
                cp.event, result.row_index, result.decisions, permit, cp.now_s, write=False
            )
            if rebuilt.checkpoint != cp:
                raise CheckpointError(f"checkpoint {cp.seq} replays to different content")
        return machine

    # --- internals -----------------------------------------------------------------------

    def _select(
        self, event: LadderEvent, ctx: GuardContext, decide: Decide
    ) -> tuple[StepResult, GuardDecision | None]:
        """First candidate row (table order) with no guard or a permitting guard."""
        candidates = rows_for(self._state, event.kind, event.arg)
        decisions: list[GuardDecision] = []
        for index in candidates:
            row = TRANSITIONS[index]
            if row.guard is None:
                return self._pending(event, decisions, index), None
            decision = decide(row.guard, ctx)
            decisions.append(decision)
            if decision.permits and (row.dst not in CLIMB_STATES or _bounded(decision)):
                return self._pending(event, decisions, index), decision
        reason = GUARD_BLOCKED if candidates else NO_ROW
        return (
            StepResult(
                transitioned=False,
                reason=reason,
                from_state=self._state,
                to_state=self._state,
                event=event,
                decisions=tuple(decisions),
            ),
            None,
        )

    def _pending(
        self, event: LadderEvent, decisions: list[GuardDecision], index: int
    ) -> StepResult:
        return StepResult(
            transitioned=False,
            reason=TRANSITIONED,
            from_state=self._state,
            to_state=TRANSITIONS[index].dst,
            event=event,
            decisions=tuple(decisions),
            row_index=index,
        )

    def _fire(
        self,
        event: LadderEvent,
        index: int,
        decisions: tuple[GuardDecision, ...],
        permit: GuardDecision | None,
        now_s: float,
        *,
        write: bool,
    ) -> StepResult:
        row = TRANSITIONS[index]
        src = self._state
        self._commit_payload(event)
        fired_timer = event.payload.extra.get("timer")
        if isinstance(fired_timer, str):
            self.timers.cancel(fired_timer, self.contact_id)
        actions = tuple(self._act(kind, row, permit, event, now_s) for kind in row.actions)
        if row.dst in HANDLING_TERMINAL_STATES:
            self.timers.cancel_contact(self.contact_id)
        if src is LadderState.ADVISING_SLOW and row.dst is not LadderState.ADVISING_SLOW:
            self.timers.cancel(SLOW_LIMIT, self.contact_id)
        self._state = row.dst
        self._rung = rung_after(self._rung, row.dst)
        self._last_tick = max(self._last_tick, event.tick)
        records = tuple(self._ledger.execute(a) for a in actions)
        checkpoint = LadderCheckpoint.build(
            seq=self._seq,
            contact_id=self.contact_id,
            tick=event.tick,
            now_s=round6(now_s),
            from_state=src,
            event=event,
            guard_decisions=decisions,
            to_state=row.dst,
            rung=self._rung,
            action_keys=tuple(a.idempotency_key for a in actions),
            prev_hash=self._head,
        )
        self._seq += 1
        self._head = checkpoint.hash
        self._checkpoints.append(checkpoint)
        if write:
            self._store.append(checkpoint)
            self._notify(checkpoint, actions, decisions)
        return StepResult(
            transitioned=True,
            reason=TRANSITIONED,
            from_state=src,
            to_state=row.dst,
            event=event,
            decisions=decisions,
            actions=actions,
            records=records,
            checkpoint=checkpoint,
            row_index=index,
        )

    def _act(
        self,
        kind: ActionKind,
        row: TransitionRow,
        permit: GuardDecision | None,
        event: LadderEvent,
        now_s: float,
    ) -> LadderAction:
        """Build one action and apply its effect on machine memory and timers."""
        scope: ActionScope | None = None
        cid = self.contact_id
        if kind in (ActionKind.ISSUE_SLOW_ADVISORY, ActionKind.START_HOLD):
            scope = permit.granted_scope if permit is not None else None
            minutes = scope.max_minutes if scope is not None else None
            if kind is ActionKind.START_HOLD:
                self._hold_scope = scope
                self.timers.cancel(SLOW_LIMIT, cid)
            if minutes is not None:
                name = HOLD_LIMIT if kind is ActionKind.START_HOLD else SLOW_LIMIT
                self.timers.arm(Timer(name, cid, round6(now_s + minutes * 60.0)))
        elif kind is ActionKind.RELEASE_HOLD:
            scope = self._hold_scope
            self._hold_scope = None
            self.timers.cancel(HOLD_LIMIT, cid)
        elif kind is ActionKind.REQUEST_APPROVAL:
            scope = event.payload.requested_scope or self._hold_longer()
            self._requested = scope
            deadline = now_s + self._timings.primary_timeout_s
            self.timers.arm(Timer(APPROVAL_TIMEOUT, cid, round6(deadline)))
        elif kind is ActionKind.PAGE_BACKUP:
            scope = self._requested
            self._backup_paged = True
            wait = self._timings.backup_timeout_s or self._timings.primary_timeout_s
            self.timers.arm(Timer(APPROVAL_TIMEOUT, cid, round6(now_s + wait)))
        elif kind is ActionKind.RECORD_APPROVED_ACTION:
            scope = self._requested
        elif kind in (ActionKind.START_WATCH, ActionKind.UPDATE_SITREP):
            base = now_s
            deadline_s = event.payload.extra.get("deadline_s")
            if kind is ActionKind.UPDATE_SITREP and isinstance(deadline_s, (int, float)):
                base = float(deadline_s)
            interval = self._timings.sitrep_update_interval_s
            self.timers.arm(Timer(SITREP_UPDATE, cid, round6(base + interval)))
        elif kind is ActionKind.CLOSE_WATCH:
            self.timers.cancel(SITREP_UPDATE, cid)
        return make_action(cid, kind, scope, row.src, row.dst, event)

    def _hold_longer(self) -> ActionScope:
        hold = self._hold_scope
        if hold is None:
            return ActionScope(action=HOLD_LONGER)
        return hold.model_copy(update={"action": HOLD_LONGER})

    def _own_rows(self, payload: EventPayload) -> list[EvidenceRow]:
        return [r for r in payload.rows if r.contact_id == self.contact_id]

    def _merged_rows(self, payload: EventPayload) -> dict[FeatureName, EvidenceRow]:
        latest = dict(self._latest_rows)
        for row in sorted(self._own_rows(payload), key=lambda r: (r.t, r.row_id)):
            known = latest.get(row.feature)
            if known is None or (row.t, row.row_id) >= (known.t, known.row_id):
                latest[row.feature] = row
        return latest

    def _commit_payload(self, event: LadderEvent) -> None:
        p = event.payload
        self._latest_rows = self._merged_rows(p)
        self._threat_mass = p.threat_mass if p.threat_mass is not None else self._threat_mass
        self._benign_mass = p.benign_mass if p.benign_mass is not None else self._benign_mass
        self._basin = p.basin if p.basin is not None else self._basin

    def _context(self, event: LadderEvent, now_s: float) -> GuardContext:
        p = event.payload
        rows = self._merged_rows(p)

        def number(feature: FeatureName) -> float | None:
            row = rows.get(feature)
            return float(row.value) if row is not None and _is_number(row.value) else None

        criticality = number(FeatureName.ZONE_CRITICALITY)
        if criticality is not None and not 0.0 <= criticality <= 1.0:
            criticality = None
        asset = rows.get(FeatureName.NEAREST_ASSET_ID)
        asset_id = asset.value if asset is not None and isinstance(asset.value, str) else None
        if asset_id == "unknown":
            asset_id = None
        return GuardContext(
            contact_id=self.contact_id,
            state=self._state,
            event=event,
            now_s=round6(now_s),
            threat_mass=p.threat_mass if p.threat_mass is not None else self._threat_mass,
            benign_mass=p.benign_mass if p.benign_mass is not None else self._benign_mass,
            time_to_nearest_critical_asset_s=number(FeatureName.TIME_TO_NEAREST_CRITICAL_ASSET),
            zone_criticality=criticality,
            basin=p.basin if p.basin is not None else self._basin,
            nearest_asset_id=asset_id,
            active_holds=self._ledger.active_holds(exclude_contact=self.contact_id),
            backup_paged=self._backup_paged,
            approval_token=p.approval_token,
            requested_scope=p.requested_scope or self._requested,
        )

    def _notify(
        self,
        checkpoint: LadderCheckpoint | None,
        actions: Sequence[LadderAction],
        decisions: Sequence[GuardDecision],
    ) -> None:
        for listener in self._listeners:
            listener(checkpoint, actions, decisions)


def _recorded(decisions: Sequence[GuardDecision]) -> Decide:
    """A ``Decide`` that returns recorded decisions in order and checks each guard name."""
    it: Iterator[GuardDecision] = iter(decisions)

    def decide(name: GuardName, ctx: GuardContext) -> GuardDecision:
        decision = next(it, None)
        if decision is None or decision.guard is not name:
            raise CheckpointError(f"recorded decisions do not match guard {name.value}")
        return decision

    return decide
