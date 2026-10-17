"""Typed vocabulary of the ladder machine: events, guards, actions, timings, guard context.

CLAUDE.md 2.1: no event, guard or action names anything after the handoff except watching.
CLAUDE.md 2.2: a guard reads a typed ``GuardContext`` built from evidence rows and numbers;
there is no free-text field a model could fill to steer a transition.
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any

from pydantic import Field, model_validator

from soundings.schemas import EvidenceRow, LadderState, load_ladder_config, load_standing_orders
from soundings.schemas._base import HarborId, NonNegInt, Probability, StrictModel
from soundings.schemas.config import LADDER_PATH, STANDING_ORDERS_PATH
from soundings.schemas.denylist import FreeText


class EventKind(StrEnum):
    """SPEC-LADDER section 2, plus the three events section 3 uses (DECISIONS.md 23:05)."""

    CONTACT_DETECTED = "contact_detected"
    PROBE_RESULT = "probe_result"
    POSTERIOR_UPDATED = "posterior_updated"
    STOP_RULE_FIRED = "stop_rule_fired"
    STANDING_ORDER_CHECK = "standing_order_check"
    APPROVAL = "approval"
    CONTACT_DEPARTED = "contact_departed"
    CONTACT_PERSISTED = "contact_persisted"
    ERROR = "error"
    TIMER = "timer"
    SCOPE_EXCEEDED = "scope_exceeded"
    HANDOFF_COMPLETE = "handoff_complete"


EVENT_ARGS: dict[EventKind, tuple[str, ...]] = {
    EventKind.STOP_RULE_FIRED: (
        "CLEARED_AT_ASK",
        "LOST_VEHICLE",
        "RESOLVED_BENIGN",
        "HAZARD_NOTICE",
        "HANDOFF",
    ),
    EventKind.STANDING_ORDER_CHECK: ("hold_permitted", "not_permitted"),
    EventKind.APPROVAL: ("granted", "denied", "timeout"),
    EventKind.CONTACT_PERSISTED: ("interval",),
    EventKind.TIMER: ("hold_limit", "slow_limit"),
}
"""The argument vocabulary per event kind; a kind not listed here takes no argument."""


class GuardName(StrEnum):
    FREE_PROBES_COMPLETE = "free_probes_complete"
    REGISTRY_EXACT_AND_HAIL_VALID = "registry_exact_and_hail_valid"
    NOT_CLEARED = "not_cleared"
    SLOW_PERMITTED = "slow_permitted"
    HOLD_PERMITTED = "hold_permitted"
    NOTHING_PREAUTHORIZED = "nothing_preauthorized"
    APPROVAL_TOKEN_VALID_AND_SCOPE_MATCHES = "approval_token_valid_and_scope_matches"
    BACKUP_EXISTS = "backup_exists"
    NO_BACKUP = "no_backup"


EVIDENCE_GUARDS: frozenset[GuardName] = frozenset(
    {
        GuardName.FREE_PROBES_COMPLETE,
        GuardName.REGISTRY_EXACT_AND_HAIL_VALID,
        GuardName.NOT_CLEARED,
    }
)
"""Guards decided from the event's evidence rows alone (``guard_port.evidence_guard``)."""


class GuardVerdict(StrEnum):
    PERMIT = "permit"
    BLOCK = "block"


class ActionKind(StrEnum):
    OPEN_EVIDENCE_TABLE = "open_evidence_table"
    ISSUE_SLOW_ADVISORY = "issue_slow_advisory"
    START_HOLD = "start_hold"
    RELEASE_HOLD = "release_hold"
    REQUEST_APPROVAL = "request_approval"
    PAGE_BACKUP = "page_backup"
    RECORD_APPROVED_ACTION = "record_approved_action"
    DELIVER_SITREP = "deliver_sitrep"
    UPDATE_SITREP = "update_sitrep"
    START_WATCH = "start_watch"
    CLOSE_WATCH = "close_watch"


