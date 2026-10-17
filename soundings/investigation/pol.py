"""Pattern of life: a seeded, simulated normal month (SPEC-INVESTIGATION section 4).

``generate_normal_month`` writes benign-only synthetic scenarios (``FILED_ON_PLAN``,
``BIOLOGIC``, ``DEBRIS_OR_DRIFT``, ``SURFACE_VESSEL_AIS_OFF``) for every zone x hour x
weekday of a ``days``-long month, runs each through ``soundings.sim.run.run_scenario``
(one contact, a short run, no biologic false alarms so every observed track is that
contact), and summarizes the observed tracks per cell ``pol[zone][hour][weekday]``:

* ``density``: observed contacts per hour in the cell (count / days with that weekday);
* ``hypothesis_mix``: the share of each benign hypothesis (sums to 1);
* ``numeric``: mean and std of ``speed_over_ground``, ``depth``, ``dwell_time_in_zone``;
* ``transit_pattern``: the frequency of each pattern.

Each zone x hour also gets a pooled cell under weekday ``"*"`` (every day of the month).
A weekday cell with fewer than ``MIN_CELL_CONTACTS`` characterized contacts is too thin to
give a mean and std, so ``PolTable.anomaly`` scores against the pooled cell instead.

The features come from :func:`soundings.investigation.features.pol_observation`, the same
definitions the evidence table uses, keyed by the zone and clock hour of the last reading.
Every observed track counts toward density and the hypothesis mix (re-issued ids after a
gap are separate contacts to the watch too), but only a track long enough to carry the
whole feature vector (``PolObservation.characterized``: 10 readings over 60 s) feeds the
means, stds and pattern frequencies; a few readings over seconds give noise, not speed.
The contact rates and behaviors below are stand-in values for a demonstration harbor, not
observed traffic; the ``pol_baseline`` evidence row says so.

This module is exempt from the evidence table's ground-truth guard: it generates its own
scenarios and knows each one's hypothesis because it chose it. It still reads only
``SimRun.tracks`` from each run.

Determinism (docs/WORKFLOW.md section 8): one ``numpy.random.default_rng(seed)`` draws
every count, behavior, start point and per-run sim seed in a fixed loop order (day, hour,
zone sorted by id, hypothesis); no wall clock; floats rounded to 6 dp when a cell is
built. ``make pol`` regenerates ``data/pol_table.json``.
"""

from __future__ import annotations

import argparse
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from functools import cache
from pathlib import Path
from typing import Any, Final

import numpy as np

from soundings.canonical import canonical_json, round6
from soundings.investigation.features import (
    POL_INPUT_FEATURES,
    TRANSIT_PATTERNS,
    WEEKDAYS,
    pol_observation,
)
from soundings.schemas._base import REPO_ROOT, NonNegFloat, NonNegInt, Probability, StrictModel
from soundings.schemas.enums import FeatureName, Hypothesis
from soundings.schemas.evidence import EvidenceValue
from soundings.schemas.harbor import Harbor, Zone, load_harbor
from soundings.schemas.scenario import Scenario, scenario_from_data
from soundings.sim.detection import DetectionParams
from soundings.sim.run import default_config, run_scenario

POL_SEED: Final = 42
POL_DAYS: Final = 28
DEFAULT_POL_PATH: Final = REPO_ROOT / "data" / "pol_table.json"
POL_SCENARIO_ID: Final = "S00"  # synthetic; never a catalog file (catalog_paths globs files)
TICK_S: Final = 2.0
MONTH_START_WEEKDAY: Final = 0  # day 0 is WEEKDAYS[0] ("mon")

