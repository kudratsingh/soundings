"""WO-089 part 2: what the screen and the deck show of the physics models.

``soundings.physics`` (WO-089 part 1) owns the equations and ``config/physics.yaml`` owns
every parameter. This module only reads them for display: the model cards with each
parameter's value, unit, source and "demo value" label; and the live sensor range rings,
the range at which the passive-sonar detection probability is 0.5 for each hydrophone node
given that node's current ambient noise (base + sea state + nearby vessel transits).

Until part 1 is on main (``soundings/physics.py`` and ``config/physics.yaml``),
``physics_available()`` is false: there are no cards, the stream sends no rings (the map
keeps each node's harbor ``range_m``), and the deck keeps its one-line disclaimer.

Nothing here decides anything (CLAUDE.md 2.2): it turns config and observed AIS fixes into
numbers to draw. Pure and deterministic (CLAUDE.md 2.4): no clock, no RNG, sorted
iteration, 6-dp rounding at the boundary.
"""

from __future__ import annotations

import importlib
import importlib.util
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Protocol

import yaml

from soundings.canonical import round6
from soundings.schemas._base import CONFIG_DIR
from soundings.schemas.harbor import SensorNode, load_harbor

PHYSICS_YAML = CONFIG_DIR / "physics.yaml"
PENDING_NOTE = "The physics cards arrive with WO-089 part 1 (soundings/physics.py)."
PHYSICS_HEADING = "Detection model: simulated for this demonstration"
DEMO_LABEL = "demo value"
RING_CLASS = "propeller_tonal_present"
"""The acoustic class a ring is drawn for: an uncooperative vehicle with a propeller tonal."""
VESSEL_FIX_MAX_AGE_S = 30.0
"""An AIS fix older than this no longer adds vessel noise at a node (three 10 s reports)."""
NOT_MODELED_FLOOR = ("hydrodynamics", "beam patterns", "multipath", "bottom type")
"""The owner's list (2026-10-17 07:50): every card says at least these are not modeled."""


class AisFix(Protocol):
    """One AIS-style surface report (``soundings.sim.types.AisReport`` satisfies it)."""

    @property
    def tick(self) -> int: ...
    @property
    def lat(self) -> float: ...
    @property
    def lon(self) -> float: ...
    @property
    def sog_mps(self) -> float: ...


@dataclass(frozen=True, slots=True)
class SensorRange:
    """One node's ring: the p = 0.5 range and the noise level it was computed from."""

    node_id: str
    effective_range_m: float
    nl_db: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "effective_range_m": self.effective_range_m,
            "nl_db": self.nl_db,
        }


def physics_available(path: Path | None = None) -> bool:
    """True once WO-089 part 1 is on this branch: the module and its config both exist."""
    return (path or PHYSICS_YAML).is_file() and (
        importlib.util.find_spec("soundings.physics") is not None
    )


def _physics() -> Any:
    """``soundings.physics`` (WO-089 part 1), imported by name so this module type-checks
    and imports before part 1 is on the branch."""
    return importlib.import_module("soundings.physics")


# --- config walking ---------------------------------------------------------------------


def _is_param(node: object) -> bool:
    return isinstance(node, Mapping) and "value" in node and "source" in node


def iter_params(
    data: Mapping[str, Any], prefix: str = ""
) -> Iterator[tuple[str, Mapping[str, Any]]]:
    """Every ``{value, unit, source, label}`` leaf of a physics mapping, in key order."""
    for key in sorted(data, key=str):
        node = data[key]
        path = f"{prefix}.{key}" if prefix else str(key)
        if _is_param(node):
            yield path, node
        elif isinstance(node, Mapping):
            yield from iter_params(node, path)


@lru_cache(maxsize=4)
def _raw_cached(path: str) -> dict[str, Any]:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected a mapping at the top level")
    return data


def raw_physics(path: Path | None = None) -> dict[str, Any]:
    """``config/physics.yaml`` as a plain mapping (the deck and the cards read it)."""
    return _raw_cached(str(path or PHYSICS_YAML))


def dig(data: Mapping[str, Any], path: str) -> Any:
    cur: Any = data
    for part in path.split("."):
        if not isinstance(cur, Mapping):
            raise KeyError(path)
        cur = cur[part] if part in cur else cur[int(part)] if part.isdigit() else cur[part]
    return cur


def param_value(data: Mapping[str, Any], path: str) -> float:
    node = dig(data, path)
    if not _is_param(node):
        raise KeyError(f"{path} is not a parameter")
    return float(node["value"])


def param_row(path: str, node: Mapping[str, Any]) -> dict[str, Any]:
    value = node["value"]
    return {
        "key": path,
        "name": path.split(".", 1)[-1],
        "value": round6(float(value)) if isinstance(value, int | float) else value,
        "unit": str(node.get("unit") or ""),
        "source": str(node["source"]),
        "label": str(node.get("label") or DEMO_LABEL),
    }


# --- cards ------------------------------------------------------------------------------


def _card_param_keys(card: Any) -> list[str]:
    """The config paths a card cites, whatever form part 1's ``parameters`` take."""
    keys: list[str] = []
    for entry in getattr(card, "parameters", ()) or ():
        if isinstance(entry, str):
            keys.append(entry)
        elif isinstance(entry, Mapping):
            key = entry.get("key") or entry.get("path") or entry.get("name")
            if key:
                keys.append(str(key))
        else:
            key = getattr(entry, "key", None) or getattr(entry, "path", None)
            key = key or getattr(entry, "name", None)
            if key:
                keys.append(str(key))
    return keys


