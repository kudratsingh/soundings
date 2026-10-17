"""Static current field by tide phase (docs/SPEC-SIM.md section 2).

``config/current_field.yaml`` holds a coarse rectilinear lat/lon grid of cell centres
with one (u, v) vector per cell and tide phase (u east, v north, m/s), plus an
hour-of-day to tide-phase table. ``CurrentField.vector_at`` is a nearest-cell lookup
with no interpolation; ties go to the lower (southern / western) cell, so the result
never depends on float noise at a boundary in an order-dependent way.

The sim integrates positions in local metres. Until ``soundings.schemas.geo`` lands,
this module carries a private equirectangular conversion about the same fixed origin
WO-010 specifies (33.74, -118.265); ``vector_at_local`` uses it.
"""

from __future__ import annotations

import math
from bisect import bisect_left
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal, cast

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CURRENT_FIELD_PATH = REPO_ROOT / "config" / "current_field.yaml"

TidePhase = Literal["flood", "ebb", "slack"]
TIDE_PHASES: tuple[TidePhase, ...] = ("flood", "ebb", "slack")

# Local tangent-plane origin; matches WO-010 soundings.schemas.geo (stage 2 switches to it).
_ORIGIN_LAT = 33.74
_ORIGIN_LON = -118.265
_M_PER_DEG_LAT = 111_320.0

Vector = tuple[float, float]
CurrentSampler = Callable[[float, float], Vector]
"""(x_m, y_m) in local metres -> (u, v) in m/s. What behaviors consume."""


def _m_per_deg_lon() -> float:
    return _M_PER_DEG_LAT * math.cos(math.radians(_ORIGIN_LAT))


def _local_to_latlon(x_m: float, y_m: float) -> tuple[float, float]:
    return _ORIGIN_LAT + y_m / _M_PER_DEG_LAT, _ORIGIN_LON + x_m / _m_per_deg_lon()


def _latlon_to_local(lat: float, lon: float) -> tuple[float, float]:
    return (lon - _ORIGIN_LON) * _m_per_deg_lon(), (lat - _ORIGIN_LAT) * _M_PER_DEG_LAT


def _nearest_index(axis: Sequence[float], value: float) -> int:
    """Index of the nearest axis value (axis strictly rising); ties go to the lower index."""
    i = bisect_left(axis, value)
    if i <= 0:
        return 0
    if i >= len(axis):
        return len(axis) - 1
    return i if axis[i] - value < value - axis[i - 1] else i - 1


@dataclass(frozen=True, slots=True)
class CurrentField:
    """Nearest-cell current lookup. ``vectors[phase][i][j]`` is the cell at lats[i], lons[j]."""

    lats: tuple[float, ...]
    lons: tuple[float, ...]
    vectors: dict[TidePhase, tuple[tuple[Vector, ...], ...]]
    hour_phase: tuple[TidePhase, ...]  # index = hour_of_day 0..23

    def __post_init__(self) -> None:
        for name, axis in (("lats", self.lats), ("lons", self.lons)):
            if not axis or any(b <= a for a, b in zip(axis, axis[1:], strict=False)):
                raise ValueError(f"{name} must be non-empty and strictly rising")
        if tuple(self.vectors) != TIDE_PHASES:
            raise ValueError(f"phases must be exactly {TIDE_PHASES} in that order")
        for phase in TIDE_PHASES:
            rows = self.vectors[phase]
            if len(rows) != len(self.lats) or any(len(r) != len(self.lons) for r in rows):
                raise ValueError(f"phase {phase}: grid must be {len(self.lats)}x{len(self.lons)}")
        if len(self.hour_phase) != 24:
            raise ValueError("hour_phase must cover hours 0-23")

    def vector_at(self, lat: float, lon: float, tide_phase: TidePhase) -> Vector:
        """(u, v) in m/s of the cell nearest to (lat, lon) for the given tide phase."""
        if tide_phase not in self.vectors:
            raise ValueError(f"unknown tide phase {tide_phase!r}")
        i = _nearest_index(self.lats, lat)
        j = _nearest_index(self.lons, lon)
        return self.vectors[tide_phase][i][j]

    def vector_at_local(self, x_m: float, y_m: float, tide_phase: TidePhase) -> Vector:
        """``vector_at`` for a position in local metres about the harbor origin."""
        lat, lon = _local_to_latlon(x_m, y_m)
        return self.vector_at(lat, lon, tide_phase)

    def sampler(self, tide_phase: TidePhase) -> CurrentSampler:
        """A local-metre sampler bound to one tide phase, for the behavior step functions."""
        if tide_phase not in self.vectors:
            raise ValueError(f"unknown tide phase {tide_phase!r}")

        def _sample(x_m: float, y_m: float) -> Vector:
            return self.vector_at_local(x_m, y_m, tide_phase)

        return _sample

    def phase_for_hour(self, hour: int) -> TidePhase:
        """Tide phase for an hour of day (0-23)."""
        if isinstance(hour, bool) or not isinstance(hour, int) or not 0 <= hour <= 23:
            raise ValueError(f"hour must be an int in 0..23, got {hour!r}")
        return self.hour_phase[hour]


