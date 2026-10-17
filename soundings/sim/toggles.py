"""Twist toggle plumbing. All nine ``ToggleName`` members parse (WO-010 ``Toggle``).

Implemented now, using ``Toggle``'s flat fields:

- ``sensor_dropout{node_id, at_tick=0}``: the node's health is 0 from ``at_tick`` on, so
  it detects nothing and raises no false alarms;
- ``contact_surge{n, at_tick=0}``: ``n`` extra ground-truth contacts appear at
  ``at_tick``; each hypothesis is drawn from ``config/priors.yaml`` ``base`` and each
  hypothesis has one fixed behavior (``SURGE_BEHAVIORS``);
- ``contact_goes_quiet{contact_id, at_tick=0}``: applies ``silent_after_tick`` to that
  contact only (the earlier tick wins if the scenario already sets one).

The other six are recorded in ``ToggleReport.parsed_not_implemented`` and change nothing.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np

from soundings.schemas._base import StrictModel, from_file_data
from soundings.schemas.config import Priors
from soundings.schemas.enums import Hypothesis, ToggleName
from soundings.schemas.geo import from_local_m, to_local_m
from soundings.schemas.harbor import Harbor
from soundings.schemas.scenario import ScenarioContact, Toggle

IMPLEMENTED_TOGGLES: frozenset[ToggleName] = frozenset(
    {ToggleName.SENSOR_DROPOUT, ToggleName.CONTACT_SURGE, ToggleName.CONTACT_GOES_QUIET}
)


class ToggleReport(StrictModel):
    """Which toggles changed the run, in order of first appearance, without repeats."""

    implemented: tuple[ToggleName, ...] = ()
    parsed_not_implemented: tuple[ToggleName, ...] = ()
    surge_contact_ids: tuple[str, ...] = ()


def _first_seen(names: Sequence[ToggleName]) -> tuple[ToggleName, ...]:
    out: list[ToggleName] = []
    for name in names:
        if name not in out:
            out.append(name)
    return tuple(out)


def toggle_report(toggles: Sequence[Toggle], surge_ids: Sequence[str] = ()) -> ToggleReport:
    names = [t.name for t in toggles]
    return ToggleReport(
        implemented=_first_seen([n for n in names if n in IMPLEMENTED_TOGGLES]),
        parsed_not_implemented=_first_seen([n for n in names if n not in IMPLEMENTED_TOGGLES]),
        surge_contact_ids=tuple(surge_ids),
    )


def dropout_ticks(toggles: Sequence[Toggle], node_ids: Sequence[str]) -> dict[str, int]:
    """node_id -> first tick of zero health, for every ``sensor_dropout`` toggle."""
    out: dict[str, int] = {}
    for t in toggles:
        if t.name is not ToggleName.SENSOR_DROPOUT:
            continue
        if t.node_id is None:
            raise ValueError("sensor_dropout needs node_id")
        if t.node_id not in node_ids:
            raise ValueError(f"sensor_dropout: unknown node_id {t.node_id}")
        at = t.at_tick if t.at_tick is not None else 0
        out[t.node_id] = min(at, out.get(t.node_id, at))
    return out


def node_health(base_health: float, node_id: str, tick: int, dropouts: dict[str, int]) -> float:
    """A node's health at ``tick`` after ``sensor_dropout``."""
    at = dropouts.get(node_id)
    if at is not None and tick >= at:
        return 0.0
    return base_health


def quiet_ticks(toggles: Sequence[Toggle]) -> dict[str, int]:
    """contact_id -> ``silent_after_tick`` from every ``contact_goes_quiet`` toggle."""
    out: dict[str, int] = {}
    for t in toggles:
        if t.name is not ToggleName.CONTACT_GOES_QUIET:
            continue
        if t.contact_id is None:
            raise ValueError("contact_goes_quiet needs contact_id")
        at = t.at_tick if t.at_tick is not None else 0
        out[t.contact_id] = min(at, out.get(t.contact_id, at))
    return out


# ---------------------------------------------------------------------------
# contact_surge
# ---------------------------------------------------------------------------

