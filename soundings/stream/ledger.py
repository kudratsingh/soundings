"""The rung ledger and the blunt-policy counter beside the map (WO-086).

``RungLedger`` reads the streamed ``tick`` and ``transition`` events and keeps, per observed
contact, the bucket of its max rung so far: resolved at watch, cleared at ask, advisories,
holds, handed off. It counts with ``soundings.eval.policies`` (``max_rung`` through
``contact_max_rung`` and ``LEDGER_BUCKET_OF_RUNG``), so the screen and the eval report agree
on the same run by construction.

``BluntCounter`` replays WO-050's ``bang`` baseline on the same seeded sim run in a worker
thread: "berth closures under the blunt policy", one per unknown contact a closure covers,
with distinct closure episodes as a secondary number. It is a counterfactual: the approvals
are assumed (``config/eval.yaml``), nothing it counts happened. Its sim run must hash to the
stream's ``run_finished.track_hash``, or the counter shows unavailable and never a guess.

``LedgerAnnotator`` inserts a ``ledger`` event right after the ``tick`` (and its per-tick
overlays) or the ``transition`` that changed a count and re-stamps ``seq`` contiguously.
The worker is joined before the first event is released, so thread timing never changes
what is streamed or when (CLAUDE.md 2.4). Display only (CLAUDE.md 2.2): nothing here sets
a rung, proposes an action or writes back into the ladder; this module imports no model
client and no ladder machine or guard.
"""

from __future__ import annotations

import bisect
import math
import threading
from collections.abc import Callable, Iterable, Sequence

from soundings.canonical import round6
from soundings.eval.policies import (
    CLOSURE_ACTION,
    LEDGER_BUCKET_OF_RUNG,
    BangPolicy,
    LedgerBucket,
    PolicyEvent,
    ShutdownInterval,
    StateStep,
    Transition,
    closure_contacts,
    contact_max_rung,
    contact_spans,
    shutdown_minutes,
)
from soundings.schemas.scenario import Scenario
from soundings.sim.hashing import track_hash
from soundings.sim.run import run_scenario
from soundings.stream.events import (
    BluntPayload,
    ErrorEvent,
    ErrorPayload,
    LedgerEvent,
    LedgerPayload,
    RunFinished,
    StreamEvent,
)

TICK_OVERLAYS: frozenset[str] = frozenset({"sensor_ranges", "responder"})
"""Per-tick overlay events that stay right behind their ``tick`` (WO-088, WO-089)."""

UNAVAILABLE_MESSAGE = "blunt counter unavailable: its sim run does not match this stream"


class RungLedger:
    """Contacts per ledger bucket over the streamed events (each contact once)."""

    def __init__(self) -> None:
        self._steps: dict[str, list[StateStep]] = {}
        self._bucket: dict[str, LedgerBucket] = {}

    def observe(self, event: StreamEvent) -> bool:
        """Fold one event in; True when a count changed."""
        changed = False
        if event.type == "tick":
            for c in event.payload.contacts:
                if c.contact_id not in self._bucket:
                    self._steps[c.contact_id] = []
                    self._bucket[c.contact_id] = LEDGER_BUCKET_OF_RUNG[contact_max_rung(())]
                    changed = True
        elif event.type == "transition":
            p = event.payload
            steps = self._steps.setdefault(p.contact_id, [])
            steps.append(p)
            bucket = LEDGER_BUCKET_OF_RUNG[contact_max_rung(steps)]
            if self._bucket.get(p.contact_id) is not bucket:
                self._bucket[p.contact_id] = bucket
                changed = True
        return changed

    def counts(self) -> dict[LedgerBucket, int]:
        out = {b: 0 for b in LedgerBucket}
        for bucket in self._bucket.values():
            out[bucket] += 1
        return out

    @property
    def contacts_seen(self) -> int:
        return len(self._bucket)


