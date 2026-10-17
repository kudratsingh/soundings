"""The run engine: start a catalog scenario at a seed and stream its typed events (WO-060).

``BatchReplaySource`` runs ``ScenarioRunner.run()`` (runner v1, WO-022) to completion and
turns the result into the WO-060 event stream (``batch_events``). The engine then releases
those events paced by a ``Clock``: an event is visible once ``clock.now_s() * speed`` reaches
its sim ``t_s``; ``speed == 0`` means unpaced. The clock only paces delivery. Every decision
was made by the runner on its own ``TickClock`` before the first event is sent, so the
stream is deterministic and identical at every speed (CLAUDE.md 2.4).

Nothing here can set a rung, a state, a threshold or a scope (CLAUDE.md 2.2): the engine
only replays what the runner decided. Part B registers a ``stepped`` source over runner v3
with ``register_source``; ``approve`` delegates to the run's source, and the batch source
never has a pending request.
"""

from __future__ import annotations

import asyncio
import bisect
import builtins
import itertools
from collections import defaultdict
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass, field
from typing import Protocol

from soundings.canonical import round6
from soundings.clock import Clock, WallClock
from soundings.ladder.table import rung_after
from soundings.runner import LostVehicleNotice, RunResult, ScenarioRunner, Transition
from soundings.schemas.enums import LadderState, Rung, ToggleName
from soundings.schemas.evidence import EvidenceRow
from soundings.schemas.scenario import Scenario
from soundings.sim.hashing import track_hash
from soundings.sim.scenario import resolve_scenario
from soundings.stream.config import ui_config

# --- WO-089 begin: live sensor range rings ---
from soundings.stream.events import (
    ApprovalResultEvent,
    EvidenceRowEvent,
    EvidenceRowPayload,
    ResponderEvent,  # WO-088
    RunFinished,
    RunFinishedPayload,
    RungEvent,
    RungPayload,
    RunStarted,
    RunStartedPayload,
    SensorRangeRow,
    SensorRangesEvent,
    SensorRangesPayload,
    SitrepEvent,
    SitrepPayload,
    StreamEvent,
    TickContact,
    TickEvent,
    TickPayload,
    TransitionEvent,
    TransitionPayload,
)

# --- WO-088 begin ---
from soundings.stream.responder import (
    ResponderSnapshotLike,
    responder_payloads,
    responder_snapshots,
)

# --- WO-088 end ---


def sensor_range_events_at(sim: object, n_ticks: int) -> dict[int, SensorRangesPayload]:
    """The ``sensor_ranges`` payload of every tick: each node's passive-sonar p = 0.5 range
    at its current noise (base + sea state + the run's AIS vessel fixes, WO-080, when
    present), sorted by node id. Computed when a ring changes and repeated in between."""
    from soundings.physics_view import sensor_range_series

    ais = getattr(sim, "ais_tracks", None) or {}
    tick_s = float(getattr(sim, "tick_s", 1.0))
    series = sensor_range_series(ais, n_ticks, tick_s)
    out: dict[int, SensorRangesPayload] = {}
    current: SensorRangesPayload | None = None
    for tick in range(max(1, n_ticks)):
        rings = series.get(tick)
        if rings is not None:
            current = SensorRangesPayload(
                nodes=[
                    SensorRangeRow(
                        node_id=r.node_id, effective_range_m=r.effective_range_m, nl_db=r.nl_db
                    )
                    for r in rings
                ]
            )
        if current is not None:
            out[tick] = current
    return out


# --- WO-089 end ---

MAX_POLL_S = 0.25
"""Longest a subscriber sleeps before re-checking the pacing clock."""


class UnknownRun(LookupError):
    """No run with that id is held (never started, or evicted)."""


class NoPendingApproval(LookupError):
    """The run's source has no approval request with that id (HTTP 409)."""


class UnsupportedOption(ValueError):
    """A start option the chosen source cannot honour (HTTP 400)."""


# --- the batch event builder --------------------------------------------------------------