def _as_float(value: Any, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"{where}: expected a number, got {value!r}")
    return float(value)


def current_field_from_mapping(raw: dict[str, Any]) -> CurrentField:
    """Build a ``CurrentField`` from the parsed YAML mapping, checking its shape."""
    if set(raw) != {"lats", "lons", "hours", "phases"}:
        raise ValueError("current field needs exactly: lats, lons, hours, phases")
    lats = tuple(_as_float(v, "lats") for v in raw["lats"])
    lons = tuple(_as_float(v, "lons") for v in raw["lons"])
    phases_raw = raw["phases"]
    if not isinstance(phases_raw, dict) or set(phases_raw) != set(TIDE_PHASES):
        raise ValueError(f"phases must be exactly {TIDE_PHASES}")
    vectors: dict[TidePhase, tuple[tuple[Vector, ...], ...]] = {}
    for phase in TIDE_PHASES:
        rows: list[tuple[Vector, ...]] = []
        for i, row in enumerate(phases_raw[phase]):
            cells: list[Vector] = []
            for j, cell in enumerate(row):
                where = f"phases.{phase}[{i}][{j}]"
                if not isinstance(cell, list) or len(cell) != 2:
                    raise ValueError(f"{where}: expected [u, v]")
                cells.append((_as_float(cell[0], where), _as_float(cell[1], where)))
            rows.append(tuple(cells))
        vectors[phase] = tuple(rows)
    hours_raw = raw["hours"]
    if not isinstance(hours_raw, dict) or set(hours_raw) != set(TIDE_PHASES):
        raise ValueError(f"hours must map exactly {TIDE_PHASES}")
    by_hour: dict[int, TidePhase] = {}
    for phase in TIDE_PHASES:
        for hour in hours_raw[phase]:
            if isinstance(hour, bool) or not isinstance(hour, int) or not 0 <= hour <= 23:
                raise ValueError(f"hours.{phase}: bad hour {hour!r}")
            if hour in by_hour:
                raise ValueError(f"hour {hour} is listed under two phases")
            by_hour[hour] = phase
    if sorted(by_hour) != list(range(24)):
        raise ValueError("hours must cover 0-23 exactly once")
    return CurrentField(
        lats=lats,
        lons=lons,
        vectors=vectors,
        hour_phase=tuple(by_hour[h] for h in range(24)),
    )


def load_current_field(path: Path | str = DEFAULT_CURRENT_FIELD_PATH) -> CurrentField:
    """Load ``config/current_field.yaml``."""
    with Path(path).open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected a mapping at the top level")
    return current_field_from_mapping(cast(dict[str, Any], data))


@lru_cache(maxsize=1)
def _default_field() -> CurrentField:
    return load_current_field(DEFAULT_CURRENT_FIELD_PATH)


def tide_phase_for_hour(hour: int, field: CurrentField | None = None) -> TidePhase:
    """Tide phase for an hour of day, from ``field`` or the default config file."""
    return (field if field is not None else _default_field()).phase_for_hour(hour)
