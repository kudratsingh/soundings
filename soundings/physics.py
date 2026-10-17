"""Physics laws for the simulated sensors (WO-089; docs/SPEC-SIM.md section 2a).

Detection is simulated for this demonstration. This module states, in one place, the laws
the simulator uses, and ``config/physics.yaml`` holds every number they take as
``{value, unit, source, label: "demo value"}``. Nothing here is real acoustics or optics;
the cards (``model_cards``) say so and list what is not modeled.

Laws:

- Passive-sonar signal excess ``SE = SL - TL - NL - DT`` with ``TL = 20 log10 r + alpha r``
  (Urick); the detection term is ``logistic(SE / k)``. The simulator multiplies it by
  ``p_max`` and node health (``soundings.sim.detection``).
- Ambient noise ``NL``: the base level, the sea-state level and each vessel transit summed
  in the power domain; a vessel's level falls ``falloff_db_per_doubling`` per doubling of
  distance from ``reference_distance_m``.
- Effective range: the range where the detection term is 0.5, in closed form with the
  Lambert W function (fixed-count Halley iteration, no scipy).
- Hail travel time ``2r / c``; camera attenuation ``exp(-k depth) exp(-turbidity)``;
  responder battery drain ``drain_coeff v^3 dt``.

Pure and deterministic (CLAUDE.md 2.4): no clock, no RNG, no I/O except the config loader,
sorted iteration, 6-dp rounding at the public boundary (``battery_drain_min`` returns the
exact product so its cubic law holds exactly).
"""

from __future__ import annotations

import math
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, field_validator

from soundings.canonical import round6
from soundings.schemas._base import CONFIG_DIR
from soundings.schemas.geo import to_local_m

PHYSICS_CONFIG_PATH: Path = CONFIG_DIR / "physics.yaml"
DEMO_LABEL: Literal["demo value"] = "demo value"
ACOUSTIC_CLASSES: tuple[str, ...] = (
    "biologic",
    "propeller_tonal_absent",
    "propeller_tonal_present",
)
NOT_MODELED: tuple[str, ...] = (
    "hydrodynamics",
    "beam patterns",
    "multipath",
    "bottom type",
    "frequency-dependent spectra",
    "real signal processing",
)
"""What no card models (the owner's list, 2026-10-17 07:50, plus WO-089's Out list)."""

_HALLEY_STEPS = 24
_MIN_RANGE_M = 1.0  # TL is referred to 1 m; closer ranges use TL(1 m)


# --- config ------------------------------------------------------------------------------


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class Param(_Strict):
    """One labeled number: value, unit, where it comes from, and the demo label."""

    value: float
    unit: str
    source: str
    label: Literal["demo value"]

    @field_validator("source", "unit")
    @classmethod
    def _non_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("must be a non-empty string")
        return v

    @field_validator("value", mode="before")
    @classmethod
    def _number(cls, v: Any) -> Any:
        if isinstance(v, bool) or not isinstance(v, int | float):
            raise ValueError("value must be a number")
        return float(v)


class SourceLevels(_Strict):
    propeller_tonal_present: Param
    propeller_tonal_absent: Param
    biologic: Param

    def for_class(self, acoustic_class: str) -> float:
        if acoustic_class not in ACOUSTIC_CLASSES:
            raise ValueError(f"unknown acoustic class {acoustic_class!r}")
        param: Param = getattr(self, acoustic_class)
        return param.value


class VesselNoise(_Strict):
    source_level_db: Param
    falloff_db_per_doubling: Param
    reference_distance_m: Param


class SonarConfig(_Strict):
    source_level_db: SourceLevels
    alpha_db_per_km: Param
    ambient_noise_base_db: Param
    sea_state_default: Param
    sea_state_table: dict[int, Param]
    detection_threshold_db: Param
    logistic_k_db: Param
    max_range_factor: Param
    vessel_noise: VesselNoise

    @field_validator("sea_state_table")
    @classmethod
    def _table(cls, v: dict[int, Param]) -> dict[int, Param]:
        if not v:
            raise ValueError("sea_state_table must not be empty")
        return dict(sorted(v.items()))

    @property
    def sea_state(self) -> int:
        """The sea state every run uses (scenarios carry none)."""
        return int(self.sea_state_default.value)


