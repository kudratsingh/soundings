"""Separation advisories: CPA/TCPA between surface vessels and in-water filed jobs (WO-083).

Every tick, each AIS-reporting vessel is paired with each in-water filed job (a case
cleared at ask whose plan is in window and whose track is fresh). When the closest point
of approach is under ``separation_m`` and the time to it lies strictly inside
``(0, advisory_window_s)``, Soundings proposes a ``slow`` advisory (``advisory_one_basin``)
for the job's basin. The proposal goes through the standing-orders guard exactly like any
other slow (``BoundaryGuards.separation_decision``): it is issued only under signed,
unexpired orders whose slow scope names that basin, and blocked otherwise. Every decision
is written to the audit log with the geometry it rests on (CLAUDE.md 2.6).

The advisory tells a vessel operator about a nearby job; the operator accepts or ignores
it. Soundings plans no route and gives no vessel any instruction. This module adds no
ladder state, changes no case, and a blocked advisory never counts as an interruption
(DECISIONS 2026-10-17 06:37).

Vessel positions come only from ``SimRun.ais_tracks`` (WO-080), never from ground truth.
Deterministic: sorted iteration, run-clock time (``tick * tick_s``), no randomness, no
wall clock, 6-dp rounding at the output boundary (CLAUDE.md 2.4).
"""

from __future__ import annotations

import bisect
import importlib
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Literal, Protocol

import numpy as np
import yaml
from pydantic import Field

from soundings.canonical import round6
from soundings.investigation.features import last_fitted_state, track_arrays
from soundings.investigation.velocity import TIME_EPS_S, TrackArrays
from soundings.ladder.audit import ActionPayload, AuditKind, AuditLog, guard_decision_payload
from soundings.ladder.guards import BoundaryGuards, SeparationFacts
from soundings.ladder.types import ActionScope, GuardDecision
from soundings.matcher import in_window
from soundings.registry.models import PlanView
from soundings.schemas._base import (
    CONFIG_DIR,
    HarborId,
    NonNegFloat,
    NonNegInt,
    PositiveFloat,
    RefId,
    StrictModel,
)
from soundings.schemas.enums import LadderState, Rung
from soundings.schemas.geo import from_local_m, to_local_m
from soundings.schemas.harbor import Harbor
from soundings.sim.detection import load_detection_params

from .cpa import cpa_tcpa, velocity_from_course

ADVISORIES_PATH = CONFIG_DIR / "advisories.yaml"
STALE_REPORT_INTERVALS = 2.0
"""A vessel report older than this many report intervals is stale and skipped."""
DEFAULT_REPORT_INTERVAL_S = 10.0
"""Used only when ``config/sim.yaml`` has no ``ais:`` block (WO-080's stand-in default)."""
KIN_WINDOW_S = 120.0
"""``last_fitted_state`` fits over the last 120 s (WO-012 ``KIN_WINDOW_S``)."""
ADVISORY_ACTION = "advisory_one_basin"

Status = Literal["issued", "blocked"]
VelocitySource = Literal["window", "kalman"]


class AdvisoryConfig(StrictModel):
    """``config/advisories.yaml``: the two thresholds (stress-test values)."""

    separation_m: PositiveFloat
    advisory_window_s: PositiveFloat


def load_advisory_config(path: str | Path = ADVISORIES_PATH) -> AdvisoryConfig:
    """Read and validate ``config/advisories.yaml`` (extra keys are rejected)."""
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    return AdvisoryConfig.model_validate(data)


# --- fixes --------------------------------------------------------------------------------


class AisReportLike(Protocol):
    """The fields of WO-080's ``soundings.sim.types.AisReport`` this module reads."""

    @property
    def tick(self) -> int: ...
    @property
    def t_s(self) -> float: ...
    @property
    def lat(self) -> float: ...
    @property
    def lon(self) -> float: ...
    @property
    def sog_mps(self) -> float: ...
    @property
    def cog_deg(self) -> float: ...


