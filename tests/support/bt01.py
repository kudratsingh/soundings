"""BT-01 helpers for the WO-085 surge-triage tests.

``bt01()`` loads ``tests/fixtures/triage_bt_synthetic.yaml`` (eleven filed jobs that clear
at ask and one unfiled ``rogue-1`` closing on ``fuel_berth_2``, S05's track) because
BT-01 (WO-080 part A) was not on main when WO-085 merged. ``SOUNDINGS_TRIAGE_BT01=1``
runs the same assertions on ``scenarios/BT-01.yaml`` once it exists (opt-in: two BT-01
runs take minutes and the WO-085 Done section records how the clauses read there).
``BT01_SOURCE`` says which scenario is in use.

``python -c "from tests.support.bt01 import main; main()"`` prints the triage log of
``bt01_with_surge(25)`` and both hashes (the PYTHONHASHSEED diff of the exit test).
"""

from __future__ import annotations

import inspect
import math
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from soundings.runner import RunResultV2, ScenarioRunnerV2
from soundings.schemas.enums import LadderState, ToggleName
from soundings.schemas.harbor import load_harbor
from soundings.schemas.scenario import Scenario, Toggle
from soundings.sim.scenario import resolve_scenario
from soundings.triage import TriageConfig, load_triage_config, triage_log_text

REPO = Path(__file__).resolve().parents[2]
BT01_PATH = REPO / "scenarios" / "BT-01.yaml"
SYNTHETIC_PATH = REPO / "tests" / "fixtures" / "triage_bt_synthetic.yaml"
USE_BT01 = os.environ.get("SOUNDINGS_TRIAGE_BT01") == "1" and BT01_PATH.exists()
BT01_SOURCE = "BT-01" if USE_BT01 else "synthetic"
ROGUE = "rogue-1"
RUN_SEED = 7
SURGE_LEAD_S = 60.0  # the surge lands one minute before the rogue starts (WO-085)


def bt01() -> Scenario:
    """The synthetic stand-in, or ``scenarios/BT-01.yaml`` with ``SOUNDINGS_TRIAGE_BT01=1``."""
    path = BT01_PATH if USE_BT01 else SYNTHETIC_PATH
    return resolve_scenario(str(path), load_harbor())


def rogue_start_tick(scenario: Scenario) -> int:
    for c in scenario.contacts:
        if c.id == ROGUE:
            return int(getattr(c, "start_tick", 0) or 0)
    raise KeyError(ROGUE)


def bt01_with_surge(n: int = 25) -> Scenario:
    sc = bt01()
    lead = math.ceil(SURGE_LEAD_S / float(sc.tick_s))
    at = max(0, rogue_start_tick(sc) - lead)
    surge = Toggle(name=ToggleName.CONTACT_SURGE, n=n, at_tick=at)
    return sc.model_copy(update={"toggles": (*sc.toggles, surge)})


def run(
    scenario: Scenario, *, triage: TriageConfig | None = None, seed: int = RUN_SEED
) -> RunResultV2:
    """Runner v2 at ``seed`` with ``llm_roles=False`` (``skip_idle_ticks=True`` when the
    runner has it, WO-080 part B)."""
    cfg = load_triage_config() if triage is None else triage
    kwargs: dict[str, Any] = {"seed": seed, "triage": cfg}
    if "skip_idle_ticks" in inspect.signature(ScenarioRunnerV2.__init__).parameters:
        kwargs["skip_idle_ticks"] = True
    return ScenarioRunnerV2(scenario, **kwargs).run(llm_roles=False)


def interruptions_per_hour(result: RunResultV2) -> float:
    return result.interruptions / (result.sim.n_ticks * float(result.sim.tick_s) / 3600.0)


def lineage(result: RunResultV2, gt_id: str = ROGUE) -> list[str]:
    """Observed contact ids of one ground-truth contact, in first-case order."""
    truth = result.sim.contact_truth
    opened: dict[str, int] = {}
    for c in result.cases:
        if c.contact_id in truth and truth[c.contact_id].gt_id == gt_id:
            opened.setdefault(c.contact_id, c.opened_tick)
    return [cid for cid, _ in sorted(opened.items(), key=lambda kv: (kv[1], kv[0]))]


def lineage_end_case(result: RunResultV2, gt_id: str = ROGUE) -> str:
    """The last case of the lineage's last observed contact (WO-022's S02 convention)."""
    ids = lineage(result, gt_id)
    if not ids:
        raise KeyError(gt_id)
    end = ids[-1]
    cases = [c for c in result.cases if c.contact_id == end]
    return cases[-1].case_id


def lineage_terminal(result: RunResultV2, gt_id: str = ROGUE) -> LadderState:
    return result.case(lineage_end_case(result, gt_id)).state


def handed_off_tick(result: RunResultV2, case_id: str) -> int:
    for t in result.transitions:
        if t.case_id == case_id and t.to_state is LadderState.HANDED_OFF:
            return t.tick
    raise KeyError(case_id)


def paid_probes(result: RunResultV2, case_id: str) -> int:
    """The ask-rung hails (budget scope, DECISIONS 23:05) plus paid investigation probes."""
    hails = len(set(result.case(case_id).hail_keys))
    paid = sum(
        1 for c in result.probe_calls if c.case_id == case_id and c.cost_usd > 0 and not c.cached
    )
    return hails + paid


def main(argv: Sequence[str] | None = None) -> int:
    del argv
    result = run(bt01_with_surge(25))
    sys.stdout.write(triage_log_text(result.triage_log))
    print(f"triage_log_hash={result.triage_log_hash}")
    print(f"transition_log_hash={result.transition_log_hash}")
    return 0


__all__ = [
    "BT01_SOURCE",
    "ROGUE",
    "bt01",
    "bt01_with_surge",
    "handed_off_tick",
    "interruptions_per_hour",
    "lineage",
    "lineage_end_case",
    "lineage_terminal",
    "main",
    "paid_probes",
    "run",
]
