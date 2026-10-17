#!/usr/bin/env python3
"""Gate for any paid model run. Prints the estimate, then requires the owner to
type the exact approval phrase on stdin. Exits non-zero otherwise.

Usage:
    python scripts/paid-run-gate.py --role probe_planner --calls 40 \
        --in-tokens 2500 --out-tokens 200 --desc "record S04-S08 planner fixtures"

Then run the recording command only if this exits 0.
Pricing: set SOUNDINGS_USD_PER_MTOK_IN / _OUT in the environment from the
provider's current price list; the script refuses to estimate without them.
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import pathlib
import sys


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--role", required=True)
    p.add_argument("--calls", type=int, required=True)
    p.add_argument("--in-tokens", type=int, required=True, help="avg input tokens per call")
    p.add_argument("--out-tokens", type=int, required=True, help="avg output tokens per call")
    p.add_argument("--desc", required=True)
    a = p.parse_args()

    if os.environ.get("SOUNDINGS_CI"):
        print("paid runs are forbidden in CI (CLAUDE.md §2.3)", file=sys.stderr)
        return 2

    try:
        usd_in = float(os.environ["SOUNDINGS_USD_PER_MTOK_IN"])
        usd_out = float(os.environ["SOUNDINGS_USD_PER_MTOK_OUT"])
    except KeyError:
        print("set SOUNDINGS_USD_PER_MTOK_IN and SOUNDINGS_USD_PER_MTOK_OUT from the provider price list first", file=sys.stderr)
        return 2

    model = os.environ.get("MODEL_NAME", "<MODEL_NAME unset>")
    est = a.calls * (a.in_tokens * usd_in + a.out_tokens * usd_out) / 1_000_000
    print("=== PAID RUN REQUEST ===")
    print(f"role:        {a.role}")
    print(f"description: {a.desc}")
    print(f"model:       {model}")
    print(f"calls:       {a.calls}  (~{a.in_tokens} in / ~{a.out_tokens} out tokens each)")
    print(f"estimate:    ${est:.4f}")
    print("mode after:  MODEL_MODE=replay (fixtures recorded to tests/fixtures/model_calls/)")
    phrase = f"approve paid run {a.desc}"
    print(f"\nType exactly:  {phrase}")
    try:
        typed = input("> ").strip()
    except EOFError:
        typed = ""
    if typed != phrase:
        print("not approved; nothing was run", file=sys.stderr)
        return 1

    log = pathlib.Path("docs/log/PAID-RUNS.md")
    log.parent.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().astimezone().isoformat(timespec="minutes")
    with log.open("a") as f:
        f.write(f"| {stamp} | {a.role} | {a.calls} | ${est:.4f} | {model} | {a.desc} | approved by owner |\n")
    print("approved; logged to docs/log/PAID-RUNS.md. Run the recording command now with MODEL_MODE=record.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
