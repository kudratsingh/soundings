"""WO-042 audit commit semantics (SPEC-LADDER section 8).

Four claims, each proved against Soundings' own ladder, sink and handoff service:

* coverage: the S05-shaped stream writes one row per transition, probe, guard decision,
  approval, action, SITREP and model call, and the counts match what the run did;
* audit-or-no-commit: when the audit write fails, the ladder step is not committed (state,
  rung, timers, ledger, checkpoint store and the log itself are as they were) and the
  caller sees the error; the same event goes through once the log works again;
* idempotent re-delivery: a mutation re-delivered under its key writes no second row and
  has no second effect, and a key re-used with other content is refused;
* the hash chain verifies after every one of the above, including after a failed batch.

Plus the SITREP faithfulness cases the part B guard tests do not already pin.
"""

from __future__ import annotations

import sqlite3
from collections import Counter
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from soundings.clock import TickClock
from soundings.investigation.machine import ProbeCall
from soundings.ladder.actions import ActionLedger
from soundings.ladder.audit import (
    AuditEntry,
    AuditError,
    AuditKind,
    AuditLog,
    AuditSink,
    IdempotencyConflict,
    ProbePayload,
    body_sha256,
)
from soundings.ladder.checkpoint import MemoryCheckpointStore
from soundings.ladder.handoff import HandoffService, issuance_key
from soundings.ladder.machine import LadderMachine
from soundings.ladder.types import EventKind, EventPayload, LadderEvent, LadderTimings
from soundings.llm.sitrep_writer import SitrepViolation, guard_sitrep
from soundings.probe_keys import idempotency_key
from soundings.schemas.enums import LadderState, ProbeId
from tests.support import sitrep_bodies
from tests.support.handoff_runs import run_s05
from tests.support.scripted_guards import ScriptedGuards

RUN = "semantics"
CONTACT = "c-0042"
TIMINGS = LadderTimings(
    sitrep_update_interval_s=300.0,
    departed_after_s_outside_zone=120.0,
    primary_timeout_s=120.0,
    backup_timeout_s=180.0,
)
LEAD_IN = (
    (EventKind.CONTACT_DETECTED, None),
    (EventKind.PROBE_RESULT, None),
    (EventKind.PROBE_RESULT, None),
)
"""DETECTED -> WATCHING -> ASKING -> INVESTIGATING with every scripted guard permitting."""


class Flaky:
    """Wraps ``AuditLog.append_many``; while ``down`` every batch fails like a lost disk."""

    def __init__(self, log: AuditLog) -> None:
        self.log = log
        self.down = False
        self.failures = 0
        self._real = log.append_many

    def append_many(self, entries: Any, *, run_id: str, t_s: float) -> Any:
        if self.down:
            self.failures += 1
            raise sqlite3.OperationalError("disk I/O error")
        return self._real(entries, run_id=run_id, t_s=t_s)


@pytest.fixture
def log(tmp_path: Path) -> Iterator[AuditLog]:
    audit = AuditLog(tmp_path / "audit.db")
    yield audit
    audit.close()


def _ladder(audit: AuditLog, *, run_id: str = RUN) -> tuple[LadderMachine, MemoryCheckpointStore]:
    store = MemoryCheckpointStore()
    machine = LadderMachine(
        CONTACT,
        ScriptedGuards(),
        TickClock(tick_s=2.0),
        ActionLedger(),
        store,
        TIMINGS,
        listeners=[AuditSink(audit, run_id)],
    )
    return machine, store


def _ev(kind: EventKind, arg: str | None, tick: int, **extra: Any) -> LadderEvent:
    return LadderEvent(kind=kind, arg=arg, tick=tick, payload=EventPayload(extra=extra))


def _investigating(machine: LadderMachine) -> None:
    for tick, (kind, arg) in enumerate(LEAD_IN):
        assert machine.step(_ev(kind, arg, tick)).transitioned
    assert machine.state is LadderState.INVESTIGATING


def _picture(machine: LadderMachine, store: MemoryCheckpointStore) -> tuple[Any, ...]:
    """Everything a committed step would change."""
    return (
        machine.state,
        machine.rung,
        machine.head_hash,
        len(machine.checkpoints),
        machine.ledger.keys(),
        machine.timers.pending(),
        machine.hold_scope,
        len(store.load()),
    )


