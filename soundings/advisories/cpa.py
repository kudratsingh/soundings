"""Closest point of approach (CPA) and time to CPA (TCPA) with the standard ARPA formulas.

Pure and scalar: ``math`` only, no numpy, no randomness, no clock (CLAUDE.md 2.4). Inputs
are local metres (``x`` east, ``y`` north, as ``soundings.schemas.geo.to_local_m``) and
metres per second. With ``r = other_pos - own_pos`` and ``v = other_vel - own_vel``:

    TCPA = -(r . v) / |v|^2        CPA = |r + v * TCPA|

When ``|v|^2 < REL_SPEED_SQ_EPS`` (no relative motion) TCPA is 0.0 and CPA is ``|r|``. A
negative TCPA is returned as computed: the pair is already opening. Both values are
rounded to 6 dp at this output boundary.
"""

from __future__ import annotations

import math
from typing import NamedTuple

from soundings.canonical import round6

REL_SPEED_SQ_EPS = 1e-9
"""Below this squared relative speed (m^2/s^2) the pair has no relative motion."""

Vec = tuple[float, float]


class CpaResult(NamedTuple):
    """CPA in metres and TCPA in seconds (negative: the closest point is in the past)."""

    cpa_m: float
    tcpa_s: float


def cpa_tcpa(own_pos: Vec, own_vel: Vec, other_pos: Vec, other_vel: Vec) -> CpaResult:
    """The ARPA CPA/TCPA of ``other`` relative to ``own`` (local metres, m/s)."""
    rx = other_pos[0] - own_pos[0]
    ry = other_pos[1] - own_pos[1]
    vx = other_vel[0] - own_vel[0]
    vy = other_vel[1] - own_vel[1]
    v2 = vx * vx + vy * vy
    if v2 < REL_SPEED_SQ_EPS:
        return CpaResult(cpa_m=round6(math.hypot(rx, ry)), tcpa_s=0.0)
    tcpa = -(rx * vx + ry * vy) / v2
    cpa = math.hypot(rx + vx * tcpa, ry + vy * tcpa)
    return CpaResult(cpa_m=round6(cpa), tcpa_s=round6(tcpa))


def velocity_from_course(speed_mps: float, heading_deg: float) -> Vec:
    """``(vx, vy)`` from speed and compass heading (0 = north, clockwise, degrees)."""
    h = math.radians(heading_deg)
    return speed_mps * math.sin(h), speed_mps * math.cos(h)


__all__ = ["REL_SPEED_SQ_EPS", "CpaResult", "cpa_tcpa", "velocity_from_course"]
