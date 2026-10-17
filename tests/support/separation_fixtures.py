"""Test helpers for WO-083: fixes from local metres, hand-built runs, signed/unsigned orders."""

from __future__ import annotations

import datetime as dt
import math
import shutil
from pathlib import Path

from soundings.advisories.separation import JobFix, VesselFix
from soundings.investigation.evidence import EvidenceTable
from soundings.ladder.boundary import load_boundary
from soundings.ladder.guards import ApproverStatus, BoundaryGuards
from soundings.ladder.standing_orders import StandingOrdersStatus, load_verified, scenario_now
from soundings.matcher import MatchCode, MatchOutcome
from soundings.registry.models import PlanSource, PlanStatus, PlanView
from soundings.runner import CaseResult, RunResult
from soundings.schemas import LadderState
from soundings.schemas.config import STANDING_ORDERS_PATH
from soundings.schemas.geo import from_local_m
from soundings.schemas.track import Track, TrackPoint
from soundings.sim.toggles import ToggleReport
from soundings.sim.types import SimRun
from tests.support.guard_streams import signed_orders_copy

HOUR = 7
"""Scenario hour used for the orders' validity window (BT-01 runs from 07:00)."""
ROW = "0" * 16
T0 = dt.datetime(2026, 10, 17, 7, 0, tzinfo=dt.timezone(dt.timedelta(hours=-7)))


def vessel(vid: str, tick: int, pos: tuple[float, float], vel: tuple[float, float]) -> VesselFix:
    lat, lon = from_local_m(*pos)
    heading = math.degrees(math.atan2(vel[0], vel[1])) % 360.0
    return VesselFix(
        vessel_id=vid,
        tick=tick,
        lat=lat,
        lon=lon,
        speed_mps=math.hypot(*vel),
        heading_deg=heading,
    )


def job(
    pid: str,
    tick: int,
    pos: tuple[float, float],
    vel: tuple[float, float] = (0.0, 0.0),
    *,
    basin: str = "inner_harbor_west",
    contact_id: str = "c-0001",
) -> JobFix:
    return JobFix(
        plan_id=pid,
        contact_id=contact_id,
        basin=basin,
        tick=tick,
        x_m=pos[0],
        y_m=pos[1],
        vx=vel[0],
        vy=vel[1],
    )


def status(path: Path, hour: int = HOUR) -> StandingOrdersStatus:
    return load_verified(path, now=scenario_now(hour, 0.0))


def signed_status(tmp: Path) -> StandingOrdersStatus:
    st = status(signed_orders_copy(tmp))
    assert st.signed and st.reason == "ok"
    return st


def unsigned_status(tmp: Path) -> StandingOrdersStatus:
    """The ``--unsigned`` pattern of WO-041/043: the placeholder signature."""
    path = tmp / "unsigned.yaml"
    shutil.copyfile(STANDING_ORDERS_PATH, path)
    lines = [
        'signature: "REPLACE_WITH_SHA256_AT_PHASE_4"' if ln.startswith("signature:") else ln
        for ln in path.read_text(encoding="utf-8").splitlines()
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    st = status(path)
    assert not st.signed and st.reason == "unsigned"
    return st


def guards_for(
    st: StandingOrdersStatus,
    *,
    epoch: dt.datetime | None = None,
    approver: ApproverStatus | None = None,
) -> BoundaryGuards:
    return BoundaryGuards(st, load_boundary(), approver, epoch=epoch)


def plan(pid: str, start: int, end: int, area: str = "inner_basin_research") -> PlanView:
    return PlanView(
        plan_id=pid,
        operator="Test Operator (demo)",
        vehicle="test vehicle",
        modem_id="MOD-0001",
        area=area,
        start_tick=start,
        end_tick=end,
        start_at=T0,
        end_at=T0 + dt.timedelta(hours=1),
        contact_channel="sim-phone",
        status=PlanStatus.ACTIVE,
        source=PlanSource.SCENARIO,
    )


def track(cid: str, ticks: range, x0: float, y0: float, vx: float, tick_s: float) -> Track:
    pts = []
    for t in ticks:
        lat, lon = from_local_m(x0 + vx * t * tick_s, y0)
        pts.append(
            TrackPoint(
                tick=t,
                lat=lat,
                lon=lon,
                depth_m=5.0,
                sigma_m=10.0,
                sensor_id="hyd_1",
                confidence=0.9,
            )
        )
    return Track(contact_id=cid, tick_s=tick_s, points=tuple(pts))


def cleared_case(case_id: str, cid: str, pid: str, closed_tick: int) -> CaseResult:
    return CaseResult(
        case_id=case_id,
        contact_id=cid,
        state=LadderState.CLEARED_AT_ASK,
        outcome=MatchOutcome(
            code=MatchCode.CLEARED_AT_ASK, reason="filed", evidence_row_ids=(ROW,), plan_id=pid
        ),
        reacquired_from=None,
        investigation_pending=False,
        opened_tick=max(0, closed_tick - 1),
        closed_tick=closed_tick,
    )


def run_result(
    tracks: dict[str, Track], cases: list[CaseResult], tick_s: float, n_ticks: int
) -> RunResult:
    sim = SimRun(
        scenario_id="S01",
        seed=1,
        tick_s=tick_s,
        n_ticks=n_ticks,
        tracks=tracks,
        detections=[],
        contact_truth={},
        truth_paths={},
        toggle_report=ToggleReport(),
    )
    return RunResult(
        scenario_id="S01",
        seed=1,
        sim=sim,
        cases=cases,
        transitions=[],
        evidence=EvidenceTable(),
        notices=[],
        interruptions=0,
        probes_used={},
        transition_log_hash="0" * 64,
    )
