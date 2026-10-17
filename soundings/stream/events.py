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
    """Which runner produced the stream (``v1`` in Part A)."""


class TickContact(_Model):
    contact_id: str
    lat: float
    lon: float
    depth_m: float
    confidence: float
    sensor_id: str


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
    outcome: Literal["granted", "timeout", "backup_paged"]
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


class RunFinishedPayload(_Model):
    terminal: dict[str, LadderState]
    track_hash: str
    transition_log_hash: str
    n_events: int


class ErrorPayload(_Model):
    message: str


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
    | ErrorEvent,
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
    "PosteriorEvent",
    "RunFinished",
    "RunStarted",
    "RungEvent",
    "SitrepEvent",
    "StreamEvent",
    "TickEvent",
    "TransitionEvent",
    "event_json",
    "export_json_schema",
    "parse_event",
]
