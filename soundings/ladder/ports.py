"""Ports that later work orders plug into the ladder machine.

- ``ApproverPort`` (WO-041 ``SimulatedApprover``, WO-061 UI approver): asks a person and
  returns their replies; ``NoReplyApprover`` never answers.
- ``DeparturePort`` (WO-043 ``DepartureDetector``): says when a watched contact has left.
- ``LadderListener`` (WO-042 audit sink): sees every checkpoint, action and guard decision.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Literal, Protocol

from soundings.schemas import TrackPoint
from soundings.schemas._base import HarborId, NonNegInt, StrictModel

from .actions import LadderAction
from .checkpoint import LadderCheckpoint
from .types import ActionScope, EventKind, EventPayload, GuardDecision, LadderEvent


class ApprovalReply(StrictModel):
    """A person's answer. ``token`` is the JSON form of a WO-041 approval token."""

    contact_id: str
    decision: Literal["granted", "denied"]
    token: dict[str, Any] | None = None
    channel: HarborId
    at_s: float


class ApproverPort(Protocol):
    def request(self, contact_id: str, scope: ActionScope, channel: str, now_s: float) -> None: ...

    def poll(self, now_s: float) -> list[ApprovalReply]: ...


class NoReplyApprover:
    """Records requests and never replies (the unreachable approver)."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, ActionScope, str, float]] = []

    def request(self, contact_id: str, scope: ActionScope, channel: str, now_s: float) -> None:
        self.requests.append((contact_id, scope, channel, now_s))

    def poll(self, now_s: float) -> list[ApprovalReply]:
        return []


def reply_event(reply: ApprovalReply, tick: NonNegInt) -> LadderEvent:
    """The ``approval(granted|denied)`` event for a reply, carrying its token."""
    return LadderEvent(
        kind=EventKind.APPROVAL,
        arg=reply.decision,
        tick=tick,
        payload=EventPayload(approval_token=reply.token, extra={"channel": reply.channel}),
    )


class DeparturePort(Protocol):
    def observe(self, contact_id: str, tick: int, point: TrackPoint | None) -> bool: ...


class LadderListener(Protocol):
    """Called after every step that transitioned or evaluated a guard.

    ``checkpoint`` is ``None`` when a guard blocked and nothing transitioned.
    """

    def __call__(
        self,
        checkpoint: LadderCheckpoint | None,
        actions: Sequence[LadderAction],
        decisions: Sequence[GuardDecision],
    ) -> None: ...
