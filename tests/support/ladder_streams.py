"""Scripted ladder event streams shaped like each Phase 2 and Phase 3 outcome (test only).

Each stream carries real ``EvidenceRow`` payloads; the unsigned-orders guard set
(``NoStandingOrdersGuards``) decides. ``python -c "from tests.support.ladder_streams import
main; main()"`` prints every checkpoint hash, which the PYTHONHASHSEED test compares
across interpreter runs.
"""

from __future__ import annotations

from soundings.clock import TickClock
from soundings.ladder.actions import ActionLedger
from soundings.ladder.checkpoint import CheckpointStore, MemoryCheckpointStore
from soundings.ladder.guard_port import NoStandingOrdersGuards
from soundings.ladder.machine import LadderMachine
from soundings.ladder.types import EventKind, EventPayload, LadderEvent, LadderTimings
from soundings.schemas import (
    EvidenceKind,
    EvidenceRow,
    EvidenceValue,
    FeatureName,
    LadderState,
    ProbeId,
    Unit,
)

TICK_S = 2.0

F = FeatureName
P = ProbeId


def row(
    cid: str,
    t: int,
    feature: FeatureName,
    value: EvidenceValue,
    unit: Unit,
    probe: ProbeId | None,
    confidence: float = 0.9,
) -> EvidenceRow:
    source = f"probe:{probe.value}" if probe is not None else "sim.detector:hyd_1"
    return EvidenceRow(
        contact_id=cid,
        t=t,
        feature=feature,
        value=value,
        unit=unit,
        source=source,
        confidence=confidence,
        kind=EvidenceKind.FACT,
        probe_id=probe,
    )


def ev(
    kind: EventKind,
    tick: int,
    arg: str | None = None,
    rows: tuple[EvidenceRow, ...] = (),
    **payload: object,
) -> LadderEvent:
    return LadderEvent(
        kind=kind,
        arg=arg,
        tick=tick,
        payload=EventPayload.model_validate({"rows": rows, **payload}),
    )


def _opening(cid: str, zone: str = "inner_harbor_west") -> list[LadderEvent]:
    return [
        ev(
            EventKind.CONTACT_DETECTED,
            0,
            rows=(row(cid, 0, F.ZONE_ID, zone, Unit.ID, None),),
        ),
        ev(
            EventKind.PROBE_RESULT,
            1,
            rows=(
                row(cid, 1, F.SPEED_OVER_GROUND, 1.2, Unit.M_PER_S, P.KINEMATICS_PROFILE),
                row(cid, 1, F.DRIFT_CONSISTENCY, 0.2, Unit.RATIO, P.DRIFT_CHECK),
            ),
        ),
    ]


def _not_cleared(cid: str, tick: int) -> LadderEvent:
    return ev(
        EventKind.PROBE_RESULT,
        tick,
        rows=(
            row(cid, tick, F.REGISTRY_MATCH, "none", Unit.CATEGORY, P.REGISTRY_LOOKUP, 1.0),
            row(cid, tick, F.HAIL_REPLY, "none", Unit.CATEGORY, P.ACOUSTIC_HAIL),
        ),
    )