class HailPhysics(_Strict):
    sound_speed_mps: Param
    modem_turnaround_s: Param


class CameraPhysics(_Strict):
    k_depth_per_m: Param
    turbidity_default: Param


class ResponderPhysics(_Strict):
    drain_coeff: Param


class PhysicsConfig(_Strict):
    """``config/physics.yaml``, strict: unknown keys raise, every parameter is sourced."""

    sonar: SonarConfig
    hail: HailPhysics
    camera: CameraPhysics
    responder: ResponderPhysics

    def params(self) -> list[tuple[str, Param]]:
        """Every parameter with its dotted config path, sorted by path."""
        return sorted(_walk(self, ""), key=lambda kv: kv[0])

    def base_noise_db(self) -> float:
        """Calm-water NL: the base level and the default sea state, no vessels."""
        return ambient_noise_db(
            self.sonar.ambient_noise_base_db.value, self.sonar.sea_state, (), 0.0, 0.0, self
        )

    def cutoff_range_m(self, sl_db: float) -> float:
        """Nothing is detected beyond ``max_range_factor`` x the calm-noise effective range."""
        return round6(
            self.sonar.max_range_factor.value * effective_range_m(sl_db, self.base_noise_db(), self)
        )


def _walk(node: object, prefix: str) -> Iterator[tuple[str, Param]]:
    if isinstance(node, Param):
        yield prefix, node
    elif isinstance(node, BaseModel):
        for name in type(node).model_fields:
            yield from _walk(getattr(node, name), f"{prefix}.{name}" if prefix else name)
    elif isinstance(node, Mapping):
        for key in sorted(node):
            yield from _walk(node[key], f"{prefix}.{key}")


@lru_cache(maxsize=4)
def _load(path: str) -> PhysicsConfig:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected a mapping at the top level")
    return PhysicsConfig.model_validate(data)


def load_physics_config(path: Path | str | None = None) -> PhysicsConfig:
    """Load ``config/physics.yaml`` (cached per path; the result is frozen)."""
    return _load(str(path if path is not None else PHYSICS_CONFIG_PATH))


# --- sonar -------------------------------------------------------------------------------


def logistic(x: float) -> float:
    """``1 / (1 + e^-x)``, computed without overflow for large ``|x|``."""
    if x >= 0.0:
        return 1.0 / (1.0 + math.exp(-x))
    e = math.exp(x)
    return e / (1.0 + e)


def transmission_loss_db(range_m: float, alpha_db_per_km: float) -> float:
    """``TL = 20 log10 r + alpha r`` (r in m, alpha in dB/km); ranges under 1 m use 1 m."""
    if range_m < 0.0:
        raise ValueError("range_m must be >= 0")
    if alpha_db_per_km < 0.0:
        raise ValueError("alpha_db_per_km must be >= 0")
    r = max(range_m, _MIN_RANGE_M)
    return 20.0 * math.log10(r) + alpha_db_per_km * r / 1000.0


def _power_sum_db(levels: Sequence[float]) -> float:
    return 10.0 * math.log10(math.fsum(10.0 ** (lv / 10.0) for lv in sorted(levels)))


def vessel_noise_db(distance_m: float, cfg: PhysicsConfig) -> float:
    """One vessel's level at a node: ``SL_v - falloff log2(max(d, d_ref) / d_ref)``."""
    vn = cfg.sonar.vessel_noise
    d_ref = vn.reference_distance_m.value
    d = max(distance_m, d_ref)
    return vn.source_level_db.value - vn.falloff_db_per_doubling.value * math.log2(d / d_ref)


