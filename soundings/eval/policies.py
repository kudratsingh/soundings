"""Baseline policies and the counting rules every policy is scored by (WO-050).

Three policies run on the same seeded sim output and return the same ``PolicyRun``:

* ``soundings``: the real system, adapted in WO-051 (``soundings.eval.soundings_policy``,
  imported lazily by ``make_policy`` so this module never imports the runner).
* ``bang``: close the berth nearest any unknown contact. The closure (``any_closure``) is
  a person-approved action at the ``hand_to_person`` rung (``config/ladder.yaml``), so
  every closure is logged ``DETECTED -> AWAITING_APPROVAL -> HANDED_OFF``; approval is an
  assumption, labeled in ``config/eval.yaml``.
* ``human_queue``: every contact goes to a person (``DETECTED -> HANDED_OFF``), served by
  one serial queue.

Baselines never go through ``LadderMachine`` (its guards would correctly refuse a closure
on first contact). They are counterfactual logs written with the same ``LadderState``
names and checked against ``BASELINE_EDGES``.

Counting rules, written once here and applied to every policy:

* Contact unit: one ``ContactOutcome`` per observed contact id; sim false alarms are kept
  and flagged. ``outcome_for_gt`` rolls a ground-truth contact up over its observed ids.
* Threat: ground truth in ``THREAT_HYPOTHESES``. Everything else, false alarms included,
  is benign for scoring.
* Missed threat: a threat none of whose observed ids ever entered ``HOLDING`` or
  ``HANDED_OFF``; an undetected threat is missed with ``reason="undetected"``.
* Shutdown: the policy's ``ShutdownInterval`` list (closures for ``bang``, holds for
  ``soundings``, none for ``human_queue``), merged per asset by interval union.
  ``ADVISING_SLOW`` time is ``slow_minutes`` and never shutdown.
* False shutdown: a shutdown interval caused by a benign contact.
* Interruption: ``interruption_kind`` (DECISIONS 2026-10-16 23:05, one definition):
  an approval request, a backup page, a handoff to the watch officer, or the officer
  being asked to look (``request_watch_officer_look`` or a person re-check). Platform
  SITREP updates after the handoff and operator notices do not count.
* Seconds seen to handled: entering a ``HANDLING_TERMINAL_STATES`` state minus first
  detection; ``None`` when never handled inside the run (never imputed).

Determinism: no clock reads and no randomness here; contacts are processed in
(first seen, contact id) order; every float leaves through ``round6``.
"""

from __future__ import annotations

import importlib
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any, Literal, Protocol, runtime_checkable

import yaml
from pydantic import AfterValidator, Field

from soundings.canonical import round6
from soundings.ladder.table import RUNG_OF_STATE
from soundings.schemas import (
    HANDLING_TERMINAL_STATES,
    Harbor,
    Hypothesis,
    LadderConfig,
    LadderState,
    LikelihoodSpec,
    ProbeId,
    Rung,
    Scenario,
    load_harbor,
    load_ladder_config,
)
from soundings.schemas._base import (
    CONFIG_DIR,
    HarborId,
    NonNegInt,
    StrictModel,
    from_file_data,
)
from soundings.schemas.denylist import FreeText
from soundings.schemas.evidence import ContactId
from soundings.sim.hashing import track_hash
from soundings.sim.run import run_scenario
from soundings.sim.types import SimRun

EVAL_CONFIG_PATH: Path = CONFIG_DIR / "eval.yaml"

S = LadderState

Seconds = Annotated[float, Field(ge=0.0), AfterValidator(round6)]
"""A non-negative float that always leaves through ``round6``."""


# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------


class PolicyName(StrEnum):
    """Member order is the column order in every report."""

    SOUNDINGS = "soundings"
    BANG = "bang"
    HUMAN_QUEUE = "human_queue"


POLICIES: tuple[PolicyName, ...] = tuple(PolicyName)

THREAT_HYPOTHESES: frozenset[Hypothesis] = frozenset(
    {Hypothesis.UNAUTHORIZED_UUV, Hypothesis.UNAUTHORIZED_DIVER}
)

RUNG_ORDER: tuple[Rung, ...] = tuple(Rung)
"""watch < ask < slow < hold < hand_to_person (``Rung`` member order)."""

CLOSURE_ACTION = "any_closure"
"""The action ``bang`` asks for; must be in ``boundary.hand_to_person.person_approves``."""

SHUTDOWN_STATES: frozenset[LadderState] = frozenset({S.HOLDING})
"""States whose intervals count as shutdown when a policy logs them (``soundings``)."""


