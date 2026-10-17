"""Detection model (docs/SPEC-SIM.md section 2): honest about range, noise, gaps, biologics.

Every function here is pure. Functions that draw take an explicit
``numpy.random.Generator``; there is no module-level RNG and no wall-clock read
(docs/WORKFLOW.md section 8). Ranges and positions are local metres (float64);
rounding to 6 dp happens at the ``SimRun`` output boundary, not here, except for
``reading_confidence``, whose 6-dp value is part of its contract.

Model, per tick and per sensor node:

- detection probability ``p = p_max * exp(-(range / r0)^2) * health``, zero beyond
  ``max_range_factor * r0``;
- position noise per axis, Gaussian with ``sigma = 5 m + 0.03 * range``;
- track gaps: each detected tick starts a gap with probability ``gap_rate_per_tick``;
  the gap length is geometric with mean ``gap_mean_ticks``;
- an undetected run longer than ``track_timeout_ticks`` closes the observed track and
  the next detection opens a new observed id;
- biologic false alarms: Poisson arrivals per node per tick at rate
  ``false_alarms_per_node_hour * tick_s / 3600``, each a short shallow track inside the
  node's ``r0`` disk with breathing or clicks with probability ``false_alarm_click_prob``;
- reading confidence ``health * exp(-(range / r0)^2)`` (SPEC-INVESTIGATION section 9.10).
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import numpy.typing as npt
import yaml

# --- WO-089 begin ---
from soundings.physics import (
    ACOUSTIC_CLASSES,
    PhysicsConfig,
    detection_probability_at,
    load_physics_config,
    transmission_loss_db,
)

# --- WO-089 end ---

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SIM_CONFIG_PATH = REPO_ROOT / "config" / "sim.yaml"
SECONDS_PER_HOUR = 3600.0
DECIMALS = 6
# --- WO-089 begin ---
DETECTION_MODELS: tuple[str, ...] = ("sonar_equation", "gaussian")
"""``sonar_equation`` (WO-089, the default) or ``gaussian`` (WO-011, kept for comparison)."""
# --- WO-089 end ---


@dataclass(frozen=True, slots=True)
class DetectionParams:
    """Detection constants. Defaults are the SPEC-SIM section 2 values plus WO-011's picks."""

    p_max: float = 0.95
    r0_m: float = 400.0
    max_range_factor: float = 2.0
    noise_sigma_base_m: float = 5.0
    noise_sigma_per_m: float = 0.03
    gap_rate_per_tick: float = 0.08
    gap_mean_ticks: int = 5
    track_timeout_ticks: int = 15
    false_alarms_per_node_hour: float = 6.0
    false_alarm_click_prob: float = 0.7
    false_alarm_mean_ticks: int = 20
    false_alarm_max_depth_m: float = 3.0
    # --- WO-089 begin: which detection model runs (docs/SPEC-SIM.md section 2a) ---
    model: str = "sonar_equation"
    # --- WO-089 end ---

    def __post_init__(self) -> None:
        # --- WO-089 begin ---
        _check(self.model in DETECTION_MODELS, f"model must be one of {DETECTION_MODELS}")
        # --- WO-089 end ---
        _check(0.0 < self.p_max <= 1.0, "p_max must be in (0, 1]")
        _check(self.r0_m > 0.0, "r0_m must be > 0")
        _check(self.max_range_factor > 0.0, "max_range_factor must be > 0")
        _check(self.noise_sigma_base_m > 0.0, "noise_sigma_base_m must be > 0")
        _check(self.noise_sigma_per_m >= 0.0, "noise_sigma_per_m must be >= 0")
        _check(0.0 <= self.gap_rate_per_tick < 1.0, "gap_rate_per_tick must be in [0, 1)")
        _check(self.gap_mean_ticks >= 1, "gap_mean_ticks must be >= 1")
        _check(self.track_timeout_ticks >= 1, "track_timeout_ticks must be >= 1")
        _check(self.false_alarms_per_node_hour >= 0.0, "false_alarms_per_node_hour must be >= 0")
        _check(0.0 <= self.false_alarm_click_prob <= 1.0, "false_alarm_click_prob in [0, 1]")
        _check(self.false_alarm_mean_ticks >= 1, "false_alarm_mean_ticks must be >= 1")
        _check(self.false_alarm_max_depth_m >= 0.0, "false_alarm_max_depth_m must be >= 0")


