"""One clock interface for every ladder timer (docs/WORKFLOW.md section 8).

``TickClock`` is the run clock in the sim, in tests and in replay: time is
``tick * tick_s`` and nothing else. ``WallClock`` exists only for the live UI mode and is
the single wall-clock read under ``soundings/`` outside the API (CLAUDE.md 2.4; an AST
scan in ``tests/test_clock.py`` holds this line). This module is standalone: it imports
nothing from ``soundings.sim``, which keeps its own internal tick counter.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


@runtime_checkable
class Clock(Protocol):
    """Anything that tells the time in seconds since its own start."""

    def now_s(self) -> float: ...


@dataclass
class TickClock:
    """The deterministic run clock: ``now_s() == tick * tick_s``."""

    tick_s: float
    tick: int = 0

    def __post_init__(self) -> None:
        if not self.tick_s > 0:
            raise ValueError("tick_s must be positive")
        if self.tick < 0:
            raise ValueError("tick must be non-negative")

    def now_s(self) -> float:
        return self.tick * self.tick_s

    def advance(self, n: int = 1) -> int:
        """Move ``n`` ticks forward (never backward) and return the new tick."""
        if n < 0:
            raise ValueError("a clock never moves backward")
        self.tick += n
        return self.tick


class WallClock:
    """Live UI mode only: seconds since construction on the monotonic clock."""

    def __init__(self) -> None:
        self._start = time.monotonic()

    def now_s(self) -> float:
        return time.monotonic() - self._start


@dataclass(frozen=True)
class Timer:
    """A named deadline (run-clock seconds) for one contact."""

    name: str
    contact_id: str
    deadline_s: float

    @property
    def sort_key(self) -> tuple[float, str, str]:
        """Due order: deadline first, then name, then contact id (deterministic ties)."""
        return (self.deadline_s, self.name, self.contact_id)


@dataclass
class TimerSet:
    """Pending timers, at most one per (name, contact_id); iteration is always sorted."""

    _timers: dict[tuple[str, str], Timer] = field(default_factory=dict)

    def arm(self, timer: Timer) -> None:
        """Arm ``timer``, replacing any pending timer with the same name and contact."""
        self._timers[(timer.name, timer.contact_id)] = timer

    def cancel(self, name: str, contact_id: str) -> bool:
        """Cancel one timer; return whether it was pending."""
        return self._timers.pop((name, contact_id), None) is not None

    def cancel_contact(self, contact_id: str) -> None:
        """Cancel every pending timer of one contact."""
        for key in [k for k in self._timers if k[1] == contact_id]:
            del self._timers[key]

    def pending(self) -> tuple[Timer, ...]:
        """Every pending timer sorted by (deadline, name, contact_id)."""
        return tuple(sorted(self._timers.values(), key=lambda t: t.sort_key))

    def due(self, clock: Clock) -> list[Timer]:
        """Timers whose deadline has passed, sorted by (deadline, name, contact_id)."""
        now = clock.now_s()
        return [t for t in self.pending() if t.deadline_s <= now]

    def pop(self, timer: Timer) -> None:
        """Remove ``timer`` (it fired)."""
        if self._timers.get((timer.name, timer.contact_id)) == timer:
            del self._timers[(timer.name, timer.contact_id)]

    def __len__(self) -> int:
        return len(self._timers)
