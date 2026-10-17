"""Pattern of life: the normal-month baseline table (SPEC-INVESTIGATION section 4).

This module holds the table the investigation reads: ``PolTable`` with one
``pol[zone][hour][weekday]`` cell per populated zone x clock hour x weekday,

* ``density``: observed contacts per hour in the cell (count / days with that weekday);
* ``hypothesis_mix``: the share of each benign hypothesis (sums to 1);
* ``numeric``: mean and std of ``speed_over_ground``, ``depth``, ``dwell_time_in_zone``;
* ``transit_pattern``: the frequency of each pattern.

Each zone x hour also gets a pooled cell under weekday ``"*"`` (every day of the month).
A weekday cell with fewer than ``MIN_CELL_CONTACTS`` characterized contacts is too thin to
give a mean and std, so ``PolTable.anomaly`` scores against the pooled cell instead.

The table is generated offline by ``soundings.sim.normal_month`` (a seeded, simulated
normal month of benign-only traffic; ``make pol`` regenerates ``data/pol_table.json``).
Generation lives with the simulator because it authors scenarios and runs them; this
module imports nothing from ``soundings.sim`` and never sees how a cell was produced
(``tests/test_boundary.py`` holds that line). The contact rates are stand-in values for
a demonstration harbor, not observed traffic; the ``pol_baseline`` evidence row says so.

Determinism (docs/WORKFLOW.md section 8): floats are rounded to 6 dp when a cell is
built; ``to_json`` is canonical.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from soundings.canonical import canonical_json, round6
from soundings.investigation.features import POL_INPUT_FEATURES, WEEKDAYS
from soundings.schemas._base import REPO_ROOT, NonNegFloat, NonNegInt, Probability, StrictModel
from soundings.schemas.enums import FeatureName, Hypothesis
from soundings.schemas.evidence import EvidenceValue

POL_SEED: Final = 42
POL_DAYS: Final = 28
DEFAULT_POL_PATH: Final = REPO_ROOT / "data" / "pol_table.json"
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


def known_number(value: EvidenceValue | None) -> float | None:
    """A number, or None for unknown, a bool, or text."""
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
        x = known_number(observed.get(feature.value))
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


def weekday_of_day(day: int) -> str:
    """Weekday key of day ``day`` of the month (day 0 is ``WEEKDAYS[MONTH_START_WEEKDAY]``)."""
    return WEEKDAYS[(MONTH_START_WEEKDAY + day) % len(WEEKDAYS)]


__all__ = [
    "ALL_WEEKDAYS",
    "BENIGN_HYPOTHESES",
    "CellKey",
    "DEFAULT_POL_PATH",
    "FeatureStats",
    "MIN_CELL_CONTACTS",
    "MONTH_START_WEEKDAY",
    "NUMERIC_FEATURES",
    "POL_DAYS",
    "POL_SEED",
    "PolCell",
    "PolTable",
    "STD_FLOOR",
    "anomaly_score",
    "known_number",
    "load_pol_table",
    "weekday_of_day",
]
