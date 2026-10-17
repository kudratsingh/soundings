"""The UI's approval token (WO-061): a person's grant for exactly the requested scope.

Besides WO-041's ``approver_sim.py`` this is the only caller of
``soundings.ladder.approval.issue_token`` (``tests/test_approval.py`` holds that line). The
token names the request's contact and scope verbatim, so a person approves only what the
ladder asked; the ladder's guard (``approval_token_valid_and_scope_matches``) still checks
it on the next tick and decides (CLAUDE.md 2.2). Nothing here sets a rung or a state.
"""

from __future__ import annotations

from soundings.canonical import round6
from soundings.ladder.approval import ApprovalToken, issue_token
from soundings.ladder.types import ActionScope
from soundings.stream.events import ApprovalRequestPayload

UI_CHANNEL = "ui"
UI_APPROVER = "primary"


class RequestExpired(ValueError):
    """The request's window has closed (the ladder has already moved on)."""


def grant(request: ApprovalRequestPayload, approver: str, now_s: float) -> ApprovalToken:
    """A WO-041 token for exactly ``request.scope``, issued at ``now_s`` until the request
    expires. ``approver`` is the token's approver id (``primary`` for the watch officer)."""
    issued = round6(now_s)
    expires = round6(request.expires_t_s)
    if expires <= issued:
        raise RequestExpired(f"request {request.request_id} expired at t {expires} s")
    return issue_token(
        contact_id=request.contact_id,
        scope=ActionScope.model_validate(request.scope),
        approver=approver,
        channel=UI_CHANNEL,
        issued_at=issued,
        expires_at=expires,
    )


__all__ = ["UI_APPROVER", "UI_CHANNEL", "RequestExpired", "grant"]
