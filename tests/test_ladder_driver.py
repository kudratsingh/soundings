"""WO-043 part A: the ladder driver over the S05-shaped synthetic feed.

Signed orders (a signed temp copy of ``config/standing_orders.yaml``) with the default
approver: ADVISING_SLOW -> HOLDING -> AWAITING_APPROVAL -> HANDED_OFF, at most one
interruption, the hold released at its limit. Unsigned: straight to HANDED_OFF. An
unreachable approver: the backup is paged, the hold released at its limit, the SITREP
flagged ``unreachable``. Expired orders: nothing above ``ask`` executes. Every threshold and
timer is read from config, never restated here.
"""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest
import yaml

from soundings.ladder.actions import HOLD_LONGER
from soundings.ladder.approver_sim import ApproverScript, SimulatedApprover
from soundings.ladder.boundary import load_criteria
from soundings.ladder.checkpoint import MemoryCheckpointStore
from soundings.ladder.driver import (
    LadderDriver,
    LadderRun,
    default_tail_ticks,
    token_ttl_s_from_config,
    transition_log,
)
from soundings.ladder.guard_port import NoStandingOrdersGuards
from soundings.ladder.machine import HOLD_LIMIT, LadderMachine
from soundings.ladder.standing_orders import read_document
from soundings.ladder.synthetic import S05_CONTACT, S05_HOUR, S05_TICK_S, s05_feed
from soundings.ladder.types import ActionKind, ActionScope, LadderTimings
from soundings.ladder.watch import DepartureDetector
from soundings.schemas import LadderState, ToggleName, load_harbor
from soundings.schemas.config import LADDER_PATH
from tests.support.guard_streams import signed_orders_copy

REPO = Path(__file__).resolve().parents[1]
S = LadderState
A = ActionKind
LADDER_YAML = yaml.safe_load(LADDER_PATH.read_text(encoding="utf-8"))
CRIT = load_criteria()