def _probe_call(probe: ProbeId, bucket: int, *, cached: bool = False) -> dict[str, Any]:
    key = idempotency_key(CONTACT, probe.value, {}, bucket)
    call = ProbeCall(
        contact_id=CONTACT,
        probe_id=probe,
        tick=bucket * 15,
        idempotency_key=key,
        cost_usd=0.0,
        cached=cached,
        source="deterministic",
    )
    return call.model_dump(mode="json")


# --- (a) one row per mutation, counted on the S05-shaped stream ---------------------------


def test_s05_stream_writes_one_row_per_mutation(tmp_path: Path) -> None:
    registry = _probe_call(ProbeId.REGISTRY_LOOKUP, 0)
    hail = _probe_call(ProbeId.ACOUSTIC_HAIL, 1)
    again = _probe_call(ProbeId.ACOUSTIC_HAIL, 1, cached=True)  # same key, served from cache
    out = run_s05(tmp_path, writer_mode="auto", probe_calls=(registry, hail, again))
    rows = out.audit.rows(run_id="s05-seed11")
    counts = Counter(r.kind for r in rows)

    assert counts[AuditKind.TRANSITION] == len(out.run.checkpoints)
    assert counts[AuditKind.PROBE] == 2  # two keys; the cached re-run is not a second probe
    assert counts[AuditKind.ACTION] == len(out.run.actions)
    distinct_guard_reads = {(d.guard, d.inputs_hash) for d in out.run.guard_decisions}
    assert counts[AuditKind.GUARD_DECISION] == len(distinct_guard_reads)
    assert counts[AuditKind.APPROVAL] == 1
    assert counts[AuditKind.SITREP] == len(out.service.packets) == len(out.run.sitreps)
    assert counts[AuditKind.LLM_CALL] == 1  # one sitrep_writer call, at the final SITREP
    assert set(counts) == set(AuditKind)

    probes = [r.payload for r in rows if r.kind == AuditKind.PROBE]
    assert [p["idempotency_key"] for p in probes] == [
        registry["idempotency_key"],
        hail["idempotency_key"],
    ]
    assert all(set(p) == set(ProbePayload.model_fields) for p in probes)
    (call,) = [r.payload for r in rows if r.kind == AuditKind.LLM_CALL]
    assert call["role"] == "sitrep_writer" and call["outcome"] == "accepted"
    assert len(call["fixture_key"]) == 64
    for row in rows:
        if row.kind == AuditKind.GUARD_DECISION:
            read = row.payload["inputs"]["read"]  # what the guard actually read
            assert read and "contact_id" in read
            token = read.get("approval_token")
            assert token is None or set(token) == {"token_hash"}
    assert out.audit.verify().ok


def test_probe_call_without_a_key_is_refused_and_the_step_rolls_back(log: AuditLog) -> None:
    machine, store = _ladder(log)
    before = _picture(machine, store)
    keyless = {"probe_id": ProbeId.DRIFT_CHECK.value, "cost_usd": 0.0}
    with pytest.raises(AuditError):
        machine.step(_ev(EventKind.CONTACT_DETECTED, None, 0, probe_calls=[keyless]))
    assert _picture(machine, store) == before
    assert len(log) == 0


# --- (b) if the audit write fails, the action does not commit -----------------------------


def test_failed_audit_write_leaves_the_machine_unchanged(
    log: AuditLog, monkeypatch: pytest.MonkeyPatch
) -> None:
    flaky = Flaky(log)
    monkeypatch.setattr(log, "append_many", flaky.append_many)
    machine, store = _ladder(log)
    _investigating(machine)
    rows_before = len(log)
    before = _picture(machine, store)

    handoff = _ev(EventKind.STOP_RULE_FIRED, "HANDOFF", 3)
    flaky.down = True
    with pytest.raises(sqlite3.OperationalError):
        machine.step(handoff)
    assert flaky.failures == 1
    assert _picture(machine, store) == before  # no state, rung, timer, ledger or checkpoint
    assert len(log) == rows_before and log.verify().ok

    # The log is back: the same event commits, exactly as on a machine that never failed.
    flaky.down = False
    result = machine.step(handoff)
    assert result.transitioned and machine.state is LadderState.ADVISING_SLOW
    assert machine.timers.pending() and machine.ledger.keys()

    control = AuditLog(":memory:")
    twin, _ = _ladder(control)
    _investigating(twin)
    twin.step(handoff)
    assert twin.head_hash == machine.head_hash
    assert [r.payload_json for r in control.rows()] == [r.payload_json for r in log.rows()]
    control.close()
    assert log.verify().ok