class BluntCounter:
    """WO-050's ``bang`` baseline on the same seeded contact stream, computed in a worker.

    ``worker_hook`` runs at the start of the worker (tests delay it to prove that thread
    timing never changes the stream).
    """

    def __init__(
        self,
        scenario: Scenario,
        seed: int,
        *,
        policy: BangPolicy | None = None,
        worker_hook: Callable[[], None] | None = None,
    ) -> None:
        self.scenario = scenario
        self.seed = seed
        self.policy = policy
        self.worker_hook = worker_hook
        self._thread: threading.Thread | None = None
        self._done = False
        self.error: str | None = None
        self.track_hash: str | None = None
        self.tick_s = 1.0
        self.n_contacts = 0
        self.transitions: tuple[Transition, ...] = ()
        self._closure_ticks: list[int] = []
        self._episode_ticks: list[int] = []
        self._shutdowns: tuple[ShutdownInterval, ...] = ()

    def start(self) -> BluntCounter:
        if self._thread is None and not self._done:
            self._thread = threading.Thread(target=self._compute, name="blunt-counter", daemon=True)
            self._thread.start()
        return self

    def join(self) -> BluntCounter:
        """Wait for the worker (or compute inline when never started)."""
        if self._thread is not None:
            self._thread.join()
            self._thread = None
        elif not self._done:
            self._compute()
        return self

    def _compute(self) -> None:
        try:
            if self.worker_hook is not None:
                self.worker_hook()
            policy = self.policy if self.policy is not None else BangPolicy()
            sim = run_scenario(self.scenario, seed=self.seed)
            spans = contact_spans(sim)
            end_s = round6(sim.n_ticks * sim.tick_s)
            transitions, shutdowns = policy.plan(spans, end_s=end_s, tick_s=sim.tick_s)
            first_tick: dict[str, int] = {}
            for t in transitions:
                if t.contact_id in first_tick:
                    continue
                if t.event is PolicyEvent.COVERED_BY_CLOSURE or (
                    t.event is PolicyEvent.APPROVAL_REQUESTED and t.action == CLOSURE_ACTION
                ):
                    first_tick[t.contact_id] = t.tick
            covered = closure_contacts(transitions)
            assert set(covered) == set(first_tick)
            self._closure_ticks = sorted(first_tick.values())
            self._episode_ticks = sorted(
                t.tick
                for t in transitions
                if t.event is PolicyEvent.APPROVAL_REQUESTED and t.action == CLOSURE_ACTION
            )
            self._shutdowns = shutdowns
            self.transitions = transitions
            self.tick_s = float(sim.tick_s)
            self.n_contacts = len(spans)
            self.track_hash = track_hash(sim)
        except Exception as exc:  # the counter shows unavailable; the stream goes on
            self.error = f"{type(exc).__name__}: {exc}"
        finally:
            self._done = True

    @property
    def available(self) -> bool:
        return self._done and self.error is None and self.track_hash is not None

    def _tick_of(self, t_s: float) -> int:
        return math.floor(round6(t_s / self.tick_s))

    def closures_at(self, t_s: float) -> int:
        """Berth closures so far: contacts whose closure step is at or before ``t_s``'s tick."""
        return bisect.bisect_right(self._closure_ticks, self._tick_of(t_s))

    def episodes_at(self, t_s: float) -> int:
        return bisect.bisect_right(self._episode_ticks, self._tick_of(t_s))

    def minutes_at(self, t_s: float) -> float:
        """Closure minutes up to ``t_s`` (union per berth, as the eval's shutdown minutes)."""
        clipped = [
            s.model_copy(update={"end_s": min(s.end_s, t_s)})
            for s in self._shutdowns
            if s.start_s < t_s
        ]
        return shutdown_minutes(clipped)