@pytest.fixture(scope="module")
def orders(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return signed_orders_copy(tmp_path_factory.mktemp("orders"))


def _orders_doc(path: Path) -> dict:
    return read_document(path)["orders"]


def drive(
    orders_path: Path | None,
    *,
    unsigned: bool = False,
    toggles: tuple[ToggleName, ...] = (),
    script: ApproverScript | None = None,
    **feed_kw: object,
) -> tuple[LadderDriver, LadderRun]:
    detector = DepartureDetector.from_config(load_harbor(), S05_TICK_S)
    driver = LadderDriver.from_config(
        tick_s=S05_TICK_S,
        hour_of_day=S05_HOUR,
        orders_path=orders_path,
        unsigned=unsigned,
        toggles=toggles,
        script=script,
        departure=detector,
    )
    runs = driver.run([s05_feed(**feed_kw)])  # type: ignore[arg-type]
    return driver, runs[S05_CONTACT]


def kinds(run: LadderRun) -> list[ActionKind]:
    return [a.kind for a in run.actions]


def in_order(visited: tuple[LadderState, ...], wanted: list[LadderState]) -> bool:
    it = iter(visited)
    return all(any(v is w for v in it) for w in wanted)


def hold_limit_ticks(orders_path: Path) -> int:
    return int(_orders_doc(orders_path)["scope"]["hold"]["max_minutes"] * 60 / S05_TICK_S)


# --- the S05 shape under signed orders ---------------------------------------------------


def test_s05_signed_default_approver(orders: Path) -> None:
    _, run = drive(orders)
    assert in_order(run.visited, [S.ADVISING_SLOW, S.HOLDING, S.AWAITING_APPROVAL, S.HANDED_OFF])
    assert run.handling_outcome is S.HANDED_OFF
    assert run.final_state is S.WATCH_AFTER_HANDOFF
    assert run.interruptions <= LADDER_YAML["budgets"]["human_interruptions_per_contact"]
    assert run.interruptions == 1
    k = kinds(run)
    assert k.count(A.REQUEST_APPROVAL) == 1 and A.PAGE_BACKUP not in k
    assert A.RECORD_APPROVED_ACTION in k
    # Draft SITREP with the request, final SITREP at the handoff.
    assert [s.kind for s in run.sitreps][:2] == ["draft", "final"]
    assert run.sitreps[0].inputs.state is S.AWAITING_APPROVAL
    assert run.sitreps[0].inputs.requested_scope is not None
    assert run.sitreps[0].inputs.requested_scope.action == HOLD_LONGER
    assert run.sitreps[1].inputs.state is S.HANDED_OFF
    assert all(s.inputs.cited_row_ids for s in run.sitreps)


def test_hold_starts_below_the_hold_criterion_and_ends_at_its_limit(orders: Path) -> None:
    _, run = drive(orders)
    feed = s05_feed()
    hold = next(cp for cp in run.checkpoints if cp.to_state is S.HOLDING)
    first_below = min(
        r.tick
        for r in feed.recommendations
        if r.time_to_nearest_critical_asset is not None
        and r.time_to_nearest_critical_asset < CRIT.hold.time_to_critical_asset_max_s
    )
    assert hold.tick == first_below
    assert hold.event.kind.value == "standing_order_check" and hold.event.arg == "hold_permitted"
    limit_tick = hold.tick + hold_limit_ticks(orders)
    awaiting = next(cp for cp in run.checkpoints if cp.to_state is S.AWAITING_APPROVAL)
    assert awaiting.tick == limit_tick
    # Released at its limit, in the same transition that asks for more time (DECISIONS 23:25).
    released = [a for a in run.actions if a.kind is A.RELEASE_HOLD]
    assert released[0].tick == limit_tick
    granted = next(cp for cp in run.checkpoints if cp.to_state is S.HANDED_OFF)
    reply_ticks = 60.0 / S05_TICK_S  # DECISIONS 23:05: the default primary grants after 60 s
    assert granted.tick == limit_tick + reply_ticks


def test_unsigned_goes_straight_to_handed_off(orders: Path) -> None:
    for path, unsigned in ((orders, True), (None, False)):
        driver, run = drive(path, unsigned=unsigned)
        assert isinstance(driver.guards, NoStandingOrdersGuards)
        assert run.handling_outcome is S.HANDED_OFF
        assert S.ADVISING_SLOW not in run.visited and S.HOLDING not in run.visited
        assert S.AWAITING_APPROVAL not in run.visited
        handed = next(i for i, s in enumerate(run.visited) if s is S.HANDED_OFF)
        assert run.visited[handed - 1] is S.INVESTIGATING
        assert run.interruptions == 0
        assert not {A.ISSUE_SLOW_ADVISORY, A.START_HOLD, A.REQUEST_APPROVAL} & set(kinds(run))


def test_unreachable_approver_pages_backup_and_flags_sitrep(orders: Path) -> None:
    _, run = drive(orders, toggles=(ToggleName.APPROVER_UNREACHABLE,))
    k = kinds(run)
    assert k.count(A.REQUEST_APPROVAL) == 1 and k.count(A.PAGE_BACKUP) == 1
    assert run.handling_outcome is S.HANDED_OFF
    hold = next(cp for cp in run.checkpoints if cp.to_state is S.HOLDING)
    limit_tick = hold.tick + hold_limit_ticks(orders)
    release = [a for a in run.actions if a.kind is A.RELEASE_HOLD]
    assert len(release) == 1 and release[0].tick == limit_tick
    # The state stays AWAITING_APPROVAL until the approval resolves (timeout, no backup left).
    seq = [cp.to_state for cp in run.checkpoints if limit_tick <= cp.tick]
    assert seq[:3] == [S.AWAITING_APPROVAL, S.AWAITING_APPROVAL, S.HANDED_OFF]
    timings = LadderTimings.from_config()
    assert timings.backup_timeout_s is not None
    page = next(a for a in run.actions if a.kind is A.PAGE_BACKUP)
    assert page.tick == limit_tick + timings.primary_timeout_s / S05_TICK_S
    handed = next(cp for cp in run.checkpoints if cp.to_state is S.HANDED_OFF)
    assert handed.tick == page.tick + timings.backup_timeout_s / S05_TICK_S
    final = next(s for s in run.sitreps if s.kind == "final")
    assert final.inputs.flags == ("unreachable",)
    assert "unreachable" in final.text
    assert any(d.reason == "unreachable" and d.permits for d in run.guard_decisions)


def test_expired_orders_permit_nothing_above_ask(orders: Path) -> None:
    driver, run = drive(orders, toggles=(ToggleName.STANDING_ORDERS_EXPIRED,))
    assert run.handling_outcome is S.HANDED_OFF
    assert S.ADVISING_SLOW not in run.visited and S.HOLDING not in run.visited
    assert not {A.ISSUE_SLOW_ADVISORY, A.START_HOLD, A.REQUEST_APPROVAL} & set(kinds(run))
    # The guards saw the orders expired, not missing or unsigned.
    assert driver.guards.status_at(0.0).reason == "expired"  # type: ignore[attr-defined]
    assert {d.reason for d in run.guard_decisions} >= {"standing_orders_unsigned"}


def test_denied_releases_once(orders: Path) -> None:
    _, run = drive(orders, script=ApproverScript.denied())
    assert run.handling_outcome is S.HANDED_OFF
    assert kinds(run).count(A.RELEASE_HOLD) == 1
    assert A.RECORD_APPROVED_ACTION not in kinds(run)


# --- scope exceeded and the hold limit while awaiting approval ---------------------------


def test_scope_exceeded_only_while_holding_with_a_request(orders: Path) -> None:
    hold_c = _orders_doc(orders)["scope"]["hold"]
    wider = ActionScope(
        action="hold_wider", asset_id="fuel_berth_1", gates=2, max_minutes=hold_c["max_minutes"]
    )
    within = ActionScope(
        action=CRIT.hold.action,
        asset_id="fuel_berth_1",
        gates=1,
        max_minutes=hold_c["max_minutes"],
    )
    base = drive(orders)[1]
    hold_tick = next(cp.tick for cp in base.checkpoints if cp.to_state is S.HOLDING)
    limit_tick = hold_tick + hold_limit_ticks(orders)
    ask_tick = limit_tick - 20

    # A request the orders cover: no event. A request before HOLDING: no event.
    feed = s05_feed()
    early = replace(feed, scope_requests=((20, wider), (ask_tick, within)))
    det = DepartureDetector.from_config(load_harbor(), S05_TICK_S)
    d = LadderDriver.from_config(
        tick_s=S05_TICK_S, hour_of_day=S05_HOUR, orders_path=orders, departure=det
    )
    run = d.run([early])[S05_CONTACT]
    assert [cp.event.kind.value for cp in run.checkpoints].count("scope_exceeded") == 0

    # A wider request while HOLDING: AWAITING_APPROVAL for exactly that scope; the hold limit
    # then comes due while awaiting and releases the hold, and the state does not change.
    wide = replace(feed, scope_requests=((ask_tick, wider),))
    det = DepartureDetector.from_config(load_harbor(), S05_TICK_S)
    d = LadderDriver.from_config(
        tick_s=S05_TICK_S,
        hour_of_day=S05_HOUR,
        orders_path=orders,
        departure=det,
        toggles=(ToggleName.APPROVER_UNREACHABLE,),
    )
    run = d.run([wide])[S05_CONTACT]
    exceeded = next(cp for cp in run.checkpoints if cp.event.kind.value == "scope_exceeded")
    assert exceeded.tick == ask_tick and exceeded.to_state is S.AWAITING_APPROVAL
    request = next(a for a in run.actions if a.kind is A.REQUEST_APPROVAL)
    assert request.scope == wider
    release = [a for a in run.actions if a.kind is A.RELEASE_HOLD]
    assert len(release) == 1 and release[0].tick == limit_tick
    assert not any(cp.tick == limit_tick for cp in run.checkpoints)  # no transition at the limit
    assert run.handling_outcome is S.HANDED_OFF


def test_strict_machine_drops_stale_timers(orders: Path) -> None:
    """A hold limit with no row in AWAITING_APPROVAL is dropped, not retried forever."""
    from soundings.clock import TickClock
    from soundings.ladder.actions import ActionLedger
    from soundings.ladder.boundary import load_boundary
    from soundings.ladder.guards import ApproverStatus, BoundaryGuards
    from soundings.ladder.standing_orders import load_verified, scenario_now
    from tests.support.ladder_streams import ev

    status = load_verified(orders, now=scenario_now(S05_HOUR, 0))
    guards = BoundaryGuards(status, load_boundary(), ApproverStatus(), epoch=scenario_now(2, 0))
    clock = TickClock(tick_s=S05_TICK_S)
    feed = s05_feed()
    m = LadderMachine(
        S05_CONTACT,
        guards,
        clock,
        ActionLedger(),
        MemoryCheckpointStore(),
        LadderTimings.from_config(),
        strict_holds=True,
    )
    from soundings.ladder.types import EventKind as E

    m.step(ev(E.CONTACT_DETECTED, 0, rows=feed.detection_rows))
    m.step(ev(E.PROBE_RESULT, 1, rows=feed.watch_rows))
    m.step(ev(E.PROBE_RESULT, 12, rows=feed.ask_rows))
    rec = next(r for r in feed.recommendations if r.time_to_nearest_critical_asset == 250.0)
    guards_blocked_slow = ev(
        E.STOP_RULE_FIRED, rec.tick, "HANDOFF", rows=tuple(rec.rows), threat_mass=0.7, basin="x"
    )
    m.step(guards_blocked_slow)  # basin outside the orders: slow blocks, hold permits
    assert m.state is S.HOLDING
    m.step(
        ev(
            E.SCOPE_EXCEEDED,
            rec.tick + 1,
            requested_scope=ActionScope(action="hold_wider", gates=2, max_minutes=15),
        )
    )
    assert m.state is S.AWAITING_APPROVAL
    assert HOLD_LIMIT in [t.name for t in m.timers.pending()]
    deadline = next(t.deadline_s for t in m.timers.pending() if t.name == HOLD_LIMIT)
    clock.tick = int(deadline / S05_TICK_S)
    results = m.advance()
    # The approval timeout (a row: backup paged) fires first, then the hold limit (no row).
    assert [r.reason for r in results] == ["transitioned", "timer_dropped"]
    assert [a.kind for a in results[1].actions] == [A.RELEASE_HOLD]
    assert HOLD_LIMIT not in [t.name for t in m.timers.pending()]
    assert m.state is S.AWAITING_APPROVAL and m.hold_scope is None
    assert m.advance() == []


# --- departure, restore, config ----------------------------------------------------------


def test_granted_hold_released_on_departure(orders: Path) -> None:
    driver, run = drive(orders, depart_at_tick=650)
    assert run.handling_outcome is S.HANDED_OFF and run.final_state is S.RESOLVED_DEPARTED
    assert kinds(run)[-2:] == [A.CLOSE_WATCH, A.RELEASE_HOLD]
    from soundings.ladder.actions import ActionLedger

    ledger = ActionLedger()
    for action in run.actions:
        ledger.execute(action)
        if action.kind is A.RECORD_APPROVED_ACTION:
            assert ledger.active_holds() == 1  # the granted hold_longer is the hold in force
    assert ledger.active_holds() == 0
    assert run.watch is not None and run.watch.departed_at_s is not None


def test_checkpoints_restore_under_strict_holds(orders: Path) -> None:
    driver, run = drive(orders, depart_at_tick=650)
    from soundings.clock import TickClock
    from soundings.ladder.actions import ActionLedger

    restored = LadderMachine.restore(
        run.checkpoints,
        guards=driver.guards,
        clock=TickClock(tick_s=S05_TICK_S),
        ledger=ActionLedger(),
        store=MemoryCheckpointStore(),
        timings=driver.timings,
        strict_holds=True,
    )
    assert restored.state is run.final_state
    assert restored.head_hash == run.checkpoints[-1].hash
    assert restored.ledger.active_holds() == 0


def test_token_ttl_and_tail_come_from_config(orders: Path) -> None:
    assert token_ttl_s_from_config() == float(LADDER_YAML["approval"]["token_ttl_s"])
    driver, _ = drive(orders)
    assert isinstance(driver.approver, SimulatedApprover)
    assert driver.approver.token_ttl_s == float(LADDER_YAML["approval"]["token_ttl_s"])
    timings = LadderTimings.from_config()
    hold_minutes = _orders_doc(orders)["scope"]["hold"]["max_minutes"]
    assert driver.max_tail_ticks == default_tail_ticks(timings, hold_minutes, S05_TICK_S)


# --- determinism and rule scans ----------------------------------------------------------


def test_transition_log_is_deterministic(orders: Path) -> None:
    for toggles in ((), (ToggleName.APPROVER_UNREACHABLE,)):
        a = transition_log({S05_CONTACT: drive(orders, toggles=toggles)[1]})
        b = transition_log({S05_CONTACT: drive(orders, toggles=toggles)[1]})
        assert a == b and a.count("\n") > 5


def _cli(args: list[str], seed: str, cwd: Path) -> str:
    env = {**os.environ, "PYTHONHASHSEED": seed, "MODEL_MODE": "replay"}
    out = subprocess.run(
        [sys.executable, "-m", "soundings.ladder", *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    return out.stdout


def test_cli_is_deterministic_across_hash_seeds(tmp_path: Path) -> None:
    orders = REPO / "config" / "standing_orders.yaml"
    outs = []
    for seed, n in (("0", 1), ("1", 2)):
        args = ["--scenario", "S05", "--seed", "11", "--orders", str(orders)]
        args += [
            "--sitrep-out",
            str(tmp_path / f"s{n}.md"),
            "--audit-db",
            str(tmp_path / f"a{n}.db"),
        ]
        outs.append(_cli(args, seed, REPO))
    assert outs[0] == outs[1]
    assert (tmp_path / "s1.md").read_text() == (tmp_path / "s2.md").read_text()
    doc = json.loads(outs[0])
    (run,) = doc["runs"]
    assert run["handling_outcome"] == "HANDED_OFF"
    assert "HOLDING" in [cp["to"] for cp in run["checkpoints"]]
    unsigned = json.loads(_cli(["--unsigned"], "0", REPO))
    assert "HOLDING" not in [cp["to"] for cp in unsigned["runs"][0]["checkpoints"]]


NEW_MODULES = ("driver", "watch", "sitrep_port", "synthetic", "__main__")


def test_no_model_imports_and_no_wall_clock() -> None:
    for name in NEW_MODULES:
        path = REPO / "soundings" / "ladder" / f"{name}.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                assert not mod.startswith(("soundings.llm", "anthropic", "llm")), (name, mod)
                assert not (mod == "datetime" and any(a.name == "datetime" for a in node.names))
                assert mod not in ("time",), (name, mod)
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert not alias.name.startswith(("anthropic", "soundings.llm")), name
                    assert alias.name != "time", name
            if isinstance(node, ast.Attribute):
                assert node.attr not in ("now", "utcnow", "today", "time_ns"), (name, node.attr)


# --- WO-043 part B: real catalog scenarios through runner v2 and the driver -----------------

from soundings.eval.catalog_verify import grade  # noqa: E402
from soundings.runner import ScenarioRunnerV3  # noqa: E402


@pytest.mark.parametrize(
    ("sid", "state"),
    [
        ("S01", LadderState.CLEARED_AT_ASK),
        ("S02", LadderState.LOST_VEHICLE),
        ("S07", LadderState.RESOLVED_BENIGN),
        ("S08", LadderState.HAZARD_NOTICE),
        ("S05", LadderState.HANDED_OFF),
    ],
)
def test_catalog_through_driver_no_standing_orders(sid: str, state: LadderState) -> None:
    """``NoStandingOrdersGuards`` (no orders): each Phase 2/3 outcome, and S05 unsigned."""
    result = ScenarioRunnerV3(sid).run(orders_path=None)
    assert result.orders == "unsigned"
    g = grade(result)
    assert g.terminal_state is state
    for run in result.ladder.values():
        assert LadderState.ADVISING_SLOW not in run.visited
        assert LadderState.HOLDING not in run.visited
        assert run.interruptions == 0


def test_s04_primary_through_driver_resolved_benign() -> None:
    result = ScenarioRunnerV3("S04").run(orders_path=None)
    primary = grade(result).lineage[0]
    assert result.ladder[primary].handling_outcome is LadderState.RESOLVED_BENIGN
