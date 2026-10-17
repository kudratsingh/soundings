"""Judge injection (WO-061): a clicked point and a behavior become a live scenario file.

This is scenario authoring, so it lives with the simulator (the only package besides eval
that may touch a scenario's answer key, ``tests/test_boundary.py``); ``soundings.inject``
re-exports it under the names the WO gives.

A judge describes a contact (where, how it moves, how it sounds); a fixed, documented
table (``BEHAVIOR_TABLE``) turns that into a WO-010 ``Scenario`` with id ``L-<stamp>`` and
an empty ``expected`` block, written to ``scenarios/live/`` (gitignored). The run then goes
through the same loader and runner as the catalog, so the contact can be reproduced from
the file and its seed (SPEC-SIM section 5). Nothing here names a rung, a state or a
threshold (CLAUDE.md 2.2): the request carries a position, a behavior and an acoustic
profile only, and the ladder decides the rest.

Pure apart from ``write_live_scenario``. A point outside every declared security zone is
refused (CLAUDE.md section 6: no surveillance outside declared zones).
"""

from __future__ import annotations

import enum
import hashlib
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml
from pydantic import BaseModel, ConfigDict

from soundings.canonical import canonical_json, round6
from soundings.schemas.enums import BehaviorType, Hypothesis, TonalBand
from soundings.schemas.geo import distance_m
from soundings.schemas.harbor import Asset, Harbor
from soundings.schemas.scenario import Scenario, load_scenario, scenario_from_data

PT = ZoneInfo("America/Los_Angeles")
LIVE_DIR_ENV = "SOUNDINGS_LIVE_DIR"
DEFAULT_LIVE_DIR = Path(__file__).resolve().parents[2] / "scenarios" / "live"
STAMP_FORMAT = "%Y%m%dT%H%M%S"
DURATION_TICKS = 600
TICK_S = 2.0
START_DEPTH_M = 6.0
CRITICAL_RADIUS_M = 1000.0
""""Nearest critical asset": the highest-criticality asset within this range, else the nearest."""


class JudgeBehavior(enum.StrEnum):
    FILED = "filed"
    UNFILED = "unfiled"
    CLOSING = "closing"
    LOITERING = "loitering"
    SILENT = "silent"


class AcousticProfile(enum.StrEnum):
    PROPELLER = "propeller"
    BIOLOGIC = "biologic"
    QUIET = "quiet"


class OutsideZonesError(ValueError):
    """The point lies outside every declared security zone (HTTP 422)."""

    def __init__(self) -> None:
        super().__init__("outside the declared security zones")


