"""Helpers for the WO-061 browser smokes (offline routing, lat/lon to map pixels)."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

LOOPBACK = {"127.0.0.1", "localhost", "::1"}


def offline(route: Any) -> None:
    """Abort every request that leaves the machine (the panel must work offline)."""
    host = urlparse(route.request.url).hostname
    if host in LOOPBACK:
        route.continue_()
    else:
        route.abort()


def map_pixel(page: Any, base: str, lat: float, lon: float) -> tuple[float, float]:
    """The page pixel of (lat, lon), fitted from two asset markers' screen positions."""
    harbor = page.request.get(f"{base}/api/harbor").json()
    assets = [f for f in harbor["features"] if f["properties"].get("kind") == "asset"]
    boxes = page.evaluate(
        "() => [...document.querySelectorAll('.asset-mark')].map(e => {"
        " const r = e.getBoundingClientRect(); return [r.x + r.width / 2, r.y + r.height / 2] })"
    )
    pts = [(a["geometry"]["coordinates"], b) for a, b in zip(assets, boxes, strict=False)]
    (lon0, lat0), (x0, y0) = min(pts, key=lambda p: p[0][0])
    (lon1, _), (x1, _) = max(pts, key=lambda p: p[0][0])
    (_, lat2), (_, y2) = min(pts, key=lambda p: p[0][1])
    (_, lat3), (_, y3) = max(pts, key=lambda p: p[0][1])
    x = x0 + (lon - lon0) * (x1 - x0) / (lon1 - lon0)
    y = y2 + (lat - lat2) * (y3 - y2) / (lat3 - lat2)
    return x, y
