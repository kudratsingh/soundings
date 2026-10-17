"""Dump the evidence table of one simulated scenario as canonical JSON.

    python -m soundings.investigation --scenario S05 --seed 11

Resolves the scenario, runs the WO-011 sim, loads ``data/pol_table.json`` and prints
``EvidenceTable.to_json()``. The table reads only the run's observed tracks and tick
length (``build_evidence``); the scenario supplies its id and clock hour. Same scenario
and seed, same bytes.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from soundings.investigation.features import build_evidence
from soundings.investigation.pol import DEFAULT_POL_PATH, load_pol_table
from soundings.schemas.harbor import load_harbor
from soundings.sim.current import load_current_field, tide_phase_for_hour
from soundings.sim.run import run_scenario
from soundings.sim.scenario import UnknownScenarioError, resolve_scenario


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m soundings.investigation",
        description="Print the evidence table of a simulated scenario as canonical JSON.",
    )
    parser.add_argument("--scenario", required=True, help="catalog id (S05), 'smoke', or a path")
    parser.add_argument("--seed", type=int, default=None, help="default: the scenario's seed")
    parser.add_argument("--ticks", type=int, default=None, help="default: the scenario length")
    parser.add_argument("--pol", type=Path, default=DEFAULT_POL_PATH, help="pattern-of-life table")
    parser.add_argument(
        "--no-pol", action="store_true", help="build without a table (pattern of life unknown)"
    )
    args = parser.parse_args(argv)

    harbor = load_harbor()
    try:
        scenario = resolve_scenario(args.scenario, harbor)
    except UnknownScenarioError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    run = run_scenario(scenario, seed=args.seed, ticks=args.ticks, harbor=harbor)
    pol = None if args.no_pol else load_pol_table(args.pol)
    field = load_current_field()
    table = build_evidence(
        run,
        harbor,
        field,
        pol,
        hour_of_day=scenario.hour_of_day,
        tide_phase_for_hour=lambda hour: tide_phase_for_hour(hour, field),
    )
    sys.stdout.write(table.to_json() + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
