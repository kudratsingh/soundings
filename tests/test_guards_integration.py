"""WO-041 integration: LadderMachine + BoundaryGuards + SimulatedApprover + TickClock.

The real ``config/ladder.yaml`` and a signed temp copy of the standing orders drive an
S05-shaped stream (threat mass 0.7, zone criticality 0.9, time to the fuel berth falling
below the hold criterion). Signed: HOLDING, then AWAITING_APPROVAL on ``timer(hold_limit)``,
then HANDED_OFF after the default approver grants 60 s later. Unsigned or expired: straight
from INVESTIGATING to HANDED_OFF. Unreachable: the backup is paged once and the hold is
released at handoff ("unreachable"); nothing climbs above hold. WO-043 repeats this on the
real S05 run.
"""

from __future__ import annotations

import datetime as dt
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from soundings.clock import TickClock
from soundings.ladder.actions import ActionLedger
from soundings.ladder.approver_sim import ApproverScript, Reply, SimulatedApprover
from soundings.ladder.boundary import load_boundary
from soundings.ladder.checkpoint import MemoryCheckpointStore
from soundings.ladder.guards import BoundaryGuards
from soundings.ladder.machine import LadderMachine
from soundings.ladder.standing_orders import StandingOrdersStatus, load_verified, scenario_now
from soundings.ladder.types import ActionKind, ActionScope, EventKind, LadderTimings
from soundings.schemas import LadderState, Rung, ToggleName
from soundings.schemas.config import STANDING_ORDERS_PATH
from tests.support.guard_streams import (
    CID,
    S05_HOUR,
    S05Run,
    run_s05,
    signed_orders_copy,
    signed_status,
)

REPO = Path(__file__).resolve().parent.parent
ORDERS = yaml.safe_load(STANDING_ORDERS_PATH.read_text(encoding="utf-8"))["orders"]
HOLD_LIMIT_S = ORDERS["scope"]["hold"]["max_minutes"] * 60
RUNG_ORDER = list(Rung)


@pytest.fixture
def signed(tmp_path: Path) -> StandingOrdersStatus:
    status = signed_status(signed_orders_copy(tmp_path))
    assert status.signed and status.reason == "ok"
    return status


