"""An S05-shaped ladder stream driven through ``BoundaryGuards`` and a simulated approver.

Test only (WO-041). The stream: a contact closes on a fuel berth; threat mass 0.7, zone
criticality 0.9, and the time to the nearest critical asset falls from 450 s to 250 s
(below the hold criterion). ``run_s05`` plays it on a ``TickClock`` through a WO-040
``LadderMachine`` and a small driver that forwards approval requests and backup pages to
the ``ApproverPort`` and feeds its replies back as ``approval`` events.

``python -c "from tests.support.guard_streams import main; main()"`` prints every
checkpoint hash and guard ``inputs_hash``; the determinism test compares that output
across ``PYTHONHASHSEED`` values.
"""

from __future__ import annotations

import datetime as dt
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from soundings.clock import TickClock
from soundings.ladder.actions import ActionLedger
from soundings.ladder.approver_sim import ApproverScript, SimulatedApprover
from soundings.ladder.boundary import load_boundary
from soundings.ladder.checkpoint import MemoryCheckpointStore
from soundings.ladder.guards import ApproverStatus, BoundaryGuards
from soundings.ladder.machine import LadderMachine, StepResult
from soundings.ladder.ports import reply_event
from soundings.ladder.sign import sign
from soundings.ladder.standing_orders import StandingOrdersStatus, load_verified, scenario_now
from soundings.ladder.types import (
    ActionKind,
    EventKind,
    GuardDecision,
    LadderEvent,
    LadderTimings,
)
from soundings.schemas import LadderState, ToggleName, Unit
from soundings.schemas._base import CONFIG_DIR
from tests.support.ladder_streams import F, P, ev, row

TICK_S = 2.0
S05_HOUR = 2
"""S05 runs at 02:00 (scenarios/S05: hour_of_day 2): the 3 AM crisis test."""
CID = "c-0005"
TAIL_TICKS = 900
"""Ticks the driver keeps running after the scripted events (covers hold + approvals)."""


def signed_orders_copy(dst_dir: Path) -> Path:
    """A signed copy of ``config/standing_orders.yaml`` in ``dst_dir``."""
    path = dst_dir / "standing_orders.yaml"
    shutil.copyfile(CONFIG_DIR / "standing_orders.yaml", path)
    sign(path)
    return path


def s05_events(cid: str = CID) -> list[LadderEvent]:
    """The scripted S05-shaped events (the timers and approvals come from the driver)."""
    basin = "inner_harbor_west"

    def closing(t: int, time_s: float) -> tuple:
        return (
            row(cid, t, F.TIME_TO_NEAREST_CRITICAL_ASSET, time_s, Unit.S, P.KINEMATICS_PROFILE),
            row(cid, t, F.ZONE_CRITICALITY, 0.9, Unit.RATIO, None),
            row(cid, t, F.NEAREST_ASSET_ID, "fuel_berth_1", Unit.ID, None),
        )

    return [
        ev(
            EventKind.CONTACT_DETECTED,
            0,
            rows=(row(cid, 0, F.ZONE_ID, "fuel_berths", Unit.ID, None),),
        ),
        ev(
            EventKind.PROBE_RESULT,
            1,
            rows=(
                row(cid, 1, F.SPEED_OVER_GROUND, 1.2, Unit.M_PER_S, P.KINEMATICS_PROFILE),
                row(cid, 1, F.DRIFT_CONSISTENCY, 0.1, Unit.RATIO, P.DRIFT_CHECK),
            ),
        ),
        ev(
            EventKind.PROBE_RESULT,
            12,
            rows=(
                row(cid, 12, F.REGISTRY_MATCH, "none", Unit.CATEGORY, P.REGISTRY_LOOKUP, 1.0),
                row(cid, 12, F.HAIL_REPLY, "none", Unit.CATEGORY, P.ACOUSTIC_HAIL),
            ),
        ),
        ev(
            EventKind.STOP_RULE_FIRED,
            30,
            "HANDOFF",
            rows=closing(30, 450.0),
            threat_mass=0.7,
            benign_mass=0.1,
            basin=basin,
        ),
        ev(
            EventKind.STANDING_ORDER_CHECK,
            60,
            "hold_permitted",
            rows=closing(60, 250.0),
            threat_mass=0.7,
            benign_mass=0.1,
        ),
    ]


@dataclass
class S05Run:
    machine: LadderMachine
    approver: SimulatedApprover
    results: list[StepResult] = field(default_factory=list)

    @property
    def visited(self) -> list[LadderState]:
        return [cp.to_state for cp in self.machine.checkpoints]

    @property
    def decisions(self) -> list[GuardDecision]:
        return [d for r in self.results for d in r.decisions]


def _drive(result: StepResult, approver: SimulatedApprover, guards: BoundaryGuards, now: float):
    primary = "ui"
    backup = "sms_sim"
    if guards.standing.orders is not None:
        approvers = guards.standing.orders.orders.approvers
        primary = approvers["primary"].channel
        backup = approvers["backup"].channel if "backup" in approvers else backup
    for action in result.actions:
        if action.kind is ActionKind.REQUEST_APPROVAL and action.scope is not None:
            approver.request(action.contact_id, action.scope, primary, now)
        elif action.kind is ActionKind.PAGE_BACKUP and action.scope is not None:
            approver.request(action.contact_id, action.scope, backup, now)


def run_s05(
    status: StandingOrdersStatus,
    script: ApproverScript | None = None,
    toggles: tuple[ToggleName, ...] = (),
    *,
    epoch: dt.datetime | None = None,
) -> S05Run:
    """Play the S05-shaped stream; return the machine, the approver and every step result."""
    clock = TickClock(tick_s=TICK_S)
    guards = BoundaryGuards(
        status, load_boundary(), ApproverStatus.from_toggles(toggles), epoch=epoch
    )
    approver = SimulatedApprover(script or ApproverScript.default(), clock)
    machine = LadderMachine(
        CID, guards, clock, ActionLedger(), MemoryCheckpointStore(), LadderTimings.from_config()
    )
    run = S05Run(machine, approver)

    def step(event: LadderEvent) -> None:
        result = machine.step(event)
        run.results.append(result)
        _drive(result, approver, guards, clock.now_s())
        if result.to_state is LadderState.HANDED_OFF and result.transitioned:
            run.results.append(machine.step(ev(EventKind.HANDOFF_COMPLETE, clock.tick)))

    def tick_work() -> None:
        for result in machine.advance():
            run.results.append(result)
            _drive(result, approver, guards, clock.now_s())
            if result.transitioned and result.to_state is LadderState.HANDED_OFF:
                run.results.append(machine.step(ev(EventKind.HANDOFF_COMPLETE, clock.tick)))
        for reply in approver.poll(clock.now_s()):
            step(reply_event(reply, clock.tick))

    events = s05_events()
    for event in events:
        while clock.tick < event.tick:
            clock.advance()
            tick_work()
        step(event)
    for _ in range(TAIL_TICKS):
        if machine.state in (LadderState.WATCH_AFTER_HANDOFF, LadderState.RESOLVED_DEPARTED):
            break
        clock.advance()
        tick_work()
    return run


def signed_status(path: Path, now_s: float = 0.0) -> StandingOrdersStatus:
    return load_verified(path, now=scenario_now(S05_HOUR, now_s))


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        status = signed_status(signed_orders_copy(Path(tmp)))
    run = run_s05(status)
    for cp in run.machine.checkpoints:
        print("cp", cp.seq, cp.to_state.value, cp.hash)
    for d in run.decisions:
        print("guard", d.guard.value, d.verdict.value, d.inputs_hash)


if __name__ == "__main__":
    main()