def ambient_noise_db(
    base_db: float,
    sea_state: int,
    vessel_fixes: Sequence[tuple[float, float, float]],
    node_lat: float,
    node_lon: float,
    cfg: PhysicsConfig,
) -> float:
    """NL at a node: base, sea state and each vessel ``(lat, lon, speed_mps)`` summed in the
    power domain, each vessel weighted by its distance to the node. 6 dp."""
    table = cfg.sonar.sea_state_table
    if sea_state not in table:
        raise ValueError(f"sea state {sea_state} is not in sonar.sea_state_table")
    levels = [float(base_db), table[sea_state].value]
    if vessel_fixes:
        nx, ny = to_local_m(node_lat, node_lon)
        for lat, lon, _speed in sorted(vessel_fixes):
            vx, vy = to_local_m(lat, lon)
            levels.append(vessel_noise_db(math.hypot(vx - nx, vy - ny), cfg))
    return round6(_power_sum_db(levels))


def detection_probability(
    sl_db: float, tl_db: float, nl_db: float, dt_db: float, k_db: float
) -> float:
    """``logistic((SL - TL - NL - DT) / k)``: the passive-sonar detection term."""
    if k_db <= 0.0:
        raise ValueError("k_db must be > 0")
    return logistic((sl_db - tl_db - nl_db - dt_db) / k_db)


def detection_probability_at(
    range_m: float, sl_db: float, nl_db: float, cfg: PhysicsConfig
) -> float:
    """The detection term at ``range_m`` with the configured alpha, DT and k."""
    s = cfg.sonar
    tl = transmission_loss_db(range_m, s.alpha_db_per_km.value)
    return detection_probability(
        sl_db, tl, nl_db, s.detection_threshold_db.value, s.logistic_k_db.value
    )


def _lambert_w(x: float) -> float:
    """Principal branch of Lambert W for ``x >= 0``, by a fixed number of Halley steps."""
    if x < 0.0:
        raise ValueError("x must be >= 0")
    if x == 0.0:
        return 0.0
    w = math.log1p(x) if x < math.e else math.log(x) - math.log(math.log(x))
    for _ in range(_HALLEY_STEPS):
        ew = math.exp(w)
        f = w * ew - x
        if f == 0.0:
            break
        denom = ew * (w + 1.0) - (w + 2.0) * f / (2.0 * w + 2.0)
        w -= f / denom
    return w


def effective_range_m(sl_db: float, nl_db: float, cfg: PhysicsConfig) -> float:
    """The range where the detection term is 0.5: ``20 log10 r + a r = E``, ``E = SL-NL-DT``.

    Closed form ``r = (c/a) W((a/c) e^(E/c))`` with ``c = 20 / ln 10`` and ``a`` in dB/m;
    ``r = 10^(E/20)`` when alpha is 0. Returns 0 when even 1 m falls short. 6 dp.
    """
    s = cfg.sonar
    excess = sl_db - nl_db - s.detection_threshold_db.value
    a = s.alpha_db_per_km.value / 1000.0
    if excess <= transmission_loss_db(_MIN_RANGE_M, s.alpha_db_per_km.value):
        return 0.0
    if a == 0.0:
        return round6(10.0 ** (excess / 20.0))
    c = 20.0 / math.log(10.0)
    log_arg = math.log(a / c) + excess / c
    if log_arg > 700.0:  # W(x) ~ log x - log log x; e^700 would overflow
        w = log_arg - math.log(log_arg)
        for _ in range(_HALLEY_STEPS):
            w -= (w + math.log(w) - log_arg) / (1.0 + 1.0 / w)
    else:
        w = _lambert_w(math.exp(log_arg))
    return round6((c / a) * w)


# --- the other laws ----------------------------------------------------------------------


def hail_latency_s(range_m: float, cfg: PhysicsConfig) -> float:
    """Two-way acoustic travel time ``2r / c`` in seconds (the modem turnaround is separate)."""
    if range_m < 0.0:
        raise ValueError("range_m must be >= 0")
    return round6(2.0 * range_m / cfg.hail.sound_speed_mps.value)


def camera_visibility(depth_m: float, turbidity: float, cfg: PhysicsConfig) -> float:
    """Camera attenuation ``exp(-k depth) exp(-turbidity)`` in [0, 1]; 1 at the surface in
    clear water. Negative depth or turbidity counts as 0."""
    k = cfg.camera.k_depth_per_m.value
    return round6(math.exp(-k * max(depth_m, 0.0)) * math.exp(-max(turbidity, 0.0)))


