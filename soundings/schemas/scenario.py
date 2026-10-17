"""The scenario file (SPEC-SIM section 4): ground truth for the sim, ``expected`` for tests.

Ground-truth types live here only; investigation types never import this module except
for pattern-of-life generation. Every key is declared: an unknown key is an error, so a
twist that adds a key adds a field here.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any

import yaml
from pydantic import Field, StrictBool, StringConstraints, field_validator, model_validator

from soundings.schemas._base import (
    REPO_ROOT,
    HarborId,
    Lat,
    Lon,
    NonNegFloat,
    NonNegInt,
    PositiveFloat,
    Probability,
    RefId,
    StrictModel,
    from_file_data,
)
from soundings.schemas.denylist import FreeText
from soundings.schemas.enums import (
    BehaviorType,
    Hypothesis,
    JobType,
    LadderState,
    ToggleName,
    TonalBand,
)
from soundings.schemas.harbor import Harbor, load_harbor

DEFAULT_SCENARIO_ROOT: Path = REPO_ROOT / "scenarios"

ScenarioId = Annotated[
    str, StringConstraints(pattern=r"^(S\d{2}|BT-\d{2}|TR-\d{2}|smoke|L-\d{8}T\d{6})$")
]
TruthContactId = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_-]*$")]
"""Ground-truth contact ids, for example ``gt-1`` (observed ids are ``c-NNNN``)."""


class PlanWindow(StrictModel):
    start_tick: NonNegInt
    end_tick: NonNegInt
    # --- WO-095: filing-window flexibility --- (optional; the registry's start bounds)
    earliest_start_tick: NonNegInt | None = None
    latest_start_tick: NonNegInt | None = None
    # --- WO-095 end ---

    @model_validator(mode="after")
    def _ordered(self) -> PlanWindow:
        if self.start_tick > self.end_tick:
            raise ValueError("plan window start_tick must be <= end_tick")
        if self.earliest_start_tick is not None and self.earliest_start_tick > self.start_tick:
            raise ValueError("plan window earliest_start_tick must be <= start_tick")
        if self.latest_start_tick is not None and self.latest_start_tick < self.start_tick:
            raise ValueError("plan window latest_start_tick must be >= start_tick")
        return self


class RegistryPlan(StrictModel):
    plan_id: RefId
    operator: FreeText
    vehicle: FreeText
    modem_id: RefId
    area: HarborId
    window: PlanWindow
    contact: FreeText
    job_type: JobType | None = None  # WO-080: set on every generated BT plan


class KnownOperator(StrictModel):
    """An operator whose modem is known to the harbor although no plan is filed (S04)."""

    operator: FreeText
    modem_id: RefId


class StartPoint(StrictModel):
    lat: Lat
    lon: Lon
    depth_m: NonNegFloat


class Waypoint(StrictModel):
    lat: Lat
    lon: Lon


VESSEL_SPEED_MIN_MPS = 3.0
VESSEL_SPEED_MAX_MPS = 6.0
"""WO-080: a surface vessel transit runs at 3-6 m/s."""


class BehaviorSpec(StrictModel):
    type: BehaviorType
    target_asset: HarborId | None = None
    waypoint: Waypoint | None = None
    area: HarborId | None = None
    speed_mps: PositiveFloat | None = None
    radius_m: PositiveFloat | None = None
    lane_spacing_m: PositiveFloat | None = None
    heading_deg: Annotated[float, Field(ge=0.0, lt=360.0)] | None = None
    fault_tick: NonNegInt | None = None
    jitter_mps: NonNegFloat | None = None
    drift_weight: Probability | None = None

    @model_validator(mode="after")
    def _per_type(self) -> BehaviorSpec:
        t = self.type
        if t is BehaviorType.TRANSIT:
            if (self.target_asset is None) == (self.waypoint is None):
                raise ValueError("transit needs exactly one of target_asset or waypoint")
            if self.speed_mps is None:
                raise ValueError("transit needs speed_mps")
        elif t is BehaviorType.ORBIT:
            if self.target_asset is None or self.radius_m is None:
                raise ValueError("orbit needs target_asset and radius_m")
        elif t is BehaviorType.LAWNMOWER:
            if self.area is None:
                raise ValueError("lawnmower needs area")
        elif t is BehaviorType.LAWNMOWER_THEN_DRIFT:
            if self.fault_tick is None:
                raise ValueError("lawnmower_then_drift needs fault_tick")
        elif t is BehaviorType.VESSEL_TRANSIT:
            if self.waypoint is None:
                raise ValueError("vessel_transit needs waypoint (the exit point)")
            if self.speed_mps is None or not (
                VESSEL_SPEED_MIN_MPS <= self.speed_mps <= VESSEL_SPEED_MAX_MPS
            ):
                raise ValueError(
                    f"vessel_transit speed_mps must be in [{VESSEL_SPEED_MIN_MPS}, "
                    f"{VESSEL_SPEED_MAX_MPS}]"
                )
            others = (
                self.target_asset,
                self.area,
                self.radius_m,
                self.lane_spacing_m,
                self.heading_deg,
                self.fault_tick,
                self.jitter_mps,
                self.drift_weight,
            )
            if any(v is not None for v in others):
                raise ValueError("vessel_transit takes only waypoint and speed_mps")
        return self


class AcousticSpec(StrictModel):
    """Each field left None is sampled by the sim from the hypothesis row of likelihoods."""

    propeller_tonal: StrictBool | None = None
    tonal_band: TonalBand | None = None
    biologic_signature: StrictBool | None = None


class ContactRegistrySpec(StrictModel):
    filed: StrictBool
    plan_id: RefId | None = None
    modem_id: RefId | None = None
    replies_to_hail: StrictBool
    silent_after_tick: NonNegInt | None = None

    @model_validator(mode="after")
    def _plan_means_filed(self) -> ContactRegistrySpec:
        if self.plan_id is not None and not self.filed:
            raise ValueError("a contact with plan_id must be filed")
        return self


class ScenarioContact(StrictModel):
    id: TruthContactId
    start: StartPoint
    behavior: BehaviorSpec
    acoustic: AcousticSpec = Field(default_factory=AcousticSpec)
    registry: ContactRegistrySpec
    # --- WO-080: contacts that enter/leave the water mid-run; per-contact truth ---
    start_tick: NonNegInt = 0
    end_tick: NonNegInt | None = None
    hypothesis: Hypothesis | None = None

    @model_validator(mode="after")
    def _wo080_checks(self) -> ScenarioContact:
        if self.end_tick is not None and self.end_tick <= self.start_tick:
            raise ValueError(f"contact {self.id}: end_tick must be > start_tick")
        if self.behavior.type is BehaviorType.VESSEL_TRANSIT and self.start.depth_m != 0.0:
            raise ValueError(f"contact {self.id}: vessel_transit starts at depth 0")
        return self


class Toggle(StrictModel):
    """A twist toggle. A bare string in YAML (``- network_down``) means ``{name: ...}``."""

    name: ToggleName
    n: Annotated[int, Field(ge=1)] | None = None
    node_id: HarborId | None = None
    contact_id: TruthContactId | None = None
    at_tick: NonNegInt | None = None

    @model_validator(mode="before")
    @classmethod
    def _bare_name(cls, data: Any) -> Any:
        if isinstance(data, str):
            return {"name": data}
        return data


class ExpectedContact(StrictModel):
    """WO-080: the expected end of one ground-truth contact's lineage."""

    id: TruthContactId
    terminal_state: LadderState
    max_interruptions: NonNegInt | None = None