@pytest.fixture
def unsigned(tmp_path: Path) -> StandingOrdersStatus:
    path = tmp_path / "unsigned.yaml"
    shutil.copyfile(STANDING_ORDERS_PATH, path)
    lines = [
        'signature: "REPLACE_WITH_SHA256_AT_PHASE_4"' if ln.startswith("signature:") else ln
        for ln in path.read_text(encoding="utf-8").splitlines()
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    status = signed_status(path)
    assert not status.signed and status.reason == "unsigned"
    return status


def _kinds(run: S05Run) -> list[ActionKind]:
    return [r.action.kind for r in run.machine.ledger.records(CID)]


def _hold_seconds(run: S05Run) -> float:
    cps = run.machine.checkpoints
    start = next(cp.now_s for cp in cps if cp.to_state is LadderState.HOLDING)
    end = next(
        cp.now_s
        for cp in cps
        if cp.from_state is LadderState.HOLDING and cp.to_state is not LadderState.HOLDING
    )
    return end - start


def test_signed_stream_holds_then_awaits_approval_then_hands_off(
    signed: StandingOrdersStatus,
) -> None:
    run = run_s05(signed)
    visited = run.visited
    holding = visited.index(LadderState.HOLDING)
    awaiting = visited.index(LadderState.AWAITING_APPROVAL)
    handed = visited.index(LadderState.HANDED_OFF)
    assert holding < awaiting < handed
    cp = run.machine.checkpoints[awaiting]
    assert (cp.event.kind, cp.event.arg) == (EventKind.TIMER, "hold_limit")
    granted = run.machine.checkpoints[handed]
    assert (granted.event.kind, granted.event.arg) == (EventKind.APPROVAL, "granted")
    assert granted.now_s - cp.now_s == 60.0  # default: the primary grants 60 s later
    assert _hold_seconds(run) <= HOLD_LIMIT_S
    assert run.machine.ledger.interruptions(CID) <= 1
    assert run.machine.state is LadderState.WATCH_AFTER_HANDOFF
    kinds = _kinds(run)
    assert kinds.count(ActionKind.START_HOLD) == 1
    assert kinds.count(ActionKind.RECORD_APPROVED_ACTION) == 1
    approved = [
        r
        for r in run.machine.ledger.records(CID)
        if r.action.kind is ActionKind.RECORD_APPROVED_ACTION
    ]
    hold = [r for r in run.machine.ledger.records(CID) if r.action.kind is ActionKind.START_HOLD]
    assert approved[0].action.scope == hold[0].action.scope.model_copy(
        update={"action": "hold_longer"}
    )
    # The approver was asked once, on the UI channel, for exactly the hold's scope.
    assert [(r.side, r.channel) for r in run.approver.requests] == [("primary", "ui")]


def _straight_to_handoff(run: S05Run) -> None:
    visited = run.visited
    assert LadderState.ADVISING_SLOW not in visited
    assert LadderState.HOLDING not in visited
    i = visited.index(LadderState.INVESTIGATING)
    assert visited[i + 1] is LadderState.HANDED_OFF
    assert run.machine.ledger.interruptions(CID) == 0
    assert ActionKind.START_HOLD not in _kinds(run)


def test_unsigned_stream_goes_straight_to_handoff(unsigned: StandingOrdersStatus) -> None:
    _straight_to_handoff(run_s05(unsigned))


def test_expired_orders_go_straight_to_handoff(tmp_path: Path) -> None:
    path = signed_orders_copy(tmp_path)
    past = dt.datetime.fromisoformat(ORDERS["valid_until"]) + dt.timedelta(minutes=1)
    status = load_verified(path, now=past)
    assert status.reason == "expired"
    _straight_to_handoff(run_s05(status))


def test_orders_expiring_mid_run_stop_preauthorizing(signed: StandingOrdersStatus) -> None:
    valid_until = dt.datetime.fromisoformat(ORDERS["valid_until"])
    epoch = valid_until - dt.timedelta(seconds=30)  # expires before the handoff at 60 s
    assert signed.at(epoch).signed
    _straight_to_handoff(run_s05(signed, epoch=epoch))
    still = run_s05(signed, epoch=scenario_now(S05_HOUR, 0.0))
    assert LadderState.HOLDING in still.visited


def test_unreachable_pages_backup_once_and_never_climbs(signed: StandingOrdersStatus) -> None:
    toggles = (ToggleName.APPROVER_UNREACHABLE,)
    run = run_s05(signed, ApproverScript.from_toggles(toggles), toggles)
    assert run.machine.state is LadderState.WATCH_AFTER_HANDOFF
    kinds = _kinds(run)
    assert kinds.count(ActionKind.PAGE_BACKUP) == 1
    assert run.machine.backup_paged
    assert ActionKind.RECORD_APPROVED_ACTION not in kinds
    assert ActionKind.RELEASE_HOLD in kinds
    handed = next(cp for cp in run.machine.checkpoints if cp.to_state is LadderState.HANDED_OFF)
    assert (handed.event.kind, handed.event.arg) == (EventKind.APPROVAL, "timeout")
    assert handed.guard_decisions[-1].reason == "unreachable"
    for cp in run.machine.checkpoints:
        if cp.to_state in (LadderState.HANDED_OFF, LadderState.WATCH_AFTER_HANDOFF):
            break
        assert RUNG_ORDER.index(cp.rung) <= RUNG_ORDER.index(Rung.HOLD)
    assert [(r.side, r.channel) for r in run.approver.requests] == [
        ("primary", "ui"),
        ("backup", "sms_sim"),
    ]


def test_denied_releases_hold_at_handoff(signed: StandingOrdersStatus) -> None:
    run = run_s05(signed, ApproverScript.denied())
    handed = next(cp for cp in run.machine.checkpoints if cp.to_state is LadderState.HANDED_OFF)
    assert (handed.event.kind, handed.event.arg) == (EventKind.APPROVAL, "denied")
    kinds = _kinds(run)
    assert ActionKind.RELEASE_HOLD in kinds
    assert ActionKind.RECORD_APPROVED_ACTION not in kinds
    assert run.machine.ledger.interruptions(CID) == 1


def test_network_down_backup_grants_over_sms(signed: StandingOrdersStatus) -> None:
    toggles = (ToggleName.NETWORK_DOWN,)
    run = run_s05(signed, ApproverScript.from_toggles(toggles), toggles)
    handed = next(cp for cp in run.machine.checkpoints if cp.to_state is LadderState.HANDED_OFF)
    assert (handed.event.kind, handed.event.arg) == (EventKind.APPROVAL, "granted")
    assert handed.event.payload.extra["channel"] == "sms_sim"
    assert ActionKind.RECORD_APPROVED_ACTION in _kinds(run)


def test_token_for_wider_scope_does_not_approve(signed: StandingOrdersStatus) -> None:
    """An approver that grants a wider scope than requested cannot widen it."""

    class WideningApprover(SimulatedApprover):
        def request(self, contact_id: str, scope: ActionScope, channel: str, now_s: float) -> None:
            super().request(
                contact_id, scope.model_copy(update={"max_minutes": 60}), channel, now_s
            )

    from tests.support import guard_streams

    original = guard_streams.SimulatedApprover
    guard_streams.SimulatedApprover = WideningApprover  # type: ignore[misc]
    try:
        script = ApproverScript(primary=Reply("granted"), backup=None)
        run = run_s05(signed, script)
    finally:
        guard_streams.SimulatedApprover = original  # type: ignore[misc]
    blocked = [
        d for d in run.decisions if d.guard.value == "approval_token_valid_and_scope_matches"
    ]
    assert blocked and all(d.reason == "scope_mismatch" for d in blocked)
    assert ActionKind.RECORD_APPROVED_ACTION not in _kinds(run)
    handed = next(cp for cp in run.machine.checkpoints if cp.to_state is LadderState.HANDED_OFF)
    assert (handed.event.kind, handed.event.arg) == (EventKind.APPROVAL, "timeout")


def test_restore_replays_without_guards(signed: StandingOrdersStatus) -> None:
    run = run_s05(signed)
    cps = run.machine.checkpoints
    restored = LadderMachine.restore(
        cps,
        guards=BoundaryGuards(signed, load_boundary()),
        clock=TickClock(tick_s=2.0, tick=cps[-1].tick),
        ledger=ActionLedger(),
        store=MemoryCheckpointStore(),
        timings=LadderTimings.from_config(),
    )
    assert restored.state is run.machine.state
    assert restored.head_hash == run.machine.head_hash


def _fingerprint(run: S05Run) -> tuple[list[str], list[str]]:
    return [cp.hash for cp in run.machine.checkpoints], [d.inputs_hash for d in run.decisions]


def test_deterministic_inprocess_and_across_hash_seeds(signed: StandingOrdersStatus) -> None:
    first, second = run_s05(signed), run_s05(signed)
    assert _fingerprint(first) == _fingerprint(second)
    outputs = []
    for seed in ("0", "1"):
        env = {**os.environ, "PYTHONHASHSEED": seed, "MODEL_MODE": "replay"}
        proc = subprocess.run(
            [sys.executable, "-c", "from tests.support.guard_streams import main; main()"],
            cwd=REPO,
            env=env,
            capture_output=True,
            text=True,
            check=True,
        )
        outputs.append(proc.stdout)
    assert outputs[0] and outputs[0] == outputs[1]
    hashes, inputs = _fingerprint(first)
    expected = [f"cp {i}" for i in range(len(hashes))]
    lines = outputs[0].splitlines()
    assert [" ".join(ln.split()[:2]) for ln in lines if ln.startswith("cp ")] == expected
    assert [ln.split()[-1] for ln in lines if ln.startswith("cp ")] == hashes
    assert [ln.split()[-1] for ln in lines if ln.startswith("guard ")] == inputs