def test_a_database_refusal_mid_batch_writes_none_of_the_batch(log: AuditLog) -> None:
    """The transition row goes in before the action rows; a refused action row takes the
    transition row with it, because the whole step is one SQLite transaction."""
    machine, store = _ladder(log)
    _investigating(machine)
    rows_before = len(log)
    before = _picture(machine, store)
    raw = sqlite3.connect(str(log.db))
    raw.execute(
        "CREATE TRIGGER refuse_actions BEFORE INSERT ON audit_row WHEN NEW.kind = 'action' "
        "BEGIN SELECT RAISE(ABORT, 'refused'); END"
    )
    raw.commit()
    with pytest.raises(Exception, match="refused"):
        machine.step(_ev(EventKind.STOP_RULE_FIRED, "HANDOFF", 3))
    assert _picture(machine, store) == before
    assert len(log) == rows_before
    assert not any(
        r.kind == AuditKind.TRANSITION and r.payload["to_state"] == "ADVISING_SLOW"
        for r in log.rows()
    )
    raw.execute("DROP TRIGGER refuse_actions")
    raw.commit()
    raw.close()
    assert machine.step(_ev(EventKind.STOP_RULE_FIRED, "HANDOFF", 3)).transitioned
    assert log.verify().ok


def test_failed_sitrep_audit_delivers_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = run_s05(tmp_path)  # template writer: draft, final, then updates
    final = next(p for p in out.service.packets if p.status.value == "final")
    assert final.inputs is not None
    rows = len(out.audit.rows())
    flaky = Flaky(out.audit)
    flaky.down = True
    monkeypatch.setattr(out.audit, "append_many", flaky.append_many)
    service = HandoffService(None, out.audit, run_id="s05-other", violations_path=out.violations)
    with pytest.raises(sqlite3.OperationalError):
        service.deliver(final.inputs, t_s=final.t_s)
    assert service.packets == [] and service.outbox == {"ui": [], "sms_sim": []}
    monkeypatch.undo()
    assert len(out.audit.rows()) == rows and out.audit.verify().ok
    # Once the log is back, the same issuance goes out once.
    packet = service.deliver(final.inputs, t_s=final.t_s)
    assert service.packets == [packet] and packet.markdown == final.markdown


# --- (c) idempotency key on every mutation -------------------------------------------------


def test_same_key_same_payload_is_one_row(log: AuditLog) -> None:
    probe = {"probe_id": "registry_lookup", "idempotency_key": "k-1", "cost_usd": 0.0}
    first = log.append(AuditKind.PROBE, probe, run_id=RUN, t_s=1.0, idempotency_key="k-1")
    again = log.append(AuditKind.PROBE, probe, run_id=RUN, t_s=9.0, idempotency_key="k-1")
    assert again.seq == first.seq and again.row_hash == first.row_hash
    assert len(log) == 1
    assert log.claimed(RUN, AuditKind.PROBE, "k-1") is not None
    # The claim is per run and per kind.
    log.append(AuditKind.PROBE, probe, run_id="other", t_s=1.0, idempotency_key="k-1")
    assert len(log) == 2 and log.verify().ok


def test_same_key_other_payload_is_refused_and_writes_nothing(log: AuditLog) -> None:
    probe = {"probe_id": "registry_lookup", "idempotency_key": "k-2", "cost_usd": 0.0}
    log.append(AuditKind.PROBE, probe, run_id=RUN, t_s=1.0, idempotency_key="k-2")
    head = log.head()
    changed = {**probe, "cost_usd": 3.0}
    batch = [
        AuditEntry(
            AuditKind.ACTION, {"action": "start_watch", "idempotency_key": "a-1"}, "", "a-1"
        ),
        AuditEntry(AuditKind.PROBE, changed, "", "k-2"),
    ]
    with pytest.raises(IdempotencyConflict):
        log.append_many(batch, run_id=RUN, t_s=2.0)
    assert len(log) == 1 and log.head() == head  # the action row before it is gone too
    assert log.claimed(RUN, AuditKind.ACTION, "a-1") is None
    assert log.verify().ok