class InjectRequest(BaseModel):
    """What a judge may say about a contact: where, how it moves, how it sounds."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    lat: float
    lon: float
    behavior: JudgeBehavior
    acoustic: AcousticProfile | None = None
    seed: int | None = None


@dataclass(frozen=True)
class BehaviorRow:
    """One row of the documented mapping (the panel's help text shows the same data)."""

    behavior_type: BehaviorType
    filed: bool
    replies_to_hail: bool
    known_operator: bool
    default_acoustic: AcousticProfile
    tonal_band: TonalBand
    ground_truth: Hypothesis
    help: str


BEHAVIOR_TABLE: dict[JudgeBehavior, BehaviorRow] = {
    JudgeBehavior.FILED: BehaviorRow(
        BehaviorType.LAWNMOWER,
        filed=True,
        replies_to_hail=True,
        known_operator=False,
        default_acoustic=AcousticProfile.QUIET,
        tonal_band=TonalBand.MID,
        ground_truth=Hypothesis.FILED_ON_PLAN,
        help="lawnmower over the clicked zone; filed plan covering the zone and window",
    ),
    JudgeBehavior.UNFILED: BehaviorRow(
        BehaviorType.LAWNMOWER,
        filed=False,
        replies_to_hail=True,
        known_operator=True,
        default_acoustic=AcousticProfile.QUIET,
        tonal_band=TonalBand.MID,
        ground_truth=Hypothesis.UNFILED_KNOWN_OPERATOR,
        help="lawnmower over the clicked zone; not filed; a known operator's modem answers",
    ),
    JudgeBehavior.CLOSING: BehaviorRow(
        BehaviorType.TRANSIT,
        filed=False,
        replies_to_hail=False,
        known_operator=False,
        default_acoustic=AcousticProfile.PROPELLER,
        tonal_band=TonalBand.MID,
        ground_truth=Hypothesis.UNAUTHORIZED_UUV,
        help="transit to the nearest critical asset at 1.0 m/s; not filed; no reply",
    ),
    JudgeBehavior.LOITERING: BehaviorRow(
        BehaviorType.ORBIT,
        filed=False,
        replies_to_hail=False,
        known_operator=False,
        default_acoustic=AcousticProfile.PROPELLER,
        tonal_band=TonalBand.LOW,
        ground_truth=Hypothesis.UNAUTHORIZED_UUV,
        help="orbit the nearest critical asset at 40 m; not filed; no reply",
    ),
    JudgeBehavior.SILENT: BehaviorRow(
        BehaviorType.LAWNMOWER_THEN_DRIFT,
        filed=True,
        replies_to_hail=True,
        known_operator=False,
        default_acoustic=AcousticProfile.QUIET,
        tonal_band=TonalBand.MID,
        ground_truth=Hypothesis.FILED_OFF_PLAN,
        help="filed survey that goes silent and drifts from tick 60",
    ),
}
CLOSING_SPEED_MPS = 1.0
LOITER_RADIUS_M = 40.0
SILENT_TICK = 60
LIVE_PLAN_MODEM = "MOD-LIVE"
LIVE_OPERATOR = "Judge survey team (demo)"


KNOWN_OPERATORS_FILE = Path(__file__).resolve().parents[2] / "config" / "known_operators.yaml"


def load_known_ops(path: Path = KNOWN_OPERATORS_FILE) -> list[dict[str, str]]:
    """``config/known_operators.yaml`` entries in file order (modem_id, operator)."""
    doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return [
        {"modem_id": str(e["modem_id"]), "operator": str(e["operator"])}
        for e in doc.get("known_operators") or []
    ]


def live_dir() -> Path:
    """``$SOUNDINGS_LIVE_DIR`` or ``scenarios/live``."""
    env = os.environ.get(LIVE_DIR_ENV)
    return Path(env) if env else DEFAULT_LIVE_DIR


def live_seed(req: InjectRequest) -> int:
    """The request's seed, else the first 8 hex digits of ``sha256(canonical_json(req))``."""
    if req.seed is not None:
        return int(req.seed)
    digest = hashlib.sha256(canonical_json(req.model_dump(mode="json")).encode("utf-8"))
    return int(digest.hexdigest()[:8], 16)


def stamp_of(when: datetime) -> str:
    """``YYYYMMDDTHHMMSS`` in Pacific time (an aware ``when``; a naive one is taken as PT)."""
    local = when.replace(tzinfo=PT) if when.tzinfo is None else when.astimezone(PT)
    return local.strftime(STAMP_FORMAT)


def critical_asset(harbor: Harbor, lat: float, lon: float) -> Asset:
    """The highest-criticality asset within 1 km (ties: nearer, then id), else the nearest."""
    near = [a for a in harbor.assets if distance_m(lat, lon, a.lat, a.lon) <= CRITICAL_RADIUS_M]
    if near:
        return min(
            near,
            key=lambda a: (-a.criticality, round6(distance_m(lat, lon, a.lat, a.lon)), a.asset_id),
        )
    nearest = harbor.nearest_asset(lat, lon)
    if nearest is None:
        raise ValueError("the harbor declares no assets")
    return nearest


def _acoustic(profile: AcousticProfile, band: TonalBand) -> dict[str, Any]:
    if profile is AcousticProfile.PROPELLER:
        return {"propeller_tonal": True, "tonal_band": band.value, "biologic_signature": False}
    if profile is AcousticProfile.BIOLOGIC:
        return {"propeller_tonal": False, "tonal_band": "none", "biologic_signature": True}
    return {"propeller_tonal": False, "tonal_band": "none", "biologic_signature": False}


def first_known_modem(known_ops: Any) -> tuple[str, str]:
    """(modem_id, operator) of the first entry of ``config/known_operators.yaml``."""
    entries = getattr(known_ops, "entries", None)
    if entries is None:
        entries = known_ops
    first = list(entries)[0]
    if isinstance(first, dict):
        return str(first["modem_id"]), str(first["operator"])
    return str(first.modem_id), str(first.operator)


def scenario_data(
    req: InjectRequest, stamp: datetime, harbor: Harbor, known_ops: Any
) -> dict[str, Any]:
    """The scenario mapping of one request (the YAML body, keys in file order)."""
    zone = harbor.zone_at(req.lat, req.lon)
    if zone is None:
        raise OutsideZonesError()
    row = BEHAVIOR_TABLE[req.behavior]
    s = stamp_of(stamp)
    hour = (stamp.replace(tzinfo=PT) if stamp.tzinfo is None else stamp.astimezone(PT)).hour
    profile = req.acoustic if req.acoustic is not None else row.default_acoustic
    behavior: dict[str, Any] = {"type": row.behavior_type.value}
    if row.behavior_type is BehaviorType.TRANSIT:
        behavior["target_asset"] = critical_asset(harbor, req.lat, req.lon).asset_id
        behavior["speed_mps"] = CLOSING_SPEED_MPS
    elif row.behavior_type is BehaviorType.ORBIT:
        behavior["target_asset"] = critical_asset(harbor, req.lat, req.lon).asset_id
        behavior["radius_m"] = LOITER_RADIUS_M
    elif row.behavior_type is BehaviorType.LAWNMOWER:
        behavior["area"] = zone.zone_id
    else:  # lawnmower, then drift
        behavior["area"] = zone.zone_id
        behavior["fault_tick"] = SILENT_TICK
    plans: list[dict[str, Any]] = []
    registry: dict[str, Any] = {"filed": row.filed}
    known: list[dict[str, Any]] = []
    if row.filed:
        plan_id = f"plan-live-{s}"
        plans.append(
            {
                "plan_id": plan_id,
                "operator": LIVE_OPERATOR,
                "vehicle": "Survey vehicle placed by a judge",
                "modem_id": LIVE_PLAN_MODEM,
                "area": zone.zone_id,
                "window": {"start_tick": 0, "end_tick": DURATION_TICKS},
                "contact": "sim-phone-judge",
            }
        )
        registry.update({"plan_id": plan_id, "modem_id": LIVE_PLAN_MODEM})
    elif row.known_operator:
        modem, operator = first_known_modem(known_ops)
        registry["modem_id"] = modem
        known.append({"operator": operator, "modem_id": modem})
    registry["replies_to_hail"] = row.replies_to_hail
    if req.behavior is JudgeBehavior.SILENT:
        registry["silent_after_tick"] = SILENT_TICK
    data: dict[str, Any] = {
        "id": f"L-{s}",
        "name": f"Judge contact: {req.behavior.value} in {zone.zone_id}",
        "seed": live_seed(req),
        "duration_ticks": DURATION_TICKS,
        "tick_s": TICK_S,
        "ground_truth": row.ground_truth.value,
        "hour_of_day": hour,
    }
    if plans:
        data["registry_plans"] = plans
    if known:
        data["known_operators"] = known
    data["contacts"] = [
        {
            "id": "gt-1",
            "start": {"lat": round6(req.lat), "lon": round6(req.lon), "depth_m": START_DEPTH_M},
            "behavior": behavior,
            "acoustic": _acoustic(profile, row.tonal_band),
            "registry": registry,
        }
    ]
    data["toggles"] = []
    data["expected"] = {}
    data["notes"] = (
        f"Judge-injected live scenario (WO-061): behavior {req.behavior.value}, "
        f"acoustic {profile.value}, zone {zone.zone_id}. Not a catalog file."
    )
    return data


def build_live_scenario(
    req: InjectRequest, stamp: datetime, harbor: Harbor, known_ops: Any
) -> Scenario:
    """The validated ``Scenario`` for one request at ``stamp`` (raises ``OutsideZonesError``)."""
    return scenario_from_data(scenario_data(req, stamp, harbor, known_ops))


def scenario_yaml(scenario: Scenario) -> str:
    """The file body: a header comment and the scenario mapping, byte-stable."""
    data = scenario.model_dump(mode="json", exclude_none=True, exclude_defaults=False)
    data["expected"] = {}
    for c in data["contacts"]:
        for key in ("acoustic", "registry", "behavior", "start"):
            c[key] = {k: v for k, v in c[key].items() if v is not None}
    body = yaml.safe_dump(data, sort_keys=False, allow_unicode=False, default_flow_style=False)
    header = "# Judge-injected live scenario (WO-061). Gitignored; never part of the catalog.\n"
    return header + body


def _restamp(scenario: Scenario, old: str, new: str) -> Scenario:
    raw = scenario.model_dump(mode="json", exclude_none=True)
    text = canonical_json(raw).replace(old, new)
    data = yaml.safe_load(text)  # JSON is YAML
    data["expected"] = {}
    return scenario_from_data(data)


def write_live_scenario(scenario: Scenario, directory: Path | str) -> Path:
    """Write ``L-<stamp>.yaml`` under ``directory`` atomically (temp file + ``os.replace``).

    If the name is taken, the stamp moves forward one second until a free name is found
    (deterministic for a given directory state). The server makes every path."""
    base = Path(directory)
    base.mkdir(parents=True, exist_ok=True)
    stamp = scenario.id.removeprefix("L-")
    current = datetime.strptime(stamp, STAMP_FORMAT)
    sc = scenario
    while (base / f"{sc.id}.yaml").exists():
        current += timedelta(seconds=1)
        new = current.strftime(STAMP_FORMAT)
        sc = _restamp(sc, stamp, new)
        stamp = new
    target = base / f"{sc.id}.yaml"
    fd, tmp = tempfile.mkstemp(prefix=".L-", suffix=".tmp", dir=base)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(scenario_yaml(sc))
        os.replace(tmp, target)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    load_scenario(target, None)  # the same loader the catalog uses
    return target


__all__ = [
    "BEHAVIOR_TABLE",
    "AcousticProfile",
    "BehaviorRow",
    "InjectRequest",
    "JudgeBehavior",
    "OutsideZonesError",
    "build_live_scenario",
    "critical_asset",
    "live_dir",
    "live_seed",
    "load_known_ops",
    "scenario_yaml",
    "stamp_of",
    "write_live_scenario",
]
