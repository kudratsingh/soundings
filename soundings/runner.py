"""Scenario runner (shared file). WO-022 owns runner v1; later WOs edit only their block.

Runner v1 is the ask rung end to end: sim -> evidence table -> watch probes ->
``registry_lookup`` + ``acoustic_hail`` -> matcher. Every case ends at CLEARED_AT_ASK,
at LOST_VEHICLE with a ``LostVehicleNotice`` for the plan's operator, or at
INVESTIGATING with ``investigation_pending=True`` for runner v2 (WO-033).

Rules the runner keeps (CLAUDE.md section 2):

* 2.2: every state change goes through ``check_transition`` against ``ASK_TRANSITIONS``
  (the five SPEC-LADDER section 3 rows from DETECTED through ASKING); anything else
  raises ``IllegalTransition`` and the case is written as FAILED. Nothing here imports
  ``soundings.llm``.
* 2.4: time is the tick (``TickClock``); every collection is iterated in sorted order;
  the transition log is canonical JSONL and ``transition_log_hash`` is its sha256.
* 2.6: every transition, outcome and notice line cites evidence row ids in
  ``RunResult.evidence``.
* The runner reads only observed data (``SimRun.tracks``, ``tick_s``, ``n_ticks``) and
  never a track point after the current tick. Ground truth is read only inside the
  simulator (which answers the hail and eyes-on probes through ``soundings.ports``), by
  ``soundings.eval`` and by tests (``tests/test_boundary.py``).

Interruptions follow the one definition of DECISIONS.md 2026-10-16 23:05: an event that
needs the watch officer or backup approver to look or act. Runner v1 raises none; a
LOST_VEHICLE notice goes to the plan's ``contact_channel`` and is not an interruption.
"""

from __future__ import annotations

import argparse
import bisect
import hashlib
import math
import sys
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from functools import partial
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, StrictBool, StrictStr

from soundings.canonical import canonical_json, round6, stable_hash
from soundings.challenge.hail import (
    HailConfig,
    HailResult,
    HailSimulator,
    KnownOperatorTable,
    hail_evidence_rows,
)
from soundings.clock import TickClock
from soundings.investigation.evidence import EvidenceTable, carry_over_reacquired
from soundings.investigation.features import (
    current_at_from_field,
    current_field_row,
    extract_acoustic,
    extract_context,
    extract_kinematics,
    hour_at,
    last_fitted_state,
    track_arrays,
    track_segments,
)
from soundings.matcher import (
    EvidenceView,
    LostTrack,
    MatchCode,
    MatchConfig,
    MatchOutcome,
    PositionFix,
    ReacquireMatch,
    RegistryLookup,
    SearchBox,
    drift_search_box,
    evidence_view,
    load_match_config,
    match,
    match_reacquisition,
    registry_lookup,
    registry_lookup_rows,
)
from soundings.probe_keys import idempotency_key, tick_bucket
from soundings.registry.models import PlanView
from soundings.registry.store import RegistryStore, plan_views_from_scenario
from soundings.schemas._base import NonNegFloat, NonNegInt, StrictModel
from soundings.schemas.denylist import FreeText
from soundings.schemas.enums import HailReply, LadderState, ProbeId, RegistryMatch
from soundings.schemas.evidence import EvidenceRow, RowId
from soundings.schemas.geo import distance_m, from_local_m
from soundings.schemas.harbor import Harbor, load_harbor
from soundings.schemas.scenario import Scenario
from soundings.schemas.track import Track
from soundings.sim.current import CurrentField, load_current_field
from soundings.sim.detection import load_detection_params
from soundings.sim.run import run_scenario
from soundings.sim.scenario import resolve_scenario
from soundings.sim.types import SimRun

# --- WO-022 begin: runner v1 ---

S = LadderState
PLAN_HORIZON_TICK = 10**9
"""``overlapping_ticks(0, PLAN_HORIZON_TICK)`` reads every tick-windowed plan."""


class RunnerEvent(StrEnum):
    """SPEC-LADDER section 2 events that runner v1 uses."""

    CONTACT_DETECTED = "contact_detected"
    PROBE_RESULT = "probe_result"
    STOP_RULE_FIRED = "stop_rule_fired"


ASK_TRANSITIONS: frozenset[tuple[LadderState, RunnerEvent, LadderState]] = frozenset(
    {
        (S.DETECTED, RunnerEvent.CONTACT_DETECTED, S.WATCHING),
        (S.WATCHING, RunnerEvent.PROBE_RESULT, S.ASKING),
        (S.ASKING, RunnerEvent.STOP_RULE_FIRED, S.CLEARED_AT_ASK),
        (S.ASKING, RunnerEvent.STOP_RULE_FIRED, S.LOST_VEHICLE),
        (S.ASKING, RunnerEvent.PROBE_RESULT, S.INVESTIGATING),
    }
)
"""The five SPEC-LADDER section 3 rows from DETECTED through ASKING; nothing else."""

V1_END_STATES: frozenset[LadderState] = frozenset(
    {S.CLEARED_AT_ASK, S.LOST_VEHICLE, S.INVESTIGATING}
)


class IllegalTransition(RuntimeError):
    """A state change outside ``ASK_TRANSITIONS``."""


def check_transition(from_state: LadderState, event: RunnerEvent, to_state: LadderState) -> None:
    """Raise ``IllegalTransition`` unless ``(from, event, to)`` is in ``ASK_TRANSITIONS``."""
    if (from_state, event, to_state) not in ASK_TRANSITIONS:
        raise IllegalTransition(
            f"{from_state.value} x {event.value} -> {to_state.value} is not an ask-rung row"
        )


class Transition(StrictModel):
    seq: NonNegInt
    case_id: StrictStr
    contact_id: StrictStr
    tick: NonNegInt
    from_state: LadderState
    event: RunnerEvent
    to_state: LadderState
    reason_code: StrictStr
    evidence_row_ids: Annotated[tuple[RowId, ...], Field(min_length=1)]
    idempotency_key: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class CaseResult(StrictModel):
    """One case: a contact's pass through the ask rung. ``outcome`` is the last outcome.

    ``outcomes`` holds every matcher outcome in order (a PENDING_SILENCE before the
    final one); ``hail_keys`` the idempotency keys of the hails the case used.
    """

    case_id: StrictStr
    contact_id: StrictStr
    state: LadderState
    outcome: MatchOutcome | None
    reacquired_from: StrictStr | None
    investigation_pending: StrictBool
    opened_tick: NonNegInt
    closed_tick: NonNegInt | None = None
    outcomes: tuple[MatchOutcome, ...] = ()
    hail_keys: tuple[str, ...] = ()
    opened_by: Literal["new_track", "plan_watch"] = "new_track"
    failure: FreeText | None = None


class HailRecord(StrictModel):
    """One hail the runner used. ``cached`` marks a reuse of an earlier hail's result."""

    purpose: Literal["ask", "plan_watch"]
    case_id: StrictStr | None
    contact_id: StrictStr
    sent_tick: NonNegInt
    reply_tick: NonNegInt
    hail_reply: HailReply
    idempotency_key: StrictStr
    cached: StrictBool
    row_ids: tuple[RowId, ...]


NoticeSection = Literal["Contact", "What we know", "What we assume", "Search box"]


class NoticeLine(StrictModel):
    """One line of a LOST_VEHICLE notice under a SPEC-LADDER section 6 name."""

    section: NoticeSection
    text: FreeText
    evidence_row_ids: Annotated[tuple[RowId, ...], Field(min_length=1)]


class LostVehicleNotice(StrictModel):
    """Sent to the plan's ``contact_channel``; the watch officer is not interrupted."""

    case_id: StrictStr
    contact_id: StrictStr
    tick: NonNegInt
    plan_id: StrictStr
    operator: FreeText
    vehicle: FreeText
    contact_channel: FreeText
    last_known: PositionFix
    silence_s: NonNegFloat
    search_box: SearchBox
    evidence_row_ids: Annotated[tuple[RowId, ...], Field(min_length=1)]
    lines: tuple[NoticeLine, ...]


@dataclass(frozen=True)
class RunResult:
    scenario_id: str
    seed: int
    sim: SimRun
    cases: list[CaseResult]
    transitions: list[Transition]
    evidence: EvidenceTable
    notices: list[LostVehicleNotice]
    interruptions: int
    probes_used: dict[str, int]
    transition_log_hash: str
    hails: tuple[HailRecord, ...] = ()
    reacquisitions: tuple[ReacquireMatch, ...] = ()

    def case(self, case_id: str) -> CaseResult:
        for c in self.cases:
            if c.case_id == case_id:
                return c
        raise KeyError(case_id)


def transition_log_lines(transitions: Iterable[Transition]) -> list[str]:
    """One canonical JSON object per transition, in ``seq`` order."""
    return [canonical_json(t) for t in sorted(transitions, key=lambda t: t.seq)]


def transition_log_text(transitions: Iterable[Transition]) -> str:
    lines = transition_log_lines(transitions)
    return "".join(f"{line}\n" for line in lines)


def transition_log_hash_of(transitions: Iterable[Transition]) -> str:
    """sha256 of the canonical JSONL text of ``transitions``."""
    return hashlib.sha256(transition_log_text(transitions).encode("utf-8")).hexdigest()


def write_transition_log(result: RunResult, path: str | Path) -> Path:
    """Write the canonical JSONL transition log; its sha256 is ``transition_log_hash``."""
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(transition_log_text(result.transitions), encoding="utf-8")
    return out


# --- internal state -----------------------------------------------------------------------


@dataclass
class _Contact:
    cid: str
    track: Track
    ticks: list[int]
    first_tick: int
    last_tick: int
    dropped_at: int
    n_seen: int = 0
    n_extracted: int = 0
    dropped: bool = False
    linked_to: str | None = None  # the successor id after a re-acquisition

    def upto(self) -> Track:
        """The track as seen so far (readings at or before the current tick)."""
        points = self.track.points[: self.n_seen]
        if not points:
            raise ValueError(f"{self.cid} has no reading yet")
        last = points[-1].tick
        gaps = tuple(g for g in self.track.gaps if g.end_tick < last)
        return self.track.model_copy(update={"points": points, "gaps": gaps})


@dataclass
class _Case:
    case_id: str
    contact_id: str
    opened_tick: int
    opened_by: Literal["new_track", "plan_watch"] = "new_track"
    state: LadderState = S.DETECTED
    outcome: MatchOutcome | None = None
    outcomes: list[MatchOutcome] = field(default_factory=list)
    hail_keys: list[str] = field(default_factory=list)
    reacquired_from: str | None = None
    investigation_pending: bool = False
    closed_tick: int | None = None
    failure: str | None = None

    def result(self) -> CaseResult:
        return CaseResult(
            case_id=self.case_id,
            contact_id=self.contact_id,
            state=self.state,
            outcome=self.outcome,
            reacquired_from=self.reacquired_from,
            investigation_pending=self.investigation_pending,
            opened_tick=self.opened_tick,
            closed_tick=self.closed_tick,
            outcomes=tuple(self.outcomes),
            hail_keys=tuple(self.hail_keys),
            opened_by=self.opened_by,
            failure=self.failure,
        )


@dataclass
class _Watch:
    """A cleared filed contact under the registry plan watch."""

    contact_id: str
    plan_id: str
    case_id: str
    next_tick: int