class PolicyEvent(StrEnum):
    """Why a transition happened. The soundings adapter maps runner events onto these."""

    CONTACT_DETECTED = "contact_detected"
    APPROVAL_REQUESTED = "approval_requested"
    APPROVAL_GRANTED = "approval_granted"
    HANDED_TO_PERSON = "handed_to_person"
    COVERED_BY_CLOSURE = "covered_by_closure"
    WATCH_STARTED = "watch_started"
    PERSON_RECHECK = "person_recheck"
    SITREP_UPDATE = "sitrep_update"
    PROBE_CALL = "probe_call"
    BACKUP_PAGED = "backup_paged"
    BERTH_REOPENED = "berth_reopened"
    CONTACT_DEPARTED = "contact_departed"
    OPERATOR_NOTICE = "operator_notice"
    STATE_CHANGE = "state_change"


class InterruptionKind(StrEnum):
    """The four interruption sources of the single definition (DECISIONS 23:05)."""

    APPROVAL_REQUEST = "approval_request"
    BACKUP_PAGE = "backup_page"
    HANDOFF = "handoff"
    EYES_ON = "eyes_on"


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------


class Transition(StrictModel):
    """One logged step for one observed contact. ``from_state`` is None only on the first."""

    seq: NonNegInt
    contact_id: ContactId
    tick: NonNegInt
    t_s: Seconds
    from_state: LadderState | None
    to_state: LadderState
    event: PolicyEvent
    action: HarborId | None = None
    asset_id: HarborId | None = None
    probe_id: ProbeId | None = None


class ShutdownInterval(StrictModel):
    """A stretch of time an asset was closed or held because of one contact.

    ``approval_seq`` is the ``seq`` of the transition that granted a person-approved
    closure (``bang``); it is None for a hold under standing orders (``soundings``).
    """

    contact_id: ContactId
    asset_id: HarborId
    kind: Literal["closure", "hold"]
    start_s: Seconds
    end_s: Seconds
    approval_seq: NonNegInt | None = None

    @property
    def minutes(self) -> float:
        return round6((self.end_s - self.start_s) / 60.0)


class PlannerStats(StrictModel):
    calls: NonNegInt = 0
    fixture_hits: NonNegInt = 0
    guard_rejections: NonNegInt = 0
    fallbacks: NonNegInt = 0


class GtContact(StrictModel):
    """One ground-truth contact of the run. ``truth`` is None for an undetected surge
    contact, whose hypothesis the sim output does not carry."""

    gt_id: str
    truth: Hypothesis | None
    is_false_alarm: bool


class ContactOutcome(StrictModel):
    observed_contact_id: ContactId
    gt_id: str | None
    truth: Hypothesis | None
    is_false_alarm: bool
    first_seen_s: Seconds
    handled_s: Seconds | None
    terminal_state: LadderState
    max_rung: Rung
    interruptions: NonNegInt
    shutdown_minutes: Seconds
    false_shutdown: bool
    probes_used: NonNegInt
    probe_sequence: tuple[ProbeId, ...]


class GtOutcome(StrictModel):
    """A ground-truth contact rolled up over its observed ids (track re-acquisition)."""

    gt_id: str
    truth: Hypothesis | None
    is_false_alarm: bool
    observed_contact_ids: tuple[ContactId, ...]
    detected: bool
    first_seen_s: Seconds | None
    handled_s: Seconds | None
    seconds_seen_to_handled: Seconds | None
    terminal_state: LadderState | None
    max_rung: Rung | None
    missed: bool
    reason: Literal["undetected", "never_held_or_handed_off"] | None
    interruptions: NonNegInt
    shutdown_minutes: Seconds
    false_shutdown: bool
    probes_used: NonNegInt
    probe_sequence: tuple[ProbeId, ...]


class PolicyRun(StrictModel):
    policy: PolicyName
    scenario_id: str
    seed: int
    variant: Literal["nominal", "misspecified"]
    planner: Literal["fallback", "llm", "none"]
    transition_log: tuple[Transition, ...]
    terminal_state: LadderState | None
    """Rolled-up terminal state of the scenario's first ground-truth contact (None when
    that contact was never detected)."""
    rung_histogram: dict[Rung, NonNegInt]
    """Max rung per detected ground-truth contact, sim false alarms excluded."""
    human_interruptions: NonNegInt
    shutdown_minutes: Seconds
    slow_minutes: Seconds
    probes_used: NonNegInt
    seconds_seen_to_handled: Seconds | None
    """Of the scenario's first ground-truth contact."""
    missed_threat: bool
    false_shutdown: bool
    contacts: tuple[ContactOutcome, ...]
    sim_hours: Seconds
    track_hash: str
    planner_stats: PlannerStats | None = None
    shutdowns: tuple[ShutdownInterval, ...] = ()
    gt_contacts: tuple[GtContact, ...] = ()


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


class BaselineConfig(StrictModel):
    human_review_s: Annotated[float, Field(gt=0.0)]
    berth_reopen_after_s: Annotated[float, Field(ge=0.0)]
    assumption_note: FreeText
    assumptions: dict[HarborId, FreeText]


class EvalDefaults(StrictModel):
    seeds: tuple[int, ...]
    perturb_magnitude: Annotated[float, Field(ge=0.0)]
    perturb_seed: NonNegInt


