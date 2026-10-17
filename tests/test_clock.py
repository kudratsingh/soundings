"""WO-040: the one Clock interface, deterministic timers, and no stray wall-clock reads."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from soundings.clock import Clock, TickClock, Timer, TimerSet, WallClock

REPO = Path(__file__).resolve().parent.parent
WALL_CALLS = {("time", "time"), ("time", "monotonic"), ("datetime", "now")}


def test_tick_clock_is_tick_times_tick_s() -> None:
    for tick_s in (0.1, 1.0, 2.0, 2.5):
        clock = TickClock(tick_s=tick_s)
        for _ in range(50):
            assert clock.now_s() == clock.tick * tick_s
            clock.advance()
    clock = TickClock(tick_s=2.0, tick=10)
    assert clock.now_s() == 20.0
    assert clock.advance(5) == 15
    assert clock.now_s() == 30.0
    with pytest.raises(ValueError):
        clock.advance(-1)
    with pytest.raises(ValueError):
        TickClock(tick_s=0.0)


def test_clocks_satisfy_protocol() -> None:
    assert isinstance(TickClock(tick_s=1.0), Clock)
    wall = WallClock()
    assert isinstance(wall, Clock)
    first = wall.now_s()
    assert first >= 0.0
    assert wall.now_s() >= first


def test_timer_order_is_deterministic_under_ties() -> None:
    timers = [
        Timer("sitrep_update", "c-0002", 10.0),
        Timer("approval_timeout", "c-0002", 10.0),
        Timer("approval_timeout", "c-0001", 10.0),
        Timer("hold_limit", "c-0001", 5.0),
        Timer("hold_limit", "c-0003", 30.0),
    ]
    expected = [
        Timer("hold_limit", "c-0001", 5.0),
        Timer("approval_timeout", "c-0001", 10.0),
        Timer("approval_timeout", "c-0002", 10.0),
        Timer("sitrep_update", "c-0002", 10.0),
    ]
    clock = TickClock(tick_s=1.0, tick=10)
    orders = []
    for perm in (timers, list(reversed(timers)), timers[2:] + timers[:2]):
        ts = TimerSet()
        for t in perm:
            ts.arm(t)
        orders.append(ts.due(clock))
        assert len(ts.pending()) == 5
    assert orders[0] == orders[1] == orders[2] == expected


def test_timer_set_replace_cancel_pop() -> None:
    ts = TimerSet()
    ts.arm(Timer("hold_limit", "c-0001", 5.0))
    ts.arm(Timer("hold_limit", "c-0001", 7.0))  # replaces
    ts.arm(Timer("sitrep_update", "c-0001", 9.0))
    ts.arm(Timer("sitrep_update", "c-0002", 9.0))
    assert len(ts) == 3
    assert ts.due(TickClock(tick_s=1.0, tick=6)) == []
    assert ts.cancel("hold_limit", "c-0001")
    assert not ts.cancel("hold_limit", "c-0001")
    ts.cancel_contact("c-0001")
    assert ts.pending() == (Timer("sitrep_update", "c-0002", 9.0),)
    ts.pop(Timer("sitrep_update", "c-0002", 9.0))
    assert len(ts) == 0


def _wall_calls(path: Path) -> list[tuple[str, int]]:
    """(enclosing class or '<module>', line) for every wall-clock attribute use in a file."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    hits: list[tuple[str, int]] = []

    def visit(node: ast.AST, owner: str) -> None:
        for child in ast.iter_child_nodes(node):
            child_owner = child.name if isinstance(child, ast.ClassDef) else owner
            if isinstance(child, ast.Attribute) and isinstance(child.value, ast.Name):
                if (child.value.id, child.attr) in WALL_CALLS:
                    hits.append((owner, child.lineno))
            if isinstance(child, ast.Attribute) and child.attr == "now":
                if isinstance(child.value, ast.Attribute) and child.value.attr == "datetime":
                    hits.append((owner, child.lineno))
            if isinstance(child, ast.ImportFrom) and child.module in ("time", "datetime"):
                names = {a.name for a in child.names}
                if names & {"time", "monotonic", "datetime"}:
                    hits.append((owner, child.lineno))
            visit(child, child_owner)

    visit(tree, "<module>")
    return hits


def test_wall_clock_reads_only_in_wallclock() -> None:
    clock_hits = _wall_calls(REPO / "soundings" / "clock.py")
    assert clock_hits, "WallClock must read the monotonic clock"
    assert {owner for owner, _ in clock_hits} == {"WallClock"}
    ladder_files = sorted((REPO / "soundings" / "ladder").glob("*.py"))
    assert ladder_files
    for path in ladder_files:
        assert _wall_calls(path) == [], f"wall-clock read in {path.name}"