def batch_events(
    result: RunResult,
    scenario: Scenario,
    *,
    run_id: str,
    speed: float,
    toggles: Sequence[ToggleName] = (),
    source: str = "batch",
    # --- WO-088 begin ---
    responder: Sequence[ResponderSnapshotLike] | None = None,
    # --- WO-088 end ---
) -> list[StreamEvent]:
    """The WO-060 event stream of one finished runner v1 result, in delivery order.

    Per tick: ``tick`` (observed readings), then the evidence rows with that ``t`` in table
    order, then the transitions in ``seq`` order, each followed by a ``rung`` event when the
    contact's rung changed and by a ``sitrep`` event for a LOST_VEHICLE notice. A row a
    transition cites is always sent before the transition (CLAUDE.md 2.6).

    WO-088: when ``responder`` snapshots are given, a ``responder`` event follows each
    ``tick`` event that has one (state, position, battery, standoff, last 60 trail points).
    """
    sim = result.sim
    tick_s = float(sim.tick_s)
    events: list[StreamEvent] = []
    seq = itertools.count()

    def stamp(tick: int) -> dict[str, object]:
        return {"run_id": run_id, "seq": next(seq), "tick": tick, "t_s": round6(tick * tick_s)}

    readings: dict[int, list[TickContact]] = defaultdict(list)
    for cid in sorted(sim.tracks):
        for p in sim.tracks[cid].points:
            readings[p.tick].append(
                TickContact(
                    contact_id=cid,
                    lat=round6(p.lat),
                    lon=round6(p.lon),
                    depth_m=round6(p.depth_m),
                    confidence=round6(p.confidence),
                    sensor_id=p.sensor_id,
                )
            )
    rows: dict[str, EvidenceRow] = {}
    rows_at: dict[int, list[EvidenceRow]] = defaultdict(list)
    for row in result.evidence:
        rows[row.row_id] = row
        rows_at[row.t].append(row)
    transitions_at: dict[int, list[Transition]] = defaultdict(list)
    for t in sorted(result.transitions, key=lambda t: t.seq):
        transitions_at[t.tick].append(t)
    notices_of: dict[str, list[LostVehicleNotice]] = defaultdict(list)
    for n in result.notices:
        notices_of[n.case_id].append(n)

    candidates = [sim.n_ticks - 1, *transitions_at, *rows_at, *(n.tick for n in result.notices)]
    last_tick = max(0, *candidates)
    # --- WO-089 begin ---
    ranges_at = sensor_range_events_at(sim, last_tick + 1)
    # --- WO-089 end ---

    events.append(
        RunStarted.model_validate(
            {
                **stamp(0),
                "payload": RunStartedPayload(
                    scenario_id=scenario.id,
                    scenario_name=scenario.name,
                    seed=result.seed,
                    tick_s=tick_s,
                    n_ticks=sim.n_ticks,
                    speed=round6(speed),
                    toggles=list(toggles),
                    source="stepped" if source == "stepped" else "batch",
                    runner="v1",
                ),
            }
        )
    )
    sent: set[str] = set()
    rung_of: dict[str, Rung] = {}
    state_of: dict[str, LadderState] = {}
    sitrep_version: dict[str, int] = defaultdict(int)

    def send_row(row: EvidenceRow, tick: int) -> None:
        rid = row.row_id
        if rid in sent:
            return
        sent.add(rid)
        events.append(
            EvidenceRowEvent.model_validate(
                {**stamp(tick), "payload": EvidenceRowPayload(row_id=rid, row=row)}
            )
        )

    # --- WO-088 begin ---
    responder_at = responder_payloads(responder) if responder else {}
    # --- WO-088 end ---
    for tick in range(last_tick + 1):
        events.append(
            TickEvent.model_validate(
                {**stamp(tick), "payload": TickPayload(contacts=readings.get(tick, []))}
            )
        )
        # --- WO-089 begin ---
        if tick in ranges_at:
            events.append(
                SensorRangesEvent.model_validate({**stamp(tick), "payload": ranges_at[tick]})
            )
        # --- WO-089 end ---
        # --- WO-088 begin ---
        if tick in responder_at:
            events.append(
                ResponderEvent.model_validate({**stamp(tick), "payload": responder_at[tick]})
            )
        # --- WO-088 end ---
        for row in rows_at.get(tick, []):
            send_row(row, tick)
        for t in transitions_at.get(tick, []):
            for rid in t.evidence_row_ids:
                send_row(rows[rid], tick)
            events.append(
                TransitionEvent.model_validate(
                    {
                        **stamp(tick),
                        "payload": TransitionPayload(
                            contact_id=t.contact_id,
                            case_id=t.case_id,
                            from_state=t.from_state,
                            event=t.event.value,
                            to_state=t.to_state,
                            reason_code=t.reason_code,
                            evidence_row_ids=list(t.evidence_row_ids),
                        ),
                    }
                )
            )
            state_of[t.contact_id] = t.to_state
            previous = rung_of.get(t.contact_id, Rung.WATCH)
            rung = rung_after(previous, t.to_state)
            if t.contact_id not in rung_of or rung is not previous:
                rung_of[t.contact_id] = rung
                events.append(
                    RungEvent.model_validate(
                        {
                            **stamp(tick),
                            "payload": RungPayload(
                                contact_id=t.contact_id, rung=rung, state=t.to_state
                            ),
                        }
                    )
                )
            if t.to_state is LadderState.LOST_VEHICLE:
                for notice in notices_of.pop(t.case_id, []):
                    sitrep_version[t.contact_id] += 1
                    events.append(
                        SitrepEvent.model_validate(
                            {
                                **stamp(tick),
                                "payload": SitrepPayload(
                                    contact_id=t.contact_id,
                                    version=sitrep_version[t.contact_id],
                                    writer="template",
                                    kind="lost_vehicle",
                                    notice=notice,
                                ),
                            }
                        )
                    )
    n_events = len(events) + 1
    events.append(
        RunFinished.model_validate(
            {
                **stamp(last_tick),
                "payload": RunFinishedPayload(
                    terminal=dict(sorted(state_of.items())),
                    track_hash=track_hash(sim),
                    transition_log_hash=result.transition_log_hash,
                    n_events=n_events,
                ),
            }
        )
    )
    return events


