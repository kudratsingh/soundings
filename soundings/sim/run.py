"""The tick loop: ground truth -> per-node detection -> gaps, timeout, id issue -> false
alarms -> acoustics -> 6-dp output (docs/SPEC-SIM.md sections 2 and 4).

Determinism (docs/WORKFLOW.md section 8):

1. One ``SeedSequence(seed)`` per run, split with ``spawn`` in a fixed order: ground-truth
   contacts (sorted by id; each splits into kinematics and detection streams), then
   sensor nodes (sorted by id; false-alarm arrivals and spawns), then false alarms (in
   ``fa-NNNN`` order; their motion and noise), then acoustics (per contact, sorted by
   id), then toggles (``contact_surge``). Every function that draws takes its ``rng``.
2. No wall clock: time is ``tick * tick_s``.
3. Every id-keyed collection is sorted before iteration where output depends on it.
4. Positions integrate in local metres (float64); lat/lon/depth/speeds/confidences are
   rounded to 6 dp only when the ``SimRun`` is built.

Observed ids are issued in order of first detection (ties by ground-truth id): ``c-0001``,
``c-0002``, ... An undetected run longer than ``track_timeout_ticks`` closes an observed
track and the next detection opens a new id; ``contact_truth`` maps both ids to the same
``gt_id``. Missing ticks inside an observed track are written to ``Track.gaps``.

Geometry choice: a lawnmower surveys its zone's bounding box clipped to the square of
half-side ``range_m`` around the sensor node nearest its start (widened to include the
start). A survey vehicle working where the harbor can hear it keeps S01/S02 tracks
continuous enough to be useful; the gaps and re-issued ids still happen at the corners.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from functools import lru_cache

import numpy as np

from soundings.canonical import round6
from soundings.physics import ambient_noise_db  # WO-089
from soundings.physics_view import AisFix, vessel_fixes_at  # WO-089
from soundings.schemas._base import LAT_MAX, LAT_MIN, LON_MAX, LON_MIN
from soundings.schemas.config import LikelihoodSpec, Priors, load_likelihood_spec, load_priors
from soundings.schemas.enums import BehaviorType, Hypothesis, ToggleName, TonalBand
from soundings.schemas.evidence import ContactId
from soundings.schemas.geo import from_local_m, to_local_m
from soundings.schemas.harbor import Harbor, SensorNode, load_harbor
from soundings.schemas.scenario import Scenario, ScenarioContact
from soundings.schemas.track import AcousticReading, Track, TrackGap, TrackPoint
from soundings.sim.acoustics import (
    AcousticDefaults,
    AcousticTruth,
    acoustic_reading,
    apply_silence,
    load_acoustic_defaults,
    load_tonal_band_weights,
    sample_acoustic_truth,
)
from soundings.sim.ais import AisConfig, ais_reports, load_ais_config
from soundings.sim.behaviors import (
    BehaviorParams,
    Drift,
    Erratic,
    KinState,
    Lawnmower,
    LawnmowerThenDrift,
    Orbit,
    Stationary,
    Transit,
    VesselTransit,
    heading_of,
    make_lawnmower,
    simulate_path,
)
from soundings.sim.current import CurrentField, load_current_field
from soundings.sim.detection import (
    DetectionParams,
    GapState,
    SonarSetup,  # WO-089
    acoustic_class,  # WO-089
    false_alarm_rate_per_tick,
    fuse_nearest,
    gap_step,
    load_detection_params,
    node_gain_db,  # WO-089
    position_sigma_m,
    reading_confidence,
    sample_detected,
    sample_detected_sonar,  # WO-089
    sample_false_alarm,
    sample_position_noise,
    sonar_reading_confidence,  # WO-089
    sonar_setup,  # WO-089
    sonar_term,  # WO-089
    split_on_timeout,
)
from soundings.sim.toggles import (
    dropout_ticks,
    node_health,
    quiet_ticks,
    surge_contacts,
    toggle_report,
)
from soundings.sim.types import AisReport, ContactTruth, Detection, SimRun, TruthPoint

DEFAULT_SPEED_MPS = 1.0
DEFAULT_LANE_SPACING_M = 50.0
ERRATIC_LEASH_M = 300.0  # an animal wanders but stays near where it was first seen
ERRATIC_MIN_DEPTH_MAX_M = 6.0
FALSE_ALARM_SPEED_MPS = 0.4
FALSE_ALARM_DRIFT_WEIGHT = 0.5


@dataclass(frozen=True, slots=True)
class SimConfig:
    """Config files a run reads (loaded once per process for the default paths)."""

    params: DetectionParams
    current: CurrentField
    likelihoods: LikelihoodSpec
    acoustic_defaults: dict[str, AcousticDefaults]
    band_weights: dict[str, float]
    ais: AisConfig = AisConfig()


@lru_cache(maxsize=1)
def default_config() -> SimConfig:
    return SimConfig(
        params=load_detection_params(),
        current=load_current_field(),
        likelihoods=load_likelihood_spec(),
        acoustic_defaults=load_acoustic_defaults(),
        band_weights=load_tonal_band_weights(),
        ais=load_ais_config(),
    )


# --- WO-089 begin ---
@lru_cache(maxsize=1)
def _default_sonar() -> SonarSetup:
    return sonar_setup()


# --- WO-089 end ---


@lru_cache(maxsize=1)
def _default_harbor() -> Harbor:
    return load_harbor()


@lru_cache(maxsize=1)
def _default_priors() -> Priors:
    return load_priors()


# ---------------------------------------------------------------------------
# Ground-truth contacts
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Truth:
    gt_id: str
    hypothesis: Hypothesis
    behavior: BehaviorParams
    start: KinState
    first_tick: int
    acoustic: AcousticTruth
    replies_to_hail: bool
    silent_after_tick: int | None
    is_false_alarm: bool = False
    node_id: str | None = None  # false alarms: the node that reports them


def _nearest_node(harbor: Harbor, x: float, y: float) -> SensorNode:
    def key(n: SensorNode) -> tuple[float, str]:
        nx, ny = to_local_m(n.lat, n.lon)
        return (round6(math.hypot(nx - x, ny - y)), n.node_id)

    return min(harbor.sensor_nodes, key=key)


def _survey(
    harbor: Harbor, area: str, x0: float, y0: float, speed: float, lane: float
) -> Lawnmower:
    pts = [to_local_m(lat, lon) for lat, lon in harbor.zone(area).polygon]
    zx0, zx1 = min(p[0] for p in pts), max(p[0] for p in pts)
    zy0, zy1 = min(p[1] for p in pts), max(p[1] for p in pts)
    node = _nearest_node(harbor, x0, y0)
    nx, ny = to_local_m(node.lat, node.lon)
    r = node.range_m
    x_lo, x_hi = max(zx0, min(nx - r, x0)), min(zx1, max(nx + r, x0))
    y_lo, y_hi = max(zy0, min(ny - r, y0)), min(zy1, max(ny + r, y0))
    if x_hi - x_lo < lane or y_hi - y_lo < lane:
        x_lo, x_hi, y_lo, y_hi = zx0, zx1, zy0, zy1
    return make_lawnmower(x0, y0, x_lo, x_hi, y_lo, y_hi, speed_mps=speed, lane_spacing_m=lane)


def behavior_params(contact: ScenarioContact, scenario: Scenario, harbor: Harbor) -> BehaviorParams:
    """Translate a scenario ``BehaviorSpec`` into local-metre behavior parameters."""
    b = contact.behavior
    x0, y0 = to_local_m(contact.start.lat, contact.start.lon)
    speed = b.speed_mps if b.speed_mps is not None else DEFAULT_SPEED_MPS
    jitter = b.jitter_mps if b.jitter_mps is not None else 0.0
    if b.type is BehaviorType.TRANSIT:
        if b.target_asset is not None:
            asset = harbor.asset(b.target_asset)
            tx, ty = to_local_m(asset.lat, asset.lon)
        else:
            assert b.waypoint is not None  # BehaviorSpec guarantees one of the two
            tx, ty = to_local_m(b.waypoint.lat, b.waypoint.lon)
        return Transit(target_x_m=tx, target_y_m=ty, speed_mps=speed)
    if b.type in (BehaviorType.LAWNMOWER, BehaviorType.LAWNMOWER_THEN_DRIFT):
        area = b.area
        if area is None and contact.registry.plan_id is not None:
            area = scenario.plan(contact.registry.plan_id).area
        if area is None:
            raise ValueError(f"contact {contact.id}: lawnmower needs an area")
        lane = b.lane_spacing_m if b.lane_spacing_m is not None else DEFAULT_LANE_SPACING_M
        mower = _survey(harbor, area, x0, y0, speed, lane)
        if b.type is BehaviorType.LAWNMOWER:
            return mower
        assert b.fault_tick is not None
        return LawnmowerThenDrift(lawnmower=mower, fault_tick=b.fault_tick, drift=Drift(jitter))
    if b.type is BehaviorType.ORBIT:
        assert b.target_asset is not None and b.radius_m is not None
        asset = harbor.asset(b.target_asset)
        cx, cy = to_local_m(asset.lat, asset.lon)
        return Orbit(center_x_m=cx, center_y_m=cy, radius_m=b.radius_m, speed_mps=speed)
    if b.type is BehaviorType.DRIFT:
        return Drift(jitter_mps=jitter)
    if b.type is BehaviorType.ERRATIC:
        return Erratic(
            speed_mps=speed,
            drift_weight=b.drift_weight if b.drift_weight is not None else 0.0,
            depth_max_m=max(ERRATIC_MIN_DEPTH_MAX_M, contact.start.depth_m),
            home_x_m=x0,
            home_y_m=y0,
            leash_m=ERRATIC_LEASH_M,
        )
    if b.type is BehaviorType.STATIONARY:
        return Stationary(anchor_x_m=x0, anchor_y_m=y0, jitter_mps=jitter)
    if b.type is BehaviorType.VESSEL_TRANSIT:  # WO-080
        assert b.waypoint is not None  # BehaviorSpec guarantees it
        return VesselTransit(
            waypoints=((x0, y0), to_local_m(b.waypoint.lat, b.waypoint.lon)), speed_mps=speed
        )
    raise ValueError(f"unknown behavior {b.type}")


def _start_state(contact: ScenarioContact) -> KinState:
    x0, y0 = to_local_m(contact.start.lat, contact.start.lon)
    b = contact.behavior
    if b.type is BehaviorType.VESSEL_TRANSIT and b.waypoint is not None:  # WO-080: under way
        wx, wy = to_local_m(b.waypoint.lat, b.waypoint.lon)
        return KinState(
            x_m=x0,
            y_m=y0,
            depth_m=0.0,
            heading_rad=heading_of(wx - x0, wy - y0, 0.0),
            speed_mps=b.speed_mps if b.speed_mps is not None else 0.0,
        )
    heading = contact.behavior.heading_deg
    return KinState(
        x_m=x0,
        y_m=y0,
        depth_m=contact.start.depth_m,
        heading_rad=math.radians(heading) if heading is not None else 0.0,
    )


# ---------------------------------------------------------------------------
# Readings
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Reading:
    tick: int
    node_id: str
    range_m: float
    health: float
    r0_m: float
    lat: float
    lon: float
    depth_m: float
    acoustic: AcousticTruth
    # --- WO-089 begin: the sonar logistic term at this reading (None under gaussian) ---
    term: float | None = None
    # --- WO-089 end ---


def _in_box(lat: float, lon: float) -> bool:
    return LAT_MIN <= lat <= LAT_MAX and LON_MIN <= lon <= LON_MAX


def _reading(
    tick: int,
    node: SensorNode,
    health: float,
    x: float,
    y: float,
    depth: float,
    range_m: float,
    acoustic: AcousticTruth,
    term: float | None = None,  # WO-089
) -> _Reading | None:
    lat, lon = from_local_m(x, y)
    if not _in_box(lat, lon):
        return None  # off the map: the harbor's sensors do not report it
    return _Reading(
        tick, node.node_id, range_m, health, node.range_m, lat, lon, depth, acoustic, term
    )


# --- WO-089 begin: per-node, per-tick ambient noise for the sonar model ---


class NodeNoise:
    """NL at each node and tick: calm NL (base + sea state) plus the vessel transits in
    ``ais_tracks`` (WO-080 part A's ``SimRun.ais_tracks``; empty until it is on main),
    weighted by distance and summed in the power domain. Pure; cached per tick."""

    def __init__(
        self,
        nodes: Sequence[SensorNode],
        setup: SonarSetup,
        tick_s: float,
        ais_tracks: Mapping[str, Sequence[AisFix]] | None = None,
        r0_m: float = 400.0,
    ) -> None:
        self.nodes = list(nodes)
        self.setup = setup
        self.gains = tuple(node_gain_db(n.range_m, r0_m, setup) for n in self.nodes)
        self.tick_s = float(tick_s)
        self._tracks = {
            k: sorted(v, key=lambda r: r.tick) for k, v in sorted((ais_tracks or {}).items())
        }
        self._cache: dict[int, tuple[float, ...]] = {}

    def levels(self, tick: int) -> tuple[float, ...]:
        """NL (dB) at every node, in node order, at ``tick``."""
        hit = self._cache.get(tick)
        if hit is not None:
            return hit
        fixes = vessel_fixes_at(self._tracks, tick, self.tick_s) if self._tracks else []
        if not fixes:
            out = tuple(self.setup.base_nl_db for _ in self.nodes)
        else:
            cfg = self.setup.cfg
            base = cfg.sonar.ambient_noise_base_db.value
            out = tuple(
                ambient_noise_db(base, cfg.sonar.sea_state, fixes, n.lat, n.lon, cfg)
                for n in self.nodes
            )
        self._cache[tick] = out
        return out


def _class_of(acoustic: AcousticTruth) -> str:
    return acoustic_class(acoustic.propeller_tonal, acoustic.biologic_signature)


# --- WO-089 end ---


def _detect_contact(
    truth: _Truth,
    path: np.ndarray,
    nodes: Sequence[SensorNode],
    node_xy: Sequence[tuple[float, float]],
    dropouts: dict[str, int],
    params: DetectionParams,
    rng: np.random.Generator,
    noise: NodeNoise | None = None,  # WO-089: set when detection.model is sonar_equation
) -> list[_Reading]:
    """Per tick: one detection draw per node (sorted), fuse nearest, gap process, noise."""
    out: list[_Reading] = []
    gap = GapState()
    # --- WO-089 begin ---
    cls = _class_of(truth.acoustic)
    nl_at: tuple[float, ...] = ()
    # --- WO-089 end ---
    for k in range(path.shape[0]):
        tick = truth.first_tick + k
        x, y, depth = float(path[k, 0]), float(path[k, 1]), float(path[k, 2])
        candidates: list[tuple[str, float]] = []
        by_id: dict[str, tuple[SensorNode, float]] = {}
        # --- WO-089 begin ---
        nl_by_id: dict[str, float] = {}
        gain_by_id: dict[str, float] = {}
        if noise is not None:
            nl_at = noise.levels(tick)
        # --- WO-089 end ---
        for i, (node, (nx, ny)) in enumerate(zip(nodes, node_xy, strict=True)):
            rng_m = math.hypot(x - nx, y - ny)
            health = node_health(node.health, node.node_id, tick, dropouts)
            # --- WO-089 begin: one draw per node either way, so the rng streams align ---
            if noise is not None:
                hit = sample_detected_sonar(
                    rng, rng_m, cls, nl_at[i], health, params, noise.setup, noise.gains[i]
                )
                nl_by_id[node.node_id] = nl_at[i]
                gain_by_id[node.node_id] = noise.gains[i]
            else:
                hit = sample_detected(rng, rng_m, node.range_m, health, params)
            # --- WO-089 end ---
            if hit:
                candidates.append((node.node_id, rng_m))
                by_id[node.node_id] = (node, health)
        fused = fuse_nearest(candidates)
        emitted, gap = gap_step(rng, gap, fused is not None, params)
        if not emitted or fused is None:
            continue
        node, health = by_id[fused[0]]
        dx, dy = sample_position_noise(rng, fused[1], params)
        acoustic, _ = apply_silence(
            tick, truth.silent_after_tick, truth.acoustic, truth.replies_to_hail
        )
        # --- WO-089 begin ---
        term = None
        if noise is not None:
            term = sonar_term(fused[1], cls, nl_by_id[fused[0]], noise.setup, gain_by_id[fused[0]])
        r = _reading(tick, node, health, x + dx, y + dy, depth, fused[1], acoustic, term)
        # --- WO-089 end ---
        if r is not None:
            out.append(r)
    return out


def _emit_false_alarm(
    truth: _Truth,
    path: np.ndarray,
    node: SensorNode,
    dropouts: dict[str, int],
    params: DetectionParams,
    rng: np.random.Generator,
    noise: NodeNoise | None = None,  # WO-089
    node_index: int = 0,  # WO-089
) -> list[_Reading]:
    """A false alarm is reported by its node every tick of its life while the node works."""
    nx, ny = to_local_m(node.lat, node.lon)
    out: list[_Reading] = []
    for k in range(path.shape[0]):
        tick = truth.first_tick + k
        x, y, depth = float(path[k, 0]), float(path[k, 1]), float(path[k, 2])
        rng_m = math.hypot(x - nx, y - ny)
        dx, dy = sample_position_noise(rng, rng_m, params)
        health = node_health(node.health, node.node_id, tick, dropouts)
        if health <= 0.0:
            continue
        # --- WO-089 begin ---
        term = None
        if noise is not None:
            nl = noise.levels(tick)[node_index]
            gain = noise.gains[node_index]
            term = sonar_term(rng_m, _class_of(truth.acoustic), nl, noise.setup, gain)
        r = _reading(tick, node, health, x + dx, y + dy, depth, rng_m, truth.acoustic, term)
        # --- WO-089 end ---
        if r is not None:
            out.append(r)
    return out


# ---------------------------------------------------------------------------
# run_scenario
# ---------------------------------------------------------------------------


def _truth_points(path: np.ndarray, first_tick: int) -> list[TruthPoint]:
    points: list[TruthPoint] = []
    for k in range(path.shape[0]):
        lat, lon = from_local_m(float(path[k, 0]), float(path[k, 1]))
        points.append(
            TruthPoint(
                tick=first_tick + k,
                lat=round6(lat),
                lon=round6(lon),
                depth_m=round6(max(float(path[k, 2]), 0.0)),
                speed_mps=round6(float(path[k, 4])),
                heading_deg=round6(math.degrees(float(path[k, 3])) % 360.0),
            )
        )
    return points


def _track(cid: str, tick_s: float, readings: Sequence[_Reading], cfg: SimConfig) -> Track:
    points: list[TrackPoint] = []
    gaps: list[TrackGap] = []
    readings_cache: dict[AcousticTruth, AcousticReading] = {}
    for prev, r in zip([None, *readings[:-1]], readings, strict=True):
        if prev is not None and r.tick - prev.tick > 1:
            gaps.append(TrackGap(start_tick=prev.tick + 1, end_tick=r.tick - 1))
        if r.acoustic not in readings_cache:
            readings_cache[r.acoustic] = acoustic_reading(r.acoustic, cfg.acoustic_defaults)
        points.append(
            TrackPoint(
                tick=r.tick,
                lat=round6(r.lat),
                lon=round6(r.lon),
                depth_m=round6(max(r.depth_m, 0.0)),
                sigma_m=round6(
                    position_sigma_m(
                        r.range_m, cfg.params.noise_sigma_base_m, cfg.params.noise_sigma_per_m
                    )
                ),
                sensor_id=r.node_id,
                # --- WO-089 begin ---
                confidence=(
                    reading_confidence(r.range_m, r.r0_m, r.health)
                    if r.term is None
                    else sonar_reading_confidence(r.term, r.health)
                ),
                # --- WO-089 end ---
                acoustic=readings_cache[r.acoustic],
            )
        )
    return Track(contact_id=cid, tick_s=tick_s, points=tuple(points), gaps=tuple(gaps))


def _detection(cid: str, truth: _Truth, tick_s: float, r: _Reading, p: TrackPoint) -> Detection:
    a = p.acoustic
    assert isinstance(a.propeller_tonal_present, bool)
    assert isinstance(a.tonal_band, TonalBand)
    assert isinstance(a.cavitation_index, float)
    assert isinstance(a.breathing_or_click_present, bool)
    assert isinstance(a.broadband_level, float)
    return Detection(
        observed_contact_id=cid,
        gt_id=truth.gt_id,
        tick=r.tick,
        t_s=round6(r.tick * tick_s),
        node_id=r.node_id,
        lat=p.lat,
        lon=p.lon,
        depth_m=p.depth_m,
        range_m=round6(r.range_m),
        sensor_health=round6(r.health),
        confidence=p.confidence,
        propeller_tonal_present=a.propeller_tonal_present,
        tonal_band=a.tonal_band,
        cavitation_index=a.cavitation_index,
        breathing_or_click_present=a.breathing_or_click_present,
        broadband_level=a.broadband_level,
        is_false_alarm=truth.is_false_alarm,
    )


def run_scenario(
    scenario: Scenario,
    *,
    seed: int | None = None,
    ticks: int | None = None,
    harbor: Harbor | None = None,
    params: DetectionParams | None = None,
    likelihoods: LikelihoodSpec | None = None,
    ais_tracks: Mapping[str, Sequence[AisFix]] | None = None,  # WO-089
) -> SimRun:
    """Run one scenario. ``seed=None`` uses ``scenario.seed``; ``ticks=None`` uses
    ``scenario.duration_ticks``; ``harbor=None`` loads ``config/harbor.geojson``;
    ``params=None`` loads ``config/sim.yaml``; ``likelihoods=None`` samples unset acoustic
    fields from ``config/likelihoods.yaml`` (WO-051 passes a perturbed table instead).
    ``ais_tracks`` (WO-089) are the vessel transits whose noise raises each node's NL under
    ``detection.model: sonar_equation``; None means none (WO-080 part A passes its own).
    Pure: same arguments, same ``SimRun``.

    Prefix-stable: with ``ticks=N`` greater than ``duration_ticks`` the first
    ``duration_ticks`` ticks are byte-identical to the default run (every stream is per
    contact, per node or per false alarm and drawn in tick order); compare with
    ``truncate_run``.
    """
    cfg = default_config()
    if params is not None:
        cfg = replace(cfg, params=params)
    if likelihoods is not None:
        cfg = replace(cfg, likelihoods=likelihoods)
    p = cfg.params
    h = harbor if harbor is not None else _default_harbor()
    run_seed = scenario.seed if seed is None else seed
    n_ticks = scenario.duration_ticks if ticks is None else ticks
    if n_ticks < 0:
        raise ValueError("ticks must be >= 0")
    tick_s = scenario.tick_s
    sampler = cfg.current.sampler(cfg.current.phase_for_hour(scenario.hour_of_day))
    nodes = sorted(h.sensor_nodes, key=lambda n: n.node_id)
    node_xy = [to_local_m(n.lat, n.lon) for n in nodes]
    node_ids = [n.node_id for n in nodes]
    dropouts = dropout_ticks(scenario.toggles, node_ids)
    quiet = quiet_ticks(scenario.toggles)
    # --- WO-089 begin ---
    noise = (
        NodeNoise(nodes, _default_sonar(), tick_s, ais_tracks, p.r0_m)
        if p.model == "sonar_equation"
        else None
    )
    # --- WO-089 end ---

    ss_contacts, ss_nodes, ss_fa, ss_acoustics, ss_toggles = np.random.SeedSequence(run_seed).spawn(
        5
    )

    # Ground-truth contacts: the scenario's plus any contact_surge, sorted by id.
    surge = []
    if any(t.name is ToggleName.CONTACT_SURGE for t in scenario.toggles):
        surge = surge_contacts(
            scenario.toggles, h, _default_priors(), np.random.default_rng(ss_toggles)
        )
    # WO-080: a scenario contact enters at ``start_tick``, leaves at ``end_tick`` and may
    # carry its own hypothesis; surge contacts keep ``at_tick``.
    entries: list[tuple[ScenarioContact, int, int, Hypothesis]] = [
        (
            c,
            c.start_tick,
            min(c.end_tick if c.end_tick is not None else n_ticks, n_ticks),
            c.hypothesis or scenario.ground_truth,
        )
        for c in scenario.contacts
    ] + [(s.contact, s.at_tick, n_ticks, s.hypothesis) for s in surge]
    entries.sort(key=lambda e: e[0].id)
    contact_streams = ss_contacts.spawn(len(entries))
    acoustic_streams = ss_acoustics.spawn(len(entries))

    truths: list[_Truth] = []
    paths: dict[str, np.ndarray] = {}
    readings: dict[str, list[_Reading]] = {}
    vessel_ids: list[str] = []
    for (contact, first_tick, end_tick, hyp), css, ass in zip(
        entries, contact_streams, acoustic_streams, strict=True
    ):
        acoustic = sample_acoustic_truth(
            np.random.default_rng(ass), hyp, contact.acoustic, cfg.likelihoods, cfg.band_weights
        )
        silent = contact.registry.silent_after_tick
        if contact.id in quiet:
            silent = quiet[contact.id] if silent is None else min(silent, quiet[contact.id])
        truth = _Truth(
            gt_id=contact.id,
            hypothesis=hyp,
            behavior=behavior_params(contact, scenario, h),
            start=_start_state(contact),
            first_tick=first_tick,
            acoustic=acoustic,
            replies_to_hail=contact.registry.replies_to_hail,
            silent_after_tick=silent,
        )
        kin_ss, det_ss = css.spawn(2)
        path = simulate_path(
            truth.behavior,
            truth.start,
            max(end_tick - first_tick, 0),
            dt_s=tick_s,
            current=sampler,
            rng=np.random.default_rng(kin_ss),
        )
        truths.append(truth)
        paths[truth.gt_id] = path
        if contact.behavior.type is BehaviorType.VESSEL_TRANSIT:
            vessel_ids.append(truth.gt_id)  # surface: never detected, AIS feed only
            continue
        readings[truth.gt_id] = _detect_contact(
            truth, path, nodes, node_xy, dropouts, p, np.random.default_rng(det_ss), noise
        )

    # Biologic false alarms: Poisson arrivals per node per tick, nodes sorted by id.
    rate = false_alarm_rate_per_tick(p.false_alarms_per_node_hour, tick_s)
    # Arrivals and spawn draws use separate streams, drawn tick by tick, so a longer run
    # reproduces a shorter one's prefix exactly (prefix stability).
    spawns = []
    for node, (nx, ny), nss in zip(nodes, node_xy, ss_nodes.spawn(len(nodes)), strict=True):
        arrival_ss, spawn_ss = nss.spawn(2)
        arrival_rng = np.random.default_rng(arrival_ss)
        spawn_rng = np.random.default_rng(spawn_ss)
        for tick in range(n_ticks):
            for _ in range(int(arrival_rng.poisson(rate))):
                fa = sample_false_alarm(spawn_rng, nx, ny, node.range_m, p)
                if node_health(node.health, node.node_id, tick, dropouts) > 0.0:
                    spawns.append((tick, node.node_id, fa))
    spawns.sort(key=lambda s: (s[0], s[1]))  # stable: arrival order within a node and tick
    node_by_id = {n.node_id: n for n in nodes}
    node_index = {n.node_id: i for i, n in enumerate(nodes)}  # WO-089
    for i, ((tick, node_id, fa), fss) in enumerate(
        zip(spawns, ss_fa.spawn(len(spawns)), strict=True), start=1
    ):
        node = node_by_id[node_id]
        nx, ny = to_local_m(node.lat, node.lon)
        truth = _Truth(
            gt_id=f"fa-{i:04d}",
            hypothesis=Hypothesis.BIOLOGIC,
            behavior=Erratic(
                speed_mps=FALSE_ALARM_SPEED_MPS,
                drift_weight=FALSE_ALARM_DRIFT_WEIGHT,
                depth_max_m=p.false_alarm_max_depth_m,
                home_x_m=nx,
                home_y_m=ny,
                leash_m=node.range_m,
            ),
            start=KinState(x_m=fa.x_m, y_m=fa.y_m, depth_m=fa.depth_m),
            first_tick=tick,
            acoustic=AcousticTruth(
                propeller_tonal=False, tonal_band="none", biologic_signature=fa.click
            ),
            replies_to_hail=False,
            silent_after_tick=None,
            is_false_alarm=True,
            node_id=node_id,
        )
        fa_kin_ss, fa_noise_ss = fss.spawn(2)
        life = min(fa.lifetime_ticks, n_ticks - tick)
        path = simulate_path(
            truth.behavior,
            truth.start,
            life,
            dt_s=tick_s,
            current=sampler,
            rng=np.random.default_rng(fa_kin_ss),
        )
        truths.append(truth)
        paths[truth.gt_id] = path
        readings[truth.gt_id] = _emit_false_alarm(
            truth,
            path,
            node,
            dropouts,
            p,
            np.random.default_rng(fa_noise_ss),
            noise,  # WO-089
            node_index[node_id],
        )

    # Observed ids: split each contact's readings on the timeout, then number segments
    # in order of first detection, ties by ground-truth id.
    segments: list[tuple[int, str, list[_Reading]]] = []
    by_gt = {t.gt_id: t for t in truths}
    for gt_id in sorted(readings):
        rs = readings[gt_id]
        by_tick = {r.tick: r for r in rs}
        for seg in split_on_timeout([r.tick for r in rs], p.track_timeout_ticks):
            segments.append((seg[0], gt_id, [by_tick[t] for t in seg]))
    segments.sort(key=lambda s: (s[0], s[1]))

    tracks: dict[ContactId, Track] = {}
    contact_truth: dict[str, ContactTruth] = {}
    detections: list[Detection] = []
    for n, (_, gt_id, seg_readings) in enumerate(segments, start=1):
        cid = f"c-{n:04d}"
        truth = by_gt[gt_id]
        track = _track(cid, tick_s, seg_readings, cfg)
        tracks[cid] = track
        contact_truth[cid] = ContactTruth(
            gt_id=gt_id,
            hypothesis=truth.hypothesis,
            replies_to_hail=truth.replies_to_hail,
            silent_after_tick=truth.silent_after_tick,
        )
        for r, point in zip(seg_readings, track.points, strict=True):
            detections.append(_detection(cid, truth, tick_s, r, point))
    detections.sort(key=lambda d: (d.tick, d.observed_contact_id))

    truth_paths = {
        gt_id: _truth_points(paths[gt_id], by_gt[gt_id].first_tick) for gt_id in sorted(paths)
    }
    ais_tracks: dict[str, tuple[AisReport, ...]] = {
        vid: ais_reports(vid, truth_paths[vid], tick_s, cfg.ais) for vid in sorted(vessel_ids)
    }
    return SimRun(
        scenario_id=scenario.id,
        seed=run_seed,
        tick_s=tick_s,
        n_ticks=n_ticks,
        tracks=tracks,
        detections=detections,
        contact_truth=contact_truth,
        truth_paths=truth_paths,
        toggle_report=toggle_report(scenario.toggles, [s.contact.id for s in surge]),
        ais_tracks={k: v for k, v in ais_tracks.items() if v},
    )


def truncate_run(run: SimRun, n_ticks: int) -> SimRun:
    """The part of ``run`` before tick ``n_ticks``, as a run of that length would report it.

    Points, detections and truth points at ``tick >= n_ticks`` are dropped, gaps after an
    observed track's last kept point are dropped, and ids left without points disappear.
    """
    if n_ticks > run.n_ticks:
        raise ValueError("cannot truncate to more ticks than the run has")
    tracks: dict[ContactId, Track] = {}
    for cid in sorted(run.tracks):
        track = run.tracks[cid]
        points = tuple(pt for pt in track.points if pt.tick < n_ticks)
        if not points:
            continue
        gaps = tuple(g for g in track.gaps if g.end_tick < points[-1].tick)
        tracks[cid] = track.model_copy(update={"points": points, "gaps": gaps})
    truth_paths: dict[str, list[TruthPoint]] = {}
    for gt_id in sorted(run.truth_paths):
        kept = [pt for pt in run.truth_paths[gt_id] if pt.tick < n_ticks]
        if kept:
            truth_paths[gt_id] = kept
    return replace(
        run,
        n_ticks=n_ticks,
        tracks=tracks,
        detections=[d for d in run.detections if d.tick < n_ticks],
        contact_truth={c: t for c, t in run.contact_truth.items() if c in tracks},
        truth_paths=truth_paths,
        ais_tracks={
            k: reports
            for k in sorted(run.ais_tracks)
            if (reports := tuple(r for r in run.ais_tracks[k] if r.tick < n_ticks))
        },
    )


def format_run(run: SimRun, track_hash_hex: str) -> str:
    """One line per tick (observed readings only, sorted by observed id), then the hash.

    ``<tick>\\t<t_s>\\t<n>`` then, per reading, ``\\t<id>@<node> <lat> <lon> <depth> <conf>``.
    """
    at_tick: dict[int, list[tuple[str, TrackPoint]]] = {}
    for cid in sorted(run.tracks):
        for point in run.tracks[cid].points:
            at_tick.setdefault(point.tick, []).append((cid, point))
    lines: list[str] = []
    for tick in range(run.n_ticks):
        rows = sorted(at_tick.get(tick, []), key=lambda r: r[0])
        parts = [f"{tick}", f"{tick * run.tick_s:.6f}", f"{len(rows)}"]
        for cid, pt in rows:
            parts.append(
                f"{cid}@{pt.sensor_id} {pt.lat:.6f} {pt.lon:.6f} {pt.depth_m:.6f} "
                f"{pt.confidence:.6f}"
            )
        lines.append("\t".join(parts))
    lines.append(f"track_hash={track_hash_hex}")
    return "\n".join(lines) + "\n"
