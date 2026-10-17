"""WO-040 integration: machine + NoStandingOrdersGuards + TickClock + real config + JSONL.

Scripted streams shaped like each Phase 2 and 3 outcome reach their terminal (or
watching) state, the unsigned handoff never climbs to slow or hold and records zero
interruptions, and a restore from the JSONL file reproduces every final state.
Determinism (CLAUDE.md 2.4): identical checkpoint hashes across PYTHONHASHSEED values and
equal chain heads across two in-process runs.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from soundings.clock import TickClock
from soundings.ladder.actions import ActionLedger
from soundings.ladder.checkpoint import JsonlCheckpointStore, MemoryCheckpointStore
from soundings.ladder.guard_port import NoStandingOrdersGuards
from soundings.ladder.machine import LadderMachine
from soundings.ladder.types import ActionKind, LadderTimings
from soundings.schemas import LadderState, Rung
from tests.support.ladder_streams import TICK_S, run_streams, streams

REPO = Path(__file__).resolve().parent.parent


def test_timings_from_real_config() -> None:
    t = LadderTimings.from_config()
    assert t.sitrep_update_interval_s == 300.0
    assert t.departed_after_s_outside_zone == 120.0
    assert t.primary_timeout_s == 120.0
    assert t.backup_timeout_s == 180.0


def test_streams_reach_every_outcome_and_restore_from_jsonl(tmp_path: Path) -> None:
    path = tmp_path / "ladder.jsonl"
    store = JsonlCheckpointStore(path)
    ledger = ActionLedger()
    timings = LadderTimings.from_config()
    machines = run_streams(store, ledger, timings)
    expected = {cid: final for cid, (final, _) in streams().items()}
    assert {cid: m.state for cid, m in machines.items()} == expected
    assert set(expected.values()) == {
        LadderState.CLEARED_AT_ASK,
        LadderState.LOST_VEHICLE,
        LadderState.RESOLVED_BENIGN,
        LadderState.HAZARD_NOTICE,
        LadderState.WATCH_AFTER_HANDOFF,
    }

    # The unsigned handoff: straight to HANDED_OFF, then watching; no slow, no hold.
    handoff = machines["c-0005"]
    visited = [cp.to_state for cp in handoff.checkpoints]
    assert LadderState.HANDED_OFF in visited
    assert visited.index(LadderState.HANDED_OFF) + 1 == visited.index(
        LadderState.WATCH_AFTER_HANDOFF
    )
    assert LadderState.ADVISING_SLOW not in visited
    assert LadderState.HOLDING not in visited
    assert handoff.rung is Rung.HAND_TO_PERSON
    for cid in machines:
        assert ledger.interruptions(cid) == 0
    assert ledger.active_holds() == 0
    kinds = [r.action.kind for r in ledger.records("c-0005")]
    assert kinds.count(ActionKind.DELIVER_SITREP) == 1
    assert kinds.count(ActionKind.START_WATCH) == 1
    assert kinds.count(ActionKind.UPDATE_SITREP) == 2  # every 300 s of run clock

    # Restore every machine from the JSONL file alone, with a fresh ledger.
    reloaded = JsonlCheckpointStore(path)
    fresh = ActionLedger()
    for cid, m in machines.items():
        cps = reloaded.load(cid)
        assert cps == m.checkpoints
        clock = TickClock(tick_s=TICK_S, tick=cps[-1].tick)
        r = LadderMachine.restore(
            cps,
            guards=NoStandingOrdersGuards(),
            clock=clock,
            ledger=fresh,
            store=reloaded,
            timings=timings,
        )
        assert r.state is m.state
        assert r.rung is m.rung
        assert r.head_hash == m.head_hash
        assert r.timers.pending() == m.timers.pending()
        assert fresh.keys(cid) == ledger.keys(cid)


def test_pythonhashseed_inprocess_chain_heads_equal() -> None:
    heads = []
    for _ in range(2):
        machines = run_streams(MemoryCheckpointStore())
        heads.append({cid: m.head_hash for cid, m in machines.items()})
    assert heads[0] == heads[1]


def test_pythonhashseed_subprocess_hashes_identical() -> None:
    outputs = []
    for seed in ("0", "1"):
        env = {**os.environ, "PYTHONHASHSEED": seed, "MODEL_MODE": "replay"}
        proc = subprocess.run(
            [sys.executable, "-c", "from tests.support.ladder_streams import main; main()"],
            cwd=REPO,
            env=env,
            capture_output=True,
            text=True,
            check=True,
        )
        outputs.append(proc.stdout)
    assert outputs[0]
    assert outputs[0] == outputs[1]
