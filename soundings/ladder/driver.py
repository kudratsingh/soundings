"""The ladder driver: turns per-contact investigation results into ladder events.

WO-043 part A. One ``LadderMachine`` per contact (``strict_holds=True``) on one shared
``TickClock``, ``ActionLedger`` and checkpoint store. Per tick, in this order, for every
contact in id order:

1. due timers fire (``LadderMachine.advance``); an ADVISING_SLOW whose slow limit is due
   gets ``standing_order_check(not_permitted)`` (the slow advisory ran out: hand off);
2. approver replies are polled once and stepped as ``reply_event`` (contact id order);
3. the feed's events for this tick, mapped as WO-043 says: the first detection is
   ``contact_detected``; the end of the watch probes is ``probe_result``; the ask outcome is
   ``stop_rule_fired(CLEARED_AT_ASK|LOST_VEHICLE)`` or ``probe_result`` (not cleared); a
   recommendation's outcome is ``stop_rule_fired(...)``; while ADVISING_SLOW,
   ``standing_order_check(hold_permitted)`` once the time to the nearest critical asset
   drops below the hold criterion (``config/ladder.yaml``), then ``not_permitted`` at the
   same tick if the hold guard blocked; ``scope_exceeded`` only while HOLDING with a
   requested scope in hand that the signed orders do not cover (WO-041 ``exceeds_scope``);
4. the departure hook (``DeparturePort``) observes the contact's point; a departure is
   stepped while HOLDING or WATCH_AFTER_HANDOFF.

Every action is forwarded: ``request_approval`` to the primary channel, ``page_backup`` to
the backup. A transition into HANDED_OFF is followed by ``handoff_complete`` in the same
tick. Once the observations end, the clock keeps advancing for up to ``max_tail_ticks``
while a handling timer is pending; only timer and approval events fire in that tail.

CLAUDE.md 2.2: the driver decides only *when* to emit an event; every transition is the
table's, on guard decisions over evidence rows. CLAUDE.md 2.4: no clock read but the
``TickClock``; contacts, replies and timers are processed in sorted order.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from soundings.canonical import canonical_json, round6, stable_hash
from soundings.clock import TickClock
from soundings.schemas import (
    HANDLING_TERMINAL_STATES,
    TERMINAL_STATES,
    EvidenceRow,
    LadderState,
    Rung,
    ToggleName,
    TrackPoint,
    load_ladder_config,
)
from soundings.schemas.config import LADDER_PATH, STANDING_ORDERS_PATH

from .actions import ActionLedger, LadderAction
from .approver_sim import ApproverScript, SimulatedApprover
from .boundary import LadderCriteria, load_boundary
from .checkpoint import LadderCheckpoint, MemoryCheckpointStore
from .guard_port import GuardPort, NoStandingOrdersGuards
from .guards import ApproverStatus, BoundaryGuards, GuardInputs, exceeds_scope
from .machine import SITREP_UPDATE, SLOW_LIMIT, LadderMachine, StepResult
from .ports import ApproverPort, DeparturePort, LadderListener, reply_event
from .sitrep_port import (
    UNREACHABLE,
    RenderedSitrep,
    SitrepInputs,
    SitrepKind,
    SitrepWriterLike,
    StubSitrepWriter,
)
from .standing_orders import effective_scope, load_verified, scenario_now
from .types import (
    EVENT_ARGS,
    ActionKind,
    ActionScope,
    EventKind,
    EventPayload,
    GuardDecision,
    GuardName,
    LadderEvent,
    LadderTimings,
)
from .watch import WatchSummary, build_update_inputs

PRIMARY_CHANNEL = "ui"
BACKUP_CHANNEL = "sms_sim"
"""Channels used when no signed orders name them (the unsigned path never asks anyone)."""

STOP_OUTCOMES = frozenset(EVENT_ARGS[EventKind.STOP_RULE_FIRED])
ASK_OUTCOMES = frozenset({"CLEARED_AT_ASK", "LOST_VEHICLE"})
INVESTIGATION_OUTCOMES = frozenset({"RESOLVED_BENIGN", "HAZARD_NOTICE", "HANDOFF"})


def token_ttl_s_from_config(path: str | Path = LADDER_PATH) -> float:
    """``approval.token_ttl_s`` from ``config/ladder.yaml`` (DECISIONS.md 23:25)."""
    approval = load_ladder_config(path).approval
    if "token_ttl_s" not in approval:
        raise ValueError("config/ladder.yaml lacks approval.token_ttl_s")
    return float(approval["token_ttl_s"])


# --- what the driver consumes ------------------------------------------------------------


class RecommendationLike(Protocol):
    """A per-tick investigation snapshot for one contact (WO-033's ``Recommendation``).

    ``outcome`` is a ``stop_rule_fired`` argument (``RESOLVED_BENIGN``, ``HAZARD_NOTICE``,
    ``HANDOFF``) or ``None`` while the investigation continues. ``rows`` are the evidence
    rows behind it; the guards read those, the driver reads the numbers only to decide when
    to emit ``standing_order_check``.
    """

    @property
    def outcome(self) -> str | None: ...
    @property
    def recommended_rung(self) -> Rung | None: ...
    @property
    def threat_mass(self) -> float | None: ...
    @property
    def benign_mass(self) -> float | None: ...
    @property
    def rows(self) -> Sequence[EvidenceRow]: ...
    @property
    def basin(self) -> str | None: ...
    @property
    def nearest_asset_id(self) -> str | None: ...
    @property
    def time_to_nearest_critical_asset(self) -> float | None: ...
    @property
    def tick(self) -> int: ...


@dataclass(frozen=True)
class Snapshot:
    """A plain ``RecommendationLike`` (synthetic streams and tests)."""

    tick: int
    rows: tuple[EvidenceRow, ...] = ()
    outcome: str | None = None
    recommended_rung: Rung | None = None
    threat_mass: float | None = None
    benign_mass: float | None = None
    basin: str | None = None
    nearest_asset_id: str | None = None
    time_to_nearest_critical_asset: float | None = None


@dataclass(frozen=True)
class ContactFeed:
    """One contact's investigation results, in run ticks. Part B builds these from runner v2.

    ``points`` are the contact's observed positions (absent tick = no detection);
    ``scope_requests`` are scopes asked of the platform while it holds (tick, scope);
    ``probe_calls`` are carried into event payloads for the audit and read by no guard.
    """

    contact_id: str
    detected_tick: int
    detection_rows: tuple[EvidenceRow, ...] = ()
    watch_tick: int | None = None
    watch_rows: tuple[EvidenceRow, ...] = ()
    ask_tick: int | None = None
    ask_outcome: str | None = None
    ask_rows: tuple[EvidenceRow, ...] = ()
    recommendations: tuple[RecommendationLike, ...] = ()
    points: tuple[TrackPoint, ...] = ()
    scope_requests: tuple[tuple[int, ActionScope], ...] = ()
    probe_calls: tuple[Mapping[str, Any], ...] = ()

    def __post_init__(self) -> None:
        if self.ask_outcome is not None and self.ask_outcome not in ASK_OUTCOMES:
            raise ValueError(f"ask_outcome must be one of {sorted(ASK_OUTCOMES)}")

    @property
    def last_tick(self) -> int:
        ticks = [self.detected_tick]
        ticks += [t for t in (self.watch_tick, self.ask_tick) if t is not None]
        ticks += [r.tick for r in self.recommendations]
        ticks += [p.tick for p in self.points]
        ticks += [t for t, _ in self.scope_requests]
        return max(ticks)


# --- what the driver returns -------------------------------------------------------------


@dataclass(frozen=True)
class LadderRun:
    """One contact's ladder run. ``handling_outcome``: first state in HANDLING_TERMINAL_STATES."""

    contact_id: str
    checkpoints: tuple[LadderCheckpoint, ...]
    actions: tuple[LadderAction, ...]
    guard_decisions: tuple[GuardDecision, ...]
    handling_outcome: LadderState | None
    final_state: LadderState
    interruptions: int
    sitreps: tuple[RenderedSitrep, ...] = ()
    watch: WatchSummary | None = None

    @property
    def visited(self) -> tuple[LadderState, ...]:
        return tuple(cp.to_state for cp in self.checkpoints)

    def canonical(self) -> dict[str, Any]:
        """The run as plain data (CLI output, determinism diffs)."""
        return {
            "contact_id": self.contact_id,
            "handling_outcome": None if self.handling_outcome is None else self.handling_outcome,
            "final_state": self.final_state,
            "interruptions": self.interruptions,
            "checkpoints": [
                {
                    "seq": cp.seq,
                    "tick": cp.tick,
                    "from": cp.from_state,
                    "event": cp.event.kind,
                    "arg": cp.event.arg,
                    "to": cp.to_state,
                    "rung": cp.rung,
                    "hash": cp.hash,
                }
                for cp in self.checkpoints
            ],
            "actions": [
                {"kind": a.kind, "tick": a.tick, "scope": a.scope, "key": a.idempotency_key}
                for a in self.actions
            ],
            "guard_decisions": [
                {"guard": d.guard, "verdict": d.verdict, "reason": d.reason, "hash": d.inputs_hash}
                for d in self.guard_decisions
            ],
            "sitreps": [{"kind": s.kind, "tick": s.tick, "digest": s.digest} for s in self.sitreps],
        }


def transition_log(runs: Mapping[str, LadderRun]) -> str:
    """One canonical JSON line per checkpoint, contacts in id order (the determinism log)."""
    lines = [
        canonical_json(
            {
                "contact_id": cp.contact_id,
                "seq": cp.seq,
                "tick": cp.tick,
                "from": cp.from_state,
                "to": cp.to_state,
                "rung": cp.rung,
                "hash": cp.hash,
            }
        )
        for cid in sorted(runs)
        for cp in runs[cid].checkpoints
    ]
    return "".join(f"{line}\n" for line in lines)


# --- the driver --------------------------------------------------------------------------


def _channels(guards: GuardPort) -> tuple[str, str]:
    if isinstance(guards, BoundaryGuards) and guards.standing.orders is not None:
        approvers = guards.standing.orders.orders.approvers
        primary = approvers["primary"].channel if "primary" in approvers else PRIMARY_CHANNEL
        backup = approvers["backup"].channel if "backup" in approvers else BACKUP_CHANNEL
        return primary, backup
    return PRIMARY_CHANNEL, BACKUP_CHANNEL


def default_tail_ticks(timings: LadderTimings, hold_max_minutes: float, tick_s: float) -> int:
    """``ceil((hold minutes * 60 + primary + backup + 2 * update + departed) / tick_s)``."""
    total = (
        hold_max_minutes * 60.0
        + timings.primary_timeout_s
        + (timings.backup_timeout_s or 0.0)
        + 2.0 * timings.sitrep_update_interval_s
        + timings.departed_after_s_outside_zone
    )
    return math.ceil(round6(total / tick_s))


class LadderDriver:
    """Drives one ``LadderMachine`` per contact from ``ContactFeed``s."""

    def __init__(
        self,
        guards: GuardPort,
        clock: TickClock,
        timings: LadderTimings,
        approver: ApproverPort,
        listeners: Sequence[LadderListener] = (),
        departure: DeparturePort | None = None,
        max_tail_ticks: int = 0,
        *,
        sitrep_writer: SitrepWriterLike | None = None,
        criteria: LadderCriteria | None = None,
    ) -> None:
        if max_tail_ticks < 0:
            raise ValueError("max_tail_ticks must be non-negative")
        self.guards = guards
        self.clock = clock
        self.timings = timings
        self.approver = approver
        self.listeners = tuple(listeners)
        self.departure = departure
        self.max_tail_ticks = max_tail_ticks
        self.sitrep_writer: SitrepWriterLike = sitrep_writer or StubSitrepWriter()
        self.criteria = criteria or load_boundary().criteria
        self.primary_channel, self.backup_channel = _channels(guards)

    @classmethod
    def from_config(
        cls,
        *,
        tick_s: float,
        hour_of_day: int,
        orders_path: str | Path | None = STANDING_ORDERS_PATH,
        unsigned: bool = False,
        toggles: Iterable[ToggleName | str] = (),
        approver: ApproverPort | None = None,
        script: ApproverScript | None = None,
        departure: DeparturePort | None = None,
        max_tail_ticks: int | None = None,
        listeners: Sequence[LadderListener] = (),
        sitrep_writer: SitrepWriterLike | None = None,
        ladder_path: str | Path = LADDER_PATH,
    ) -> LadderDriver:
        """A driver over the real guards, orders, timings and simulated approver.

        ``orders_path=None`` or ``unsigned=True``: ``NoStandingOrdersGuards``. Otherwise the
        orders are verified at ``scenario_now(hour_of_day, 0)``, and ``BoundaryGuards``
        re-checks them on every decision from that epoch. ``standing_orders_expired`` moves
        the epoch to the orders' ``valid_until``, so they are expired from run-clock zero
        (DECISIONS.md 23:25). The approver defaults to ``SimulatedApprover`` over
        ``ApproverScript.from_toggles(toggles)`` with ``approval.token_ttl_s`` from config.
        """
        names = tuple(sorted({ToggleName(t) for t in toggles}))
        clock = TickClock(tick_s=tick_s)
        boundary = load_boundary(ladder_path)
        guards: GuardPort
        timings_orders: str | Path = STANDING_ORDERS_PATH
        if unsigned or orders_path is None:
            guards = NoStandingOrdersGuards()
        else:
            timings_orders = orders_path
            epoch = scenario_now(hour_of_day, 0.0)
            status = load_verified(orders_path, now=epoch)
            if ToggleName.STANDING_ORDERS_EXPIRED in names and status.orders is not None:
                epoch = status.orders.valid_until
                status = status.at(epoch)
            guards = BoundaryGuards(
                status, boundary, ApproverStatus.from_toggles(names), epoch=epoch
            )
        timings = LadderTimings.from_config(ladder_path, timings_orders)
        primary, backup = _channels(guards)
        if approver is None:
            approver = SimulatedApprover(
                script or ApproverScript.from_toggles(names),
                clock,
                primary_channel=primary,
                backup_channel=backup,
                token_ttl_s=token_ttl_s_from_config(ladder_path),
            )
        if max_tail_ticks is None:
            hold_minutes = 0.0
            if isinstance(guards, BoundaryGuards) and guards.standing.orders is not None:
                hold_minutes = float(guards.standing.orders.orders.scope.hold.max_minutes)
            max_tail_ticks = default_tail_ticks(timings, hold_minutes, tick_s)
        return cls(
            guards,
            clock,
            timings,
            approver,
            listeners,
            departure,
            max_tail_ticks,
            sitrep_writer=sitrep_writer,
            criteria=boundary.criteria,
        )

    def start(self, feeds: Sequence[ContactFeed]) -> DriverSession:
        return DriverSession(self, feeds)

    def run(self, feeds: Sequence[ContactFeed]) -> dict[str, LadderRun]:
        session = self.start(feeds)
        while not session.done:
            session.step()
        return session.results()


@dataclass
class _Contact:
    feed: ContactFeed
    machine: LadderMachine
    recs: dict[int, list[RecommendationLike]]
    points: dict[int, TrackPoint]
    requests: dict[int, list[ActionScope]]
    rows: dict[str, EvidenceRow] = field(default_factory=dict)
    decisions: list[GuardDecision] = field(default_factory=list)
    sitreps: list[RenderedSitrep] = field(default_factory=list)
    latest: RecommendationLike | None = None
    hold_checked: bool = False
    handling_outcome: LadderState | None = None
    departed_at_s: float | None = None
    updates: int = 0


class DriverSession:
    """A driver run stepped one tick at a time (runner v3 and the UI step it)."""

    def __init__(self, driver: LadderDriver, feeds: Sequence[ContactFeed]) -> None:
        ids = [f.contact_id for f in feeds]
        if len(set(ids)) != len(ids):
            raise ValueError("one feed per contact")
        self.driver = driver
        self.clock = driver.clock
        self.ledger = ActionLedger()
        self.store = MemoryCheckpointStore()
        self._contacts: dict[str, _Contact] = {}
        for feed in sorted(feeds, key=lambda f: f.contact_id):
            recs: dict[int, list[RecommendationLike]] = {}
            for rec in feed.recommendations:
                recs.setdefault(rec.tick, []).append(rec)
            requests: dict[int, list[ActionScope]] = {}
            for tick, scope in feed.scope_requests:
                requests.setdefault(tick, []).append(scope)
            machine = LadderMachine(
                feed.contact_id,
                driver.guards,
                self.clock,
                self.ledger,
                self.store,
                driver.timings,
                driver.listeners,
                strict_holds=True,
            )
            self._contacts[feed.contact_id] = _Contact(
                feed, machine, recs, {p.tick: p for p in feed.points}, requests
            )
        self.last_observed_tick = max((f.last_tick for f in feeds), default=0)
        self._started = False
        self._done = not feeds
        self._tail = 0

    # --- public ------------------------------------------------------------------------

    @property
    def done(self) -> bool:
        return self._done

    @property
    def tick(self) -> int:
        return self.clock.tick

    def step(self) -> list[StepResult]:
        """Process one tick (the first call processes the clock's current tick)."""
        if self._done:
            return []
        if self._started:
            self.clock.advance()
        self._started = True
        out: list[StepResult] = []
        tick = self.clock.tick
        observing = tick <= self.last_observed_tick
        for c in self._contacts.values():
            self._timers(c, tick, out)
        for reply in self.driver.approver.poll(self.clock.now_s()):
            target = self._contacts.get(reply.contact_id)
            if target is not None:
                self._step(target, reply_event(reply, tick), out)
        if observing:
            for c in self._contacts.values():
                self._feed(c, tick, out)
        else:
            self._tail += 1
        self._done = not observing and (
            self._tail >= self.driver.max_tail_ticks or not self._pending()
        )
        if tick == self.last_observed_tick and not self._pending():
            self._done = True
        if all(c.machine.state in TERMINAL_STATES for c in self._contacts.values()):
            self._done = True
        return out

    def pending_approvals(self) -> list[str]:
        """Contacts sitting in AWAITING_APPROVAL, in id order."""
        return [
            cid
            for cid, c in self._contacts.items()
            if c.machine.state is LadderState.AWAITING_APPROVAL
        ]

    def results(self) -> dict[str, LadderRun]:
        out: dict[str, LadderRun] = {}
        for cid, c in self._contacts.items():
            m = c.machine
            watch = None
            if c.handling_outcome is LadderState.HANDED_OFF:
                last = getattr(self.driver.departure, "last_known", None)
                watch = WatchSummary(
                    contact_id=cid,
                    updates=c.updates,
                    departed_at_s=c.departed_at_s,
                    last_known=last(cid) if callable(last) else None,
                )
            out[cid] = LadderRun(
                contact_id=cid,
                checkpoints=m.checkpoints,
                actions=tuple(r.action for r in self.ledger.records(cid)),
                guard_decisions=tuple(c.decisions),
                handling_outcome=c.handling_outcome,
                final_state=m.state,
                interruptions=self.ledger.interruptions(cid),
                sitreps=tuple(c.sitreps),
                watch=watch,
            )
        return out

    # --- per tick ----------------------------------------------------------------------

    def _pending(self) -> bool:
        """A handling timer (anything but the watch's update timer) is still pending."""
        for c in self._contacts.values():
            if c.machine.state in HANDLING_TERMINAL_STATES:
                continue
            if c.machine.state is LadderState.WATCH_AFTER_HANDOFF:
                continue
            if any(t.name != SITREP_UPDATE for t in c.machine.timers.pending()):
                return True
        return False

    def _timers(self, c: _Contact, tick: int, out: list[StepResult]) -> None:
        for result in c.machine.advance(tick):
            self._after(c, result, out)
        m = c.machine
        if m.state is LadderState.ADVISING_SLOW and any(
            t.name == SLOW_LIMIT and t.contact_id == c.feed.contact_id
            for t in m.timers.due(self.clock)
        ):
            self._step(c, self._event(c, EventKind.STANDING_ORDER_CHECK, "not_permitted"), out)

    def _feed(self, c: _Contact, tick: int, out: list[StepResult]) -> None:
        f = c.feed
        if tick == f.detected_tick:
            self._step(c, self._event(c, EventKind.CONTACT_DETECTED, None, f.detection_rows), out)
        if tick == f.watch_tick:
            self._step(c, self._event(c, EventKind.PROBE_RESULT, None, f.watch_rows), out)
        if tick == f.ask_tick:
            if f.ask_outcome is None:
                ev = self._event(c, EventKind.PROBE_RESULT, None, f.ask_rows)
            else:
                ev = self._event(c, EventKind.STOP_RULE_FIRED, f.ask_outcome, f.ask_rows)
            self._step(c, ev, out)
        for rec in c.recs.get(tick, ()):
            self._recommendation(c, rec, out)
        for scope in c.requests.get(tick, ()):
            self._scope_request(c, scope, out)
        if self.driver.departure is not None and tick >= f.detected_tick:
            departed = self.driver.departure.observe(f.contact_id, tick, c.points.get(tick))
            if departed and c.machine.state in (
                LadderState.HOLDING,
                LadderState.WATCH_AFTER_HANDOFF,
            ):
                self._step(c, self._event(c, EventKind.CONTACT_DEPARTED, None), out)
                if c.machine.state is LadderState.RESOLVED_DEPARTED:
                    c.departed_at_s = round6(self.clock.now_s())

    def _recommendation(self, c: _Contact, rec: RecommendationLike, out: list[StepResult]) -> None:
        c.latest = rec
        rows = tuple(rec.rows)
        state = c.machine.state
        if state is LadderState.INVESTIGATING and rec.outcome in INVESTIGATION_OUTCOMES:
            extra = {"recommendation_hash": _rec_hash(rec)} if rec.outcome == "HANDOFF" else {}
            self._step(
                c, self._rec_event(c, rec, EventKind.STOP_RULE_FIRED, rec.outcome, extra), out
            )
        elif state is LadderState.ADVISING_SLOW and not c.hold_checked:
            tta = rec.time_to_nearest_critical_asset
            if tta is not None and tta < self.driver.criteria.hold.time_to_critical_asset_max_s:
                c.hold_checked = True
                check = EventKind.STANDING_ORDER_CHECK
                r = self._step(c, self._rec_event(c, rec, check, "hold_permitted"), out)
                if not r.transitioned:
                    self._step(c, self._rec_event(c, rec, check, "not_permitted"), out)
        else:
            self._remember(c, rows)

    def _scope_request(self, c: _Contact, scope: ActionScope, out: list[StepResult]) -> None:
        """``scope_exceeded`` only while HOLDING, and only when the orders do not cover it."""
        m = c.machine
        guards = self.driver.guards
        if m.state is not LadderState.HOLDING or not isinstance(guards, BoundaryGuards):
            return
        now_s = round6(self.clock.now_s())
        status = guards.status_at(now_s)
        reasons = guards.approver_status.degraded_reasons()
        inputs = GuardInputs(
            contact_id=c.feed.contact_id,
            now_s=now_s,
            standing_orders_signed=status.signed,
            standing_orders_scope=effective_scope(status, reasons),
            active_holds=self.ledger.active_holds(exclude_contact=c.feed.contact_id),
            requested=scope,
            degraded_reasons=reasons,
        )
        if exceeds_scope(inputs, self.driver.criteria):
            ev = LadderEvent(
                kind=EventKind.SCOPE_EXCEEDED,
                tick=self.clock.tick,
                payload=EventPayload(requested_scope=scope, extra=self._extra(c)),
            )
            self._step(c, ev, out)

    # --- stepping helpers --------------------------------------------------------------

    def _extra(self, c: _Contact, more: Mapping[str, Any] | None = None) -> dict[str, Any]:
        extra: dict[str, Any] = {"probe_calls": [dict(p) for p in c.feed.probe_calls]}
        if more:
            extra.update(more)
        return extra

    def _event(
        self,
        c: _Contact,
        kind: EventKind,
        arg: str | None,
        rows: Sequence[EvidenceRow] = (),
    ) -> LadderEvent:
        self._remember(c, rows)
        return LadderEvent(
            kind=kind,
            arg=arg,
            tick=self.clock.tick,
            payload=EventPayload(
                rows=tuple(rows),
                extra=self._extra(c, {"row_ids": [r.row_id for r in rows]}),
            ),
        )

    def _rec_event(
        self,
        c: _Contact,
        rec: RecommendationLike,
        kind: EventKind,
        arg: str | None,
        more: Mapping[str, Any] | None = None,
    ) -> LadderEvent:
        rows = tuple(rec.rows)
        self._remember(c, rows)
        extra = {"row_ids": [r.row_id for r in rows], **(more or {})}
        return LadderEvent(
            kind=kind,
            arg=arg,
            tick=self.clock.tick,
            payload=EventPayload(
                rows=rows,
                threat_mass=rec.threat_mass,
                benign_mass=rec.benign_mass,
                basin=rec.basin,
                extra=self._extra(c, extra),
            ),
        )

    def _remember(self, c: _Contact, rows: Iterable[EvidenceRow]) -> None:
        for r in rows:
            if r.contact_id == c.feed.contact_id:
                c.rows.setdefault(r.row_id, r)

    def _step(self, c: _Contact, event: LadderEvent, out: list[StepResult]) -> StepResult:
        result = c.machine.step(event)
        self._after(c, result, out)
        return result

    def _after(self, c: _Contact, result: StepResult, out: list[StepResult]) -> None:
        out.append(result)
        c.decisions.extend(result.decisions)
        if result.transitioned and c.handling_outcome is None:
            if result.to_state in HANDLING_TERMINAL_STATES:
                c.handling_outcome = result.to_state
        now_s = self.clock.now_s()
        unreachable = any(
            d.guard is GuardName.NO_BACKUP and d.permits and d.reason == UNREACHABLE
            for d in result.decisions
        )
        for action in result.actions:
            if action.kind is ActionKind.REQUEST_APPROVAL and action.scope is not None:
                self.driver.approver.request(
                    action.contact_id, action.scope, self.driver.primary_channel, now_s
                )
                self._sitrep(c, "draft", action.scope, ())
            elif action.kind is ActionKind.PAGE_BACKUP and action.scope is not None:
                self.driver.approver.request(
                    action.contact_id, action.scope, self.driver.backup_channel, now_s
                )
            elif action.kind is ActionKind.DELIVER_SITREP:
                self._sitrep(c, "final", None, (UNREACHABLE,) if unreachable else ())
            elif action.kind is ActionKind.UPDATE_SITREP:
                self._update(c)
        if result.transitioned and result.to_state is LadderState.HANDED_OFF:
            done = LadderEvent(kind=EventKind.HANDOFF_COMPLETE, tick=self.clock.tick)
            self._step(c, done, out)

    # --- SITREPs -----------------------------------------------------------------------

    def _base_inputs(
        self, c: _Contact, kind: SitrepKind, scope: ActionScope | None, flags: tuple[str, ...]
    ) -> SitrepInputs:
        rec = c.latest
        latest: dict[str, EvidenceRow] = {}
        for r in sorted(c.rows.values(), key=lambda r: (r.t, r.row_id)):
            latest[r.feature.value] = r
        rows = sorted(latest.values(), key=lambda r: (r.t, r.row_id))
        return SitrepInputs(
            kind=kind,
            contact_id=c.feed.contact_id,
            tick=self.clock.tick,
            now_s=round6(self.clock.now_s()),
            state=c.machine.state,
            rung=c.machine.rung,
            recommended_rung=None if rec is None else rec.recommended_rung,
            threat_mass=None if rec is None else rec.threat_mass,
            benign_mass=None if rec is None else rec.benign_mass,
            basin=None if rec is None else rec.basin,
            nearest_asset_id=None if rec is None else rec.nearest_asset_id,
            time_to_nearest_critical_asset_s=(
                None if rec is None else rec.time_to_nearest_critical_asset
            ),
            requested_scope=scope,
            cited_row_ids=tuple(r.row_id for r in rows),
            flags=flags,
        )

    def _render(self, c: _Contact, inputs: SitrepInputs) -> None:
        # --- WO-042 begin ---
        # A writer that also takes the contact's evidence rows (WO-042 ``HandoffService``)
        # gets every row the driver kept for the contact; any other ``SitrepWriterLike``
        # renders from the inputs alone, exactly as before.
        with_rows = getattr(self.driver.sitrep_writer, "render_with_rows", None)
        if callable(with_rows):
            text = str(with_rows(inputs, tuple(c.rows.values())))
        else:
            text = self.driver.sitrep_writer.render(inputs)
        # --- WO-042 end ---
        c.sitreps.append(RenderedSitrep.build(inputs, text))

    def _sitrep(
        self, c: _Contact, kind: SitrepKind, scope: ActionScope | None, flags: tuple[str, ...]
    ) -> None:
        self._render(c, self._base_inputs(c, kind, scope, flags))

    def _update(self, c: _Contact) -> None:
        previous = c.sitreps[-1].inputs if c.sitreps else None
        dep = self.driver.departure
        last_known = getattr(dep, "last_known", None)
        lost_at = getattr(dep, "track_lost_at", None)
        inputs = build_update_inputs(
            contact_id=c.feed.contact_id,
            tick=self.clock.tick,
            now_s=self.clock.now_s(),
            state=c.machine.state,
            rung=c.machine.rung,
            rows=c.rows.values(),
            since_tick=-1 if previous is None else previous.tick,
            base=previous,
            last_known=last_known(c.feed.contact_id) if callable(last_known) else None,
            track_lost_at_tick=lost_at(c.feed.contact_id) if callable(lost_at) else None,
        )
        c.updates += 1
        self._render(c, inputs)


def _rec_hash(rec: RecommendationLike) -> str:
    return stable_hash(
        {
            "tick": rec.tick,
            "outcome": rec.outcome,
            "recommended_rung": rec.recommended_rung,
            "threat_mass": rec.threat_mass,
            "benign_mass": rec.benign_mass,
            "basin": rec.basin,
            "nearest_asset_id": rec.nearest_asset_id,
            "time_to_nearest_critical_asset": rec.time_to_nearest_critical_asset,
            "row_ids": [r.row_id for r in rec.rows],
        }
    )


__all__ = [
    "ContactFeed",
    "DriverSession",
    "LadderDriver",
    "LadderRun",
    "RecommendationLike",
    "Snapshot",
    "default_tail_ticks",
    "token_ttl_s_from_config",
    "transition_log",
]
