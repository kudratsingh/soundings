"""WO-040: one checkpoint per transition, a linked hash chain, restore, and idempotency.

CLAUDE.md section 4 (checkpoint per transition, idempotency key per action) and 2.4
(rebuilt machine equals the original; a re-delivered event never starts a second hold).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from soundings.clock import TickClock, Timer
from soundings.ladder.actions import ActionLedger
from soundings.ladder.checkpoint import (
    GENESIS_HASH,
    CheckpointError,
    JsonlCheckpointStore,
    LadderCheckpoint,
    MemoryCheckpointStore,
    verify_chain,
)
from soundings.ladder.machine import NO_ROW, LadderMachine
from soundings.ladder.types import (
    ActionKind,
    EventKind,
    GuardName,
    GuardVerdict,
    LadderEvent,
)
from soundings.schemas import LadderState, Rung
from tests.support.scripted_guards import ScriptedGuards
from tests.test_ladder import CID, TIMINGS
from tests.test_ladder_property import seeded_steps

Snapshot = tuple[LadderState, Rung, tuple[Timer, ...], tuple[str, ...], str]


def snapshot(m: LadderMachine) -> Snapshot:
    return (m.state, m.rung, m.timers.pending(), m.ledger.keys(m.contact_id), m.head_hash)


def ev(kind: EventKind, arg: str | None, tick: int) -> LadderEvent:
    return LadderEvent(kind=kind, arg=arg, tick=tick)


def s05_run(
    store: MemoryCheckpointStore | JsonlCheckpointStore,
) -> tuple[LadderMachine, ScriptedGuards, TickClock, list[Snapshot], list[LadderEvent]]:
    """Detected -> ... -> HOLDING -> AWAITING_APPROVAL -> backup -> HANDED_OFF -> watching.

    Includes a blocked guard and a no-row event, neither of which may checkpoint.
    """
    guards = ScriptedGuards(blocked=[GuardName.SLOW_PERMITTED, GuardName.FREE_PROBES_COMPLETE])
    clock = TickClock(tick_s=2.0)
    m = LadderMachine(CID, guards, clock, ActionLedger(), store, TIMINGS)
    snaps: list[Snapshot] = []
    fired: list[LadderEvent] = []

    def step(e: LadderEvent) -> None:
        clock.tick = e.tick
        r = m.step(e)
        if r.transitioned:
            snaps.append(snapshot(m))
            fired.append(e)

    step(ev(EventKind.CONTACT_DETECTED, None, 0))
    step(ev(EventKind.PROBE_RESULT, None, 1))  # blocked: free probes incomplete
    guards.set({GuardName.SLOW_PERMITTED: GuardVerdict.BLOCK})
    step(ev(EventKind.PROBE_RESULT, None, 2))
    step(ev(EventKind.APPROVAL, "granted", 3))  # no row in ASKING
    step(ev(EventKind.PROBE_RESULT, None, 4))
    step(ev(EventKind.STOP_RULE_FIRED, "HANDOFF", 5))
    assert m.state is LadderState.HOLDING
    for _ in range(1000):
        clock.advance()
        for r in m.advance():
            if r.transitioned:
                snaps.append(snapshot(m))
                fired.append(r.event)
        if m.state is LadderState.AWAITING_APPROVAL and m.backup_paged:
            break
    guards.set({GuardName.BACKUP_EXISTS: GuardVerdict.BLOCK})
    while m.state is LadderState.AWAITING_APPROVAL:
        clock.advance()
        for r in m.advance():
            snaps.append(snapshot(m))
            fired.append(r.event)
    step(ev(EventKind.HANDOFF_COMPLETE, None, clock.tick))
    for _ in range(320):
        clock.advance()
        for r in m.advance():
            snaps.append(snapshot(m))
            fired.append(r.event)
    assert m.state is LadderState.WATCH_AFTER_HANDOFF
    return m, guards, clock, snaps, fired


def test_one_checkpoint_per_transition_and_chain_links() -> None:
    store = MemoryCheckpointStore()
    m, _, _, snaps, fired = s05_run(store)
    cps = store.load()
    assert len(cps) == len(snaps) == len(fired) == len(m.checkpoints)
    assert [cp.event for cp in cps] == fired
    assert [cp.to_state for cp in cps][:8] == [
        LadderState.WATCHING,
        LadderState.ASKING,
        LadderState.INVESTIGATING,
        LadderState.HOLDING,
        LadderState.AWAITING_APPROVAL,
        LadderState.AWAITING_APPROVAL,
        LadderState.HANDED_OFF,
        LadderState.WATCH_AFTER_HANDOFF,
    ]
    assert cps[0].prev_hash == GENESIS_HASH
    for prev, cp in zip(cps, cps[1:], strict=False):
        assert cp.prev_hash == prev.hash and cp.seq == prev.seq + 1
    verify_chain(cps)
    assert cps[-1].hash == m.head_hash
    holding = cps[3]
    assert [d.guard for d in holding.guard_decisions] == [
        GuardName.SLOW_PERMITTED,
        GuardName.HOLD_PERMITTED,
    ]
    assert len(holding.action_keys) == 1


def test_tampering_breaks_the_chain() -> None:
    store = MemoryCheckpointStore()
    s05_run(store)
    cps = list(store.load())
    edited = cps[3].model_copy(update={"to_state": LadderState.HANDED_OFF})
    with pytest.raises(CheckpointError):
        verify_chain([*cps[:3], edited, *cps[4:]])
    with pytest.raises(CheckpointError):
        verify_chain([*cps[:2], *cps[3:]])
    # A forged checkpoint with a recomputed hash still does not replay through the table.
    forged = LadderCheckpoint.build(
        **{**cps[3].model_dump(exclude={"hash"}), "to_state": LadderState.HANDED_OFF}
    )
    with pytest.raises(CheckpointError):
        LadderMachine.restore(
            [*cps[:3], forged],
            guards=ScriptedGuards(),
            clock=TickClock(tick_s=2.0),
            ledger=ActionLedger(),
            store=MemoryCheckpointStore(),
            timings=TIMINGS,
        )


def test_restore_every_prefix_gives_same_state_rung_timers_and_keys() -> None:
    store = MemoryCheckpointStore()
    _, _, _, snaps, _ = s05_run(store)
    cps = store.load()
    for k in range(1, len(cps) + 1):
        r = LadderMachine.restore(
            cps[:k],
            guards=ScriptedGuards(blocked=list(GuardName)),  # never consulted
            clock=TickClock(tick_s=2.0, tick=cps[k - 1].tick),
            ledger=ActionLedger(),
            store=MemoryCheckpointStore(),
            timings=TIMINGS,
        )
        assert snapshot(r) == snaps[k - 1], f"prefix {k}"


def test_restore_from_jsonl(tmp_path: Path) -> None:
    store = JsonlCheckpointStore(tmp_path / "cp.jsonl")
    m, _, clock, _, _ = s05_run(store)
    loaded = JsonlCheckpointStore(tmp_path / "cp.jsonl").load()
    assert loaded == m.checkpoints
    r = LadderMachine.restore(
        loaded,
        guards=ScriptedGuards(),
        clock=clock,
        ledger=ActionLedger(),
        store=store,
        timings=TIMINGS,
    )
    assert snapshot(r) == snapshot(m)


def test_redelivered_last_event_after_restore_is_not_executed_twice() -> None:
    guards = ScriptedGuards(blocked=[GuardName.SLOW_PERMITTED])
    clock = TickClock(tick_s=2.0)
    ledger = ActionLedger()
    store = MemoryCheckpointStore()
    m = LadderMachine(CID, guards, clock, ledger, store, TIMINGS)
    events = [
        ev(EventKind.CONTACT_DETECTED, None, 0),
        ev(EventKind.PROBE_RESULT, None, 1),
        ev(EventKind.PROBE_RESULT, None, 2),
        ev(EventKind.STOP_RULE_FIRED, "HANDOFF", 3),
    ]
    for e in events:
        clock.tick = e.tick
        m.step(e)
    assert m.state is LadderState.HOLDING and ledger.active_holds() == 1
    records_before = ledger.records()

    # (a) Full restore, then the last event arrives again: no row, nothing executes.
    r = LadderMachine.restore(
        store.load(), guards=guards, clock=clock, ledger=ledger, store=store, timings=TIMINGS
    )
    assert ledger.hits == len(records_before)  # restore re-ran every key: all hits
    again = r.step(events[-1])
    assert again.reason == NO_ROW and r.state is LadderState.HOLDING
    assert ledger.records() == records_before

    # (b) The last checkpoint was lost after its action ran: restore the prefix and
    # re-deliver. The transition replays, its start_hold key is a ledger hit.
    hits = ledger.hits
    prefix = store.load()[:-1]
    r2 = LadderMachine.restore(
        prefix,
        guards=guards,
        clock=clock,
        ledger=ledger,
        store=MemoryCheckpointStore(),
        timings=TIMINGS,
    )
    assert r2.state is LadderState.INVESTIGATING
    replay = r2.step(events[-1])
    assert replay.transitioned and r2.state is LadderState.HOLDING
    assert [a.kind for a in replay.actions] == [ActionKind.START_HOLD]
    assert replay.records[0] == records_before[-1]  # the first record, returned again
    assert ledger.hits == hits + len(prefix_keys(prefix)) + 1
    assert ledger.records() == records_before
    assert ledger.active_holds() == 1  # one hold stays one hold
    assert replay.checkpoint == store.load()[-1]  # same content, same hash


def prefix_keys(cps: tuple[LadderCheckpoint, ...]) -> list[str]:
    return [k for cp in cps for k in cp.action_keys]


@settings(max_examples=200, deadline=None, derandomize=True)
@given(st.integers(min_value=0, max_value=2**32 - 1))
def test_restore_random_runs(seed: int) -> None:
    guards = ScriptedGuards()
    clock = TickClock(tick_s=2.0)
    store = MemoryCheckpointStore()
    m = LadderMachine(CID, guards, clock, ActionLedger(), store, TIMINGS)
    for what, payload, script in seeded_steps(seed):
        guards.set(script)
        if what == "event":
            kind, arg = payload  # type: ignore[misc]
            m.step(LadderEvent(kind=kind, arg=arg, tick=clock.tick))
        else:
            clock.advance(payload)  # type: ignore[arg-type]
            m.advance()
        clock.advance()
    if not store.load():
        return
    r = LadderMachine.restore(
        store.load(),
        guards=ScriptedGuards(),
        clock=clock,
        ledger=ActionLedger(),
        store=MemoryCheckpointStore(),
        timings=TIMINGS,
    )
    assert snapshot(r) == snapshot(m)