def _check(ok: bool, message: str) -> None:
    if not ok:
        raise ValueError(message)


_INT_FIELDS = frozenset({"gap_mean_ticks", "track_timeout_ticks", "false_alarm_mean_ticks"})


def detection_params_from_mapping(raw: Mapping[str, Any]) -> DetectionParams:
    """Build ``DetectionParams`` from a plain mapping; unknown keys and wrong types raise."""
    known = set(DetectionParams.__dataclass_fields__)
    unknown = sorted(set(raw) - known)
    if unknown:
        raise ValueError(f"unknown detection keys: {unknown}")
    values: dict[str, float | int | str] = {}
    for key in sorted(raw):
        value = raw[key]
        # --- WO-089 begin: the one string key ---
        if key == "model":
            if not isinstance(value, str) or value not in DETECTION_MODELS:
                raise ValueError(f"detection.model must be one of {DETECTION_MODELS}")
            values[key] = value
            continue
        # --- WO-089 end ---
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise ValueError(f"detection.{key} must be a number, got {value!r}")
        if key in _INT_FIELDS:
            if not isinstance(value, int):
                raise ValueError(f"detection.{key} must be an integer, got {value!r}")
            values[key] = value
        else:
            values[key] = float(value)
    return DetectionParams(**values)  # type: ignore[arg-type]


def load_sim_yaml(path: Path | str = DEFAULT_SIM_CONFIG_PATH) -> dict[str, Any]:
    """Read ``config/sim.yaml`` as a plain mapping."""
    with Path(path).open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected a mapping at the top level")
    return data


def load_detection_params(path: Path | str = DEFAULT_SIM_CONFIG_PATH) -> DetectionParams:
    """Load the ``detection`` block of ``config/sim.yaml``."""
    block = load_sim_yaml(path).get("detection")
    if not isinstance(block, dict):
        raise ValueError(f"{path}: missing 'detection' mapping")
    return detection_params_from_mapping(block)


# ---------------------------------------------------------------------------
# Closed-form model
# ---------------------------------------------------------------------------


def _falloff(range_m: float, r0_m: float) -> float:
    if r0_m <= 0.0:
        raise ValueError("r0_m must be > 0")
    if range_m < 0.0:
        raise ValueError("range_m must be >= 0")
    return math.exp(-((range_m / r0_m) ** 2))


def _clamp_health(health: float) -> float:
    if not 0.0 <= health <= 1.0:
        raise ValueError("health must be in [0, 1]")
    return health


def max_detection_range_m(r0_m: float, max_range_factor: float = 2.0) -> float:
    """Range beyond which nothing is detected."""
    return max_range_factor * r0_m


def detection_probability(
    range_m: float,
    r0_m: float,
    health: float,
    p_max: float,
    max_range_factor: float = 2.0,
) -> float:
    """Per-tick detection probability ``p_max * exp(-(range/r0)^2) * health``; 0 past the cutoff."""
    health = _clamp_health(health)
    if range_m > max_detection_range_m(r0_m, max_range_factor):
        return 0.0
    return p_max * _falloff(range_m, r0_m) * health


def position_sigma_m(range_m: float, base_m: float = 5.0, per_m: float = 0.03) -> float:
    """Per-axis position-noise standard deviation in metres: ``base + per_m * range``."""
    if range_m < 0.0:
        raise ValueError("range_m must be >= 0")
    return base_m + per_m * range_m


def reading_confidence(range_m: float, r0_m: float, health: float) -> float:
    """Per-reading confidence ``round(health * exp(-(range/r0)^2), 6)`` in [0, 1]."""
    return round(_clamp_health(health) * _falloff(range_m, r0_m), DECIMALS)


def false_alarm_rate_per_tick(per_node_hour: float, tick_s: float) -> float:
    """Poisson rate of false-alarm arrivals per node per tick."""
    if tick_s <= 0.0:
        raise ValueError("tick_s must be > 0")
    return per_node_hour * tick_s / SECONDS_PER_HOUR


# ---------------------------------------------------------------------------
# Draws (explicit rng)
# ---------------------------------------------------------------------------


def sample_detected(
    rng: np.random.Generator,
    range_m: float,
    r0_m: float,
    health: float,
    params: DetectionParams,
) -> bool:
    """One Bernoulli draw at ``detection_probability``. Always consumes exactly one draw."""
    p = detection_probability(range_m, r0_m, health, params.p_max, params.max_range_factor)
    return bool(rng.random() < p)


