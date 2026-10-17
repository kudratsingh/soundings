"""Seeded, tick-based simulator. Same seed, same inputs, byte-identical output.

Phase 0 ships only the ``smoke`` scenario; Phase 1 replaces it with the world model
described in docs/SPEC-SIM.md.
"""

from soundings.sim.smoke import COLUMNS, SmokeRow, format_rows, run_smoke

__all__ = ["COLUMNS", "SmokeRow", "format_rows", "run_smoke"]
