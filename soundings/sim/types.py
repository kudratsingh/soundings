"""What a sim run returns. Observed data and ground truth live in separate fields.

``SimRun.tracks`` holds WO-010 ``Track`` objects: observed data only, the one field the
investigation reads. Ground truth lives only in ``contact_truth``, ``truth_paths`` and
the ``Detection`` rows (``gt_id``, ``is_false_alarm``), for tests and grading; the
investigation never reads them (``tests/test_boundary.py``). The two ``SimRun`` methods
``hail_responder`` and ``eyes_on_path`` are the world answering a physical probe
(``soundings.ports.WorldRun``): they read truth here, inside the simulator, and return
no hypothesis label.

Every float here is already rounded to 6 dp (the ``SimRun`` output boundary).
"""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import StrictBool

from soundings.ports import EyesOnPoint, HailResponder
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


FALSE_ALARM_PREFIX = "fa-"
"""Ground-truth id prefix of a sensor false alarm (nothing physical is there)."""


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

    def hail_responder(self, contact_id: str) -> HailResponder | None:
        """How the thing behind ``contact_id`` answers a hail; None if nothing can.

        An id with no truth entry, or a false alarm, never replies. ``ContactTruth`` is
        read (not the scenario file), so a ``contact_goes_quiet`` toggle is honoured.
        """
        truth = self.contact_truth.get(contact_id)
        if truth is None or truth.gt_id.startswith(FALSE_ALARM_PREFIX):
            return None
        return HailResponder(
            registry_contact_id=truth.gt_id,
            replies_to_hail=truth.replies_to_hail,
            silent_after_tick=truth.silent_after_tick,
        )

    def eyes_on_path(self, contact_id: str) -> list[EyesOnPoint]:
        """``(tick, lat, lon, depth_m)`` of the simulated contact behind ``contact_id``."""
        truth = self.contact_truth.get(contact_id)
        if truth is None:
            return []
        return [(p.tick, p.lat, p.lon, p.depth_m) for p in self.truth_paths.get(truth.gt_id, [])]