@dataclass(order=True)
class _Event:
    tick: int
    order: int
    key: str
    kind: str = field(compare=False)
    hail: HailResult | None = field(default=None, compare=False)


_ORDER = {"ask_send": 0, "ask_reply": 1, "watch_reply": 2}


class _Plans:
    """Plans as the matcher sees them: a list, or a ``RegistryStore`` read on the hot path."""

    def __init__(self, source: RegistryStore | Sequence[PlanView]) -> None:
        self._store = source if isinstance(source, RegistryStore) else None
        self._list = None if isinstance(source, RegistryStore) else list(source)
        self._cache: tuple[int, list[PlanView]] | None = None

    def at(self, tick: int) -> list[PlanView]:
        if self._list is not None:
            return self._list
        if self._cache is not None and self._cache[0] == tick:
            return self._cache[1]
        assert self._store is not None
        plans = self._store.overlapping_ticks(0, PLAN_HORIZON_TICK)
        self._cache = (tick, plans)
        return plans


# --- the runner ---------------------------------------------------------------------------


class ScenarioRunner:
    """Runs one scenario through the ask rung. ``plans`` defaults to the scenario's plans.

    ``plans`` may be a ``RegistryStore`` (read through ``overlapping_ticks`` at every
    lookup) or a list of ``PlanView``. Later WOs add keyword arguments only.
    """

    def __init__(
        self,
        scenario: Scenario | str,
        *,
        seed: int | None = None,
        plans: RegistryStore | Sequence[PlanView] | None = None,
        harbor: Harbor | None = None,
        current_field: CurrentField | None = None,
        match_cfg: MatchConfig | None = None,
        hail_cfg: HailConfig | None = None,
        known: KnownOperatorTable | None = None,
    ) -> None:
        self.harbor = harbor if harbor is not None else load_harbor()
        self.scenario = (
            resolve_scenario(scenario, self.harbor) if isinstance(scenario, str) else scenario
        )
        self.seed = self.scenario.seed if seed is None else int(seed)
        self.plans_source: RegistryStore | Sequence[PlanView] = (
            plans if plans is not None else plan_views_from_scenario(self.scenario)
        )
        self.current_field = current_field if current_field is not None else load_current_field()
        self.match_cfg = match_cfg if match_cfg is not None else load_match_config()
        self.hail_cfg = hail_cfg
        self.known = known
        self.timeout_ticks = load_detection_params().track_timeout_ticks

    def run(self) -> RunResult:
        """Run the scenario and return the ask-rung result (deterministic)."""
        return _V1Run(self).execute()


