"""What a sim run returns. Observed data and ground truth live in separate fields.

``SimRun.tracks`` holds WO-010 ``Track`` objects: observed data only, the one field the
investigation reads. Ground truth lives only in ``contact_truth``, ``truth_paths`` and
the ``Detection`` rows (``gt_id``, ``is_false_alarm``), for tests and grading; the
investigation never reads them (WO-012 checks this with a grep).

Every float here is already rounded to 6 dp (the ``SimRun`` output boundary).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from pydantic import StrictBool

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
from soundings.schemas.enums import Hypothesis, TonalBand
from soundings.schemas.evidence import ContactId
from soundings.schemas.track import Track
from soundings.sim.toggles import ToggleReport


class Detection(StrictModel):
    """One emitted reading with its ground-truth label. Never read by the investigation."""

    observed_contact_id: ContactId
    gt_id: str
    tick: NonNegInt
    t_s: NonNegFloat
    node_id: HarborId
    lat: Lat
    lon: Lon
    depth_m: NonNegFloat
    range_m: NonNegFloat
    sensor_health: Probability
    confidence: Probability
    propeller_tonal_present: StrictBool
    tonal_band: TonalBand
    cavitation_index: NonNegFloat
    breathing_or_click_present: StrictBool
    broadband_level: float
    is_false_alarm: StrictBool


class ContactTruth(StrictModel):
    """Ground truth behind one observed contact id.

    ``replies_to_hail`` is the contact's reply behavior before any silence;
    ``silent_after_tick`` (from the scenario or a ``contact_goes_quiet`` toggle) is the
    tick from which acoustics go to none and hail replies stop.
    """

    gt_id: str
    hypothesis: Hypothesis
    replies_to_hail: StrictBool
    silent_after_tick: NonNegInt | None = None


class TruthPoint(StrictModel):
    """A ground-truth position at one tick (before detection and noise)."""

    tick: NonNegInt
    lat: float
    lon: float
    depth_m: NonNegFloat
    speed_mps: NonNegFloat
    heading_deg: float


class AisReport(StrictModel):
    """WO-080: one AIS-style surface report of a vessel transit (truthful, 6 dp)."""

    ais_id: str
    tick: NonNegInt
    t_s: NonNegFloat
    lat: float
    lon: float
    sog_mps: NonNegFloat
    cog_deg: float


@dataclass(frozen=True, slots=True)
class SimRun:
    """The result of ``soundings.sim.run.run_scenario``.

    ``tracks`` is keyed by observed id in id order; ``detections`` is sorted by
    (tick, observed id); ``contact_truth`` maps every observed id (``c-NNNN``) to its
    truth; ``truth_paths`` is keyed by ground-truth id (scenario ids, surge ids, and
    ``fa-NNNN`` false alarms), each point at one tick of that contact's life.
    """

    scenario_id: str
    seed: int
    tick_s: PositiveFloat
    n_ticks: int
    tracks: dict[ContactId, Track]
    detections: list[Detection]
    contact_truth: dict[str, ContactTruth]
    truth_paths: dict[str, list[TruthPoint]]
    toggle_report: ToggleReport
    ais_tracks: dict[str, tuple[AisReport, ...]] = field(default_factory=dict)
    """WO-080: surface vessel reports keyed by vessel id (``ais-01``...); never detections."""