# --- sources ------------------------------------------------------------------------------


@dataclass(frozen=True)
class SourceRequest:
    """What a source factory gets: the scenario, the seed and the start options."""

    scenario: Scenario
    seed: int
    run_id: str
    speed: float
    toggles: tuple[ToggleName, ...] = ()
    use_llm: bool = False


class RunSource(Protocol):
    """One run's event producer. ``events()`` is the whole stream in delivery order."""

    name: str

    def events(self) -> list[StreamEvent]: ...

    def approve(self, request_id: str, approver: str) -> ApprovalResultEvent: ...


SourceFactory = Callable[[SourceRequest], RunSource]


class BatchReplaySource:
    """Runner v1 run to completion, streamed from its logs (no pending approvals, ever)."""

    name = "batch"

    def __init__(self, req: SourceRequest) -> None:
        if req.toggles:
            raise UnsupportedOption("the batch source on runner v1 takes no toggles")
        if req.use_llm:
            raise UnsupportedOption("use_llm needs runner v3 (WO-060 part B); v1 has no roles")
        self.req = req
        self._events: list[StreamEvent] | None = None

    def events(self) -> list[StreamEvent]:
        if self._events is None:
            result = ScenarioRunner(self.req.scenario, seed=self.req.seed).run()
            self._events = batch_events(
                result,
                self.req.scenario,
                run_id=self.req.run_id,
                speed=self.req.speed,
                toggles=self.req.toggles,
                # --- WO-088 begin ---
                responder=responder_snapshots(result, self.req.scenario),
                # --- WO-088 end ---
            )
        return self._events

    def approve(self, request_id: str, approver: str) -> ApprovalResultEvent:
        raise NoPendingApproval(f"the batch source has no pending request {request_id!r}")


_SOURCES: dict[str, SourceFactory] = {"batch": BatchReplaySource}


def register_source(name: str, factory: SourceFactory) -> None:
    """Register a run source (WO-061 registers ``stepped``)."""
    _SOURCES[name] = factory


def source_names() -> list[str]:
    return sorted(_SOURCES)


# --- runs and the engine ------------------------------------------------------------------


@dataclass
class RunHandle:
    """One held run: its events and the pacing clock that releases them."""

    run_id: str
    scenario_id: str
    seed: int
    speed: float
    source: RunSource
    events: list[StreamEvent]
    clock: Clock
    order: int
    _due: list[float] = field(default_factory=list, repr=False)

    def __post_init__(self) -> None:
        self._due = [e.t_s for e in self.events]

    def available(self) -> int:
        """How many events (a prefix) the pacing clock has released so far."""
        if self.speed <= 0:
            return len(self.events)
        return bisect.bisect_right(self._due, self.clock.now_s() * self.speed)

    @property
    def finished(self) -> bool:
        return self.available() >= len(self.events)

    def wait_s(self, index: int) -> float:
        """Wall seconds until event ``index`` is released (0 when it already is)."""
        if self.speed <= 0 or index >= len(self.events):
            return 0.0
        return max(0.0, self._due[index] / self.speed - self.clock.now_s())

    def summary(self) -> dict[str, object]:
        return {
            "run_id": self.run_id,
            "scenario": self.scenario_id,
            "seed": self.seed,
            "speed": self.speed,
            "source": self.source.name,
            "n_events": len(self.events),
            "finished": self.finished,
        }