class _V1Run:
    """One execution of runner v1. All mutable state lives here."""

    def __init__(self, runner: ScenarioRunner) -> None:
        self.r = runner
        sc = runner.scenario
        self.cfg = runner.match_cfg
        self.harbor = runner.harbor
        self.sim = run_scenario(sc, seed=runner.seed, harbor=runner.harbor)
        self.tick_s = float(self.sim.tick_s)
        self.clock = TickClock(self.tick_s)
        self.hail = HailSimulator(
            sc, self.sim, seed=runner.seed, known=runner.known, cfg=runner.hail_cfg
        )
        self.plans = _Plans(runner.plans_source)
        self.table = EvidenceTable()
        cf = runner.current_field
        self.current_at = current_at_from_field(
            cf.vector_at, cf.phase_for_hour, hour_of_day=sc.hour_of_day
        )
        self.contacts: dict[str, _Contact] = {}
        for cid in sorted(self.sim.tracks):
            track = self.sim.tracks[cid]
            ticks = [p.tick for p in track.points]
            self.contacts[cid] = _Contact(
                cid=cid,
                track=track,
                ticks=ticks,
                first_tick=ticks[0],
                last_tick=ticks[-1],
                dropped_at=ticks[-1] + runner.timeout_ticks + 1,
            )
        self.cases: dict[str, _Case] = {}
        self.case_count: dict[str, int] = {}
        self.active_case: dict[str, str] = {}
        self.transitions: list[Transition] = []
        self.notices: list[LostVehicleNotice] = []
        self.events: list[_Event] = []
        self.watches: dict[str, _Watch] = {}
        self.hail_records: list[HailRecord] = []
        self.hail_keys_sent: set[str] = set()
        self.lookup_keys: set[str] = set()
        self.reacquisitions: list[ReacquireMatch] = []
        self.rehail_ticks = max(
            1, int(math.ceil(round(self.cfg.plan_watch.rehail_interval_s / self.tick_s, 9)))
        )

    # --- evidence -------------------------------------------------------------------------

    def append(self, rows: Iterable[EvidenceRow]) -> list[str]:
        ids: list[str] = []
        for row in rows:
            rid = row.row_id
            if rid not in self.table:
                self.table.append(row)
            ids.append(rid)
        return ids

    def extract(self, c: _Contact) -> None:
        """Extractor rows over the track so far; acoustic rows for the new readings only."""
        if c.n_seen == 0 or c.n_seen == c.n_extracted:
            return
        track = c.upto()
        current_row = current_field_row(track, self.current_at)
        segments = track_segments(track)
        fresh = track.model_copy(update={"points": track.points[c.n_extracted :], "gaps": ()})
        rows = [current_row, *dict.fromkeys(segments.values())]
        rows += extract_kinematics(
            track, self.harbor, self.current_at, segments=segments, current_row=current_row
        )
        rows += extract_acoustic(fresh)
        rows += extract_context(track, self.harbor, hour_of_day=self.r.scenario.hour_of_day)
        self.append(rows)
        c.n_extracted = c.n_seen

    def view(self, c: _Contact, tick: int) -> EvidenceView:
        self.extract(c)
        return evidence_view(self.table, c.cid, c.upto(), tick, self.tick_s)

    def tide_phase(self, tick: int) -> str:
        hour = hour_at(self.r.scenario.hour_of_day, tick * self.tick_s)
        return self.r.current_field.phase_for_hour(hour)

    # --- probes ---------------------------------------------------------------------------

    def lookup(
        self, c: _Contact, view: EvidenceView, tick: int, hail_rows: Sequence[EvidenceRow] = ()
    ) -> tuple[RegistryLookup, list[str], str]:
        modem = view.modem_id if hail_rows else None
        result = registry_lookup(
            self.plans.at(tick),
            self.harbor,
            view.position.lat,
            view.position.lon,
            tick,
            self.cfg,
            modem_id=modem,
        )
        assert view.position.row_id is not None
        rows = registry_lookup_rows(
            result,
            contact_id=c.cid,
            track_segment=self.table.get(view.position.row_id),
            hail_rows=hail_rows,
        )
        ids = self.append(rows)
        args = {} if modem is None else {"modem_id": modem}
        key = idempotency_key(
            c.cid, ProbeId.REGISTRY_LOOKUP.value, args, tick_bucket(tick, self.tick_s)
        )
        self.lookup_keys.add(key)
        return result, ids, key

    def send_hail(
        self, c: _Contact, tick: int, *, previously_replied: bool, case_id: str | None, purpose: str
    ) -> HailResult:
        key = self.hail.key(c.cid, tick)
        cached = key in self.hail_keys_sent
        result = self.hail.send(c.cid, tick, previously_replied=previously_replied)
        self.hail_keys_sent.add(key)
        self._record_hail(result, case_id=case_id, purpose=purpose, cached=cached)
        return result

    def _record_hail(
        self, result: HailResult, *, case_id: str | None, purpose: str, cached: bool
    ) -> None:
        rows = hail_evidence_rows(result, tick_s=self.tick_s)
        self.hail_records.append(
            HailRecord(
                purpose="ask" if purpose == "ask" else "plan_watch",
                case_id=case_id,
                contact_id=result.contact_id,
                sent_tick=result.sent_tick,
                reply_tick=result.reply_tick,
                hail_reply=result.hail_reply,
                idempotency_key=result.idempotency_key,
                cached=cached,
                row_ids=tuple(r.row_id for r in rows),
            )
        )

    # --- transitions ----------------------------------------------------------------------

    def advance(
        self,
        case: _Case,
        tick: int,
        event: RunnerEvent,
        to_state: LadderState,
        *,
        reason_code: str,
        row_ids: Iterable[str],
        key: str,
    ) -> None:
        try:
            check_transition(case.state, event, to_state)
        except IllegalTransition as exc:
            case.failure = str(exc)
            case.state = S.FAILED
            case.closed_tick = tick
            self.active_case.pop(case.contact_id, None)
            raise
        ids = tuple(sorted(set(row_ids)))
        missing = [i for i in ids if i not in self.table]
        if missing or not ids:
            raise RuntimeError(f"{case.case_id}: transition cites rows not in the table")
        self.transitions.append(
            Transition(
                seq=len(self.transitions),
                case_id=case.case_id,
                contact_id=case.contact_id,
                tick=tick,
                from_state=case.state,
                event=event,
                to_state=to_state,
                reason_code=reason_code,
                evidence_row_ids=ids,
                idempotency_key=key,
            )
        )
        case.state = to_state
        if to_state in V1_END_STATES:
            case.closed_tick = tick
            self.active_case.pop(case.contact_id, None)

    def transition_key(self, case: _Case, label: str, tick: int) -> str:
        return stable_hash(["transition", case.case_id, label, tick])

    # --- cases ----------------------------------------------------------------------------

    def open_case(self, c: _Contact, tick: int, *, opened_by: str) -> _Case:
        n = self.case_count.get(c.cid, 0) + 1
        self.case_count[c.cid] = n
        case = _Case(
            case_id=f"{c.cid}#{n}",
            contact_id=c.cid,
            opened_tick=tick,
            opened_by="plan_watch" if opened_by == "plan_watch" else "new_track",
        )
        self.cases[case.case_id] = case
        self.active_case[c.cid] = case.case_id
        return case

    def on_new_track(self, c: _Contact, tick: int) -> None:
        case = self.open_case(c, tick, opened_by="new_track")
        view = self.view(c, tick)
        cited = [view.position.row_id]
        found = self.try_reacquire(c, tick, view.position)
        if found is not None:
            ids = carry_over_reacquired(
                self.table,
                found.old_id,
                c.cid,
                t=tick,
                match_confidence=found.match_confidence,
                reason=found.reason,
            )
            case.reacquired_from = found.old_id
            self.contacts[found.old_id].linked_to = c.cid
            self.reacquisitions.append(found)
            cited.append(ids[0])
        self.advance(
            case,
            tick,
            RunnerEvent.CONTACT_DETECTED,
            S.WATCHING,
            reason_code="contact_detected",
            row_ids=[i for i in cited if i is not None],
            key=self.transition_key(case, "contact_detected", tick),
        )

    def try_reacquire(self, c: _Contact, tick: int, first: PositionFix) -> ReacquireMatch | None:
        lost: list[LostTrack] = []
        for oid in sorted(self.contacts):
            o = self.contacts[oid]
            if oid == c.cid or o.linked_to is not None or o.n_seen == 0:
                continue
            if not o.last_tick + self.r.timeout_ticks < first.tick:
                continue
            if _beyond_reacquire_gap(self, o.last_tick, first.tick):  # WO-080 (same filter)
                continue
            if not self.table.for_contact(oid):
                continue
            lost.append(self.lost_track(o))
        if not lost:
            return None
        return match_reacquisition(
            first,
            None,  # one reading: the new track's speed is not measured yet
            lost,
            tick,
            self.tick_s,
            self.r.current_field,
            self.tide_phase(first.tick),
            self.cfg,
            new_id=c.cid,
        )

    def lost_track(self, o: _Contact) -> LostTrack:
        track = o.upto()
        last = track.points[-1]
        state = last_fitted_state(track_arrays(track))
        if state is None:
            lat, lon, vel = last.lat, last.lon, (0.0, 0.0)
        else:
            x, y, vx, vy = state
            lat, lon = from_local_m(x, y)
            vel = (round6(vx), round6(vy))
        seg = evidence_view(self.table, o.cid, track, last.tick, self.tick_s).position.row_id
        return LostTrack(
            contact_id=o.cid,
            last_fix=PositionFix(
                lat=round6(lat),
                lon=round6(lon),
                tick=last.tick,
                sigma_m=round6(last.sigma_m),
                row_id=seg,
            ),
            velocity_mps=vel,
            last_tick=last.tick,
        )

    def watch_step(self, case: _Case, c: _Contact, tick: int, *, force: str | None) -> None:
        """WATCHING: registry lookup each cadence; hand to ask once there is enough track."""
        cadence = (tick - c.first_tick) % self.cfg.evidence_every_ticks == 0
        ready = c.n_seen >= self.cfg.watch_min_points or force is not None
        if not (cadence or ready):
            return
        view = self.view(c, tick)
        _, ids, key = self.lookup(c, view, tick)
        if not ready:
            return
        reason = force if force is not None else "free_probes_complete"
        self.advance(
            case,
            tick,
            RunnerEvent.PROBE_RESULT,
            S.ASKING,
            reason_code=reason,
            row_ids=[*ids, view.position.row_id or ids[0]],
            key=key,
        )
        self.ask_send(case, c, tick)

    def ask_send(self, case: _Case, c: _Contact, tick: int) -> None:
        view = self.view(c, tick)
        result = self.send_hail(
            c, tick, previously_replied=view.lineage_replied, case_id=case.case_id, purpose="ask"
        )
        case.hail_keys.append(result.idempotency_key)
        self.schedule(result.reply_tick, "ask_reply", case.case_id, result)

    def schedule(self, tick: int, kind: str, key: str, hail: HailResult | None = None) -> None:
        bisect.insort(self.events, _Event(tick, _ORDER[kind], key, kind, hail))

    def ask_reply(self, case: _Case, c: _Contact, tick: int, hail: HailResult) -> None:
        if case.state is not S.ASKING:
            return
        rows = hail_evidence_rows(hail, tick_s=self.tick_s)
        self.append(rows)
        view = self.view(c, tick)
        lookup, lookup_ids, _ = self.lookup(c, view, tick, rows)
        outcome = match(view, self.plans.at(tick), tick, harbor=self.harbor, cfg=self.cfg)
        case.outcomes.append(outcome)
        code = outcome.code
        if code is MatchCode.PENDING_SILENCE:
            if len(case.hail_keys) < self.cfg.ask.max_hails_per_case:
                assert view.last_heard_tick is not None
                limit_s = self.cfg.lost_vehicle_silence_s
                limit_ticks = int(math.floor(round(limit_s / self.tick_s, 9)))
                due = view.last_heard_tick + limit_ticks + 1
                send_at = max(tick + 1, due - self.hail.latency_ticks)
                case.outcome = outcome
                self.schedule(send_at, "ask_send", case.case_id)
                return
            outcome = MatchOutcome(
                code=MatchCode.ESCALATE,
                reason=(
                    f"hail budget of {self.cfg.ask.max_hails_per_case} spent with silence "
                    f"still pending ({outcome.reason})"
                ),
                evidence_row_ids=outcome.evidence_row_ids,
                plan_id=outcome.plan_id,
                silence_s=outcome.silence_s,
            )
            case.outcomes.append(outcome)
            code = outcome.code
        case.outcome = outcome
        cited = [*outcome.evidence_row_ids, *lookup_ids]
        if code is MatchCode.CLEARED_AT_ASK:
            if lookup.match is not RegistryMatch.EXACT or view.hail_reply is not HailReply.VALID:
                raise IllegalTransition(f"{case.case_id}: CLEARED_AT_ASK needs exact and valid")
            self.advance(
                case,
                tick,
                RunnerEvent.STOP_RULE_FIRED,
                S.CLEARED_AT_ASK,
                reason_code=code.value,
                row_ids=cited,
                key=hail.idempotency_key,
            )
            assert outcome.plan_id is not None
            self.watches[c.cid] = _Watch(
                contact_id=c.cid,
                plan_id=outcome.plan_id,
                case_id=case.case_id,
                next_tick=hail.sent_tick + self.rehail_ticks,
            )
        elif code is MatchCode.LOST_VEHICLE:
            notice = self.notice(case, c, view, outcome, tick)
            self.advance(
                case,
                tick,
                RunnerEvent.STOP_RULE_FIRED,
                S.LOST_VEHICLE,
                reason_code=code.value,
                row_ids=[*cited, *notice.evidence_row_ids],
                key=hail.idempotency_key,
            )
            self.notices.append(notice)
            self.watches.pop(c.cid, None)
        else:
            self.advance(
                case,
                tick,
                RunnerEvent.PROBE_RESULT,
                S.INVESTIGATING,
                reason_code=code.value,
                row_ids=cited,
                key=hail.idempotency_key,
            )
            case.investigation_pending = True

    # --- plan watch -----------------------------------------------------------------------

    def plan_watch_sends(self, tick: int) -> None:
        for cid in sorted(self.watches):
            w = self.watches[cid]
            c = self.contacts[cid]
            plan = next((p for p in self.plans.at(tick) if p.plan_id == w.plan_id), None)
            ended = plan is None or plan.end_tick is None or tick > plan.end_tick
            if c.dropped or ended or cid in self.active_case:
                del self.watches[cid]
                continue
            if tick < w.next_tick:
                continue
            view = self.view(c, tick)
            result = self.send_hail(
                c,
                tick,
                previously_replied=view.lineage_replied,
                case_id=None,
                purpose="plan_watch",
            )
            w.next_tick = tick + self.rehail_ticks
            self.schedule(result.reply_tick, "watch_reply", cid, result)

    def watch_reply(self, c: _Contact, tick: int, hail: HailResult) -> None:
        self.append(hail_evidence_rows(hail, tick_s=self.tick_s))
        if c.cid in self.active_case or c.cid not in self.watches:
            return
        view = self.view(c, tick)
        outcome = match(view, self.plans.at(tick), tick, harbor=self.harbor, cfg=self.cfg)
        if outcome.code is not MatchCode.LOST_VEHICLE:
            return
        # Silence past the threshold: a NEW case; the cleared case stays terminal.
        del self.watches[c.cid]
        case = self.open_case(c, tick, opened_by="plan_watch")
        self.advance(
            case,
            tick,
            RunnerEvent.CONTACT_DETECTED,
            S.WATCHING,
            reason_code="plan_watch_silence",
            row_ids=[*outcome.evidence_row_ids],
            key=self.transition_key(case, "contact_detected", tick),
        )
        _, ids, key = self.lookup(c, view, tick)
        self.advance(
            case,
            tick,
            RunnerEvent.PROBE_RESULT,
            S.ASKING,
            reason_code="plan_watch_silence",
            row_ids=[*ids, *outcome.evidence_row_ids],
            key=key,
        )
        reused = self.send_hail(
            c,
            hail.sent_tick,
            previously_replied=view.lineage_replied,
            case_id=case.case_id,
            purpose="ask",
        )
        case.hail_keys.append(reused.idempotency_key)
        self.ask_reply(case, c, tick, reused)

    # --- the notice -----------------------------------------------------------------------

    def notice(
        self, case: _Case, c: _Contact, view: EvidenceView, outcome: MatchOutcome, tick: int
    ) -> LostVehicleNotice:
        plan_id = outcome.plan_id or view.lineage_plan_id
        assert plan_id is not None
        plan = next((p for p in self.plans.at(tick) if p.plan_id == plan_id), None)
        operator = plan.operator if plan is not None else "operator not on file"
        vehicle = plan.vehicle if plan is not None else "vehicle not on file"
        channel = plan.contact_channel if plan is not None else "no contact channel on file"
        pos = view.position
        cur_id = view.current_field_row_id
        if cur_id is None or pos.row_id is None:
            raise RuntimeError(f"{case.case_id}: a notice needs a position and current row")
        phase = self.tide_phase(pos.tick)
        box = drift_search_box(
            pos,
            tick,
            self.tick_s,
            self.r.current_field,
            phase,
            self.cfg,
            assumption_row_id=cur_id,
        )
        silence = outcome.silence_s if outcome.silence_s is not None else 0.0
        limit = self.cfg.lost_vehicle_silence_s
        drifted = distance_m(pos.lat, pos.lon, box.center_lat, box.center_lon)
        elapsed = (tick - pos.tick) * self.tick_s
        lines: list[NoticeLine] = []

        def add(section: NoticeSection, text: str, ids: Sequence[str | None]) -> None:
            clean = tuple(sorted({i for i in ids if i is not None}))
            lines.append(NoticeLine(section=section, text=text, evidence_row_ids=clean))

        add(
            "Contact",
            f"{c.cid} ({case.case_id}): {vehicle}, operator {operator}, filed on plan {plan_id}",
            [view.lineage_plan_row_id],
        )
        if view.reacquired_from is not None:
            add(
                "Contact",
                f"track re-acquired from {view.reacquired_from}; evidence carried over",
                [view.reacquired_row_id],
            )
        add(
            "What we know",
            f"last known position {pos.lat:.6f}, {pos.lon:.6f} at tick {pos.tick} "
            f"(t={pos.tick * self.tick_s:.0f} s), position sigma {pos.sigma_m:.1f} m",
            [pos.row_id],
        )
        reply = view.hail_reply.value if view.hail_reply is not None else "none"
        add(
            "What we know",
            f"acoustic hail answered {reply}; no reply from the vehicle's modem",
            [view.hail_row_id],
        )
        add(
            "What we know",
            f"silence {silence:.0f} s > {limit:.0f} s since it was last heard at tick "
            f"{view.last_heard_tick}",
            [view.last_heard_row_id],
        )
        add(
            "What we assume",
            f"search-box drift uses the configured current field ({phase} tide); current "
            "field is the configured default; no live gauge",
            [cur_id],
        )
        add(
            "Search box",
            f"centre {box.center_lat:.6f}, {box.center_lon:.6f}, radius {box.radius_m:.0f} m "
            f"at tick {tick}; drifted {drifted:.0f} m from the last known position over "
            f"{elapsed:.0f} s",
            [pos.row_id, cur_id],
        )
        all_ids = tuple(sorted({i for line in lines for i in line.evidence_row_ids}))
        return LostVehicleNotice(
            case_id=case.case_id,
            contact_id=c.cid,
            tick=tick,
            plan_id=plan_id,
            operator=operator,
            vehicle=vehicle,
            contact_channel=channel,
            last_known=pos,
            silence_s=silence,
            search_box=box,
            evidence_row_ids=all_ids,
            lines=tuple(lines),
        )

    # --- the loop -------------------------------------------------------------------------

    def guarded(self, case: _Case, fn: Callable[[], None]) -> None:
        """Run one case step; an illegal transition writes the case as FAILED."""
        try:
            fn()
        except IllegalTransition as exc:
            if case.state is not S.FAILED:
                case.failure = str(exc)
                case.state = S.FAILED
                case.closed_tick = self.clock.tick
                self.active_case.pop(case.contact_id, None)

    def step(self, tick: int) -> None:
        in_run = tick < self.sim.n_ticks
        for cid in sorted(self.contacts):
            c = self.contacts[cid]
            c.n_seen = bisect.bisect_right(c.ticks, tick)
            if not c.dropped and tick >= c.dropped_at:
                c.dropped = True
        # New observed ids.
        for cid in sorted(self.contacts):
            c = self.contacts[cid]
            if c.first_tick == tick:
                self.on_new_track(c, tick)
        # Evidence cadence for every tracked contact.
        for cid in sorted(self.contacts):
            c = self.contacts[cid]
            if c.n_seen and not c.dropped:
                if (tick - c.first_tick) % self.cfg.evidence_every_ticks == 0:
                    self.extract(c)
            if c.dropped and tick == c.dropped_at:
                self.extract(c)
        # WATCHING cases.
        for case_id in sorted(self.cases):
            case = self.cases[case_id]
            if case.state is not S.WATCHING:
                continue
            c = self.contacts[case.contact_id]
            force = None
            if c.dropped:
                force = "track_dropped"
            elif not in_run:
                force = "run_ended"
            self.guarded(case, partial(self.watch_step, case, c, tick, force=force))
        # Scheduled hail sends and replies.
        while self.events and self.events[0].tick <= tick:
            ev = self.events.pop(0)
            if ev.kind == "watch_reply":
                assert ev.hail is not None
                self.watch_reply(self.contacts[ev.key], tick, ev.hail)
                continue
            case = self.cases[ev.key]
            c = self.contacts[case.contact_id]
            if ev.kind == "ask_send":
                if case.state is S.ASKING:
                    self.guarded(case, partial(self.ask_send, case, c, tick))
            else:
                assert ev.hail is not None
                self.guarded(case, partial(self.ask_reply, case, c, tick, ev.hail))
        if in_run:
            self.plan_watch_sends(tick)

    def execute(self) -> RunResult:
        n = self.sim.n_ticks
        for tick in range(n):
            self.clock.tick = tick
            self.step(tick)
        tick = n
        while self.events or any(k.state is S.WATCHING for k in self.cases.values()):
            self.clock.tick = tick
            self.step(tick)
            tick += 1
        return self.result()

    def result(self) -> RunResult:
        cases = [self.cases[k].result() for k in sorted(self.cases, key=_case_sort_key)]
        probes = {
            ProbeId.ACOUSTIC_HAIL.value: len(self.hail_keys_sent),
            ProbeId.REGISTRY_LOOKUP.value: len(self.lookup_keys),
        }
        return RunResult(
            scenario_id=self.r.scenario.id,
            seed=self.r.seed,
            sim=self.sim,
            cases=cases,
            transitions=list(self.transitions),
            evidence=self.table,
            notices=list(self.notices),
            interruptions=0,
            probes_used=probes,
            transition_log_hash=transition_log_hash_of(self.transitions),
            hails=tuple(self.hail_records),
            reacquisitions=tuple(self.reacquisitions),
        )


