"""Seeded perturbation of the likelihood tables for the mis-specified eval (WO-051).

The sim and the scorer share ``config/likelihoods.yaml``. The mis-specified variant lets
the sim draw acoustics from a perturbed copy while the scorer keeps the real tables, so
the eval shows how outcomes shift when the world does not match the model.

* ``perturb_likelihoods(spec, magnitude, seed)``: every row is multiplied bucket by bucket
  by ``exp(magnitude * z)``, ``z ~ N(0, 1)`` drawn from ``numpy.random.default_rng(seed)``
  in sorted (feature, hypothesis) order, one draw per bucket. A zero bucket is first
  floored at ``ZERO_FLOOR`` so every perturbed bucket is > 0. Each row is then
  renormalized, rounded with ``round6`` (buckets kept >= ``MIN_BUCKET``), and the rounding
  residual goes on the largest bucket so every row sums to exactly 1.
* ``unpin_acoustics(scenario)``: clears the scenario's acoustic pins for features that
  have a likelihood table; without this a pinned feature ignores the perturbed table.
* ``table_shift(a, b)``: max and mean absolute cell difference.

Pure functions: the input spec and scenario are never mutated, and the same seed always
gives the same table.
"""

from __future__ import annotations

import math

import numpy as np

from soundings.canonical import round6
from soundings.schemas import FeatureName, LikelihoodSpec, Scenario
from soundings.schemas._base import StrictModel
from soundings.schemas.config import FeatureBuckets

ZERO_FLOOR = 1e-4
"""A structural zero becomes this before perturbing (only in the perturbed copy)."""

MIN_BUCKET = 1e-6
"""The smallest bucket a perturbed row may hold after rounding."""

# AcousticSpec field -> the likelihood feature the sim samples it from (soundings.sim.acoustics).
ACOUSTIC_PIN_FEATURES: dict[str, FeatureName] = {
    "propeller_tonal": FeatureName.PROPELLER_TONAL_PRESENT,
    "tonal_band": FeatureName.TONAL_BAND,
    "biologic_signature": FeatureName.BREATHING_OR_CLICK_PRESENT,
}


class TableShift(StrictModel):
    """Absolute cell differences between two likelihood tables of the same shape."""

    max_abs: float
    mean_abs: float
    cells: int


def _perturb_row(row: tuple[float, ...], z: np.ndarray, magnitude: float) -> tuple[float, ...]:
    raw = [
        max(p, ZERO_FLOOR) * math.exp(magnitude * float(zi)) for p, zi in zip(row, z, strict=True)
    ]
    total = sum(raw)
    out = [max(round6(v / total), MIN_BUCKET) for v in raw]
    big = max(range(len(out)), key=lambda i: (out[i], -i))
    out[big] = round6(1.0 - sum(v for i, v in enumerate(out) if i != big))
    return tuple(out)


def perturb_likelihoods(spec: LikelihoodSpec, magnitude: float, seed: int) -> LikelihoodSpec:
    """A perturbed copy of ``spec`` (see the module docstring). ``magnitude=0`` returns an
    equal table; the same ``seed`` always returns the same table."""
    if magnitude < 0.0 or not math.isfinite(magnitude):
        raise ValueError("magnitude must be a finite number >= 0")
    if magnitude == 0.0:
        return spec.model_copy(deep=True)
    rng = np.random.default_rng(seed)
    features: dict[FeatureName, FeatureBuckets] = {}
    for feature in sorted(spec.features, key=str):
        block = spec.features[feature]
        rows = {}
        for hyp in sorted(block.rows, key=str):
            z = rng.standard_normal(len(block.buckets))
            rows[hyp] = _perturb_row(block.rows[hyp], z, magnitude)
        features[feature] = block.model_copy(update={"rows": rows})
    ordered = {f: features[f] for f in spec.features}
    return LikelihoodSpec(features=ordered)


def unpin_acoustics(scenario: Scenario, spec: LikelihoodSpec | None = None) -> Scenario:
    """A copy of ``scenario`` whose contacts sample every acoustic field that has a
    likelihood table (``spec`` defaults to ``config/likelihoods.yaml``). ``tonal_band`` is
    also cleared when ``propeller_tonal`` is, because the sim derives the band from the
    tonal draw when no band table exists (a pinned band on an unpinned tonal would
    contradict it)."""
    if spec is None:
        from soundings.schemas import load_likelihood_spec

        spec = load_likelihood_spec()
    tables = set(spec.features)
    contacts = []
    for contact in scenario.contacts:
        cleared = {
            field: None
            for field, feature in ACOUSTIC_PIN_FEATURES.items()
            if feature in tables and getattr(contact.acoustic, field) is not None
        }
        if "propeller_tonal" in cleared:
            cleared["tonal_band"] = None
        acoustic = contact.acoustic.model_copy(update=cleared)
        contacts.append(contact.model_copy(update={"acoustic": acoustic}))
    return scenario.model_copy(update={"contacts": tuple(contacts)})


def table_shift(a: LikelihoodSpec, b: LikelihoodSpec) -> TableShift:
    """Max and mean absolute difference over the cells both tables share (6 dp)."""
    diffs: list[float] = []
    for feature in sorted(set(a.features) & set(b.features), key=str):
        ra, rb = a.features[feature].rows, b.features[feature].rows
        for hyp in sorted(set(ra) & set(rb), key=str):
            diffs.extend(abs(x - y) for x, y in zip(ra[hyp], rb[hyp], strict=True))
    if not diffs:
        return TableShift(max_abs=0.0, mean_abs=0.0, cells=0)
    return TableShift(
        max_abs=round6(max(diffs)), mean_abs=round6(sum(diffs) / len(diffs)), cells=len(diffs)
    )
