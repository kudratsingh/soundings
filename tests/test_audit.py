"""WO-042 part A: the append-only, hash-chained audit log and its verify CLI."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.exc import DBAPIError
from sqlmodel import Session, select

from soundings.canonical import canonical_json, stable_hash
from soundings.ladder.approval import issue_token, token_hash
from soundings.ladder.audit import (
    APPEND_ONLY_MESSAGE,
    GENESIS_HASH,
    PAYLOAD_MODELS,
    RETENTION_LABEL,
    AuditError,
    AuditKind,
    AuditLog,
    AuditRow,
    LlmCallPayload,
    ProbePayload,
    approval_payload,
    chain_hash,
    guard_decision_payload,
)
from soundings.ladder.guards import GuardInputs
from soundings.ladder.types import ActionScope, GuardDecision, GuardName, GuardVerdict
from soundings.probe_keys import idempotency_key, tick_bucket

REPO = Path(__file__).resolve().parent.parent
RUN = "S05-11"
CID = "c-0001"
ROW_A = "0123456789abcdef"
ROW_B = "fedcba9876543210"


def _token() -> Any:
    scope = ActionScope(action="hold_longer", basin="fuel_berths", max_minutes=10)
    return issue_token(
        contact_id=CID,
        scope=scope,
        approver="primary",
        channel="ui",
        issued_at=300.0,
        expires_at=900.0,
    )


def _guard_rows() -> tuple[GuardDecision, GuardInputs]:
    inputs = GuardInputs(
        contact_id=CID,
        now_s=240.0,
        threat_mass=0.71234567,
        benign_mass=0.1,
        time_to_nearest_critical_asset_s=150.0,
        zone_criticality=1.0,
        standing_orders_signed=True,
        nearest_asset_id="fuel_berth_1",
    )
    decision = GuardDecision(
        guard=GuardName.HOLD_PERMITTED,
        verdict=GuardVerdict.PERMIT,
        reason="hold permitted under signed standing orders",
        inputs_hash=stable_hash(inputs),
    )
    return decision, inputs


def _payloads() -> list[tuple[AuditKind, Any, float]]:
    """One payload per kind, in the order a handoff produces them."""
    key = idempotency_key(CID, "acoustic_hail", {}, tick_bucket(100, 2.0))
    decision, inputs = _guard_rows()
    return [
        (
            AuditKind.PROBE,
            ProbePayload(
                probe_id="acoustic_hail",
                idempotency_key=key,
                cost_usd=0.5,
                evidence_row_ids=(ROW_A,),
            ),
            200.0,
        ),
        (
            AuditKind.TRANSITION,
            {
                "from_state": "INVESTIGATING",
                "to_state": "HOLDING",
                "event": "standing_order_check",
                "arg": "hold_permitted",
                "rung": "hold",
                "evidence_row_ids": [ROW_A, ROW_B],
            },
            240.0,
        ),
        (AuditKind.GUARD_DECISION, guard_decision_payload(decision, inputs), 240.0),
        (
            AuditKind.ACTION,
            {"action": "request_approval", "idempotency_key": "act-0001", "rung": "hold"},
            240.0,
        ),
        (AuditKind.APPROVAL, approval_payload(_token(), outcome="granted"), 300.0),
        (
            AuditKind.SITREP,
            {
                "markdown_sha256": hashlib.sha256(b"# SITREP").hexdigest(),
                "writer": "template",
                "status": "final",
                "recommended_rung": "hand_to_person",
            },
            302.0,
        ),
        (
            AuditKind.LLM_CALL,
            LlmCallPayload(
                role="sitrep_writer", fixture_key="ab" * 32, outcome="fallback_no_fixture"
            ),
            302.0,
        ),
    ]


def _filled(path: Path | str) -> AuditLog:
    log = AuditLog(path)
    for kind, payload, t_s in _payloads():
        log.append(kind, payload, run_id=RUN, t_s=t_s, contact_id=CID)
    return log


# --- the chain ----------------------------------------------------------------------------


def test_every_kind_has_a_payload_schema() -> None:
    assert set(PAYLOAD_MODELS) == set(AuditKind)
    assert [k.value for k in AuditKind] == [
        "transition",
        "probe",
        "guard_decision",
        "approval",
        "action",
        "sitrep",
        "llm_call",
    ]


def test_each_kind_appends_a_chained_row(tmp_path: Path) -> None:
    log = _filled(tmp_path / "audit.db")
    rows = log.rows()
    assert [r.seq for r in rows] == list(range(1, len(AuditKind) + 1))
    assert {r.kind for r in rows} == {k.value for k in AuditKind}
    assert rows[0].prev_hash == GENESIS_HASH == "0" * 64
    for prev, row in zip(rows, rows[1:], strict=False):
        assert row.prev_hash == prev.row_hash
    for row in rows:
        assert row.retention_label == RETENTION_LABEL == "30 days (demo)"
        assert row.run_id == RUN and row.contact_id == CID
        assert len(row.row_hash) == 64
    result = log.verify()
    assert result.ok and result.rows == len(rows) and result.first_bad_seq is None
    assert result.head == rows[-1].row_hash == log.head()
    assert [r.kind for r in log.rows(kind="probe")] == ["probe"]
    assert log.rows(run_id="other") == []


def test_row_hash_formula(tmp_path: Path) -> None:
    """row_hash = sha256(prev_hash + canonical_json(seq, run, contact, kind, t_s, payload))."""
    log = _filled(tmp_path / "audit.db")
    prev = GENESIS_HASH
    for row in log.rows():
        body = {
            "seq": row.seq,
            "run_id": row.run_id,
            "contact_id": row.contact_id,
            "kind": row.kind,
            "t_s": row.t_s,
            "payload": json.loads(row.payload_json),
        }
        expected = hashlib.sha256((prev + canonical_json(body)).encode()).hexdigest()
        assert (
            row.row_hash
            == expected
            == chain_hash(
                prev, payload=row.payload, **{k: v for k, v in body.items() if k != "payload"}
            )
        )
        assert row.payload_json == canonical_json(row.payload)
        prev = row.row_hash


def test_probe_row_carries_idempotency_key(tmp_path: Path) -> None:
    log = _filled(tmp_path / "audit.db")
    (probe,) = log.rows(kind=AuditKind.PROBE)
    expected = idempotency_key(CID, "acoustic_hail", {}, tick_bucket(100, 2.0))
    assert probe.payload["idempotency_key"] == expected
    assert probe.payload["probe_id"] == "acoustic_hail"
    with pytest.raises(AuditError):
        log.append("probe", {"probe_id": "acoustic_hail", "cost_usd": 0.5}, run_id=RUN, t_s=1.0)
    with pytest.raises(AuditError):
        log.append(
            "probe", {"probe_id": "acoustic_hail", "idempotency_key": ""}, run_id=RUN, t_s=1.0
        )


def test_guard_row_carries_full_inputs(tmp_path: Path) -> None:
    log = _filled(tmp_path / "audit.db")
    (row,) = log.rows(kind=AuditKind.GUARD_DECISION)
    decision, inputs = _guard_rows()
    assert row.payload["inputs"] == json.loads(canonical_json(inputs))
    assert row.payload["inputs"]["threat_mass"] == 0.712346  # 6 dp at the boundary
    assert row.payload["inputs_hash"] == decision.inputs_hash
    assert row.payload["guard"] == "hold_permitted" and row.payload["verdict"] == "permit"
    with pytest.raises(AuditError):  # a guard row without its inputs is refused
        log.append(
            "guard_decision",
            {
                "guard": "hold_permitted",
                "verdict": "permit",
                "reason": "x",
                "inputs_hash": "0" * 64,
            },
            run_id=RUN,
            t_s=1.0,
        )


def test_approval_row_holds_token_hash_never_token(tmp_path: Path) -> None:
    log = _filled(tmp_path / "audit.db")
    (row,) = log.rows(kind=AuditKind.APPROVAL)
    token = _token()
    assert row.payload["token_hash"] == token_hash(token)
    assert set(row.payload) == {"token_hash", "outcome", "action", "approver", "channel"}
    assert token.mac not in row.payload_json
    with pytest.raises(AuditError):  # the token itself is never accepted
        log.append(
            "approval",
            {**row.payload, "token": token.model_dump(mode="json")},
            run_id=RUN,
            t_s=1.0,
        )
    with pytest.raises(AuditError):  # a grant without a hash is refused
        log.append(
            "approval",
            {"token_hash": None, "outcome": "granted", "action": "hold_longer"},
            run_id=RUN,
            t_s=1.0,
        )
    denied = approval_payload(None, outcome="timeout", action="hold_longer")
    assert log.append("approval", denied, run_id=RUN, t_s=400.0).payload["token_hash"] is None


def test_llm_call_row_outcomes(tmp_path: Path) -> None:
    log = AuditLog(tmp_path / "audit.db")
    rejected = LlmCallPayload(
        role="sitrep_writer",
        fixture_key="cd" * 32,
        outcome="rejected",
        violation_reason="recommended_rung_mismatch",
    )
    row = log.append(AuditKind.LLM_CALL, rejected, run_id=RUN, t_s=10.0, contact_id=CID)
    assert row.payload["violation_reason"] == "recommended_rung_mismatch"
    with pytest.raises(AuditError):  # rejected needs a reason
        log.append(
            "llm_call",
            {"role": "sitrep_writer", "fixture_key": "cd" * 32, "outcome": "rejected"},
            run_id=RUN,
            t_s=10.0,
        )
    with pytest.raises(AuditError):  # accepted carries none
        log.append(
            "llm_call",
            {
                "role": "sitrep_writer",
                "fixture_key": "cd" * 32,
                "outcome": "accepted",
                "violation_reason": "schema",
            },
            run_id=RUN,
            t_s=10.0,
        )
    with pytest.raises(AuditError):
        log.append("llm_call", {**rejected.model_dump(), "outcome": "maybe"}, run_id=RUN, t_s=1)


def test_bad_appends_leave_the_log_unchanged(tmp_path: Path) -> None:
    log = _filled(tmp_path / "audit.db")
    head = log.head()
    with pytest.raises(AuditError):
        log.append("closure", {}, run_id=RUN, t_s=1.0)
    with pytest.raises(AuditError):
        log.append("action", {"action": "x", "idempotency_key": "k"}, run_id="", t_s=1.0)
    with pytest.raises(AuditError):
        log.append("action", {"action": "x", "idempotency_key": "k"}, run_id=RUN, t_s=-1.0)
    with pytest.raises(AuditError):  # a payload model of the wrong kind
        log.append(
            "sitrep", ProbePayload(probe_id="drift_check", idempotency_key="k"), run_id=RUN, t_s=1.0
        )
    assert log.head() == head and log.verify().ok


def test_reopen_continues_the_chain(tmp_path: Path) -> None:
    path = tmp_path / "audit.db"
    first = _filled(path)
    head = first.head()
    first.close()
    again = AuditLog(path)
    row = again.append("action", {"action": "x", "idempotency_key": "k2"}, run_id=RUN, t_s=9.0)
    assert row.seq == len(AuditKind) + 1 and row.prev_hash == head
    assert again.verify().ok


def test_db_path_from_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "shared.db"
    monkeypatch.setenv("SOUNDINGS_DB", str(path))
    log = AuditLog()
    log.append("action", {"action": "x", "idempotency_key": "k"}, run_id=RUN, t_s=0.0)
    assert Path(log.db) == path and path.is_file()


def test_memory_db() -> None:
    log = _filled(":memory:")
    assert log.verify().ok and len(log) == len(AuditKind)


# --- append-only and tamper evidence ------------------------------------------------------


def _cli(*args: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "MODEL_MODE": "replay"}
    return subprocess.run(
        [sys.executable, "-m", "soundings.audit", *args],
        capture_output=True,
        text=True,
        cwd=REPO,
        env=env,
        check=False,
    )


def _raw(path: Path) -> sqlite3.Connection:
    """A raw connection with the append-only triggers dropped (what an attacker would do)."""
    conn = sqlite3.connect(path)
    conn.execute("DROP TRIGGER audit_row_no_update")
    conn.execute("DROP TRIGGER audit_row_no_delete")
    return conn


def test_append_only_and_tamper(tmp_path: Path) -> None:
    path = tmp_path / "audit.db"
    log = _filled(path)
    n = len(AuditKind)
    assert all(r.retention_label == "30 days (demo)" for r in log.rows())

    clean = _cli("verify", "--db", str(path))
    assert clean.returncode == 0, clean.stderr
    assert clean.stdout.strip() == f"ok {n} rows"

    # UPDATE and DELETE through SQLModel raise.
    with Session(log.engine) as session:
        row = session.exec(select(AuditRow).where(AuditRow.seq == 3)).one()
        row.payload_json = "{}"
        session.add(row)
        with pytest.raises(DBAPIError, match=APPEND_ONLY_MESSAGE):
            session.commit()
    with Session(log.engine) as session:
        row = session.exec(select(AuditRow).where(AuditRow.seq == 3)).one()
        session.delete(row)
        with pytest.raises(DBAPIError, match=APPEND_ONLY_MESSAGE):
            session.commit()
    assert log.verify().ok and len(log) == n
    log.close()

    # A raw connection drops the triggers and edits one payload.
    conn = _raw(path)
    payload = json.loads(
        conn.execute("SELECT payload_json FROM audit_row WHERE seq=4").fetchone()[0]
    )
    payload["rung"] = "watch"
    conn.execute("UPDATE audit_row SET payload_json=? WHERE seq=4", (canonical_json(payload),))
    conn.commit()
    conn.close()
    result = AuditLog(path).verify()
    assert not result.ok and result.first_bad_seq == 4
    broken = _cli("verify", "--db", str(path))
    assert broken.returncode == 1
    assert broken.stdout.strip() == "broken at seq 4"


def test_tamper_rehash_one_row_breaks_the_next(tmp_path: Path) -> None:
    """Editing a payload and recomputing its own row_hash still breaks the following link."""
    path = tmp_path / "audit.db"
    _filled(path).close()
    conn = _raw(path)
    seq, run_id, cid, kind, t_s, payload_json, prev = conn.execute(
        "SELECT seq, run_id, contact_id, kind, t_s, payload_json, prev_hash "
        "FROM audit_row WHERE seq=2"
    ).fetchone()
    payload = json.loads(payload_json)
    payload["to_state"] = "WATCHING"
    new_hash = chain_hash(
        prev, seq=seq, run_id=run_id, contact_id=cid, kind=kind, t_s=t_s, payload=payload
    )
    conn.execute(
        "UPDATE audit_row SET payload_json=?, row_hash=? WHERE seq=2",
        (canonical_json(payload), new_hash),
    )
    conn.commit()
    conn.close()
    assert AuditLog(path).verify().first_bad_seq == 3


def test_deleted_row_detected(tmp_path: Path) -> None:
    path = tmp_path / "audit.db"
    _filled(path).close()
    conn = _raw(path)
    conn.execute("DELETE FROM audit_row WHERE seq=3")
    conn.commit()
    conn.close()
    result = AuditLog(path).verify()
    assert not result.ok and result.first_bad_seq == 3
    assert _cli("verify", "--db", str(path)).returncode == 1


def test_swapped_rows_detected(tmp_path: Path) -> None:
    path = tmp_path / "audit.db"
    _filled(path).close()
    conn = _raw(path)
    conn.execute("UPDATE audit_row SET seq=-1 WHERE seq=5")
    conn.execute("UPDATE audit_row SET seq=5 WHERE seq=6")
    conn.execute("UPDATE audit_row SET seq=6 WHERE seq=-1")
    conn.commit()
    conn.close()
    result = AuditLog(path).verify()
    assert not result.ok and result.first_bad_seq == 5


def test_swapped_contents_detected(tmp_path: Path) -> None:
    """Swapping two rows' payloads and hashes in place (seq kept) is detected."""
    path = tmp_path / "audit.db"
    _filled(path).close()
    conn = _raw(path)
    cols = "run_id, contact_id, kind, t_s, payload_json, prev_hash, row_hash"
    a = conn.execute(f"SELECT {cols} FROM audit_row WHERE seq=2").fetchone()
    b = conn.execute(f"SELECT {cols} FROM audit_row WHERE seq=3").fetchone()
    sets = ", ".join(f"{c.strip()}=?" for c in cols.split(","))
    conn.execute(f"UPDATE audit_row SET {sets} WHERE seq=2", b)
    conn.execute(f"UPDATE audit_row SET {sets} WHERE seq=3", a)
    conn.commit()
    conn.close()
    assert AuditLog(path).verify().first_bad_seq == 2


