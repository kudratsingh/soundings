"""Acoustic features the simulated detector attaches to each reading.

Shared-tables honesty note: the simulator samples acoustic features from the same
``config/likelihoods.yaml`` tables the scorer reads, so demo results flatter the
scorer. The Phase 5 eval adds runs where the sim draws from perturbed tables to show
the scorer is not grading its own homework.

Stage 1 (this file today) holds the per-tonal-band detector defaults
(``cavitation_index``, ``broadband_level``) from ``config/sim.yaml`` and the
``silent_after_tick`` modifier. Sampling unset fields from the likelihood rows is
added once ``soundings.schemas.config.load_likelihood_spec`` exists.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path

from soundings.sim.detection import DEFAULT_SIM_CONFIG_PATH, load_sim_yaml

TONAL_BANDS: tuple[str, ...] = ("none", "low", "mid", "high")


@dataclass(frozen=True, slots=True)
class AcousticDefaults:
    """Detector readings that depend only on the tonal band."""

    cavitation_index: float
    broadband_level_db: float


@dataclass(frozen=True, slots=True)
class AcousticTruth:
    """What a contact actually sounds like at one tick (ground truth, before detection)."""

    propeller_tonal: bool
    tonal_band: str
    biologic_signature: bool

    def __post_init__(self) -> None:
        if self.tonal_band not in TONAL_BANDS:
            raise ValueError(f"tonal_band must be one of {TONAL_BANDS}, got {self.tonal_band!r}")


SILENT = AcousticTruth(propeller_tonal=False, tonal_band="none", biologic_signature=False)


def load_acoustic_defaults(
    path: Path | str = DEFAULT_SIM_CONFIG_PATH,
) -> dict[str, AcousticDefaults]:
    """Load ``acoustic_defaults`` from ``config/sim.yaml``, keyed by tonal band in band order."""
    block = load_sim_yaml(path).get("acoustic_defaults")
    if not isinstance(block, dict):
        raise ValueError(f"{path}: missing 'acoustic_defaults' mapping")
    if set(block) != set(TONAL_BANDS):
        raise ValueError(f"acoustic_defaults keys must be exactly {TONAL_BANDS}")
    out: dict[str, AcousticDefaults] = {}
    for band in TONAL_BANDS:
        row = block[band]
        if not isinstance(row, dict) or set(row) != {"cavitation_index", "broadband_level_db"}:
            raise ValueError(f"acoustic_defaults.{band} needs cavitation_index, broadband_level_db")
        cav, bb = row["cavitation_index"], row["broadband_level_db"]
        for name, value in (("cavitation_index", cav), ("broadband_level_db", bb)):
            if isinstance(value, bool) or not isinstance(value, int | float):
                raise ValueError(f"acoustic_defaults.{band}.{name} must be a number")
        if not 0.0 <= float(cav) <= 1.0:
            raise ValueError(f"acoustic_defaults.{band}.cavitation_index must be in [0, 1]")
        out[band] = AcousticDefaults(cavitation_index=float(cav), broadband_level_db=float(bb))
    return out


def is_silent(tick: int, silent_after_tick: int | None) -> bool:
    """True once the ``silent_after_tick`` modifier has taken effect (from that tick on)."""
    return silent_after_tick is not None and tick >= silent_after_tick


def apply_silence(
    tick: int,
    silent_after_tick: int | None,
    acoustic: AcousticTruth,
    replies_to_hail: bool,
) -> tuple[AcousticTruth, bool]:
    """The ``silent_after_tick`` modifier: acoustics go to none and hail replies stop.

    Applies from ``silent_after_tick`` onward (aligned with ``fault_tick`` in S02, where
    the vehicle loses propulsion and goes quiet on the same tick). Before that, the
    inputs pass through unchanged.
    """
    if is_silent(tick, silent_after_tick):
        return replace(SILENT), False
    return acoustic, replies_to_hail
