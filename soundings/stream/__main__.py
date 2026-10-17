"""``python -m soundings.stream --scenario S02 --seed 1``: the event stream as canonical JSONL.

Unpaced (speed 0) and deterministic: two runs with the same arguments print identical
bytes. It serves debugging, the determinism diff and the demo runner (WO-062).
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from soundings.stream.engine import UnsupportedOption, stream_events
from soundings.stream.events import event_json


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m soundings.stream", description=__doc__)
    parser.add_argument("--scenario", required=True, help="catalog id such as S02")
    parser.add_argument("--seed", type=int, default=None, help="default: the scenario's seed")
    parser.add_argument("--run-id", default="cli")
    parser.add_argument(
        "--approve-first",
        action="store_true",
        help="for the stepped source (WO-061); the batch source has nothing to approve",
    )
    parser.add_argument("--toggles", default="", help="comma list; runner v1 takes none")
    args = parser.parse_args(argv)
    if args.toggles:
        raise SystemExit(str(UnsupportedOption("the batch source on runner v1 takes no toggles")))
    out = sys.stdout
    for event in stream_events(args.scenario, args.seed, run_id=args.run_id):
        out.write(event_json(event))
        out.write("\n")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