class Expected(StrictModel):
    # --- WO-061 begin: optional only for judge-injected live scenarios (^L-) ---
    terminal_state: LadderState | None = None

    @field_validator("terminal_state", mode="before")
    @classmethod
    def _no_explicit_null(cls, value: Any) -> Any:
        if value is None:
            raise ValueError("terminal_state may be omitted (L- ids only), never null")
        return value

    # --- WO-061 end ---
    passes_through: tuple[LadderState, ...] = ()
    top_hypothesis: Hypothesis | None = None
    min_threat_mass: Probability | None = None
    max_human_interruptions: NonNegInt | None = None
    max_probes: NonNegInt | None = None
    sitrep_has_search_box: StrictBool | None = None
    search_box_contains_truth: StrictBool | None = None
    contacts: tuple[ExpectedContact, ...] = ()  # WO-080


class Scenario(StrictModel):
    id: ScenarioId
    name: FreeText
    seed: NonNegInt
    duration_ticks: Annotated[int, Field(gt=0)]
    tick_s: PositiveFloat
    ground_truth: Hypothesis
    hour_of_day: Annotated[int, Field(ge=0, le=23)]
    registry_plans: tuple[RegistryPlan, ...] = ()
    known_operators: tuple[KnownOperator, ...] = ()
    contacts: Annotated[tuple[ScenarioContact, ...], Field(min_length=1)]
    toggles: tuple[Toggle, ...] = ()
    expected: Expected
    notes: FreeText = ""

    @model_validator(mode="after")
    def _cross_field(self) -> Scenario:
        contact_ids = [c.id for c in self.contacts]
        if len(set(contact_ids)) != len(contact_ids):
            raise ValueError("contact ids must be unique")
        # --- WO-061 begin: an empty expected block only for judge-injected live ids ---
        if self.expected.terminal_state is None and not self.id.startswith("L-"):
            raise ValueError("expected.terminal_state is required (only L- ids may omit it)")
        # --- WO-061 end ---
        plan_ids = [p.plan_id for p in self.registry_plans]
        if len(set(plan_ids)) != len(plan_ids):
            raise ValueError("plan ids must be unique")
        for plan in self.registry_plans:
            if plan.window.end_tick > self.duration_ticks:
                raise ValueError(f"plan {plan.plan_id} window ends after duration_ticks")
            w = plan.window  # WO-095: a plan moved to its latest start still ends in the run
            if w.latest_start_tick is not None and (
                w.latest_start_tick + w.end_tick - w.start_tick > self.duration_ticks
            ):
                raise ValueError(f"plan {plan.plan_id} latest start ends after duration_ticks")
        for c in self.contacts:
            reg = c.registry
            if reg.plan_id is not None and reg.plan_id not in plan_ids:
                raise ValueError(f"contact {c.id}: plan_id {reg.plan_id} not in registry_plans")
            if reg.silent_after_tick is not None and reg.silent_after_tick >= self.duration_ticks:
                raise ValueError(f"contact {c.id}: silent_after_tick must be < duration_ticks")
            b = c.behavior
            if b.fault_tick is not None and b.fault_tick >= self.duration_ticks:
                raise ValueError(f"contact {c.id}: fault_tick must be < duration_ticks")
            if b.type is BehaviorType.LAWNMOWER_THEN_DRIFT and b.area is None and not reg.plan_id:
                raise ValueError(f"contact {c.id}: lawnmower_then_drift needs area or a plan")
            if c.start_tick >= self.duration_ticks:
                raise ValueError(f"contact {c.id}: start_tick must be < duration_ticks")
            if c.end_tick is not None and c.end_tick > self.duration_ticks:
                raise ValueError(f"contact {c.id}: end_tick must be <= duration_ticks")
        for ec in self.expected.contacts:
            if ec.id not in contact_ids:
                raise ValueError(f"expected.contacts: unknown contact id {ec.id}")
        for toggle in self.toggles:
            if toggle.contact_id is not None and toggle.contact_id not in contact_ids:
                raise ValueError(f"toggle {toggle.name}: unknown contact_id {toggle.contact_id}")
            if toggle.at_tick is not None and toggle.at_tick >= self.duration_ticks:
                raise ValueError(f"toggle {toggle.name}: at_tick must be < duration_ticks")
        return self

    def plan(self, plan_id: str) -> RegistryPlan:
        for p in self.registry_plans:
            if p.plan_id == plan_id:
                return p
        raise KeyError(plan_id)


