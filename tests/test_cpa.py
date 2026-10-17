"""WO-083: the ARPA CPA/TCPA formulas against hand-computed geometry (CLAUDE.md 2.4)."""

from __future__ import annotations

import math

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from soundings.advisories.cpa import CpaResult, cpa_tcpa, velocity_from_course

# (name, own_pos, own_vel, other_pos, other_vel, expected CPA m, expected TCPA s).
# Hand-computed: r = other - own, v = other_vel - own_vel, TCPA = -(r.v)/|v|^2,
# CPA = |r + v TCPA|. Crossing: r = (-700, 500), v = (6, -5), r.v = -6700, |v|^2 = 61.
KNOWN = [
    ("head_on", (0.0, 0.0), (5.0, 0.0), (1000.0, 50.0), (-5.0, 0.0), 50.0, 100.0),
    ("crossing", (0.0, -500.0), (0.0, 5.0), (-700.0, 0.0), (6.0, 0.0), 64.018, 109.836),
    ("wide_crossing", (0.0, -500.0), (0.0, 5.0), (-900.0, 0.0), (6.0, 0.0), 192.055, 129.508),
    ("diverging", (0.0, 0.0), (5.0, 0.0), (200.0, 100.0), (8.0, 0.0), 100.0, -66.667),
    ("parallel", (0.0, 0.0), (3.0, 0.0), (100.0, 120.0), (3.0, 0.0), 156.205, 0.0),
    ("far", (0.0, 0.0), (1.0, 0.0), (7000.0, 0.0), (-1.0, 0.0), 0.0, 3500.0),
]


@pytest.mark.parametrize(("name", "op", "ov", "xp", "xv", "cpa", "tcpa"), KNOWN)
def test_known_geometry(
    name: str,
    op: tuple[float, float],
    ov: tuple[float, float],
    xp: tuple[float, float],
    xv: tuple[float, float],
    cpa: float,
    tcpa: float,
) -> None:
    res = cpa_tcpa(op, ov, xp, xv)
    assert isinstance(res, CpaResult)
    assert abs(res.cpa_m - cpa) <= 1.0, name
    assert abs(res.tcpa_s - tcpa) <= 1.0, name
    # tighter than the directive's 1 m / 1 s: the hand values are given to 3 dp
    assert res.cpa_m == pytest.approx(cpa, abs=1e-3)
    assert res.tcpa_s == pytest.approx(tcpa, abs=1e-3)
    assert res.cpa_m == round(res.cpa_m, 6) and res.tcpa_s == round(res.tcpa_s, 6)


def test_course_conversion() -> None:
    s = 4.0
    for heading, (ex, ey) in {
        0.0: (0.0, s),
        90.0: (s, 0.0),
        225.0: (-s / math.sqrt(2), -s / math.sqrt(2)),
    }.items():
        vx, vy = velocity_from_course(s, heading)
        assert abs(vx - ex) <= 1e-9 and abs(vy - ey) <= 1e-9, heading
    for _, op, ov, xp, xv, _, _ in KNOWN:
        a = cpa_tcpa(op, ov, xp, xv)
        b = cpa_tcpa(xp, xv, op, ov)
        assert a.cpa_m == pytest.approx(b.cpa_m, abs=1e-6)
        assert a.tcpa_s == pytest.approx(b.tcpa_s, abs=1e-6)


coord = st.floats(min_value=-5000.0, max_value=5000.0, allow_nan=False, allow_infinity=False)
speed = st.floats(min_value=-10.0, max_value=10.0, allow_nan=False, allow_infinity=False)
vec = st.tuples(coord, coord)
vel = st.tuples(speed, speed)


@settings(max_examples=300, deadline=None, derandomize=True)
@given(op=vec, ov=vel, xp=vec, xv=vel, off=vec)
def test_properties(
    op: tuple[float, float],
    ov: tuple[float, float],
    xp: tuple[float, float],
    xv: tuple[float, float],
    off: tuple[float, float],
) -> None:
    res = cpa_tcpa(op, ov, xp, xv)
    assert res.cpa_m >= 0.0
    rng = math.hypot(xp[0] - op[0], xp[1] - op[1])
    if res.tcpa_s > 0.0:
        assert res.cpa_m <= rng + 1e-6
    moved = cpa_tcpa((op[0] + off[0], op[1] + off[1]), ov, (xp[0] + off[0], xp[1] + off[1]), xv)
    tol_cpa = 1e-6 * max(1.0, rng)
    assert abs(moved.cpa_m - res.cpa_m) <= max(1e-6, tol_cpa) + 1e-9
    assert abs(moved.tcpa_s - res.tcpa_s) <= max(1e-6, 1e-6 * max(1.0, abs(res.tcpa_s))) + 1e-9