class VesselFix(StrictModel):
    """A vessel at ``tick``: its latest AIS report dead-reckoned to ``tick * tick_s``."""

    vessel_id: RefId
    tick: NonNegInt
    lat: float
    lon: float
    speed_mps: NonNegFloat
    heading_deg: float

    def position_m(self) -> tuple[float, float]:
        x, y = to_local_m(self.lat, self.lon)
        return round6(x), round6(y)

    def velocity_mps(self) -> tuple[float, float]:
        vx, vy = velocity_from_course(self.speed_mps, self.heading_deg)
        return round6(vx), round6(vy)


class JobFix(StrictModel):
    """An in-water filed job at ``tick``: fitted position (dead-reckoned) and velocity."""

    plan_id: RefId
    contact_id: RefId
    basin: HarborId
    tick: NonNegInt
    x_m: float
    y_m: float
    vx: float
    vy: float
    velocity_source: VelocitySource = "window"


def report_interval_s() -> float:
    """``ais.report_interval_s`` from ``config/sim.yaml`` (WO-080), else the 10 s default."""
    try:
        ais_mod: Any = importlib.import_module("soundings.sim.ais")
    except ImportError:  # WO-080 part A not merged: its documented default
        return DEFAULT_REPORT_INTERVAL_S
    return float(ais_mod.load_ais_config().report_interval_s)


def _ais_tracks(sim: Any) -> Mapping[str, Sequence[AisReportLike]]:
    tracks: Mapping[str, Sequence[AisReportLike]] = getattr(sim, "ais_tracks", None) or {}
    return tracks


def vessel_fixes(sim: Any, tick: int, *, interval_s: float | None = None) -> list[VesselFix]:
    """Every vessel with a fresh report at ``tick``, sorted by ``vessel_id``.

    Reads ``sim.ais_tracks`` only (never ``truth_paths``): the latest report at or before
    ``tick``, dead-reckoned with its ``sog_mps``/``cog_deg`` to ``tick * sim.tick_s``, and
    skipped when older than ``STALE_REPORT_INTERVALS x report_interval_s``.
    """
    interval = report_interval_s() if interval_s is None else interval_s
    stale_s = STALE_REPORT_INTERVALS * interval
    now_s = tick * float(sim.tick_s)
    out: list[VesselFix] = []
    tracks = _ais_tracks(sim)
    for vessel_id in sorted(tracks):
        reports = tracks[vessel_id]
        i = bisect.bisect_right([r.tick for r in reports], tick)
        if i == 0:
            continue
        rep = reports[i - 1]
        age = now_s - float(rep.t_s)
        if age > stale_s + TIME_EPS_S:
            continue
        x, y = to_local_m(rep.lat, rep.lon)
        vx, vy = velocity_from_course(rep.sog_mps, rep.cog_deg)
        lat, lon = from_local_m(x + vx * age, y + vy * age)
        out.append(
            VesselFix(
                vessel_id=vessel_id,
                tick=tick,
                lat=lat,
                lon=lon,
                speed_mps=round6(rep.sog_mps),
                heading_deg=round6(rep.cog_deg),
            )
        )
    return out


class JobTracks:
    """Feature-ready track columns per observed contact, built once per run."""

    def __init__(self, result: Any) -> None:
        self.tick_s = float(result.sim.tick_s)
        self._tracks = result.sim.tracks
        self._arrays: dict[str, TrackArrays] = {}
        self._ticks: dict[str, list[int]] = {}

    def ticks(self, contact_id: str) -> list[int]:
        if contact_id not in self._ticks:
            track = self._tracks.get(contact_id)
            self._ticks[contact_id] = [] if track is None else [p.tick for p in track.points]
        return self._ticks[contact_id]

    def arrays(self, contact_id: str) -> TrackArrays:
        if contact_id not in self._arrays:
            self._arrays[contact_id] = track_arrays(self._tracks[contact_id])
        return self._arrays[contact_id]

    def state_at(self, contact_id: str, tick: int) -> tuple[float, float, float, float] | None:
        """``last_fitted_state`` over the track prefix up to ``tick`` (its last 120 s)."""
        end = bisect.bisect_right(self.ticks(contact_id), tick)
        if end < 2:
            return None
        arr = self.arrays(contact_id)
        t_last = float(arr.t_s[end - 1])
        start = int(np.searchsorted(arr.t_s, t_last - KIN_WINDOW_S - TIME_EPS_S, side="right"))
        return last_fitted_state(arr.select(slice(start, end)))


