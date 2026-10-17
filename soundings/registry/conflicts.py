"""Conflict detection at filing (WO-081). Pure: no I/O, no clock, no model import.

Two plans conflict only when they overlap in all three of area, depth band and time
window. Every rule is half-open, so back-to-back work never conflicts:

- area: the buffered zone polygons (local metres) have ``intersection.area > 0``;
  touching edges do not count;
- depth: ``min(max) - max(min) > 0``;
- window: ``floor((min(end) - max(start)) / 60 s) > 0`` whole minutes.

The registry flags a conflict and an operator decides what to do about it; nothing here
moves or reschedules any plan.

Candidates come from a shapely ``STRtree`` over the existing plans sorted by
``(start_at, plan_id)``; tree hits are sorted before use, so the result never depends on
the tree's internal order. Output is ordered by ``(other.start_at, other_plan_id)``.
"""

from __future__ import annotations

import enum
import math
from collections.abc import Sequence
from datetime import datetime, tzinfo

from pydantic import AwareDatetime, BaseModel, ConfigDict
from shapely import Polygon, STRtree

from soundings.registry.models import ConflictConfig, ConflictView, PlanView
from soundings.schemas.geo import to_local_m
from soundings.schemas.harbor import Harbor

__all__ = [
    "Conflict",
    "ConflictReason",
    "PlanFootprint",
    "conflict_view",
    "conflicts_among",
    "detect_conflicts",
    "footprint_of",
    "mirror",
    "zone_geometry",
]


class ConflictReason(enum.StrEnum):
    AREA = "area"
    DEPTH = "depth"
    WINDOW = "window"


ALL_REASONS: frozenset[ConflictReason] = frozenset(ConflictReason)


class PlanFootprint(BaseModel):
    """What conflict detection needs from a plan: zone, window and depth band."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    plan_id: str
    area: str
    start_at: AwareDatetime
    end_at: AwareDatetime
    depth_min_m: float
    depth_max_m: float


class Conflict(BaseModel):
    """A conflict of ``plan_id`` with ``other_plan_id``. Exists only if all three overlap.

    ``overlap_start``/``overlap_end`` carry the offset ``plan_id`` was filed with.
    ``depth_overlap_m`` and ``area_overlap_m2`` are informational (6 dp).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    plan_id: str
    other_plan_id: str
    reasons: frozenset[ConflictReason]
    overlap_start: AwareDatetime
    overlap_end: AwareDatetime
    overlap_minutes: int
    depth_overlap_m: float
    area_overlap_m2: float


def footprint_of(view: PlanView, cfg: ConflictConfig) -> PlanFootprint:
    """The plan's footprint; a plan with no depth band takes its job type's default."""
    if view.depth_min_m is not None and view.depth_max_m is not None:
        lo, hi = float(view.depth_min_m), float(view.depth_max_m)
    else:
        lo, hi = cfg.depth_band(view.job_type)
    return PlanFootprint(
        plan_id=view.plan_id,
        area=view.area,
        start_at=view.start_at,
        end_at=view.end_at,
        depth_min_m=lo,
        depth_max_m=hi,
    )


_GEOMETRY_CACHE: dict[tuple[str, str, float], Polygon] = {}


def zone_geometry(harbor: Harbor, zone_id: str, buffer_m: float) -> Polygon:
    """The zone polygon in ``to_local_m`` metres, buffered by ``buffer_m``.

    Cached per ``(harbor name, zone_id, buffer_m)``.
    """
    key = (str(harbor.name), zone_id, float(buffer_m))
    geom = _GEOMETRY_CACHE.get(key)
    if geom is None:
        zone = harbor.zone(zone_id)
        base = Polygon([to_local_m(lat, lon) for lat, lon in zone.polygon])
        geom = base.buffer(float(buffer_m)) if buffer_m != 0 else base
        _GEOMETRY_CACHE[key] = geom
    return geom


def _window_minutes(
    a_start: datetime, a_end: datetime, b_start: datetime, b_end: datetime
) -> tuple[datetime, datetime, int]:
    lo = max(a_start, b_start)
    hi = min(a_end, b_end)
    if hi <= lo:
        return lo, hi, 0
    return lo, hi, math.floor((hi - lo).total_seconds() / 60.0)


def _order_key(f: PlanFootprint) -> tuple[datetime, str]:
    return (f.start_at, f.plan_id)