BENIGN_HYPOTHESES: Final[tuple[Hypothesis, ...]] = (
    Hypothesis.FILED_ON_PLAN,
    Hypothesis.BIOLOGIC,
    Hypothesis.DEBRIS_OR_DRIFT,
    Hypothesis.SURFACE_VESSEL_AIS_OFF,
)
NUMERIC_FEATURES: Final[tuple[FeatureName, ...]] = tuple(
    f for f in POL_INPUT_FEATURES if f is not FeatureName.TRANSIT_PATTERN
)
STD_FLOOR: Final[dict[FeatureName, float]] = {
    FeatureName.SPEED_OVER_GROUND: 0.25,  # m/s
    FeatureName.DEPTH: 1.5,  # m
    FeatureName.DWELL_TIME_IN_ZONE: 120.0,  # s
}
"""A cell's std is at least this (the spread of ordinary traffic), so a cell of a few
near-identical contacts never turns a small difference into a large z."""
ALL_WEEKDAYS: Final = "*"
"""Weekday key of the pooled ``pol[zone][hour]["*"]`` cell (all days of the month)."""
MIN_CELL_CONTACTS: Final = 8
"""A weekday cell with fewer characterized contacts is too thin to score; ``anomaly``
uses the pooled zone x hour cell instead."""

RUN_TICKS: Final[dict[Hypothesis, int]] = {
    Hypothesis.FILED_ON_PLAN: 600,  # a survey leg: 20 min
    Hypothesis.BIOLOGIC: 150,  # an animal passing through: 5 min
    Hypothesis.DEBRIS_OR_DRIFT: 450,  # drift in view: 15 min
    Hypothesis.SURFACE_VESSEL_AIS_OFF: 120,  # a hull crossing the zone: 4 min
}
"""Run length per hypothesis (short runs keep the month under the 60 s bound)."""

DAY_START_HOUR: Final = 7  # 07:00 <= hour < 18:00 is the working day
DAY_END_HOUR: Final = 18
TWILIGHT_HOURS: Final = frozenset({5, 6, 18, 19})
WEEKEND: Final = frozenset({"sat", "sun"})

# Stand-in contact rates (observed-contact arrivals per hour per zone) and zone weights.
FILED_RATE_WORKDAY: Final = 0.30
FILED_RATE_WEEKEND: Final = 0.10
FILED_RATE_NIGHT: Final = 0.02
BIOLOGIC_RATE: Final = 0.25
BIOLOGIC_RATE_TWILIGHT: Final = 0.45
DEBRIS_RATE: Final = 0.10
SURFACE_RATE_DAY: Final = 0.12
SURFACE_RATE_NIGHT: Final = 0.04
FILED_ZONE_WEIGHT: Final[dict[str, float]] = {
    "inner_basin_research": 2.0,
    "pier_400_approach": 1.0,
    "inner_harbor_west": 1.0,
    "main_channel": 0.6,
    "fuel_berths": 0.5,
    "vincent_thomas_bridge": 0.5,
}
SURFACE_ZONE_WEIGHT: Final[dict[str, float]] = {
    "main_channel": 2.0,
    "pier_400_approach": 1.5,
}
DEBRIS_ZONE_WEIGHT: Final[dict[str, float]] = {"main_channel": 1.5}
MAX_START_TRIES: Final = 200


# ---------------------------------------------------------------------------
# The table
# ---------------------------------------------------------------------------


class FeatureStats(StrictModel):
    """Mean and population std of one numeric feature over a cell's known values."""

    n: NonNegInt
    mean: float
    std: NonNegFloat


class PolCell(StrictModel):
    """One ``pol[zone][hour][weekday]`` cell (SPEC-INVESTIGATION section 4)."""

    n_contacts: NonNegInt  # every observed track
    n_characterized: NonNegInt  # tracks that fed numeric and transit_pattern
    density: NonNegFloat  # observed contacts per hour
    hypothesis_mix: dict[str, Probability]
    numeric: dict[str, FeatureStats]
    transit_pattern: dict[str, Probability]


CellKey = tuple[str, int, str]  # (zone_id, hour 0-23, weekday)


