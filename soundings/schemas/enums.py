"""The fixed vocabularies. Member order is part of the contract (tests pin it).

Every enum is a ``StrEnum`` whose Pydantic schema is lax, so a string read from YAML or
GeoJSON becomes a member while every other field of a model stays strict. A value outside
the vocabulary is always rejected: a stray string cannot become a rung or a state
(CLAUDE.md 2.2), and no state after ``HANDED_OFF`` names an action (CLAUDE.md 2.1).
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, cast

from pydantic import GetCoreSchemaHandler
from pydantic_core import CoreSchema


class _VocabEnum(StrEnum):
    """StrEnum base whose Pydantic schema accepts member values given as plain strings."""

    @classmethod
    def __get_pydantic_core_schema__(
        cls, source_type: Any, handler: GetCoreSchemaHandler
    ) -> CoreSchema:
        schema: dict[str, Any] = dict(handler(source_type))
        schema["strict"] = False
        return cast(CoreSchema, schema)


class Hypothesis(_VocabEnum):
    FILED_ON_PLAN = "FILED_ON_PLAN"
    FILED_OFF_PLAN = "FILED_OFF_PLAN"
    UNFILED_KNOWN_OPERATOR = "UNFILED_KNOWN_OPERATOR"
    BIOLOGIC = "BIOLOGIC"
    DEBRIS_OR_DRIFT = "DEBRIS_OR_DRIFT"
    SURFACE_VESSEL_AIS_OFF = "SURFACE_VESSEL_AIS_OFF"
    UNAUTHORIZED_UUV = "UNAUTHORIZED_UUV"
    UNAUTHORIZED_DIVER = "UNAUTHORIZED_DIVER"


class Rung(_VocabEnum):
    """The organizers' ladder; values match ``config/ladder.yaml`` ``rungs``."""

    WATCH = "watch"
    ASK = "ask"
    SLOW = "slow"
    HOLD = "hold"
    HAND_TO_PERSON = "hand_to_person"


class LadderState(_VocabEnum):
    """SPEC-LADDER section 1. After HANDED_OFF only watching and departure exist."""

    DETECTED = "DETECTED"
    WATCHING = "WATCHING"
    ASKING = "ASKING"
    INVESTIGATING = "INVESTIGATING"
    ADVISING_SLOW = "ADVISING_SLOW"
    HOLDING = "HOLDING"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    HANDED_OFF = "HANDED_OFF"
    WATCH_AFTER_HANDOFF = "WATCH_AFTER_HANDOFF"
    RESOLVED_BENIGN = "RESOLVED_BENIGN"
    CLEARED_AT_ASK = "CLEARED_AT_ASK"
    HAZARD_NOTICE = "HAZARD_NOTICE"
    LOST_VEHICLE = "LOST_VEHICLE"
    RESOLVED_DEPARTED = "RESOLVED_DEPARTED"
    FAILED = "FAILED"


TERMINAL_STATES: frozenset[LadderState] = frozenset(
    {
        LadderState.RESOLVED_BENIGN,
        LadderState.CLEARED_AT_ASK,
        LadderState.HAZARD_NOTICE,
        LadderState.LOST_VEHICLE,
        LadderState.RESOLVED_DEPARTED,
        LadderState.FAILED,
    }
)
"""States from which the machine never moves."""

HANDLING_TERMINAL_STATES: frozenset[LadderState] = TERMINAL_STATES | {LadderState.HANDED_OFF}
"""HANDED_OFF ends handling (SPEC-LADDER section 3); only watching follows it."""


class EvidenceKind(_VocabEnum):
    FACT = "fact"
    ASSUMPTION = "assumption"
    DERIVED = "derived"