def _case_sort_key(case_id: str) -> tuple[str, int]:
    cid, _, n = case_id.partition("#")
    return cid, int(n)


def hail_count(result: RunResult, case_id: str) -> int:
    """Hails a case used (distinct idempotency keys)."""
    return len(set(result.case(case_id).hail_keys))


def format_transition(t: Transition) -> str:
    return (
        f"{t.seq}\t{t.tick}\t{t.case_id}\t{t.from_state.value}->{t.to_state.value}\t"
        f"{t.event.value}\t{t.reason_code}"
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m soundings.runner", description="Run a scenario through the ask rung."
    )
    parser.add_argument("--scenario", required=True, help="catalog id (S01), smoke, or a path")
    parser.add_argument("--seed", type=int, default=None, help="default: the scenario's seed")
    parser.add_argument("--plans", choices=("scenario", "db"), default="scenario")
    parser.add_argument("--out", default=None, help="directory for transitions.jsonl")
    args = parser.parse_args(argv)
    plans: RegistryStore | None = RegistryStore() if args.plans == "db" else None
    try:
        result = ScenarioRunner(args.scenario, seed=args.seed, plans=plans).run()
    finally:
        if plans is not None:
            plans.close()
    for t in result.transitions:
        print(format_transition(t))
    if args.out:
        out = Path(args.out)
        write_transition_log(result, out / "transitions.jsonl")
        notices = "".join(f"{canonical_json(n)}\n" for n in result.notices)
        (out / "notices.jsonl").write_text(notices, encoding="utf-8")
    print(f"transition_log_hash={result.transition_log_hash}")
    return 0


# --- WO-022 end ---

# --- WO-080 begin: re-acquisition gap pre-filter ---


def _beyond_reacquire_gap(run: _V1Run, last_tick: int, first_tick: int) -> bool:
    """``match_reacquisition``'s own gap test, applied before building a ``LostTrack``.

    Outcome-identical (the matcher rejects these candidates by the same test); it saves
    building a LostTrack for every long-lost track, which dominated BT-01's runtime.
    """
    return (first_tick - last_tick) * run.tick_s > run.cfg.reacquire.max_gap_s


# --- WO-080 end ---

# --- WO-033 begin: runner v2 ---
# Runner v2: the matcher's ESCALATE enters the investigation (ASKING -> INVESTIGATING, as in
# v1), the part A ``InvestigationMachine`` runs tick by tick through WO-031's planner and
# probes (WO-032's roles when ``llm_roles=True``, replay only), and its frozen
# ``Recommendation`` ends the case through ``stop_rule_fired(outcome)``. ``ScenarioRunner``
# (v1) is unchanged; ``ScenarioRunnerV2`` subclasses it. Phase 3 maps HANDOFF straight to
# HANDED_OFF (nothing pre-authorized; WO-043 adds the slow/hold pass-through). The handoff
# SITREP to the watch officer counts as one interruption, as does an eyes-on probe
# (DECISIONS 2026-10-16 23:05, one definition). The recommended rung is a computed field
# of the frozen recommendation; nothing here can edit it. runner.py imports nothing from
# ``soundings.llm``: the model roles are built inside ``soundings.investigation_wiring``.

import dataclasses  # noqa: E402  # WO-088
from collections.abc import Mapping  # noqa: E402
from types import MappingProxyType  # noqa: E402
from typing import Any  # noqa: E402

from soundings.investigation.machine import (  # noqa: E402
    InvestigationCheckpoint,
    InvestigationLedger,
    InvestigationState,
    Recommendation,
    StopOutcome,
    load_budgets,
    load_stopping_config,
)
from soundings.investigation.planner import PlannerChoice  # noqa: E402
from soundings.investigation.pol import load_pol_table  # noqa: E402
from soundings.investigation.posterior import (  # noqa: E402
    load_likelihood_tables,
    load_prior_config,
)
from soundings.investigation.probes import (  # noqa: E402  # WO-088
    ResponderPort,
    run_probe,
    search_position,
)
from soundings.investigation_wiring import (  # noqa: E402
    DEFAULT_VIOLATIONS_PATH,
    InvestigationHandle,
    ProbeWorld,
    make_kit,
    responder_available,  # WO-088
)

# --- WO-088 begin ---
RESPONDER_SOURCE = "responder"
"""``ProbeCall.source`` of a dispatch the runner makes itself (the S02 search beat)."""
# --- WO-088 end ---

OUTCOME_STATES: dict[StopOutcome, LadderState] = {
    StopOutcome.RESOLVED_BENIGN: S.RESOLVED_BENIGN,
    StopOutcome.HAZARD_NOTICE: S.HAZARD_NOTICE,
    StopOutcome.CLEARED_AT_ASK: S.CLEARED_AT_ASK,
    StopOutcome.LOST_VEHICLE: S.LOST_VEHICLE,
    StopOutcome.HANDOFF: S.HANDED_OFF,
}

V2_TRANSITIONS: frozenset[tuple[LadderState, RunnerEvent, LadderState]] = frozenset(
    (S.INVESTIGATING, RunnerEvent.STOP_RULE_FIRED, to) for to in OUTCOME_STATES.values()
)
"""SPEC-LADDER section 3 INVESTIGATING rows runner v2 adds (Phase 3: no slow/hold)."""


def check_transition_v2(from_state: LadderState, event: RunnerEvent, to_state: LadderState) -> None:
    """Raise ``IllegalTransition`` unless ``(from, event, to)`` is in ``V2_TRANSITIONS``."""
    if (from_state, event, to_state) not in V2_TRANSITIONS:
        raise IllegalTransition(
            f"{from_state.value} x {event.value} -> {to_state.value} is not a runner v2 row"
        )


def outcome_to_ladder_state(outcome: StopOutcome) -> LadderState:
    """HANDOFF -> HANDED_OFF; every other outcome -> the same-named state."""
    return OUTCOME_STATES[StopOutcome(outcome)]


class ProbeCall(StrictModel):
    """One investigation probe the run executed (``cached`` for an idempotent repeat)."""

    case_id: StrictStr
    contact_id: StrictStr
    probe_id: ProbeId
    tick: NonNegInt
    idempotency_key: StrictStr
    cost_usd: NonNegFloat
    cached: StrictBool
    source: StrictStr


class CasePlannerChoice(StrictModel):
    """One planner decision (WO-031 ``PlannerChoice``) with its case and tick."""

    case_id: StrictStr
    contact_id: StrictStr
    tick: NonNegInt
    choice: PlannerChoice

    @property
    def source(self) -> str:
        return self.choice.source


@dataclass(frozen=True)
class RunResultV2(RunResult):
    """``RunResult`` plus the investigation. ``recommendations`` is read-only."""

    recommendations: Mapping[str, Recommendation] = field(
        default_factory=lambda: MappingProxyType({})
    )
    investigation_checkpoints: Mapping[str, tuple[InvestigationCheckpoint, ...]] = field(
        default_factory=lambda: MappingProxyType({})
    )
    probe_calls: tuple[ProbeCall, ...] = ()
    planner_choices: tuple[CasePlannerChoice, ...] = ()
    nested_log: tuple[str, ...] = ()
    nested_log_hash: str = ""


def nested_log_text(lines: Iterable[str]) -> str:
    return "".join(f"{line}\n" for line in lines)


