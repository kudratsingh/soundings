"""The simulated approver: a headless stand-in for the watch officer and the backup.

Implements WO-040's ``ApproverPort``. The primary answers on the ``ui`` channel (a stand-in
for the one approval button) and the backup on ``sms_sim`` (the simulated SMS reply).
Every grant goes through ``issue_token`` for exactly the requested scope, so a simulated
approval can never widen scope (SPEC-LADDER section 7).

Default (DECISIONS.md 23:05): the primary grants 60 s after the request, which keeps S05
at one interruption. ``ApproverScript.denied()`` and ``.unreachable()`` exist for the
``approver_unreachable`` toggle and the tests. CLAUDE.md 2.4: replies come due on the run
clock only, in (due time, contact id, side) order.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Literal

from soundings.canonical import round6
from soundings.clock import Clock
from soundings.schemas import ToggleName

from .approval import issue_token
from .ports import ApprovalReply
from .types import ActionScope

Side = Literal["primary", "backup"]

DEFAULT_REPLY_AFTER_S = 60.0
"""The default headless approver answers 60 s after a request (DECISIONS.md 23:05)."""

DEFAULT_TOKEN_TTL_S = 600.0
"""How long a simulated approval stays valid, in run-clock seconds."""


@dataclass(frozen=True)
class Reply:
    """One side's scripted answer: ``decision`` arrives ``after_s`` after the request."""

    decision: Literal["granted", "denied"]
    after_s: float = DEFAULT_REPLY_AFTER_S

    def __post_init__(self) -> None:
        if self.decision not in ("granted", "denied"):
            raise ValueError(f"reply decision must be granted or denied, got {self.decision!r}")
        if self.after_s < 0:
            raise ValueError("after_s must be non-negative")


@dataclass(frozen=True)
class ApproverScript:
    """Who answers and how. ``None`` on a side means that side never replies."""

    primary: Reply | None
    backup: Reply | None

    @classmethod
    def default(cls) -> ApproverScript:
        """The primary grants 60 s after the request; the backup would too."""
        return cls(primary=Reply("granted"), backup=Reply("granted"))

    @classmethod
    def denied(cls) -> ApproverScript:
        """The primary denies 60 s after the request (the hold is released at handoff)."""
        return cls(primary=Reply("denied"), backup=Reply("denied"))

    @classmethod
    def unreachable(cls) -> ApproverScript:
        """Nobody answers: the backup is paged, then the hold is released at handoff."""
        return cls(primary=None, backup=None)

    @classmethod
    def from_toggles(cls, toggles: Iterable[ToggleName | str]) -> ApproverScript:
        """``approver_unreachable``: nobody answers. ``network_down``: the primary is silent."""
        names = {ToggleName(t) for t in toggles}
        script = cls.default()
        if ToggleName.APPROVER_UNREACHABLE in names:
            return cls.unreachable()
        if ToggleName.NETWORK_DOWN in names:
            return cls(primary=None, backup=script.backup)
        return script


@dataclass(frozen=True)
class _Request:
    contact_id: str
    scope: ActionScope
    side: Side
    channel: str
    requested_at: float


@dataclass
class SimulatedApprover:
    """``ApproverPort`` over an ``ApproverScript``; answers each request at most once."""

    script: ApproverScript
    clock: Clock
    primary_channel: str = "ui"
    backup_channel: str = "sms_sim"
    token_ttl_s: float = DEFAULT_TOKEN_TTL_S
    requests: list[_Request] = field(default_factory=list)
    _answered: set[int] = field(default_factory=set)

    def _side(self, channel: str) -> Side:
        if channel == self.primary_channel:
            return "primary"
        if channel == self.backup_channel:
            return "backup"
        raise ValueError(f"unknown approver channel {channel!r}")

    def request(self, contact_id: str, scope: ActionScope, channel: str, now_s: float) -> None:
        """Ask the side behind ``channel`` to approve exactly ``scope``."""
        side = self._side(channel)
        self.requests.append(_Request(contact_id, scope, side, channel, round6(now_s)))

    def _reply_of(self, side: Side) -> Reply | None:
        return self.script.primary if side == "primary" else self.script.backup

    def poll(self, now_s: float | None = None) -> list[ApprovalReply]:
        """Every reply due by ``now_s`` (default: the clock), each request answered once."""
        now = round6(self.clock.now_s() if now_s is None else now_s)
        due: list[tuple[float, str, str, int, Reply]] = []
        for i, req in enumerate(self.requests):
            reply = self._reply_of(req.side)
            if i in self._answered or reply is None:
                continue
            at = round6(req.requested_at + reply.after_s)
            if at <= now:
                due.append((at, req.contact_id, req.side, i, reply))
        out: list[ApprovalReply] = []
        for at, _, _, i, reply in sorted(due, key=lambda d: d[:4]):
            self._answered.add(i)
            req = self.requests[i]
            token = None
            if reply.decision == "granted":
                token = issue_token(
                    contact_id=req.contact_id,
                    scope=req.scope,
                    approver=req.side,
                    channel=req.channel,
                    issued_at=at,
                    expires_at=at + self.token_ttl_s,
                ).model_dump(mode="json")
            out.append(
                ApprovalReply(
                    contact_id=req.contact_id,
                    decision=reply.decision,
                    token=token,
                    channel=req.channel,
                    at_s=at,
                )
            )
        return out
