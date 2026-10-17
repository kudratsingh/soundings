"""A deterministic stand-in for ``soundings.sim.responder.Responder`` (WO-088 contract).

Agent A builds the real simulation; the probe tests run against this double through the
``ResponderPort`` Protocol. It keeps the contract's invariants in the simplest form: it
moves at ``speed_mps`` toward a station point ``standoff_m`` from the contact on the side
away from the nearest asset, never ends a step inside the standoff, stays inside the
declared zones, and reports ``ON_STATION`` once it reaches the station point. A search
dispatch runs a lawnmower over the drift box's bounding square.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from typing import Any

from soundings.canonical import round6
from soundings.schemas.geo import distance_m, from_local_m, to_local_m
from soundings.schemas.harbor import Harbor

SPEED_MPS = 1.5
STANDOFF_M = 25.0
ARRIVE_M = 1.0
LANE_SPACING_M = 100.0


@dataclass(frozen=True)
class Snap:
    tick: int
    t_s: float
    lat: float
    lon: float
    heading_deg: float
    speed_mps: float
    battery_min: float
    state: str
    target_contact_id: str | None
    standoff_m: float
    distance_to_target_m: float | None


@dataclass(frozen=True)
class Dispatch:
    accepted: bool
    reason: str
    eta_s: float


def zone_centroid(harbor: Harbor, zone_id: str) -> tuple[float, float]:
    poly = harbor.zone(zone_id).polygon
    return (
        sum(p[0] for p in poly) / len(poly),
        sum(p[1] for p in poly) / len(poly),
    )


class FakeResponder:
    def __init__(
        self, harbor: Harbor, dock: tuple[float, float], *, tick_s: float = 2.0, tick: int = 0
    ) -> None:
        self.harbor = harbor
        self.tick_s = tick_s
        self.dock = dock
        self._snap = Snap(
            tick, tick * tick_s, dock[0], dock[1], 0.0, 0.0, 90.0, "DOCKED", None, STANDOFF_M, None
        )
        self.trail: list[Snap] = [self._snap]
        self._mode: str | None = None
        self._target: tuple[float, float] | None = None
        self._waypoints: list[tuple[float, float]] = []
        self.dispatches: list[tuple[str, str, int]] = []

    @property
    def snapshot(self) -> Snap:
        return self._snap

    def inside_geofence(self, lat: float, lon: float) -> bool:
        return self.harbor.zone_at(lat, lon) is not None

    def travel_time_s(self, lat1: float, lon1: float, lat2: float, lon2: float) -> float:
        return distance_m(lat1, lon1, lat2, lon2) / SPEED_MPS

    def dispatch_to_look(
        self, contact_id: str, contact_lat: float, contact_lon: float, tick: int
    ) -> Dispatch:
        if self._snap.state != "DOCKED":
            return Dispatch(False, "busy", 0.0)
        if not self.inside_geofence(contact_lat, contact_lon):
            return Dispatch(False, "outside_geofence", 0.0)
        self._mode = "look"
        self._target = (contact_lat, contact_lon)
        self.dispatches.append(("look", contact_id, tick))
        self._snap = replace(self._snap, state="TRANSIT", target_contact_id=contact_id)
        eta = self.travel_time_s(self._snap.lat, self._snap.lon, contact_lat, contact_lon)
        return Dispatch(True, "dispatched_to_look", round6(eta))

    def dispatch_search(self, contact_id: str, search_box: Any, tick: int) -> Dispatch:
        if self._snap.state != "DOCKED":
            return Dispatch(False, "busy", 0.0)
        cx, cy = to_local_m(search_box.center_lat, search_box.center_lon)
        r = float(search_box.radius_m)
        lanes = max(1, math.ceil(2 * r / LANE_SPACING_M))
        pts: list[tuple[float, float]] = []
        for i in range(lanes + 1):
            x = cx - r + min(2 * r, i * LANE_SPACING_M)
            ys = (cy - r, cy + r) if i % 2 == 0 else (cy + r, cy - r)
            pts += [from_local_m(x, ys[0]), from_local_m(x, ys[1])]
        self._waypoints = [p for p in pts if self.inside_geofence(*p)]
        self._mode = "search"
        self.dispatches.append(("search", contact_id, tick))
        self._snap = replace(self._snap, state="TRANSIT", target_contact_id=contact_id)
        return Dispatch(True, "dispatched_to_search", 0.0)

    def recall(self, tick: int) -> None:
        self._mode = "return"
        self._waypoints = []
        self._snap = replace(self._snap, state="RETURNING", target_contact_id=None)

    def _move_toward(self, goal: tuple[float, float], budget_m: float) -> tuple[float, float]:
        x, y = to_local_m(self._snap.lat, self._snap.lon)
        gx, gy = to_local_m(*goal)
        d = math.hypot(gx - x, gy - y)
        if d <= budget_m:
            return goal
        f = budget_m / d
        nxt = from_local_m(x + (gx - x) * f, y + (gy - y) * f)
        return nxt if self.inside_geofence(*nxt) else (self._snap.lat, self._snap.lon)

    def step(
        self,
        tick: int,
        *,
        contact_lat: float | None = None,
        contact_lon: float | None = None,
        contact_speed_mps: float | None = None,
        nearest_asset_lat: float | None = None,
        nearest_asset_lon: float | None = None,
    ) -> Snap:
        budget = SPEED_MPS * self.tick_s * max(0, tick - self._snap.tick)
        lat, lon, state, dist = self._snap.lat, self._snap.lon, self._snap.state, None
        if self._mode == "look":
            if contact_lat is not None and contact_lon is not None:
                self._target = (contact_lat, contact_lon)
            assert self._target is not None
            tx, ty = to_local_m(*self._target)
            if nearest_asset_lat is not None and nearest_asset_lon is not None:
                ax, ay = to_local_m(nearest_asset_lat, nearest_asset_lon)
            else:
                ax, ay = to_local_m(self._snap.lat, self._snap.lon)
                ax, ay = 2 * tx - ax, 2 * ty - ay  # station on the responder's side
            ux, uy = tx - ax, ty - ay
            norm = math.hypot(ux, uy) or 1.0
            station = from_local_m(tx + ux / norm * STANDOFF_M, ty + uy / norm * STANDOFF_M)
            lat, lon = self._move_toward(station, budget)
            x, y = to_local_m(lat, lon)
            d = math.hypot(x - tx, y - ty)
            if d < STANDOFF_M:  # never inside the standoff: push out along the ray
                k = STANDOFF_M / (d or 1.0)
                lat, lon = from_local_m(tx + (x - tx) * k, ty + (y - ty) * k)
            dist = distance_m(lat, lon, *self._target)
            state = "ON_STATION" if distance_m(lat, lon, *station) <= ARRIVE_M else "TRANSIT"
        elif self._mode == "return":
            lat, lon = self._move_toward(self.dock, budget)
            if distance_m(lat, lon, *self.dock) <= ARRIVE_M:
                lat, lon, state, self._mode = self.dock[0], self.dock[1], "DOCKED", None
            else:
                state = "RETURNING"
        elif self._mode == "search":
            while budget > 1e-6 and self._waypoints:
                goal = self._waypoints[0]
                before = (lat, lon)
                self._snap = replace(self._snap, lat=lat, lon=lon)
                lat, lon = self._move_toward(goal, budget)
                budget -= distance_m(*before, lat, lon)
                if distance_m(lat, lon, *goal) <= ARRIVE_M or (lat, lon) == before:
                    self._waypoints.pop(0)  # reached, or the next step would leave the zones
            state = "TRANSIT" if self._waypoints else "RETURNING"
        self._snap = Snap(
            tick,
            round6(tick * self.tick_s),
            round6(lat),
            round6(lon),
            0.0,
            SPEED_MPS if state == "TRANSIT" else 0.0,
            self._snap.battery_min,
            state,
            self._snap.target_contact_id,
            STANDOFF_M,
            None if dist is None else round6(dist),
        )
        self.trail.append(self._snap)
        return self._snap