def test_retention_label_edit_detected(tmp_path: Path) -> None:
    path = tmp_path / "audit.db"
    _filled(path).close()
    conn = _raw(path)
    conn.execute("UPDATE audit_row SET retention_label='forever' WHERE seq=1")
    conn.commit()
    conn.close()
    assert AuditLog(path).verify().first_bad_seq == 1


def test_cli_missing_db(tmp_path: Path) -> None:
    out = _cli("verify", "--db", str(tmp_path / "absent.db"))
    assert out.returncode == 2
    assert not (tmp_path / "absent.db").exists()


def test_cli_reads_env_db(tmp_path: Path) -> None:
    path = tmp_path / "env.db"
    _filled(path).close()
    env = {**os.environ, "SOUNDINGS_DB": str(path), "MODEL_MODE": "replay"}
    out = subprocess.run(
        [sys.executable, "-m", "soundings.audit", "verify"],
        capture_output=True,
        text=True,
        cwd=REPO,
        env=env,
        check=False,
    )
    assert out.returncode == 0 and out.stdout.strip() == f"ok {len(AuditKind)} rows"


# --- determinism ---------------------------------------------------------------------------

_HEAD_SCRIPT = """
import sys
sys.path.insert(0, {tests!r})
from test_audit import _filled
print(_filled(":memory:").head())
"""


def test_deterministic_chain_head(tmp_path: Path) -> None:
    """Same appends, two DBs: equal heads; PYTHONHASHSEED 0 and 1 print the same head."""
    a = _filled(tmp_path / "a.db").head()
    b = _filled(tmp_path / "b.db").head()
    assert a == b
    heads = []
    for seed in ("0", "1"):
        env = {**os.environ, "PYTHONHASHSEED": seed, "MODEL_MODE": "replay"}
        out = subprocess.run(
            [sys.executable, "-c", _HEAD_SCRIPT.format(tests=str(REPO / "tests"))],
            capture_output=True,
            text=True,
            cwd=REPO,
            env=env,
            check=True,
        )
        heads.append(out.stdout.strip())
    assert heads == [a, a]


def test_no_wall_clock_or_model_imports() -> None:
    text = (REPO / "soundings" / "ladder" / "audit.py").read_text(encoding="utf-8")
    for needle in ("soundings.llm", "anthropic", "datetime", "time.time"):
        assert needle not in text, needle
