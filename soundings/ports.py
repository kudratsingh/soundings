"""The seam between the simulated world and the code that investigates it.

The investigation (``soundings.investigation``) and the ladder (``soundings.ladder``)
import nothing from ``soundings.sim`` (``tests/test_boundary.py`` holds that line). What
they may learn about a run comes through ``WorldRun``: the observed tracks, the tick
length and the run seed, plus two physical probes that the world itself answers:

* ``hail_responder``: how the thing behind an observed id would answer an acoustic hail
  (whether it is set up to reply, from when it goes quiet, and which scenario contact's
  registry entry carries its modem). ``None`` means nothing there will ever reply (an id
  with no source, or a sensor false alarm). It names no hypothesis.
* ``eyes_on_path``: the positions a person on the water would see when sent to look,
  as ``(tick, lat, lon, depth_m)`` per tick, oldest first. Empty when nothing is known.

``soundings.sim.types.SimRun`` implements both by reading its own ground truth; that read
happens inside the simulator, which is the world in this demonstration. Nothing on this
side of the seam reads a ground-truth field, and nothing here carries a hypothesis label.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

from soundings.schemas.track import Track

EyesOnPoint = tuple[int, float, float, float]
"""``(tick, lat, lon, depth_m)`` of one position seen by eye."""


@dataclass(frozen=True, slots=True)
class HailResponder:
    """How one observed contact would answer a hail (no hypothesis, no label).

    ``registry_contact_id`` is the scenario contact whose registry entry (modem id, plan
    id) the reply would carry, or an id with no scenario entry (a surge contact), in which
    case a reply carries no modem id. ``silent_after_tick`` is the first tick at which
    replies stop (None: never).
    """

    registry_contact_id: str
    replies_to_hail: bool
    silent_after_tick: int | None = None

    def answers_at(self, tick: int) -> bool:
        """Whether a hail sent at ``tick`` gets an answer (before any reply loss)."""
        if not self.replies_to_hail:
            return False
        return self.silent_after_tick is None or tick < self.silent_after_tick


class ObservedTracks(Protocol):
    """The observed side of a run: tracks keyed by observed id, and the tick length."""

    @property
    def tracks(self) -> Mapping[str, Track]: ...

    @property
    def tick_s(self) -> float: ...


class WorldRun(ObservedTracks, Protocol):
    """A run the probes may act on: observed tracks plus the world's physical answers."""

    @property
    def scenario_id(self) -> str: ...

    @property
    def seed(self) -> int: ...

    def hail_responder(self, contact_id: str) -> HailResponder | None: ...

    def eyes_on_path(self, contact_id: str) -> Sequence[EyesOnPoint]: ...


__all__ = ["EyesOnPoint", "HailResponder", "ObservedTracks", "WorldRun"]
