"""Naive-Bayes posterior over the eight fixed hypotheses (SPEC-INVESTIGATION sections 1-3).

Every number here traces to a cell of ``config/likelihoods.yaml`` or ``config/priors.yaml``
that a mentor can edit; nothing is learned, nothing is sampled, and no model output enters.
The scorer reads only the WO-012 evidence table and cannot express a ladder rung. Its
probabilities are stress-test estimates from a declared catalog, never an operational
forecast; every output carries ``STRESS_TEST_LABEL``.

Which rows count. For each feature with a likelihood table, in table order, the scorer
reads one row: ``EvidenceTable.latest(contact_id, feature)``. A feature's rows are
repeated readings of the same quantity (one acoustic row per reading, for example), so
multiplying them all in would double-count. A row whose value is ``unknown``, a feature
with no row, and a row whose confidence is ``unknown`` are skipped and recorded with a
reason (``unknown``, ``absent``, ``unknown_confidence``); a feature the contact has rows
for but the tables do not cover is recorded as ``no_table``. Skipped means never imputed.

Confidence weighting (SPEC-INVESTIGATION 9.10). A row with confidence `c ∈ [0,1]`
contributes `P(bucket | h) ** c`. c = 1 is a full naive-Bayes factor. c = 0 contributes
nothing. A confidence of `"unknown"` is treated as c = 0, and the row is recorded as
skipped with reason `unknown_confidence`.

Degenerate update. When a row gives every hypothesis weight 0, the posterior stays
unchanged and the row id goes into ``degenerate_row_ids``. No probability floor is
applied, so the tables stay exactly as a judge reads them.

Entropy is Shannon entropy in bits with 0 log 0 = 0. Floats are rounded to 6 dp only at
the output boundary (``to_dict``).
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from pathlib import Path
from types import MappingProxyType
from typing import Any, Final

import yaml
from pydantic import ConfigDict, Field, field_validator

from soundings.canonical import round6
from soundings.investigation.evidence import EvidenceTable
from soundings.investigation.features import bucketize
from soundings.schemas._base import StrictModel
from soundings.schemas.config import (
    LIKELIHOODS_PATH,
    PRIORS_PATH,
    LikelihoodSpec,
    PriorAdjustment,
    Priors,
    load_likelihood_spec,
    load_priors,
)
from soundings.schemas.enums import FeatureName, Hypothesis
from soundings.schemas.evidence import UNKNOWN

HYPOTHESES: Final[tuple[Hypothesis, ...]] = tuple(Hypothesis)
THREAT_HYPOTHESES: Final[tuple[Hypothesis, ...]] = (
    Hypothesis.UNAUTHORIZED_UUV,
    Hypothesis.UNAUTHORIZED_DIVER,
)
BENIGN_HYPOTHESES: Final[tuple[Hypothesis, ...]] = (
    Hypothesis.FILED_ON_PLAN,
    Hypothesis.BIOLOGIC,
    Hypothesis.DEBRIS_OR_DRIFT,
    Hypothesis.SURFACE_VESSEL_AIS_OFF,
)
STRESS_TEST_LABEL: Final = "stress-test estimate"
ROW_SUM_TOL: Final = 1e-6
PROB_SUM_TOL: Final = 1e-9

# Skip reasons (PosteriorReport.skipped values).
SKIP_UNKNOWN: Final = "unknown"
SKIP_ABSENT: Final = "absent"
SKIP_NO_TABLE: Final = "no_table"
SKIP_UNKNOWN_CONFIDENCE: Final = "unknown_confidence"

# Adjustment conditions read from the evidence table for the prior.
_NIGHT_FEATURE: Final = FeatureName.HOUR_OF_DAY
_ZONE_FEATURE: Final = FeatureName.ZONE_CRITICALITY

PRIOR_BASE: Final = "priors.base"


class LikelihoodTableError(ValueError):
    """A likelihood row (or the prior base) is negative or does not sum to 1 within 1e-6."""

    def __init__(self, feature: str, hypothesis: str | None, total: float) -> None:
        self.feature = feature
        self.hypothesis = hypothesis
        self.total = total
        where = feature if hypothesis is None else f"{feature} / {hypothesis}"
        super().__init__(
            f"{where}: row sums to {total!r} (tolerance {ROW_SUM_TOL}) or has a negative cell"
        )


class DegenerateUpdateError(ValueError):
    """Every hypothesis would get weight 0; the caller keeps the posterior unchanged."""


def _check_row(feature: str, hypothesis: str | None, cells: Sequence[Any]) -> None:
    numbers = [c for c in cells if isinstance(c, int | float) and not isinstance(c, bool)]
    if len(numbers) != len(cells):
        return  # not numeric: the WO-010 shape loader reports it
    total = math.fsum(float(c) for c in numbers)
    if any(float(c) < 0.0 for c in numbers) or abs(total - 1.0) > ROW_SUM_TOL:
        raise LikelihoodTableError(feature, hypothesis, total)


# --- likelihood tables -------------------------------------------------------------------


class LikelihoodTables:
    """P(bucket | hypothesis) per feature, in ``config/likelihoods.yaml`` order.

    Built from the WO-010 ``LikelihoodSpec``; every row is checked to be non-negative and
    to sum to 1 within ``ROW_SUM_TOL``.
    """

    def __init__(self, spec: LikelihoodSpec) -> None:
        self._buckets: dict[FeatureName, tuple[str, ...]] = {}
        self._rows: dict[FeatureName, dict[Hypothesis, tuple[float, ...]]] = {}
        for feature, body in spec.features.items():
            for hyp in HYPOTHESES:
                _check_row(feature.value, hyp.value, body.rows[hyp])
            self._buckets[feature] = tuple(body.buckets)
            self._rows[feature] = {h: tuple(float(p) for p in body.rows[h]) for h in HYPOTHESES}
        self._index = {f: {b: i for i, b in enumerate(bs)} for f, bs in self._buckets.items()}

    def features(self) -> tuple[FeatureName, ...]:
        """Features with a table, in YAML order (the scorer's application order)."""
        return tuple(self._buckets)

    def has(self, feature: FeatureName | str) -> bool:
        try:
            return FeatureName(feature) in self._buckets
        except ValueError:
            return False

    def buckets(self, feature: FeatureName | str) -> tuple[str, ...]:
        """Bucket names of one feature in table order (KeyError if no table)."""
        return self._buckets[FeatureName(feature)]

    def likelihood(self, feature: FeatureName | str, bucket: str, hypothesis: Hypothesis) -> float:
        """P(bucket | hypothesis). KeyError for an unknown feature or bucket."""
        name = FeatureName(feature)
        return self._rows[name][Hypothesis(hypothesis)][self._index[name][bucket]]

    def row(self, feature: FeatureName | str, hypothesis: Hypothesis) -> Mapping[str, float]:
        """One hypothesis row as a read-only ``bucket -> P(bucket | h)`` map."""
        name = FeatureName(feature)
        cells = self._rows[name][Hypothesis(hypothesis)]
        return MappingProxyType(dict(zip(self._buckets[name], cells, strict=True)))


def load_likelihood_tables(path: str | Path = LIKELIHOODS_PATH) -> LikelihoodTables:
    """Load and validate ``config/likelihoods.yaml``.

    Raises ``LikelihoodTableError(feature, hypothesis, sum)`` when a row has a negative
    cell or is off 1 by more than ``ROW_SUM_TOL``. The raw file is checked before the
    WO-010 shape loader so a negative cell is reported as a row error too.
    """
    doc = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    for feature, block in (doc.get("features") or {}).items():
        for key, cells in dict(block).items():
            if key in ("buckets", "thresholds") or not isinstance(cells, list):
                continue
            _check_row(str(feature), str(key), cells)
    return LikelihoodTables(load_likelihood_spec(path))


# --- priors ------------------------------------------------------------------------------


class PriorConfig:
    """``config/priors.yaml``: the base prior and its adjustments in YAML order."""

    def __init__(self, priors: Priors) -> None:
        cells = [priors.base[h] for h in HYPOTHESES]
        _check_row(PRIOR_BASE, None, cells)
        self.base: Mapping[Hypothesis, float] = MappingProxyType(
            {h: float(priors.base[h]) for h in HYPOTHESES}
        )
        self.adjustments: tuple[tuple[str, PriorAdjustment], ...] = tuple(
            priors.adjustments.items()
        )


def load_prior_config(path: str | Path = PRIORS_PATH) -> PriorConfig:
    """Load ``config/priors.yaml``; raises ``LikelihoodTableError`` (feature
    ``priors.base``) when ``base`` is off 1 by more than ``ROW_SUM_TOL``."""
    doc = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    base = doc.get("base") if isinstance(doc, dict) else None
    if isinstance(base, dict):
        _check_row(PRIOR_BASE, None, list(base.values()))
    return PriorConfig(load_priors(path))


def _known_number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str):
        return None  # "unknown" (or any non-number) never satisfies a condition
    if isinstance(value, int | float) and math.isfinite(float(value)):
        return float(value)
    return None


