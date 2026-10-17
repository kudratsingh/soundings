"""The AIS-style surface feed (WO-080): truthful reports of vessel transits.

A ``vessel_transit`` contact is a surface vessel. The subsurface detection model never
hears it; the only place it appears is ``SimRun.ais_tracks``, one report every
``report_interval_s`` of scenario time (``config/sim.yaml`` ``ais:``; the 10 s default is
a stand-in for a simulated feed). Reports are built from the ground-truth path, so they
draw nothing and read no clock. Floats are rounded to 6 dp.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from soundings.canonical import round6
from soundings.sim.detection import DEFAULT_SIM_CONFIG_PATH, load_sim_yaml
from soundings.sim.types import AisReport, TruthPoint

DEFAULT_REPORT_INTERVAL_S = 10.0


@dataclass(frozen=True, slots=True)
class AisConfig:
    report_interval_s: float = DEFAULT_REPORT_INTERVAL_S

    def every_ticks(self, tick_s: float) -> int:
        """Report cadence in ticks (at least every tick)."""
        return max(1, int(math.floor(round(self.report_interval_s / tick_s, 9))))


def load_ais_config(path: Path | str = DEFAULT_SIM_CONFIG_PATH) -> AisConfig:
    """The ``ais:`` block of ``config/sim.yaml`` (defaults if the block is absent)."""
    block = load_sim_yaml(path).get("ais")
    if block is None:
        return AisConfig()
    if not isinstance(block, dict):
        raise ValueError(f"{path}: 'ais' must be a mapping")
    interval = float(block.get("report_interval_s", DEFAULT_REPORT_INTERVAL_S))
    if interval <= 0.0:
        raise ValueError(f"{path}: ais.report_interval_s must be > 0")
    return AisConfig(report_interval_s=interval)


def ais_reports(
    ais_id: str, points: Sequence[TruthPoint], tick_s: float, cfg: AisConfig
) -> tuple[AisReport, ...]:
    """One report per ``cfg`` interval along a vessel's truth path, from its first tick."""
    if not points:
        return ()
    every = cfg.every_ticks(tick_s)
    first = points[0].tick
    return tuple(
        AisReport(
            ais_id=ais_id,
            tick=p.tick,
            t_s=round6(p.tick * tick_s),
            lat=p.lat,
            lon=p.lon,
            sog_mps=p.speed_mps,
            cog_deg=p.heading_deg,
        )
        for p in points
        if (p.tick - first) % every == 0
    )
