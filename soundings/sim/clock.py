"""Tick clock. Sim time is ``tick * tick_s``; nothing in ``soundings/sim`` reads a wall clock.

One tick clock serves the sim and the ladder: ``TickClock`` lives in ``soundings.clock``
(WO-040) and is re-exported here so ``soundings.sim.clock.TickClock`` keeps working.
"""

from __future__ import annotations

from soundings.clock import TickClock

__all__ = ["TickClock"]
