#!/usr/bin/env python3
"""Gate for any paid model run. Prints the estimate, then requires the owner to
type the exact approval phrase on stdin. Exits non-zero otherwise.

Usage:
    python scripts/paid-run-gate.py --role probe_planner --calls 40 \
        --in-tokens 2500 --out-tokens 200 --desc "record S04-S08 planner fixtures"

Then run the recording command only if this exits 0.

Stage live mode (WO-087), Sunday only:
    python scripts/paid-run-gate.py --role stage_live --calls 40 \
        --in-tokens 2500 --out-tokens 300
asks for exactly ``approve paid run stage live budget 40`` and, once typed, prints the
environment for the stage run (``MODEL_MODE=live LIVE_CALL_BUDGET=40``).
Pricing: set SOUNDINGS_USD_PER_MTOK_IN / _OUT in the environment from the
provider's current price list; the script refuses to estimate without them.
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import pathlib
import sys

STAGE_LIVE_ROLE = "stage_live"  # WO-087: the Sunday enable step for bounded live mode


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--role", required=True)
    p.add_argument("--calls", type=int, required=True)
    p.add_argument("--in-tokens", type=int, required=True, help="avg input tokens per call")
    p.add_argument("--out-tokens", type=int, required=True, help="avg output tokens per call")
    p.add_argument("--desc", default=None)
    a = p.parse_args()
    # --- WO-087 begin: stage live wording ---
    stage_live = a.role == STAGE_LIVE_ROLE
    if stage_live:
        a.desc = f"stage live budget {a.calls}"
    elif not a.desc:
        p.error("--desc is required")
    # --- WO-087 end ---

    if os.environ.get("SOUNDINGS_CI"):
        print("paid runs are forbidden in CI (CLAUDE.md §2.3)", file=sys.stderr)
        return 2

    try:
        usd_in = float(os.environ["SOUNDINGS_USD_PER_MTOK_IN"])
        usd_out = float(os.environ["SOUNDINGS_USD_PER_MTOK_OUT"])
    except KeyError:
        print(
            "set SOUNDINGS_USD_PER_MTOK_IN and SOUNDINGS_USD_PER_MTOK_OUT"
            " from the provider price list first",
            file=sys.stderr,
        )
        return 2

    model = os.environ.get("MODEL_NAME", "<MODEL_NAME unset>")
    est = a.calls * (a.in_tokens * usd_in + a.out_tokens * usd_out) / 1_000_000
    print("=== PAID RUN REQUEST ===")
    print(f"role:        {a.role}")
    print(f"description: {a.desc}")
    print(f"model:       {model}")
    print(f"calls:       {a.calls}  (~{a.in_tokens} in / ~{a.out_tokens} out tokens each)")
    print(f"estimate:    ${est:.4f}")
    # --- WO-087 begin: stage live wording ---
    if stage_live:
        print(f"mode:        MODEL_MODE=live LIVE_CALL_BUDGET={a.calls} for the stage run only")
        print(
            "bounds:      hard cap of "
            f"{a.calls} calls (artifacts/live_calls.json), 6 s per call, any error or"
            " timeout -> deterministic fallback; the badge shows model: live or model: fallback"
        )
        print("mode after:  MODEL_MODE=replay (unset LIVE_CALL_BUDGET after the stage run)")
    else:
        print("mode after:  MODEL_MODE=replay (fixtures recorded to tests/fixtures/model_calls/)")
    # --- WO-087 end ---
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
        f.write(
            f"| {stamp} | {a.role} | {a.calls} | ${est:.4f} | {model} | {a.desc}"
            " | approved by owner |\n"
        )
    # --- WO-087 begin: stage live wording ---
    if stage_live:
        print(
            "approved; logged to docs/log/PAID-RUNS.md."
            f" Start the stage run with MODEL_MODE=live LIVE_CALL_BUDGET={a.calls}."
        )
        return 0
    # --- WO-087 end ---
    print(
        "approved; logged to docs/log/PAID-RUNS.md."
        " Run the recording command now with MODEL_MODE=record."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
