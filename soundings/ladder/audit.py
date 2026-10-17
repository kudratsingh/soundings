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
* ``llm_call``: role, fixture key, ``accepted|rejected|fallback_no_fixture|fallback``,
  violation reason; with WO-087 also the path (``live|replay|fallback``), the fallback
  reason, tokens in/out and latency (left out of the payload when unset).

Retention label on every row: "30 days (demo)". The DB path defaults to env
``SOUNDINGS_DB`` (the registry's file).
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, Literal, Protocol

from pydantic import (
    BaseModel,
    Field,
    SerializerFunctionWrapHandler,
    StrictStr,
    model_serializer,
    model_validator,
)
from sqlalchemy import Engine, event, text
from sqlalchemy.pool import StaticPool
from sqlmodel import Field as SQLField
from sqlmodel import Session, SQLModel, col, create_engine, select

from soundings.canonical import canonical_json, round6
from soundings.clock import TickClock
from soundings.ladder.actions import LadderAction
from soundings.ladder.approval import ApprovalToken, token_hash
from soundings.ladder.checkpoint import LadderCheckpoint
from soundings.ladder.types import GuardDecision
from soundings.registry.store import db_path_from_env
from soundings.schemas._base import HarborId, NonNegFloat, NonNegInt, StrictModel
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

# --- WO-042 audit semantics ---
_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:|#=-]*$")
# --- WO-042 audit semantics end ---


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


SITREP_PATHS = ("template", "fixture-replay", "fallback-after-rejection", "live")
"""Which path produced a SITREP (WO-042 part B); ``live`` is reserved for WO-087."""


class SitrepPayload(StrictModel):
    """A SITREP row. ``path``, ``fixture_key`` and ``violation_reason`` (WO-042 part B) are
    left out of the stored payload when unset, so part A rows hash exactly as before."""

    markdown_sha256: Hex64
    writer: Literal["template", "llm"]
    status: Literal["draft", "final", "update"]
    recommended_rung: Rung
    path: Literal["template", "fixture-replay", "fallback-after-rejection", "live"] | None = None
    fixture_key: Key | None = None
    violation_reason: HarborId | None = None
    channel: HarborId | None = None

    @model_serializer(mode="wrap")
    def _drop_unset(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        data: dict[str, Any] = handler(self)
        for name in ("path", "fixture_key", "violation_reason", "channel"):
            if data.get(name) is None:
                data.pop(name, None)
        return data

    @model_validator(mode="after")
    def _reason_iff_rejected(self) -> SitrepPayload:
        rejected = self.path == "fallback-after-rejection"
        if self.path is not None and rejected != (self.violation_reason is not None):
            raise ValueError("violation_reason is set exactly on fallback-after-rejection")
        return self


class LlmCallPayload(StrictModel):
    """One model call. The WO-087 fields are dropped from the stored payload when unset, so
    rows written before them hash exactly as before."""

    role: HarborId
    fixture_key: Key
    outcome: Literal["accepted", "rejected", "fallback_no_fixture", "fallback"]
    violation_reason: HarborId | None = None
    # --- WO-087 begin: which path answered, why it fell back, tokens, latency ---
    path: Literal["live", "replay", "fallback"] | None = None
    reason: Key | None = None
    tokens_in: NonNegInt | None = None
    tokens_out: NonNegInt | None = None
    latency_ms: NonNegFloat | None = None

    @model_serializer(mode="wrap")
    def _drop_unset(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        data: dict[str, Any] = handler(self)
        for name in ("path", "reason", "tokens_in", "tokens_out", "latency_ms"):
            if data.get(name) is None:
                data.pop(name, None)
        return data

    # --- WO-087 end ---

    @model_validator(mode="after")
    def _reason_iff_rejected(self) -> LlmCallPayload:
        if (self.outcome == "rejected") != (self.violation_reason is not None):
            raise ValueError("violation_reason is set exactly when the outcome is rejected")
        return self


# --- WO-087 begin: an llm_call row from a client call record ---
class CallRecordLike(Protocol):
    """The fields of a model client's call record this row reads (structural, so the audit
    module never imports the model package)."""

    @property
    def path(self) -> Literal["live", "replay", "fallback"]: ...
    @property
    def reason(self) -> str | None: ...
    @property
    def tokens_in(self) -> int: ...
    @property
    def tokens_out(self) -> int: ...
    @property
    def latency_ms(self) -> float: ...


def llm_call_payload(
    *,
    role: str,
    fixture_key: str,
    outcome: Literal["accepted", "rejected", "fallback_no_fixture", "fallback"],
    violation_reason: str | None = None,
    call: CallRecordLike | None = None,
    reason: str | None = None,
) -> LlmCallPayload:
    """An ``llm_call`` row; ``call`` (the client's record) adds path, reason, tokens, latency.

    With no ``call`` (the role's own budget refused the call before the client) the path is
    ``fallback`` and only the outcome tells why.
    """
    if call is None:
        return LlmCallPayload(
            role=role,
            fixture_key=fixture_key,
            outcome=outcome,
            violation_reason=violation_reason,
            path="fallback" if outcome != "accepted" else None,
            reason=reason,
        )
    return LlmCallPayload(
        role=role,
        fixture_key=fixture_key,
        outcome=outcome,
        violation_reason=violation_reason,
        path=call.path,
        reason=call.reason,
        tokens_in=call.tokens_in,
        tokens_out=call.tokens_out,
        latency_ms=round6(call.latency_ms),
    )


# --- WO-087 end ---


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


# --- WO-042 audit semantics ---
# Idempotency claims live in their own table, outside the hash chain, so the chained row
# format (and every part A row hash) is unchanged. A claim names (run_id, kind, key) and
# the sha256 of the canonical payload it was first written with; the claim and its row
# are inserted in the same SQLite transaction as every other row of the batch.

AUDIT_KEY_TABLE = "audit_key"


class AuditKeyRow(SQLModel, table=True):
    """One idempotency claim: the first row written under ``(run_id, kind, key)``."""

    __tablename__ = AUDIT_KEY_TABLE

    run_id: str = SQLField(primary_key=True)
    kind: str = SQLField(primary_key=True)
    key: str = SQLField(primary_key=True)
    body_sha256: str
    seq: int


class IdempotencyConflict(AuditError):
    """A key already claimed for this run and kind was re-used with a different payload."""


@dataclass(frozen=True)
class AuditEntry:
    """One row to write. ``idempotency_key`` (when set) makes a re-delivery a no-op."""

    kind: AuditKind | str
    payload: BaseModel | Mapping[str, Any]
    contact_id: str = ""
    idempotency_key: str | None = None


def body_sha256(body: Mapping[str, Any]) -> str:
    """sha256 of a payload's canonical JSON (sorted keys, fixed separators)."""
    return hashlib.sha256(canonical_json(dict(body)).encode("utf-8")).hexdigest()


# --- WO-042 audit semantics end ---


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
        # --- WO-042 audit semantics ---
        tables = [AuditRow.__table__, AuditKeyRow.__table__]  # type: ignore[attr-defined]
        SQLModel.metadata.create_all(self.engine, tables=tables)
        # --- WO-042 audit semantics end ---
        with self.engine.begin() as conn:
            for ddl in _TRIGGERS:
                conn.execute(text(ddl))

    @contextmanager
    def _session(self) -> Iterator[Session]:
        with Session(self.engine, expire_on_commit=False) as session:
            yield session

    def close(self) -> None:
        self.engine.dispose()

    # --- WO-042 audit semantics ---
    def append(
        self,
        kind: AuditKind | str,
        payload: BaseModel | Mapping[str, Any],
        *,
        run_id: str,
        t_s: float,
        contact_id: str = "",
        idempotency_key: str | None = None,
    ) -> AuditRow:
        """Validate ``payload`` against the kind's schema, chain it, and insert it.

        With ``idempotency_key``, a second append of the same key and payload returns the
        first row and writes nothing; the same key with another payload raises
        ``IdempotencyConflict``.
        """
        (row,) = self.append_many(
            [AuditEntry(kind, payload, contact_id, idempotency_key)], run_id=run_id, t_s=t_s
        )
        return row

    def append_many(
        self, entries: Sequence[AuditEntry], *, run_id: str, t_s: float
    ) -> list[AuditRow]:
        """Write ``entries`` as one SQLite transaction: every row, or none of them.

        Every payload is validated before the database is touched. Keyed entries whose key
        is already claimed with the same payload return the stored row and add nothing; a
        claimed key with a different payload aborts the whole batch. Any error (including
        a failing database) leaves the log exactly as it was and propagates to the caller,
        so the step that asked for these rows can refuse to commit.
        """
        if not run_id:
            raise AuditError("run_id must be non-empty")
        t = round6(float(t_s))
        if t < 0.0:
            raise AuditError("t_s is run-clock seconds and cannot be negative")
        prepared: list[tuple[AuditKind, dict[str, Any], AuditEntry]] = []
        for entry in entries:
            try:
                k = AuditKind(entry.kind)
            except ValueError as exc:
                raise AuditError(f"unknown audit kind {entry.kind!r}") from exc
            if entry.idempotency_key is not None and not _KEY_RE.match(entry.idempotency_key):
                raise AuditError(f"malformed idempotency key {entry.idempotency_key!r}")
            prepared.append((k, _payload_dict(k, entry.payload), entry))
        with self._session() as session:
            last = session.exec(
                select(AuditRow).order_by(col(AuditRow.seq).desc()).limit(1)
            ).first()
            seq = 0 if last is None else last.seq
            prev = GENESIS_HASH if last is None else last.row_hash
            out: list[AuditRow] = []
            claimed: dict[tuple[str, str], tuple[str, AuditRow]] = {}
            for k, body, entry in prepared:
                key = entry.idempotency_key
                digest = body_sha256(body)
                if key is not None:
                    known = claimed.get((k.value, key))
                    if known is None:
                        stored = session.get(AuditKeyRow, (run_id, k.value, key))
                        if stored is not None:
                            first = session.get(AuditRow, stored.seq)
                            if first is None:  # pragma: no cover - the chain forbids deletes
                                raise AuditError(f"claim {key!r} points at a missing row")
                            known = (stored.body_sha256, first)
                    if known is not None:
                        if known[0] != digest:
                            raise IdempotencyConflict(
                                f"{k.value} key {key!r} was already used with another payload"
                            )
                        out.append(known[1])
                        continue
                seq += 1
                row = AuditRow(
                    seq=seq,
                    run_id=run_id,
                    contact_id=entry.contact_id,
                    kind=k.value,
                    t_s=t,
                    payload_json=canonical_json(body),
                    prev_hash=prev,
                    row_hash=chain_hash(
                        prev,
                        seq=seq,
                        run_id=run_id,
                        contact_id=entry.contact_id,
                        kind=k.value,
                        t_s=t,
                        payload=body,
                    ),
                )
                prev = row.row_hash
                session.add(row)
                if key is not None:
                    session.add(
                        AuditKeyRow(
                            run_id=run_id, kind=k.value, key=key, body_sha256=digest, seq=seq
                        )
                    )
                    claimed[(k.value, key)] = (digest, row)
                out.append(row)
            session.commit()
            return out

    def claimed(self, run_id: str, kind: AuditKind | str, key: str) -> AuditRow | None:
        """The row first written under ``(run_id, kind, key)``, or ``None``."""
        with self._session() as session:
            stored = session.get(AuditKeyRow, (run_id, AuditKind(kind).value, key))
            return None if stored is None else session.get(AuditRow, stored.seq)

    # --- WO-042 audit semantics end ---

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


# --- the ladder listener (WO-042 part B) --------------------------------------------------


class AuditSink:
    """A WO-040 ``LadderListener`` that writes every step to an ``AuditLog``.

    Per listener call, in this order: probe rows from the event's ``probe_calls``, the
    transition row (when a checkpoint was written), one guard row per ``GuardDecision``,
    the approval row for an ``approval`` event (``token_hash`` only), then one row per
    action. ``t_s`` is the run clock (``clock.now_s()``; the checkpoint's ``now_s`` when
    there is one).

    A ``GuardDecision`` carries only ``inputs_hash`` and the granted scope, so the guard
    row's ``inputs`` holds exactly those unless ``guard_inputs`` (a lookup from
    ``inputs_hash`` to the dumped inputs, e.g. ``RecordingGuards.inputs_for``) knows more.
    """

    # --- WO-042 audit semantics ---
    # All rows of one call are one ``AuditLog.append_many`` transaction, and it runs while
    # the ladder step is still uncommitted (``LadderMachine`` notifies listeners before it
    # commits): if the write fails, nothing is recorded, the exception propagates and the
    # machine rolls the step back. Every mutation row carries an idempotency key, so a
    # re-delivered step writes no second row:
    #   probe       the probe's own key (a call without one is refused: fail closed);
    #   transition  "<contact>#<checkpoint seq>" (a different checkpoint at a seq conflicts);
    #   approval    the transition key of the approval event;
    #   action      the action's ledger key;
    #   guard       "<contact>:<guard>:<inputs_hash>" (one row per decision on given inputs).

    def __init__(
        self,
        audit: AuditLog,
        run_id: str,
        clock: TickClock | None = None,
        guard_inputs: Callable[[str], Mapping[str, Any] | None] | None = None,
    ) -> None:
        self.audit = audit
        self.run_id = run_id
        self.clock = clock
        self.guard_inputs = guard_inputs

    def _t_s(self, checkpoint: LadderCheckpoint | None) -> float:
        if checkpoint is not None:
            return float(checkpoint.now_s)
        if self.clock is not None:
            return float(self.clock.now_s())
        return 0.0

    def entries(
        self,
        checkpoint: LadderCheckpoint | None,
        actions: Sequence[LadderAction],
        decisions: Sequence[GuardDecision],
    ) -> list[AuditEntry]:
        """The rows one listener call writes, in order (pure: nothing is written)."""
        cid = ""
        if checkpoint is not None:
            cid = checkpoint.contact_id
        elif actions:
            cid = actions[0].contact_id
        out: list[AuditEntry] = []
        step_key: str | None = None
        if checkpoint is not None:
            event = checkpoint.event
            row_ids = tuple(r.row_id for r in event.payload.rows)
            step_key = f"{cid}#{checkpoint.seq}"
            for call in event.payload.extra.get("probe_calls", ()) or ():
                probe = probe_payload(call)
                out.append(AuditEntry(AuditKind.PROBE, probe, cid, probe.idempotency_key))
            transition = TransitionPayload(
                from_state=checkpoint.from_state,
                to_state=checkpoint.to_state,
                event=event.kind.value,
                arg=event.arg,
                rung=checkpoint.rung,
                checkpoint_hash=checkpoint.hash,
                evidence_row_ids=row_ids,
            )
            out.append(AuditEntry(AuditKind.TRANSITION, transition, cid, step_key))
        for decision in decisions:
            dumped: dict[str, Any] = {
                "inputs_hash": decision.inputs_hash,
                "granted_scope": None
                if decision.granted_scope is None
                else decision.granted_scope.model_dump(mode="json"),
            }
            known = None if self.guard_inputs is None else self.guard_inputs(decision.inputs_hash)
            if known is not None:
                dumped["read"] = dict(known)
            key = f"{cid}:{decision.guard.value}:{decision.inputs_hash}"
            guard_row = guard_decision_payload(decision, dumped)
            out.append(AuditEntry(AuditKind.GUARD_DECISION, guard_row, cid, key))
        if checkpoint is not None and checkpoint.event.kind.value == "approval":
            out.append(AuditEntry(AuditKind.APPROVAL, _approval_row(checkpoint), cid, step_key))
        for action in actions:
            action_row = ActionPayload(
                action=action.kind.value,
                idempotency_key=action.idempotency_key,
                rung=None if checkpoint is None else checkpoint.rung,
            )
            out.append(
                AuditEntry(
                    AuditKind.ACTION, action_row, action.contact_id or cid, action.idempotency_key
                )
            )
        return out

    def __call__(
        self,
        checkpoint: LadderCheckpoint | None,
        actions: Sequence[LadderAction],
        decisions: Sequence[GuardDecision],
    ) -> None:
        entries = self.entries(checkpoint, actions, decisions)
        if entries:
            self.audit.append_many(entries, run_id=self.run_id, t_s=self._t_s(checkpoint))


class GuardInputsLog:
    """What each guard decision read, by ``inputs_hash`` (a ``BoundaryGuards.observer``).

    ``record`` is the observer; calling the log looks an ``inputs_hash`` up for the sink.
    """

    def __init__(self) -> None:
        self._read: dict[str, dict[str, Any]] = {}

    def record(self, inputs_hash: str, read: dict[str, Any]) -> None:
        self._read[inputs_hash] = json.loads(canonical_json(read))

    def __call__(self, inputs_hash: str) -> Mapping[str, Any] | None:
        return self._read.get(inputs_hash)


PROBE_CALL_DISPLAY_FIELDS = frozenset({"contact_id", "tick", "cached", "source"})
"""Fields of an investigation ``ProbeCall`` that the probe row does not store."""


def probe_payload(call: Mapping[str, Any] | BaseModel) -> ProbePayload:
    """A probe row from a probe call (a mapping or an investigation ``ProbeCall``).

    Display-only fields are dropped and ``row_ids`` is read as ``evidence_row_ids``; any
    other field, or a call without an idempotency key, is refused with ``AuditError`` so
    no probe reaches the ladder unrecorded.
    """
    raw = call.model_dump(mode="json") if isinstance(call, BaseModel) else dict(call)
    data = {k: v for k, v in raw.items() if k not in PROBE_CALL_DISPLAY_FIELDS}
    if "row_ids" in data and "evidence_row_ids" not in data:
        data["evidence_row_ids"] = data.pop("row_ids")
    try:
        return ProbePayload.model_validate_json(canonical_json(data))
    except (ValueError, TypeError) as exc:
        raise AuditError(f"probe call cannot be audited: {exc}") from exc


# --- WO-042 audit semantics end ---


def _approval_row(checkpoint: LadderCheckpoint) -> ApprovalPayload:
    event = checkpoint.event
    outcome: Literal["granted", "denied", "timeout"] = (
        "granted" if event.arg == "granted" else "denied" if event.arg == "denied" else "timeout"
    )
    raw = event.payload.approval_token
    token = None if raw is None else ApprovalToken.model_validate(raw)
    scope = event.payload.requested_scope
    action = None if token is not None else (scope.action if scope is not None else "approval")
    return approval_payload(token, outcome=outcome, action=action)


__all__ = [
    "APPEND_ONLY_MESSAGE",
    "AUDIT_TABLE",
    "GENESIS_HASH",
    "PAYLOAD_MODELS",
    "RETENTION_LABEL",
    "SITREP_PATHS",
    "ActionPayload",
    "ApprovalPayload",
    "AuditEntry",
    "AuditError",
    "AuditKeyRow",
    "AuditKind",
    "AuditLog",
    "AuditRow",
    "AuditSink",
    "GuardDecisionPayload",
    "GuardInputsLog",
    "IdempotencyConflict",
    "LlmCallPayload",
    "llm_call_payload",
    "ProbePayload",
    "SitrepPayload",
    "TransitionPayload",
    "VerifyResult",
    "approval_payload",
    "body_sha256",
    "chain_hash",
    "guard_decision_payload",
    "probe_payload",
]
