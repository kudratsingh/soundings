"""Separation advisories (WO-083): CPA/TCPA between surface vessels and in-water filed jobs.

Soundings proposes a slow advisory through the standing-orders guard; vessel operators
accept or ignore it. Nothing here plans a route or gives a vessel an instruction.
"""

from .cpa import CpaResult, cpa_tcpa, velocity_from_course

__all__ = ["CpaResult", "cpa_tcpa", "velocity_from_course"]
