"""Approval tokens: one contact, one scope, one expiry (SPEC-LADDER section 7).

A token is created only by ``issue_token``, and ``issue_token`` is called only from the
simulated approver (``approver_sim.py``) and the UI button path
(``soundings/stream/ladder_adapter.py``, WO-061); ``tests/test_approval.py`` holds that
line with an AST scan. The mac is HMAC-SHA256 (stdlib ``hmac``) over ``canonical_json``
of every other field, keyed by ``SOUNDINGS_APPROVAL_KEY`` or, when that is unset, a fixed
demo key the UI labels "demo key".

``verify_token`` blocks on a bad mac, another contact, any scope difference from the
request (wider or narrower), an expired token or one issued in the future. An approval can
therefore never widen scope: it approves exactly what was asked. Times are run-clock
seconds. CLAUDE.md 2.2: nothing here reads model output.
"""

from __future__ import annotations

import hashlib
import hmac
import os
from collections.abc import Mapping
from typing import Annotated, Any, Literal

from pydantic import Field, ValidationError, model_validator

from soundings.canonical import canonical_json, round6, stable_hash
from soundings.schemas._base import HarborId, StrictModel

from .types import ActionScope, GuardDecision, GuardName, GuardVerdict

KEY_ENV = "SOUNDINGS_APPROVAL_KEY"
DEMO_KEY = b"soundings-demo-approval-key-not-a-secret"
DEMO_KEY_LABEL = "demo key"

Channel = Literal["ui", "sms_sim"]
CHANNELS: tuple[str, ...] = ("ui", "sms_sim")

GUARD = GuardName.APPROVAL_TOKEN_VALID_AND_SCOPE_MATCHES

Mac = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class ApprovalToken(StrictModel):
    """A person's approval of exactly ``scope`` for ``contact_id`` until ``expires_at``.

    ``approver`` is the approver's id in the standing orders (``primary`` / ``backup``),
    never free text.
    """

    contact_id: Annotated[str, Field(pattern=r"^c-\d{4,6}$")]
    action: HarborId
    scope: ActionScope
    approver: HarborId
    channel: Channel
    issued_at: float
    expires_at: float
    mac: Mac

    @model_validator(mode="after")
    def _consistent(self) -> ApprovalToken:
        if self.action != self.scope.action:
            raise ValueError("token action must equal scope.action")
        return self


def approval_key() -> tuple[bytes, bool]:
    """(key, is_demo): ``SOUNDINGS_APPROVAL_KEY`` if set and non-empty, else the demo key."""
    env = os.environ.get(KEY_ENV, "")
    if env:
        return env.encode("utf-8"), False
    return DEMO_KEY, True


def key_label() -> str:
    """What the UI shows next to an approval: "demo key" when no key is configured."""
    return DEMO_KEY_LABEL if approval_key()[1] else "configured key"


def _mac(fields: Mapping[str, Any], key: bytes) -> str:
    body = {k: v for k, v in fields.items() if k != "mac"}
    return hmac.new(key, canonical_json(body).encode("utf-8"), hashlib.sha256).hexdigest()


def issue_token(
    *,
    contact_id: str,
    scope: ActionScope,
    approver: str,
    channel: str,
    issued_at: float,
    expires_at: float,
) -> ApprovalToken:
    """Create a token for exactly ``scope``. Only the UI button and the SMS stand-in call this."""
    if channel not in CHANNELS:
        raise ValueError(f"approval channel must be one of {list(CHANNELS)}, got {channel!r}")
    issued = round6(issued_at)
    expires = round6(expires_at)
    if expires <= issued:
        raise ValueError("expires_at must be after issued_at")
    fields: dict[str, Any] = {
        "contact_id": contact_id,
        "action": scope.action,
        "scope": scope.model_dump(mode="json"),
        "approver": approver,
        "channel": channel,
        "issued_at": issued,
        "expires_at": expires,
    }
    key, _ = approval_key()
    return ApprovalToken.model_validate({**fields, "scope": scope, "mac": _mac(fields, key)})


def token_hash(token: ApprovalToken) -> str:
    """What the audit log stores: ``stable_hash`` of the whole token, mac included."""
    return stable_hash(token)


def parse_token(raw: ApprovalToken | Mapping[str, Any] | None) -> ApprovalToken | None:
    """A token from its JSON form; ``None`` if absent or malformed."""
    if raw is None or isinstance(raw, ApprovalToken):
        return raw
    try:
        return ApprovalToken.model_validate_json(canonical_json(dict(raw)))
    except (ValidationError, TypeError, ValueError):
        return None


def _decision(
    permit: bool, reason: str, inputs_hash: str, scope: ActionScope | None
) -> GuardDecision:
    return GuardDecision(
        guard=GUARD,
        verdict=GuardVerdict.PERMIT if permit else GuardVerdict.BLOCK,
        reason=reason,
        inputs_hash=inputs_hash,
        granted_scope=scope if permit else None,
    )


def token_problem(
    token: ApprovalToken, *, contact_id: str, requested: ActionScope, now_s: float
) -> str | None:
    """The first reason ``token`` does not approve ``requested`` now, or ``None``."""
    key, _ = approval_key()
    expected = _mac(token.model_dump(mode="json"), key)
    if not hmac.compare_digest(expected, token.mac):
        return "bad_mac"
    if token.contact_id != contact_id:
        return "other_contact"
    if token.scope != requested:
        return "scope_mismatch"
    now = round6(now_s)
    if token.issued_at > now:
        return "issued_in_future"
    if now >= token.expires_at:
        return "expired"
    return None


def verify_token(
    token: ApprovalToken | Mapping[str, Any] | None,
    *,
    contact_id: str,
    requested: ActionScope | None,
    now_s: float,
) -> GuardDecision:
    """The ``approval_token_valid_and_scope_matches`` verdict for one token and request."""
    parsed = parse_token(token)
    inputs_hash = stable_hash(
        {
            "token": parsed,
            "token_present": token is not None,
            "contact_id": contact_id,
            "requested": requested,
            "now_s": round6(now_s),
        }
    )
    if token is None:
        return _decision(False, "unknown_input:approval_token", inputs_hash, None)
    if parsed is None:
        return _decision(False, "malformed_token", inputs_hash, None)
    if requested is None:
        return _decision(False, "unknown_input:requested", inputs_hash, None)
    problem = token_problem(parsed, contact_id=contact_id, requested=requested, now_s=now_s)
    if problem is not None:
        return _decision(False, problem, inputs_hash, None)
    return _decision(True, "token_valid_scope_matches", inputs_hash, requested)