def in_water_jobs(
    result: Any,
    plans: Sequence[PlanView],
    harbor: Harbor,
    tick: int,
    timeout_ticks: int,
    *,
    tracks: JobTracks | None = None,
) -> list[JobFix]:
    """The filed jobs in the water at ``tick``, sorted by ``plan_id``.

    A job qualifies when its plan is ``in_window`` at ``tick``; a ``CaseResult`` is
    CLEARED_AT_ASK against that plan with ``closed_tick <= tick``; the contact has a
    reading in ``(tick - timeout_ticks, tick]``; and ``last_fitted_state`` over the track
    prefix up to ``tick`` is not ``None``. The position is dead-reckoned from the last
    reading to ``tick * tick_s`` so vessel and job sit at the same instant. When two cleared
    cases of one lineage name the same plan, the one with the latest reading is kept (ties:
    the lower ``contact_id``). The basin is ``harbor.zone(plan.area).basin``.
    """
    jt = tracks or JobTracks(result)
    by_plan = {p.plan_id: p for p in plans}
    best: dict[str, tuple[int, str, JobFix]] = {}
    for case in sorted(result.cases, key=lambda c: c.case_id):
        if case.state is not LadderState.CLEARED_AT_ASK or case.outcome is None:
            continue
        plan_id = case.outcome.plan_id
        if plan_id is None or plan_id not in by_plan:
            continue
        if case.closed_tick is None or case.closed_tick > tick:
            continue
        plan = by_plan[plan_id]
        if not in_window(plan, tick):
            continue
        ticks = jt.ticks(case.contact_id)
        i = bisect.bisect_right(ticks, tick)
        if i == 0 or ticks[i - 1] <= tick - timeout_ticks:
            continue
        state = jt.state_at(case.contact_id, tick)
        if state is None:
            continue
        x, y, vx, vy = state
        dt_s = tick * jt.tick_s - ticks[i - 1] * jt.tick_s
        fix = JobFix(
            plan_id=plan_id,
            contact_id=case.contact_id,
            basin=harbor.zone(plan.area).basin,
            tick=tick,
            x_m=round6(x + vx * dt_s),
            y_m=round6(y + vy * dt_s),
            vx=round6(vx),
            vy=round6(vy),
        )
        last = ticks[i - 1]
        held = best.get(plan_id)
        if held is None or (last, held[1]) > (held[0], case.contact_id):
            best[plan_id] = (last, case.contact_id, fix)
    return [best[pid][2] for pid in sorted(best)]


# --- the check ----------------------------------------------------------------------------


class SeparationAdvisory(StrictModel):
    """One vessel-job pair inside both thresholds at ``tick``: a proposed slow advisory."""

    basin: HarborId
    vessel_id: RefId
    plan_id: RefId
    contact_id: RefId
    tick: NonNegInt
    cpa_m: NonNegFloat
    tcpa_s: float
    inputs: SeparationFacts

    @property
    def key(self) -> tuple[str, str]:
        return self.vessel_id, self.plan_id


def separation_facts(vessel: VesselFix, job: JobFix, cfg: AdvisoryConfig) -> SeparationFacts:
    """The CPA/TCPA facts for one pair: computed from exactly the rounded recorded inputs."""
    own_pos = vessel.position_m()
    own_vel = vessel.velocity_mps()
    other_pos = (round6(job.x_m), round6(job.y_m))
    other_vel = (round6(job.vx), round6(job.vy))
    res = cpa_tcpa(own_pos, own_vel, other_pos, other_vel)
    return SeparationFacts(
        vessel_id=vessel.vessel_id,
        plan_id=job.plan_id,
        own_x_m=own_pos[0],
        own_y_m=own_pos[1],
        own_vx_mps=own_vel[0],
        own_vy_mps=own_vel[1],
        other_x_m=other_pos[0],
        other_y_m=other_pos[1],
        other_vx_mps=other_vel[0],
        other_vy_mps=other_vel[1],
        velocity_source=job.velocity_source,
        cpa_m=res.cpa_m,
        tcpa_s=res.tcpa_s,
        separation_m=cfg.separation_m,
        advisory_window_s=cfg.advisory_window_s,
    )


