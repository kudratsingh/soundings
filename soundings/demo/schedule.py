"""Deterministic step scheduler for the demo (WO-062 Part A).

The script's time marks are compressed into a configurable total with a speed factor.
``realtime`` keeps the marks (scale 1), ``video`` scales them so the whole sequence,
including a short eval card after the last mark (``tail_s``), fits in ``total_s``
(default 170 s, DECISIONS 2026-10-16 23:05), and ``fast`` never waits (scale 0, used by
tests). ``speed`` divides every offset (2.0 runs twice as fast). The schedule is a pure
function of its inputs; offsets are rounded to 6 dp.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

from soundings.canonical import round6

DEFAULT_TOTAL_S = 170.0
DEFAULT_TAIL_S = 10.0


class Pace(StrEnum):
    REALTIME = "realtime"
    FAST = "fast"
    VIDEO = "video"


@dataclass(frozen=True)
class ScheduledStep:
    index: int
    mark_s: int
    at_s: float
    """Offset from the demo start at which the step begins (scaled, 6 dp)."""


@dataclass(frozen=True)
class Schedule:
    pace: Pace
    scale: float
    total_s: float
    """Offset at which the demo ends (last mark plus the tail, scaled)."""
    steps: tuple[ScheduledStep, ...]


def build_schedule(
    marks_s: Sequence[int],
    *,
    pace: Pace = Pace.VIDEO,
    total_s: float = DEFAULT_TOTAL_S,
    tail_s: float = DEFAULT_TAIL_S,
    speed: float = 1.0,
) -> Schedule:
    """Map time marks (seconds, strictly increasing) to wall offsets for ``pace``."""
    if not marks_s:
        raise ValueError("a schedule needs at least one mark")
    if any(m < 0 for m in marks_s):
        raise ValueError("time marks are never negative")
    if any(b <= a for a, b in zip(marks_s, marks_s[1:], strict=False)):
        raise ValueError("time marks must be strictly increasing")
    if not speed > 0:
        raise ValueError("speed must be > 0")
    if not total_s > 0 or tail_s < 0:
        raise ValueError("total_s must be > 0 and tail_s >= 0")
    span = float(marks_s[-1]) + float(tail_s)
    if pace is Pace.FAST:
        base = 0.0
    elif pace is Pace.REALTIME:
        base = 1.0
    else:
        base = 1.0 if span <= total_s else total_s / span
    scale = round6(base / speed)
    steps = tuple(
        ScheduledStep(index=i, mark_s=int(m), at_s=round6(m * base / speed))
        for i, m in enumerate(marks_s)
    )
    return Schedule(pace=pace, scale=scale, total_s=round6(span * base / speed), steps=steps)


__all__ = [
    "DEFAULT_TAIL_S",
    "DEFAULT_TOTAL_S",
    "Pace",
    "Schedule",
    "ScheduledStep",
    "build_schedule",
]
