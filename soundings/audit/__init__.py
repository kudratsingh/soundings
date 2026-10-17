"""``python -m soundings.audit verify [--db PATH]``: recompute the audit hash chain.

Prints ``ok <n> rows`` and exits 0 when the chain holds, ``broken at seq <k>`` and exits 1
when it does not, and exits 2 when there is no database at the path. The table and its
rules live in :mod:`soundings.ladder.audit`.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from soundings.ladder.audit import AuditLog
from soundings.registry.store import db_path_from_env


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m soundings.audit")
    sub = parser.add_subparsers(dest="command", required=True)
    verify = sub.add_parser("verify", help="recompute the hash chain of the audit table")
    verify.add_argument("--db", type=Path, default=None, help="SQLite file (default $SOUNDINGS_DB)")
    args = parser.parse_args(argv)

    db: Path = args.db if args.db is not None else db_path_from_env()
    if not db.is_file():
        print(f"no audit database at {db}", file=sys.stderr)
        return 2
    log = AuditLog(db)
    try:
        result = log.verify()
    finally:
        log.close()
    if result.ok:
        print(f"ok {result.rows} rows")
        return 0
    print(f"broken at seq {result.first_bad_seq}")
    return 1


__all__ = ["main"]