def inside_thresholds(cpa_m: float, tcpa_s: float, cfg: AdvisoryConfig) -> bool:
    """``cpa_m < separation_m`` and ``0 < tcpa_s < advisory_window_s`` (strict edges)."""
    return cpa_m < cfg.separation_m and 0.0 < tcpa_s < cfg.advisory_window_s


def separation_check(
    vessels: Sequence[VesselFix], jobs: Sequence[JobFix], cfg: AdvisoryConfig
) -> list[SeparationAdvisory]:
    """Pure: every (vessel, job) pair inside both thresholds, in ``(vessel_id, plan_id)``
    order."""
    out: list[SeparationAdvisory] = []
    for v in sorted(vessels, key=lambda f: f.vessel_id):
        for j in sorted(jobs, key=lambda f: f.plan_id):
            if v.tick != j.tick:
                raise ValueError(
                    f"fixes at different ticks: {v.vessel_id} {v.tick}, {j.plan_id} {j.tick}"
                )
            facts = separation_facts(v, j, cfg)
            if not inside_thresholds(facts.cpa_m, facts.tcpa_s, cfg):
                continue
            out.append(
                SeparationAdvisory(
                    basin=j.basin,
                    vessel_id=v.vessel_id,
                    plan_id=j.plan_id,
                    contact_id=j.contact_id,
                    tick=j.tick,
                    cpa_m=facts.cpa_m,
                    tcpa_s=facts.tcpa_s,
                    inputs=facts,
                )
            )
    return out


# --- through the guard --------------------------------------------------------------------

OUT_OF_SCOPE_REASONS = frozenset({"basin_out_of_standing_scope", "slow_not_in_standing_scope"})
LABEL_OUT_OF_SCOPE = "advisory blocked: out of standing-orders scope"
LABEL_UNSIGNED = "advisory blocked: standing orders unsigned or expired"
LABEL_OTHER_BLOCK = "advisory blocked: outside the slow advisory criteria"


class AdvisoryDecision(StrictModel):
    """The guard's verdict on one advisory. ``blocked`` never counts as an interruption."""

    advisory: SeparationAdvisory
    decision: GuardDecision
    status: Status
    granted_scope: ActionScope | None = None
    audit_seq: NonNegInt | None = None

    @property
    def label(self) -> str:
        """The one line a UI shows: "slow advisory proposed ..." or "advisory blocked: ..."."""
        if self.status == "issued":
            return f"slow advisory proposed for basin {self.advisory.basin}"
        reason = self.decision.reason
        if reason in OUT_OF_SCOPE_REASONS or reason.startswith("scope_exceeded:"):
            return LABEL_OUT_OF_SCOPE
        if reason == "standing_orders_unsigned":
            return LABEL_UNSIGNED
        return LABEL_OTHER_BLOCK