class _V2Run(_V1Run):
    """Runner v1 plus one ``InvestigationHandle`` per escalated case, stepped each tick."""

    def __init__(
        self,
        runner: ScenarioRunner,
        *,
        llm_roles: bool,
        violations_path: Path,
        fixture_store: Any,
        responder: ResponderPort | None = None,  # WO-088
    ) -> None:
        super().__init__(runner)
        sc = runner.scenario
        world = ProbeWorld(
            run=self.sim,
            scenario=sc,
            harbor=self.harbor,
            current_field=runner.current_field,
            pol=load_pol_table(),
            plans_at=self.plans.at,
            hail=self.hail,
            match_cfg=self.cfg,
            known=runner.known,
            responder=responder,  # WO-088
        )
        self.kit = make_kit(
            world,
            tables=load_likelihood_tables(),
            prior_cfg=load_prior_config(),
            stopping=load_stopping_config(),
            budgets=load_budgets(),
            llm_roles=llm_roles,
            fixture_store=fixture_store,
            violations_path=violations_path,
            label=f"{sc.id}/seed{runner.seed}/",
        )
        self.handles: dict[str, InvestigationHandle] = {}
        self.recs: dict[str, Recommendation] = {}
        self.choices: list[CasePlannerChoice] = []
        self.calls: list[ProbeCall] = []
        self.case_interruptions: dict[str, int] = {}
        self.nested: list[str] = []
        self.logged = 0

    # --- entering the investigation -----------------------------------------------------

    def ask_reply(self, case: _Case, c: _Contact, tick: int, hail: HailResult) -> None:
        sent = len(self.notices)  # WO-088
        super().ask_reply(case, c, tick, hail)
        if len(self.notices) > sent and case.state is S.LOST_VEHICLE:  # WO-088
            self.responder_search_beat(case, c, self.notices[-1], tick)
        if case.state is S.INVESTIGATING and case.case_id not in self.handles:
            # Budget scope (DECISIONS 23:05): the ask-rung hails count as paid probes.
            ledger = InvestigationLedger(paid_used=len(set(case.hail_keys)))
            self.handles[case.case_id] = self.kit.handle(
                c.cid, self.table, max_tick=self.sim.n_ticks - 1, ledger=ledger
            )

    # --- WO-088 begin ---
    def responder_search_beat(
        self, case: _Case, c: _Contact, notice: LostVehicleNotice, tick: int
    ) -> None:
        """The S02 search beat (CLAUDE.md section 8 A2): with a responder, a LOST_VEHICLE
        case sends it over the drift box (rung ask, one dispatch per contact, guard-checked)
        and the plan's operator gets a follow-up notice, stamped at the tick the search
        result lands, with what it found. Not an interruption (A5); the case stays closed."""
        world = self.kit.world
        if world.responder is None or not responder_available(world, c.cid, self.table, tick):
            return
        ctx = world.context(c.cid, tick, self.kit.rung)
        ctx = dataclasses.replace(ctx, search_box=notice.search_box)
        res = run_probe(ProbeId.RESPONDER_SEARCH_BOX, ctx, self.table, cache=self.kit.cache)
        self.calls.append(
            ProbeCall(
                case_id=case.case_id,
                contact_id=c.cid,
                probe_id=res.probe_id,
                tick=res.landed_tick,
                idempotency_key=res.key,
                cost_usd=0.0,
                cached=res.cached,
                source=RESPONDER_SOURCE,
            )
        )
        ids = tuple(sorted({r.row_id for r in res.rows}))
        line = {
            "case_id": case.case_id,
            "contact_id": c.cid,
            "probe_id": res.probe_id,
            "sent_tick": res.sent_tick,
            "landed_tick": res.landed_tick,
            "rows": list(ids),
        }
        self.nested.append(canonical_json({"responder": line}))
        if not ids:
            return
        found = search_position(res.rows)
        where = (
            f"found the vehicle at {found[0]:.6f}, {found[1]:.6f}"
            if found is not None
            else "did not find the vehicle in the box"
        )
        text = (
            f"responder searched the drift box (dispatched at tick {tick}, result at tick "
            f"{res.landed_tick}): {where}"
        )
        extra = NoticeLine(section="Search box", text=text, evidence_row_ids=ids)
        self.notices.append(
            notice.model_copy(
                update={
                    "tick": res.landed_tick,
                    "lines": (*notice.lines, extra),
                    "evidence_row_ids": tuple(sorted({*notice.evidence_row_ids, *ids})),
                }
            )
        )

    # --- WO-088 end ---

    # --- log --------------------------------------------------------------------------------

    def flush_ladder(self) -> None:
        for t in self.transitions[self.logged :]:
            self.nested.append(canonical_json({"ladder": t}))
        self.logged = len(self.transitions)

    def log_checkpoints(self, case: _Case, cps: Sequence[InvestigationCheckpoint]) -> None:
        for cp in cps:
            line = {
                "case_id": case.case_id,
                "seq": cp.seq,
                "contact_id": cp.contact_id,
                "tick": cp.tick,
                "from": cp.from_state,
                "event": cp.label,
                "to": cp.to_state,
                "action_key": cp.action_key,
                "hash": cp.hash,
            }
            if cp.recommendation is not None:
                line["recommendation_hash"] = cp.recommendation.recommendation_hash
                line["recommended_rung"] = cp.recommendation.recommended_rung
            self.nested.append(canonical_json({"investigation": line}))

    # --- stepping ---------------------------------------------------------------------------

    def step(self, tick: int) -> None:
        super().step(tick)
        self.flush_ladder()
        for case_id in sorted(self.handles, key=_case_sort_key):
            handle = self.handles[case_id]
            case = self.cases[case_id]
            if handle.done or case.state is not S.INVESTIGATING:
                continue
            before = len(self.kit.choices)
            cps = handle.step(tick) if handle.started else handle.start(tick)
            for rc in self.kit.choices[before:]:
                self.choices.append(
                    CasePlannerChoice(
                        case_id=case_id, contact_id=rc.contact_id, tick=rc.tick, choice=rc.choice
                    )
                )
            self.log_checkpoints(case, cps)
            if handle.done:
                self.finish(case, handle, tick)
                self.flush_ladder()

    def finish(self, case: _Case, handle: InvestigationHandle, tick: int) -> None:
        result = handle.result()
        for call in result.probe_calls:
            self.calls.append(
                ProbeCall(
                    case_id=case.case_id, **call.model_dump(exclude={"source"}), source=call.source
                )
            )
        rec = result.recommendation
        if result.state is not InvestigationState.STOPPED or rec is None:
            case.failure = f"investigation {result.state.value}: {result.error}"
            case.state = S.FAILED
            case.closed_tick = tick
            # A failed investigation goes to a person: one interruption (SPEC-LADDER 3).
            self.case_interruptions[case.case_id] = result.ledger.interruptions + 1
            return
        self.recs[case.case_id] = rec
        to_state = outcome_to_ladder_state(rec.outcome)
        handoff = to_state is S.HANDED_OFF
        self.case_interruptions[case.case_id] = rec.interruptions + (1 if handoff else 0)
        cited = [i for i in rec.evidence_row_ids if i in self.table]
        if to_state is S.LOST_VEHICLE:
            c = self.contacts[case.contact_id]
            view = self.view(c, tick)
            outcome = case.outcome
            if outcome is not None and (outcome.plan_id or view.lineage_plan_id):
                notice = self.notice(case, c, view, outcome, tick)
                self.notices.append(notice)
                cited += list(notice.evidence_row_ids)
                self.responder_search_beat(case, c, notice, tick)  # WO-088
        self.advance(
            case,
            tick,
            RunnerEvent.STOP_RULE_FIRED,
            to_state,
            reason_code=f"{rec.outcome.value}:{rec.rule_id.value}",
            row_ids=cited,
            key=result.checkpoints[-1].action_key,
        )
        case.investigation_pending = False

    def advance(
        self,
        case: _Case,
        tick: int,
        event: RunnerEvent,
        to_state: LadderState,
        *,
        reason_code: str,
        row_ids: Iterable[str],
        key: str,
    ) -> None:
        """v1 ``advance`` for the ask rows; ``check_transition_v2`` for INVESTIGATING."""
        if case.state is not S.INVESTIGATING:
            super().advance(
                case, tick, event, to_state, reason_code=reason_code, row_ids=row_ids, key=key
            )
            return
        check_transition_v2(case.state, event, to_state)
        ids = tuple(sorted(set(row_ids)))
        if not ids or any(i not in self.table for i in ids):
            raise RuntimeError(f"{case.case_id}: transition cites rows not in the table")
        self.transitions.append(
            Transition(
                seq=len(self.transitions),
                case_id=case.case_id,
                contact_id=case.contact_id,
                tick=tick,
                from_state=case.state,
                event=event,
                to_state=to_state,
                reason_code=reason_code,
                evidence_row_ids=ids,
                idempotency_key=key,
            )
        )
        case.state = to_state
        case.closed_tick = tick

    def active(self) -> bool:
        return any(
            not h.done and self.cases[k].state is S.INVESTIGATING for k, h in self.handles.items()
        )

    def execute(self) -> RunResult:
        n = self.sim.n_ticks
        for tick in range(n):
            self.clock.tick = tick
            self.step(tick)
        tick = n
        while (
            self.events or any(k.state is S.WATCHING for k in self.cases.values()) or self.active()
        ):
            self.clock.tick = tick
            self.step(tick)
            tick += 1
        return self.result()

    def result(self) -> RunResultV2:
        base = super().result()
        probes = dict(base.probes_used)
        for call in self.calls:
            if not call.cached:
                probes[call.probe_id.value] = probes.get(call.probe_id.value, 0) + 1
        checkpoints = {
            k: self.handles[k].result().checkpoints
            for k in sorted(self.handles)
            if self.handles[k].started
        }
        nested = tuple(self.nested)
        return RunResultV2(
            scenario_id=base.scenario_id,
            seed=base.seed,
            sim=base.sim,
            cases=base.cases,
            transitions=base.transitions,
            evidence=base.evidence,
            notices=base.notices,
            interruptions=sum(self.case_interruptions.values()),
            probes_used=dict(sorted(probes.items())),
            transition_log_hash=base.transition_log_hash,
            hails=base.hails,
            reacquisitions=base.reacquisitions,
            recommendations=MappingProxyType(dict(sorted(self.recs.items()))),
            investigation_checkpoints=MappingProxyType(checkpoints),
            probe_calls=tuple(self.calls),
            planner_choices=tuple(self.choices),
            nested_log=nested,
            nested_log_hash=hashlib.sha256(nested_log_text(nested).encode("utf-8")).hexdigest(),
        )


class ScenarioRunnerV2(ScenarioRunner):
    """Runner v2: v1 through the matcher, then the investigation for every ESCALATE."""

    def run(
        self,
        *,
        llm_roles: bool = False,
        violations_path: Path = DEFAULT_VIOLATIONS_PATH,
        fixture_store: Any = None,
        responder: ResponderPort | None = None,
    ) -> RunResultV2:
        """``llm_roles`` defaults to False (DECISIONS 23:05); when on, replay only.
        ``responder`` (WO-088, default None: the catalog run) adds the S02 search beat
        and lets the planner dispatch the responder to look."""
        run = _V2Run(
            self,
            llm_roles=llm_roles,
            violations_path=violations_path,
            fixture_store=fixture_store,
            responder=responder,
        )
        result = run.execute()
        assert isinstance(result, RunResultV2)
        return result


def case_interruptions(result: RunResultV2, case_id: str) -> int:
    """Interruptions of one case: eyes-on probes plus the handoff SITREP."""
    rec = result.recommendations.get(case_id)
    if rec is None:
        return 0
    return rec.interruptions + (1 if rec.outcome is StopOutcome.HANDOFF else 0)


def main_v2(argv: Sequence[str] | None = None) -> int:
    """``python -m soundings.runner_v2 --scenario S05 --seed 11``: the nested canonical
    JSONL log (ladder transitions with the investigation transitions nested), then the
    two hashes."""
    parser = argparse.ArgumentParser(
        prog="python -m soundings.runner_v2",
        description="Run a scenario through the ask rung and the investigation (runner v2).",
    )
    parser.add_argument("--scenario", required=True, help="catalog id (S05), smoke, or a path")
    parser.add_argument("--seed", type=int, default=None, help="default: the scenario's seed")
    args = parser.parse_args(argv)
    result = ScenarioRunnerV2(args.scenario, seed=args.seed).run()
    sys.stdout.write(nested_log_text(result.nested_log))
    print(f"transition_log_hash={result.transition_log_hash}")
    print(f"nested_log_hash={result.nested_log_hash}")
    return 0


