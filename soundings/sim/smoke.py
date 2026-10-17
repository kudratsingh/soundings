"""Smoke run for ``make sim``: a thin wrapper over ``scenarios/smoke.yaml``.

One ``transit`` and one ``stationary`` contact, seen by a one-node harbor subset
(``hyd_1``) with biologic false alarms set to 0, so the output is short and readable.
The smoke file is not part of the catalog (``scenarios/S[0-9][0-9]-*.yaml``).
"""

from __future__ import annotations

from dataclasses import replace

from soundings.schemas.harbor import Harbor, load_harbor
from soundings.sim.detection import load_detection_params
from soundings.sim.run import run_scenario
from soundings.sim.scenario import SMOKE_PATH, resolve_scenario
from soundings.sim.types import SimRun

SMOKE_NODE_ID = "hyd_1"


def smoke_harbor(harbor: Harbor | None = None) -> Harbor:
    """The harbor with only the ``hyd_1`` sensor node."""
    h = harbor if harbor is not None else load_harbor()
    return h.model_copy(update={"sensor_nodes": (h.node(SMOKE_NODE_ID),)})


def run_smoke(seed: int | None = None, ticks: int | None = None) -> SimRun:
    """Run ``scenarios/smoke.yaml`` on the one-node harbor with no false alarms."""
    harbor = load_harbor()
    scenario = resolve_scenario(SMOKE_PATH, harbor)
    params = replace(load_detection_params(), false_alarms_per_node_hour=0.0)
    return run_scenario(
        scenario, seed=seed, ticks=ticks, harbor=smoke_harbor(harbor), params=params
    )