def streams() -> dict[str, tuple[LadderState, list[LadderEvent]]]:
    """contact id -> (expected final state, events)."""
    out: dict[str, tuple[LadderState, list[LadderEvent]]] = {}

    cid = "c-0001"  # S01/S03 shape: filed, on plan, valid hail
    out[cid] = (
        LadderState.CLEARED_AT_ASK,
        _opening(cid)
        + [
            ev(
                EventKind.STOP_RULE_FIRED,
                12,
                "CLEARED_AT_ASK",
                rows=(
                    row(cid, 2, F.REGISTRY_MATCH, "exact", Unit.CATEGORY, P.REGISTRY_LOOKUP, 1.0),
                    row(cid, 12, F.HAIL_REPLY, "valid", Unit.CATEGORY, P.ACOUSTIC_HAIL, 1.0),
                ),
            )
        ],
    )

    cid = "c-0002"  # S02 shape: filed vehicle gone silent
    out[cid] = (
        LadderState.LOST_VEHICLE,
        _opening(cid)
        + [
            ev(
                EventKind.STOP_RULE_FIRED,
                12,
                "LOST_VEHICLE",
                rows=(
                    row(cid, 2, F.REGISTRY_MATCH, "exact", Unit.CATEGORY, P.REGISTRY_LOOKUP, 1.0),
                    row(cid, 12, F.HAIL_REPLY, "timeout", Unit.CATEGORY, P.ACOUSTIC_HAIL),
                ),
            )
        ],
    )

    cid = "c-0003"  # S07 shape: biologic, resolved benign
    out[cid] = (
        LadderState.RESOLVED_BENIGN,
        _opening(cid)
        + [
            _not_cleared(cid, 12),
            ev(EventKind.POSTERIOR_UPDATED, 14, benign_mass=0.7, threat_mass=0.05),
            ev(
                EventKind.STOP_RULE_FIRED,
                20,
                "RESOLVED_BENIGN",
                rows=(row(cid, 20, F.BREATHING_OR_CLICK_PRESENT, True, Unit.BOOL, None),),
                benign_mass=0.91,
                threat_mass=0.02,
            ),
        ],
    )

    cid = "c-0004"  # S06 shape: debris or drift
    out[cid] = (
        LadderState.HAZARD_NOTICE,
        _opening(cid, "main_channel")
        + [
            _not_cleared(cid, 12),
            ev(
                EventKind.STOP_RULE_FIRED,
                20,
                "HAZARD_NOTICE",
                rows=(row(cid, 20, F.DRIFT_CONSISTENCY, 0.95, Unit.RATIO, P.DRIFT_CHECK),),
                benign_mass=0.88,
                threat_mass=0.03,
            ),
        ],
    )

    cid = "c-0005"  # S05 shape: closing on a critical asset; unsigned orders
    out[cid] = (
        LadderState.WATCH_AFTER_HANDOFF,
        _opening(cid, "fuel_berths")
        + [
            _not_cleared(cid, 12),
            ev(
                EventKind.STOP_RULE_FIRED,
                30,
                "HANDOFF",
                rows=(
                    row(
                        cid,
                        30,
                        F.TIME_TO_NEAREST_CRITICAL_ASSET,
                        250.0,
                        Unit.S,
                        P.KINEMATICS_PROFILE,
                    ),
                    row(cid, 30, F.ZONE_CRITICALITY, 0.9, Unit.RATIO, None),
                    row(cid, 30, F.NEAREST_ASSET_ID, "fuel_berth_1", Unit.ID, None),
                ),
                threat_mass=0.7,
                benign_mass=0.1,
                basin="inner_harbor_west",
            ),
            ev(EventKind.HANDOFF_COMPLETE, 30),
        ],
    )
    return out


WATCH_TAIL_TICKS = 320
"""Ticks the handoff stream keeps watching after handoff (>= 2 SITREP update intervals)."""


def run_streams(
    store: CheckpointStore,
    ledger: ActionLedger | None = None,
    timings: LadderTimings | None = None,
) -> dict[str, LadderMachine]:
    """Run every stream through its own machine; the handoff stream then watches a while."""
    timings = timings if timings is not None else LadderTimings.from_config()
    ledger = ledger if ledger is not None else ActionLedger()
    machines: dict[str, LadderMachine] = {}
    for cid, (_, events) in streams().items():
        clock = TickClock(tick_s=TICK_S)
        m = LadderMachine(cid, NoStandingOrdersGuards(), clock, ledger, store, timings)
        for event in events:
            clock.tick = event.tick
            m.advance()
            m.step(event)
        if m.state is LadderState.WATCH_AFTER_HANDOFF:
            for _ in range(WATCH_TAIL_TICKS):
                clock.advance()
                m.advance()
        machines[cid] = m
    return machines


def main() -> None:
    store = MemoryCheckpointStore()
    run_streams(store)
    for cp in store.load():
        print(cp.contact_id, cp.seq, cp.to_state.value, cp.hash)


if __name__ == "__main__":
    main()
