"""Judge injection (WO-061): the documented mapping from a judge's choice to a live scenario.

The implementation is ``soundings.sim.live_scenario`` (scenario authoring belongs to the
simulator); this module is the interface the WO names. A request carries a position, a
behavior, an acoustic profile and an optional seed, nothing that names a rung or a state.
"""

from __future__ import annotations

from soundings.sim.live_scenario import (
    BEHAVIOR_TABLE,
    AcousticProfile,
    BehaviorRow,
    InjectRequest,
    JudgeBehavior,
    OutsideZonesError,
    build_live_scenario,
    critical_asset,
    live_dir,
    live_seed,
    load_known_ops,
    scenario_yaml,
    stamp_of,
    write_live_scenario,
)

__all__ = [
    "BEHAVIOR_TABLE",
    "AcousticProfile",
    "BehaviorRow",
    "InjectRequest",
    "JudgeBehavior",
    "OutsideZonesError",
    "build_live_scenario",
    "critical_asset",
    "live_dir",
    "live_seed",
    "load_known_ops",
    "scenario_yaml",
    "stamp_of",
    "write_live_scenario",
]
