"""The WebSocket event schema (WO-060, ``SCHEMA_VERSION = 1``).

Every event is an envelope ``{v, run_id, seq, tick, t_s, type, payload}``. ``seq`` starts at
0 and rises by one; ``tick`` and ``t_s`` are sim time only (CLAUDE.md 2.4: no wall time is
ever streamed). Models forbid unknown fields so the backend and ``web/src/events.ts`` cannot
drift silently; ``export_json_schema()`` is committed as
``web/src/generated/stream-events.schema.json`` and a test keeps the two equal.

Part A (runner v1) emits ``run_started``, ``tick``, ``evidence_row``, ``transition``,
``rung``, ``sitrep`` (the LOST_VEHICLE notice) and ``run_finished``. The ``posterior``,
``approval_request`` and ``approval_result`` models are defined here so the contract is
whole; Part B and WO-061 emit them.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

# --- WO-086 begin ---
from soundings.eval.policies import LedgerBucket
from soundings.investigation.kalman import ProjectionView

# --- WO-086 end ---
from soundings.runner import LostVehicleNotice
from soundings.schemas.enums import Hypothesis, LadderState, Rung, ToggleName
from soundings.schemas.evidence import EvidenceRow

SCHEMA_VERSION: Literal[1] = 1
POSTERIOR_LABEL: Literal["stress-test estimate"] = "stress-test estimate"


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


# --- payloads -----------------------------------------------------------------------------


class RunStartedPayload(_Model):
    scenario_id: str
    scenario_name: str
    seed: int
    tick_s: float
    n_ticks: int
    speed: float
    toggles: list[ToggleName] = Field(default_factory=list)
    source: Literal["batch", "stepped"]
    runner: str
    """Which runner produced the stream (``v1`` in Part A, ``v3`` for the WO-060B source)."""
    # --- WO-060B begin: run_started extras ---
    model_path: Literal["off", "replay", "live"] = "off"
    """Which model client the run was given: ``off`` (deterministic planner and template
    SITREP), ``replay`` (fixtures with fallbacks) or ``live`` (stage live mode, WO-087)."""
    orders: Literal["signed", "unsigned"] = "signed"
    # --- WO-060B end ---


class TickContact(_Model):
    contact_id: str
    lat: float
    lon: float
    depth_m: float
    confidence: float
    sensor_id: str
    # --- WO-086 begin ---
    projection: ProjectionView | None = None
    """WO-084's 60 s projection from the observed track up to this reading (None below two
    readings). A stress-test estimate: it assumes the contact holds its velocity."""
    # --- WO-086 end ---


class TickPayload(_Model):
    contacts: list[TickContact]
    """Observed readings at this tick only; ground truth is never streamed."""


class EvidenceRowPayload(_Model):
    row_id: str
    row: EvidenceRow


class PosteriorPayload(_Model):
    contact_id: str
    probs: dict[Hypothesis, float]
    entropy: float
    top2_margin: float
    threat_mass: float
    benign_mass: float
    label: Literal["stress-test estimate"] = POSTERIOR_LABEL


class GuardInfo(_Model):
    name: str
    result: str
    inputs: dict[str, Any] = Field(default_factory=dict)


class TransitionPayload(_Model):
    contact_id: str
    case_id: str
    from_state: LadderState
    event: str
    to_state: LadderState
    reason_code: str
    evidence_row_ids: list[str]
    guard: GuardInfo | None = None
    audit_row_hash: str | None = None


class RungPayload(_Model):
    contact_id: str
    rung: Rung
    state: LadderState


class ApprovalRequestPayload(_Model):
    contact_id: str
    request_id: str
    action: str
    action_label: str
    scope: dict[str, Any] = Field(default_factory=dict)
    recommended_rung: Rung
    expires_t_s: float
    approver: str


class ApprovalResultPayload(_Model):
    request_id: str
    # --- WO-060B begin: denied (a person's no through the approval UI, runner v3) ---
    outcome: Literal["granted", "denied", "timeout", "backup_paged"]
    # --- WO-060B end ---
    token_hash: str | None = None


class SitrepPayload(_Model):
    """A SITREP. Part A streams the runner v1 LOST_VEHICLE notice as ``kind=lost_vehicle``;
    the WO-042 ``Sitrep`` arrives in ``sitrep`` with ``kind=sitrep`` (Part B)."""

    contact_id: str
    version: int
    writer: Literal["template", "llm"]
    kind: Literal["lost_vehicle", "sitrep"]
    notice: LostVehicleNotice | None = None
    sitrep: dict[str, Any] | None = None
    # --- WO-060B begin: runner v3 SITREP packets ---
    status: Literal["draft", "final", "update"] | None = None
    """WO-042 ``SitrepStatus``: draft at AWAITING_APPROVAL, final at HANDED_OFF, update while
    watching after the handoff. ``None`` for the LOST_VEHICLE notice."""
    markdown: str | None = None
    path: str | None = None
    """WO-042 ``SitrepPath`` (template, fixture-replay, fallback-after-rejection, live)."""
    # --- WO-060B end ---


class RunFinishedPayload(_Model):
    terminal: dict[str, LadderState]
    track_hash: str
    transition_log_hash: str
    n_events: int


class ErrorPayload(_Model):
    message: str


# --- WO-060B begin: investigation, recommendation and ladder-action payloads ---
class InvestigationStepPayload(_Model):
    """One planner choice of an investigation (runner v2 inside runner v3).

    ``source`` is where the choice came from: ``deterministic`` (no model), ``llm`` (a
    model reply that passed the guard) or ``fallback`` (the model path was refused or
    missed, so the deterministic choice stands; ``fallback_reason`` says why). The event is
    sent at ``landed_tick``, after the rows the probe produced (``row_ids``)."""

    contact_id: str
    case_id: str
    probe_id: str | None
    source: Literal["deterministic", "llm", "fallback"]
    fallback_reason: str | None = None
    why: str | None = None
    reason: str
    sent_tick: Annotated[int, Field(ge=0)]
    landed_tick: Annotated[int, Field(ge=0)]
    row_ids: list[str] = Field(default_factory=list)


class RecommendationPayload(_Model):
    """The frozen end of an investigation (WO-033 ``Recommendation``). The rung is computed
    from the outcome by the investigation machine; nothing downstream can edit it."""

    contact_id: str
    case_id: str
    outcome: str
    recommended_rung: Rung
    stop_clause: str
    threat_mass: float
    benign_mass: float
    top: Hypothesis
    uncertain: bool
    question_for_human: str | None = None
    evidence_row_ids: list[str] = Field(default_factory=list)
    label: Literal["stress-test estimate"] = POSTERIOR_LABEL


class LadderActionPayload(_Model):
    """One action the ladder executed (WO-043 driver), with the clause it rests on.

    ``clause`` is ``standing_orders.<rung>.<action>`` for an advisory or a hold (the
    standing orders' signer in ``signed_by``), ``approval.<action>`` for an approved action.
    ``timeout_s``/``expires_t_s`` are set for an approval request or a backup page."""

    contact_id: str
    state: LadderState
    action: str
    scope: dict[str, Any] | None = None
    clause: str | None = None
    signed_by: str | None = None
    timeout_s: float | None = None
    expires_t_s: float | None = None


# --- WO-060B end ---


# --- WO-089 begin: live sensor range rings ---
class SensorRangeRow(_Model):
    """One hydrophone node's ring: the passive-sonar p = 0.5 range at its current noise."""

    node_id: str
    effective_range_m: Annotated[float, Field(ge=0.0)]
    nl_db: float


class SensorRangesPayload(_Model):
    """Every node's ring, sent every tick after the ``tick`` event, sorted by node id
    (simulated physics, ``config/physics.yaml`` demo values; a vessel transit raises NL and
    shrinks the ring)."""

    nodes: list[SensorRangeRow]


# --- WO-089 end ---


# --- WO-086 begin: rung ledger and blunt-policy counter ---
BLUNT_LABEL: Literal["counterfactual: bang baseline"] = "counterfactual: bang baseline"


class BluntPayload(_Model):
    """Berth closures the bang baseline would have asked for on the same seeded contact
    stream (WO-050). Counterfactual: the approvals are assumed, nothing here happened."""

    berth_closures: Annotated[int, Field(ge=0)]
    """One per unknown contact a closure covers (DECISIONS 2026-10-17 06:37)."""
    episodes: Annotated[int, Field(ge=0)]
    """Distinct closure episodes (one per closure request); the secondary number."""
    closure_minutes: Annotated[float, Field(ge=0.0)]
    available: bool
    """False when the counter's sim run does not match the stream (shown as unavailable)."""
    label: Literal["counterfactual: bang baseline"] = BLUNT_LABEL


class LedgerPayload(_Model):
    """Contacts per ledger bucket so far (each contact once, at its max rung) and the
    blunt counter. Display only: nothing reads it back into the ladder."""

    counts: dict[LedgerBucket, Annotated[int, Field(ge=0)]]
    contacts_seen: Annotated[int, Field(ge=0)]
    blunt: BluntPayload


# --- WO-086 end ---


# --- envelopes ----------------------------------------------------------------------------


class EventEnvelope(_Model):
    """The fields every event carries."""

    v: Literal[1] = SCHEMA_VERSION
    run_id: str
    seq: Annotated[int, Field(ge=0)]
    tick: Annotated[int, Field(ge=0)]
    t_s: Annotated[float, Field(ge=0.0)]


class RunStarted(EventEnvelope):
    type: Literal["run_started"] = "run_started"
    payload: RunStartedPayload


class TickEvent(EventEnvelope):
    type: Literal["tick"] = "tick"
    payload: TickPayload


class EvidenceRowEvent(EventEnvelope):
    type: Literal["evidence_row"] = "evidence_row"
    payload: EvidenceRowPayload


class PosteriorEvent(EventEnvelope):
    type: Literal["posterior"] = "posterior"
    payload: PosteriorPayload


class TransitionEvent(EventEnvelope):
    type: Literal["transition"] = "transition"
    payload: TransitionPayload


class RungEvent(EventEnvelope):
    type: Literal["rung"] = "rung"
    payload: RungPayload


class ApprovalRequestEvent(EventEnvelope):
    type: Literal["approval_request"] = "approval_request"
    payload: ApprovalRequestPayload


class ApprovalResultEvent(EventEnvelope):
    type: Literal["approval_result"] = "approval_result"
    payload: ApprovalResultPayload


class SitrepEvent(EventEnvelope):
    type: Literal["sitrep"] = "sitrep"
    payload: SitrepPayload


class RunFinished(EventEnvelope):
    type: Literal["run_finished"] = "run_finished"
    payload: RunFinishedPayload


class ErrorEvent(EventEnvelope):
    type: Literal["error"] = "error"
    payload: ErrorPayload


# --- WO-060B begin ---
class InvestigationStepEvent(EventEnvelope):
    type: Literal["investigation_step"] = "investigation_step"
    payload: InvestigationStepPayload


class RecommendationEvent(EventEnvelope):
    type: Literal["recommendation"] = "recommendation"
    payload: RecommendationPayload


class LadderActionEvent(EventEnvelope):
    type: Literal["ladder_action"] = "ladder_action"
    payload: LadderActionPayload


# --- WO-060B end ---
# --- WO-089 begin ---
class SensorRangesEvent(EventEnvelope):
    type: Literal["sensor_ranges"] = "sensor_ranges"
    payload: SensorRangesPayload


# --- WO-089 end ---
# --- WO-088 begin ---
ResponderStateName = Literal["DOCKED", "TRANSIT", "ON_STATION", "RETURNING"]
"""The responder's states (``soundings.sim.responder.ResponderState`` names)."""


class ResponderTrailPoint(_Model):
    tick: Annotated[int, Field(ge=0)]
    lat: float
    lon: float


class ResponderPayload(_Model):
    """The harbor's responder at this tick (CLAUDE.md section 8 A2): a mobile probe that is
    dispatched to look, holds station at standoff and returns to dock. Display only: no
    field here can dispatch, position or recall it."""

    id: str
    state: ResponderStateName
    lat: float
    lon: float
    heading_deg: float
    speed_mps: float
    battery_min: float
    battery_capacity_min: float
    standoff_m: float
    target_contact_id: str | None = None
    distance_to_target_m: float | None = None
    dispatched: bool = False
    """True on the tick a dispatch to look begins (the UI's dispatch sound cue)."""
    trail: list[ResponderTrailPoint]
    """The last ``TRAIL_POINTS`` positions, oldest first, ending at this tick."""


class ResponderEvent(EventEnvelope):
    type: Literal["responder"] = "responder"
    payload: ResponderPayload


# --- WO-088 end ---
# --- WO-086 begin ---
class LedgerEvent(EventEnvelope):
    type: Literal["ledger"] = "ledger"
    payload: LedgerPayload


# --- WO-086 end ---


StreamEvent = Annotated[
    RunStarted
    | TickEvent
    | EvidenceRowEvent
    | PosteriorEvent
    | TransitionEvent
    | RungEvent
    | ApprovalRequestEvent
    | ApprovalResultEvent
    | SitrepEvent
    | RunFinished
    | ErrorEvent
    | SensorRangesEvent  # WO-089
    # --- WO-060B begin ---
    | InvestigationStepEvent
    | RecommendationEvent
    | LadderActionEvent
    # --- WO-060B end ---
    # --- WO-088 begin ---
    | ResponderEvent
    # --- WO-088 end ---
    # --- WO-086 begin ---
    | LedgerEvent,
    # --- WO-086 end ---
    Field(discriminator="type"),
]

EVENT_TYPES: tuple[str, ...] = (
    "run_started",
    "tick",
    "evidence_row",
    "posterior",
    "transition",
    "rung",
    "approval_request",
    "approval_result",
    "sitrep",
    "run_finished",
    "error",
    "sensor_ranges",  # WO-089
    # --- WO-060B begin ---
    "investigation_step",
    "recommendation",
    "ladder_action",
    # --- WO-060B end ---
    # --- WO-088 begin ---
    "responder",
    # --- WO-088 end ---
    # --- WO-086 begin ---
    "ledger",
    # --- WO-086 end ---
)

STREAM_ADAPTER: TypeAdapter[StreamEvent] = TypeAdapter(StreamEvent)


def parse_event(text: str | bytes) -> StreamEvent:
    """Validate one JSON event (unknown fields and unknown types are rejected)."""
    return STREAM_ADAPTER.validate_json(text)


def event_json(event: StreamEvent) -> str:
    """The event as canonical JSON (sorted keys, 6-dp floats): the wire and JSONL form."""
    from soundings.canonical import canonical_json

    return canonical_json(event)


def export_json_schema() -> dict[str, Any]:
    """The JSON Schema of ``StreamEvent``, committed under ``web/src/generated/``."""
    schema = STREAM_ADAPTER.json_schema(mode="serialization")
    schema["title"] = "SoundingsStreamEvent"
    schema["x-schema-version"] = SCHEMA_VERSION
    return schema


__all__ = [
    "EVENT_TYPES",
    "POSTERIOR_LABEL",
    "SCHEMA_VERSION",
    "ApprovalRequestEvent",
    "ApprovalResultEvent",
    "ErrorEvent",
    "EventEnvelope",
    "EvidenceRowEvent",
    "InvestigationStepEvent",  # WO-060B
    "InvestigationStepPayload",  # WO-060B
    "LadderActionEvent",  # WO-060B
    "LadderActionPayload",  # WO-060B
    "RecommendationEvent",  # WO-060B
    "RecommendationPayload",  # WO-060B
    "PosteriorEvent",
    "RunFinished",
    "RunStarted",
    "BLUNT_LABEL",  # WO-086
    "BluntPayload",  # WO-086
    "LedgerEvent",  # WO-086
    "LedgerPayload",  # WO-086
    "ResponderEvent",
    "ResponderPayload",
    "ResponderTrailPoint",
    "RungEvent",
    "SensorRangeRow",  # WO-089
    "SensorRangesEvent",  # WO-089
    "SensorRangesPayload",  # WO-089
    "SitrepEvent",
    "StreamEvent",
    "TickEvent",
    "TransitionEvent",
    "event_json",
    "export_json_schema",
    "parse_event",
]
