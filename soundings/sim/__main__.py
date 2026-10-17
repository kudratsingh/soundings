"""CLI: ``python -m soundings.sim --seed 7 --ticks 60 --scenario smoke``.

``--scenario`` takes ``smoke``, a catalog id such as ``S04``, or a path to a scenario
file. Prints one line per tick (observed readings only), then ``track_hash=<hex>``.
Exit code 2 for an unknown or invalid scenario or bad arguments.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from pydantic import ValidationError

from soundings.sim.hashing import track_hash
from soundings.sim.run import format_run, run_scenario
from soundings.sim.scenario import UnknownScenarioError, resolve_scenario
from soundings.sim.smoke import run_smoke
from soundings.sim.types import SimRun


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m soundings.sim", description=__doc__)
    parser.add_argument("--seed", type=int, required=True, help="RNG seed (required)")
    parser.add_argument(
        "--ticks", type=int, default=None, help="number of ticks (default: the scenario's)"
    )
    parser.add_argument(
        "--scenario", type=str, default="smoke", help="smoke, a catalog id (S04), or a path"
    )
    return parser


def run_ref(ref: str, seed: int, ticks: int | None) -> SimRun:
    """Run a scenario reference the way the CLI does (``smoke`` uses the smoke harbor)."""
    if ref == "smoke":
        return run_smoke(seed, ticks)
    return run_scenario(resolve_scenario(ref), seed=seed, ticks=ticks)


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.ticks is not None and args.ticks < 0:
        print("--ticks must be >= 0", file=sys.stderr)
        return 2
    try:
        run = run_ref(args.scenario, args.seed, args.ticks)
    except UnknownScenarioError as exc:
        print(f"unknown scenario {args.scenario!r}: {exc}", file=sys.stderr)
        return 2
    except (ValidationError, ValueError) as exc:
        print(f"invalid scenario {args.scenario!r}: {exc}", file=sys.stderr)
        return 2
    sys.stdout.write(format_run(run, track_hash(run)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