def applied_adjustments(
    cfg: PriorConfig, hour_of_day: Any, zone_criticality: Any
) -> tuple[str, ...]:
    """Names of the adjustments whose conditions hold, in YAML order.

    ``night_hours``-style entries need a known ``hour_of_day`` in ``hours``;
    ``critical_zone``-style entries need a known ``zone_criticality`` at or above
    ``zone_criticality_min``. An entry with both conditions needs both. An ``unknown``
    or absent condition, or an entry with no condition, is skipped, never assumed true.
    """
    hour = _known_number(hour_of_day)
    crit = _known_number(zone_criticality)
    names: list[str] = []
    for name, adj in cfg.adjustments:
        if adj.hours is None and adj.zone_criticality_min is None:
            continue
        if adj.hours is not None and (hour is None or int(hour) != hour or hour not in adj.hours):
            continue
        if adj.zone_criticality_min is not None and (
            crit is None or crit < adj.zone_criticality_min
        ):
            continue
        names.append(name)
    return tuple(names)


def conditioned_prior(cfg: PriorConfig, hour_of_day: Any, zone_criticality: Any) -> Posterior:
    """``base`` times every adjustment whose condition holds (YAML order), renormalized."""
    weights = dict(cfg.base)
    by_name = dict(cfg.adjustments)
    for name in applied_adjustments(cfg, hour_of_day, zone_criticality):
        for hyp, factor in by_name[name].multipliers.items():
            weights[hyp] *= float(factor)
    return Posterior.from_weights(weights)