# --- WO-033 end: runner v2 ---

# --- v3 (WO-043) ---
# --- WO-043 begin: runner v3 ---
# Runner v3: sim -> evidence -> ask (v1) -> investigation (v2) -> the WO-043 ladder driver
# (BoundaryGuards under signed standing orders, or NoStandingOrdersGuards when unsigned),
# the simulated approver (or any ``ApproverPort``), WO-042's ``HandoffService`` as SITREP
# writer and listener (template by default; replay through a caller's client in ``auto``),
# the hash-chained audit log, then watch-after-handoff. Runner v2 runs unchanged on the
# scenario's own ``duration_ticks`` (every v1/v2 field is identical); the ladder observes an
# extended sim of ``duration_ticks + tail`` ticks (tail from config, capped at 2x, WO-011
# prefix stability), so timers that come due after the scenario's end still fire.
# ``ScenarioRunnerV3.start()`` precomputes v1/v2 and the feeds; each ``RunSession.step()``
# advances the ladder one tick (the UI run engine steps it; approvals arrive through the
# ``ApproverPort``, e.g. ``ManualApprover.reply``). ``run()`` is ``start()`` stepped to the
# end. CLAUDE.md 2.1: after HANDED_OFF only watching happens. 2.2: the driver passes only
# guard decisions to the machine; runner.py still imports nothing from ``soundings.llm``.

from collections import Counter  # noqa: E402

from soundings.investigation.posterior import score_contact  # noqa: E402
from soundings.ladder.approver_sim import ApproverScript  # noqa: E402
from soundings.ladder.audit import AuditKind, AuditLog  # noqa: E402
from soundings.ladder.driver import (  # noqa: E402
    INVESTIGATION_OUTCOMES,
    ContactFeed,
    DriverSession,
    LadderDriver,
    LadderRun,
    Snapshot,
)
from soundings.ladder.guards import BoundaryGuards  # noqa: E402
from soundings.ladder.handoff import HandoffPacket, HandoffService  # noqa: E402
from soundings.ladder.machine import StepResult  # noqa: E402
from soundings.ladder.ports import ApprovalReply, ApproverPort  # noqa: E402
from soundings.ladder.sitrep import Sitrep, SitrepStatus  # noqa: E402
from soundings.ladder.sitrep import SitrepInputs as WriterInputs  # noqa: E402
from soundings.ladder.sitrep_port import SitrepInputs as PortInputs  # noqa: E402
from soundings.ladder.types import ActionKind, ActionScope  # noqa: E402
from soundings.ladder.watch import DepartureDetector, WatchSummary  # noqa: E402
from soundings.schemas import TrackPoint  # noqa: E402
from soundings.schemas.config import STANDING_ORDERS_PATH  # noqa: E402
from soundings.schemas.enums import EvidenceKind, FeatureName, Rung, ToggleName  # noqa: E402
from soundings.schemas.scenario import Toggle  # noqa: E402

MONITOR_SOURCE = "ladder_monitor"
"""Source of the rows the ladder re-extracts from the track prefix (guards read evidence)."""
MONITOR_FEATURES: tuple[FeatureName, ...] = (
    FeatureName.TIME_TO_NEAREST_CRITICAL_ASSET,
    FeatureName.ZONE_CRITICALITY,
    FeatureName.NEAREST_ASSET_ID,
    FeatureName.ZONE_ID,
)
ApproverChoice = Literal["default", "denied", "unreachable"]
WriterModeV3 = Literal["template", "auto"]
INTERRUPTION_ACTIONS = frozenset({ActionKind.REQUEST_APPROVAL, ActionKind.PAGE_BACKUP})


class ManualApprover:
    """An ``ApproverPort`` a person answers (the WO-061 live approval path).

    ``request`` records what the ladder asked; ``reply(contact_id, decision, at_s, token=)``
    queues the person's answer, and the driver polls it on the next tick. A grant must carry
    the WO-041 token the approval UI issued (``issue_token`` is called only from the approver
    paths, ``tests/test_approval.py``); the ladder's guard checks it against the requested
    scope, so a grant without a valid token for exactly that scope changes nothing.
    """

    def __init__(self) -> None:
        self.requests: list[tuple[str, ActionScope, str, float]] = []
        self._queue: list[ApprovalReply] = []
        self._answered: set[int] = set()

    def request(self, contact_id: str, scope: ActionScope, channel: str, now_s: float) -> None:
        self.requests.append((contact_id, scope, channel, round6(now_s)))

    def open_requests(self) -> list[tuple[str, ActionScope, str, float]]:
        return [r for i, r in enumerate(self.requests) if i not in self._answered]

    def reply(
        self,
        contact_id: str,
        decision: Literal["granted", "denied"],
        at_s: float,
        token: Mapping[str, Any] | None = None,
    ) -> ApprovalReply:
        """Answer the oldest open request for ``contact_id``."""
        for i, (cid, _scope, channel, _) in enumerate(self.requests):
            if cid != contact_id or i in self._answered:
                continue
            self._answered.add(i)
            reply = ApprovalReply(
                contact_id=cid,
                decision=decision,
                token=None if token is None else dict(token),
                channel=channel,
                at_s=round6(at_s),
            )
            self._queue.append(reply)
            return reply
        raise KeyError(f"no open approval request for {contact_id}")

    def poll(self, now_s: float) -> list[ApprovalReply]:
        out, self._queue = self._queue, []
        return out


@dataclass(frozen=True)
class StepRecord:
    """One ladder step of one ``RunSession.step()`` (UI run engine, WO-060 part B)."""

    tick: int
    t_s: float
    contact_id: str | None
    from_state: LadderState
    to_state: LadderState
    event: str
    arg: str | None
    transitioned: bool
    reason: str
    actions: tuple[str, ...]


@dataclass(frozen=True)
class LadderTransition:
    """One ladder checkpoint as the eval reads it (WO-050 ``Transition`` field names)."""

    seq: int
    contact_id: str
    case_id: str | None
    tick: int
    t_s: float
    from_state: LadderState
    to_state: LadderState
    event: str
    arg: str | None
    action: str | None
    asset_id: str | None
    probe_id: str | None
    rung: Rung
    hash: str


@dataclass(frozen=True)
class RunResultV3(RunResultV2):
    """``RunResultV2`` plus the ladder. ``interruptions``/``probes_used`` count approvals.

    ``sim`` stays the scenario-length run (v2); ``sim_extended`` is what the ladder watched.
    """

    ladder: Mapping[str, LadderRun] = field(default_factory=lambda: MappingProxyType({}))
    handling_outcome: Mapping[str, LadderState] = field(
        default_factory=lambda: MappingProxyType({})
    )
    sitreps: Mapping[str, tuple[Sitrep, ...]] = field(default_factory=lambda: MappingProxyType({}))
    sitrep_packets: tuple[HandoffPacket, ...] = ()
    watch: Mapping[str, WatchSummary] = field(default_factory=lambda: MappingProxyType({}))
    audit_run_id: str = ""
    audit_db: str = ""
    audit_head: str = ""
    audit_ok: bool = False
    audit_counts: Mapping[str, int] = field(default_factory=lambda: MappingProxyType({}))
    ladder_transitions: tuple[LadderTransition, ...] = ()
    ladder_log_hash: str = ""
    contact_interruptions: Mapping[str, int] = field(default_factory=lambda: MappingProxyType({}))
    contact_cases: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))
    planner_stats: Mapping[str, int] = field(default_factory=lambda: MappingProxyType({}))
    degraded_reasons: tuple[str, ...] = ()
    orders: str = "unsigned"
    sim_extended: SimRun | None = None
    tail_ticks: int = 0
    audit: AuditLog | None = None


def ladder_log_text(transitions: Iterable[LadderTransition]) -> str:
    """Canonical JSONL of the ladder transitions (the v3 determinism log)."""
    return "".join(f"{canonical_json(dataclasses.asdict(t))}\n" for t in transitions)


def _prefix(track: Track, upto_tick: int) -> Track | None:
    points = tuple(p for p in track.points if p.tick <= upto_tick)
    if not points:
        return None
    gaps = tuple(g for g in track.gaps if g.end_tick < points[-1].tick)
    return track.model_copy(update={"points": points, "gaps": gaps})


def monitor_rows(
    track: Track, harbor: Harbor, current_at: Any, hour_of_day: int
) -> tuple[EvidenceRow, ...]:
    """WO-012 kinematic and context rows over ``track`` re-labelled ``ladder_monitor``."""
    current_row = current_field_row(track, current_at)
    segments = track_segments(track)
    rows = extract_kinematics(track, harbor, current_at, segments=segments, current_row=current_row)
    rows += extract_context(track, harbor, hour_of_day=hour_of_day)
    out: list[EvidenceRow] = []
    for r in rows:
        if r.feature not in MONITOR_FEATURES:
            continue
        kind = EvidenceKind.FACT if r.kind is EvidenceKind.DERIVED else r.kind
        out.append(
            EvidenceRow(
                contact_id=r.contact_id,
                t=r.t,
                feature=r.feature,
                value=r.value,
                unit=r.unit,
                source=MONITOR_SOURCE,
                confidence=r.confidence,
                kind=kind,
                probe_id=None,
                assumes=r.assumes if kind is EvidenceKind.ASSUMPTION else "",
            )
        )
    return tuple(sorted(out, key=lambda r: (r.feature.value, r.row_id)))


def _number(rows: Iterable[EvidenceRow], feature: FeatureName) -> float | None:
    for r in rows:
        if r.feature is feature and isinstance(r.value, (int, float)):
            if not isinstance(r.value, bool):
                return round6(float(r.value))
    return None


def _text(rows: Iterable[EvidenceRow], feature: FeatureName) -> str | None:
    for r in rows:
        if r.feature is feature and isinstance(r.value, str) and r.value != "unknown":
            return r.value
    return None


def _basin(harbor: Harbor, track: Track, asset_id: str | None) -> str | None:
    """The basin a slow advisory would cover: the one holding the critical asset the contact
    is nearest to (the asset the advisory protects), else the contact's own basin."""
    ids = {a.asset_id for a in harbor.assets}
    if asset_id is not None and asset_id in ids:
        asset = harbor.asset(asset_id)
        zone = harbor.zone_at(asset.lat, asset.lon)
        if zone is not None:
            return str(zone.basin)
    last = track.points[-1]
    own = harbor.zone_at(last.lat, last.lon)
    return None if own is None else str(own.basin)