class EvalConfig(StrictModel):
    baselines: BaselineConfig
    eval: EvalDefaults


def load_eval_config(path: str | Path = EVAL_CONFIG_PATH) -> EvalConfig:
    """Load ``config/eval.yaml`` (``baselines:`` assumptions and ``eval:`` CLI defaults)."""
    with Path(path).open(encoding="utf-8") as fh:
        doc = yaml.safe_load(fh)
    base = dict(doc["baselines"])
    for key in ("human_review_s", "berth_reopen_after_s"):
        base[key] = float(base[key])
    ev = dict(doc["eval"])
    ev["perturb_magnitude"] = float(ev["perturb_magnitude"])
    return EvalConfig.model_validate(from_file_data({"baselines": base, "eval": ev}))


@lru_cache(maxsize=1)
def _default_eval_config() -> EvalConfig:
    return load_eval_config()


@lru_cache(maxsize=1)
def _default_ladder() -> LadderConfig:
    return load_ladder_config()


@lru_cache(maxsize=1)
def _default_harbor() -> Harbor:
    return load_harbor()


# ---------------------------------------------------------------------------
# Counting rules (shared by every policy)
# ---------------------------------------------------------------------------


def is_threat(truth: Hypothesis | None) -> bool:
    return truth in THREAT_HYPOTHESES


def interruption_kind(t: Transition) -> InterruptionKind | None:
    """The single interruption definition, applied to one transition.

    One transition is at most one interruption; a policy that pages a backup while
    entering ``AWAITING_APPROVAL`` logs the page as its own transition.
    """
    if t.to_state is S.AWAITING_APPROVAL and t.from_state is not S.AWAITING_APPROVAL:
        return InterruptionKind.APPROVAL_REQUEST
    if t.to_state is S.HANDED_OFF and t.from_state is not S.HANDED_OFF:
        return InterruptionKind.HANDOFF
    if t.event is PolicyEvent.BACKUP_PAGED:
        return InterruptionKind.BACKUP_PAGE
    if t.event is PolicyEvent.BERTH_REOPENED:
        # Reopening a person-approved closure is the person's call (anything after the
        # handoff is person-approved), so it is an approval request.
        return InterruptionKind.APPROVAL_REQUEST
    if t.probe_id is ProbeId.REQUEST_WATCH_OFFICER_LOOK or t.event is PolicyEvent.PERSON_RECHECK:
        return InterruptionKind.EYES_ON
    return None


def count_interruptions(transitions: Iterable[Transition]) -> int:
    """Number of transitions that interrupt the watch officer or the backup approver."""
    return sum(1 for t in transitions if interruption_kind(t) is not None)


def max_rung(transitions: Iterable[Transition]) -> Rung | None:
    """Highest rung reached, over WO-040's ``RUNG_OF_STATE`` (a None rung keeps the
    previous one). None when no transition carries a rung."""
    current: Rung | None = None
    best: Rung | None = None
    for t in transitions:
        rung = RUNG_OF_STATE[t.to_state]
        if rung is not None:
            current = rung
        if current is not None and (best is None or rung_index(current) > rung_index(best)):
            best = current
    return best


def rung_index(rung: Rung) -> int:
    return RUNG_ORDER.index(rung)


def merge_intervals(intervals: Iterable[tuple[float, float]]) -> list[tuple[float, float]]:
    """Interval union: sorted, overlapping or touching intervals joined, empty ones dropped."""
    merged: list[tuple[float, float]] = []
    for start, end in sorted(intervals):
        if end <= start:
            continue
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def shutdown_minutes(shutdowns: Iterable[ShutdownInterval]) -> float:
    """Minutes of shutdown: union per asset (never counted twice), summed over assets."""
    by_asset: dict[str, list[tuple[float, float]]] = {}
    for s in shutdowns:
        by_asset.setdefault(s.asset_id, []).append((s.start_s, s.end_s))
    total = 0.0
    for asset in sorted(by_asset):
        total += sum(e - s for s, e in merge_intervals(by_asset[asset]))
    return round6(total / 60.0)


def state_intervals(
    transitions: Iterable[Transition], states: frozenset[LadderState], end_s: float
) -> dict[str, list[tuple[float, float]]]:
    """Per contact, the intervals spent in any of ``states`` (open ones end at ``end_s``)."""
    out: dict[str, list[tuple[float, float]]] = {}
    entered: dict[str, float] = {}
    for t in transitions:
        inside = t.to_state in states
        was = t.contact_id in entered
        if inside and not was:
            entered[t.contact_id] = t.t_s
        elif was and not inside:
            out.setdefault(t.contact_id, []).append((entered.pop(t.contact_id), t.t_s))
    for cid in sorted(entered):
        out.setdefault(cid, []).append((entered[cid], end_s))
    return out


