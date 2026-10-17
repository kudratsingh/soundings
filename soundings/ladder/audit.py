"""Append-only, hash-chained audit log (SPEC-LADDER section 8, CLAUDE.md 2.4).

One SQLite table, ``audit_row``. Each row carries ``prev_hash`` (the previous row's
``row_hash``; 64 zeros for the first row) and

    row_hash = sha256(prev_hash + canonical_json({seq, run_id, contact_id, kind, t_s, payload}))

so editing, deleting or reordering any row breaks ``AuditLog.verify``. ``seq`` is
contiguous from 1. ``t_s`` is the run clock (seconds on the tick clock), never the wall
clock. SQLite ``BEFORE UPDATE`` and ``BEFORE DELETE`` triggers abort with "append-only",
and ``AuditLog`` has no update or delete method.

Row kinds and their payloads (typed, extra keys forbidden):

* ``transition``: from/to state, event, rung, the evidence rows it cites;
* ``probe``: probe id, idempotency key, cost;
* ``guard_decision``: the decision and the full guard inputs it read;
* ``approval``: the token hash, never the token itself;
* ``action``: action kind and idempotency key;
* ``sitrep``: sha256 of the rendered markdown, writer, status, recommended rung;
* ``llm_call``: role, fixture key, ``accepted|rejected|fallback_no_fixture``, violation reason.

Retention label on every row: "30 days (demo)". The DB path defaults to env
``SOUNDINGS_DB`` (the registry's file).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, StrictStr, model_validator
from sqlalchemy import Engine, event, text
from sqlalchemy.pool import StaticPool
from sqlmodel import Field as SQLField
from sqlmodel import Session, SQLModel, col, create_engine, select

from soundings.canonical import canonical_json, round6
from soundings.ladder.approval import ApprovalToken, token_hash
from soundings.ladder.types import GuardDecision
from soundings.registry.store import db_path_from_env
from soundings.schemas._base import HarborId, NonNegFloat, StrictModel
from soundings.schemas.enums import LadderState, ProbeId, Rung

GENESIS_HASH = "0" * 64
RETENTION_LABEL = "30 days (demo)"
AUDIT_TABLE = "audit_row"
APPEND_ONLY_MESSAGE = "append-only"
MEMORY = ":memory:"

Hex64 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
RowIdRef = Annotated[str, Field(pattern=r"^[0-9a-f]{16}$")]
Key = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:|#=-]*$")]
"""Idempotency keys and fixture keys: non-empty, no whitespace."""


class AuditKind(StrEnum):
    TRANSITION = "transition"
    PROBE = "probe"
    GUARD_DECISION = "guard_decision"
    APPROVAL = "approval"
    ACTION = "action"
    SITREP = "sitrep"
    LLM_CALL = "llm_call"


class AuditError(ValueError):
    """An append was refused (bad payload, bad kind); the log is unchanged."""


# --- payloads ---------------------------------------------------------------------------


class TransitionPayload(StrictModel):
    from_state: LadderState
    to_state: LadderState
    event: HarborId
    arg: StrictStr | None = None
    rung: Rung | None = None
    checkpoint_hash: Hex64 | None = None
    evidence_row_ids: tuple[RowIdRef, ...] = ()


class ProbePayload(StrictModel):
    probe_id: ProbeId
    idempotency_key: Key
    cost_usd: NonNegFloat = 0.0
    evidence_row_ids: tuple[RowIdRef, ...] = ()


class GuardDecisionPayload(StrictModel):
    """One guard decision with the full inputs it read (a ``GuardInputs`` dump)."""

    guard: HarborId
    verdict: Literal["permit", "block"]
    reason: StrictStr
    inputs_hash: Hex64
    inputs: dict[str, Any]


class ApprovalPayload(StrictModel):
    """An approval outcome. Only ``token_hash`` identifies the token; the token is never stored."""

    token_hash: Hex64 | None
    outcome: Literal["granted", "denied", "timeout"]
    action: HarborId
    approver: HarborId | None = None
    channel: HarborId | None = None

    @model_validator(mode="after")
    def _granted_has_hash(self) -> ApprovalPayload:
        if self.outcome == "granted" and self.token_hash is None:
            raise ValueError("a granted approval row must carry the token hash")
        return self


class ActionPayload(StrictModel):
    action: HarborId
    idempotency_key: Key
    rung: Rung | None = None
    evidence_row_ids: tuple[RowIdRef, ...] = ()


class SitrepPayload(StrictModel):
    markdown_sha256: Hex64
    writer: Literal["template", "llm"]
    status: Literal["draft", "final", "update"]
    recommended_rung: Rung


class LlmCallPayload(StrictModel):
    role: HarborId
    fixture_key: Key
    outcome: Literal["accepted", "rejected", "fallback_no_fixture"]
    violation_reason: HarborId | None = None

    @model_validator(mode="after")
    def _reason_iff_rejected(self) -> LlmCallPayload:
        if (self.outcome == "rejected") != (self.violation_reason is not None):
            raise ValueError("violation_reason is set exactly when the outcome is rejected")
        return self


def guard_decision_payload(
    decision: GuardDecision, inputs: BaseModel | Mapping[str, Any]
) -> GuardDecisionPayload:
    """A guard row from WO-040's ``GuardDecision`` and the full inputs it read."""
    dumped = inputs.model_dump(mode="json") if isinstance(inputs, BaseModel) else dict(inputs)
    return GuardDecisionPayload(
        guard=decision.guard.value,
        verdict=decision.verdict.value,
        reason=decision.reason,
        inputs_hash=decision.inputs_hash,
        inputs=json.loads(canonical_json(dumped)),
    )