# --- posterior ---------------------------------------------------------------------------


def _entropy_bits(probs: Sequence[float]) -> float:
    return -math.fsum(p * math.log2(p) for p in probs if p > 0.0)


class Posterior(StrictModel):
    """A probability vector over ``HYPOTHESES`` (spec order), immutable.

    Probabilities are non-negative and sum to 1 within ``PROB_SUM_TOL``.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=False, allow_inf_nan=False)

    probs: dict[Hypothesis, float] = Field(...)

    @field_validator("probs")
    @classmethod
    def _valid(cls, v: dict[Hypothesis, float]) -> dict[Hypothesis, float]:
        if set(v) != set(HYPOTHESES):
            raise ValueError(f"probs keys must be exactly the {len(HYPOTHESES)} hypotheses")
        ordered = {h: float(v[h]) for h in HYPOTHESES}
        if any(p < 0.0 for p in ordered.values()):
            raise ValueError("probabilities must be non-negative")
        total = math.fsum(ordered.values())
        if abs(total - 1.0) > PROB_SUM_TOL:
            raise ValueError(f"probabilities sum to {total!r}, not 1 within {PROB_SUM_TOL}")
        return ordered

    @classmethod
    def from_weights(cls, weights: Mapping[Hypothesis, float]) -> Posterior:
        """Normalize non-negative weights; raises ``DegenerateUpdateError`` if all are 0."""
        total = math.fsum(weights[h] for h in HYPOTHESES)
        if not total > 0.0:
            raise DegenerateUpdateError("every hypothesis has weight 0")
        return cls(probs={h: weights[h] / total for h in HYPOTHESES})

    @classmethod
    def uniform(cls) -> Posterior:
        return cls(probs={h: 1.0 / len(HYPOTHESES) for h in HYPOTHESES})

    def p(self, hypothesis: Hypothesis) -> float:
        return self.probs[hypothesis]

    def mass(self, hypotheses: Sequence[Hypothesis]) -> float:
        return math.fsum(self.probs[h] for h in hypotheses)

    def update(
        self,
        feature: FeatureName | str,
        bucket: str,
        confidence: float,
        tables: LikelihoodTables,
    ) -> Posterior:
        """A new posterior after one row: weight_h = p_h * P(bucket | h) ** confidence.

        Raises ``DegenerateUpdateError`` when every weight is 0 (the caller keeps this
        posterior and records the row) and ``ValueError`` for a confidence outside [0, 1].
        """
        c = float(confidence)
        if not 0.0 <= c <= 1.0:
            raise ValueError(f"confidence must lie in [0, 1], got {confidence!r}")
        weights = {
            h: self.probs[h] * tables.likelihood(feature, bucket, h) ** c for h in HYPOTHESES
        }
        return Posterior.from_weights(weights)

    def entropy(self) -> float:
        """Shannon entropy in bits (0 log 0 = 0)."""
        return _entropy_bits(list(self.probs.values()))

    def ranked(self) -> tuple[tuple[Hypothesis, float], ...]:
        """Hypotheses by descending probability; ties keep spec order."""
        order = {h: i for i, h in enumerate(HYPOTHESES)}
        return tuple(sorted(self.probs.items(), key=lambda kv: (-kv[1], order[kv[0]])))

    def top(self) -> Hypothesis:
        return self.ranked()[0][0]

    def top2_margin(self) -> float:
        ranked = self.ranked()
        return ranked[0][1] - ranked[1][1]

    def threat_mass(self) -> float:
        """P(UNAUTHORIZED_UUV) + P(UNAUTHORIZED_DIVER)."""
        return self.mass(THREAT_HYPOTHESES)

    def benign_mass(self) -> float:
        """P(FILED_ON_PLAN) + P(BIOLOGIC) + P(DEBRIS_OR_DRIFT) + P(SURFACE_VESSEL_AIS_OFF)."""
        return self.mass(BENIGN_HYPOTHESES)

    def rounded(self) -> dict[str, float]:
        """``hypothesis -> round6(p)`` in spec order."""
        return {h.value: round6(p) for h, p in self.probs.items()}

    def to_dict(self) -> dict[str, Any]:
        """6-dp output with the stress-test label."""
        return {
            "label": STRESS_TEST_LABEL,
            "posterior": self.rounded(),
            "entropy_bits": round6(self.entropy()),
            "top2_margin": round6(self.top2_margin()),
            "threat_mass": round6(self.threat_mass()),
            "benign_mass": round6(self.benign_mass()),
            "top": self.top().value,
        }


class PosteriorReport(StrictModel):
    """One contact's prior, posterior and the rows behind it.

    ``used_row_ids`` are in application order; ``buckets`` maps each used feature to its
    bucket; ``skipped`` maps feature name to reason; ``degenerate_row_ids`` lists rows
    that would have zeroed every hypothesis (posterior left unchanged).
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=False, allow_inf_nan=False)

    contact_id: str
    prior: Posterior
    posterior: Posterior
    prior_adjustments: tuple[str, ...] = ()
    used_row_ids: tuple[str, ...] = ()
    buckets: dict[str, str] = Field(default_factory=dict)
    skipped: dict[str, str] = Field(default_factory=dict)
    degenerate_row_ids: tuple[str, ...] = ()
    label: str = STRESS_TEST_LABEL

    def to_dict(self) -> dict[str, Any]:
        """The posterior's ``to_dict`` plus provenance, 6 dp, labeled."""
        return {
            **self.posterior.to_dict(),
            "contact_id": self.contact_id,
            "prior": self.prior.rounded(),
            "prior_adjustments": list(self.prior_adjustments),
            "used_row_ids": list(self.used_row_ids),
            "buckets": dict(self.buckets),
            "skipped": dict(self.skipped),
            "degenerate_row_ids": list(self.degenerate_row_ids),
        }