def _known_number(value: EvidenceValue | None) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def anomaly_score(cell: PolCell, observed: Mapping[str, EvidenceValue]) -> float | None:
    """Anomaly in [0, 1] of ``observed`` against ``cell``; None when nothing compares.

    The mean of ``erf(|z_k| / sqrt(2))`` over the numeric features (z against the cell's
    mean and ``max(std, STD_FLOOR)``) and ``1 - freq(pattern) / max_freq`` for
    ``transit_pattern``. Features missing from ``observed`` (unknown) are skipped, never
    imputed. An empty cell (no contacts) gives None.
    """
    if cell.n_contacts == 0:
        return None
    terms: list[float] = []
    for feature in NUMERIC_FEATURES:
        x = _known_number(observed.get(feature.value))
        stats = cell.numeric.get(feature.value)
        if x is None or stats is None or stats.n == 0:
            continue
        z = (x - stats.mean) / max(stats.std, STD_FLOOR[feature])
        terms.append(math.erf(abs(z) / math.sqrt(2.0)))
    pattern = observed.get(FeatureName.TRANSIT_PATTERN.value)
    if isinstance(pattern, str) and cell.transit_pattern:
        max_freq = max(cell.transit_pattern.values())
        if max_freq > 0.0:
            terms.append(1.0 - cell.transit_pattern.get(pattern, 0.0) / max_freq)
    if not terms:
        return None
    return round6(min(max(math.fsum(terms) / len(terms), 0.0), 1.0))


@dataclass(frozen=True)
class PolTable:
    """The normal-month table. Implements ``features.PolBaseline`` for ``build_evidence``.

    ``cells`` holds the populated cells only; a missing key is an empty cell. Weekday
    ``ALL_WEEKDAYS`` (``"*"``) keys the pooled zone x hour cell over the whole month.
    """

    seed: int
    days: int
    cells: Mapping[CellKey, PolCell] = field(default_factory=dict)

    def cell(self, zone_id: str, hour: int, weekday: str) -> PolCell | None:
        return self.cells.get((zone_id, hour, weekday))

    def baseline_cell(self, zone_id: str, hour: int, weekday: str) -> PolCell | None:
        """The cell ``anomaly`` scores against: the weekday cell when it holds at least
        ``MIN_CELL_CONTACTS`` characterized contacts, else the pooled zone x hour cell
        (None when that is empty too)."""
        if weekday not in WEEKDAYS:
            raise ValueError(f"unknown weekday {weekday!r}")
        cell = self.cell(zone_id, hour, weekday)
        if cell is not None and cell.n_characterized >= MIN_CELL_CONTACTS:
            return cell
        return self.cell(zone_id, hour, ALL_WEEKDAYS)

    def density(self, zone_id: str, hour: int, weekday: str) -> float:
        cell = self.cell(zone_id, hour, weekday)
        return 0.0 if cell is None else cell.density

    def anomaly(
        self, zone_id: str, hour: int, weekday: str, observed: Mapping[str, EvidenceValue]
    ) -> float | None:
        """``anomaly_score`` against ``baseline_cell``, or None when it is empty."""
        cell = self.baseline_cell(zone_id, hour, weekday)
        return None if cell is None else anomaly_score(cell, observed)

    def to_data(self) -> dict[str, Any]:
        nested: dict[str, dict[str, dict[str, Any]]] = {}
        for (zone_id, hour, weekday), cell in sorted(self.cells.items()):
            by_hour = nested.setdefault(zone_id, {})
            by_hour.setdefault(f"{hour:02d}", {})[weekday] = cell.model_dump(mode="json")
        return {
            "assumes": (
                f"pattern of life is a simulated normal month (seed {self.seed}); "
                "not observed history"
            ),
            "cells": nested,
            "days": self.days,
            "seed": self.seed,
            "min_cell_contacts": MIN_CELL_CONTACTS,
            "weekdays": [*WEEKDAYS, ALL_WEEKDAYS],
        }

    def to_json(self) -> str:
        """Canonical JSON (sorted keys, 6 dp) plus a trailing newline."""
        return canonical_json(self.to_data()) + "\n"

    @classmethod
    def from_data(cls, data: Mapping[str, Any]) -> PolTable:
        cells: dict[CellKey, PolCell] = {}
        for zone_id, by_hour in data["cells"].items():
            for hour_text, by_weekday in by_hour.items():
                hour = int(hour_text)
                if not 0 <= hour <= 23:
                    raise ValueError(f"pol table: bad hour {hour_text!r}")
                for weekday, raw in by_weekday.items():
                    if weekday not in WEEKDAYS and weekday != ALL_WEEKDAYS:
                        raise ValueError(f"pol table: bad weekday {weekday!r}")
                    cells[(zone_id, hour, weekday)] = PolCell.model_validate(raw)
        return cls(seed=int(data["seed"]), days=int(data["days"]), cells=cells)

    @classmethod
    def from_json(cls, text: str) -> PolTable:
        return cls.from_data(json.loads(text))


