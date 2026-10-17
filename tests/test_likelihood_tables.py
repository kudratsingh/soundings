"""WO-030: the likelihood and prior loaders guard the catalog (row sums within 1e-6)."""

from __future__ import annotations

import math
from pathlib import Path

import pytest
import yaml

from soundings.investigation.features import BUCKETS
from soundings.investigation.posterior import (
    HYPOTHESES,
    PRIOR_BASE,
    ROW_SUM_TOL,
    LikelihoodTableError,
    LikelihoodTables,
    load_likelihood_tables,
    load_prior_config,
)
from soundings.schemas.config import LIKELIHOODS_PATH, PRIORS_PATH, load_likelihood_spec
from soundings.schemas.enums import FeatureName, Hypothesis

FIXTURES = Path(__file__).parent / "fixtures" / "config"
BAD_ROWSUM = FIXTURES / "likelihoods_bad_rowsum.yaml"
BAD_PRIOR = FIXTURES / "priors_bad_sum.yaml"


@pytest.fixture(scope="module")
def tables() -> LikelihoodTables:
    return load_likelihood_tables()


def test_every_likelihood_row_sums_to_one(tables: LikelihoodTables) -> None:
    assert tables.features(), "no likelihood features loaded"
    for feature in tables.features():
        for hyp in HYPOTHESES:
            row = tables.row(feature, hyp)
            assert list(row) == list(tables.buckets(feature))
            assert all(p >= 0.0 for p in row.values()), (feature, hyp)
            assert abs(math.fsum(row.values()) - 1.0) <= ROW_SUM_TOL, (feature, hyp)


def test_raw_yaml_rows_sum_to_one() -> None:
    """Independent of the loader: read the file directly."""
    doc = yaml.safe_load(LIKELIHOODS_PATH.read_text(encoding="utf-8"))
    for feature, block in doc["features"].items():
        for hyp in HYPOTHESES:
            cells = block[hyp.value]
            assert abs(math.fsum(cells) - 1.0) <= ROW_SUM_TOL, (feature, hyp)


def test_priors_base_sums_to_one() -> None:
    cfg = load_prior_config()
    assert list(cfg.base) == list(HYPOTHESES)
    assert abs(math.fsum(cfg.base.values()) - 1.0) <= ROW_SUM_TOL
    assert [name for name, _ in cfg.adjustments] == ["night_hours", "critical_zone"]
    assert PRIORS_PATH.exists()


def test_bad_rowsum_fixture_raises_naming_feature_and_hypothesis() -> None:
    with pytest.raises(LikelihoodTableError) as info:
        load_likelihood_tables(BAD_ROWSUM)
    err = info.value
    assert err.feature == "drift_consistency"
    assert err.hypothesis == "DEBRIS_OR_DRIFT"
    assert err.total == pytest.approx(1.05)
    assert "drift_consistency" in str(err) and "DEBRIS_OR_DRIFT" in str(err)


def test_bad_prior_fixture_raises_naming_base() -> None:
    with pytest.raises(LikelihoodTableError) as info:
        load_prior_config(BAD_PRIOR)
    err = info.value
    assert err.feature == PRIOR_BASE
    assert err.hypothesis is None
    assert err.total == pytest.approx(1.05)
    assert PRIOR_BASE in str(err)


def test_negative_cell_raises(tmp_path: Path) -> None:
    text = LIKELIHOODS_PATH.read_text(encoding="utf-8")
    old = "    UNAUTHORIZED_UUV:        [0.85, 0.12, 0.03]"
    assert text.count(old) == 1
    path = tmp_path / "likelihoods_negative.yaml"
    path.write_text(text.replace(old, "    UNAUTHORIZED_UUV:        [1.10, -0.13, 0.03]"))
    with pytest.raises(LikelihoodTableError) as info:
        load_likelihood_tables(path)
    assert info.value.feature == "drift_consistency"
    assert info.value.hypothesis == "UNAUTHORIZED_UUV"


def test_off_by_less_than_tolerance_is_accepted(tmp_path: Path) -> None:
    text = LIKELIHOODS_PATH.read_text(encoding="utf-8")
    old = "    UNAUTHORIZED_UUV:        [0.85, 0.12, 0.03]"
    path = tmp_path / "likelihoods_close.yaml"
    path.write_text(text.replace(old, "    UNAUTHORIZED_UUV:        [0.8500004, 0.12, 0.03]"))
    load_likelihood_tables(path)  # 4e-7 off: within 1e-6


def test_tables_api_matches_yaml_and_feature_buckets(tables: LikelihoodTables) -> None:
    spec = load_likelihood_spec()
    assert tables.features() == tuple(spec.features)
    assert tables.features()[0] is FeatureName.DRIFT_CONSISTENCY
    assert tables.likelihood("depth", "deep", Hypothesis.UNAUTHORIZED_UUV) == 0.30
    assert tables.row(FeatureName.HAIL_REPLY, Hypothesis.FILED_ON_PLAN)["valid"] == 0.90
    assert tables.has("depth") and not tables.has("zone_id") and not tables.has("nonsense")
    for feature in tables.features():  # the scorer's bucketize agrees with the tables
        assert BUCKETS[feature].names == tables.buckets(feature)
    with pytest.raises(KeyError):
        tables.likelihood("depth", "abyssal", Hypothesis.BIOLOGIC)
    with pytest.raises(TypeError):
        tables.row("depth", Hypothesis.BIOLOGIC)["deep"] = 0.5  # type: ignore[index]
