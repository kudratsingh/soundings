"""``GET /api/physics`` (WO-089): the physics model cards and each sensor node's range.

Read-only, served from ``config/physics.yaml`` and ``soundings.physics``; never typed into
the UI. The node ranges here are at base noise (calm sea state, no vessel transits); the
live rings during a run arrive as ``sensor_ranges`` stream events.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from soundings.physics_view import (
    DEMO_LABEL,
    PENDING_NOTE,
    PHYSICS_HEADING,
    RING_CLASS,
    node_ranges,
    physics_available,
    physics_cards,
    ring_inputs,
)
from soundings.schemas.harbor import load_harbor

router = APIRouter(tags=["physics"])


def physics_payload() -> dict[str, Any]:
    if not physics_available():
        return {
            "heading": PHYSICS_HEADING,
            "label": DEMO_LABEL,
            "cards": [],
            "nodes": [],
            "ring_class": RING_CLASS,
            "sea_state": None,
            "config": "config/physics.yaml",
            "pending": PENDING_NOTE,
        }
    inputs = ring_inputs()
    nodes = {n.node_id: n for n in load_harbor().sensor_nodes}
    return {
        "heading": PHYSICS_HEADING,
        "label": DEMO_LABEL,
        "cards": physics_cards(),
        "nodes": [
            {**r.as_dict(), "name": nodes[r.node_id].name}
            for r in node_ranges(list(nodes.values()), (), inputs=inputs)
        ],
        "ring_class": RING_CLASS,
        "sea_state": inputs.sea_state,
        "config": "config/physics.yaml",
        "pending": None,
    }


@router.get("/api/physics")
def physics() -> dict[str, Any]:
    return physics_payload()
