"""WO-041: approval tokens bind one contact, one scope, one expiry (SPEC-LADDER section 7).

An approval can never widen scope: a token whose scope differs from the request in any way
(wider or narrower) blocks. Only the simulated approver and the UI button path may create
a token.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from soundings.ladder.approval import (
    DEMO_KEY_LABEL,
    KEY_ENV,
    ApprovalToken,
    issue_token,
    key_label,
    parse_token,
    token_hash,
    verify_token,
)
from soundings.ladder.types import ActionScope, GuardName

REPO = Path(__file__).resolve().parent.parent
CID = "c-0005"
REQ = ActionScope(action="hold_longer", asset_id="fuel_berth_1", gates=1, max_minutes=15)
ISSUED = 1000.0
EXPIRES = 1600.0
NOW = 1100.0


def make(scope: ActionScope = REQ, **over: object) -> ApprovalToken:
    fields: dict[str, object] = {
        "contact_id": CID,
        "scope": scope,
        "approver": "primary",
        "channel": "ui",
        "issued_at": ISSUED,
        "expires_at": EXPIRES,
    }
    fields.update(over)
    return issue_token(**fields)  # type: ignore[arg-type]


def check(token: object, *, contact_id: str = CID, requested: ActionScope = REQ, now: float = NOW):
    return verify_token(token, contact_id=contact_id, requested=requested, now_s=now)  # type: ignore[arg-type]


def test_valid_token_permits() -> None:
    token = make()
    d = check(token)
    assert d.permits and d.guard is GuardName.APPROVAL_TOKEN_VALID_AND_SCOPE_MATCHES
    assert d.granted_scope == REQ
    # The JSON form (what ApprovalReply / EventPayload carry) verifies the same way.
    assert check(token.model_dump(mode="json")).permits
    assert parse_token(token.model_dump(mode="json")) == token


def _flip(hexstr: str, i: int) -> str:
    c = hexstr[i]
    return hexstr[:i] + ("0" if c != "0" else "1") + hexstr[i + 1 :]


@pytest.mark.parametrize("i", [0, 31, 63])
def test_flipped_mac_byte_blocks(i: int) -> None:
    token = make()
    bad = token.model_copy(update={"mac": _flip(token.mac, i)})
    d = check(bad)
    assert not d.permits and d.reason == "bad_mac"


def test_edited_field_breaks_mac() -> None:
    token = make()
    stretched = token.model_copy(update={"expires_at": EXPIRES + 3600})
    assert check(stretched, now=EXPIRES + 10).reason == "bad_mac"
    data = token.model_dump(mode="json")
    data["scope"]["max_minutes"] = 30
    data["action"] = "hold_longer"
    assert check(data, requested=REQ.model_copy(update={"max_minutes": 30})).reason == "bad_mac"


def test_other_contact_blocks() -> None:
    assert check(make(), contact_id="c-0006").reason == "other_contact"
    assert check(make(contact_id="c-0006")).reason == "other_contact"


def test_expired_and_future_tokens_block() -> None:
    token = make()
    assert check(token, now=EXPIRES).reason == "expired"
    assert check(token, now=EXPIRES + 1).reason == "expired"
    assert check(token, now=EXPIRES - 1).permits
    assert check(token, now=ISSUED - 1).reason == "issued_in_future"
    assert check(token, now=ISSUED).permits


WIDER = REQ.model_copy(update={"gates": 2, "max_minutes": 30})
NARROWER = REQ.model_copy(update={"max_minutes": 10})


@pytest.mark.parametrize(
    "token_scope",
    [
        WIDER,
        NARROWER,
        REQ.model_copy(update={"asset_id": "fuel_berth_2"}),
        REQ.model_copy(update={"action": "hold_wider"}),
    ],
    ids=["wider_2_gates_30_min", "narrower", "other_asset", "other_action"],
)
def test_scope_mismatch_blocks(token_scope: ActionScope) -> None:
    d = check(make(token_scope))
    assert not d.permits and d.reason == "scope_mismatch"


def test_missing_or_malformed_token_blocks() -> None:
    assert check(None).reason == "unknown_input:approval_token"
    assert check({"contact_id": CID}).reason == "malformed_token"
    assert verify_token(make(), contact_id=CID, requested=None, now_s=NOW).reason == (
        "unknown_input:requested"
    )


@pytest.mark.parametrize("channel", ["sms", "email", "voice", "", "UI", "auto"])
def test_issue_token_rejects_other_channels(channel: str) -> None:
    with pytest.raises(ValueError, match="channel"):
        make(channel=channel)


def test_issue_token_accepts_ui_and_sms_sim() -> None:
    assert make(channel="ui").channel == "ui"
    assert make(channel="sms_sim").channel == "sms_sim"
    with pytest.raises(ValueError):
        make(expires_at=ISSUED)


def test_token_hash_is_stable_and_covers_the_mac() -> None:
    a, b = make(), make()
    assert a == b and token_hash(a) == token_hash(b)
    assert len(token_hash(a)) == 64
    assert token_hash(a) != token_hash(a.model_copy(update={"mac": _flip(a.mac, 0)}))


def test_key_from_env_or_demo_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(KEY_ENV, raising=False)
    demo = make()
    assert key_label() == DEMO_KEY_LABEL
    monkeypatch.setenv(KEY_ENV, "another-key-for-this-test")
    assert key_label() != DEMO_KEY_LABEL
    assert check(demo).reason == "bad_mac"  # a demo-key token fails under a real key
    keyed = make()
    assert keyed.mac != demo.mac and check(keyed).permits
    monkeypatch.delenv(KEY_ENV)
    assert check(keyed).reason == "bad_mac"


ALLOWED_CALLERS = {"soundings/ladder/approver_sim.py", "soundings/stream/ladder_adapter.py"}


def _calls_issue_token(path: Path) -> bool:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            name = (
                f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else ""
            )
            if name == "issue_token":
                return True
        if isinstance(node, ast.ImportFrom) and any(a.name == "issue_token" for a in node.names):
            return True
    return False


def test_only_approver_paths_issue_tokens() -> None:
    callers = {
        p.relative_to(REPO).as_posix()
        for p in sorted((REPO / "soundings").rglob("*.py"))
        if p.name != "approval.py" and _calls_issue_token(p)
    }
    assert callers <= ALLOWED_CALLERS, callers
    assert "soundings/ladder/approver_sim.py" in callers
    for extra in (REPO / "scripts").rglob("*.py"):
        assert not _calls_issue_token(extra), extra
