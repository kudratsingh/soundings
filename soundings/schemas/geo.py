"""Pure geometry over the harbor: an equirectangular projection about a fixed origin.

The harbor spans a few kilometres, so a local flat-earth frame is accurate to well under
a metre here. ``x`` is metres east of the origin and ``y`` metres north. Polygons are
sequences of ``(lat, lon)`` vertices, open or closed.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

EARTH_RADIUS_M = 6_371_000.0
ORIGIN_LAT = 33.74
ORIGIN_LON = -118.265
_COS_ORIGIN = math.cos(math.radians(ORIGIN_LAT))


def to_local_m(lat: float, lon: float) -> tuple[float, float]:
    """``(lat, lon)`` in degrees to ``(x_east_m, y_north_m)`` about the fixed origin."""
    x = EARTH_RADIUS_M * math.radians(lon - ORIGIN_LON) * _COS_ORIGIN
    y = EARTH_RADIUS_M * math.radians(lat - ORIGIN_LAT)
    return x, y


def from_local_m(x: float, y: float) -> tuple[float, float]:
    """Inverse of ``to_local_m``: ``(x_east_m, y_north_m)`` to ``(lat, lon)`` in degrees."""
    lat = ORIGIN_LAT + math.degrees(y / EARTH_RADIUS_M)
    lon = ORIGIN_LON + math.degrees(x / (EARTH_RADIUS_M * _COS_ORIGIN))
    return lat, lon


def distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Planar distance in metres between two ``(lat, lon)`` points."""
    x1, y1 = to_local_m(lat1, lon1)
    x2, y2 = to_local_m(lat2, lon2)
    return math.hypot(x2 - x1, y2 - y1)


def point_in_polygon(lat: float, lon: float, polygon: Sequence[tuple[float, float]]) -> bool:
    """Ray casting (even-odd rule) over ``(lat, lon)`` vertices. Fewer than 3 is False."""
    n = len(polygon)
    if n < 3:
        return False
    inside = False
    j = n - 1
    for i in range(n):
        lat_i, lon_i = polygon[i]
        lat_j, lon_j = polygon[j]
        if (lat_i > lat) != (lat_j > lat):
            lon_cross = lon_i + (lat - lat_i) * (lon_j - lon_i) / (lat_j - lat_i)
            if lon < lon_cross:
                inside = not inside
        j = i
    return inside