class _RunnerHandoff(HandoffService):
    """``HandoffService`` plus what runner v3 knows: the LOST_VEHICLE notice's search box
    (over the whole evidence table, so carried-over rows resolve) and the recommendation's
    ``question_for_human`` / ``uncertain``."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.notices: dict[str, LostVehicleNotice] = {}
        self.recs: dict[str, Recommendation] = {}
        self.table: EvidenceTable | None = None

    def adapt(self, port: PortInputs, rows: Sequence[EvidenceRow]) -> WriterInputs:
        cid = port.contact_id
        notice = self.notices.get(cid)
        if notice is not None and self.table is not None and port.kind == "final":
            report = score_contact(self.table, cid, load_likelihood_tables(), load_prior_config())
            rung = port.recommended_rung if port.recommended_rung is not None else port.rung
            inputs = WriterInputs.from_report(
                self.table,
                report,
                recommended_rung=rung,
                search_box=notice.search_box,
                decision_row_ids=notice.evidence_row_ids,
                status=SitrepStatus.FINAL,
            )
        else:
            inputs = super().adapt(port, rows)
        rec = self.recs.get(cid)
        if rec is not None:
            inputs = dataclasses.replace(
                inputs, question_for_human=rec.question_for_human, uncertain=rec.uncertain
            )
        return inputs


class RunSession:
    """One runner v3 execution, stepped one ladder tick at a time.

    ``step() -> list[StepRecord]``; ``done``; ``tick``; ``pending_approvals()`` (contacts in
    AWAITING_APPROVAL); ``open_requests()`` (what the approver was asked);
    ``result() -> RunResultV3`` (valid at any time; final once ``done``).
    """

    def __init__(
        self,
        runner: ScenarioRunnerV3,
        *,
        llm_roles: bool = False,
        violations_path: Path = DEFAULT_VIOLATIONS_PATH,
        fixture_store: Any = None,
        responder: ResponderPort | None = None,
        orders_path: str | Path | None = STANDING_ORDERS_PATH,
        unsigned: bool = False,
        approver: ApproverPort | None = None,
        approver_script: ApproverChoice | None = None,
        toggles: Iterable[ToggleName | str] = (),
        db_url: str | Path | None = None,
        writer_mode: WriterModeV3 = "template",
        sitrep_client: Any = None,
        run_id: str | None = None,
    ) -> None:
        self.runner = runner
        sc = runner.scenario
        self.v2 = ScenarioRunnerV2.run(
            runner,
            llm_roles=llm_roles,
            violations_path=violations_path,
            fixture_store=fixture_store,
            responder=responder,
        )
        names = sorted({ToggleName(t.name) for t in sc.toggles} | {ToggleName(t) for t in toggles})
        self.toggles = tuple(names)
        self.unsigned = bool(unsigned or orders_path is None)
        self.orders = "unsigned" if self.unsigned else "signed"
        self.run_id = run_id or f"{sc.id}-seed{runner.seed}-{self.orders}"
        self.audit_db = ":memory:" if db_url is None else str(db_url)
        self.audit = AuditLog(self.audit_db)
        self.service = _RunnerHandoff(
            sitrep_client,
            self.audit,
            run_id=self.run_id,
            violations_path=violations_path,
            writer_mode=writer_mode,
        )
        tick_s = float(self.v2.sim.tick_s)
        self.detector = DepartureDetector.from_config(runner.harbor, tick_s)
        script = None
        if approver_script == "denied":
            script = ApproverScript.denied()
        elif approver_script == "unreachable":
            script = ApproverScript.unreachable()
        elif approver_script == "default":
            script = ApproverScript.default()
        self.driver = LadderDriver.from_config(
            tick_s=tick_s,
            hour_of_day=sc.hour_of_day,
            orders_path=None if self.unsigned else orders_path,
            unsigned=self.unsigned,
            toggles=self.toggles,
            approver=approver,
            script=script,
            departure=self.detector,
            listeners=(self.service,),
            sitrep_writer=self.service,
        )
        if self.service.sink is not None:
            self.service.sink.clock = self.driver.clock
        guards = self.driver.guards
        reasons: tuple[ToggleName, ...] = ()
        if isinstance(guards, BoundaryGuards):
            reasons = guards.approver_status.degraded_reasons()
        else:
            degrading = {ToggleName.NETWORK_DOWN, ToggleName.APPROVER_UNREACHABLE}
            reasons = tuple(t for t in self.toggles if t in degrading)
        self.degraded_reasons = tuple(str(r.value) for r in reasons)
        # The tail: config-sized, capped at the scenario's own length (2x in all).
        self.tail_ticks = min(self.driver.max_tail_ticks, sc.duration_ticks)
        self.driver.max_tail_ticks = 0
        self.sim_ext = run_scenario(
            sc,
            seed=runner.seed,
            ticks=sc.duration_ticks + self.tail_ticks,
            harbor=runner.harbor,
        )
        cf = runner.current_field
        self.current_at = current_at_from_field(
            cf.vector_at, cf.phase_for_hour, hour_of_day=sc.hour_of_day
        )
        self.case_of: dict[str, str] = {}
        self.feeds = self._feeds()
        self.service.table = self.v2.evidence
        for n in self.v2.notices:
            if self.case_of.get(n.contact_id) == n.case_id:
                self.service.notices[n.contact_id] = n
        for case_id, rec in self.v2.recommendations.items():
            cid = case_id.partition("#")[0]
            if self.case_of.get(cid) == case_id:
                self.service.recs[cid] = rec
        self.session: DriverSession = self.driver.start(self.feeds)

    # --- feeds ------------------------------------------------------------------------------

    def _rows(self, ids: Iterable[str]) -> tuple[EvidenceRow, ...]:
        table = self.v2.evidence
        return tuple(table.get(i) for i in sorted(set(ids)) if i in table)

    def _feeds(self) -> list[ContactFeed]:
        v2 = self.v2
        by_case: dict[str, list[Transition]] = {}
        for t in v2.transitions:
            by_case.setdefault(t.case_id, []).append(t)
        calls: dict[str, list[dict[str, Any]]] = {}
        for call in v2.probe_calls:
            calls.setdefault(call.case_id, []).append(
                {
                    "probe_id": call.probe_id.value,
                    "idempotency_key": call.idempotency_key,
                    "cost_usd": call.cost_usd,
                }
            )
        feeds: list[ContactFeed] = []
        for case in v2.cases:  # sorted by case id; one ladder per contact (its first case)
            cid = case.contact_id
            if cid in self.case_of or case.case_id not in by_case:
                continue
            feed = self._feed(case, by_case[case.case_id], tuple(calls.get(case.case_id, ())))
            if feed is not None:
                self.case_of[cid] = case.case_id
                feeds.append(feed)
        return feeds

    def _feed(
        self, case: CaseResult, ts: list[Transition], calls: tuple[Mapping[str, Any], ...]
    ) -> ContactFeed | None:
        cid = case.contact_id
        detected = next((t for t in ts if t.event is RunnerEvent.CONTACT_DETECTED), None)
        if detected is None:
            return None
        watch = next((t for t in ts if t.from_state is S.WATCHING), None)
        ask = next((t for t in ts if t.from_state is S.ASKING), None)
        ask_outcome: str | None = None
        ask_ids: list[str] = []
        if ask is not None:
            ask_ids = list(ask.evidence_row_ids)
            if ask.to_state in (S.CLEARED_AT_ASK, S.LOST_VEHICLE):
                ask_outcome = ask.to_state.value
        track = self.sim_ext.tracks.get(cid)
        points: tuple[TrackPoint, ...] = () if track is None else tuple(track.points)
        snaps: list[Snapshot] = []
        rec = self.v2.recommendations.get(case.case_id)
        stop = next((t for t in ts if t.from_state is S.INVESTIGATING), None)
        if rec is not None and stop is not None and rec.outcome.value in INVESTIGATION_OUTCOMES:
            snaps.append(self._snapshot(cid, track, stop.tick, rec, rec.outcome.value, stop))
            if rec.outcome is StopOutcome.HANDOFF and track is not None:
                every = self.runner.match_cfg.evidence_every_ticks
                for p in track.points:
                    if p.tick > stop.tick and (p.tick - stop.tick) % every == 0:
                        snaps.append(self._snapshot(cid, track, p.tick, rec, None, None))
        return ContactFeed(
            contact_id=cid,
            detected_tick=detected.tick,
            detection_rows=self._rows(detected.evidence_row_ids),
            watch_tick=None if watch is None else watch.tick,
            watch_rows=() if watch is None else self._rows(watch.evidence_row_ids),
            ask_tick=None if ask is None else ask.tick,
            ask_outcome=ask_outcome,
            ask_rows=self._rows(ask_ids),
            recommendations=tuple(snaps),
            points=points,
            probe_calls=calls,
        )

    def _snapshot(
        self,
        cid: str,
        track: Track | None,
        tick: int,
        rec: Recommendation,
        outcome: str | None,
        stop: Transition | None,
    ) -> Snapshot:
        monitor: tuple[EvidenceRow, ...] = ()
        prefix = None if track is None else _prefix(track, tick)
        if prefix is not None:
            monitor = monitor_rows(
                prefix, self.runner.harbor, self.current_at, self.runner.scenario.hour_of_day
            )
        asset_id = _text(monitor, FeatureName.NEAREST_ASSET_ID)
        basin = None if prefix is None else _basin(self.runner.harbor, prefix, asset_id)
        cited: tuple[EvidenceRow, ...] = ()
        if stop is not None:
            cited = self._rows([*rec.evidence_row_ids, *stop.evidence_row_ids])
        return Snapshot(
            tick=tick,
            rows=cited + monitor,
            outcome=outcome,
            recommended_rung=rec.recommended_rung,
            threat_mass=round6(rec.threat_mass),
            benign_mass=round6(rec.benign_mass),
            basin=basin,
            nearest_asset_id=asset_id,
            time_to_nearest_critical_asset=_number(
                monitor, FeatureName.TIME_TO_NEAREST_CRITICAL_ASSET
            ),
        )

    # --- stepping ---------------------------------------------------------------------------

    @property
    def done(self) -> bool:
        return self.session.done

    @property
    def tick(self) -> int:
        return self.session.tick

    def step(self) -> list[StepRecord]:
        """Advance the ladder (and the watch) one tick."""
        t_s = 0.0
        out: list[StepRecord] = []
        for r in self.session.step():
            t_s = round6(self.driver.clock.now_s())
            out.append(_step_record(r, t_s))
        return out

    def pending_approvals(self) -> list[str]:
        return self.session.pending_approvals()

    def open_requests(self) -> list[tuple[str, ActionScope, str, float]]:
        approver = self.driver.approver
        if isinstance(approver, ManualApprover):
            return approver.open_requests()
        reqs = getattr(approver, "requests", [])
        return [
            (r.contact_id, r.scope, r.channel, r.requested_at)
            for r in reqs
            if hasattr(r, "contact_id")
        ]

    def approve(
        self,
        contact_id: str,
        decision: Literal["granted", "denied"] = "granted",
        token: Mapping[str, Any] | None = None,
    ) -> None:
        """A person's answer through a ``ManualApprover`` (delivered on the next step)."""
        approver = self.driver.approver
        if not isinstance(approver, ManualApprover):
            raise TypeError("approve() needs ScenarioRunnerV3.start(approver=ManualApprover())")
        approver.reply(contact_id, decision, self.driver.clock.now_s(), token=token)

    def run_to_end(self) -> RunResultV3:
        while not self.done:
            self.step()
        return self.result()

    # --- result -----------------------------------------------------------------------------

    def result(self) -> RunResultV3:
        v2 = self.v2
        runs = self.session.results()
        tick_s = float(v2.sim.tick_s)
        recs_by_contact = {
            cid: v2.recommendations[case_id]
            for cid, case_id in self.case_of.items()
            if case_id in v2.recommendations
        }
        per_contact: dict[str, int] = {}
        for cid in sorted(runs):
            run = runs[cid]
            rec = recs_by_contact.get(cid)
            eyes = rec.interruptions if rec is not None else 0
            asked = any(a.kind is ActionKind.REQUEST_APPROVAL for a in run.actions)
            handoff = 1 if run.handling_outcome is S.HANDED_OFF and not asked else 0
            per_contact[cid] = eyes + run.interruptions + handoff
        for case_id, rec in v2.recommendations.items():  # contacts without a ladder run
            cid = case_id.partition("#")[0]
            if cid not in runs:
                per_contact[cid] = per_contact.get(cid, 0) + rec.interruptions
        probes = dict(v2.probes_used)
        counts = Counter(
            a.kind.value
            for cid in sorted(runs)
            for a in runs[cid].actions
            if a.kind in INTERRUPTION_ACTIONS
        )
        for kind in sorted(INTERRUPTION_ACTIONS, key=lambda k: k.value):
            probes[kind.value] = counts.get(kind.value, 0)
        trans = _ladder_transitions(runs, self.case_of, tick_s)
        packets = tuple(self.service.packets)
        sitreps = {
            cid: tuple(p.sitrep for p in packets if p.contact_id == cid)
            for cid in sorted({p.contact_id for p in packets})
        }
        verify = self.audit.verify()
        audit_counts = {
            kind.value: len(self.audit.rows(run_id=self.run_id, kind=kind)) for kind in AuditKind
        }
        stats = Counter(c.source for c in v2.planner_choices)
        watch = {cid: r.watch for cid, r in sorted(runs.items()) if r.watch is not None}
        handling = {
            cid: r.handling_outcome
            for cid, r in sorted(runs.items())
            if r.handling_outcome is not None
        }
        log_text = ladder_log_text(trans)
        return dataclasses.replace(
            _as_v3(v2),
            interruptions=sum(per_contact.values()),
            probes_used=dict(sorted(probes.items())),
            ladder=MappingProxyType(dict(sorted(runs.items()))),
            handling_outcome=MappingProxyType(handling),
            sitreps=MappingProxyType(sitreps),
            sitrep_packets=packets,
            watch=MappingProxyType(watch),
            audit_run_id=self.run_id,
            audit_db=self.audit_db,
            audit_head=verify.head,
            audit_ok=verify.ok,
            audit_counts=MappingProxyType(audit_counts),
            ladder_transitions=trans,
            ladder_log_hash=hashlib.sha256(log_text.encode("utf-8")).hexdigest(),
            contact_interruptions=MappingProxyType(dict(sorted(per_contact.items()))),
            contact_cases=MappingProxyType(dict(sorted(self.case_of.items()))),
            planner_stats=MappingProxyType(dict(sorted(stats.items()))),
            degraded_reasons=self.degraded_reasons,
            orders=self.orders,
            sim_extended=self.sim_ext,
            tail_ticks=self.tail_ticks,
            audit=self.audit,
        )


