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
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
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

# --- WO-086 begin ---
from soundings.stream.ledger import BluntCounter, LedgerAnnotator, with_ledger
from soundings.stream.projection import projection_series

# --- WO-086 end ---
# --- WO-088 begin ---
from soundings.stream.responder import (
    ResponderSnapshotLike,
    real_responder_trail,  # WO-088D
    responder_payloads,
    responder_snapshots,
)

# --- WO-088 end ---


# --- WO-086 begin: projection per reading, rung ledger and blunt counter ---
def ledger_stream(events: Sequence[StreamEvent], blunt: BluntCounter) -> builtins.list[StreamEvent]:
    """The stream with WO-086 ``ledger`` events, the blunt worker joined first."""
    return with_ledger(events, blunt)


# --- WO-086 end ---


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
        # --- WO-086 begin ---
        projections = projection_series(sim.tracks[cid])
        # --- WO-086 end ---
        for p, projection in zip(sim.tracks[cid].points, projections, strict=True):
            readings[p.tick].append(
                TickContact(
                    contact_id=cid,
                    lat=round6(p.lat),
                    lon=round6(p.lon),
                    depth_m=round6(p.depth_m),
                    confidence=round6(p.confidence),
                    sensor_id=p.sensor_id,
                    projection=projection,  # WO-086
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
    # --- WO-088D begin ---
    responder: bool = False
    """True: the run has the harbor's real responder (``config/responder.yaml``); its
    trail comes from runner v2 with the responder when the run itself carries none."""
    # --- WO-088D end ---


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
        self.triage_log_hash = ""  # WO-085: non-empty for a surge run

    def events(self) -> list[StreamEvent]:
        if self._events is None:
            # --- WO-085: surge triage --- (a scenario carrying contact_surge runs through
            # runner v2 with the triage queue; every other run stays runner v1, unchanged)
            result = surge_runner(self.req.scenario, self.req.seed).run()
            self.triage_log_hash = result.triage_log_hash
            # --- WO-085 end ---
            self._events = batch_events(
                result,
                self.req.scenario,
                run_id=self.req.run_id,
                speed=self.req.speed,
                toggles=self.req.toggles,
                # --- WO-088 begin ---
                responder=responder_snapshots(result, self.req.scenario)
                or (  # WO-088D: the real responder when the request asks for one
                    real_responder_trail(self.req.scenario, self.req.seed)
                    if self.req.responder
                    else None
                ),
                # --- WO-088 end ---
            )
        return self._events

    def approve(self, request_id: str, approver: str) -> ApprovalResultEvent:
        raise NoPendingApproval(f"the batch source has no pending request {request_id!r}")


# --- WO-085: surge triage ---
def has_surge(scenario: Scenario, toggles: Sequence[ToggleName | str] = ()) -> bool:
    """True when the scenario carries a ``contact_surge`` toggle or the request names it."""
    names = {ToggleName(t.name) for t in scenario.toggles} | {ToggleName(t) for t in toggles}
    return ToggleName.CONTACT_SURGE in names


def surge_runner(
    scenario: Scenario, seed: int, toggles: Sequence[ToggleName | str] = ()
) -> ScenarioRunner:
    """``ScenarioRunnerV2(..., triage=load_triage_config())`` for a surge run (WO-085),
    else the unchanged runner v1."""
    if has_surge(scenario, toggles):
        from soundings.runner import ScenarioRunnerV2
        from soundings.triage import load_triage_config

        return ScenarioRunnerV2(scenario, seed=seed, triage=load_triage_config())
    return ScenarioRunner(scenario, seed=seed)


# --- WO-085 end ---

_SOURCES: dict[str, SourceFactory] = {"batch": BatchReplaySource}


# --- WO-060B begin: lazy sources (runner v3) ---
class LazySource(Protocol):
    """A source that produces its stream as the pacing clock asks for it (runner v3).

    ``buffer`` is the stream so far (the handle shares the list), ``pull(until_t_s)`` steps
    every tick up to that sim time, ``exhausted`` is true once ``run_finished`` is in."""

    name: str
    lazy: bool
    buffer: list[StreamEvent]

    @property
    def exhausted(self) -> bool: ...

    def pull(self, until_t_s: float) -> int: ...

    def events(self) -> list[StreamEvent]: ...

    @property
    def track_hash(self) -> str: ...

    def annotate(self, annotator: LedgerAnnotator) -> None: ...

    def approve(self, request_id: str, approver: str) -> ApprovalResultEvent: ...


def _is_lazy(src: object) -> bool:
    return bool(getattr(src, "lazy", False)) and hasattr(src, "pull")


def _register_v3() -> None:
    from soundings.stream.v3 import v3_manual_source, v3_source

    _SOURCES.setdefault("v3", v3_source)
    _SOURCES.setdefault("v3-manual", v3_manual_source)


# --- WO-060B end ---


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
    # --- WO-060B begin: piecewise pacing (1x while awaiting approval) and lazy sources ---
    awaiting_speed: float = 0.0
    """Sim seconds per wall second while any contact is AWAITING_APPROVAL (0: no change)."""
    _awaiting: set[str] = field(default_factory=set, repr=False)
    _last_t: float = field(default=0.0, repr=False)
    _anchor: tuple[float, float, float] = field(default=(0.0, 0.0, 1.0), repr=False)

    def __post_init__(self) -> None:
        self._due = []
        self._extend_due()

    def _pace(self) -> float:
        if self._awaiting and self.awaiting_speed > 0:
            return min(self.awaiting_speed, self.speed)
        return self.speed

    def _extend_due(self) -> None:
        """Wall-second release time of every new event: sim time divided by the pace in
        force when the previous event was released (1x while a contact awaits approval,
        so the approver gets the real timeout; the ladder itself runs on sim ticks)."""
        if self.speed <= 0:
            return
        for e in self.events[len(self._due) :]:
            if not self._due:
                self._anchor = (0.0, 0.0, self.speed)
            wall0, sim0, pace = self._anchor
            due = wall0 + max(0.0, e.t_s - sim0) / pace
            self._due.append(due)
            self._last_t = e.t_s
            if e.type == "transition":
                p = e.payload
                if p.to_state is LadderState.AWAITING_APPROVAL:
                    self._awaiting.add(p.contact_id)
                else:
                    self._awaiting.discard(p.contact_id)
                if self._pace() != pace:  # the pace changes from this event on
                    self._anchor = (due, e.t_s, self._pace())

    @property
    def lazy(self) -> bool:
        return _is_lazy(self.source)

    def _fill(self, now: float) -> None:
        """Let a lazy source step up to the sim time the pacing clock has reached."""
        src = self.source
        if not _is_lazy(src):
            return
        lazy: LazySource = src  # type: ignore[assignment]
        while not lazy.exhausted:
            if self.speed <= 0:
                lazy.pull(float("inf"))
            else:
                wall0, sim0, pace = self._anchor if self._due else (0.0, 0.0, self.speed)
                target = sim0 + max(0.0, now - wall0) * pace
                if lazy.pull(target) == 0:
                    if lazy.exhausted:
                        break
                    # Nothing new up to the target: release the next tick when it is due.
                    break
            self._extend_due()
            if self._due and self._due[-1] > now:
                break

    @property
    def exhausted(self) -> bool:
        src = self.source
        return not _is_lazy(src) or bool(getattr(src, "exhausted", True))

    def available(self) -> int:
        """How many events (a prefix) the pacing clock has released so far."""
        if self.speed <= 0:
            self._fill(float("inf"))
            return len(self.events)
        now = self.clock.now_s()
        self._fill(now)
        return bisect.bisect_right(self._due, now)

    @property
    def finished(self) -> bool:
        return self.available() >= len(self.events) and self.exhausted

    def wait_s(self, index: int) -> float:
        """Wall seconds until event ``index`` is released (0 when it already is)."""
        if self.speed <= 0:
            return 0.0
        if index >= len(self._due):
            return MAX_POLL_S if not self.exhausted else 0.0
        return max(0.0, self._due[index] - self.clock.now_s())

    # --- WO-060B end ---

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
        responder: bool = False,  # WO-088D: the API (UI runs) passes True
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
        # --- WO-086 begin: the blunt counter runs while the source builds its stream ---
        blunt = BluntCounter(sc, run_seed).start()
        # --- WO-086 end ---
        src = factory(
            SourceRequest(
                scenario=sc,
                seed=run_seed,
                run_id=run_id,
                speed=pace,
                toggles=tuple(toggles),
                use_llm=use_llm,
                responder=responder,  # WO-088D
            )
        )
        # --- WO-060B begin: a lazy source shares its growing buffer with the handle, and
        # the WO-086 ledger annotates it event by event as it is stepped ---
        if _is_lazy(src):
            lazy: LazySource = src  # type: ignore[assignment]
            lazy.annotate(LedgerAnnotator(blunt, expected_track_hash=lazy.track_hash))
            events = lazy.buffer
        else:
            events = src.events()
            events = ledger_stream(events, blunt)  # WO-086
        # --- WO-060B end ---
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
            awaiting_speed=ui_config().awaiting_approval_speed,  # WO-060B
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
        # --- WO-060B begin ---
        handle = self.get(run_id)
        if _is_lazy(handle.source):
            handle.source.events()
            handle._extend_due()
        # --- WO-060B end ---
        return list(handle.events)

    # --- WO-060B begin: the approval hook for WO-061 ---
    def deliver_approval(self, run_id: str, token: Mapping[str, object]) -> str:
        """Hand a person's WO-041 approval token to the run's ladder (WO-061's button).

        Only a ``v3-manual`` run waits on a person; the ladder's guard checks the token
        against the requested scope on the next tick, so a token for another scope changes
        nothing. Returns the contact id. ``NoPendingApproval`` when the run has no open
        request for the token's contact or its source takes no tokens; ``ValueError``
        (``soundings.stream.v3.TokenRefused``) when the token does not verify for it."""
        src = self.get(run_id).source
        deliver = getattr(src, "deliver_approval", None)
        if deliver is None:
            raise NoPendingApproval(f"run {run_id!r} ({src.name}) takes no approval tokens")
        try:
            return str(deliver(token))
        except LookupError as exc:
            raise NoPendingApproval(str(exc)) from exc

    # --- WO-060B end ---

    async def subscribe(self, run_id: str, after: int = -1) -> AsyncIterator[StreamEvent]:
        """Events with ``seq > after``: the released backlog first, then live as paced."""
        handle = self.get(run_id)
        nxt = max(after + 1, 0)
        # --- WO-060B begin: a lazy source's stream grows while it is watched ---
        while True:
            ready = handle.available()
            while nxt < ready:
                yield handle.events[nxt]
                nxt += 1
            if nxt >= len(handle.events) and handle.exhausted:
                break
            await asyncio.sleep(min(handle.wait_s(nxt), MAX_POLL_S))
        # --- WO-060B end ---


_ENGINE: RunEngine | None = None


def get_engine() -> RunEngine:
    """The app-scoped engine singleton."""
    global _ENGINE
    if _ENGINE is None:
        _ENGINE = RunEngine()
    return _ENGINE


def stream_events(
    scenario: Scenario | str,
    seed: int | None = None,
    *,
    run_id: str = "cli",
    responder: bool = False,  # WO-088D
) -> list[StreamEvent]:
    """The unpaced event stream of one batch run (the CLI and the determinism diff)."""
    sc = resolve_scenario(scenario) if isinstance(scenario, str) else scenario
    req = SourceRequest(
        scenario=sc,
        seed=sc.seed if seed is None else seed,
        run_id=run_id,
        speed=0,
        responder=responder,  # WO-088D
    )
    # --- WO-086 begin ---
    blunt = BluntCounter(sc, req.seed).start()
    return ledger_stream(BatchReplaySource(req).events(), blunt)
    # --- WO-086 end ---


# --- WO-060B begin: the unpaced runner v3 stream ---
def v3_stream_events(
    scenario: Scenario | str,
    seed: int | None = None,
    *,
    run_id: str = "cli",
    toggles: Sequence[ToggleName] = (),
    use_llm: bool = False,
) -> list[StreamEvent]:
    """The unpaced event stream of one runner v3 run (CLI ``--source v3``, determinism)."""
    from soundings.stream.v3 import V3Source

    sc = resolve_scenario(scenario) if isinstance(scenario, str) else scenario
    req = SourceRequest(
        scenario=sc,
        seed=sc.seed if seed is None else seed,
        run_id=run_id,
        speed=0,
        toggles=tuple(toggles),
        use_llm=use_llm,
    )
    src = V3Source(req)
    src.annotate(
        LedgerAnnotator(BluntCounter(sc, req.seed).start(), expected_track_hash=src.track_hash)
    )
    return src.events()


# --- WO-060B end ---


__all__ = [
    "BatchReplaySource",
    "LazySource",  # WO-060B
    "NoPendingApproval",
    "RunEngine",
    "RunHandle",
    "RunSource",
    "SourceRequest",
    "UnknownRun",
    "UnsupportedOption",
    "batch_events",
    "get_engine",
    "ledger_stream",  # WO-086
    "register_source",
    "sensor_range_events_at",  # WO-089
    "source_names",
    "stream_events",
    "v3_stream_events",  # WO-060B
]

# --- WO-060B begin: register the runner v3 sources ---
_register_v3()
# --- WO-060B end ---
