"""The handoff: SITREPs at the approval request and at HANDED_OFF, delivered and audited.

WO-042 part B (SPEC-LADDER sections 3, 6, 8; CLAUDE.md 2.1, 2.2, 2.6). ``HandoffService`` is
both the WO-043 driver's ``SitrepWriterLike`` and a WO-040 ``LadderListener``:

* as a listener it forwards every step to an ``AuditSink`` (transition, probe, guard
  decision, approval token hash, action rows) and remembers the actions already taken
  (slow advisory, hold, approved action) with the evidence rows their guards read;
* as the writer it turns the driver's port ``SitrepInputs`` (ids and numbers only) plus the
  contact's evidence rows into WO-042 ``SitrepInputs``: the WO-030 posterior is scored from
  those rows (``score_contact``, labeled "stress-test estimate"), the recommended rung is
  the computed one (the recommendation's rung, else the machine's rung), and the driver's
  ``unreachable`` flag becomes ``approver_unreachable``.

Draft SITREPs (entering AWAITING_APPROVAL) and updates (WATCH_AFTER_HANDOFF) are template
only. The final SITREP (HANDED_OFF) makes at most one ``sitrep_writer`` call per contact
when ``writer_mode="auto"``, never retried; a replay miss or a guard rejection delivers
the template. Each SITREP is delivered to the configured channel (``ui`` or the
``sms_sim`` stub) as a ``HandoffPacket`` and audited with the path that produced it
(``template | fixture-replay | fallback-after-rejection``), the fixture key and the
violation reason. The final SITREP is the handoff to the officer and counts as one
interruption (DECISIONS 23:05); the draft rides with the approval request (counted by the
ledger) and updates are not interruptions.

Nothing here acts after HANDED_OFF: updates only describe what the watch observed.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, Literal

from soundings.canonical import round6
from soundings.clock import TickClock
from soundings.investigation.evidence import EvidenceError, EvidenceTable
from soundings.investigation.posterior import (
    PosteriorReport,
    load_likelihood_tables,
    load_prior_config,
    score_contact,
)
from soundings.llm.client import ModelClient
from soundings.llm.guard import DEFAULT_VIOLATIONS_PATH
from soundings.llm.sitrep_writer import (
    ROLE_SITREP_WRITER,
    SitrepOutcome,
    SitrepPath,
    SitrepWriterRole,
    template_sitrep,
)
from soundings.schemas.enums import Rung
from soundings.schemas.evidence import EvidenceRow

from .actions import LadderAction
from .audit import AuditKind, AuditLog, AuditSink, LlmCallPayload, SitrepPayload
from .checkpoint import LadderCheckpoint
from .sitrep import (
    ActionLine,
    Sitrep,
    SitrepInputs,
    SitrepLine,
    SitrepStatus,
    render_markdown,
)
from .sitrep_port import UNREACHABLE
from .sitrep_port import SitrepInputs as PortInputs
from .types import ActionKind, GuardDecision

DeliveryChannel = Literal["ui", "sms_sim"]
WriterMode = Literal["auto", "template"]

TAKEN_KINDS: Final = frozenset(
    {ActionKind.ISSUE_SLOW_ADVISORY, ActionKind.START_HOLD, ActionKind.RECORD_APPROVED_ACTION}
)
"""Actions a SITREP lists under "Actions already taken"."""


@dataclass(frozen=True)
class HandoffPacket:
    """One delivered SITREP."""

    contact_id: str
    status: SitrepStatus
    sitrep: Sitrep
    markdown: str
    channel: DeliveryChannel
    path: SitrepPath
    t_s: float
    audit_seq: int | None = None
    fixture_key: str | None = None
    violation: str | None = None
    interruption: bool = False
    unreachable: bool = False
    inputs: SitrepInputs | None = None


PosteriorFn = Callable[[EvidenceTable, str], PosteriorReport]


def _default_posterior() -> PosteriorFn:
    tables = load_likelihood_tables()
    priors = load_prior_config()

    def score(table: EvidenceTable, contact_id: str) -> PosteriorReport:
        return score_contact(table, contact_id, tables, priors)

    return score


def evidence_table(rows: Iterable[EvidenceRow]) -> EvidenceTable:
    """A table of ``rows`` in (tick, row id) order; rows the table refuses are left out."""
    table = EvidenceTable()
    pending = sorted({r.row_id: r for r in rows}.values(), key=lambda r: (r.t, r.row_id))
    for _attempt in range(2):  # a derived row may sort before a source row
        retry: list[EvidenceRow] = []
        for row in pending:
            try:
                table.append(row)
            except EvidenceError:
                retry.append(row)
        pending = retry
    return table


@dataclass
class _ContactLog:
    rows: dict[str, EvidenceRow] = field(default_factory=dict)
    actions: list[tuple[ActionKind, Rung, float, str, tuple[str, ...]]] = field(
        default_factory=list
    )
    llm_called: bool = False


class HandoffService:
    """The driver's SITREP writer and a ladder listener (see the module docstring)."""

    def __init__(
        self,
        client: ModelClient | None = None,
        audit: AuditLog | None = None,
        *,
        run_id: str = "run",
        violations_path: Path = DEFAULT_VIOLATIONS_PATH,
        writer_mode: WriterMode = "template",
        channel: DeliveryChannel = "ui",
        clock: TickClock | None = None,
        posterior: PosteriorFn | None = None,
    ) -> None:
        if writer_mode == "auto" and client is None:
            raise ValueError("writer_mode='auto' needs a ModelClient")
        self.client = client
        self.audit = audit
        self.run_id = run_id
        self.violations_path = Path(violations_path)
        self.writer_mode: WriterMode = writer_mode
        self.channel: DeliveryChannel = channel
        self.sink = None if audit is None else AuditSink(audit, run_id, clock)
        self._posterior = posterior
        self._contacts: dict[str, _ContactLog] = {}
        self.packets: list[HandoffPacket] = []
        self.outbox: dict[DeliveryChannel, list[HandoffPacket]] = {"ui": [], "sms_sim": []}

    # --- LadderListener -------------------------------------------------------------------

    def __call__(
        self,
        checkpoint: LadderCheckpoint | None,
        actions: Sequence[LadderAction],
        decisions: Sequence[GuardDecision],
    ) -> None:
        if self.sink is not None:
            self.sink(checkpoint, actions, decisions)
        if checkpoint is None:
            return
        log = self._log(checkpoint.contact_id)
        rows = checkpoint.event.payload.rows
        for r in rows:
            if r.contact_id == checkpoint.contact_id:
                log.rows.setdefault(r.row_id, r)
        ids = tuple(r.row_id for r in rows)
        for action in actions:
            if action.kind not in TAKEN_KINDS or action.scope is None:
                continue
            rung = checkpoint.rung
            if action.kind is ActionKind.RECORD_APPROVED_ACTION:
                # The person approved the action at the rung it extends (the last one taken).
                clause = f"approval.{action.scope.action}"
                cite = ids or (log.actions[-1][4] if log.actions else ())
                rung = log.actions[-1][1] if log.actions else rung
            else:
                clause = f"standing_orders.{rung.value}.{action.scope.action}"
                cite = ids
            log.actions.append((action.kind, rung, checkpoint.now_s, clause, cite))

    # --- SitrepWriterLike -----------------------------------------------------------------

    def render(self, inputs: PortInputs) -> str:
        """Render from the rows this listener has seen (the driver hook passes all rows)."""
        log = self._log(inputs.contact_id)
        return self.render_with_rows(inputs, tuple(log.rows.values()))

    def render_with_rows(self, inputs: PortInputs, rows: Sequence[EvidenceRow]) -> str:
        """The WO-043 driver hook: port inputs plus every row the driver kept for the contact."""
        log = self._log(inputs.contact_id)
        for r in rows:
            if r.contact_id == inputs.contact_id:
                log.rows.setdefault(r.row_id, r)
        sitrep_inputs = self.adapt(inputs, tuple(log.rows.values()))
        notes = _watch_notes(inputs)
        packet = self.deliver(sitrep_inputs, t_s=inputs.now_s, notes=notes)
        return packet.markdown

    # --- assembly ---------------------------------------------------------------------------

    def adapt(self, port: PortInputs, rows: Sequence[EvidenceRow]) -> SitrepInputs:
        """WO-043 port inputs plus evidence rows -> WO-042 ``SitrepInputs``."""
        if self._posterior is None:
            self._posterior = _default_posterior()
        table = evidence_table(rows)
        report = self._posterior(table, port.contact_id)
        rung = port.recommended_rung if port.recommended_rung is not None else port.rung
        index = {r.row_id for r in table.rows()}
        decision_ids = tuple(i for i in port.cited_row_ids if i in index)
        taken: list[ActionLine] = []
        for _kind, a_rung, t_s, clause, cite in self._log(port.contact_id).actions:
            ids = tuple(i for i in cite if i in index)
            if ids:
                taken.append(ActionLine(rung=a_rung, t_s=t_s, clause=clause, evidence_ids=ids))
        return SitrepInputs.from_report(
            table,
            report,
            recommended_rung=rung,
            actions_taken=tuple(taken),
            approver_unreachable=UNREACHABLE in port.flags,
            status=SitrepStatus(port.kind),
            decision_row_ids=decision_ids,
        )

    def deliver(
        self,
        inputs: SitrepInputs,
        *,
        t_s: float,
        notes: Sequence[str] = (),
    ) -> HandoffPacket:
        """Write, deliver and audit one SITREP for ``inputs``."""
        cid = inputs.contact_id
        log = self._log(cid)
        t = round6(float(t_s))
        outcome: SitrepOutcome
        if (
            inputs.status is SitrepStatus.FINAL
            and self.writer_mode == "auto"
            and self.client is not None
            and not log.llm_called
        ):
            log.llm_called = True  # one call per handoff, never retried
            role = SitrepWriterRole(self.client, violations_path=self.violations_path)
            outcome = role.write(inputs, when=f"{self.run_id} {cid} t={t}")
        else:
            outcome = SitrepOutcome(template_sitrep(inputs), SitrepPath.TEMPLATE, None, None)
        sitrep = outcome.sitrep
        if notes:
            cite = sitrep.contact[0].evidence_ids
            extra = tuple(SitrepLine(text=n, evidence_ids=cite) for n in notes)
            sitrep = sitrep.model_copy(update={"contact": sitrep.contact + extra})
        markdown = render_markdown(sitrep)
        seq = self._audit(outcome, sitrep, markdown, t)
        packet = HandoffPacket(
            contact_id=cid,
            status=sitrep.status,
            sitrep=sitrep,
            markdown=markdown,
            channel=self.channel,
            path=outcome.path,
            t_s=t,
            audit_seq=seq,
            fixture_key=outcome.fixture_key,
            violation=None if outcome.violation is None else outcome.violation.value,
            interruption=sitrep.status is SitrepStatus.FINAL,
            unreachable=inputs.approver_unreachable,
            inputs=inputs,
        )
        self.packets.append(packet)
        self.outbox[self.channel].append(packet)
        return packet

    def _audit(
        self, outcome: SitrepOutcome, sitrep: Sitrep, markdown: str, t_s: float
    ) -> int | None:
        if self.audit is None:
            return None
        cid = sitrep.contact_id
        if outcome.llm_outcome is not None and outcome.fixture_key is not None:
            self.audit.append(
                AuditKind.LLM_CALL,
                LlmCallPayload(
                    role=ROLE_SITREP_WRITER,
                    fixture_key=outcome.fixture_key,
                    outcome=outcome.llm_outcome,
                    violation_reason=None if outcome.violation is None else outcome.violation.value,
                ),
                run_id=self.run_id,
                t_s=t_s,
                contact_id=cid,
            )
        row = self.audit.append(
            AuditKind.SITREP,
            SitrepPayload(
                markdown_sha256=_sha256(markdown),
                writer=sitrep.writer.value,
                status=sitrep.status.value,
                recommended_rung=sitrep.recommended_rung,
                path=outcome.path.value,
                fixture_key=outcome.fixture_key,
                violation_reason=None if outcome.violation is None else outcome.violation.value,
                channel=self.channel,
            ),
            run_id=self.run_id,
            t_s=t_s,
            contact_id=cid,
        )
        return row.seq

    # --- queries ----------------------------------------------------------------------------

    def interruptions(self, contact_id: str) -> int:
        """Handoff deliveries to the officer for ``contact_id`` (one per final SITREP)."""
        return sum(1 for p in self.packets if p.contact_id == contact_id and p.interruption)

    def packets_for(self, contact_id: str) -> list[HandoffPacket]:
        return [p for p in self.packets if p.contact_id == contact_id]

    def _log(self, contact_id: str) -> _ContactLog:
        return self._contacts.setdefault(contact_id, _ContactLog())


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _watch_notes(port: PortInputs) -> tuple[str, ...]:
    """Watch-after-handoff observations for an update (what the watch saw, nothing more)."""
    if port.kind != "update":
        return ()
    notes: list[str] = []
    if port.last_known is not None:
        where = port.last_known.zone_id or "outside every harbor zone"
        notes.append(
            f"Watch after handoff: last known position at tick {port.last_known.tick}, {where}"
        )
    if port.track_lost_at_tick is not None:
        notes.append(f"Watch after handoff: track lost at tick {port.track_lost_at_tick}")
    return tuple(notes)


__all__ = [
    "TAKEN_KINDS",
    "DeliveryChannel",
    "HandoffPacket",
    "HandoffService",
    "PosteriorFn",
    "WriterMode",
    "evidence_table",
]
