"""Observed tracks: what the sensors report, never ground truth.

Positions must lie inside the simplified harbor bounding box. Ticks rise strictly; gaps
are inclusive tick ranges with no reading, sorted, disjoint, and never covering a
reading's tick.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import Field, StrictBool, model_validator

from soundings.canonical import round6
from soundings.schemas._base import (
    HarborId,
    Lat,
    Lon,
    NonNegFloat,
    NonNegInt,
    PositiveFloat,
    Probability,
    StrictModel,
)
from soundings.schemas.enums import TonalBand
from soundings.schemas.evidence import UNKNOWN, ContactId, Unknown


class AcousticReading(StrictModel):
    """Stubbed acoustic features per reading. ``"unknown"`` means the detector did not say."""

    propeller_tonal_present: StrictBool | Unknown = UNKNOWN
    tonal_band: TonalBand | Unknown = UNKNOWN
    cavitation_index: NonNegFloat | Unknown = UNKNOWN
    breathing_or_click_present: StrictBool | Unknown = UNKNOWN
    broadband_level: float | Unknown = UNKNOWN


class TrackPoint(StrictModel):
    tick: NonNegInt
    lat: Lat
    lon: Lon
    depth_m: NonNegFloat
    sigma_m: PositiveFloat
    sensor_id: HarborId
    confidence: Probability
    acoustic: AcousticReading = Field(default_factory=AcousticReading)


class TrackGap(StrictModel):
    """Inclusive tick range with no reading."""

    start_tick: NonNegInt
    end_tick: NonNegInt

    @model_validator(mode="after")
    def _ordered(self) -> TrackGap:
        if self.start_tick > self.end_tick:
            raise ValueError("gap start_tick must be <= end_tick")
        return self

    @property
    def n_ticks(self) -> int:
        return self.end_tick - self.start_tick + 1


class Track(StrictModel):
    contact_id: ContactId
    tick_s: PositiveFloat
    points: Annotated[tuple[TrackPoint, ...], Field(min_length=1)]
    gaps: tuple[TrackGap, ...] = ()

    @model_validator(mode="after")
    def _consistent(self) -> Track:
        ticks = [p.tick for p in self.points]
        for prev, cur in zip(ticks, ticks[1:], strict=False):
            if cur <= prev:
                raise ValueError("track point ticks must rise strictly")
        for prev_gap, gap in zip(self.gaps, self.gaps[1:], strict=False):
            if gap.start_tick <= prev_gap.end_tick:
                raise ValueError("gaps must be sorted and disjoint")
        for gap in self.gaps:
            for tick in ticks:
                if gap.start_tick <= tick <= gap.end_tick:
                    raise ValueError(f"gap {gap.start_tick}..{gap.end_tick} covers tick {tick}")
        return self

    @property
    def gap_ratio(self) -> float:
        """Gap ticks inside the track's span over the span length, 6 dp."""
        first, last = self.points[0].tick, self.points[-1].tick
        span = last - first + 1
        covered = 0
        for gap in self.gaps:
            lo, hi = max(gap.start_tick, first), min(gap.end_tick, last)
            if lo <= hi:
                covered += hi - lo + 1
        return round6(covered / span)


class Contact(StrictModel):
    """An observed contact. ``reacquired_from`` names the id it continues (SPEC-INV 9.9)."""

    contact_id: ContactId
    track: Track
    reacquired_from: ContactId | None = None

    @model_validator(mode="after")
    def _ids(self) -> Contact:
        if self.track.contact_id != self.contact_id:
            raise ValueError("track.contact_id must equal contact_id")
        if self.reacquired_from == self.contact_id:
            raise ValueError("a contact cannot be re-acquired from itself")
        return self