def battery_drain_min(speed_mps: float, dt_s: float, cfg: PhysicsConfig) -> float:
    """Battery minutes used moving at ``speed_mps`` for ``dt_s``: ``drain_coeff v^3 dt``."""
    if speed_mps < 0.0 or dt_s < 0.0:
        raise ValueError("speed_mps and dt_s must be >= 0")
    return cfg.responder.drain_coeff.value * speed_mps**3 * dt_s


# --- cards -------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CardParameter:
    """One parameter on a card, as ``config/physics.yaml`` states it."""

    key: str
    value: float
    unit: str
    source: str
    label: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "value": round6(self.value),
            "unit": self.unit,
            "source": self.source,
            "label": self.label,
        }


@dataclass(frozen=True, slots=True)
class PhysicsCard:
    """One model: its equation, its parameters with sources, and what it does not model."""

    name: str
    equation: str
    parameters: tuple[CardParameter, ...]
    not_modeled: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "equation": self.equation,
            "parameters": [p.as_dict() for p in self.parameters],
            "not_modeled": list(self.not_modeled),
        }


_CARDS: tuple[tuple[str, str, tuple[str, ...], tuple[str, ...]], ...] = (
    (
        "Passive sonar detection",
        "p = p_max * health * logistic((SL - TL - NL - DT) / k), TL = 20 log10(r) + alpha r;"
        " none beyond max_range_factor x the calm-noise effective range",
        (
            "sonar.source_level_db",
            "sonar.alpha_db_per_km",
            "sonar.detection_threshold_db",
            "sonar.logistic_k_db",
            "sonar.max_range_factor",
        ),
        ("array gain", "sensor clock drift"),
    ),
    (
        "Ambient noise",
        "NL = 10 log10(10^(NL_base/10) + 10^(NL_sea/10) + sum_i 10^(L_i/10)),"
        " L_i = SL_v - falloff x log2(max(d_i, d_ref) / d_ref)",
        (
            "sonar.ambient_noise_base_db",
            "sonar.sea_state_default",
            "sonar.sea_state_table",
            "sonar.vessel_noise",
        ),
        ("rain and biologic choruses", "vessel speed or size dependence"),
    ),
    (
        "Hail latency",
        "t_reply = t_turnaround + 2r / c",
        ("hail.sound_speed_mps", "hail.modem_turnaround_s"),
        ("modem protocol", "reply authentication"),
    ),
    (
        "Camera visibility",
        "V = V_hour * exp(-k * depth) * exp(-turbidity); fog gives 0",
        ("camera.k_depth_per_m", "camera.turbidity_default"),
        ("glare and surface waves", "artificial lighting"),
    ),
    (
        "Responder battery drain",
        "drain_min = drain_coeff * v^3 * dt",
        ("responder.drain_coeff",),
        ("current set and drift on the hull", "battery temperature"),
    ),
)


def model_cards(cfg: PhysicsConfig | None = None) -> list[PhysicsCard]:
    """One card per law, in a fixed order; every parameter is read from the config."""
    conf = cfg if cfg is not None else load_physics_config()
    params = conf.params()
    cards: list[PhysicsCard] = []
    for name, equation, prefixes, extra in _CARDS:
        rows = tuple(
            CardParameter(key, p.value, p.unit, p.source, p.label)
            for key, p in params
            if any(key == pre or key.startswith(f"{pre}.") for pre in prefixes)
        )
        cards.append(PhysicsCard(name, equation, rows, (*NOT_MODELED, *extra)))
    return cards


__all__ = [
    "ACOUSTIC_CLASSES",
    "DEMO_LABEL",
    "NOT_MODELED",
    "PHYSICS_CONFIG_PATH",
    "CardParameter",
    "Param",
    "PhysicsCard",
    "PhysicsConfig",
    "ambient_noise_db",
    "battery_drain_min",
    "camera_visibility",
    "detection_probability",
    "detection_probability_at",
    "effective_range_m",
    "hail_latency_s",
    "load_physics_config",
    "logistic",
    "model_cards",
    "transmission_loss_db",
    "vessel_noise_db",
]