def _in_offset(value: datetime, tz: tzinfo | None) -> datetime:
    return value.astimezone(tz)


def _pair(
    new: PlanFootprint, new_geom: Polygon, other: PlanFootprint, other_geom: Polygon
) -> Conflict | None:
    lo, hi, minutes = _window_minutes(new.start_at, new.end_at, other.start_at, other.end_at)
    if minutes <= 0:
        return None
    depth = min(new.depth_max_m, other.depth_max_m) - max(new.depth_min_m, other.depth_min_m)
    if depth <= 0:
        return None
    area = float(new_geom.intersection(other_geom).area)
    if area <= 0:
        return None
    tz = new.start_at.tzinfo
    return Conflict(
        plan_id=new.plan_id,
        other_plan_id=other.plan_id,
        reasons=ALL_REASONS,
        overlap_start=_in_offset(lo, tz),
        overlap_end=_in_offset(hi, tz),
        overlap_minutes=minutes,
        depth_overlap_m=round(depth, 6),
        area_overlap_m2=round(area, 6),
    )


def detect_conflicts(
    new: PlanFootprint,
    existing: Sequence[PlanFootprint],
    harbor: Harbor,
    cfg: ConflictConfig,
) -> tuple[Conflict, ...]:
    """Every plan in ``existing`` that overlaps ``new`` in area, depth and window.

    ``existing`` may come in any order; a footprint with ``new``'s own plan id is skipped.
    Ordered by ``(other.start_at, other_plan_id)``.
    """
    ordered = sorted((f for f in existing if f.plan_id != new.plan_id), key=_order_key)
    if not ordered:
        return ()
    buffer_m = cfg.area_buffer_m
    new_geom = zone_geometry(harbor, new.area, buffer_m)
    if new_geom.is_empty:
        return ()
    geoms = [zone_geometry(harbor, f.area, buffer_m) for f in ordered]
    tree = STRtree(geoms)
    hits = sorted(int(i) for i in tree.query(new_geom, predicate="intersects"))
    out: list[Conflict] = []
    for i in hits:
        conflict = _pair(new, new_geom, ordered[i], geoms[i])
        if conflict is not None:
            out.append(conflict)
    by_id = {f.plan_id: f for f in ordered}
    out.sort(key=lambda c: (by_id[c.other_plan_id].start_at, c.other_plan_id))
    return tuple(out)


def mirror(conflict: Conflict, other_tz: tzinfo | None) -> Conflict:
    """The same conflict seen from ``other_plan_id`` (times in the other plan's offset)."""
    return Conflict(
        plan_id=conflict.other_plan_id,
        other_plan_id=conflict.plan_id,
        reasons=conflict.reasons,
        overlap_start=conflict.overlap_start.astimezone(other_tz),
        overlap_end=conflict.overlap_end.astimezone(other_tz),
        overlap_minutes=conflict.overlap_minutes,
        depth_overlap_m=conflict.depth_overlap_m,
        area_overlap_m2=conflict.area_overlap_m2,
    )


def conflicts_among(
    footprints: Sequence[PlanFootprint], harbor: Harbor, cfg: ConflictConfig
) -> tuple[Conflict, ...]:
    """Batch form: file ``footprints`` in the given order, as sequential ``create()`` does.

    Returns both directions of every conflict, ordered by
    ``(plan start_at, plan_id, other start_at, other_plan_id)``.
    """
    by_id = {f.plan_id: f for f in footprints}
    out: list[Conflict] = []
    for i, new in enumerate(footprints):
        for c in detect_conflicts(new, footprints[:i], harbor, cfg):
            out.append(c)
            out.append(mirror(c, by_id[c.other_plan_id].start_at.tzinfo))

    def key(c: Conflict) -> tuple[datetime, str, datetime, str]:
        a, b = by_id[c.plan_id], by_id[c.other_plan_id]
        return (a.start_at, a.plan_id, b.start_at, b.plan_id)

    return tuple(sorted(out, key=key))


def conflict_view(conflict: Conflict, resolved_by: str | None = None) -> ConflictView:
    """The API/view form of a conflict (reasons sorted)."""
    return ConflictView(
        other_plan_id=conflict.other_plan_id,
        overlap_start=conflict.overlap_start,
        overlap_end=conflict.overlap_end,
        overlap_minutes=conflict.overlap_minutes,
        reasons=tuple(sorted(r.value for r in conflict.reasons)),
        resolved_by=resolved_by,
    )