class RunEngine:
    """An in-process registry of at most ``max_runs`` runs; the oldest finished goes first."""

    def __init__(
        self,
        *,
        max_runs: int | None = None,
        clock_factory: Callable[[], Clock] = WallClock,
        default_speed: float | None = None,
    ) -> None:
        cfg = ui_config()
        self.max_runs = cfg.max_live_runs if max_runs is None else max_runs
        self.default_speed = cfg.default_speed if default_speed is None else default_speed
        self.clock_factory = clock_factory
        self._runs: dict[str, RunHandle] = {}
        self._counter = itertools.count(1)

    def start(
        self,
        scenario: Scenario | str,
        seed: int | None = None,
        *,
        speed: float | None = None,
        toggles: Sequence[ToggleName] = (),
        use_llm: bool = False,
        source: str = "batch",
    ) -> RunHandle:
        sc = resolve_scenario(scenario) if isinstance(scenario, str) else scenario
        factory = _SOURCES.get(source)
        if factory is None:
            raise UnsupportedOption(f"unknown source {source!r}; known: {source_names()}")
        pace = self.default_speed if speed is None else float(speed)
        if pace < 0:
            raise UnsupportedOption("speed must be >= 0")
        n = next(self._counter)
        run_seed = sc.seed if seed is None else int(seed)
        run_id = f"run-{n:04d}-{sc.id}-s{run_seed}"
        src = factory(
            SourceRequest(
                scenario=sc,
                seed=run_seed,
                run_id=run_id,
                speed=pace,
                toggles=tuple(toggles),
                use_llm=use_llm,
            )
        )
        events = src.events()
        self._evict()
        handle = RunHandle(
            run_id=run_id,
            scenario_id=sc.id,
            seed=run_seed,
            speed=pace,
            source=src,
            events=events,
            clock=self.clock_factory(),
            order=n,
        )
        self._runs[run_id] = handle
        return handle

    def _evict(self) -> None:
        while len(self._runs) >= self.max_runs:
            held = sorted(self._runs.values(), key=lambda h: h.order)
            done = [h for h in held if h.finished]
            victim = (done or held)[0]
            del self._runs[victim.run_id]

    def get(self, run_id: str) -> RunHandle:
        try:
            return self._runs[run_id]
        except KeyError:
            raise UnknownRun(run_id) from None

    def list(self) -> builtins.list[RunHandle]:
        """Held runs, newest first."""
        return sorted(self._runs.values(), key=lambda h: h.order, reverse=True)

    def approve(self, run_id: str, request_id: str, approver: str) -> ApprovalResultEvent:
        """Delegate to the run's source; the ladder's guard still decides (WO-061)."""
        return self.get(run_id).source.approve(request_id, approver)

    def run_to_end(self, run_id: str, approve_first: bool = False) -> builtins.list[StreamEvent]:
        """Every event of the run, unpaced. ``approve_first`` matters only to ``stepped``."""
        return list(self.get(run_id).events)

    async def subscribe(self, run_id: str, after: int = -1) -> AsyncIterator[StreamEvent]:
        """Events with ``seq > after``: the released backlog first, then live as paced."""
        handle = self.get(run_id)
        nxt = max(after + 1, 0)
        total = len(handle.events)
        while nxt < total:
            ready = handle.available()
            while nxt < ready:
                yield handle.events[nxt]
                nxt += 1
            if nxt >= total:
                break
            await asyncio.sleep(min(handle.wait_s(nxt), MAX_POLL_S))


_ENGINE: RunEngine | None = None


def get_engine() -> RunEngine:
    """The app-scoped engine singleton."""
    global _ENGINE
    if _ENGINE is None:
        _ENGINE = RunEngine()
    return _ENGINE


def stream_events(
    scenario: Scenario | str, seed: int | None = None, *, run_id: str = "cli"
) -> list[StreamEvent]:
    """The unpaced event stream of one batch run (the CLI and the determinism diff)."""
    sc = resolve_scenario(scenario) if isinstance(scenario, str) else scenario
    req = SourceRequest(scenario=sc, seed=sc.seed if seed is None else seed, run_id=run_id, speed=0)
    return BatchReplaySource(req).events()


__all__ = [
    "BatchReplaySource",
    "NoPendingApproval",
    "RunEngine",
    "RunHandle",
    "RunSource",
    "SourceRequest",
    "UnknownRun",
    "UnsupportedOption",
    "batch_events",
    "get_engine",
    "register_source",
    "sensor_range_events_at",  # WO-089
    "source_names",
    "stream_events",
]