def slow_minutes(transitions: Iterable[Transition], end_s: float) -> float:
    """Minutes in ``ADVISING_SLOW``, merged across contacts (reported, never shutdown)."""
    spans = state_intervals(transitions, frozenset({S.ADVISING_SLOW}), end_s)
    pooled = [iv for cid in sorted(spans) for iv in spans[cid]]
    return round6(sum(e - s for s, e in merge_intervals(pooled)) / 60.0)


def hold_shutdowns(
    transitions: Sequence[Transition], end_s: float, asset_of: dict[str, str]
) -> list[ShutdownInterval]:
    """``HOLDING`` intervals as shutdowns (the soundings adapter's hook). ``asset_of`` maps
    contact id to the held asset; a missing contact uses the ``asset_id`` on its entry
    transition."""
    entry_asset: dict[str, str] = {}
    for t in transitions:
        if t.to_state is S.HOLDING and t.asset_id is not None:
            entry_asset.setdefault(t.contact_id, t.asset_id)
    out: list[ShutdownInterval] = []
    spans = state_intervals(transitions, SHUTDOWN_STATES, end_s)
    for cid in sorted(spans):
        asset = asset_of.get(cid, entry_asset.get(cid))
        if asset is None:
            raise ValueError(f"hold by {cid} names no asset")
        for start, end in spans[cid]:
            out.append(
                ShutdownInterval(
                    contact_id=cid, asset_id=asset, kind="hold", start_s=start, end_s=end
                )
            )
    return out


# ---------------------------------------------------------------------------
# Sim view: observed contacts with grading labels
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ContactSpan:
    """One observed contact as the baselines see it: first and last detection and the
    first position. ``gt_id``/``truth``/``is_false_alarm`` are for grading only; no
    baseline decision reads them."""

    contact_id: str
    first_seen_s: float
    last_seen_s: float
    lat: float
    lon: float
    gt_id: str | None = None
    truth: Hypothesis | None = None
    is_false_alarm: bool = False


def contact_spans(sim: SimRun) -> tuple[ContactSpan, ...]:
    """Every observed contact, in (first seen, contact id) order."""
    fa_ids = {d.observed_contact_id for d in sim.detections if d.is_false_alarm}
    spans: list[ContactSpan] = []
    for cid in sorted(sim.tracks):
        pts = sim.tracks[cid].points
        truth = sim.contact_truth.get(cid)
        spans.append(
            ContactSpan(
                contact_id=cid,
                first_seen_s=round6(pts[0].tick * sim.tick_s),
                last_seen_s=round6(pts[-1].tick * sim.tick_s),
                lat=pts[0].lat,
                lon=pts[0].lon,
                gt_id=truth.gt_id if truth is not None else None,
                truth=truth.hypothesis if truth is not None else None,
                is_false_alarm=cid in fa_ids,
            )
        )
    spans.sort(key=lambda s: (s.first_seen_s, s.contact_id))
    return tuple(spans)


def gt_contacts(scenario: Scenario, sim: SimRun) -> tuple[GtContact, ...]:
    """Every ground-truth contact of the run (scenario, surge and false-alarm ids)."""
    hyp: dict[str, Hypothesis] = {c.id: scenario.ground_truth for c in scenario.contacts}
    for cid in sorted(sim.contact_truth):
        ct = sim.contact_truth[cid]
        hyp[ct.gt_id] = ct.hypothesis
    fa = {d.gt_id for d in sim.detections if d.is_false_alarm}
    ids = sorted(set(sim.truth_paths) | set(hyp))
    return tuple(
        GtContact(
            gt_id=g,
            truth=hyp.get(g),
            is_false_alarm=g in fa or g.startswith("fa-"),
        )
        for g in ids
    )


# ---------------------------------------------------------------------------
# Log building
# ---------------------------------------------------------------------------

BASELINE_EDGES: frozenset[tuple[LadderState | None, LadderState]] = frozenset(
    {
        (None, S.DETECTED),
        (S.DETECTED, S.AWAITING_APPROVAL),
        (S.AWAITING_APPROVAL, S.HANDED_OFF),
        (S.DETECTED, S.HANDED_OFF),
        (S.DETECTED, S.HOLDING),
        (S.HOLDING, S.HOLDING),
        (S.HOLDING, S.RESOLVED_DEPARTED),
        (S.HANDED_OFF, S.WATCH_AFTER_HANDOFF),
        (S.WATCH_AFTER_HANDOFF, S.WATCH_AFTER_HANDOFF),
        (S.WATCH_AFTER_HANDOFF, S.RESOLVED_DEPARTED),
    }
)
"""Every (from, to) a baseline log may contain. After ``HANDED_OFF`` only watching and
departure exist (CLAUDE.md 2.1)."""


class ForgedTransitionError(ValueError):
    """A baseline tried to log a step outside ``BASELINE_EDGES``."""


@dataclass
class _Pending:
    t_s: float
    order: int
    local: int
    contact_id: str
    from_state: LadderState | None
    to_state: LadderState
    event: PolicyEvent
    action: str | None
    asset_id: str | None


