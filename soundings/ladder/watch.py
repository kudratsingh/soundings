"""Watch after the handoff: the departure detector and the SITREP update inputs.

CLAUDE.md 2.1: after HANDED_OFF the platform only observes. ``DepartureDetector`` is WO-040's
``DeparturePort``: it says when a watched contact has left (the same detector covers
``HOLDING x contact_departed``). A contact that persists gets a template ``update`` SITREP
every ``sitrep_update_interval_s`` (the machine's ``sitrep_update`` timer), built by
``build_update_inputs`` from the evidence rows added since the previous SITREP. Nothing
here climbs the ladder: the rung is copied from the machine, never chosen.

CLAUDE.md 2.4: no clock read; time is the tick the caller passes times ``tick_s``.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from soundings.canonical import round6
from soundings.schemas import (
    EvidenceRow,
    FeatureName,
    Harbor,
    LadderState,
    Rung,
    TrackPoint,
    load_ladder_config,
)
from soundings.schemas.config import LADDER_PATH

from .sitrep_port import LastKnown, SitrepInputs


def departed_after_s_from_config(path: str | Path = LADDER_PATH) -> float:
    """``watch_after_handoff.departed_after_s_outside_zone`` from ``config/ladder.yaml``."""
    return float(load_ladder_config(path).watch_after_handoff["departed_after_s_outside_zone"])


@dataclass
class _Seen:
    last: TrackPoint | None = None
    outside_since: int | None = None
    lost_at: int | None = None


@dataclass
class DepartureDetector:
    """Reports a departure once a contact has been continuously outside every zone.

    A contact is outside when its latest observed point has ``harbor.zone_at(...) is None``.
    The time outside at tick ``t`` is ``(t - first outside tick) * tick_s``; the detector
    reports ``True`` once that reaches ``departed_after_s``. A tick with no detection keeps
    the last known point, so a track lost while its last point is inside never departs.
    """

    harbor: Harbor
    departed_after_s: float
    tick_s: float
    _seen: dict[str, _Seen] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.departed_after_s < 0:
            raise ValueError("departed_after_s must be non-negative")
        if not self.tick_s > 0:
            raise ValueError("tick_s must be positive")

    @classmethod
    def from_config(
        cls, harbor: Harbor, tick_s: float, path: str | Path = LADDER_PATH
    ) -> DepartureDetector:
        return cls(harbor, departed_after_s_from_config(path), tick_s)

    def observe(self, contact_id: str, tick: int, point: TrackPoint | None) -> bool:
        seen = self._seen.setdefault(contact_id, _Seen())
        if point is None:
            if seen.last is not None and seen.lost_at is None:
                seen.lost_at = tick
        else:
            seen.last = point
            seen.lost_at = None
            if self.harbor.zone_at(point.lat, point.lon) is None:
                if seen.outside_since is None:
                    seen.outside_since = tick
            else:
                seen.outside_since = None
        if seen.last is None or seen.outside_since is None:
            return False
        outside_s = round6((tick - seen.outside_since) * self.tick_s)
        return outside_s >= round6(self.departed_after_s)

    def last_known(self, contact_id: str) -> LastKnown | None:
        seen = self._seen.get(contact_id)
        if seen is None or seen.last is None:
            return None
        p = seen.last
        zone = self.harbor.zone_at(p.lat, p.lon)
        return LastKnown(
            tick=p.tick, lat=p.lat, lon=p.lon, zone_id=None if zone is None else zone.zone_id
        )

    def track_lost_at(self, contact_id: str) -> int | None:
        """The first tick of the current run of ticks with no detection, or ``None``."""
        seen = self._seen.get(contact_id)
        return None if seen is None else seen.lost_at


@dataclass(frozen=True)
class WatchSummary:
    """What watching after the handoff saw for one contact."""

    contact_id: str
    updates: int
    departed_at_s: float | None
    last_known: LastKnown | None


def build_update_inputs(
    *,
    contact_id: str,
    tick: int,
    now_s: float,
    state: LadderState,
    rung: Rung,
    rows: Iterable[EvidenceRow],
    since_tick: int,
    base: SitrepInputs | None = None,
    last_known: LastKnown | None = None,
    track_lost_at_tick: int | None = None,
) -> SitrepInputs:
    """The ``update`` SITREP inputs: the rows newer than the previous SITREP, cited by id.

    ``base`` (the previous SITREP's inputs) carries the assessment over unchanged: an update
    reports fresh observation, it never re-decides the rung (``rung`` is the machine's).
    The latest fresh ``time_to_nearest_critical_asset`` row, when there is one, refreshes
    that number.
    """
    fresh = sorted(
        (r for r in rows if r.contact_id == contact_id and r.t > since_tick),
        key=lambda r: (r.t, r.row_id),
    )
    tta = base.time_to_nearest_critical_asset_s if base is not None else None
    for r in fresh:
        if r.feature is FeatureName.TIME_TO_NEAREST_CRITICAL_ASSET and isinstance(
            r.value, (int, float)
        ):
            tta = float(r.value)
    return SitrepInputs(
        kind="update",
        contact_id=contact_id,
        tick=tick,
        now_s=round6(now_s),
        state=state,
        rung=rung,
        recommended_rung=None if base is None else base.recommended_rung,
        threat_mass=None if base is None else base.threat_mass,
        benign_mass=None if base is None else base.benign_mass,
        basin=None if base is None else base.basin,
        nearest_asset_id=None if base is None else base.nearest_asset_id,
        time_to_nearest_critical_asset_s=tta,
        cited_row_ids=tuple(dict.fromkeys(r.row_id for r in fresh)),
        flags=() if base is None else base.flags,
        last_known=last_known,
        track_lost_at_tick=track_lost_at_tick,
    )


__all__ = [
    "DepartureDetector",
    "WatchSummary",
    "build_update_inputs",
    "departed_after_s_from_config",
]