def approval_payload(
    token: ApprovalToken | None,
    *,
    outcome: Literal["granted", "denied", "timeout"],
    action: str | None = None,
) -> ApprovalPayload:
    """An approval row: ``token_hash(token)`` and the token's ids, never the token itself."""
    if token is None:
        if action is None:
            raise AuditError("an approval row without a token must name the action")
        return ApprovalPayload(token_hash=None, outcome=outcome, action=action)
    return ApprovalPayload(
        token_hash=token_hash(token),
        outcome=outcome,
        action=token.action,
        approver=token.approver,
        channel=token.channel,
    )


PAYLOAD_MODELS: dict[AuditKind, type[StrictModel]] = {
    AuditKind.TRANSITION: TransitionPayload,
    AuditKind.PROBE: ProbePayload,
    AuditKind.GUARD_DECISION: GuardDecisionPayload,
    AuditKind.APPROVAL: ApprovalPayload,
    AuditKind.ACTION: ActionPayload,
    AuditKind.SITREP: SitrepPayload,
    AuditKind.LLM_CALL: LlmCallPayload,
}
"""The payload schema of each row kind."""


# --- the table --------------------------------------------------------------------------


class AuditRow(SQLModel, table=True):
    """One audit row. Never updated, never deleted (triggers abort with "append-only")."""

    __tablename__ = AUDIT_TABLE

    seq: int = SQLField(primary_key=True)
    run_id: str = SQLField(index=True)
    contact_id: str = ""
    kind: str
    t_s: float
    payload_json: str
    prev_hash: str
    row_hash: str
    retention_label: str = RETENTION_LABEL

    @property
    def payload(self) -> dict[str, Any]:
        loaded: dict[str, Any] = json.loads(self.payload_json)
        return loaded


def chain_hash(
    prev_hash: str,
    *,
    seq: int,
    run_id: str,
    contact_id: str,
    kind: str,
    t_s: float,
    payload: Mapping[str, Any],
) -> str:
    """``sha256(prev_hash + canonical_json({seq, run_id, contact_id, kind, t_s, payload}))``."""
    body = canonical_json(
        {
            "seq": seq,
            "run_id": run_id,
            "contact_id": contact_id,
            "kind": kind,
            "t_s": t_s,
            "payload": dict(payload),
        }
    )
    return hashlib.sha256((prev_hash + body).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class VerifyResult:
    """``ok`` when every row chains; ``first_bad_seq`` is the first seq that does not."""

    ok: bool
    rows: int
    first_bad_seq: int | None
    head: str


_TRIGGERS = (
    f"CREATE TRIGGER IF NOT EXISTS {AUDIT_TABLE}_no_update BEFORE UPDATE ON {AUDIT_TABLE} "
    f"BEGIN SELECT RAISE(ABORT, '{APPEND_ONLY_MESSAGE}'); END",
    f"CREATE TRIGGER IF NOT EXISTS {AUDIT_TABLE}_no_delete BEFORE DELETE ON {AUDIT_TABLE} "
    f"BEGIN SELECT RAISE(ABORT, '{APPEND_ONLY_MESSAGE}'); END",
)


def _make_engine(db: Path | str) -> Engine:
    if str(db) == MEMORY:
        engine = create_engine(
            "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
        )
    elif str(db).startswith("sqlite:"):
        engine = create_engine(str(db), connect_args={"check_same_thread": False})
    else:
        path = Path(db)
        path.parent.mkdir(parents=True, exist_ok=True)
        engine = create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False})

    @event.listens_for(engine, "connect")
    def _pragmas(dbapi_conn: Any, _record: Any) -> None:
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.close()

    return engine


def _payload_dict(kind: AuditKind, payload: BaseModel | Mapping[str, Any]) -> dict[str, Any]:
    model = PAYLOAD_MODELS[kind]
    try:
        if isinstance(payload, model):
            validated = payload
        elif isinstance(payload, BaseModel):
            raise AuditError(f"{kind.value} rows take {model.__name__}, not {type(payload)}")
        else:
            validated = model.model_validate_json(canonical_json(dict(payload)))
    except AuditError:
        raise
    except (ValueError, TypeError) as exc:
        raise AuditError(f"invalid {kind.value} payload: {exc}") from exc
    loaded: dict[str, Any] = json.loads(canonical_json(validated))
    return loaded