def _latest_value(table: EvidenceTable, contact_id: str, feature: FeatureName) -> Any:
    row = table.latest(contact_id, feature)
    return None if row is None else row.value


def score_contact(
    table: EvidenceTable,
    contact_id: str,
    tables: LikelihoodTables,
    prior_cfg: PriorConfig,
) -> PosteriorReport:
    """Score one contact from its evidence rows only.

    The prior is ``conditioned_prior`` with the contact's latest ``hour_of_day`` and
    ``zone_criticality`` rows (unknown or absent: that adjustment is skipped). Then one
    row per likelihood feature (``table.latest``) in ``tables.features()`` order.
    """
    hour = _latest_value(table, contact_id, _NIGHT_FEATURE)
    crit = _latest_value(table, contact_id, _ZONE_FEATURE)
    adjustments = applied_adjustments(prior_cfg, hour, crit)
    prior = conditioned_prior(prior_cfg, hour, crit)

    post = prior
    used: list[str] = []
    buckets: dict[str, str] = {}
    skipped: dict[str, str] = {}
    degenerate: list[str] = []
    for feature in tables.features():
        row = table.latest(contact_id, feature)
        if row is None:
            skipped[feature.value] = SKIP_ABSENT
            continue
        bucket = bucketize(feature.value, row.value)
        if bucket is None:
            skipped[feature.value] = SKIP_UNKNOWN
            continue
        if isinstance(row.confidence, str) and row.confidence == UNKNOWN:
            skipped[feature.value] = SKIP_UNKNOWN_CONFIDENCE
            continue
        try:
            post = post.update(feature, bucket, float(row.confidence), tables)
        except DegenerateUpdateError:
            degenerate.append(row.row_id)
            continue
        used.append(row.row_id)
        buckets[feature.value] = bucket
    present = sorted({r.feature.value for r in table.for_contact(contact_id)})
    for name in present:
        if not tables.has(name):
            skipped[name] = SKIP_NO_TABLE
    return PosteriorReport(
        contact_id=contact_id,
        prior=prior,
        posterior=post,
        prior_adjustments=adjustments,
        used_row_ids=tuple(used),
        buckets=buckets,
        skipped=dict(sorted(skipped.items())),
        degenerate_row_ids=tuple(degenerate),
    )