class SeparationReport(StrictModel):
    """Every advisory decision of one run, in tick then ``(vessel_id, plan_id)`` order."""

    run_id: RefId
    scenario_id: str
    seed: int
    ticks_checked: NonNegInt
    decisions: tuple[AdvisoryDecision, ...] = ()
    issued: NonNegInt = 0
    blocked: NonNegInt = 0
    audit_head: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class SeparationAdvisor:
    """Decides each new encounter once through the standing-orders guard and audits it.

    An encounter is a ``(vessel_id, plan_id)`` pair inside both thresholds that was not
    inside them at the previous step; while it stays inside it is not re-decided. Once it
    opens and closes again it is a new encounter. ``audit`` may be ``None`` (no rows).
    """

    def __init__(
        self,
        guards: BoundaryGuards,
        audit: AuditLog | None,
        run_id: str,
        cfg: AdvisoryConfig,
    ) -> None:
        self.guards = guards
        self.audit = audit
        self.run_id = run_id
        self.cfg = cfg
        self._open: set[tuple[str, str]] = set()

    def step(
        self,
        tick: int,
        tick_s: float,
        vessels: Sequence[VesselFix],
        jobs: Sequence[JobFix],
    ) -> list[AdvisoryDecision]:
        advisories = separation_check(vessels, jobs, self.cfg)
        now_s = round6(tick * tick_s)
        out: list[AdvisoryDecision] = []
        for adv in advisories:
            if adv.key in self._open:
                continue
            out.append(self._decide(adv, now_s))
        self._open = {a.key for a in advisories}
        return out

    def _decide(self, adv: SeparationAdvisory, now_s: float) -> AdvisoryDecision:
        decision, inputs = self.guards.separation_decision(
            adv.inputs, contact_id=adv.contact_id, basin=adv.basin, now_s=now_s
        )
        seq: int | None = None
        if self.audit is not None:
            row = self.audit.append(
                AuditKind.GUARD_DECISION,
                guard_decision_payload(
                    decision, {"guard_inputs": inputs, "separation": adv.inputs}
                ),
                run_id=self.run_id,
                t_s=now_s,
                contact_id=adv.contact_id,
            )
            seq = row.seq
            if decision.permits:
                self.audit.append(
                    AuditKind.ACTION,
                    ActionPayload(
                        action=ADVISORY_ACTION,
                        idempotency_key=f"sep:{adv.vessel_id}:{adv.plan_id}:{adv.tick}",
                        rung=Rung.SLOW,
                    ),
                    run_id=self.run_id,
                    t_s=now_s,
                    contact_id=adv.contact_id,
                )
        return AdvisoryDecision(
            advisory=adv,
            decision=decision,
            status="issued" if decision.permits else "blocked",
            granted_scope=decision.granted_scope,
            audit_seq=seq,
        )


def advise_run(
    result: Any,
    plans: Sequence[PlanView],
    harbor: Harbor,
    guards: BoundaryGuards,
    audit: AuditLog | None,
    run_id: str,
    cfg: AdvisoryConfig,
    *,
    interval_s: float | None = None,
    timeout_ticks: int | None = None,
) -> SeparationReport:
    """Step every tick of ``result.sim`` in order and decide each new encounter.

    Read-only over ``result``: no case, transition or interruption count changes.
    """
    sim = result.sim
    interval = report_interval_s() if interval_s is None else interval_s
    timeout = (
        load_detection_params().track_timeout_ticks if timeout_ticks is None else timeout_ticks
    )
    advisor = SeparationAdvisor(guards, audit, run_id, cfg)
    tracks = JobTracks(result)
    decisions: list[AdvisoryDecision] = []
    tick_s = float(sim.tick_s)
    for tick in range(int(sim.n_ticks)):
        vessels = vessel_fixes(sim, tick, interval_s=interval)
        jobs = in_water_jobs(result, plans, harbor, tick, timeout, tracks=tracks) if vessels else []
        decisions.extend(advisor.step(tick, tick_s, vessels, jobs))
    issued = sum(1 for d in decisions if d.status == "issued")
    return SeparationReport(
        run_id=run_id,
        scenario_id=str(result.scenario_id),
        seed=int(result.seed),
        ticks_checked=int(sim.n_ticks),
        decisions=tuple(decisions),
        issued=issued,
        blocked=len(decisions) - issued,
        audit_head=None if audit is None else audit.head(),
    )


__all__ = [
    "ADVISORIES_PATH",
    "ADVISORY_ACTION",
    "LABEL_OUT_OF_SCOPE",
    "AdvisoryConfig",
    "AdvisoryDecision",
    "JobFix",
    "JobTracks",
    "SeparationAdvisor",
    "SeparationAdvisory",
    "SeparationReport",
    "VesselFix",
    "advise_run",
    "in_water_jobs",
    "inside_thresholds",
    "load_advisory_config",
    "report_interval_s",
    "separation_check",
    "separation_facts",
    "vessel_fixes",
]
