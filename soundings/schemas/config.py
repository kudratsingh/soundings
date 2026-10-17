"""Shape-only loaders for ``config/*.yaml``. Sums and signatures are checked elsewhere.

Likelihood and prior row sums belong to WO-030; the standing-orders signature to WO-041.
These loaders catch typos early: unknown keys, off-vocabulary names, wrong lengths.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any

import yaml
from pydantic import Field, model_validator

from soundings.schemas._base import (
    CONFIG_DIR,
    HarborId,
    NonNegInt,
    PositiveFloat,
    Probability,
    StrictModel,
    from_file_data,
)
from soundings.schemas.denylist import FreeText
from soundings.schemas.enums import FeatureName, Hypothesis, Rung, ToggleName
from soundings.schemas.harbor import Harbor, load_harbor

LIKELIHOODS_PATH: Path = CONFIG_DIR / "likelihoods.yaml"
PRIORS_PATH: Path = CONFIG_DIR / "priors.yaml"
LADDER_PATH: Path = CONFIG_DIR / "ladder.yaml"
STANDING_ORDERS_PATH: Path = CONFIG_DIR / "standing_orders.yaml"

BucketName = Annotated[str, Field(min_length=1)]


def _read_yaml(path: str | Path) -> Any:
    return yaml.safe_load(Path(path).read_text(encoding="utf-8"))


def _require_all_hypotheses(keys: Any, what: str) -> None:
    if set(keys) != set(Hypothesis) or len(keys) != len(Hypothesis):
        raise ValueError(f"{what} keys must be exactly the {len(Hypothesis)} hypotheses")


# --- likelihoods -------------------------------------------------------------------------


class FeatureBuckets(StrictModel):
    buckets: Annotated[tuple[BucketName, ...], Field(min_length=2)]
    thresholds: tuple[float, ...] | None = None
    rows: dict[Hypothesis, tuple[Probability, ...]]

    @model_validator(mode="after")
    def _shape(self) -> FeatureBuckets:
        if len(set(self.buckets)) != len(self.buckets):
            raise ValueError("bucket names must be unique")
        if self.thresholds is not None:
            if len(self.thresholds) != len(self.buckets) - 1:
                raise ValueError("len(thresholds) must equal len(buckets) - 1")
            if any(b <= a for a, b in zip(self.thresholds, self.thresholds[1:], strict=False)):
                raise ValueError("thresholds must rise strictly")
        _require_all_hypotheses(self.rows, "likelihood row")
        for hyp, row in self.rows.items():
            if len(row) != len(self.buckets):
                raise ValueError(f"row {hyp}: {len(row)} entries, {len(self.buckets)} buckets")
        return self


class LikelihoodSpec(StrictModel):
    features: dict[FeatureName, FeatureBuckets]


def _bucket_name(raw: Any) -> str:
    if isinstance(raw, bool):  # PyYAML reads [false, true] as booleans
        return "true" if raw else "false"
    if not isinstance(raw, str):
        raise ValueError(f"bucket name must be a string or boolean, got {raw!r}")
    return raw


def load_likelihood_spec(path: str | Path = LIKELIHOODS_PATH) -> LikelihoodSpec:
    """Load ``config/likelihoods.yaml`` into ``LikelihoodSpec`` (shape checks only)."""
    doc = _read_yaml(path)
    features: dict[str, Any] = {}
    for name, block in doc["features"].items():
        block = dict(block)
        buckets = [_bucket_name(b) for b in block.pop("buckets")]
        thresholds = block.pop("thresholds", None)
        features[name] = {"buckets": buckets, "thresholds": thresholds, "rows": block}
    return LikelihoodSpec.model_validate(from_file_data({"features": features}))


# --- priors ------------------------------------------------------------------------------


class PriorAdjustment(StrictModel):
    hours: tuple[Annotated[int, Field(ge=0, le=23)], ...] | None = None
    zone_criticality_min: Probability | None = None
    multipliers: dict[Hypothesis, PositiveFloat]


class Priors(StrictModel):
    base: dict[Hypothesis, Probability]
    adjustments: dict[HarborId, PriorAdjustment] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _all_hypotheses(self) -> Priors:
        _require_all_hypotheses(self.base, "priors base")
        return self


def load_priors(path: str | Path = PRIORS_PATH) -> Priors:
    """Load ``config/priors.yaml``; ``base`` keys must be exactly the 8 hypotheses."""
    doc = _read_yaml(path)
    adjustments: dict[str, Any] = {}
    for name, block in (doc.get("adjustments") or {}).items():
        block = dict(block)
        conditions = {k: block.pop(k) for k in ("hours", "zone_criticality_min") if k in block}
        adjustments[name] = {**conditions, "multipliers": block}
    data = {"base": doc["base"], "adjustments": adjustments}
    return Priors.model_validate(from_file_data(data))


# --- ladder ------------------------------------------------------------------------------


class RungBoundary(StrictModel):
    platform_alone: tuple[HarborId, ...] = ()
    standing_orders: tuple[HarborId, ...] = ()
    person_approves: tuple[HarborId, ...] = ()


class LadderConfig(StrictModel):
    rungs: tuple[Rung, ...]
    lives_on: tuple[Rung, ...]
    boundary: dict[Rung, RungBoundary]
    criteria: dict[Rung, dict[HarborId, float]]
    stopping: dict[HarborId, float]
    budgets: dict[HarborId, NonNegInt]
    costs: dict[HarborId, float]
    watch_after_handoff: dict[HarborId, float]

    @model_validator(mode="after")
    def _rungs(self) -> LadderConfig:
        if self.rungs != tuple(Rung):
            raise ValueError("ladder rungs must be exactly watch, ask, slow, hold, hand_to_person")
        if not set(self.lives_on) <= set(Rung):
            raise ValueError("lives_on must be rungs")
        if set(self.boundary) != set(Rung):
            raise ValueError("boundary keys must be exactly the rungs")
        return self


def load_ladder_config(path: str | Path = LADDER_PATH) -> LadderConfig:
    """Load ``config/ladder.yaml`` (shape checks only; Phase 4 extends)."""
    return LadderConfig.model_validate(from_file_data(_read_yaml(path)))


# --- standing orders ---------------------------------------------------------------------


class SlowScope(StrictModel):
    basins: tuple[HarborId, ...]
    max_minutes: Annotated[int, Field(gt=0)]


class HoldScope(StrictModel):
    max_gates: NonNegInt
    max_minutes: Annotated[int, Field(gt=0)]


class OrdersScope(StrictModel):
    slow: SlowScope
    hold: HoldScope


class Approver(StrictModel):
    name: FreeText
    channel: HarborId
    timeout_s: PositiveFloat


class DegradedMode(StrictModel):
    when: tuple[ToggleName, ...]
    allowed_rungs: tuple[Rung, ...]
    never: tuple[HarborId, ...] = ()


class Orders(StrictModel):
    signed_by: FreeText
    signed_at: Annotated[str, Field(min_length=1)]
    valid_until: Annotated[str, Field(min_length=1)]
    scope: OrdersScope
    approvers: dict[HarborId, Approver]
    degraded_mode: DegradedMode


class StandingOrders(StrictModel):
    orders: Orders
    signature: Annotated[str, Field(min_length=1)]


def load_standing_orders(
    harbor: Harbor | None = None, path: str | Path = STANDING_ORDERS_PATH
) -> StandingOrders:
    """Load ``config/standing_orders.yaml``; basins must exist in ``harbor``.

    The signature is not verified here (WO-041).
    """
    so = StandingOrders.model_validate(from_file_data(_read_yaml(path)))
    h = harbor if harbor is not None else load_harbor()
    unknown = sorted(set(so.orders.scope.slow.basins) - set(h.basins()))
    if unknown:
        raise ValueError(f"standing orders name basins not in the harbor: {unknown}")
    return so