def expected_entropy_after(
    posterior: Posterior,
    feature: FeatureName | str,
    tables: LikelihoodTables,
    delta_c: float = 1.0,
) -> float:
    """Sum over buckets b of P(b) * H(posterior updated with b at confidence ``delta_c``).

    P(b) = sum_h P(h) * P(b | h). Buckets with P(b) = 0 contribute nothing. Pure; the
    WO-031 planner builds information gain on it. Returns an unrounded float.
    """
    terms: list[float] = []
    for bucket in tables.buckets(feature):
        pb = math.fsum(
            posterior.probs[h] * tables.likelihood(feature, bucket, h) for h in HYPOTHESES
        )
        if pb <= 0.0:
            continue
        try:
            after = posterior.update(feature, bucket, delta_c, tables)
        except DegenerateUpdateError:  # cannot happen when pb > 0; kept for safety
            after = posterior
        terms.append(pb * after.entropy())
    return math.fsum(terms)


# --- CLI ---------------------------------------------------------------------------------


def score_run_table(
    table: EvidenceTable, tables: LikelihoodTables, prior_cfg: PriorConfig
) -> list[PosteriorReport]:
    """``score_contact`` for every contact in the table, in sorted id order."""
    return [score_contact(table, cid, tables, prior_cfg) for cid in table.contacts()]


def main(argv: list[str] | None = None) -> int:
    """``python -m soundings.investigation.posterior --scenario S05 --seed 11``.

    The simulator wiring lives in the ``soundings.dumps`` composition root, imported only
    when the command runs; this module imports nothing from ``soundings.sim``.
    """
    from soundings.dumps import posterior_dump

    return posterior_dump(argv)


__all__ = [
    "BENIGN_HYPOTHESES",
    "HYPOTHESES",
    "PRIOR_BASE",
    "PROB_SUM_TOL",
    "ROW_SUM_TOL",
    "SKIP_ABSENT",
    "SKIP_NO_TABLE",
    "SKIP_UNKNOWN",
    "SKIP_UNKNOWN_CONFIDENCE",
    "STRESS_TEST_LABEL",
    "THREAT_HYPOTHESES",
    "DegenerateUpdateError",
    "LikelihoodTableError",
    "LikelihoodTables",
    "Posterior",
    "PosteriorReport",
    "PriorConfig",
    "applied_adjustments",
    "conditioned_prior",
    "expected_entropy_after",
    "load_likelihood_tables",
    "load_prior_config",
    "main",
    "score_contact",
    "score_run_table",
]


if __name__ == "__main__":
    raise SystemExit(main())
