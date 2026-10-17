"""WO-082 integration: slot proposals over real scenarios.

``test_s01_in_water_plan_kept``: an accepted proposal never disturbs a vehicle already
working (S01's plan is in the water at tick 0 and is pinned; only the new plan moves),
and S01 still clears at ask with no interruptions afterwards.

``test_bt01_components_solve_in_time``: every conflicted component of BT-01 at tick 0 is
solved within the 2 s budget with a proven status (OPTIMAL or INFEASIBLE, never a
time-out), and every INFEASIBLE names its binding constraint.

WO-095 gave BT-01's plans real filing-window flexibility (``WINDOW_FLEX_MIN``). With the
configured zone capacity 2 every component is still INFEASIBLE, and now ``capacity`` is
what binds, not ``latest_start``: ``test_bt01_capacity_binds_by_arithmetic`` proves it
without the solver (each component's filed work exceeds capacity x the hours from tick 0
to 24:00, so no window could fit it). ``test_bt01_slots_with_capacity_what_if`` restores
the strong assertion (``"OPTIMAL" in statuses``) on the real BT-01 under a test-only
capacity of 16; ``config/registry.yaml`` stays 2 (owner decision, DECISIONS WO-095).
"""

from __future__ import annotations

import time
from datetime import timedelta
from pathlib import Path

import pytest

from soundings.registry.models import (
    PlanStatus,
    load_conflict_config,
    scenario_epoch,
    tick_to_datetime,
)
from soundings.registry.slots import (
    BindingConstraint,
    PinReason,
    SlotConfig,
    SlotProposal,
    load_slot_config,
    model_set,
    propose_slots,
)
from soundings.registry.store import RegistryStore, plan_views_from_scenario
from soundings.runner import ScenarioRunner
from soundings.schemas import LadderState, load_harbor
from soundings.sim.scenario import resolve_scenario
from tests.support.busy_plans import bt01_available, bt01_or_standin

HARBOR = load_harbor()


def test_s01_in_water_plan_kept(tmp_path: Path) -> None:
    scenario = ScenarioRunner("S01").scenario
    epoch = scenario_epoch(scenario)
    s01 = scenario.registry_plans[0]
    cfg = load_slot_config().model_copy(update={"zone_capacity": {s01.area: 1}})
    # the store clock is the scenario epoch (proposal rows are stamped with it)
    store = RegistryStore(tmp_path / "s01.db", harbor=HARBOR, now=lambda: epoch, slot_config=cfg)
    try:
        (seeded,) = store.seed_from_scenario(scenario)
        other = store.create(
            {
                "plan_id": "P-SECOND-MODEM",
                "operator": "Harbor Survey Co (demo)",
                "vehicle": "ROV-Blue-2",
                "modem_id": "MOD-3399",
                "area": seeded.area,
                "start_tick": 300,
                "end_tick": seeded.end_tick,
                "contact_channel": "radio ch 16",
                "source": "scenario",
                "created_at": epoch,
            },
            epoch=epoch,
            tick_s=scenario.tick_s,
        )
        assert other.status is PlanStatus.CONFLICTED
        before = store.get(seeded.plan_id)
        assert before is not None
        now = tick_to_datetime(epoch, 0, scenario.tick_s)
        proposal = store.propose(other.plan_id, now=now)
        assert proposal.status == "feasible" and proposal.optimal
        shifts = {s.plan_id: s for s in proposal.shifts}
        assert shifts[seeded.plan_id].pinned is PinReason.IN_WATER
        assert shifts[seeded.plan_id].shift_min == 0
        assert shifts[other.plan_id].shift_min > 0
        assert [p for p, s in shifts.items() if s.shift_min != 0] == [other.plan_id]
        store.save_proposal(proposal)
        stored, plans = store.accept_proposal(proposal.proposal_id, at=now)
        assert stored.status.value == "accepted"
        after = store.get(seeded.plan_id)
        assert after is not None
        keep = {"status", "conflicts"}
        assert after.model_dump(exclude=keep) == before.model_dump(exclude=keep)
        moved = store.get(other.plan_id)
        assert moved is not None and moved.start_tick is not None
        assert moved.start_tick == 300 + round(
            shifts[other.plan_id].shift_min * 60 / scenario.tick_s
        )
        assert all(p.status is PlanStatus.ACTIVE for p in plans)
        result = ScenarioRunner(scenario, plans=store).run()
    finally:
        store.close()
    assert any(c.state is LadderState.CLEARED_AT_ASK for c in result.cases)
    assert result.interruptions == 0