@dataclass
class _LogBuilder:
    """Collects per-contact steps, checks each against ``BASELINE_EDGES`` and the
    contact's current state, then orders them by (time, contact order, step)."""

    end_s: float
    tick_s: float
    order: dict[str, int] = field(default_factory=dict)
    state: dict[str, LadderState] = field(default_factory=dict)
    steps: list[_Pending] = field(default_factory=list)
    counter: dict[str, int] = field(default_factory=dict)

    def add(
        self,
        contact_id: str,
        t_s: float,
        to_state: LadderState,
        event: PolicyEvent,
        *,
        action: str | None = None,
        asset_id: str | None = None,
    ) -> bool:
        """Log one step; returns False (and logs nothing) at or after the end of the run."""
        t_s = round6(t_s)
        if t_s >= self.end_s:
            return False
        frm = self.state.get(contact_id)
        if (frm, to_state) not in BASELINE_EDGES:
            raise ForgedTransitionError(f"{contact_id}: {frm} -> {to_state} is not allowed")
        self.order.setdefault(contact_id, len(self.order))
        local = self.counter.get(contact_id, 0)
        self.counter[contact_id] = local + 1
        self.steps.append(
            _Pending(
                t_s=t_s,
                order=self.order[contact_id],
                local=local,
                contact_id=contact_id,
                from_state=frm,
                to_state=to_state,
                event=event,
                action=action,
                asset_id=asset_id,
            )
        )
        self.state[contact_id] = to_state
        return True

    def build(self) -> tuple[Transition, ...]:
        ordered = sorted(self.steps, key=lambda p: (p.t_s, p.order, p.local))
        return tuple(
            Transition(
                seq=i,
                contact_id=p.contact_id,
                tick=int(p.t_s // self.tick_s),
                t_s=p.t_s,
                from_state=p.from_state,
                to_state=p.to_state,
                event=p.event,
                action=p.action,
                asset_id=p.asset_id,
            )
            for i, p in enumerate(ordered)
        )


def serial_queue(arrivals: Sequence[float], review_s: float) -> list[float]:
    """Completion times for one person serving requests in arrival order."""
    free = 0.0
    done: list[float] = []
    for t in arrivals:
        free = round6(max(t, free) + review_s)
        done.append(free)
    return done


# ---------------------------------------------------------------------------
# Assembling a PolicyRun (shared; WO-051's adapter calls this too)
# ---------------------------------------------------------------------------


def _handled_s(transitions: Sequence[Transition]) -> float | None:
    for t in transitions:
        if t.to_state in HANDLING_TERMINAL_STATES:
            return t.t_s
    return None


def _probe_sequence(transitions: Sequence[Transition]) -> tuple[ProbeId, ...]:
    return tuple(
        t.probe_id
        for t in transitions
        if t.event is PolicyEvent.PROBE_CALL and t.probe_id is not None
    )


def outcome_for_gt(run: PolicyRun, gt_id: str) -> GtOutcome:
    """Roll a ground-truth contact up over its observed ids: max rung over the ids, the
    terminal state of its last id, missed iff a threat none of whose ids ever entered
    ``HOLDING`` or ``HANDED_OFF``."""
    gt = next((g for g in run.gt_contacts if g.gt_id == gt_id), None)
    outcomes = sorted(
        (c for c in run.contacts if c.gt_id == gt_id),
        key=lambda c: (c.first_seen_s, c.observed_contact_id),
    )
    truth = gt.truth if gt is not None else (outcomes[0].truth if outcomes else None)
    fa = gt.is_false_alarm if gt is not None else any(c.is_false_alarm for c in outcomes)
    threat = is_threat(truth)
    if not outcomes:
        return GtOutcome(
            gt_id=gt_id,
            truth=truth,
            is_false_alarm=fa,
            observed_contact_ids=(),
            detected=False,
            first_seen_s=None,
            handled_s=None,
            seconds_seen_to_handled=None,
            terminal_state=None,
            max_rung=None,
            missed=threat,
            reason="undetected" if threat else None,
            interruptions=0,
            shutdown_minutes=0.0,
            false_shutdown=False,
            probes_used=0,
            probe_sequence=(),
        )
    ids = tuple(c.observed_contact_id for c in outcomes)
    idset = set(ids)
    reached = {t.to_state for t in run.transition_log if t.contact_id in idset}
    caught = bool(reached & {S.HOLDING, S.HANDED_OFF})
    missed = threat and not caught
    handled = [c.handled_s for c in outcomes if c.handled_s is not None]
    first_seen = min(c.first_seen_s for c in outcomes)
    handled_s = min(handled) if handled else None
    best = max((c.max_rung for c in outcomes), key=rung_index)
    return GtOutcome(
        gt_id=gt_id,
        truth=truth,
        is_false_alarm=fa,
        observed_contact_ids=ids,
        detected=True,
        first_seen_s=first_seen,
        handled_s=handled_s,
        seconds_seen_to_handled=(round6(handled_s - first_seen) if handled_s is not None else None),
        terminal_state=outcomes[-1].terminal_state,
        max_rung=best,
        missed=missed,
        reason="never_held_or_handed_off" if missed else None,
        interruptions=sum(c.interruptions for c in outcomes),
        shutdown_minutes=shutdown_minutes(s for s in run.shutdowns if s.contact_id in idset),
        false_shutdown=any(c.false_shutdown for c in outcomes),
        probes_used=sum(c.probes_used for c in outcomes),
        probe_sequence=tuple(p for c in outcomes for p in c.probe_sequence),
    )


def assemble_run(
    *,
    policy: PolicyName,
    scenario: Scenario,
    sim: SimRun,
    transitions: Sequence[Transition],
    shutdowns: Sequence[ShutdownInterval],
    variant: Literal["nominal", "misspecified"],
    planner: Literal["fallback", "llm", "none"],
    planner_stats: PlannerStats | None = None,
    slow_min: float | None = None,
) -> PolicyRun:
    """Build a ``PolicyRun`` from a sim run, a transition log and shutdown intervals with
    the shared counting rules (the hook WO-051's soundings adapter calls)."""
    return build_policy_run(
        policy=policy,
        scenario_id=scenario.id,
        seed=sim.seed,
        first_gt_id=scenario.contacts[0].id if scenario.contacts else None,
        spans=contact_spans(sim),
        gts=gt_contacts(scenario, sim),
        transitions=transitions,
        shutdowns=shutdowns,
        end_s=round6(sim.n_ticks * sim.tick_s),
        track_hash=track_hash(sim),
        variant=variant,
        planner=planner,
        planner_stats=planner_stats,
        slow_min=slow_min,
    )


def build_policy_run(
    *,
    policy: PolicyName,
    scenario_id: str,
    seed: int,
    first_gt_id: str | None,
    spans: Sequence[ContactSpan],
    gts: Sequence[GtContact],
    transitions: Sequence[Transition],
    shutdowns: Sequence[ShutdownInterval],
    end_s: float,
    track_hash: str,
    variant: Literal["nominal", "misspecified"],
    planner: Literal["fallback", "llm", "none"],
    planner_stats: PlannerStats | None = None,
    slow_min: float | None = None,
) -> PolicyRun:
    """The counting rules over plain inputs (no sim needed; unit tests use this).
    ``slow_min=None`` derives slow minutes from ``ADVISING_SLOW`` intervals."""
    by_contact: dict[str, list[Transition]] = {}
    for t in transitions:
        by_contact.setdefault(t.contact_id, []).append(t)
    sd_by_contact: dict[str, list[ShutdownInterval]] = {}
    for s in shutdowns:
        sd_by_contact.setdefault(s.contact_id, []).append(s)

    contacts: list[ContactOutcome] = []
    for sp in spans:
        ts = by_contact.get(sp.contact_id, [])
        own = sd_by_contact.get(sp.contact_id, [])
        minutes = shutdown_minutes(own)
        probes = _probe_sequence(ts)
        contacts.append(
            ContactOutcome(
                observed_contact_id=sp.contact_id,
                gt_id=sp.gt_id,
                truth=sp.truth,
                is_false_alarm=sp.is_false_alarm,
                first_seen_s=sp.first_seen_s,
                handled_s=_handled_s(ts),
                terminal_state=ts[-1].to_state if ts else S.DETECTED,
                max_rung=max_rung(ts) or Rung.WATCH,
                interruptions=count_interruptions(ts),
                shutdown_minutes=minutes,
                false_shutdown=minutes > 0.0 and not is_threat(sp.truth),
                probes_used=len(probes),
                probe_sequence=probes,
            )
        )

    draft = PolicyRun(
        policy=policy,
        scenario_id=scenario_id,
        seed=seed,
        variant=variant,
        planner=planner,
        transition_log=tuple(transitions),
        terminal_state=None,
        rung_histogram={r: 0 for r in Rung},
        human_interruptions=count_interruptions(transitions),
        shutdown_minutes=shutdown_minutes(shutdowns),
        slow_minutes=slow_minutes(transitions, end_s) if slow_min is None else slow_min,
        probes_used=len(_probe_sequence(transitions)),
        seconds_seen_to_handled=None,
        missed_threat=False,
        false_shutdown=any(c.false_shutdown for c in contacts),
        contacts=tuple(contacts),
        sim_hours=round6(end_s / 3600.0),
        track_hash=track_hash,
        planner_stats=planner_stats,
        shutdowns=tuple(shutdowns),
        gt_contacts=tuple(gts),
    )
    rolled = {g.gt_id: outcome_for_gt(draft, g.gt_id) for g in gts}
    hist = {r: 0 for r in Rung}
    for g in gts:
        o = rolled[g.gt_id]
        if o.detected and not o.is_false_alarm and o.max_rung is not None:
            hist[o.max_rung] += 1
    first = rolled.get(first_gt_id) if first_gt_id is not None else None
    return draft.model_copy(
        update={
            "terminal_state": first.terminal_state if first is not None else None,
            "seconds_seen_to_handled": first.seconds_seen_to_handled if first else None,
            "rung_histogram": hist,
            "missed_threat": any(o.missed for o in rolled.values()),
        }
    )


# ---------------------------------------------------------------------------
# Policies
# ---------------------------------------------------------------------------


@runtime_checkable
class Policy(Protocol):
    name: PolicyName

    def run(
        self, scenario: Scenario, seed: int, *, sim_likelihoods: LikelihoodSpec | None = None
    ) -> PolicyRun: ...


def _variant(sim_likelihoods: LikelihoodSpec | None) -> Literal["nominal", "misspecified"]:
    return "nominal" if sim_likelihoods is None else "misspecified"


@dataclass
class _Episode:
    """One closure of one berth: its approving request and the contacts it covers."""

    berth: str
    approver: str
    approval_s: float
    open_until: float
    members: list[tuple[str, float, float]]  # (contact, start, unclipped end)


class BangPolicy:
    """Close the berth nearest any unknown contact, with a person's (assumed) approval."""

    name: PolicyName = PolicyName.BANG

    def __init__(
        self,
        cfg: EvalConfig | None = None,
        *,
        ladder: LadderConfig | None = None,
        harbor: Harbor | None = None,
    ) -> None:
        self.cfg = cfg if cfg is not None else _default_eval_config()
        self.ladder = ladder if ladder is not None else _default_ladder()
        self.harbor = harbor if harbor is not None else _default_harbor()
        approves = self.ladder.boundary[Rung.HAND_TO_PERSON].person_approves
        if CLOSURE_ACTION not in approves:
            raise ValueError(f"{CLOSURE_ACTION} is not person-approved in config/ladder.yaml")
        self.action = CLOSURE_ACTION
        berths = tuple(a for a in self.harbor.assets if a.asset_kind == "berth")
        if not berths:
            raise ValueError("bang needs at least one berth in the harbor map")
        self._berth_map = self.harbor.model_copy(update={"assets": berths})

    def nearest_berth(self, lat: float, lon: float) -> str:
        asset = self._berth_map.nearest_asset(lat, lon)
        assert asset is not None  # at least one berth, min_criticality 0
        return asset.asset_id

    def plan(
        self, spans: Sequence[ContactSpan], *, end_s: float, tick_s: float
    ) -> tuple[tuple[Transition, ...], tuple[ShutdownInterval, ...]]:
        """The counterfactual log and closures for these contacts (pure)."""
        review = self.cfg.baselines.human_review_s
        reopen = self.cfg.baselines.berth_reopen_after_s
        log = _LogBuilder(end_s=end_s, tick_s=tick_s)
        queue_free = 0.0

        open_eps: dict[str, _Episode] = {}
        episodes: list[_Episode] = []
        for sp in spans:
            berth = self.nearest_berth(sp.lat, sp.lon)
            log.add(sp.contact_id, sp.first_seen_s, S.DETECTED, PolicyEvent.CONTACT_DETECTED)
            ep = open_eps.get(berth)
            if ep is not None and ep.open_until > sp.first_seen_s:
                # The berth already has a pending or active closure: no new request.
                start = round6(max(sp.first_seen_s, ep.approval_s))
                end = round6(max(start, sp.last_seen_s + reopen))
                if log.add(
                    sp.contact_id,
                    start,
                    S.HOLDING,
                    PolicyEvent.COVERED_BY_CLOSURE,
                    asset_id=berth,
                ):
                    ep.members.append((sp.contact_id, start, end))
                ep.open_until = max(ep.open_until, end)
                continue
            log.add(
                sp.contact_id,
                sp.first_seen_s,
                S.AWAITING_APPROVAL,
                PolicyEvent.APPROVAL_REQUESTED,
                action=self.action,
                asset_id=berth,
            )
            approval = round6(max(sp.first_seen_s, queue_free) + review)
            queue_free = approval
            end = round6(max(approval, sp.last_seen_s + reopen))
            ep = _Episode(berth, sp.contact_id, approval, end, [])
            open_eps[berth] = ep
            episodes.append(ep)
            if log.add(
                sp.contact_id,
                approval,
                S.HANDED_OFF,
                PolicyEvent.APPROVAL_GRANTED,
                action=self.action,
                asset_id=berth,
            ):
                log.add(sp.contact_id, approval, S.WATCH_AFTER_HANDOFF, PolicyEvent.WATCH_STARTED)
                ep.members.append((sp.contact_id, approval, end))

        # Reopen once per closure episode, then each contact departs at its own end.
        for ep in episodes:
            if not ep.members:
                continue
            ep_end = max(m[2] for m in ep.members)
            last = min((m for m in ep.members if m[2] == ep_end), key=lambda m: m[0])
            current = log.state[last[0]]
            log.add(last[0], ep_end, current, PolicyEvent.BERTH_REOPENED, asset_id=ep.berth)
            for cid, _start, end in sorted(ep.members, key=lambda m: m[0]):
                log.add(cid, end, S.RESOLVED_DEPARTED, PolicyEvent.CONTACT_DEPARTED)
        transitions = log.build()

        approval_seq = {
            t.contact_id: t.seq
            for t in transitions
            if t.from_state is S.AWAITING_APPROVAL and t.to_state is S.HANDED_OFF
        }
        shutdowns: list[ShutdownInterval] = []
        for ep in episodes:
            for cid, start, end in ep.members:
                clipped = min(end, end_s)
                if clipped <= start:
                    continue
                shutdowns.append(
                    ShutdownInterval(
                        contact_id=cid,
                        asset_id=ep.berth,
                        kind="closure",
                        start_s=start,
                        end_s=clipped,
                        approval_seq=approval_seq[ep.approver],
                    )
                )
        shutdowns.sort(key=lambda s: (s.start_s, s.contact_id))
        return transitions, tuple(shutdowns)

    def run(
        self, scenario: Scenario, seed: int, *, sim_likelihoods: LikelihoodSpec | None = None
    ) -> PolicyRun:
        sim = run_scenario(scenario, seed=seed, likelihoods=sim_likelihoods)
        end_s = round6(sim.n_ticks * sim.tick_s)
        transitions, shutdowns = self.plan(contact_spans(sim), end_s=end_s, tick_s=sim.tick_s)
        return assemble_run(
            policy=self.name,
            scenario=scenario,
            sim=sim,
            transitions=transitions,
            shutdowns=shutdowns,
            variant=_variant(sim_likelihoods),
            planner="none",
            slow_min=0.0,
        )


class HumanQueuePolicy:
    """Every contact goes to a person; one person, one serial queue. No probes and no
    shutdown (the person's decisions are not simulated; labeled in config/eval.yaml)."""

    name: PolicyName = PolicyName.HUMAN_QUEUE

    def __init__(self, cfg: EvalConfig | None = None, *, ladder: LadderConfig | None = None):
        self.cfg = cfg if cfg is not None else _default_eval_config()
        self.ladder = ladder if ladder is not None else _default_ladder()

    def plan(
        self, spans: Sequence[ContactSpan], *, end_s: float, tick_s: float
    ) -> tuple[Transition, ...]:
        """The counterfactual log for these contacts (pure)."""
        review = self.cfg.baselines.human_review_s
        interval = self.ladder.watch_after_handoff["sitrep_update_interval_s"]
        departed = self.ladder.watch_after_handoff["departed_after_s_outside_zone"]
        log = _LogBuilder(end_s=end_s, tick_s=tick_s)
        handoffs = serial_queue([sp.first_seen_s for sp in spans], review)
        for sp, handoff in zip(spans, handoffs, strict=True):
            log.add(sp.contact_id, sp.first_seen_s, S.DETECTED, PolicyEvent.CONTACT_DETECTED)
            if not log.add(sp.contact_id, handoff, S.HANDED_OFF, PolicyEvent.HANDED_TO_PERSON):
                continue
            log.add(sp.contact_id, handoff, S.WATCH_AFTER_HANDOFF, PolicyEvent.WATCH_STARTED)
            k = 1
            while handoff + k * interval <= sp.last_seen_s:
                t = handoff + k * interval
                if not log.add(sp.contact_id, t, S.WATCH_AFTER_HANDOFF, PolicyEvent.PERSON_RECHECK):
                    break
                k += 1
            gone = max(handoff, sp.last_seen_s + departed)
            log.add(sp.contact_id, gone, S.RESOLVED_DEPARTED, PolicyEvent.CONTACT_DEPARTED)
        return log.build()

    def run(
        self, scenario: Scenario, seed: int, *, sim_likelihoods: LikelihoodSpec | None = None
    ) -> PolicyRun:
        sim = run_scenario(scenario, seed=seed, likelihoods=sim_likelihoods)
        end_s = round6(sim.n_ticks * sim.tick_s)
        transitions = self.plan(contact_spans(sim), end_s=end_s, tick_s=sim.tick_s)
        return assemble_run(
            policy=self.name,
            scenario=scenario,
            sim=sim,
            transitions=transitions,
            shutdowns=(),
            variant=_variant(sim_likelihoods),
            planner="none",
            slow_min=0.0,
        )


SOUNDINGS_POLICY_MODULE = "soundings.eval.soundings_policy"


def make_policy(name: str | PolicyName, **kw: Any) -> Policy:
    """Build a policy by name. ``soundings`` resolves lazily to WO-051's ``SoundingsPolicy``
    so this module never imports the runner."""
    pname = PolicyName(name)
    if pname is PolicyName.BANG:
        return BangPolicy(**kw)
    if pname is PolicyName.HUMAN_QUEUE:
        return HumanQueuePolicy(**kw)
    module = importlib.import_module(SOUNDINGS_POLICY_MODULE)
    policy: Policy = module.SoundingsPolicy(**kw)
    return policy
