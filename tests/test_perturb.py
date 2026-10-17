"""WO-051 unit tests: seeded likelihood perturbation, acoustic unpinning, table shift."""

from __future__ import annotations

import pytest

from soundings.canonical import canonical_json
from soundings.eval.perturb import (
    ACOUSTIC_PIN_FEATURES,
    perturb_likelihoods,
    table_shift,
    unpin_acoustics,
)
from soundings.schemas import LikelihoodSpec, load_likelihood_spec
from soundings.sim.scenario import resolve_scenario


@pytest.fixture(scope="module")
def spec() -> LikelihoodSpec:
    return load_likelihood_spec()


@pytest.mark.parametrize("magnitude", [0.1, 0.5, 1.0, 2.0])
def test_rows_sum_to_one_and_buckets_positive(spec: LikelihoodSpec, magnitude: float) -> None:
    out = perturb_likelihoods(spec, magnitude, 17)
    assert set(out.features) == set(spec.features)
    for feature, block in out.features.items():
        assert block.buckets == spec.features[feature].buckets
        for hyp, row in block.rows.items():
            assert f"{sum(row):.6f}" == "1.000000", (feature, hyp, row)
            assert round(sum(row), 6) == 1.0
            assert all(p > 0.0 for p in row), (feature, hyp, row)
            assert all(round(p, 6) == p for p in row)


def test_magnitude_zero_is_identity(spec: LikelihoodSpec) -> None:
    assert perturb_likelihoods(spec, 0.0, 17) == spec


def test_same_seed_same_table_different_seed_different(spec: LikelihoodSpec) -> None:
    a = perturb_likelihoods(spec, 0.5, 17)
    b = perturb_likelihoods(spec, 0.5, 17)
    c = perturb_likelihoods(spec, 0.5, 18)
    assert a == b
    assert canonical_json(a) == canonical_json(b)
    assert a != c


def test_shift_grows_with_magnitude(spec: LikelihoodSpec) -> None:
    shifts = [table_shift(spec, perturb_likelihoods(spec, m, 17)) for m in (0.1, 0.5, 1.0)]
    means = [s.mean_abs for s in shifts]
    assert means[0] < means[1] < means[2]
    assert all(s.cells > 0 for s in shifts)
    assert table_shift(spec, spec).max_abs == 0.0
    assert table_shift(spec, spec).mean_abs == 0.0


def test_input_spec_not_mutated(spec: LikelihoodSpec) -> None:
    before = canonical_json(spec)
    perturb_likelihoods(spec, 1.0, 3)
    assert canonical_json(spec) == before


def test_negative_magnitude_refused(spec: LikelihoodSpec) -> None:
    with pytest.raises(ValueError):
        perturb_likelihoods(spec, -0.1, 1)


def test_unpin_s05_leaves_no_pinned_feature_with_a_table(spec: LikelihoodSpec) -> None:
    s05 = resolve_scenario("S05")
    before = canonical_json(s05)
    assert any(c.acoustic.propeller_tonal is not None for c in s05.contacts)
    out = unpin_acoustics(s05, spec)
    for contact in out.contacts:
        for field, feature in ACOUSTIC_PIN_FEATURES.items():
            if feature in spec.features:
                assert getattr(contact.acoustic, field) is None, field
    assert canonical_json(s05) == before  # input not mutated
    assert [c.id for c in out.contacts] == [c.id for c in s05.contacts]
    assert out.model_copy(update={"contacts": s05.contacts}) == s05


def test_unpin_default_spec_matches_config(spec: LikelihoodSpec) -> None:
    s03 = resolve_scenario("S03")
    assert unpin_acoustics(s03) == unpin_acoustics(s03, spec)
