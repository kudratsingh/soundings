"""WO-040 property test (SPEC-LADDER section 3): 2,000 random event sequences.

Random events (every kind and argument the vocabulary allows), random ``ScriptedGuards``
verdicts per step, and random clock advances that fire due timers. Proves the machine:
- never holds a state outside ``LadderState``;
- never enters ADVISING_SLOW or HOLDING without a ``permit`` decision in that checkpoint;
- after HANDED_OFF, takes only the WATCH_AFTER_HANDOFF transition (CLAUDE.md 2.1);
- never transitions again from RESOLVED_*, CLEARED_AT_ASK, HAZARD_NOTICE, LOST_VEHICLE or
  FAILED;
- never changes rung in WATCH_AFTER_HANDOFF;
and that every transition is a table row and writes exactly one checkpoint.
"""

from __future__ import annotations

import random

from hypothesis import HealthCheck, event, given, settings
from hypothesis import strategies as st

from soundings.clock import TickClock
from soundings.ladder.actions import ActionLedger
from soundings.ladder.checkpoint import MemoryCheckpointStore
from soundings.ladder.machine import LadderMachine, StepResult
from soundings.ladder.table import CLIMB_STATES, TRANSITIONS
from soundings.ladder.types import EVENT_ARGS, EventKind, GuardName, GuardVerdict, LadderEvent
from soundings.schemas import TERMINAL_STATES, LadderState, Rung
from tests.support.scripted_guards import ScriptedGuards
from tests.test_ladder import TIMINGS

ALL_EVENTS = tuple((k, a) for k in EventKind for a in (EVENT_ARGS.get(k) or (None,)))
FROZEN = TERMINAL_STATES  # RESOLVED_*, CLEARED_AT_ASK, HAZARD_NOTICE, LOST_VEHICLE, FAILED

# Bias toward the forward path so deep states (HOLDING, AWAITING_APPROVAL, watching) are
# reached often, while every other event still appears.
FORWARD = (
    (EventKind.CONTACT_DETECTED, None),
    (EventKind.PROBE_RESULT, None),
    (EventKind.STOP_RULE_FIRED, "HANDOFF"),
    (EventKind.STANDING_ORDER_CHECK, "hold_permitted"),
    (EventKind.APPROVAL, "timeout"),
    (EventKind.HANDOFF_COMPLETE, None),
    (EventKind.CONTACT_PERSISTED, "interval"),
)
EVENT = st.sampled_from(FORWARD * 12 + ALL_EVENTS)
VERDICT = st.sampled_from([GuardVerdict.PERMIT] * 3 + [GuardVerdict.BLOCK])
SCRIPT = st.dictionaries(st.sampled_from(list(GuardName)), VERDICT)
STEP = st.one_of(
    st.tuples(st.just("event"), EVENT, SCRIPT),
    st.tuples(st.just("event"), EVENT, SCRIPT),
    st.tuples(st.just("event"), EVENT, SCRIPT),
    st.tuples(st.just("advance"), st.integers(min_value=1, max_value=400), SCRIPT),
)


Step = tuple[str, object, dict[GuardName, GuardVerdict]]


def seeded_steps(seed: int, n: int = 60) -> list[Step]:
    """``n`` steps from ``random.Random(seed)``: deep states are reached far more often than
    with Hypothesis's small-value bias, while Hypothesis still varies and shrinks the tail."""
    rng = random.Random(seed)
    out: list[Step] = []
    for _ in range(n):
        script = {
            g: rng.choice((GuardVerdict.PERMIT,) * 3 + (GuardVerdict.BLOCK,)) for g in GuardName
        }
        if rng.random() < 0.15:
            out.append(("advance", rng.randint(1, 600), script))
        else:
            pick = rng.choice(FORWARD) if rng.random() < 0.7 else rng.choice(ALL_EVENTS)
            out.append(("event", pick, script))
    return out


def _check(result: StepResult, rung_before: Rung) -> Rung:
    """Check one step against the invariants; return the rung after it."""
    src, dst = result.from_state, result.to_state
    assert dst in set(LadderState)
    if not result.transitioned:
        assert dst is src and result.checkpoint is None
        return rung_before
    row = TRANSITIONS[result.row_index]  # type: ignore[index]
    assert row.matches(src, result.event.kind, result.event.arg) and row.dst is dst
    cp = result.checkpoint
    assert cp is not None and cp.from_state is src and cp.to_state is dst
    if dst in CLIMB_STATES:
        permits = [d for d in cp.guard_decisions if d.guard is row.guard and d.permits]
        assert permits, "climb without a permit decision"
        assert permits[-1].granted_scope is not None
    if src is LadderState.HANDED_OFF:
        assert dst is LadderState.WATCH_AFTER_HANDOFF
    assert src not in FROZEN
    if src is LadderState.WATCH_AFTER_HANDOFF:
        assert cp.rung is rung_before
    return cp.rung


@settings(
    max_examples=2000,
    deadline=None,
    derandomize=True,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)
@given(st.integers(min_value=0, max_value=2**32 - 1), st.lists(STEP, max_size=20))
def test_random_event_sequences(seed: int, tail: list[Step]) -> None:
    steps = seeded_steps(seed) + tail
    guards = ScriptedGuards()
    clock = TickClock(tick_s=2.0)
    store = MemoryCheckpointStore()
    m = LadderMachine("c-0001", guards, clock, ActionLedger(), store, TIMINGS)
    transitions = 0
    rung = m.rung
    for what, payload, script in steps:
        guards.set(script)
        if what == "event":
            kind, arg = payload  # type: ignore[misc]
            results = [m.step(LadderEvent(kind=kind, arg=arg, tick=clock.tick))]
        else:
            clock.advance(payload)  # type: ignore[arg-type]
            results = m.advance()
        for result in results:
            rung = _check(result, rung)
            transitions += result.transitioned
        assert rung is m.rung
        clock.advance()
    assert len(store.load()) == transitions == len(m.checkpoints)
    for state in sorted({cp.to_state.value for cp in m.checkpoints}):
        event(f"visited {state}")  # coverage shown by --hypothesis-show-statistics