class ActionScope(StrictModel):
    """What an action covers. ``action`` is a boundary action name from config/ladder.yaml."""

    action: HarborId
    basin: HarborId | None = None
    asset_id: HarborId | None = None
    gates: NonNegInt = 0
    max_minutes: Annotated[int, Field(gt=0)] | None = None


class GuardDecision(StrictModel):
    """One guard's verdict. ``inputs_hash`` is ``stable_hash`` of the context it read."""

    guard: GuardName
    verdict: GuardVerdict
    reason: FreeText
    inputs_hash: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    granted_scope: ActionScope | None = None

    @property
    def permits(self) -> bool:
        return self.verdict is GuardVerdict.PERMIT


class EventPayload(StrictModel):
    """What an event carries. Guards read rows and numbers here, never free text.

    ``approval_token`` is the JSON form of a WO-041 token (opaque to this module);
    ``extra`` carries audit-only references (probe calls, recommendation hash) that no
    guard reads.
    """

    rows: tuple[EvidenceRow, ...] = ()
    threat_mass: Probability | None = None
    benign_mass: Probability | None = None
    basin: HarborId | None = None
    requested_scope: ActionScope | None = None
    approval_token: dict[str, Any] | None = None
    extra: dict[str, Any] = Field(default_factory=dict)


class LadderEvent(StrictModel):
    """One ladder event at a run tick. ``arg`` must belong to ``EVENT_ARGS[kind]``."""

    kind: EventKind
    arg: str | None = None
    tick: NonNegInt
    payload: EventPayload = Field(default_factory=EventPayload)

    @model_validator(mode="after")
    def _arg_in_vocabulary(self) -> LadderEvent:
        allowed = EVENT_ARGS.get(self.kind, ())
        if allowed and self.arg not in allowed:
            raise ValueError(f"{self.kind.value} takes one of {list(allowed)}, got {self.arg!r}")
        if not allowed and self.arg is not None:
            raise ValueError(f"{self.kind.value} takes no argument")
        return self


class LadderTimings(StrictModel):
    """Every ladder timer length, in run-clock seconds, from config (never literals)."""

    sitrep_update_interval_s: Annotated[float, Field(gt=0)]
    departed_after_s_outside_zone: Annotated[float, Field(ge=0)]
    primary_timeout_s: Annotated[float, Field(gt=0)]
    backup_timeout_s: Annotated[float, Field(gt=0)] | None = None

    @classmethod
    def from_config(
        cls,
        ladder_path: str | Path = LADDER_PATH,
        orders_path: str | Path = STANDING_ORDERS_PATH,
    ) -> LadderTimings:
        """Read ``watch_after_handoff`` and the approver timeouts through WO-010's loaders.

        The standing-orders signature is not checked here (WO-041 owns that).
        """
        ladder = load_ladder_config(ladder_path)
        orders = load_standing_orders(path=orders_path).orders
        wah = ladder.watch_after_handoff
        backup = orders.approvers.get("backup")
        return cls(
            sitrep_update_interval_s=float(wah["sitrep_update_interval_s"]),
            departed_after_s_outside_zone=float(wah["departed_after_s_outside_zone"]),
            primary_timeout_s=float(orders.approvers["primary"].timeout_s),
            backup_timeout_s=None if backup is None else float(backup.timeout_s),
        )


class GuardContext(StrictModel):
    """Everything a guard may read. Unknown inputs are ``None``, never imputed.

    ``active_holds`` counts the holds of every other contact in the run (a contact has at
    most one hold of its own: no row enters HOLDING from HOLDING).
    """

    contact_id: str
    state: LadderState
    event: LadderEvent
    now_s: float
    threat_mass: Probability | None = None
    benign_mass: Probability | None = None
    time_to_nearest_critical_asset_s: float | None = None
    zone_criticality: Probability | None = None
    basin: HarborId | None = None
    nearest_asset_id: str | None = None
    active_holds: NonNegInt = 0
    backup_paged: bool = False
    approval_token: dict[str, Any] | None = None
    requested_scope: ActionScope | None = None
