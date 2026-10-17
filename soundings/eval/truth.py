"""Ground-truth lookups for grading and presentation, kept inside ``soundings.eval``.

Outside the simulator, only ``soundings.eval`` (and the tests) may read a run's ground
truth (``tests/test_boundary.py``). Code elsewhere that needs a graded fact asks here and
gets back an answer with no hypothesis label in it.
"""

from __future__ import annotations

from soundings.sim.types import FALSE_ALARM_PREFIX, SimRun


def physical_contact_ids(run: SimRun) -> frozenset[str]:
    """Observed ids with something physical behind them (not a sensor false alarm)."""
    return frozenset(
        cid
        for cid, truth in run.contact_truth.items()
        if not truth.gt_id.startswith(FALSE_ALARM_PREFIX)
    )


__all__ = ["physical_contact_ids"]
