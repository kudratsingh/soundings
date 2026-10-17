"""The simulated responder platform: one small vehicle per harbor, dispatched to look (WO-088).

The responder is a mobile probe at rung *ask* (CLAUDE.md section 8 A2). It is docked at a
pier, it may be dispatched to look at one contact, it keeps station at a standoff on the
side of the contact away from the nearest asset, and it returns to dock. It is never an
action against anything: nothing here names a position for a contact, and the planner may
request a look, never a position (the guards enforce that; this module only moves the
platform's own sensor carrier).

Rules, all deterministic geometry in the local metre frame of ``soundings.schemas.geo``:

* Every trail point lies inside a declared security zone named in ``geofence_zone_ids``.
* Whenever a contact position is known, every trail point is at least ``standoff_m`` from
  the contact and outside the corridor between the contact and its nearest asset (the
  segment contact to asset, ``corridor_half_width_m`` either side).
* The station point is ``standoff_m + restation_hysteresis_m`` from the contact on the
  side away from the nearest asset; the responder sweeps an arc there with period
  ``orbit_period_s`` (it orbits at standoff, never inside).
* On station it never closes on the contact faster than the contact moves; if the range is
  opening it holds station, and beyond ``look_range_max_m`` it returns to dock (no
  following). It re-stations only when the contact has moved more than
  ``restation_hysteresis_m`` from where the station was computed.
* Battery drains with a cubic law in speed plus a hotel load; the responder returns to dock
  when the battery would fall below the return cost.

Each ``step`` call moves the responder to the given tick and appends one
``ResponderSnapshot`` to ``trail``. No wall clock, no randomness: same calls, same trail.
Positions are stored as 6 dp latitude and longitude, and every check runs on the rounded
values, so the reported trail is exactly what was checked.
"""

from __future__ import annotations

import heapq
import math
from collections.abc import Sequence
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any, Protocol

import yaml
from pydantic import Field, model_validator

from soundings.canonical import round6
from soundings.schemas._base import (
    CONFIG_DIR,
    HarborId,
    Lat,
    Lon,
    NonNegFloat,
    NonNegInt,
    PositiveFloat,
    Probability,
    StrictModel,
    from_file_data,
)
from soundings.schemas.geo import distance_m, from_local_m, point_in_polygon, to_local_m
from soundings.schemas.harbor import Harbor

DEFAULT_RESPONDER_PATH: Path = CONFIG_DIR / "responder.yaml"

MIN_STANDOFF_M = 25.0
"""The platform-alone floor (CLAUDE.md section 8 A2). Below it a person approves."""

_SAFETY_EPS_M = 0.5
"""Numerical cushion on the standoff and corridor checks (6 dp latitude is about 0.11 m)."""

_LOS_STEP_M = 5.0
"""Sample spacing when checking that a straight leg stays inside the allowed region."""

_ORBIT_CHORD_M = 0.5
"""Range a single orbit step may give up inside the station band (a chord of the arc)."""

_ORBIT_OFFSETS_DEG = (0, 15, -15, 30, -30, 45, -45, 60, -60, 75, -75, 90, -90)
_RING_DIRECTIONS = 16


# ---------------------------------------------------------------------------- config


class DockConfig(StrictModel):
    lat: Lat
    lon: Lon


class ResponderConfig(StrictModel):
    """``config/responder.yaml``. Every value is a demo value (see the file)."""

    id: HarborId
    dock: DockConfig
    speed_mps: PositiveFloat
    standoff_m: Annotated[float, Field(ge=MIN_STANDOFF_M)]
    battery_min: PositiveFloat
    drain_coeff: NonNegFloat
    hotel_load_min_per_min: NonNegFloat
    battery_reserve_frac: Probability
    geofence_zone_ids: Annotated[tuple[HarborId, ...], Field(min_length=1)]
    restation_hysteresis_m: PositiveFloat
    orbit_period_s: PositiveFloat
    orbit_half_arc_deg: Annotated[float, Field(ge=0.0, le=90.0)]
    corridor_half_width_m: PositiveFloat
    corridor_tie_margin_m: NonNegFloat
    look_range_max_m: PositiveFloat
    transit_timeout_factor: Annotated[float, Field(ge=1.0)]
    route_cell_m: PositiveFloat
    search_track_spacing_m: PositiveFloat

    @model_validator(mode="after")
    def _check(self) -> ResponderConfig:
        if len(set(self.geofence_zone_ids)) != len(self.geofence_zone_ids):
            raise ValueError("geofence_zone_ids must be unique")
        if self.look_range_max_m <= self.standoff_m + self.restation_hysteresis_m:
            raise ValueError("look_range_max_m must exceed standoff_m + restation_hysteresis_m")
        return self

    @property
    def station_radius_m(self) -> float:
        return self.standoff_m + self.restation_hysteresis_m