def check_scenario_refs(scenario: Scenario, harbor: Harbor) -> None:
    """Raise ``ValueError`` if any zone, asset or node id is not in ``harbor``."""
    zone_ids = {z.zone_id for z in harbor.zones}
    asset_ids = {a.asset_id for a in harbor.assets}
    node_ids = {n.node_id for n in harbor.sensor_nodes}
    missing: list[str] = []
    for plan in scenario.registry_plans:
        if plan.area not in zone_ids:
            missing.append(f"plan {plan.plan_id} area {plan.area}")
    for c in scenario.contacts:
        b = c.behavior
        if b.target_asset is not None and b.target_asset not in asset_ids:
            missing.append(f"contact {c.id} target_asset {b.target_asset}")
        if b.area is not None and b.area not in zone_ids:
            missing.append(f"contact {c.id} area {b.area}")
    for t in scenario.toggles:
        if t.node_id is not None and t.node_id not in node_ids:
            missing.append(f"toggle {t.name} node_id {t.node_id}")
    if missing:
        raise ValueError(f"scenario {scenario.id}: unresolved harbor ids: {missing}")


def scenario_from_data(data: Any) -> Scenario:
    """Validate a parsed YAML mapping as a ``Scenario`` (lists become tuples)."""
    return Scenario.model_validate(from_file_data(data))


def load_scenario(path: str | Path, harbor: Harbor | None = None) -> Scenario:
    """Load one scenario YAML and resolve its ids against ``harbor`` (default harbor if None)."""
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    scenario = scenario_from_data(data)
    check_scenario_refs(scenario, harbor if harbor is not None else load_harbor())
    return scenario


def scenario_paths(root: str | Path = DEFAULT_SCENARIO_ROOT) -> list[Path]:
    """``root/*.yaml`` and ``root/live/*.yaml``, sorted by path."""
    base = Path(root)
    return sorted([*base.glob("*.yaml"), *(base / "live").glob("*.yaml")])


def load_scenarios(
    root: str | Path = DEFAULT_SCENARIO_ROOT, harbor: Harbor | None = None
) -> list[Scenario]:
    """Every scenario under ``root`` (and ``root/live``), sorted by path."""
    h = harbor if harbor is not None else load_harbor()
    return [load_scenario(p, h) for p in scenario_paths(root)]
