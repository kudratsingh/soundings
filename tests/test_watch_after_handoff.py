"""WO-043 part A: watch after the handoff (BUILD-PLAN Phase 4).

- After handoff, a contact that leaves the zone becomes ``RESOLVED_DEPARTED``.
- One that persists re-issues an updated SITREP at the configured interval without climbing
  the ladder.

The streams are the synthetic S05 shape (``soundings.ladder.synthetic``); part B adds the
runner-v3 versions (S06 at seed 13, X01). Thresholds come from ``config/ladder.yaml``.
"""

from __future__ import annotations

from dataclasses import replace
from functools import cache
from pathlib import Path

import pytest
import yaml
from hypothesis import given, settings
from hypothesis import strategies as st

from soundings.clock import TickClock
from soundings.ladder.actions import ActionLedger
from soundings.ladder.checkpoint import MemoryCheckpointStore
from soundings.ladder.driver import LadderDriver, LadderRun, transition_log
from soundings.ladder.guard_port import NoStandingOrdersGuards
from soundings.ladder.machine import LadderMachine
from soundings.ladder.synthetic import (
    INSIDE,
    OUTSIDE,
    S05_CONTACT,
    S05_HOUR,
    S05_TICK_S,
    s05_feed,
)
from soundings.ladder.types import ActionKind, EventKind, LadderEvent, LadderTimings
from soundings.ladder.watch import DepartureDetector, departed_after_s_from_config
from soundings.schemas import LadderState, Rung, TrackPoint, load_harbor
from soundings.schemas.config import LADDER_PATH
from tests.support.guard_streams import signed_orders_copy

S = LadderState
A = ActionKind
WAH = yaml.safe_load(LADDER_PATH.read_text(encoding="utf-8"))["watch_after_handoff"]
INTERVAL_S = float(WAH["sitrep_update_interval_s"])
DEPARTED_S = float(WAH["departed_after_s_outside_zone"])
WATCH_ACTIONS = {A.DELIVER_SITREP, A.START_WATCH, A.UPDATE_SITREP, A.CLOSE_WATCH}
HARBOR = load_harbor()