def _floats(obj: Any) -> Any:
    """YAML ints become floats (strict models); ids and nested keys pass through."""
    if isinstance(obj, dict):
        return {k: _floats(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_floats(v) for v in obj]
    if isinstance(obj, int) and not isinstance(obj, bool):
        return float(obj)
    return obj


def load_responder_config(path: str | Path | None = None) -> ResponderConfig:
    """Load ``config/responder.yaml`` (the ``responder:`` mapping) into a strict model."""
    p = DEFAULT_RESPONDER_PATH if path is None else Path(path)
    doc = yaml.safe_load(p.read_text(encoding="utf-8"))
    if not isinstance(doc, dict) or set(doc) != {"responder"}:
        raise ValueError("responder config must hold exactly one top-level key: responder")
    return ResponderConfig.model_validate(from_file_data(_floats(doc["responder"])))


def battery_drain_min(speed_mps: float, dt_s: float, drain_coeff: float, hotel: float) -> float:
    """Battery minutes used over ``dt_s`` seconds at ``speed_mps``: (hotel + k v^3) dt / 60.

    A stated cubic law (propulsive power grows with the cube of speed); demo values.
    """
    if speed_mps < 0.0 or dt_s < 0.0:
        raise ValueError("speed and duration must be non-negative")
    return (hotel + drain_coeff * speed_mps**3) * dt_s / 60.0


# ---------------------------------------------------------------------------- outputs


class ResponderState(StrEnum):
    DOCKED = "DOCKED"
    TRANSIT = "TRANSIT"
    ON_STATION = "ON_STATION"
    RETURNING = "RETURNING"


class ResponderSnapshot(StrictModel):
    """The responder at one tick. Floats are rounded to 6 dp."""

    tick: NonNegInt
    t_s: NonNegFloat
    lat: Lat
    lon: Lon
    heading_deg: Annotated[float, Field(ge=0.0, lt=360.0)]
    speed_mps: NonNegFloat
    battery_min: NonNegFloat
    state: ResponderState
    target_contact_id: str | None
    standoff_m: PositiveFloat
    distance_to_target_m: NonNegFloat | None


class DispatchResult(StrictModel):
    accepted: bool
    reason: str
    eta_s: NonNegFloat


class SearchBoxLike(Protocol):
    """The fields of ``soundings.matcher.SearchBox`` that the lawnmower uses."""

    @property
    def min_lat(self) -> float: ...
    @property
    def max_lat(self) -> float: ...
    @property
    def min_lon(self) -> float: ...
    @property
    def max_lon(self) -> float: ...


# ---------------------------------------------------------------------------- geometry

XY = tuple[float, float]


def _snap(x: float, y: float) -> tuple[float, float, float, float]:
    """Local metres to 6 dp (lat, lon), then back to the metres of the rounded point."""
    lat, lon = from_local_m(x, y)
    lat, lon = round6(lat), round6(lon)
    sx, sy = to_local_m(lat, lon)
    return lat, lon, sx, sy


def _heading(dx: float, dy: float) -> float:
    """Compass bearing in degrees (0 north, 90 east) of a move, in [0, 360)."""
    h = math.degrees(math.atan2(dx, dy)) % 360.0
    return 0.0 if h >= 360.0 else h


def in_corridor(p: XY, contact: XY, asset: XY, half_width_m: float) -> bool:
    """True when ``p`` lies between the contact and the asset (strictly along the segment
    and within ``half_width_m`` of it)."""
    dx, dy = asset[0] - contact[0], asset[1] - contact[1]
    seg2 = dx * dx + dy * dy
    if seg2 < 1e-12:
        return False
    px, py = p[0] - contact[0], p[1] - contact[1]
    t = (px * dx + py * dy) / seg2
    if t <= 0.0 or t >= 1.0:
        return False
    cross = abs(px * dy - py * dx) / math.sqrt(seg2)
    return cross < half_width_m


def _corridor_depth(p: XY, contact: XY, asset: XY, half_width_m: float) -> float:
    """How far inside the contact-to-asset corridor ``p`` lies (0 when outside)."""
    dx, dy = asset[0] - contact[0], asset[1] - contact[1]
    seg2 = dx * dx + dy * dy
    if seg2 < 1e-12:
        return 0.0
    px, py = p[0] - contact[0], p[1] - contact[1]
    t = (px * dx + py * dy) / seg2
    if t <= 0.0 or t >= 1.0:
        return 0.0
    cross = abs(px * dy - py * dx) / math.sqrt(seg2)
    return max(0.0, half_width_m - cross)


class _Region:
    """The geofence (union of declared zones) plus, optionally, one contact's keep-out.

    The keep-out is the standoff disk around the contact and the corridor between the
    contact and each asset given (the nearest asset and any asset nearly as near, so that
    a change of nearest asset never puts the responder in a corridor), each grown by the
    margin the caller chose.
    """

    __slots__ = ("assets", "contact", "corridor_m", "disk_m", "polys")

    def __init__(
        self,
        polys: tuple[tuple[tuple[float, float], ...], ...],
        contact: XY | None,
        assets: tuple[XY, ...],
        disk_m: float,
        corridor_m: float,
    ) -> None:
        self.polys = polys
        self.contact = contact
        self.assets = assets
        self.disk_m = disk_m
        self.corridor_m = corridor_m

    def geofence(self, lat: float, lon: float) -> bool:
        return any(point_in_polygon(lat, lon, poly) for poly in self.polys)

    def keepout(self, x: float, y: float) -> bool:
        if self.contact is None:
            return False
        cx, cy = self.contact
        if math.hypot(x - cx, y - cy) < self.disk_m:
            return True
        return any(in_corridor((x, y), self.contact, a, self.corridor_m) for a in self.assets)

    def depth(self, x: float, y: float) -> float:
        """Total depth inside the keep-out (0 when clear of it)."""
        if self.contact is None:
            return 0.0
        cx, cy = self.contact
        d = max(0.0, self.disk_m - math.hypot(x - cx, y - cy))
        for a in self.assets:
            d += _corridor_depth((x, y), self.contact, a, self.corridor_m)
        return d

    def ok_xy(self, x: float, y: float) -> bool:
        if self.keepout(x, y):
            return False
        lat, lon = from_local_m(x, y)
        return self.geofence(lat, lon)

    def segment_ok(self, a: XY, b: XY) -> bool:
        length = math.hypot(b[0] - a[0], b[1] - a[1])
        n = max(1, math.ceil(length / _LOS_STEP_M))
        for i in range(1, n + 1):
            f = i / n
            if not self.ok_xy(a[0] + f * (b[0] - a[0]), a[1] + f * (b[1] - a[1])):
                return False
        return True


@lru_cache(maxsize=8)
def _static_grid(
    polys: tuple[tuple[tuple[float, float], ...], ...], cell_m: float
) -> tuple[float, float, int, int, tuple[bool, ...]]:
    """Planning grid over the geofence bounding box: a cell is free when its centre and
    its four corners (shrunk by 10 percent) lie inside the geofence."""
    xs: list[float] = []
    ys: list[float] = []
    for poly in polys:
        for lat, lon in poly:
            x, y = to_local_m(lat, lon)
            xs.append(x)
            ys.append(y)
    x0, y0 = min(xs), min(ys)
    nx = max(1, math.ceil((max(xs) - x0) / cell_m))
    ny = max(1, math.ceil((max(ys) - y0) / cell_m))
    region = _Region(polys, None, (), 0.0, 0.0)
    h = 0.4 * cell_m
    free: list[bool] = []
    for j in range(ny):
        for i in range(nx):
            cx = x0 + (i + 0.5) * cell_m
            cy = y0 + (j + 0.5) * cell_m
            pts = ((cx, cy), (cx - h, cy - h), (cx + h, cy - h), (cx - h, cy + h), (cx + h, cy + h))
            free.append(all(region.ok_xy(px, py) for px, py in pts))
    return x0, y0, nx, ny, tuple(free)


# ---------------------------------------------------------------------------- responder


class Responder:
    """One simulated responder. Construct, dispatch, then ``step`` once per tick."""

    def __init__(self, config: ResponderConfig, harbor: Harbor, *, tick_s: float) -> None:
        if tick_s <= 0.0:
            raise ValueError("tick_s must be positive")
        known = {z.zone_id for z in harbor.zones}
        missing = sorted(set(config.geofence_zone_ids) - known)
        if missing:
            raise ValueError(f"geofence zones not in the harbor map: {missing}")
        self.config = config
        self.harbor = harbor
        self.tick_s = float(tick_s)
        self._polys = tuple(harbor.zone(zid).polygon for zid in sorted(config.geofence_zone_ids))
        if not self.inside_geofence(config.dock.lat, config.dock.lon):
            raise ValueError("the dock must lie inside the geofence")
        self._dock_lat, self._dock_lon, dx, dy = _snap(
            *to_local_m(config.dock.lat, config.dock.lon)
        )
        self._dock_xy: XY = (dx, dy)
        self._all_assets: tuple[XY, ...] = tuple(
            to_local_m(a.lat, a.lon) for a in sorted(harbor.assets, key=lambda a: a.asset_id)
        )
        self._xy: XY = self._dock_xy
        self._lat, self._lon = self._dock_lat, self._dock_lon
        self._heading = 0.0
        self._battery = float(config.battery_min)
        self._state = ResponderState.DOCKED
        self._target: str | None = None
        self._dispatched: list[str] = []
        self._mode: str = "idle"  # idle | look | search | return
        self._route: list[XY] = []
        self._contact: XY | None = None
        self._contact_prev: XY | None = None
        self._asset: XY | None = None
        self._plan_contact: XY | None = None
        self._station: XY | None = None
        self._search_points: list[XY] = []
        self._t_station: float = 0.0
        self._transit_deadline_s: float = math.inf
        self._searched = False
        self._clock_tick = 0  # last tick the motion was integrated to
        self.last_reason: str = "docked"
        first = self._make_snapshot(0, 0.0)
        self._snapshot = first
        self._trail: list[ResponderSnapshot] = [first]

    # ------------------------------------------------------------------ public reads

    @property
    def snapshot(self) -> ResponderSnapshot:
        return self._snapshot

    @property
    def trail(self) -> tuple[ResponderSnapshot, ...]:
        return tuple(self._trail)

    @property
    def state(self) -> ResponderState:
        return self._state

    @property
    def dispatched_contact_ids(self) -> tuple[str, ...]:
        return tuple(self._dispatched)

    @property
    def search_complete(self) -> bool:
        """True once a search pattern has been flown to its last leg."""
        return self._mode in ("return", "idle") and self._searched

    def inside_geofence(self, lat: float, lon: float) -> bool:
        """True when ``(lat, lon)`` lies in one of the declared zones of the geofence."""
        return any(point_in_polygon(lat, lon, poly) for poly in self._polys)

    def travel_time_s(self, lat1: float, lon1: float, lat2: float, lon2: float) -> float:
        """Seconds at ``speed_mps`` along the route inside the geofence (straight when the
        leg stays inside; else the grid route; straight distance when an end is outside)."""
        a, b = to_local_m(lat1, lon1), to_local_m(lat2, lon2)
        region = self._region(None)
        if not (region.ok_xy(*a) and region.ok_xy(*b)):
            return round6(distance_m(lat1, lon1, lat2, lon2) / self.config.speed_mps)
        route = self._plan(a, b, region)
        if route is None:
            return round6(distance_m(lat1, lon1, lat2, lon2) / self.config.speed_mps)
        return round6(_route_length(a, route) / self.config.speed_mps)

    def nearest_pass_m(self, lat: float, lon: float) -> float | None:
        """Closest trail point to ``(lat, lon)`` while searching or looking, or None."""
        best: float | None = None
        for s in self._trail:
            if s.state is ResponderState.ON_STATION:
                d = distance_m(lat, lon, s.lat, s.lon)
                best = d if best is None or d < best else best
        return None if best is None else round6(best)

    # ------------------------------------------------------------------ commands

    def dispatch_to_look(
        self, contact_id: str, contact_lat: float, contact_lon: float, tick: int
    ) -> DispatchResult:
        """Dispatch to look at one contact. Rejected when busy, when this contact already
        had its one dispatch, when no station point exists inside the geofence, or when the
        battery cannot cover the round trip."""
        refusal = self._precheck(contact_id)
        if refusal is not None:
            return refusal
        c = to_local_m(contact_lat, contact_lon)
        asset = self._asset_xy(contact_lat, contact_lon, None, None)
        if not self._region(c, asset).ok_xy(*self._xy):
            return DispatchResult(
                accepted=False,
                reason="contact within standoff or corridor of the dock: a person decides",
                eta_s=0.0,
            )
        station = self._station_point(c, asset, 0.0)
        if station is None:
            return DispatchResult(
                accepted=False, reason="no station point inside the geofence at standoff", eta_s=0.0
            )
        region = self._region(c, asset, margin=True)
        route = self._plan(self._xy, station, region) or self._plan(
            self._xy, station, self._region(c, asset)
        )
        if route is None:
            return DispatchResult(
                accepted=False, reason="no route to station inside the geofence", eta_s=0.0
            )
        eta = _route_length(self._xy, route) / self.config.speed_mps
        if not self._battery_covers(2.0 * eta + self.config.orbit_period_s):
            return DispatchResult(accepted=False, reason="battery below round-trip cost", eta_s=0.0)
        self._dispatched.append(contact_id)
        self._target = contact_id
        self._mode = "look"
        self._searched = False
        self._state = ResponderState.TRANSIT
        self._contact = c
        self._contact_prev = c
        self._asset = asset
        self._plan_contact = c
        self._station = station
        self._route = route
        t_now = tick * self.tick_s
        self._transit_deadline_s = t_now + self.config.transit_timeout_factor * eta + 120.0
        self._clock_tick = max(self._clock_tick, tick)  # docked until the dispatch tick
        self.last_reason = "dispatched to look"
        return DispatchResult(accepted=True, reason="dispatched to look", eta_s=round6(eta))

    def dispatch_search(
        self, contact_id: str, search_box: SearchBoxLike, tick: int
    ) -> DispatchResult:
        """Dispatch to fly a lawnmower over a drift box (LOST_VEHICLE). Legs are spaced
        ``search_track_spacing_m``; points outside the geofence are skipped."""
        refusal = self._precheck(contact_id)
        if refusal is not None:
            return refusal
        points = self._lawnmower(search_box)
        if not points:
            return DispatchResult(
                accepted=False, reason="search box lies outside the geofence", eta_s=0.0
            )
        region = self._region(None)
        route = self._plan(self._xy, points[0], region)
        if route is None:
            return DispatchResult(
                accepted=False, reason="no route to the search box inside the geofence", eta_s=0.0
            )
        eta = _route_length(self._xy, route) / self.config.speed_mps
        pattern = _route_length(points[0], points[1:]) / self.config.speed_mps
        if not self._battery_covers(2.0 * eta + pattern):
            return DispatchResult(accepted=False, reason="battery below search cost", eta_s=0.0)
        self._dispatched.append(contact_id)
        self._target = contact_id
        self._mode = "search"
        self._searched = False
        self._state = ResponderState.TRANSIT
        self._contact = None
        self._contact_prev = None
        self._asset = None
        self._station = None
        self._route = route
        self._search_points = points[1:]
        t_now = tick * self.tick_s
        self._transit_deadline_s = t_now + self.config.transit_timeout_factor * eta + 120.0
        self._clock_tick = max(self._clock_tick, tick)  # docked until the dispatch tick
        self.last_reason = "dispatched to search"
        return DispatchResult(accepted=True, reason="dispatched to search", eta_s=round6(eta))

    def recall(self, tick: int) -> None:
        """Return to dock now (a person's call, or the end of the look)."""
        del tick
        if self._state is not ResponderState.DOCKED:
            self._begin_return("recalled")

    # ------------------------------------------------------------------ step

    def step(
        self,
        tick: int,
        *,
        contact_lat: float | None = None,
        contact_lon: float | None = None,
        contact_speed_mps: float | None = None,
        nearest_asset_lat: float | None = None,
        nearest_asset_lon: float | None = None,
    ) -> ResponderSnapshot:
        """Advance to ``tick`` and return the snapshot. The same tick twice is a no-op."""
        last = self._snapshot.tick
        if tick < last:
            raise ValueError(f"tick {tick} is before the last step {last}")
        if tick == last and len(self._trail) > 1:
            return self._snapshot
        dt = max(0, tick - self._clock_tick) * self.tick_s
        self._clock_tick = max(self._clock_tick, tick)
        t_now = tick * self.tick_s
        tracking = self._mode == "look" or (self._mode == "return" and self._target is not None)
        if tracking and contact_lat is not None and contact_lon is not None:
            c = to_local_m(contact_lat, contact_lon)
            self._contact_prev = self._contact if self._contact is not None else c
            self._contact = c
            self._asset = self._asset_xy(
                contact_lat, contact_lon, nearest_asset_lat, nearest_asset_lon
            )
            if contact_speed_mps is None and dt > 0.0 and self._contact_prev is not None:
                contact_speed_mps = (
                    math.hypot(c[0] - self._contact_prev[0], c[1] - self._contact_prev[1]) / dt
                )
        v_contact = max(0.0, contact_speed_mps or 0.0)
        start = self._xy
        if dt > 0.0 and self._state is not ResponderState.DOCKED:
            self._advance(dt, t_now, v_contact)
        moved = math.hypot(self._xy[0] - start[0], self._xy[1] - start[1])
        speed = moved / dt if dt > 0.0 else 0.0
        if moved > 1e-9:
            self._heading = _heading(self._xy[0] - start[0], self._xy[1] - start[1])
        if self._state is not ResponderState.DOCKED and dt > 0.0:
            self._battery = max(
                0.0,
                self._battery
                - battery_drain_min(
                    speed, dt, self.config.drain_coeff, self.config.hotel_load_min_per_min
                ),
            )
        if self._state is not ResponderState.DOCKED and self._state is not ResponderState.RETURNING:
            if not self._battery_covers(self._return_time_s() + dt):
                self._begin_return("battery at return cost")
        if self._state is ResponderState.RETURNING and self._at_dock():
            self._state = ResponderState.DOCKED
            self._mode = "idle"
            self._target = None
            self._contact = None
            self._contact_prev = None
            self._asset = None
            self._route = []
            self.last_reason = "docked"
        snap = self._make_snapshot(tick, speed)
        self._snapshot = snap
        self._trail.append(snap)
        return snap

    # ------------------------------------------------------------------ internals

    def _precheck(self, contact_id: str) -> DispatchResult | None:
        if self._state is not ResponderState.DOCKED:
            return DispatchResult(
                accepted=False,
                reason="responder busy: a second vehicle needs a person's approval",
                eta_s=0.0,
            )
        if contact_id in self._dispatched:
            return DispatchResult(
                accepted=False, reason="one dispatch per contact: already dispatched", eta_s=0.0
            )
        return None

    def _asset_xy(
        self, c_lat: float, c_lon: float, a_lat: float | None, a_lon: float | None
    ) -> XY | None:
        if a_lat is not None and a_lon is not None:
            return to_local_m(a_lat, a_lon)
        asset = self.harbor.nearest_asset(c_lat, c_lon)
        return None if asset is None else to_local_m(asset.lat, asset.lon)

    def _region(
        self, contact: XY | None, asset: XY | None = None, *, margin: bool = False
    ) -> _Region:
        cfg = self.config
        grow = cfg.restation_hysteresis_m if margin else _SAFETY_EPS_M
        assets: tuple[XY, ...] = ()
        if contact is not None:
            assets = self._corridor_assets(contact)
            if asset is not None and asset not in assets:
                assets = (asset, *assets)
        return _Region(
            self._polys,
            contact,
            assets,
            cfg.standoff_m + grow,
            cfg.corridor_half_width_m + grow,
        )

    def _corridor_assets(self, contact: XY) -> tuple[XY, ...]:
        """The nearest asset and every asset within ``corridor_tie_margin_m`` of that
        distance, so a change of nearest asset never puts the responder in a corridor."""
        dists = [math.hypot(a[0] - contact[0], a[1] - contact[1]) for a in self._all_assets]
        if not dists:
            return ()
        cut = min(dists) + self.config.corridor_tie_margin_m
        return tuple(a for a, d in zip(self._all_assets, dists, strict=True) if d <= cut)

    def _strict_region(self) -> _Region:
        return self._region(self._contact, self._asset if self._contact is not None else None)

    def _away(self, c: XY, asset: XY | None) -> XY:
        if asset is not None:
            dx, dy = c[0] - asset[0], c[1] - asset[1]
            n = math.hypot(dx, dy)
            if n > 1.0:
                return dx / n, dy / n
        dx, dy = self._xy[0] - c[0], self._xy[1] - c[1]
        n = math.hypot(dx, dy)
        return (dx / n, dy / n) if n > 1e-9 else (0.0, 1.0)

    def _station_point(self, c: XY, asset: XY | None, orbit_deg: float) -> XY | None:
        """First allowed point on the away side: the orbit angle first, then fanning out
        to 90 degrees either side, then larger radii up to ``look_range_max_m``."""
        cfg = self.config
        ux, uy = self._away(c, asset)
        region = self._region(c, asset)
        radius = cfg.station_radius_m
        radii: list[float] = []
        while radius <= cfg.look_range_max_m + 1e-9:
            radii.append(radius)
            radius += cfg.restation_hysteresis_m
        for r in radii:
            for off in _ORBIT_OFFSETS_DEG:
                a = math.radians(orbit_deg + off)
                if abs(orbit_deg + off) > 90.0 + 1e-9:
                    continue
                # rotate the away unit vector by a (counter-clockwise)
                vx = ux * math.cos(a) - uy * math.sin(a)
                vy = ux * math.sin(a) + uy * math.cos(a)
                p = (c[0] + r * vx, c[1] + r * vy)
                lat, lon, sx, sy = _snap(*p)
                if region.ok_xy(sx, sy) and self.inside_geofence(lat, lon):
                    return sx, sy
        return None

    def _plan(self, a: XY, b: XY, region: _Region) -> list[XY] | None:
        """Waypoints from ``a`` to ``b`` (excluding ``a``) through ``region``: the straight
        leg when clear, else A* over the static grid with the keep-out, string-pulled."""
        if region.segment_ok(a, b):
            return [b]
        x0, y0, nx, ny, free = _static_grid(self._polys, self.config.route_cell_m)
        cell = self.config.route_cell_m

        def idx(p: XY) -> int:
            i = min(nx - 1, max(0, int((p[0] - x0) // cell)))
            j = min(ny - 1, max(0, int((p[1] - y0) // cell)))
            return j * nx + i

        def centre(k: int) -> XY:
            return x0 + (k % nx + 0.5) * cell, y0 + (k // nx + 0.5) * cell

        def passable(k: int) -> bool:
            if not free[k]:
                return False
            return not region.keepout(*centre(k))

        start, goal = idx(a), idx(b)
        gx, gy = centre(goal)
        best: dict[int, float] = {start: 0.0}
        parent: dict[int, int] = {}
        heap: list[tuple[float, int]] = [(0.0, start)]
        done: set[int] = set()
        found = False
        while heap:
            _, k = heapq.heappop(heap)
            if k in done:
                continue
            if k == goal:
                found = True
                break
            done.add(k)
            ki, kj = k % nx, k // nx
            for di, dj in ((1, 0), (-1, 0), (0, 1), (0, -1), (1, 1), (1, -1), (-1, 1), (-1, -1)):
                ni, nj = ki + di, kj + dj
                if not (0 <= ni < nx and 0 <= nj < ny):
                    continue
                nk = nj * nx + ni
                if nk in done or (nk != goal and not passable(nk)):
                    continue
                g = best[k] + cell * (1.4142135623730951 if di and dj else 1.0)
                if g < best.get(nk, math.inf) - 1e-9:
                    best[nk] = g
                    parent[nk] = k
                    cx, cy = centre(nk)
                    heapq.heappush(heap, (round(g + math.hypot(gx - cx, gy - cy), 6), nk))
        if not found:
            return None
        cells: list[int] = [goal]
        while cells[-1] != start:
            cells.append(parent[cells[-1]])
        cells.reverse()
        pts: list[XY] = [centre(k) for k in cells[1:-1]] + [b]
        # string-pull: from each anchor jump to the farthest point with a clear leg
        out: list[XY] = []
        anchor = a
        i = 0
        while i < len(pts):
            j = len(pts) - 1
            while j > i and not region.segment_ok(anchor, pts[j]):
                j -= 1
            out.append(pts[j])
            anchor = pts[j]
            i = j + 1
        return out

    def _lawnmower(self, box: SearchBoxLike) -> list[XY]:
        x_min, y_min = to_local_m(box.min_lat, box.min_lon)
        x_max, y_max = to_local_m(box.max_lat, box.max_lon)
        s = self.config.search_track_spacing_m
        half = s / 2.0
        rows = max(1, math.ceil((y_max - y_min) / s))
        cols = max(1, math.ceil((x_max - x_min) / s))
        pts: list[XY] = []
        region = self._region(None)
        for r in range(rows):
            y = min(y_max, y_min + half + r * s)
            xs = [min(x_max, x_min + half + c * s) for c in range(cols)]
            if r % 2 == 1:
                xs.reverse()
            for x in xs:
                lat, lon, sx, sy = _snap(x, y)
                if region.ok_xy(sx, sy) and self.inside_geofence(lat, lon):
                    pts.append((sx, sy))
        return pts

    def _battery_covers(self, seconds: float) -> bool:
        cfg = self.config
        rate = cfg.hotel_load_min_per_min + cfg.drain_coeff * cfg.speed_mps**3
        need = rate * seconds / 60.0 * (1.0 + cfg.battery_reserve_frac)
        return self._battery >= need

    def _return_time_s(self) -> float:
        """Conservative return time: straight distance times 1.5 for detours."""
        d = math.hypot(self._xy[0] - self._dock_xy[0], self._xy[1] - self._dock_xy[1])
        return 1.5 * d / self.config.speed_mps

    def _at_dock(self) -> bool:
        return math.hypot(self._xy[0] - self._dock_xy[0], self._xy[1] - self._dock_xy[1]) < 0.5

    def _begin_return(self, reason: str) -> None:
        self._state = ResponderState.RETURNING
        self._mode = "return"
        self.last_reason = reason
        region = self._strict_region()
        route = self._plan(self._xy, self._dock_xy, region) or self._plan(
            self._xy, self._dock_xy, self._region(None)
        )
        self._route = route if route is not None else [self._dock_xy]

    def _advance(self, dt: float, t_now: float, v_contact: float) -> None:
        cfg = self.config
        step_m = cfg.speed_mps * dt
        if self._mode == "look" and self._contact is not None:
            c = self._contact
            assert self._plan_contact is not None
            moved = math.hypot(c[0] - self._plan_contact[0], c[1] - self._plan_contact[1])
            if self._state is ResponderState.TRANSIT:
                if t_now > self._transit_deadline_s:
                    self._begin_return("station not reached in time; no following")
                elif moved > cfg.restation_hysteresis_m:
                    self._restation(c)
            elif self._state is ResponderState.ON_STATION:
                rng = math.hypot(self._xy[0] - c[0], self._xy[1] - c[1])
                if rng > cfg.look_range_max_m:
                    self._begin_return("contact beyond look range; no following")
                else:
                    self._keep_station(dt, t_now, v_contact, rng)
                    return
        if self._state is ResponderState.ON_STATION and self._mode == "search":
            if not self._route and self._search_points:
                self._route = [self._search_points.pop(0)]
            if not self._route:
                self._searched = True
                self._begin_return("search pattern complete")
        cand, passed = _walk(self._xy, self._route, step_m)
        self._try_move(cand, step_m, passed=passed)
        if self._state is ResponderState.TRANSIT and not self._route:
            if self._mode == "look":
                assert self._station is not None
                if (
                    math.hypot(self._xy[0] - self._station[0], self._xy[1] - self._station[1])
                    <= cfg.restation_hysteresis_m
                ):
                    self._state = ResponderState.ON_STATION
                    self._t_station = t_now
                    self.last_reason = "on station"
            else:
                self._state = ResponderState.ON_STATION
                self._t_station = t_now
                self.last_reason = "searching"

    def _restation(self, c: XY) -> None:
        station = self._station_point(c, self._asset, 0.0)
        if station is None:
            self._begin_return("no station point inside the geofence")
            return
        self._plan_contact = c
        self._station = station
        route = self._plan(self._xy, station, self._region(c, self._asset, margin=True))
        if route is None:
            route = self._plan(self._xy, station, self._strict_region())
        if route is not None:
            self._route = route

    def _keep_station(self, dt: float, t_now: float, v_contact: float, rng: float) -> None:
        """Orbit at standoff on the away side; hold when the range is opening."""
        cfg = self.config
        c = self._contact
        assert c is not None and self._contact_prev is not None
        prev_rng = math.hypot(
            self._xy[0] - self._contact_prev[0], self._xy[1] - self._contact_prev[1]
        )
        opening = rng > prev_rng + 1e-6
        if opening and self._strict_region().ok_xy(*self._xy):
            return  # hold station: no following
        phase = 2.0 * math.pi * (t_now - self._t_station) / cfg.orbit_period_s
        orbit = cfg.orbit_half_arc_deg * math.sin(phase)
        station = self._station_point(c, self._asset, orbit)
        if station is None:
            self._begin_return("no station point inside the geofence")
            return
        self._station = station
        self._plan_contact = c
        step_m = cfg.speed_mps * dt
        cand, _ = _walk(self._xy, [station], step_m)
        # Never close faster than the contact. Inside the station band a small chord
        # allowance lets it sweep the arc; outside the band it does not close at all
        # beyond what the contact's own motion brings (no following).
        floor = rng - v_contact * dt
        if rng <= cfg.station_radius_m + cfg.restation_hysteresis_m:
            floor -= _ORBIT_CHORD_M
        cx, cy = cand
        d = math.hypot(cx - c[0], cy - c[1])
        if d < floor - 1e-9:
            cand = self._xy
        self._try_move(cand, step_m, passed=None)

    def _try_move(self, cand: XY, step_m: float, *, passed: int | None) -> None:
        """Take ``cand`` if allowed; else hold if allowed; else the allowed point on a ring
        around the current position that is farthest from the contact."""
        strict = self._strict_region()
        lat, lon, sx, sy = _snap(*cand)
        if strict.ok_xy(sx, sy) and self.inside_geofence(lat, lon):
            self._set_pos(lat, lon, sx, sy)
            if passed is not None:
                _consume(self._route, passed, (sx, sy))
            return
        if strict.ok_xy(*self._xy):
            if passed is not None and self._route:
                # the planned leg is blocked (the keep-out moved): replan from here
                goal = self._route[-1]
                route = self._plan(self._xy, goal, strict)
                if route is not None:
                    self._route = route
            return
        best: tuple[float, int] | None = None
        best_pos: tuple[float, float, float, float] | None = None
        for frac in (1.0, 0.5):
            for k in range(_RING_DIRECTIONS):
                a = 2.0 * math.pi * k / _RING_DIRECTIONS
                p = (
                    self._xy[0] + frac * step_m * math.sin(a),
                    self._xy[1] + frac * step_m * math.cos(a),
                )
                plat, plon, px, py = _snap(*p)
                if not (strict.ok_xy(px, py) and self.inside_geofence(plat, plon)):
                    continue
                score = 0.0
                if self._contact is not None:
                    score = math.hypot(px - self._contact[0], py - self._contact[1])
                key = (-round(score, 6), k)
                if best is None or key < best:
                    best = key
                    best_pos = (plat, plon, px, py)
            if best_pos is not None:
                break
        if best_pos is not None:
            self._set_pos(*best_pos)
            return
        # Inside the keep-out (the contact or its nearest asset changed faster than the
        # platform can move): take the in-geofence ring point that leaves it fastest.
        here = strict.depth(*self._xy)
        best_d: tuple[float, int] | None = None
        for k in range(_RING_DIRECTIONS):
            a = 2.0 * math.pi * k / _RING_DIRECTIONS
            p = (self._xy[0] + step_m * math.sin(a), self._xy[1] + step_m * math.cos(a))
            plat, plon, px, py = _snap(*p)
            if not self.inside_geofence(plat, plon):
                continue
            key = (round(strict.depth(px, py), 6), k)
            if key[0] < here and (best_d is None or key < best_d):
                best_d = key
                best_pos = (plat, plon, px, py)
        if best_pos is not None:
            self._set_pos(*best_pos)

    def _set_pos(self, lat: float, lon: float, x: float, y: float) -> None:
        self._lat, self._lon = lat, lon
        self._xy = (x, y)

    def _make_snapshot(self, tick: int, speed: float) -> ResponderSnapshot:
        dist: float | None = None
        if self._contact is not None and self._target is not None and self._mode == "look":
            dist = round6(
                math.hypot(self._xy[0] - self._contact[0], self._xy[1] - self._contact[1])
            )
        elif self._contact is not None and self._target is not None and self._mode == "return":
            dist = round6(
                math.hypot(self._xy[0] - self._contact[0], self._xy[1] - self._contact[1])
            )
        return ResponderSnapshot(
            tick=tick,
            t_s=round6(tick * self.tick_s),
            lat=self._lat,
            lon=self._lon,
            heading_deg=round6(self._heading) % 360.0,
            speed_mps=round6(speed),
            battery_min=round6(self._battery),
            state=self._state,
            target_contact_id=self._target,
            standoff_m=self.config.standoff_m,
            distance_to_target_m=dist,
        )


def _route_length(a: XY, route: Sequence[XY]) -> float:
    total = 0.0
    prev = a
    for p in route:
        total += math.hypot(p[0] - prev[0], p[1] - prev[1])
        prev = p
    return total


def _walk(a: XY, route: Sequence[XY], step_m: float) -> tuple[XY, int]:
    """The point ``step_m`` along ``route`` from ``a`` (the last waypoint if shorter), and
    how many waypoints that passes or reaches."""
    left = step_m
    prev = a
    for n, p in enumerate(route):
        seg = math.hypot(p[0] - prev[0], p[1] - prev[1])
        if seg > left:
            f = left / seg
            return (prev[0] + f * (p[0] - prev[0]), prev[1] + f * (p[1] - prev[1])), n
        left -= seg
        prev = p
    return prev, len(route)


def _consume(route: list[XY], passed: int, pos: XY) -> None:
    """Drop the ``passed`` waypoints, then any next one within 0.5 m of ``pos``."""
    del route[:passed]
    while route and math.hypot(route[0][0] - pos[0], route[0][1] - pos[1]) < 0.5:
        route.pop(0)
