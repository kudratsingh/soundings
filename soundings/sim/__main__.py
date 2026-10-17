"""CLI: ``python -m soundings.sim --seed 7 --ticks 60 --scenario smoke``."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Sequence

from soundings.sim.smoke import format_rows, run_smoke

SCENARIOS: dict[str, Callable[[int, int], str]] = {
    "smoke": lambda seed, ticks: format_rows(run_smoke(seed, ticks)),
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m soundings.sim", description=__doc__)
    parser.add_argument("--seed", type=int, required=True, help="RNG seed (required)")
    parser.add_argument("--ticks", type=int, default=60, help="number of ticks (default 60)")
    parser.add_argument("--scenario", type=str, default="smoke", help="scenario name")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    runner = SCENARIOS.get(args.scenario)
    if runner is None:
        known = ", ".join(sorted(SCENARIOS))
        print(f"unknown scenario {args.scenario!r}; known: {known}", file=sys.stderr)
        return 2
    if args.ticks < 0:
        print("--ticks must be >= 0", file=sys.stderr)
        return 2
    sys.stdout.write(runner(args.seed, args.ticks))
    return 0


if __name__ == "__main__":
    sys.exit(main())