def test_payload_hash_ignores_key_order() -> None:
    assert body_sha256({"a": 1, "b": [1.0, 2]}) == body_sha256({"b": [1.0, 2], "a": 1})
    assert body_sha256({"a": 1}) != body_sha256({"a": 2})


def test_redelivered_step_writes_no_second_row(log: AuditLog) -> None:
    machine, _ = _ladder(log)
    _investigating(machine)
    machine.step(_ev(EventKind.STOP_RULE_FIRED, "HANDOFF", 3))
    rows = log.rows()
    cp = machine.checkpoints[-1]
    actions = machine.ledger.records(CONTACT)[-1:]
    # The same step reaches the sink again: a fresh sink, as after a process restart.
    replay = AuditSink(log, RUN)
    replay(cp, [r.action for r in actions], cp.guard_decisions)
    replay(cp, [r.action for r in actions], cp.guard_decisions)
    assert [r.row_hash for r in log.rows()] == [r.row_hash for r in rows]
    assert log.verify().ok


def test_forked_transition_at_a_seq_is_refused_and_rolled_back(log: AuditLog) -> None:
    """Two machines claiming different checkpoints at the same seq of one contact: the
    second is refused by the audit and does not commit."""
    first, _ = _ladder(log)
    first.step(_ev(EventKind.CONTACT_DETECTED, None, 0))
    fork, store = _ladder(log)
    before = _picture(fork, store)
    with pytest.raises(IdempotencyConflict):
        fork.step(_ev(EventKind.CONTACT_DETECTED, None, 5))
    assert _picture(fork, store) == before
    assert len(log.rows(kind=AuditKind.TRANSITION)) == 1 and log.verify().ok


def test_redelivered_sitrep_issuance_is_one_packet_one_row(tmp_path: Path) -> None:
    out = run_s05(tmp_path)
    service = out.service
    final = next(p for p in service.packets if p.status.value == "final")
    assert final.inputs is not None
    packets, rows = len(service.packets), len(out.audit.rows())
    again = service.deliver(final.inputs, t_s=final.t_s)
    assert again is final
    assert len(service.packets) == packets and len(out.audit.rows()) == rows
    assert issuance_key(final.inputs) == issuance_key(final.inputs)
    assert issuance_key(final.inputs) != issuance_key(service.packets[0].inputs)  # type: ignore[arg-type]

    # A new service over the same log (a restart) re-issues the same SITREP: same row.
    restarted = HandoffService(None, out.audit, run_id="s05-seed11", violations_path=out.violations)
    packet = restarted.deliver(final.inputs, t_s=final.t_s)
    assert packet.audit_seq == final.audit_seq
    assert len(out.audit.rows()) == rows and out.audit.verify().ok


def test_redelivered_approval_is_one_row(tmp_path: Path) -> None:
    out = run_s05(tmp_path)
    (approval,) = out.audit.rows(kind=AuditKind.APPROVAL)
    cp = next(c for c in out.run.checkpoints if c.event.kind is EventKind.APPROVAL)
    AuditSink(out.audit, "s05-seed11", guard_inputs=out.service.guard_log)(
        cp, (), cp.guard_decisions
    )
    assert [r.seq for r in out.audit.rows(kind=AuditKind.APPROVAL)] == [approval.seq]
    assert out.audit.verify().ok


# --- (e) faithfulness: every cited id exists, or the template renders ----------------------


def _final_inputs(tmp_path: Path) -> Any:
    out = run_s05(tmp_path, writer_mode="auto")
    final = next(p for p in out.service.packets if p.status.value == "final")
    return final.inputs


def test_unknown_line_citing_a_row_that_does_not_exist_is_refused(tmp_path: Path) -> None:
    inputs = _final_inputs(tmp_path)
    body = sitrep_bodies.good_body(inputs)
    body["unknowns"].append({"text": "Who launched it", "evidence_ids": ["00000000000000aa"]})
    result = guard_sitrep(body, inputs)
    assert (result.accepted, result.violation) == (False, SitrepViolation.CITATION)


def test_missing_line_for_a_feature_that_has_a_row_is_refused(tmp_path: Path) -> None:
    inputs = _final_inputs(tmp_path)
    present = inputs.contact_rows[0].feature.value
    body = sitrep_bodies.good_body(inputs)
    body["unknowns"].append({"text": f"missing: {present}", "evidence_ids": []})
    result = guard_sitrep(body, inputs)
    assert (result.accepted, result.violation) == (False, SitrepViolation.CITATION)