@pytest.fixture(scope="module")
def orders(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return signed_orders_copy(tmp_path_factory.mktemp("orders"))


def drive(orders_path: Path | None, **feed_kw: object) -> LadderRun:
    detector = DepartureDetector.from_config(HARBOR, S05_TICK_S)
    driver = LadderDriver.from_config(
        tick_s=S05_TICK_S, hour_of_day=S05_HOUR, orders_path=orders_path, departure=detector
    )
    return driver.run([s05_feed(**feed_kw)])[S05_CONTACT]  # type: ignore[arg-type]


def after_handoff(run: LadderRun) -> int:
    return next(cp.tick for cp in run.checkpoints if cp.to_state is S.HANDED_OFF)


def point(tick: int, where: tuple[float, float]) -> TrackPoint:
    lat, lon = where
    return TrackPoint(
        tick=tick, lat=lat, lon=lon, depth_m=6.0, sigma_m=10.0, sensor_id="hyd_1", confidence=0.9
    )


# --- BUILD-PLAN Phase 4 ------------------------------------------------------------------


@pytest.mark.parametrize("signed", [True, False])
def test_contact_that_leaves_after_handoff_becomes_resolved_departed(
    orders: Path, signed: bool
) -> None:
    """After handoff, a contact that leaves the zone becomes RESOLVED_DEPARTED."""
    depart_at = 650
    run = drive(orders if signed else None, depart_at_tick=depart_at)
    assert run.handling_outcome is S.HANDED_OFF
    assert run.final_state is S.RESOLVED_DEPARTED
    last = run.checkpoints[-1]
    assert (
        last.from_state is S.WATCH_AFTER_HANDOFF and last.event.kind is EventKind.CONTACT_DEPARTED
    )
    assert last.tick == depart_at + DEPARTED_S / S05_TICK_S
    assert A.CLOSE_WATCH in [a.kind for a in run.actions if a.tick == last.tick]
    assert run.watch is not None and run.watch.departed_at_s == last.tick * S05_TICK_S
    assert run.watch.last_known is not None and run.watch.last_known.zone_id is None


@pytest.mark.parametrize("signed", [True, False])
def test_persisting_contact_gets_updates_at_the_interval_without_climbing(
    orders: Path, signed: bool
) -> None:
    """One that persists re-issues an updated SITREP at the configured interval, no climb."""
    run = drive(orders if signed else None)
    handed = after_handoff(run)
    updates = [s for s in run.sitreps if s.kind == "update"]
    assert len(updates) >= 2
    step = INTERVAL_S / S05_TICK_S
    assert [u.tick for u in updates] == [handed + step * (i + 1) for i in range(len(updates))]
    post = [cp for cp in run.checkpoints if cp.tick >= handed and cp.seq > 0]
    post = post[next(i for i, cp in enumerate(post) if cp.to_state is S.HANDED_OFF) :]
    assert {cp.rung for cp in post} == {Rung.HAND_TO_PERSON}
    assert {cp.to_state for cp in post} <= {S.HANDED_OFF, S.WATCH_AFTER_HANDOFF}
    assert all(u.inputs.rung is Rung.HAND_TO_PERSON for u in updates)
    # Each update cites at least one evidence row newer than the previous SITREP.
    previous = [s for s in run.sitreps if s.kind != "update"][-1]
    for u in updates:
        assert u.inputs.cited_row_ids
        assert u.inputs.kind == "update"
        assert previous.tick < u.tick
        previous = u


def test_watch_only_observes(orders: Path) -> None:
    """CLAUDE.md 2.1: after HANDED_OFF only watching happens (plus releasing a granted hold)."""
    for kw in ({}, {"depart_at_tick": 650}):
        for path in (orders, None):
            run = drive(path, **kw)
            handed = after_handoff(run)
            later = [a for a in run.actions if a.tick > handed]
            extra = {a.kind for a in later} - WATCH_ACTIONS
            # The one non-watch action: lifting the person-approved hold when the contact
            # departs (DECISIONS: a granted hold is released on departure).
            assert extra <= {A.RELEASE_HOLD}
            if extra:
                assert run.final_state is S.RESOLVED_DEPARTED
            post = [cp for cp in run.checkpoints if cp.tick > handed]
            assert all(cp.guard_decisions == () for cp in post)


# --- the detector ------------------------------------------------------------------------


def test_detector_units(orders: Path) -> None:
    # Thresholds come from config/ladder.yaml.
    assert departed_after_s_from_config() == DEPARTED_S
    det = DepartureDetector.from_config(HARBOR, S05_TICK_S)
    assert det.departed_after_s == DEPARTED_S
    n = int(DEPARTED_S / S05_TICK_S)

    # Outside for departed_after_s - tick_s: no event; one more tick: the event.
    seen = [det.observe("c-0001", t, point(t, OUTSIDE)) for t in range(n)]
    assert not any(seen)
    assert det.observe("c-0001", n, point(n, OUTSIDE))

    # An excursion back inside resets the count.
    det = DepartureDetector(HARBOR, DEPARTED_S, S05_TICK_S)
    for t in range(n - 1):
        assert not det.observe("c-0002", t, point(t, OUTSIDE))
    assert not det.observe("c-0002", n - 1, point(n - 1, INSIDE))
    for t in range(n, 2 * n):
        assert not det.observe("c-0002", t, point(t, OUTSIDE))
    assert det.observe("c-0002", 2 * n, point(2 * n, OUTSIDE))

    # A lost track whose last point is inside never departs.
    det = DepartureDetector(HARBOR, DEPARTED_S, S05_TICK_S)
    assert not det.observe("c-0003", 0, point(0, INSIDE))
    assert not any(det.observe("c-0003", t, None) for t in range(1, 10 * n))
    assert det.track_lost_at("c-0003") == 1
    lk = det.last_known("c-0003")
    assert lk is not None and lk.zone_id is not None
    # A lost track whose last point is outside keeps counting from that point.
    assert not det.observe("c-0004", 0, point(0, OUTSIDE))
    assert det.observe("c-0004", n, None)

    # A contact departing while HOLDING gives RESOLVED_DEPARTED and a release_hold action.
    run = drive(orders, depart_at_tick=300)
    assert S.HOLDING in run.visited and S.ADVISING_SLOW in run.visited
    last = run.checkpoints[-1]
    assert (last.from_state, last.to_state) == (S.HOLDING, S.RESOLVED_DEPARTED)
    assert last.tick == 300 + n
    assert [a.kind for a in run.actions if a.tick == last.tick] == [A.RELEASE_HOLD]
    assert run.handling_outcome is S.RESOLVED_DEPARTED


def test_track_lost_update_names_last_known_inside(orders: Path) -> None:
    feed = s05_feed()
    lost_from = 700
    feed = replace(feed, points=tuple(p for p in feed.points if p.tick < lost_from))
    det = DepartureDetector.from_config(HARBOR, S05_TICK_S)
    driver = LadderDriver.from_config(
        tick_s=S05_TICK_S, hour_of_day=S05_HOUR, orders_path=None, departure=det
    )
    run = driver.run([feed])[S05_CONTACT]
    assert run.final_state is S.WATCH_AFTER_HANDOFF
    late = [s for s in run.sitreps if s.kind == "update" and s.tick > lost_from]
    assert late
    assert all(s.inputs.track_lost_at_tick == lost_from for s in late)
    assert f"track lost at tick {lost_from}, last known inside zone" in late[0].text


def test_transition_log_identical_across_two_runs(orders: Path) -> None:
    for kw in ({}, {"depart_at_tick": 650}):
        a = transition_log({S05_CONTACT: drive(orders, **kw)})
        b = transition_log({S05_CONTACT: drive(orders, **kw)})
        assert a == b


# --- hypothesis: random persist and depart sequences never change the rung ---------------


@cache
def _unsigned_run() -> LadderRun:
    return drive(None)


def _watching_machine() -> tuple[LadderMachine, TickClock]:
    run = _unsigned_run()
    clock = TickClock(tick_s=S05_TICK_S)
    handed = after_handoff(run)
    cps = [cp for cp in run.checkpoints if cp.tick <= handed]
    m = LadderMachine.restore(
        cps,
        guards=NoStandingOrdersGuards(),
        clock=clock,
        ledger=ActionLedger(),
        store=MemoryCheckpointStore(),
        timings=LadderTimings.from_config(),
        strict_holds=True,
    )
    clock.tick = handed
    assert m.state is S.WATCH_AFTER_HANDOFF
    return m, clock


@settings(max_examples=60, deadline=None)
@given(st.lists(st.sampled_from(["persist", "depart", "wait"]), min_size=1, max_size=25))
def test_random_watch_sequences_never_change_rung(seq: list[str]) -> None:
    m, clock = _watching_machine()
    before = len(m.ledger.records())
    for what in seq:
        clock.advance(10)
        if what == "persist":
            m.step(LadderEvent(kind=EventKind.CONTACT_PERSISTED, arg="interval", tick=clock.tick))
        elif what == "depart":
            m.step(LadderEvent(kind=EventKind.CONTACT_DEPARTED, tick=clock.tick))
        else:
            m.advance()
        assert m.rung is Rung.HAND_TO_PERSON
        assert m.state in (S.WATCH_AFTER_HANDOFF, S.RESOLVED_DEPARTED)
    later = [r.action.kind for r in m.ledger.records()][before:]
    assert set(later) <= WATCH_ACTIONS
