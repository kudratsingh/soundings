"""``python -m soundings.ladder``: replay a ladder run and print each ``LadderRun`` as JSON.

WO-043 part A replays the synthetic S05-shaped feed (``soundings.ladder.synthetic``);
part B swaps in runner v3 for every catalog scenario. With no ``--orders`` the
unsigned-orders guard set decides (nothing above ``ask`` is pre-authorized).

``--audit-db`` writes a plain SQLite log of every checkpoint and action (a stand-in until
WO-042's audit sink is wired); ``--sitrep-out`` writes every rendered SITREP.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

from soundings.canonical import canonical_json
from soundings.schemas import ToggleName, load_harbor

from .driver import LadderDriver, LadderRun
from .synthetic import S05_HOUR, S05_TICK_S, s05_feed
from .watch import DepartureDetector

SCENARIOS = ("S05",)


def write_audit_db(path: Path, runs: Mapping[str, LadderRun]) -> None:
    """One row per checkpoint and per action, contacts in id order; replaces the file."""
    if path.exists():
        path.unlink()
    con = sqlite3.connect(path)
    try:
        con.execute(
            "CREATE TABLE ladder_audit (seq INTEGER PRIMARY KEY, contact_id TEXT, "
            "kind TEXT, tick INTEGER, body TEXT)"
        )
        seq = 0
        for cid in sorted(runs):
            run = runs[cid]
            for cp in run.checkpoints:
                con.execute(
                    "INSERT INTO ladder_audit VALUES (?, ?, ?, ?, ?)",
                    (seq, cid, "checkpoint", cp.tick, canonical_json(cp)),
                )
                seq += 1
            for action in run.actions:
                con.execute(
                    "INSERT INTO ladder_audit VALUES (?, ?, ?, ?, ?)",
                    (seq, cid, "action", action.tick, canonical_json(action)),
                )
                seq += 1
        con.commit()
    finally:
        con.close()


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m soundings.ladder", description=__doc__)
    ap.add_argument("--scenario", default="S05", choices=SCENARIOS)
    ap.add_argument("--seed", type=int, default=11, help="recorded; the part-A feed is fixed")
    ap.add_argument("--orders", type=Path, default=None, help="standing orders YAML")
    ap.add_argument("--unsigned", action="store_true", help="ignore the standing orders")
    ap.add_argument("--toggle", action="append", default=[], choices=[t.value for t in ToggleName])
    ap.add_argument("--sitrep-out", type=Path, default=None)
    ap.add_argument("--audit-db", type=Path, default=None)
    args = ap.parse_args(argv)

    detector = DepartureDetector.from_config(load_harbor(), S05_TICK_S)
    driver = LadderDriver.from_config(
        tick_s=S05_TICK_S,
        hour_of_day=S05_HOUR,
        orders_path=args.orders,
        unsigned=args.unsigned,
        toggles=args.toggle,
        departure=detector,
    )
    runs = driver.run([s05_feed()])
    doc = {
        "scenario": args.scenario,
        "seed": args.seed,
        "orders": "unsigned" if args.unsigned or args.orders is None else "signed_file",
        "toggles": sorted(args.toggle),
        "runs": [runs[cid].canonical() for cid in sorted(runs)],
    }
    sys.stdout.write(canonical_json(doc) + "\n")
    if args.sitrep_out is not None:
        texts = [s.text for cid in sorted(runs) for s in runs[cid].sitreps]
        args.sitrep_out.write_text("\n".join(texts), encoding="utf-8")
    if args.audit_db is not None:
        write_audit_db(args.audit_db, runs)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
