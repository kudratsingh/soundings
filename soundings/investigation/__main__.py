"""Dump the evidence table of one simulated scenario as canonical JSON.

    python -m soundings.investigation --scenario S05 --seed 11

Resolves the scenario, runs the WO-011 sim, loads ``data/pol_table.json`` and prints
``EvidenceTable.to_json()``. The table reads only the run's observed tracks and tick
length (``build_evidence``); the scenario supplies its id and clock hour. Same scenario
and seed, same bytes.

The simulator wiring lives in the ``soundings.dumps`` composition root, imported only when
the command runs; the investigation package imports nothing from ``soundings.sim``.
"""

from __future__ import annotations


def main(argv: list[str] | None = None) -> int:
    from soundings.dumps import evidence_dump

    return evidence_dump(argv)


if __name__ == "__main__":
    raise SystemExit(main())
