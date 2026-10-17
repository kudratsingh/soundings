"""Acoustic features the simulated detector attaches to each reading.

Shared-tables honesty note: the simulator samples acoustic features from the same
``config/likelihoods.yaml`` tables the scorer reads, so demo results flatter the
scorer. The Phase 5 eval adds runs where the sim draws from perturbed tables to show
the scorer is not grading its own homework.

Per contact, a field set in the scenario's ``acoustic`` block is used as is; a field
left ``None`` is sampled once from the contact's hypothesis row
(``sample_acoustic_truth``). ``config/likelihoods.yaml`` has rows for
``propeller_tonal_present`` and ``breathing_or_click_present`` but none for
``tonal_band``; an unset band is ``none`` for a contact without a propeller tonal and is
drawn from ``tonal_band_when_tonal`` in ``config/sim.yaml`` otherwise (a ``tonal_band``
likelihood row, if one is added later, takes precedence). ``cavitation_index`` and
``broadband_level`` are detector defaults keyed by tonal band (``acoustic_defaults``).
The ``silent_after_tick`` modifier (``apply_silence``) turns everything to none.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path

import numpy as np

from soundings.schemas.config import LikelihoodSpec
from soundings.schemas.enums import FeatureName, Hypothesis, TonalBand
from soundings.schemas.scenario import AcousticSpec
from soundings.schemas.track import AcousticReading
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


# ---------------------------------------------------------------------------
# Sampling unset fields from the likelihood tables
# ---------------------------------------------------------------------------

TONAL_WHEN_TONAL_BANDS: tuple[str, ...] = ("low", "mid", "high")


def load_tonal_band_weights(path: Path | str = DEFAULT_SIM_CONFIG_PATH) -> dict[str, float]:
    """``tonal_band_when_tonal`` from ``config/sim.yaml``: band weights for a tonal contact."""
    block = load_sim_yaml(path).get("tonal_band_when_tonal")
    if not isinstance(block, dict) or set(block) != set(TONAL_WHEN_TONAL_BANDS):
        raise ValueError(f"tonal_band_when_tonal keys must be exactly {TONAL_WHEN_TONAL_BANDS}")
    out: dict[str, float] = {}
    for band in TONAL_WHEN_TONAL_BANDS:
        w = block[band]
        if isinstance(w, bool) or not isinstance(w, int | float) or w < 0:
            raise ValueError(f"tonal_band_when_tonal.{band} must be a number >= 0")
        out[band] = float(w)
    if sum(out.values()) <= 0.0:
        raise ValueError("tonal_band_when_tonal weights must not all be 0")
    return out


def _p_true(spec: LikelihoodSpec, feature: FeatureName, hypothesis: Hypothesis) -> float:
    block = spec.features[feature]
    return float(block.rows[hypothesis][block.buckets.index("true")])


def _pick(names: tuple[str, ...], weights: tuple[float, ...], u: float) -> str:
    """Inverse-CDF pick of a name with ``u`` in [0, 1); the last name absorbs rounding."""
    total = sum(weights)
    acc = 0.0
    for name, w in zip(names, weights, strict=True):
        acc += w / total
        if u < acc:
            return name
    return names[-1]


def sample_acoustic_truth(
    rng: np.random.Generator,
    hypothesis: Hypothesis,
    acoustic: AcousticSpec,
    likelihoods: LikelihoodSpec,
    band_weights: dict[str, float],
) -> AcousticTruth:
    """Resolve a contact's acoustic truth. Always draws exactly three uniforms, in order
    (tonal, band, biologic), so setting one field never shifts another field's draw."""
    u_tonal, u_band, u_bio = (float(u) for u in rng.random(3))
    if acoustic.propeller_tonal is not None:
        tonal = acoustic.propeller_tonal
    else:
        p = _p_true(likelihoods, FeatureName.PROPELLER_TONAL_PRESENT, hypothesis)
        tonal = u_tonal < p
    if acoustic.tonal_band is not None:
        band = str(acoustic.tonal_band.value)
    elif FeatureName.TONAL_BAND in likelihoods.features:
        block = likelihoods.features[FeatureName.TONAL_BAND]
        band = _pick(block.buckets, block.rows[hypothesis], u_band)
    elif not tonal:
        band = "none"
    else:
        names = TONAL_WHEN_TONAL_BANDS
        band = _pick(names, tuple(band_weights[b] for b in names), u_band)
    if acoustic.biologic_signature is not None:
        bio = acoustic.biologic_signature
    else:
        p = _p_true(likelihoods, FeatureName.BREATHING_OR_CLICK_PRESENT, hypothesis)
        bio = u_bio < p
    return AcousticTruth(propeller_tonal=tonal, tonal_band=band, biologic_signature=bio)


def acoustic_reading(
    truth: AcousticTruth, defaults: dict[str, AcousticDefaults]
) -> AcousticReading:
    """The detector's reading for a contact with this acoustic truth (6-dp floats)."""
    d = defaults[truth.tonal_band]
    return AcousticReading(
        propeller_tonal_present=truth.propeller_tonal,
        tonal_band=TonalBand(truth.tonal_band),
        cavitation_index=round(d.cavitation_index, 6),
        breathing_or_click_present=truth.biologic_signature,
        broadband_level=round(d.broadband_level_db, 6),
    )