def sample_position_noise(
    rng: np.random.Generator, range_m: float, params: DetectionParams
) -> tuple[float, float]:
    """Gaussian (dx, dy) in metres with per-axis sigma from ``position_sigma_m``."""
    sigma = position_sigma_m(range_m, params.noise_sigma_base_m, params.noise_sigma_per_m)
    dx, dy = rng.normal(0.0, sigma, size=2)
    return float(dx), float(dy)


def sample_geometric_length(rng: np.random.Generator, mean_ticks: int) -> int:
    """Geometric length on {1, 2, ...} with the given mean."""
    if mean_ticks < 1:
        raise ValueError("mean_ticks must be >= 1")
    return int(rng.geometric(1.0 / mean_ticks))


@dataclass(frozen=True, slots=True)
class GapState:
    """Ticks of the current gap still to run (0 = not in a gap)."""

    remaining: int = 0


def gap_step(
    rng: np.random.Generator,
    state: GapState,
    detected: bool,
    params: DetectionParams,
) -> tuple[bool, GapState]:
    """Advance the gap process by one tick; return (emitted, new state).

    A tick inside a gap emits nothing and counts the gap down. A detected tick outside
    a gap is *eligible*: with probability ``gap_rate_per_tick`` a gap starts on this tick
    (this tick is suppressed and ``L - 1`` more follow, ``L`` geometric with mean
    ``gap_mean_ticks``); otherwise the reading is emitted. Undetected ticks outside a gap
    draw nothing.
    """
    if state.remaining > 0:
        return False, GapState(state.remaining - 1)
    if not detected:
        return False, state
    if rng.random() < params.gap_rate_per_tick:
        length = sample_geometric_length(rng, params.gap_mean_ticks)
        return False, GapState(length - 1)
    return True, state


def split_on_timeout(ticks: Sequence[int], timeout_ticks: int) -> list[list[int]]:
    """Split strictly rising detection ticks into observed-track segments.

    A run of consecutive undetected ticks longer than ``timeout_ticks`` (that is,
    ``next - prev - 1 > timeout_ticks``) ends a segment; the next tick opens a new one.
    """
    segments: list[list[int]] = []
    prev: int | None = None
    for tick in ticks:
        if prev is not None and tick <= prev:
            raise ValueError("ticks must rise strictly")
        if prev is None or tick - prev - 1 > timeout_ticks:
            segments.append([])
        segments[-1].append(tick)
        prev = tick
    return segments


def sample_false_alarm_arrivals(
    rng: np.random.Generator, rate_per_tick: float, size: int | None = None
) -> int | npt.NDArray[np.int64]:
    """Poisson arrival count for one tick, or an array of ``size`` ticks."""
    if size is None:
        return int(rng.poisson(rate_per_tick))
    return rng.poisson(rate_per_tick, size=size).astype(np.int64)


@dataclass(frozen=True, slots=True)
class FalseAlarmSpawn:
    """A biologic false alarm: start position (local m), depth, lifetime, click flag."""

    x_m: float
    y_m: float
    depth_m: float
    lifetime_ticks: int
    click: bool


def sample_false_alarm(
    rng: np.random.Generator,
    node_x_m: float,
    node_y_m: float,
    r0_m: float,
    params: DetectionParams,
) -> FalseAlarmSpawn:
    """Draw one false alarm uniformly in the node's ``r0`` disk. Fixed draw order."""
    radius = r0_m * math.sqrt(float(rng.random()))
    angle = 2.0 * math.pi * float(rng.random())
    depth = params.false_alarm_max_depth_m * float(rng.random())
    lifetime = sample_geometric_length(rng, params.false_alarm_mean_ticks)
    click = bool(rng.random() < params.false_alarm_click_prob)
    return FalseAlarmSpawn(
        x_m=node_x_m + radius * math.cos(angle),
        y_m=node_y_m + radius * math.sin(angle),
        depth_m=depth,
        lifetime_ticks=lifetime,
        click=click,
    )


def fuse_nearest(candidates: Sequence[tuple[str, float]]) -> tuple[str, float] | None:
    """Pick the reading to keep from (node_id, range_m) pairs of nodes that detected.

    The node with the smallest range supplies the reading; ties go to the lowest node_id.
    Returns None when no node detected.
    """
    if not candidates:
        return None
    return min(candidates, key=lambda c: (c[1], c[0]))