def _resolve(data: Mapping[str, Any], key: str) -> list[tuple[str, Mapping[str, Any]]]:
    """A card's parameter key to its leaves: an exact path, a subtree, or a leaf name."""
    try:
        node = dig(data, key)
    except (KeyError, IndexError, ValueError):
        node = None
    if _is_param(node):
        return [(key, node)]
    if isinstance(node, Mapping):
        return list(iter_params(node, key))
    hits = [(p, n) for p, n in iter_params(data) if p.endswith(f".{key}") or p == key]
    return hits


def physics_cards(path: Path | None = None) -> list[dict[str, Any]]:
    """One card per model: name, equation, parameters (value, unit, source, label), and
    what the model does not include. Equations come from ``soundings.physics.model_cards``;
    every number comes from ``config/physics.yaml``."""
    if not physics_available(path):
        return []
    model_cards = _physics().model_cards

    data = raw_physics(path)
    out: list[dict[str, Any]] = []
    for card in model_cards():
        rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        for key in _card_param_keys(card):
            for leaf, node in _resolve(data, key):
                if leaf not in seen:
                    seen.add(leaf)
                    rows.append(param_row(leaf, node))
        not_modeled = [str(x) for x in getattr(card, "not_modeled", ()) or ()]
        for item in NOT_MODELED_FLOOR:
            if item not in not_modeled:
                not_modeled.append(item)
        out.append(
            {
                "name": str(card.name),
                "equation": str(card.equation),
                "parameters": rows,
                "not_modeled": not_modeled,
            }
        )
    return out


# --- rings ------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RingInputs:
    """What every ring shares: the source level of the ring's class and the calm-water NL."""

    sl_db: float
    base_db: float
    sea_state: int


def ring_inputs(path: Path | None = None) -> RingInputs:
    data = raw_physics(path)
    levels = dig(data, "sonar.source_level_db")
    cls = RING_CLASS if RING_CLASS in levels else sorted(levels)[0]
    table = dig(data, "sonar.sea_state_table")
    try:
        sea_state = int(param_value(data, "sonar.sea_state_default"))
    except KeyError:
        sea_state = min(int(k) for k in table)
    return RingInputs(
        sl_db=param_value(data, f"sonar.source_level_db.{cls}"),
        base_db=param_value(data, "sonar.ambient_noise_base_db"),
        sea_state=sea_state,
    )


def node_ranges(
    nodes: Sequence[SensorNode],
    vessel_fixes: Sequence[tuple[float, float, float]] = (),
    *,
    inputs: RingInputs | None = None,
    cfg: Any = None,
) -> list[SensorRange]:
    """Each node's ring at the given vessel fixes ``(lat, lon, speed_mps)``, by node id."""
    phys = _physics()
    ambient_noise_db = phys.ambient_noise_db
    effective_range_m = phys.effective_range_m
    load_physics_config = phys.load_physics_config

    inp = inputs or ring_inputs()
    conf = cfg if cfg is not None else load_physics_config()
    out: list[SensorRange] = []
    for node in sorted(nodes, key=lambda n: n.node_id):
        nl = ambient_noise_db(
            inp.base_db, inp.sea_state, list(vessel_fixes), node.lat, node.lon, conf
        )
        r = effective_range_m(inp.sl_db, nl, conf)
        out.append(SensorRange(node.node_id, round6(max(0.0, r)), round6(nl)))
    return out


def vessel_fixes_at(
    ais_tracks: Mapping[str, Sequence[AisFix]],
    tick: int,
    tick_s: float,
    max_age_s: float = VESSEL_FIX_MAX_AGE_S,
) -> list[tuple[float, float, float]]:
    """Each vessel's latest report at or before ``tick`` and no older than ``max_age_s``."""
    fixes: list[tuple[float, float, float]] = []
    for vid in sorted(ais_tracks):
        latest: AisFix | None = None
        for rep in ais_tracks[vid]:
            if rep.tick > tick:
                break
            latest = rep
        if latest is not None and (tick - latest.tick) * tick_s <= max_age_s:
            fixes.append((float(latest.lat), float(latest.lon), float(latest.sog_mps)))
    return fixes


def sensor_range_series(
    ais_tracks: Mapping[str, Sequence[AisFix]],
    n_ticks: int,
    tick_s: float,
    nodes: Sequence[SensorNode] | None = None,
) -> dict[int, list[SensorRange]]:
    """The rings at tick 0 and at every tick where any ring changes (the stream sends
    these; the screen holds the last one). Without vessels that is tick 0 alone; without
    WO-089 part 1 it is empty."""
    if not physics_available():
        return {}
    load_physics_config = _physics().load_physics_config

    hub = list(nodes) if nodes is not None else list(load_harbor().sensor_nodes)
    inp = ring_inputs()
    conf = load_physics_config()
    tracks = {k: sorted(v, key=lambda r: r.tick) for k, v in sorted(ais_tracks.items())}
    out: dict[int, list[SensorRange]] = {}
    last: list[SensorRange] | None = None
    last_fixes: list[tuple[float, float, float]] | None = None
    for tick in range(max(1, n_ticks)):
        fixes = vessel_fixes_at(tracks, tick, tick_s) if tracks else []
        if fixes == last_fixes:
            continue
        last_fixes = fixes
        rings = node_ranges(hub, fixes, inputs=inp, cfg=conf)
        if rings != last:
            out[tick] = rings
            last = rings
    return out


__all__ = [
    "DEMO_LABEL",
    "NOT_MODELED_FLOOR",
    "PHYSICS_HEADING",
    "PHYSICS_YAML",
    "RING_CLASS",
    "RingInputs",
    "SensorRange",
    "iter_params",
    "node_ranges",
    "param_row",
    "PENDING_NOTE",
    "physics_available",
    "physics_cards",
    "raw_physics",
    "ring_inputs",
    "sensor_range_series",
    "vessel_fixes_at",
]
