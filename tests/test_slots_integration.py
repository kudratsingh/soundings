"""WO-082 integration: slot proposals over real scenarios.

``test_s01_in_water_plan_kept``: an accepted proposal never disturbs a vehicle already
working (S01's plan is in the water at tick 0 and is pinned; only the new plan moves),
and S01 still clears at ask with no interruptions afterwards.

``test_bt01_components_solve_in_time``: every conflicted component of BT-01 at tick 0 is
solved within the 2 s budget with a proven status. With ``scenarios/BT-01.yaml`` on main
every component must be OPTIMAL; until then the seeded 40-plan stand-in from
``tests/support/busy_plans`` is used, whose default-capacity components are mostly
proven INFEASIBLE (40 plans of 1-3 h cannot share 2 slots in one afternoon), so it
asserts a proven status (OPTIMAL or INFEASIBLE, never a time-out) instead.
"""

from __future__ import annotations

import time
from pathlib import Path

from soundings.registry.models import PlanStatus, scenario_epoch, tick_to_datetime
from soundings.registry.slots import PinReason, load_slot_config, propose_slots
from soundings.registry.store import RegistryStore, plan_views_from_scenario
from soundings.runner import ScenarioRunner
from soundings.schemas import LadderState, load_harbor
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


def test_bt01_components_solve_in_time() -> None:
    scenario = bt01_or_standin()
    real = bt01_available()
    views = plan_views_from_scenario(scenario)
    now = scenario_epoch(scenario)
    seen: set[tuple[str, ...]] = set()
    statuses: list[str] = []
    for target in views:
        if target.status is not PlanStatus.CONFLICTED:
            continue
        started = time.process_time()
        proposal = propose_slots(target.plan_id, views, harbor=HARBOR, now=now)
        spent = time.process_time() - started
        if proposal.plan_ids in seen:
            continue
        seen.add(proposal.plan_ids)
        statuses.append(proposal.solver_status)
        assert spent < 2.0, (target.plan_id, spent)
        if real:
            assert proposal.solver_status == "OPTIMAL", proposal.detail
        else:
            assert proposal.solver_status in ("OPTIMAL", "INFEASIBLE"), proposal.detail
            assert proposal.binding_constraint is None or proposal.binding_constraint.value in (
                "capacity",
                "latest_start",
                "in_water",
            )
    assert statuses, "BT-01 must have at least one conflicted component"
