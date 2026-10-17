"""Smoke scenario: one contact doing a slow, seeded random walk.

Determinism rules (docs/WORKFLOW.md section 8): the RNG is created from the seed and
passed explicitly, there is no module-level RNG, no wall-clock read, and floats are
rounded to 6 dp at the output boundary.
"""

from __future__ import annotations

import math
from typing import NamedTuple

import numpy as np

COLUMNS: tuple[str, ...] = ("tick", "contact_id", "lat", "lon", "depth_m", "speed_mps")

# Start point: an arbitrary spot in a working harbor basin. Only the shape matters here.
START_LAT = 33.7300
START_LON = -118.2600
START_DEPTH_M = 12.0
START_SPEED_MPS = 1.0
TICK_SECONDS = 1.0
METERS_PER_DEG_LAT = 111_320.0
CONTACT_ID = "C-001"
DECIMALS = 6


class SmokeRow(NamedTuple):
    tick: int
    contact_id: str
    lat: float
    lon: float
    depth_m: float
    speed_mps: float


def _step(
    rng: np.random.Generator,
    lat: float,
    lon: float,
    depth_m: float,
    speed_mps: float,
    heading_rad: float,
) -> tuple[float, float, float, float, float]:
    heading_rad = (heading_rad + float(rng.normal(0.0, 0.15))) % (2.0 * math.pi)
    speed_mps = min(max(speed_mps + float(rng.normal(0.0, 0.05)), 0.2), 2.5)
    depth_m = min(max(depth_m + float(rng.normal(0.0, 0.2)), 2.0), 30.0)
    dist_m = speed_mps * TICK_SECONDS
    lat += dist_m * math.cos(heading_rad) / METERS_PER_DEG_LAT
    lon += dist_m * math.sin(heading_rad) / (METERS_PER_DEG_LAT * math.cos(math.radians(lat)))
    return lat, lon, depth_m, speed_mps, heading_rad


def run_smoke(seed: int, ticks: int) -> list[SmokeRow]:
    """Return one row per tick for a single contact. Pure: depends only on its arguments."""
    if ticks < 0:
        raise ValueError("ticks must be >= 0")
    rng = np.random.default_rng(seed)
    lat, lon, depth_m, speed_mps = START_LAT, START_LON, START_DEPTH_M, START_SPEED_MPS
    heading_rad = float(rng.uniform(0.0, 2.0 * math.pi))
    rows: list[SmokeRow] = []
    for tick in range(ticks):
        rows.append(
            SmokeRow(
                tick=tick,
                contact_id=CONTACT_ID,
                lat=round(lat, DECIMALS),
                lon=round(lon, DECIMALS),
                depth_m=round(depth_m, DECIMALS),
                speed_mps=round(speed_mps, DECIMALS),
            )
        )
        lat, lon, depth_m, speed_mps, heading_rad = _step(
            rng, lat, lon, depth_m, speed_mps, heading_rad
        )
    return rows


def format_rows(rows: list[SmokeRow]) -> str:
    """Render rows as a tab-separated table with a header line and fixed 6 dp floats."""
    lines = ["\t".join(COLUMNS)]
    for r in rows:
        lines.append(
            f"{r.tick}\t{r.contact_id}\t{r.lat:.6f}\t{r.lon:.6f}\t{r.depth_m:.6f}\t{r.speed_mps:.6f}"
        )
    return "\n".join(lines) + "\n"
