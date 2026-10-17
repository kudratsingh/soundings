"""WO-081 stand-in for BT-01's 40 filed plans until WO-080 part A lands on main.

``bt01_or_standin()`` returns ``scenarios/BT-01.yaml`` when it exists (switch-over is
automatic) and otherwise a seeded, equivalent 40-plan day built from S01 with the
registry's own types: 40 plans over 8 h at tick_s 5, hour_of_day 7, every declared zone,
1-3 h windows. ``model_copy`` skips scenario validation on purpose: the stand-in only
feeds the registry (plans), never the simulator.
"""

from __future__ import annotations

import random

from soundings.schemas.harbor import load_harbor
from soundings.schemas.scenario import PlanWindow, RegistryPlan, Scenario
from soundings.sim.scenario import UnknownScenarioError, resolve_scenario

BT01_JOBS = 40
BT01_TICK_S = 5.0
BT01_HOURS = 8


def busy_standin(seed: int = 7, jobs: int = BT01_JOBS) -> Scenario:
    rng = random.Random(seed)
    base = resolve_scenario("S01")
    zones = sorted(z.zone_id for z in load_harbor().zones)
    ticks_per_hour = int(3600 / BT01_TICK_S)
    duration = BT01_HOURS * ticks_per_hour
    plans = []
    for i in range(jobs):
        length = rng.randint(1 * ticks_per_hour, 3 * ticks_per_hour)
        start = rng.randint(0, duration - length)
        plans.append(
            RegistryPlan(
                plan_id=f"P-BT-{i + 1:03d}",
                operator=f"Harbor Works {i % 7} (demo)",
                vehicle=f"ROV-{i + 1:03d}",
                modem_id=f"MOD-{2000 + i}",
                area=zones[rng.randrange(len(zones))],
                window=PlanWindow(start_tick=start, end_tick=start + length),
                contact="sim-phone-operator",
            )
        )
    return base.model_copy(
        update={
            "id": "BT-01",
            "duration_ticks": duration,
            "tick_s": BT01_TICK_S,
            "hour_of_day": 7,
            "registry_plans": tuple(plans),
        }
    )


def bt01_available() -> bool:
    try:
        resolve_scenario("BT-01")
    except (UnknownScenarioError, ValueError):
        return False
    return True


def bt01_or_standin() -> Scenario:
    """The real BT-01 once WO-080 part A merges; the seeded stand-in until then."""
    if bt01_available():
        return resolve_scenario("BT-01")
    return busy_standin()
