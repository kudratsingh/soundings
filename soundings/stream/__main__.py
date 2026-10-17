"""``python -m soundings.stream --scenario S02 --seed 1``: the event stream as canonical JSONL.

Unpaced (speed 0) and deterministic: two runs with the same arguments print identical
bytes. It serves debugging, the determinism diff and the demo runner (WO-062).
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from soundings.schemas.enums import ToggleName  # WO-060B
from soundings.stream.engine import (
    UnsupportedOption,
    stream_events,
    v3_stream_events,  # WO-060B
)
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
    # --- WO-060B begin: --source v3 (runner v3 stepped per tick) ---
    parser.add_argument("--source", default="batch", choices=["batch", "v3"])
    args = parser.parse_args(argv)
    if args.source == "v3":
        events = v3_stream_events(
            args.scenario,
            args.seed,
            run_id=args.run_id,
            toggles=[ToggleName(t) for t in args.toggles.split(",") if t],
        )
    elif args.toggles:
        raise SystemExit(str(UnsupportedOption("the batch source on runner v1 takes no toggles")))
    else:
        events = stream_events(args.scenario, args.seed, run_id=args.run_id)
    # --- WO-060B end ---
    out = sys.stdout
    for event in events:
        out.write(event_json(event))
        out.write("\n")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
