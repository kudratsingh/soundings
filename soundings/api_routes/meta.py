"""Read-only facts for the screen: scenarios, boundary table, About text, harbor map.

Everything here is served from config and code constants, never typed into the UI, so the
honesty text has one source (SPEC-INVESTIGATION section 8, SPEC-SIM sections 1-2). No route
here takes a request body.
"""

from __future__ import annotations

import dataclasses
import json
import re
from typing import Any

from fastapi import APIRouter

from soundings.ladder.boundary import load_boundary
from soundings.ladder.standing_orders import SIGNATURE_LABEL, load_verified, scenario_now
from soundings.registry.models import load_registry_config
from soundings.schemas import load_ladder_config
from soundings.schemas._base import CONFIG_DIR
from soundings.sim.detection import load_detection_params
from soundings.sim.scenario import catalog_paths, resolve_scenario
from soundings.stream.config import ui_config
from soundings.stream.events import POSTERIOR_LABEL

router = APIRouter(tags=["meta"])

HARBOR_GEOJSON = CONFIG_DIR / "harbor.geojson"
CATALOG_ID = re.compile(r"^(S\d{2})-")

DETECTION_HEADING = "Detection model: simulated for this demonstration"
MAP_NOTE = "Port of Los Angeles, simplified for demonstration"
SHARED_TABLES_NOTE = (
    "The demo sim and the scorer share the likelihood tables in config/likelihoods.yaml: "
    "the sim samples acoustic features from the same per-hypothesis tables the posterior "
    "uses, which is why the eval also includes mis-specified runs drawn from perturbed tables."
)
HANDOFF_LINE = "The work stops at the handoff."
DETECTION_ASSUMPTIONS = (
    "detection probability falls off with range from each hydrophone (p_max, r0_m), "
    "nothing beyond max_range_factor x r0",
    "position noise grows with range (noise_sigma_base_m + noise_sigma_per_m x range)",
    "tracks have gaps (gap_rate_per_tick, gap_mean_ticks) and close after "
    "track_timeout_ticks without a reading",
    "biologic false alarms arrive per node per hour (false_alarms_per_node_hour), shallow, "
    "often with breathing or clicks",
)


def scenario_ids() -> list[tuple[str, str]]:
    """(id, path) for every catalog file, sorted by id; never smoke or live files."""
    out: list[tuple[str, str]] = []
    for path in catalog_paths():
        m = CATALOG_ID.match(path.name)
        if m:
            out.append((m.group(1), path.name))
    return sorted(out)


@router.get("/api/scenarios")
def list_scenarios() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for sid, _ in scenario_ids():
        sc = resolve_scenario(sid)
        rows.append(
            {
                "id": sc.id,
                "name": sc.name,
                "seed": sc.seed,
                "duration_ticks": sc.duration_ticks,
                "tick_s": sc.tick_s,
            }
        )
    return rows


def standing_orders_summary() -> dict[str, Any]:
    """Signature status checked at the scenario anchor (sim time; no wall clock)."""
    now = scenario_now(0, 0.0)
    status = load_verified(now=now)
    orders = status.orders
    return {
        "signed": status.signed,
        "reason": status.reason,
        "signer": orders.signed_by if orders is not None else None,
        "signed_at": orders.signed_at.isoformat() if orders is not None else None,
        "valid_until": orders.valid_until.isoformat() if orders is not None else None,
        "checked_at": now.isoformat(),
        "signature_label": SIGNATURE_LABEL,
    }


@router.get("/api/boundary")
def boundary() -> dict[str, Any]:
    table = load_boundary()
    cfg = load_ladder_config()
    return {
        "rungs": [r.value for r in cfg.rungs],
        "lives_on": [r.value for r in cfg.lives_on],
        "rows": table.rows(),
        "never": list(table.never),
        "standing_orders": standing_orders_summary(),
    }


@router.get("/api/about")
def about() -> dict[str, Any]:
    ui = ui_config()
    params = dataclasses.asdict(load_detection_params())
    return {
        "title": "About Soundings",
        "honesty": {
            "detection_heading": DETECTION_HEADING,
            "detection_params": params,
            "detection_assumptions": list(DETECTION_ASSUMPTIONS),
            "probability_label": POSTERIOR_LABEL,
            "shared_tables_note": SHARED_TABLES_NOTE,
            "map_note": MAP_NOTE,
            "handoff_line": HANDOFF_LINE,
            "retention": (
                f"Records are kept no longer than {load_registry_config().retention_days} days."
            ),
            "model_mode": "replay",
        },
        "standing_orders": standing_orders_summary(),
        "tiles": ui.tiles.model_dump(),
        "tile_policy": ui.tile_policy.model_dump(),
        "colors": ui.colors.model_dump(),
        "default_speed": ui.default_speed,
    }


@router.get("/api/harbor")
def harbor() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(HARBOR_GEOJSON.read_text(encoding="utf-8"))
    return data
