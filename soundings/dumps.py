"""Developer dump CLIs that run the simulator and print what the investigation makes of it.

This is a composition root: it is the one place that wires ``soundings.sim`` (scenario,
run, current field) to ``soundings.investigation`` (evidence table, posterior) for a
command line. The investigation package imports nothing from the simulator; its two
``python -m`` entry points (``soundings.investigation`` and
``soundings.investigation.posterior``) hand off to the functions here at call time.

* ``evidence_dump``: ``python -m soundings.investigation --scenario S05 --seed 11`` prints
  ``EvidenceTable.to_json()``.
* ``posterior_dump``: ``python -m soundings.investigation.posterior --scenario S05
  --seed 11`` prints every contact's posterior report as canonical JSON.

Both build the table from the run's observed tracks and tick length only
(``build_evidence``); the scenario supplies its id and clock hour. Same scenario and
seed, same bytes.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from soundings.canonical import canonical_json
from soundings.investigation.evidence import EvidenceTable
from soundings.investigation.features import build_evidence
from soundings.investigation.pol import DEFAULT_POL_PATH, load_pol_table
from soundings.investigation.posterior import (
    STRESS_TEST_LABEL,
    load_likelihood_tables,
    load_prior_config,
    score_run_table,
)
from soundings.schemas.harbor import load_harbor
from soundings.sim.current import load_current_field, tide_phase_for_hour
from soundings.sim.run import run_scenario
from soundings.sim.scenario import UnknownScenarioError, resolve_scenario
from soundings.sim.types import SimRun

EXIT_UNKNOWN_SCENARIO = 2


def _parser(prog: str, description: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=prog, description=description)
    parser.add_argument("--scenario", required=True, help="catalog id (S05), 'smoke', or a path")
    parser.add_argument("--seed", type=int, default=None, help="default: the scenario's seed")
    parser.add_argument("--ticks", type=int, default=None, help="default: the scenario length")
    parser.add_argument("--pol", type=Path, default=DEFAULT_POL_PATH, help="pattern-of-life table")
    parser.add_argument(
        "--no-pol", action="store_true", help="build without a table (pattern of life unknown)"
    )
    return parser


def _simulate_and_tabulate(args: argparse.Namespace) -> tuple[SimRun, EvidenceTable] | None:
    """Run the named scenario and build its evidence table; None for an unknown scenario."""
    harbor = load_harbor()
    try:
        scenario = resolve_scenario(args.scenario, harbor)
    except UnknownScenarioError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return None
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
    return run, table


def evidence_dump(argv: Sequence[str] | None = None) -> int:
    """Print the evidence table of a simulated scenario as canonical JSON."""
    args = _parser(
        "python -m soundings.investigation",
        "Print the evidence table of a simulated scenario as canonical JSON.",
    ).parse_args(None if argv is None else list(argv))
    built = _simulate_and_tabulate(args)
    if built is None:
        return EXIT_UNKNOWN_SCENARIO
    _, table = built
    sys.stdout.write(table.to_json() + "\n")
    return 0


def posterior_dump(argv: Sequence[str] | None = None) -> int:
    """Print every contact's posterior report for a simulated scenario."""
    args = _parser(
        "python -m soundings.investigation.posterior",
        "Print every contact's posterior report for a simulated scenario.",
    ).parse_args(None if argv is None else list(argv))
    built = _simulate_and_tabulate(args)
    if built is None:
        return EXIT_UNKNOWN_SCENARIO
    run, table = built
    reports = score_run_table(table, load_likelihood_tables(), load_prior_config())
    doc = {
        "label": STRESS_TEST_LABEL,
        "scenario": run.scenario_id,
        "seed": run.seed,
        "contacts": [r.to_dict() for r in reports],
    }
    sys.stdout.write(canonical_json(doc) + "\n")
    return 0


__all__ = ["EXIT_UNKNOWN_SCENARIO", "evidence_dump", "posterior_dump"]