def load_pol_table(path: Path | str = DEFAULT_POL_PATH) -> PolTable:
    """Read ``data/pol_table.json`` (or ``path``)."""
    return PolTable.from_json(Path(path).read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# The simulated month
# ---------------------------------------------------------------------------


def weekday_of_day(day: int) -> str:
    return WEEKDAYS[(MONTH_START_WEEKDAY + day) % len(WEEKDAYS)]


def contact_rate(hypothesis: Hypothesis, zone_id: str, hour: int, weekday: str) -> float:
    """Stand-in arrivals per hour of one benign hypothesis in one zone at one hour."""
    working = DAY_START_HOUR <= hour < DAY_END_HOUR
    if hypothesis is Hypothesis.FILED_ON_PLAN:
        if not working:
            base = FILED_RATE_NIGHT
        else:
            base = FILED_RATE_WEEKEND if weekday in WEEKEND else FILED_RATE_WORKDAY
        return base * FILED_ZONE_WEIGHT.get(zone_id, 1.0)
    if hypothesis is Hypothesis.BIOLOGIC:
        return BIOLOGIC_RATE_TWILIGHT if hour in TWILIGHT_HOURS else BIOLOGIC_RATE
    if hypothesis is Hypothesis.DEBRIS_OR_DRIFT:
        return DEBRIS_RATE * DEBRIS_ZONE_WEIGHT.get(zone_id, 1.0)
    if hypothesis is Hypothesis.SURFACE_VESSEL_AIS_OFF:
        base = SURFACE_RATE_DAY if working else SURFACE_RATE_NIGHT
        return base * SURFACE_ZONE_WEIGHT.get(zone_id, 1.0)
    raise ValueError(f"{hypothesis.value} is not a benign hypothesis")


def _point_in_zone(zone: Zone, rng: np.random.Generator) -> tuple[float, float]:
    lats = [p[0] for p in zone.polygon]
    lons = [p[1] for p in zone.polygon]
    for _ in range(MAX_START_TRIES):
        lat = round6(float(rng.uniform(min(lats), max(lats))))
        lon = round6(float(rng.uniform(min(lons), max(lons))))
        if zone.contains(lat, lon):
            return lat, lon
    raise RuntimeError(f"zone {zone.zone_id}: no start point after {MAX_START_TRIES} tries")


def _u(rng: np.random.Generator, lo: float, hi: float) -> float:
    return round6(float(rng.uniform(lo, hi)))


def synthetic_scenario(
    hypothesis: Hypothesis, zone: Zone, hour: int, rng: np.random.Generator
) -> Scenario:
    """One benign contact starting inside ``zone`` at ``hour``, behavior drawn from ``rng``."""
    lat, lon = _point_in_zone(zone, rng)
    registry: dict[str, Any] = {"filed": False, "replies_to_hail": False}
    acoustic: dict[str, Any]
    if hypothesis is Hypothesis.FILED_ON_PLAN:
        depth_m = _u(rng, 4.0, 12.0)
        if rng.random() < 0.7:
            behavior: dict[str, Any] = {
                "type": "lawnmower",
                "area": zone.zone_id,
                "speed_mps": _u(rng, 0.8, 1.5),
                "lane_spacing_m": 50.0,
            }
        else:
            wlat, wlon = _point_in_zone(zone, rng)
            behavior = {
                "type": "transit",
                "waypoint": {"lat": wlat, "lon": wlon},
                "speed_mps": _u(rng, 1.0, 2.0),
            }
        acoustic = {"propeller_tonal": True, "biologic_signature": False}
        registry = {"filed": True, "replies_to_hail": True}
    elif hypothesis is Hypothesis.BIOLOGIC:
        depth_m = _u(rng, 0.5, 4.0)
        behavior = {
            "type": "erratic",
            "speed_mps": _u(rng, 0.5, 1.5),
            "drift_weight": _u(rng, 0.4, 0.8),
        }
        acoustic = {"propeller_tonal": False, "tonal_band": "none", "biologic_signature": True}
    elif hypothesis is Hypothesis.DEBRIS_OR_DRIFT:
        depth_m = _u(rng, 0.2, 2.0)
        behavior = {"type": "drift", "jitter_mps": _u(rng, 0.02, 0.08)}
        acoustic = {"propeller_tonal": False, "tonal_band": "none", "biologic_signature": False}
    elif hypothesis is Hypothesis.SURFACE_VESSEL_AIS_OFF:
        depth_m = _u(rng, 0.5, 1.5)
        wlat, wlon = _point_in_zone(zone, rng)
        behavior = {
            "type": "transit",
            "waypoint": {"lat": wlat, "lon": wlon},
            "speed_mps": _u(rng, 2.0, 5.0),
        }
        acoustic = {"propeller_tonal": True, "biologic_signature": False}
    else:
        raise ValueError(f"{hypothesis.value} is not a benign hypothesis")
    return scenario_from_data(
        {
            "id": POL_SCENARIO_ID,
            "name": f"normal month: {hypothesis.value} in {zone.zone_id}",
            "seed": 0,
            "duration_ticks": RUN_TICKS[hypothesis],
            "tick_s": TICK_S,
            "ground_truth": hypothesis.value,
            "hour_of_day": hour,
            "contacts": [
                {
                    "id": "pol-1",
                    "start": {"lat": lat, "lon": lon, "depth_m": depth_m},
                    "behavior": behavior,
                    "acoustic": acoustic,
                    "registry": registry,
                }
            ],
            "expected": {"terminal_state": "RESOLVED_BENIGN"},
        }
    )


@dataclass
class _CellAcc:
    n: int = 0
    n_characterized: int = 0
    hypotheses: dict[str, int] = field(default_factory=dict)
    numeric: dict[str, list[float]] = field(default_factory=dict)
    patterns: dict[str, int] = field(default_factory=dict)

    def add(self, hypothesis: Hypothesis, values: Mapping[str, EvidenceValue] | None) -> None:
        """Count one track; ``values=None`` (not characterized) adds no feature values."""
        self.n += 1
        self.hypotheses[hypothesis.value] = self.hypotheses.get(hypothesis.value, 0) + 1
        if values is None:
            return
        self.n_characterized += 1
        for feature in NUMERIC_FEATURES:
            x = _known_number(values.get(feature.value))
            if x is not None:
                self.numeric.setdefault(feature.value, []).append(x)
        pattern = values.get(FeatureName.TRANSIT_PATTERN.value)
        if isinstance(pattern, str):
            self.patterns[pattern] = self.patterns.get(pattern, 0) + 1


def _stats(values: Sequence[float]) -> FeatureStats:
    n = len(values)
    mean = math.fsum(values) / n
    var = math.fsum((v - mean) ** 2 for v in values) / n
    return FeatureStats(n=n, mean=round6(mean), std=round6(math.sqrt(var)))


def _cell(acc: _CellAcc, days_in_cell: int) -> PolCell:
    n_patterns = sum(acc.patterns.values())
    return PolCell(
        n_contacts=acc.n,
        n_characterized=acc.n_characterized,
        density=round6(acc.n / days_in_cell),
        hypothesis_mix={
            h.value: round6(acc.hypotheses.get(h.value, 0) / acc.n) for h in BENIGN_HYPOTHESES
        },
        numeric={f: _stats(v) for f, v in sorted(acc.numeric.items()) if v},
        transit_pattern=(
            {p: round6(acc.patterns.get(p, 0) / n_patterns) for p in TRANSIT_PATTERNS}
            if n_patterns
            else {}
        ),
    )


@cache
def _pol_params() -> DetectionParams:
    # Biologic false alarms off: each run's observed tracks are its one contact.
    return replace(default_config().params, false_alarms_per_node_hour=0.0)


def generate_normal_month(
    seed: int = POL_SEED, days: int = POL_DAYS, harbor: Harbor | None = None
) -> PolTable:
    """Simulate ``days`` benign days and summarize them per zone x hour x weekday."""
    if days < 1:
        raise ValueError("days must be >= 1")
    h = harbor if harbor is not None else load_harbor()
    zones = sorted(h.zones, key=lambda z: z.zone_id)
    rng = np.random.default_rng(seed)
    params = _pol_params()
    days_per_weekday = {w: 0 for w in WEEKDAYS}
    accs: dict[CellKey, _CellAcc] = {}
    for day in range(days):
        weekday = weekday_of_day(day)
        days_per_weekday[weekday] += 1
        for hour in range(24):
            for zone in zones:
                for hypothesis in BENIGN_HYPOTHESES:
                    count = int(rng.poisson(contact_rate(hypothesis, zone.zone_id, hour, weekday)))
                    for _ in range(count):
                        scenario = synthetic_scenario(hypothesis, zone, hour, rng)
                        run_seed = int(rng.integers(0, 2**31 - 1))
                        run = run_scenario(scenario, seed=run_seed, harbor=h, params=params)
                        for contact_id in sorted(run.tracks):
                            obs = pol_observation(run.tracks[contact_id], h, hour_of_day=hour)
                            if obs.zone_id is None:
                                continue  # outside every zone: no cell to count it in
                            for key in (
                                (obs.zone_id, obs.hour, weekday),
                                (obs.zone_id, obs.hour, ALL_WEEKDAYS),
                            ):
                                accs.setdefault(key, _CellAcc()).add(
                                    hypothesis, obs.values if obs.characterized else None
                                )
    days_per_weekday[ALL_WEEKDAYS] = days
    cells = {key: _cell(acc, days_per_weekday[key[2]]) for key, acc in sorted(accs.items())}
    return PolTable(seed=seed, days=days, cells=cells)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m soundings.investigation.pol",
        description="Generate the simulated normal-month pattern-of-life table.",
    )
    parser.add_argument("--seed", type=int, default=POL_SEED)
    parser.add_argument("--days", type=int, default=POL_DAYS)
    parser.add_argument("--out", type=Path, default=DEFAULT_POL_PATH)
    args = parser.parse_args(argv)
    table = generate_normal_month(seed=args.seed, days=args.days)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(table.to_json(), encoding="utf-8")
    print(f"pol: {len(table.cells)} populated cells -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "ALL_WEEKDAYS",
    "BENIGN_HYPOTHESES",
    "DEFAULT_POL_PATH",
    "FeatureStats",
    "MIN_CELL_CONTACTS",
    "NUMERIC_FEATURES",
    "POL_DAYS",
    "POL_SEED",
    "PolCell",
    "PolTable",
    "STD_FLOOR",
    "anomaly_score",
    "contact_rate",
    "generate_normal_month",
    "load_pol_table",
    "synthetic_scenario",
    "weekday_of_day",
]
