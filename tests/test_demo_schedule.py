"""WO-062 Part A: the step scheduler compresses marks deterministically."""

from __future__ import annotations

import pytest

from soundings.demo.schedule import DEFAULT_TOTAL_S, Pace, build_schedule

MARKS = (0, 30, 60, 120, 165, 210, 240)


def test_video_fits_total() -> None:
    s = build_schedule(MARKS, pace=Pace.VIDEO)
    assert s.total_s <= DEFAULT_TOTAL_S
    assert s.total_s == pytest.approx(170.0)
    assert s.steps[-1].at_s < s.total_s
    assert [st.mark_s for st in s.steps] == list(MARKS)


def test_video_configurable_total_and_tail() -> None:
    s = build_schedule(MARKS, pace=Pace.VIDEO, total_s=60.0, tail_s=0.0)
    assert s.total_s == pytest.approx(60.0)
    assert s.steps[-1].at_s == pytest.approx(60.0)
    assert s.scale == pytest.approx(0.25)


def test_video_never_stretches_a_short_script() -> None:
    s = build_schedule((0, 10, 20), pace=Pace.VIDEO, total_s=170.0, tail_s=5.0)
    assert s.scale == 1.0
    assert [st.at_s for st in s.steps] == [0.0, 10.0, 20.0]
    assert s.total_s == 25.0


def test_realtime_keeps_marks_and_speed_divides() -> None:
    s = build_schedule(MARKS, pace=Pace.REALTIME, tail_s=10.0)
    assert [st.at_s for st in s.steps] == [float(m) for m in MARKS]
    assert s.total_s == 250.0
    fast2 = build_schedule(MARKS, pace=Pace.REALTIME, tail_s=10.0, speed=2.0)
    assert [st.at_s for st in fast2.steps] == [m / 2 for m in MARKS]
    assert fast2.total_s == 125.0


def test_fast_never_waits() -> None:
    s = build_schedule(MARKS, pace=Pace.FAST)
    assert all(st.at_s == 0.0 for st in s.steps)
    assert s.total_s == 0.0


@pytest.mark.parametrize("pace", list(Pace))
def test_ordering_preserved(pace: Pace) -> None:
    s = build_schedule(MARKS, pace=pace, speed=3.0)
    offsets = [st.at_s for st in s.steps]
    assert offsets == sorted(offsets)
    assert [st.index for st in s.steps] == list(range(len(MARKS)))
    if pace is not Pace.FAST:
        assert all(b > a for a, b in zip(offsets, offsets[1:], strict=False))


def test_deterministic_and_rounded() -> None:
    a = build_schedule(MARKS, pace=Pace.VIDEO, total_s=100.0, tail_s=7.0, speed=1.3)
    b = build_schedule(MARKS, pace=Pace.VIDEO, total_s=100.0, tail_s=7.0, speed=1.3)
    assert a == b
    for st in a.steps:
        assert st.at_s == round(st.at_s, 6)


@pytest.mark.parametrize(
    ("marks", "kw"),
    [
        ((), {}),
        ((0, 0), {}),
        ((10, 5), {}),
        ((-1, 5), {}),
        ((0, 5), {"speed": 0.0}),
        ((0, 5), {"total_s": 0.0}),
        ((0, 5), {"tail_s": -1.0}),
    ],
)
def test_rejects_bad_input(marks: tuple[int, ...], kw: dict[str, float]) -> None:
    with pytest.raises(ValueError):
        build_schedule(marks, **kw)  # type: ignore[arg-type]
