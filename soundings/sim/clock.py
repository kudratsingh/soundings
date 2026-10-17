"""Tick clock. Sim time is ``tick * tick_s``; nothing in ``soundings/sim`` reads a wall clock."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class TickClock:
    """A counter of whole ticks with a fixed tick length in seconds."""

    tick_s: float
    tick: int = 0

    def __post_init__(self) -> None:
        if self.tick_s <= 0:
            raise ValueError("tick_s must be > 0")
        if self.tick < 0:
            raise ValueError("tick must be >= 0")

    @property
    def t_s(self) -> float:
        """Sim time in seconds at the current tick."""
        return self.tick * self.tick_s

    def advance(self, n: int = 1) -> int:
        """Move forward ``n`` ticks and return the new tick."""
        if n < 0:
            raise ValueError("n must be >= 0")
        self.tick += n
        return self.tick