def _as_v3(v2: RunResultV2) -> RunResultV3:
    return RunResultV3(**{f.name: getattr(v2, f.name) for f in dataclasses.fields(RunResultV2)})


def _step_record(r: StepResult, t_s: float) -> StepRecord:
    cid = None
    if r.checkpoint is not None:
        cid = r.checkpoint.contact_id
    elif r.actions:
        cid = r.actions[0].contact_id
    return StepRecord(
        tick=r.event.tick,
        t_s=t_s,
        contact_id=cid,
        from_state=r.from_state,
        to_state=r.to_state,
        event=r.event.kind.value,
        arg=r.event.arg,
        transitioned=r.transitioned,
        reason=r.reason,
        actions=tuple(a.kind.value for a in r.actions),
    )


def _ladder_transitions(
    runs: Mapping[str, LadderRun], case_of: Mapping[str, str], tick_s: float
) -> tuple[LadderTransition, ...]:
    rows: list[tuple[int, str, int, LadderTransition]] = []
    for cid in sorted(runs):
        run = runs[cid]
        by_key = {a.idempotency_key: a for a in run.actions}
        for cp in run.checkpoints:
            acts = [by_key[k] for k in cp.action_keys if k in by_key]
            first = acts[0] if acts else None
            asset = next(
                (a.scope.asset_id for a in acts if a.scope is not None and a.scope.asset_id),
                None,
            )
            rows.append(
                (
                    cp.tick,
                    cid,
                    cp.seq,
                    LadderTransition(
                        seq=0,
                        contact_id=cid,
                        case_id=case_of.get(cid),
                        tick=cp.tick,
                        t_s=round6(cp.tick * tick_s),
                        from_state=cp.from_state,
                        to_state=cp.to_state,
                        event=cp.event.kind.value,
                        arg=cp.event.arg,
                        action=None if first is None else first.kind.value,
                        asset_id=asset,
                        probe_id=None,
                        rung=cp.rung,
                        hash=cp.hash,
                    ),
                )
            )
    rows.sort(key=lambda r: (r[0], r[1], r[2]))
    return tuple(dataclasses.replace(t, seq=i) for i, (_, _, _, t) in enumerate(rows))


class ScenarioRunnerV3(ScenarioRunnerV2):
    """Runner v3. ``run(**kw)`` equals ``start(**kw)`` stepped to the end."""

    def start(
        self,
        *,
        llm_roles: bool = False,
        violations_path: Path = DEFAULT_VIOLATIONS_PATH,
        fixture_store: Any = None,
        responder: ResponderPort | None = None,
        orders_path: str | Path | None = STANDING_ORDERS_PATH,
        unsigned: bool = False,
        approver: ApproverPort | None = None,
        approver_script: ApproverChoice | None = None,
        toggles: Iterable[ToggleName | str] = (),
        db_url: str | Path | None = None,
        writer_mode: WriterModeV3 = "template",
        sitrep_client: Any = None,
        run_id: str | None = None,
    ) -> RunSession:
        return RunSession(
            self,
            llm_roles=llm_roles,
            violations_path=violations_path,
            fixture_store=fixture_store,
            responder=responder,
            orders_path=orders_path,
            unsigned=unsigned,
            approver=approver,
            approver_script=approver_script,
            toggles=toggles,
            db_url=db_url,
            writer_mode=writer_mode,
            sitrep_client=sitrep_client,
            run_id=run_id,
        )

    def run(
        self,
        *,
        llm_roles: bool = False,
        violations_path: Path = DEFAULT_VIOLATIONS_PATH,
        fixture_store: Any = None,
        responder: ResponderPort | None = None,
        orders_path: str | Path | None = STANDING_ORDERS_PATH,
        unsigned: bool = False,
        approver: ApproverPort | None = None,
        approver_script: ApproverChoice | None = None,
        toggles: Iterable[ToggleName | str] = (),
        db_url: str | Path | None = None,
        writer_mode: WriterModeV3 = "template",
        sitrep_client: Any = None,
        run_id: str | None = None,
    ) -> RunResultV3:
        return self.start(
            llm_roles=llm_roles,
            violations_path=violations_path,
            fixture_store=fixture_store,
            responder=responder,
            orders_path=orders_path,
            unsigned=unsigned,
            approver=approver,
            approver_script=approver_script,
            toggles=toggles,
            db_url=db_url,
            writer_mode=writer_mode,
            sitrep_client=sitrep_client,
            run_id=run_id,
        ).run_to_end()


def with_toggles(scenario: Scenario, *names: ToggleName | str) -> Scenario:
    """A copy of ``scenario`` with ``names`` added to its toggles."""
    have = {t.name for t in scenario.toggles}
    extra = tuple(Toggle(name=ToggleName(n)) for n in names if ToggleName(n) not in have)
    return scenario.model_copy(update={"toggles": scenario.toggles + extra})


def ladder_main(argv: Sequence[str] | None = None) -> int:
    """``python -m soundings.runner ladder --scenario S05 --seed 11 [--orders PATH]
    [--unsigned] [--toggle NAME] [--sitrep-out PATH] [--audit-db PATH]``: runner v3, then
    canonical JSON of every ``LadderRun`` (no ``--orders``: the unsigned guard set).
    ``--audit-db`` is WO-042's hash-chained log (replaced if present); ``--sitrep-out``
    writes every delivered SITREP's markdown. ``python -m soundings.ladder`` stays part A's
    synthetic stream: the ladder package may not reach the sim (tests/test_boundary.py)."""
    ap = argparse.ArgumentParser(prog="python -m soundings.runner ladder")
    ap.add_argument("--scenario", default="S05", help="catalog id (S05), smoke, or a path")
    ap.add_argument("--seed", type=int, default=None, help="default: the scenario's seed")
    ap.add_argument("--orders", type=Path, default=None, help="standing orders YAML")
    ap.add_argument("--unsigned", action="store_true", help="ignore the standing orders")
    ap.add_argument("--toggle", action="append", default=[], choices=[t.value for t in ToggleName])
    ap.add_argument("--sitrep-out", type=Path, default=None)
    ap.add_argument("--audit-db", type=Path, default=None)
    args = ap.parse_args(argv)
    if args.audit_db is not None and args.audit_db.exists():
        args.audit_db.unlink()
    runner = ScenarioRunnerV3(args.scenario, seed=args.seed)
    unsigned = args.unsigned or args.orders is None
    result = runner.run(
        orders_path=args.orders, unsigned=unsigned, toggles=args.toggle, db_url=args.audit_db
    )
    runs = result.ladder
    doc = {
        "scenario": runner.scenario.id,
        "seed": runner.seed,
        "orders": "unsigned" if unsigned else "signed_file",
        "toggles": sorted(args.toggle),
        "audit_head": result.audit_head,
        "audit_ok": result.audit_ok,
        "ladder_log_hash": result.ladder_log_hash,
        "runs": [runs[cid].canonical() for cid in sorted(runs)],
    }
    sys.stdout.write(canonical_json(doc) + "\n")
    if args.sitrep_out is not None:
        args.sitrep_out.write_text(
            "\n".join(p.markdown for p in result.sitrep_packets), encoding="utf-8"
        )
    if result.audit is not None:
        result.audit.close()
    return 0


_main_v1 = main


def main(argv: Sequence[str] | None = None) -> int:  # type: ignore[no-redef]
    """``python -m soundings.runner verify [...]`` runs the catalog verifier and
    ``python -m soundings.runner ladder [...]`` runner v3 (WO-043); every other invocation
    is the unchanged runner v1 CLI."""
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] == "verify":
        from soundings.eval.catalog_verify import main as verify_main

        return verify_main(args[1:])
    if args and args[0] == "ladder":
        return ladder_main(args[1:])
    return _main_v1(args)


# --- WO-043 end: runner v3 ---
# --- end v3 (WO-043) ---

# --- sim_likelihoods (WO-051) ---
# --- end sim_likelihoods (WO-051) ---

__all__ = [
    "ASK_TRANSITIONS",
    "CaseResult",
    "HailRecord",
    "IllegalTransition",
    "LostVehicleNotice",
    "NoticeLine",
    "RunResult",
    "RunnerEvent",
    "ScenarioRunner",
    "Transition",
    "check_transition",
    "format_transition",
    "hail_count",
    "main",
    "transition_log_hash_of",
    "transition_log_lines",
    "write_transition_log",
]


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