# One fixed behavior per hypothesis (depth in metres). ``waypoint`` and ``target_asset``
# are filled per contact: a second random point near the same node, the nearest asset.
SURGE_BEHAVIORS: dict[Hypothesis, tuple[dict[str, Any], float]] = {
    Hypothesis.FILED_ON_PLAN: ({"type": "transit", "speed_mps": 1.0}, 8.0),
    Hypothesis.FILED_OFF_PLAN: ({"type": "drift", "jitter_mps": 0.05}, 6.0),
    Hypothesis.UNFILED_KNOWN_OPERATOR: ({"type": "transit", "speed_mps": 0.5}, 6.0),
    Hypothesis.BIOLOGIC: ({"type": "erratic", "speed_mps": 1.0, "drift_weight": 0.6}, 2.0),
    Hypothesis.DEBRIS_OR_DRIFT: ({"type": "drift", "jitter_mps": 0.05}, 1.5),
    Hypothesis.SURFACE_VESSEL_AIS_OFF: ({"type": "transit", "speed_mps": 2.0}, 0.5),
    Hypothesis.UNAUTHORIZED_UUV: (
        {"type": "orbit", "radius_m": 40.0, "speed_mps": 0.6},
        8.0,
    ),
    Hypothesis.UNAUTHORIZED_DIVER: ({"type": "stationary", "jitter_mps": 0.2}, 3.0),
}
SURGE_REPLIES: frozenset[Hypothesis] = frozenset(
    {Hypothesis.FILED_ON_PLAN, Hypothesis.FILED_OFF_PLAN, Hypothesis.UNFILED_KNOWN_OPERATOR}
)
SURGE_SPAWN_RADIUS_FRACTION = 0.5  # spawn within this fraction of the node's range_m


@dataclass(frozen=True, slots=True)
class SurgeContact:
    contact: ScenarioContact
    at_tick: int
    hypothesis: Hypothesis


def _pick_hypothesis(priors: Priors, u: float) -> Hypothesis:
    total = sum(priors.base[h] for h in Hypothesis)
    acc = 0.0
    for h in Hypothesis:
        acc += priors.base[h] / total
        if u < acc:
            return h
    return list(Hypothesis)[-1]


def _disk_point(cx: float, cy: float, radius: float, u_r: float, u_a: float) -> tuple[float, float]:
    r = radius * math.sqrt(u_r)
    a = 2.0 * math.pi * u_a
    return from_local_m(cx + r * math.cos(a), cy + r * math.sin(a))


def surge_contacts(
    toggles: Sequence[Toggle],
    harbor: Harbor,
    priors: Priors,
    rng: np.random.Generator,
) -> list[SurgeContact]:
    """The extra contacts of every ``contact_surge`` toggle, ids ``surge-01``, ``surge-02``, ...

    Each contact draws exactly six uniforms in a fixed order (hypothesis, node, start
    radius, start angle, waypoint radius, waypoint angle).
    """
    nodes = sorted(harbor.sensor_nodes, key=lambda n: n.node_id)
    out: list[SurgeContact] = []
    for t in toggles:
        if t.name is not ToggleName.CONTACT_SURGE:
            continue
        if t.n is None:
            raise ValueError("contact_surge needs n")
        at = t.at_tick if t.at_tick is not None else 0
        for _ in range(t.n):
            u = [float(v) for v in rng.random(6)]
            hyp = _pick_hypothesis(priors, u[0])
            node = nodes[min(int(u[1] * len(nodes)), len(nodes) - 1)]
            cx, cy = to_local_m(node.lat, node.lon)
            radius = SURGE_SPAWN_RADIUS_FRACTION * node.range_m
            lat, lon = _disk_point(cx, cy, radius, u[2], u[3])
            behavior, depth = SURGE_BEHAVIORS[hyp]
            behavior = dict(behavior)
            if behavior["type"] == "transit":
                wlat, wlon = _disk_point(cx, cy, radius, u[4], u[5])
                behavior["waypoint"] = {"lat": wlat, "lon": wlon}
            if behavior["type"] == "orbit":
                asset = harbor.nearest_asset(lat, lon)
                if asset is None:
                    raise ValueError("contact_surge orbit needs an asset in the harbor")
                behavior["target_asset"] = asset.asset_id
            data = {
                "id": f"surge-{len(out) + 1:02d}",
                "start": {"lat": lat, "lon": lon, "depth_m": depth},
                "behavior": behavior,
                "registry": {
                    "filed": hyp in (Hypothesis.FILED_ON_PLAN, Hypothesis.FILED_OFF_PLAN),
                    "replies_to_hail": hyp in SURGE_REPLIES,
                },
            }
            contact = ScenarioContact.model_validate(from_file_data(data))
            out.append(SurgeContact(contact=contact, at_tick=at, hypothesis=hyp))
    return out