class LedgerAnnotator:
    """Interleave ``ledger`` events into a run's stream and re-stamp ``seq`` from 0."""

    def __init__(self, blunt: BluntCounter, *, expected_track_hash: str | None = None) -> None:
        self.blunt = blunt
        self.ledger = RungLedger()
        self.expected_track_hash = expected_track_hash
        self._seq = 0
        self._last: tuple[object, ...] | None = None
        self._pending: StreamEvent | None = None
        self._blunt_ok: bool | None = None

    def _blunt_available(self) -> bool:
        if self._blunt_ok is None:
            self.blunt.join()
            self._blunt_ok = self.blunt.available and (
                self.expected_track_hash is None
                or self.blunt.track_hash == self.expected_track_hash
            )
        return self._blunt_ok

    def _blunt_at(self, t_s: float) -> BluntPayload:
        if not self._blunt_available():
            return BluntPayload(berth_closures=0, episodes=0, closure_minutes=0.0, available=False)
        return BluntPayload(
            berth_closures=self.blunt.closures_at(t_s),
            episodes=self.blunt.episodes_at(t_s),
            closure_minutes=self.blunt.minutes_at(t_s),
            available=True,
        )

    def _stamp(self, event: StreamEvent) -> StreamEvent:
        update: dict[str, object] = {"seq": self._seq}
        if isinstance(event, RunFinished):
            update["payload"] = event.payload.model_copy(update={"n_events": self._seq + 1})
        self._seq += 1
        return event.model_copy(update=update)

    def _ledger_event(self, event: StreamEvent) -> StreamEvent | None:
        blunt = self._blunt_at(event.t_s)
        counts = self.ledger.counts()
        key = (
            tuple(counts.values()),
            blunt.berth_closures,
            blunt.episodes,
            blunt.available,
        )
        if key == self._last:
            return None
        self._last = key
        return LedgerEvent(
            run_id=event.run_id,
            seq=0,
            tick=event.tick,
            t_s=event.t_s,
            payload=LedgerPayload(
                counts=counts, contacts_seen=self.ledger.contacts_seen, blunt=blunt
            ),
        )

    def _error_event(self, event: StreamEvent, message: str) -> StreamEvent:
        return ErrorEvent(
            run_id=event.run_id,
            seq=0,
            tick=event.tick,
            t_s=event.t_s,
            payload=ErrorPayload(message=message),
        )

    def feed(self, event: StreamEvent) -> list[StreamEvent]:
        """The event (re-stamped) followed by a ``ledger`` event when a count changed.

        After a ``tick`` the ledger event waits for that tick's overlays
        (``TICK_OVERLAYS``), which stay right behind their ``tick`` event."""
        out: list[StreamEvent] = []
        if self._pending is not None and event.type not in TICK_OVERLAYS:
            out.append(self._stamp(self._pending))
            self._pending = None
        if isinstance(event, RunFinished) and self.expected_track_hash is None:
            # Streaming without a known hash: check it now; a mismatch never stands.
            self.expected_track_hash = event.payload.track_hash
            was = self._blunt_ok
            self._blunt_ok = None
            if was and not self._blunt_available():
                out.append(self._stamp(self._error_event(event, UNAVAILABLE_MESSAGE)))
                ledger = self._ledger_event(event)
                if ledger is not None:
                    out.append(self._stamp(ledger))
        out.append(self._stamp(event))
        if event.type == "run_started":
            if not self._blunt_available():
                out.append(self._stamp(self._error_event(event, UNAVAILABLE_MESSAGE)))
        elif event.type in ("tick", "transition"):
            self.ledger.observe(event)
            ledger = self._ledger_event(event)
            if ledger is not None and event.type == "tick":
                self._pending = ledger
            elif ledger is not None:
                out.append(self._stamp(ledger))
        return out

    def flush(self) -> list[StreamEvent]:
        """A ledger event still waiting behind tick overlays (a stream cut short)."""
        if self._pending is None:
            return []
        pending, self._pending = self._pending, None
        return [self._stamp(pending)]

    def annotate(self, events: Iterable[StreamEvent]) -> list[StreamEvent]:
        """The whole stream with ledger events; the run's own ``track_hash`` is checked
        before the first event, so the counter never shows a number it cannot back."""
        evs = list(events)
        if self.expected_track_hash is None:
            for e in reversed(evs):
                if isinstance(e, RunFinished):
                    self.expected_track_hash = e.payload.track_hash
                    break
        out: list[StreamEvent] = []
        for e in evs:
            out.extend(self.feed(e))
        out.extend(self.flush())
        return out


def has_ledger(events: Sequence[StreamEvent]) -> bool:
    return any(e.type == "ledger" for e in events)


def with_ledger(events: Sequence[StreamEvent], blunt: BluntCounter) -> list[StreamEvent]:
    """``events`` annotated with ledger events (unchanged when they already carry them)."""
    if has_ledger(events):
        return list(events)
    return LedgerAnnotator(blunt).annotate(events)


__all__ = [
    "TICK_OVERLAYS",
    "UNAVAILABLE_MESSAGE",
    "BluntCounter",
    "LedgerAnnotator",
    "RungLedger",
    "has_ledger",
    "with_ledger",
]