def _bt01_components(cfg: SlotConfig | None = None) -> list[SlotProposal]:
    """One proposal per distinct conflicted component of BT-01 at tick 0, timed."""
    scenario = bt01_or_standin()
    views = plan_views_from_scenario(scenario)
    now = scenario_epoch(scenario)
    seen: set[tuple[str, ...]] = set()
    out: list[SlotProposal] = []
    for target in views:
        if target.status is not PlanStatus.CONFLICTED:
            continue
        started = time.process_time()
        proposal = propose_slots(target.plan_id, views, harbor=HARBOR, now=now, cfg=cfg)
        spent = time.process_time() - started
        if proposal.plan_ids in seen:
            continue
        seen.add(proposal.plan_ids)
        assert spent < 2.0, (target.plan_id, spent)
        out.append(proposal)
    return out


def _assert_honest(proposals: list[SlotProposal]) -> list[str]:
    statuses = [p.solver_status for p in proposals]
    assert statuses, "BT-01 must have at least one conflicted component"
    for p in proposals:
        assert p.solver_status in ("OPTIMAL", "INFEASIBLE"), p.detail
        if p.solver_status == "INFEASIBLE":
            # an honest "no slot" names what binds (WO-085 triage surfaces it)
            assert p.binding_constraint is not None, p.detail
            assert p.binding_constraint.value in ("capacity", "latest_start", "in_water")
    return statuses


def test_bt01_components_solve_in_time() -> None:
    real = bt01_available()
    proposals = _bt01_components()
    _assert_honest(proposals)
    if real:
        # WO-095: the windows are wide now, so what binds at capacity 2 is the capacity.
        assert {p.zone_id for p in proposals} == {
            "inner_basin_research",
            "main_channel",
            "pier_400_approach",
            "vincent_thomas_bridge",
        }
        for p in proposals:
            assert p.solver_status == "INFEASIBLE" and p.capacity == 2, p.detail
            assert p.binding_constraint is BindingConstraint.CAPACITY, p.detail


def test_bt01_capacity_binds_by_arithmetic() -> None:
    """Each component's filed work exceeds capacity x (24:00 - tick 0): no window fits it."""
    if not bt01_available():
        pytest.skip("needs scenarios/BT-01.yaml")
    scenario = resolve_scenario("BT-01")
    views = plan_views_from_scenario(scenario)
    now = scenario_epoch(scenario)
    cfg = load_slot_config()
    ccfg = load_conflict_config()
    horizon = now.replace(hour=0, minute=0) + timedelta(minutes=cfg.horizon_minutes)
    available_h = (horizon - now).total_seconds() / 3600.0
    for zone in sorted({v.area for v in views}):
        target = next(v for v in views if v.area == zone)
        members = model_set(target, views, HARBOR, ccfg)
        work_h = sum((v.end_at - v.start_at).total_seconds() for v in members) / 3600.0
        cap = cfg.capacity_for(zone)
        assert work_h > cap * available_h, (zone, work_h, cap, available_h)


def test_bt01_slots_with_capacity_what_if() -> None:
    """Strong assertion on the real BT-01 under a test-only capacity (config stays 2)."""
    if not bt01_available():
        pytest.skip("needs scenarios/BT-01.yaml")
    what_if = load_slot_config().model_copy(update={"zone_capacity_default": 16})
    proposals = _bt01_components(what_if)
    statuses = _assert_honest(proposals)
    assert "OPTIMAL" in statuses, statuses
    by_zone = {p.zone_id: p for p in proposals}
    bridge = by_zone["vincent_thomas_bridge"]
    assert bridge.solver_status == "OPTIMAL" and bridge.total_delay_min > 0, bridge.detail
    # the WO-095 bounds are what the proposal respects: no shift leaves a plan's bounds
    views = {v.plan_id: v for v in plan_views_from_scenario(resolve_scenario("BT-01"))}
    for p in proposals:
        for s in p.shifts:
            v = views[s.plan_id]
            assert v.earliest_start_at is not None and v.latest_start_at is not None
            if s.shift_min:
                assert v.earliest_start_at <= s.new_start_at <= v.latest_start_at, s
