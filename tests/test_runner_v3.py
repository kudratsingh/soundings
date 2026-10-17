"""WO-043 part B: runner v3 (ask -> investigation -> ladder -> handoff -> watch) end to end.

Scenarios are graded on the ground-truth lineage (WO-022/WO-033 convention): the observed
ids that track the scenario's vehicle, sim false alarms excluded. Ground truth is read only
here and in ``soundings.eval.catalog_verify`` to pick what to grade; the runner never reads it.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from functools import cache
from pathlib import Path

import pytest

from soundings.eval.catalog_verify import check_expected, grade
from soundings.ladder.actions import INTERRUPTION_KINDS
from soundings.ladder.approval import issue_token
from soundings.ladder.audit import AuditKind
from soundings.ladder.sitrep import SitrepStatus, validate_sitrep
from soundings.ladder.types import ActionKind
from soundings.runner import (
    ManualApprover,
    RunResultV3,
    ScenarioRunner,
    ScenarioRunnerV2,
    ScenarioRunnerV3,
    with_toggles,
)
from soundings.schemas import LadderState, ToggleName
from soundings.schemas.config import STANDING_ORDERS_PATH
from soundings.sim.hashing import track_hash
from soundings.sim.run import truncate_run

REPO = Path(__file__).resolve().parents[1]
S = LadderState
ORDERS = STANDING_ORDERS_PATH
CLIMB = {S.ADVISING_SLOW, S.HOLDING, S.AWAITING_APPROVAL}


@cache
def v3(sid: str, unsigned: bool = False, toggles: tuple[str, ...] = ()) -> RunResultV3:
    runner = ScenarioRunnerV3(sid)
    if toggles:
        runner = ScenarioRunnerV3(with_toggles(runner.scenario, *toggles))
    return runner.run(orders_path=ORDERS, unsigned=unsigned)


def held_contact(result: RunResultV3) -> str:
    (cid,) = [c for c, r in result.ladder.items() if S.HOLDING in r.visited]
    return cid


# --- S05 under signed orders, and unsigned ------------------------------------------------


def test_s05_signed_passes_through_holding_then_handed_off() -> None:
    result = v3("S05")
    g = grade(result)
    assert check_expected(g, ScenarioRunner("S05").scenario) == []
    assert g.terminal_state is S.HANDED_OFF
    i_hold = g.visited.index(S.HOLDING)
    assert S.HANDED_OFF in g.visited[i_hold:]
    assert g.threat_mass is not None and g.threat_mass >= 0.6
    assert max(g.interruptions) <= 1 and max(g.probes) <= 4
    end = result.contact_cases[g.lineage[-1]]
    assert result.recommendations[end].posterior["UNAUTHORIZED_UUV"] >= 0.6
    assert result.audit_ok
    # The held contact: slow, hold, hold limit, approval (60 s), handoff, watch.
    cid = held_contact(result)
    run = result.ladder[cid]
    assert run.visited[:8] == (
        S.WATCHING,
        S.ASKING,
        S.INVESTIGATING,
        S.ADVISING_SLOW,
        S.HOLDING,
        S.AWAITING_APPROVAL,
        S.HANDED_OFF,
        S.WATCH_AFTER_HANDOFF,
    )
    ticks = {cp.to_state: cp.tick for cp in run.checkpoints}
    tick_s = result.sim.tick_s
    assert (ticks[S.AWAITING_APPROVAL] - ticks[S.HOLDING]) * tick_s == 15 * 60
    assert (ticks[S.HANDED_OFF] - ticks[S.AWAITING_APPROVAL]) * tick_s == 60
    assert ticks[S.HANDED_OFF] >= result.sim.n_ticks  # the tail is why the sim extends
    assert result.contact_interruptions[cid] == 1
    kinds = [s.kind for s in run.sitreps if s.kind != "update"]
    assert kinds == ["draft", "final"]
    assert ActionKind.RELEASE_HOLD in [a.kind for a in run.actions]


def test_s05_unsigned_goes_straight_to_handed_off() -> None:
    result = v3("S05", unsigned=True)
    g = grade(result)
    assert g.terminal_state is S.HANDED_OFF
    for run in result.ladder.values():
        assert not CLIMB & set(run.visited), run.contact_id
    for cid in g.lineage:
        visited = result.ladder[cid].visited
        i = visited.index(S.INVESTIGATING)
        assert visited[i + 1] is S.HANDED_OFF
        assert result.contact_interruptions[cid] == 1
    assert result.orders == "unsigned"


# --- the other catalog outcomes -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("sid", "state"),
    [
        ("S01", S.CLEARED_AT_ASK),
        ("S03", S.CLEARED_AT_ASK),
        ("S07", S.RESOLVED_BENIGN),
        ("S08", S.HAZARD_NOTICE),
        ("S06", S.HANDED_OFF),
    ],
)
def test_catalog_lineage_end(sid: str, state: LadderState) -> None:
    g = grade(v3(sid))
    assert g.terminal_state is state
    assert check_expected(g, ScenarioRunner(sid).scenario) == []


def test_s01_clears_at_ask_with_no_sitrep() -> None:
    result = v3("S01")
    for cid in grade(result).lineage:
        assert result.ladder[cid].sitreps == ()
        assert result.contact_interruptions[cid] == 0


def test_s02_lost_vehicle_search_box_contains_truth() -> None:
    result = v3("S02")
    g = grade(result)
    assert g.terminal_state is S.LOST_VEHICLE
    assert g.has_search_box and g.box_contains_truth is True
    assert max(g.interruptions) == 0
    end = g.lineage[-1]
    (packet,) = [p for p in result.sitrep_packets if p.contact_id == end]
    assert packet.status is SitrepStatus.FINAL and packet.inputs is not None
    assert validate_sitrep(packet.sitrep, packet.inputs) == []


def test_s04_primary_resolves_benign_with_zero_interruptions() -> None:
    result = v3("S04")
    primary = grade(result).lineage[0]
    assert result.ladder[primary].handling_outcome is S.RESOLVED_BENIGN
    assert result.contact_interruptions[primary] == 0


# --- toggles ------------------------------------------------------------------------------------


def test_toggles_unreachable_pages_backup_and_releases_hold() -> None:
    result = v3("S05", toggles=(ToggleName.APPROVER_UNREACHABLE.value,))
    cid = held_contact(result)
    run = result.ladder[cid]
    kinds = [a.kind for a in run.actions]
    assert kinds.count(ActionKind.PAGE_BACKUP) == 1
    assert ActionKind.RELEASE_HOLD in kinds
    assert ActionKind.RECORD_APPROVED_ACTION not in kinds
    assert run.handling_outcome is S.HANDED_OFF
    finals = [p for p in result.sitrep_packets if p.contact_id == cid and p.interruption]
    assert len(finals) == 1 and finals[0].unreachable
    assert "approver_unreachable" in result.degraded_reasons
    assert result.contact_interruptions[cid] == 2  # the request and the backup page


def test_toggles_standing_orders_expired_is_the_unsigned_path() -> None:
    result = v3("S05", toggles=(ToggleName.STANDING_ORDERS_EXPIRED.value,))
    for run in result.ladder.values():
        assert not CLIMB & set(run.visited)
    assert grade(result).terminal_state is S.HANDED_OFF


def test_toggles_network_down_holds_within_scope_only() -> None:
    result = v3("S05", toggles=(ToggleName.NETWORK_DOWN.value,))
    assert "network_down" in result.degraded_reasons
    cid = held_contact(result)
    for run in result.ladder.values():
        for a in run.actions:
            if a.scope is not None and a.kind is ActionKind.RECORD_APPROVED_ACTION:
                assert a.scope.action == "hold_longer"
    assert result.ladder[cid].handling_outcome is S.HANDED_OFF


# --- v2 kept, prefix stability, eval fields, audit ------------------------------------------------


def test_v2_fields_unchanged_and_prefix_stable() -> None:
    v2 = ScenarioRunnerV2("S05").run()
    result = v3("S05")
    assert result.transitions == v2.transitions
    assert result.nested_log_hash == v2.nested_log_hash
    assert dict(result.recommendations) == dict(v2.recommendations)
    assert result.sim_extended is not None and result.tail_ticks > 0
    assert result.sim_extended.n_ticks == result.sim.n_ticks + result.tail_ticks
    assert result.tail_ticks <= result.sim.n_ticks  # at most 2x in all
    prefix = truncate_run(result.sim_extended, result.sim.n_ticks)
    assert track_hash(prefix) == track_hash(result.sim)


def test_eval_fields() -> None:
    result = v3("S05")
    hold = [t for t in result.ladder_transitions if t.to_state is S.HOLDING]
    assert hold and hold[0].action == ActionKind.START_HOLD.value and hold[0].asset_id
    assert [t.seq for t in result.ladder_transitions] == list(range(len(result.ladder_transitions)))
    assert result.probe_calls and result.planner_stats
    assert result.probes_used[ActionKind.REQUEST_APPROVAL.value] == 1
    assert result.interruptions == sum(result.contact_interruptions.values())
    assert result.audit_db == ":memory:" and result.audit_run_id == "S05-seed11-signed"


def test_audit_one_row_per_sitrep_and_no_llm_call_in_template_mode() -> None:
    result = v3("S05")
    assert result.audit is not None and result.audit.verify().ok
    assert result.audit_counts[AuditKind.SITREP.value] == len(result.sitrep_packets)
    assert result.audit_counts[AuditKind.LLM_CALL.value] == 0
    assert result.audit_counts[AuditKind.APPROVAL.value] >= 1


# --- the step session ----------------------------------------------------------------------------


def test_step_session() -> None:
    runner = ScenarioRunnerV3("S05")
    session = runner.start(orders_path=ORDERS)
    seen_pending = False
    while not session.done:
        records = session.step()
        pending = session.pending_approvals()
        for cid in pending:
            assert session.result().ladder[cid].final_state is S.AWAITING_APPROVAL
        seen_pending = seen_pending or bool(pending)
        assert all(r.tick == session.tick for r in records)
    stepped = session.result()
    whole = v3("S05")
    assert seen_pending
    assert stepped.ladder_log_hash == whole.ladder_log_hash
    assert stepped.audit_head == whole.audit_head


def test_step_session_manual_approval() -> None:
    approver = ManualApprover()
    session = ScenarioRunnerV3("S05").start(orders_path=ORDERS, approver=approver)
    answered: list[str] = []
    while not session.done:
        session.step()
        for cid in session.pending_approvals():
            if cid not in answered and approver.open_requests():
                (_, scope, channel, _at) = approver.open_requests()[0]
                now = session.driver.clock.now_s()
                token = issue_token(
                    contact_id=cid,
                    scope=scope,
                    approver="primary",
                    channel=channel,
                    issued_at=now,
                    expires_at=now + 600.0,
                ).model_dump(mode="json")
                session.approve(cid, "granted", token=token)
                answered.append(cid)
    result = session.result()
    (cid,) = answered
    kinds = [a.kind for a in result.ladder[cid].actions]
    assert ActionKind.RECORD_APPROVED_ACTION in kinds
    assert result.ladder[cid].handling_outcome is S.HANDED_OFF
    assert sum(1 for a in result.ladder[cid].actions if a.kind in INTERRUPTION_KINDS) == 1


# --- determinism ---------------------------------------------------------------------------------


def test_s05_transition_log_identical_across_two_runs() -> None:
    a = ScenarioRunnerV3("S05").run(orders_path=ORDERS)
    b = ScenarioRunnerV3("S05").run(orders_path=ORDERS)
    assert a.ladder_transitions == b.ladder_transitions
    assert a.ladder_log_hash == b.ladder_log_hash and a.audit_head == b.audit_head


def _cli(args: list[str], seed: str) -> str:
    env = {**os.environ, "PYTHONHASHSEED": seed, "MODEL_MODE": "replay"}
    out = subprocess.run(
        [sys.executable, "-m", "soundings.runner", "ladder", *args],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    return out.stdout


def test_pythonhashseed(tmp_path: Path) -> None:
    outs = []
    for seed, n in (("0", 1), ("1", 2)):
        args = ["--scenario", "S05", "--seed", "11", "--orders", str(ORDERS)]
        args += [
            "--sitrep-out",
            str(tmp_path / f"s{n}.md"),
            "--audit-db",
            str(tmp_path / f"a{n}.db"),
        ]
        outs.append(_cli(args, seed))
    assert outs[0] == outs[1]
    assert (tmp_path / "s1.md").read_text() == (tmp_path / "s2.md").read_text()
    doc = json.loads(outs[0])
    assert doc["audit_ok"] and doc["orders"] == "signed_file"
    assert any("HOLDING" in [cp["to"] for cp in r["checkpoints"]] for r in doc["runs"])
    unsigned = json.loads(_cli(["--scenario", "S05", "--unsigned"], "0"))
    for r in unsigned["runs"]:
        assert "HOLDING" not in [cp["to"] for cp in r["checkpoints"]]