class AuditLog:
    """The audit table in SQLite: ``append``, ``rows``, ``head``, ``verify``. No update or delete.

    ``db`` is a path, a ``sqlite:`` URL or ``":memory:"``; ``None`` reads ``SOUNDINGS_DB``.
    """

    def __init__(self, db: Path | str | None = None) -> None:
        self.db: Path | str = db if db is not None else db_path_from_env()
        self.engine = _make_engine(self.db)
        SQLModel.metadata.create_all(self.engine, tables=[AuditRow.__table__])  # type: ignore[attr-defined]
        with self.engine.begin() as conn:
            for ddl in _TRIGGERS:
                conn.execute(text(ddl))

    @contextmanager
    def _session(self) -> Iterator[Session]:
        with Session(self.engine, expire_on_commit=False) as session:
            yield session

    def close(self) -> None:
        self.engine.dispose()

    def append(
        self,
        kind: AuditKind | str,
        payload: BaseModel | Mapping[str, Any],
        *,
        run_id: str,
        t_s: float,
        contact_id: str = "",
    ) -> AuditRow:
        """Validate ``payload`` against the kind's schema, chain it, and insert it."""
        try:
            k = AuditKind(kind)
        except ValueError as exc:
            raise AuditError(f"unknown audit kind {kind!r}") from exc
        if not run_id:
            raise AuditError("run_id must be non-empty")
        t = round6(float(t_s))
        if t < 0.0:
            raise AuditError("t_s is run-clock seconds and cannot be negative")
        body = _payload_dict(k, payload)
        with self._session() as session:
            last = session.exec(
                select(AuditRow).order_by(col(AuditRow.seq).desc()).limit(1)
            ).first()
            seq = 1 if last is None else last.seq + 1
            prev = GENESIS_HASH if last is None else last.row_hash
            row = AuditRow(
                seq=seq,
                run_id=run_id,
                contact_id=contact_id,
                kind=k.value,
                t_s=t,
                payload_json=canonical_json(body),
                prev_hash=prev,
                row_hash=chain_hash(
                    prev,
                    seq=seq,
                    run_id=run_id,
                    contact_id=contact_id,
                    kind=k.value,
                    t_s=t,
                    payload=body,
                ),
            )
            session.add(row)
            session.commit()
            return row

    def rows(
        self, *, run_id: str | None = None, kind: AuditKind | str | None = None
    ) -> list[AuditRow]:
        """Rows in ``seq`` order, optionally filtered by run and kind."""
        stmt = select(AuditRow)
        if run_id is not None:
            stmt = stmt.where(AuditRow.run_id == run_id)
        if kind is not None:
            stmt = stmt.where(AuditRow.kind == AuditKind(kind).value)
        with self._session() as session:
            return list(session.exec(stmt.order_by(col(AuditRow.seq))).all())

    def __len__(self) -> int:
        return len(self.rows())

    def head(self) -> str:
        """The last ``row_hash`` (``GENESIS_HASH`` for an empty log)."""
        with self._session() as session:
            last = session.exec(
                select(AuditRow).order_by(col(AuditRow.seq).desc()).limit(1)
            ).first()
        return GENESIS_HASH if last is None else last.row_hash

    def verify(self) -> VerifyResult:
        """Recompute the chain from the genesis hash; report the first seq that breaks it."""
        rows = self.rows()
        prev = GENESIS_HASH
        for expected, row in enumerate(rows, start=1):
            if row.seq != expected:
                return VerifyResult(False, len(rows), min(expected, row.seq), prev)
            try:
                payload = json.loads(row.payload_json)
                recomputed = chain_hash(
                    prev,
                    seq=row.seq,
                    run_id=row.run_id,
                    contact_id=row.contact_id,
                    kind=row.kind,
                    t_s=row.t_s,
                    payload=payload,
                )
            except (ValueError, TypeError):
                return VerifyResult(False, len(rows), row.seq, prev)
            if (
                row.prev_hash != prev
                or row.row_hash != recomputed
                or row.retention_label != RETENTION_LABEL
            ):
                return VerifyResult(False, len(rows), row.seq, prev)
            prev = row.row_hash
        return VerifyResult(True, len(rows), None, prev)


__all__ = [
    "APPEND_ONLY_MESSAGE",
    "AUDIT_TABLE",
    "GENESIS_HASH",
    "PAYLOAD_MODELS",
    "RETENTION_LABEL",
    "ActionPayload",
    "ApprovalPayload",
    "AuditError",
    "AuditKind",
    "AuditLog",
    "AuditRow",
    "GuardDecisionPayload",
    "LlmCallPayload",
    "ProbePayload",
    "SitrepPayload",
    "TransitionPayload",
    "VerifyResult",
    "approval_payload",
    "chain_hash",
    "guard_decision_payload",
]