class FeatureName(_VocabEnum):
    """The 32 SPEC-INVESTIGATION section 2 features in section 2 order, then 5 support names."""

    # Kinematic
    SPEED_OVER_GROUND = "speed_over_ground"
    SPEED_VARIANCE = "speed_variance"
    DEPTH = "depth"
    DEPTH_VARIANCE = "depth_variance"
    HEADING_PERSISTENCE = "heading_persistence"
    TURN_RATE = "turn_rate"
    CLOSING_RATE_TO_NEAREST_CRITICAL_ASSET = "closing_rate_to_nearest_critical_asset"
    TIME_TO_NEAREST_CRITICAL_ASSET = "time_to_nearest_critical_asset"
    DWELL_TIME_IN_ZONE = "dwell_time_in_zone"
    TRACK_GAP_RATIO = "track_gap_ratio"
    DRIFT_CONSISTENCY = "drift_consistency"
    LOITER_SCORE = "loiter_score"
    TRANSIT_PATTERN = "transit_pattern"
    # Acoustic
    PROPELLER_TONAL_PRESENT = "propeller_tonal_present"
    TONAL_BAND = "tonal_band"
    CAVITATION_INDEX = "cavitation_index"
    BREATHING_OR_CLICK_PRESENT = "breathing_or_click_present"
    BROADBAND_LEVEL = "broadband_level"
    # Registry / identity
    REGISTRY_MATCH = "registry_match"
    PLAN_ID = "plan_id"
    MODEM_ID = "modem_id"
    HAIL_SENT_AT = "hail_sent_at"
    HAIL_REPLY = "hail_reply"
    OPERATOR_KNOWN = "operator_known"
    # Context
    ZONE_ID = "zone_id"
    ZONE_CRITICALITY = "zone_criticality"
    NEAREST_ASSET_ID = "nearest_asset_id"
    HOUR_OF_DAY = "hour_of_day"
    PATTERN_OF_LIFE_ANOMALY = "pattern_of_life_anomaly"
    AIS_SURFACE_CORRELATION = "ais_surface_correlation"
    SENSOR_HEALTH = "sensor_health"
    VISIBILITY_FOR_CAMERA = "visibility_for_camera"
    # Support: provenance rows, no likelihood row, never scored
    TRACK_SEGMENT = "track_segment"
    CURRENT_FIELD = "current_field"
    POL_BASELINE = "pol_baseline"
    WEEKDAY = "weekday"
    REACQUIRED_FROM = "reacquired_from"


class ProbeId(_VocabEnum):
    REGISTRY_LOOKUP = "registry_lookup"
    KINEMATICS_PROFILE = "kinematics_profile"
    DRIFT_CHECK = "drift_check"
    PATTERN_OF_LIFE_CHECK = "pattern_of_life_check"
    AIS_SURFACE_CORRELATE = "ais_surface_correlate"
    ACOUSTIC_FEATURE_READ = "acoustic_feature_read"
    ACOUSTIC_HAIL = "acoustic_hail"
    CAMERA_CUE = "camera_cue"
    SECOND_SENSOR_DWELL = "second_sensor_dwell"
    ASK_OPERATOR_BY_PHONE = "ask_operator_by_phone"
    REQUEST_WATCH_OFFICER_LOOK = "request_watch_officer_look"


class TransitPattern(_VocabEnum):
    STRAIGHT = "straight"
    LAWNMOWER = "lawnmower"
    ORBIT = "orbit"
    RANDOM = "random"
    STATIONARY = "stationary"


class TonalBand(_VocabEnum):
    NONE = "none"
    LOW = "low"
    MID = "mid"
    HIGH = "high"


class RegistryMatch(_VocabEnum):
    EXACT = "exact"
    AREA_MISMATCH = "area_mismatch"
    WINDOW_MISMATCH = "window_mismatch"
    NONE = "none"


class HailReply(_VocabEnum):
    VALID = "valid"
    INVALID = "invalid"
    NONE = "none"
    TIMEOUT = "timeout"


class BehaviorType(_VocabEnum):
    TRANSIT = "transit"
    LAWNMOWER = "lawnmower"
    ORBIT = "orbit"
    DRIFT = "drift"
    ERRATIC = "erratic"
    STATIONARY = "stationary"
    LAWNMOWER_THEN_DRIFT = "lawnmower_then_drift"
    VESSEL_TRANSIT = "vessel_transit"  # WO-080: a surface vessel, reported only on the AIS feed


class JobType(_VocabEnum):
    """Kinds of filed underwater work (WO-080). ``other`` is never generated (WO-081 default)."""

    SURVEY = "survey"
    HULL_CLEANING = "hull_cleaning"
    PILE_INSPECTION = "pile_inspection"
    DREDGE = "dredge"
    OTHER = "other"


class ToggleName(_VocabEnum):
    CONTACT_SURGE = "contact_surge"
    SENSOR_DROPOUT = "sensor_dropout"
    NETWORK_DOWN = "network_down"
    APPROVER_UNREACHABLE = "approver_unreachable"
    AIS_SPOOF = "ais_spoof"
    SECOND_COORDINATED_CONTACT = "second_coordinated_contact"
    STANDING_ORDERS_EXPIRED = "standing_orders_expired"
    CONTACT_GOES_QUIET = "contact_goes_quiet"
    FOG_NO_CAMERA = "fog_no_camera"


class Unit(_VocabEnum):
    M = "m"
    M_PER_S = "m/s"
    M_PER_S2 = "m/s2"
    DEG_PER_S = "deg/s"
    S = "s"
    RATIO = "ratio"
    BOOL = "bool"
    CATEGORY = "category"
    ID = "id"
    HOUR = "hour"
    DB = "dB"
    UNKNOWN = "unknown"