# --- WO-089 begin: passive-sonar-equation detection (docs/SPEC-SIM.md section 2a) ---
#
# p_tick = p_max * health * logistic((SL - TL(r) - NL - DT) / k), zero beyond
# max_range_factor x the effective range at calm NL; reading confidence is
# round6(health * logistic term). The laws and every number live in soundings.physics and
# config/physics.yaml; this block only binds them to the per-tick draw.


def acoustic_class(propeller_tonal: bool, biologic_signature: bool) -> str:
    """The source-level class of a contact: tonal present, tonal absent, or biologic."""
    if propeller_tonal:
        return "propeller_tonal_present"
    if biologic_signature:
        return "biologic"
    return "propeller_tonal_absent"


@dataclass(frozen=True, slots=True)
class SonarSetup:
    """What every sonar draw of one run shares: the config, calm NL, SL and cut-off by class."""

    cfg: PhysicsConfig
    base_nl_db: float
    sl_db: tuple[tuple[str, float], ...]
    cutoff_m: tuple[tuple[str, float], ...]

    def sl(self, acoustic: str) -> float:
        return dict(self.sl_db)[acoustic]

    def cutoff(self, acoustic: str) -> float:
        return dict(self.cutoff_m)[acoustic]


def sonar_setup(cfg: PhysicsConfig | None = None) -> SonarSetup:
    """Build the run-wide sonar constants from ``config/physics.yaml`` (or ``cfg``)."""
    conf = cfg if cfg is not None else load_physics_config()
    levels = tuple((c, conf.sonar.source_level_db.for_class(c)) for c in ACOUSTIC_CLASSES)
    return SonarSetup(
        cfg=conf,
        base_nl_db=conf.base_noise_db(),
        sl_db=levels,
        cutoff_m=tuple((c, conf.cutoff_range_m(sl)) for c, sl in levels),
    )


def node_gain_db(node_range_m: float, r0_m: float, setup: SonarSetup) -> float:
    """A node's sensitivity offset: ``TL(node range_m) - TL(r0)``, so a node whose harbor
    ``range_m`` differs from the nominal ``r0_m`` keeps that range as its calm-noise
    effective range (0 exactly for every 400 m node on main)."""
    if node_range_m == r0_m:
        return 0.0
    alpha = setup.cfg.sonar.alpha_db_per_km.value
    return transmission_loss_db(node_range_m, alpha) - transmission_loss_db(r0_m, alpha)


def sonar_term(
    range_m: float, acoustic: str, nl_db: float, setup: SonarSetup, gain_db: float = 0.0
) -> float:
    """The logistic detection term at ``range_m`` for a node with ``gain_db`` sensitivity
    offset, 0 beyond the cut-off (``max_range_factor`` x its calm-noise effective range)."""
    if range_m < 0.0:
        raise ValueError("range_m must be >= 0")
    sl = setup.sl(acoustic) + gain_db
    cutoff = setup.cutoff(acoustic) if gain_db == 0.0 else setup.cfg.cutoff_range_m(sl)
    if range_m > cutoff:
        return 0.0
    return detection_probability_at(range_m, sl, nl_db, setup.cfg)


def sonar_detection_probability(
    range_m: float,
    acoustic: str,
    nl_db: float,
    health: float,
    p_max: float,
    setup: SonarSetup,
    gain_db: float = 0.0,
) -> float:
    """Per-tick probability ``p_max * health * logistic term`` (0 past the cut-off)."""
    health = _clamp_health(health)
    return p_max * health * sonar_term(range_m, acoustic, nl_db, setup, gain_db)


def sonar_reading_confidence(term: float, health: float) -> float:
    """Per-reading confidence ``round(health * logistic term, 6)`` in [0, 1]."""
    return round(_clamp_health(health) * term, DECIMALS)


def sample_detected_sonar(
    rng: np.random.Generator,
    range_m: float,
    acoustic: str,
    nl_db: float,
    health: float,
    params: DetectionParams,
    setup: SonarSetup,
    gain_db: float = 0.0,
) -> bool:
    """One Bernoulli draw at ``sonar_detection_probability``; always exactly one draw, so the
    rng streams line up with the Gaussian model's."""
    p = sonar_detection_probability(range_m, acoustic, nl_db, health, params.p_max, setup, gain_db)
    return bool(rng.random() < p)


# --- WO-089 end ---
