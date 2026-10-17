"""The harbor map: zones, assets and sensor nodes, loaded from ``config/harbor.geojson``.

Ids match ``^[a-z][a-z0-9_]*$`` and are unique across all three kinds. Polygons are
stored as ``(lat, lon)`` vertices without the closing duplicate (GeoJSON stores
``[lon, lat]``; ``load_harbor`` converts). Every lookup that could tie breaks the tie on
the lowest id, so results never depend on file or set order.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import Field, model_validator

from soundings.canonical import round6
from soundings.schemas._base import (
    CONFIG_DIR,
    HarborId,
    Lat,
    Lon,
    PositiveFloat,
    Probability,
    StrictModel,
    from_file_data,
)
from soundings.schemas.denylist import FreeText
from soundings.schemas.geo import distance_m, point_in_polygon

DEFAULT_HARBOR_PATH: Path = CONFIG_DIR / "harbor.geojson"

LatLon = tuple[Lat, Lon]


class Zone(StrictModel):
    zone_id: HarborId
    name: FreeText
    criticality: Probability
    basin: HarborId
    polygon: Annotated[tuple[LatLon, ...], Field(min_length=3)]

    def contains(self, lat: float, lon: float) -> bool:
        return point_in_polygon(lat, lon, self.polygon)


class Asset(StrictModel):
    asset_id: HarborId
    name: FreeText
    criticality: Probability
    asset_kind: Literal["berth", "gate", "piling"]
    lat: Lat
    lon: Lon


class SensorNode(StrictModel):
    node_id: HarborId
    name: FreeText
    lat: Lat
    lon: Lon
    range_m: PositiveFloat
    health: Probability


class Harbor(StrictModel):
    name: FreeText
    zones: tuple[Zone, ...]
    assets: tuple[Asset, ...]
    sensor_nodes: tuple[SensorNode, ...]

    @model_validator(mode="after")
    def _unique_ids(self) -> Harbor:
        ids = [z.zone_id for z in self.zones]
        ids += [a.asset_id for a in self.assets]
        ids += [n.node_id for n in self.sensor_nodes]
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        if dupes:
            raise ValueError(f"harbor ids must be unique across kinds: {dupes}")
        return self

    def zone_at(self, lat: float, lon: float) -> Zone | None:
        """The containing zone with the highest criticality (ties: lowest zone_id), or None."""
        inside = [z for z in self.zones if z.contains(lat, lon)]
        if not inside:
            return None
        return min(inside, key=lambda z: (-z.criticality, z.zone_id))

    def nearest_asset(self, lat: float, lon: float, min_criticality: float = 0.0) -> Asset | None:
        """The nearest asset with criticality >= ``min_criticality`` (ties: lowest asset_id)."""
        eligible = [a for a in self.assets if a.criticality >= min_criticality]
        if not eligible:
            return None
        return min(eligible, key=lambda a: (round6(distance_m(lat, lon, a.lat, a.lon)), a.asset_id))

    def basins(self) -> list[str]:
        """Sorted unique basin names."""
        return sorted({z.basin for z in self.zones})

    def zone(self, zone_id: str) -> Zone:
        for z in self.zones:
            if z.zone_id == zone_id:
                return z
        raise KeyError(zone_id)

    def asset(self, asset_id: str) -> Asset:
        for a in self.assets:
            if a.asset_id == asset_id:
                return a
        raise KeyError(asset_id)

    def node(self, node_id: str) -> SensorNode:
        for n in self.sensor_nodes:
            if n.node_id == node_id:
                return n
        raise KeyError(node_id)


def _point(geometry: dict[str, Any]) -> dict[str, Any]:
    if geometry.get("type") != "Point":
        raise ValueError("asset and sensor_node features need Point geometry")
    lon, lat = geometry["coordinates"]
    return {"lat": lat, "lon": lon}


def _polygon(geometry: dict[str, Any]) -> tuple[tuple[Any, Any], ...]:
    if geometry.get("type") != "Polygon":
        raise ValueError("zone features need Polygon geometry")
    ring = [(lat, lon) for lon, lat in geometry["coordinates"][0]]
    if len(ring) > 1 and ring[0] == ring[-1]:
        ring = ring[:-1]
    return tuple(ring)


def harbor_from_geojson(doc: dict[str, Any]) -> Harbor:
    """Build a ``Harbor`` from a parsed GeoJSON FeatureCollection."""
    if doc.get("type") != "FeatureCollection":
        raise ValueError("harbor file must be a GeoJSON FeatureCollection")
    zones: list[dict[str, Any]] = []
    assets: list[dict[str, Any]] = []
    nodes: list[dict[str, Any]] = []
    for feature in doc["features"]:
        props = dict(feature["properties"])
        kind = props.pop("kind")
        geometry = feature["geometry"]
        if kind == "zone":
            zones.append({**props, "polygon": _polygon(geometry)})
        elif kind == "asset":
            assets.append({**props, **_point(geometry)})
        elif kind == "sensor_node":
            nodes.append({**props, **_point(geometry)})
        else:
            raise ValueError(f"unknown feature kind {kind!r}")
    data = {"name": doc["name"], "zones": zones, "assets": assets, "sensor_nodes": nodes}
    return Harbor.model_validate(from_file_data(data))


def load_harbor(path: str | Path = DEFAULT_HARBOR_PATH) -> Harbor:
    """Load and validate the harbor map (default ``config/harbor.geojson``)."""
    doc = json.loads(Path(path).read_text(encoding="utf-8"))
    return harbor_from_geojson(doc)
