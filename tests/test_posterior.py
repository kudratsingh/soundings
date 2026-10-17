"""WO-030: naive-Bayes posterior over the eight hypotheses (SPEC-INVESTIGATION section 3)."""

from __future__ import annotations

import math
from collections.abc import Iterable, Iterator
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from soundings.canonical import canonical_json
from soundings.investigation.evidence import EvidenceTable
from soundings.investigation.posterior import (
    BENIGN_HYPOTHESES,
    HYPOTHESES,
    SKIP_ABSENT,
    SKIP_NO_TABLE,
    SKIP_UNKNOWN,
    SKIP_UNKNOWN_CONFIDENCE,
    STRESS_TEST_LABEL,
    THREAT_HYPOTHESES,
    DegenerateUpdateError,
    LikelihoodTables,
    Posterior,
    PosteriorReport,
    PriorConfig,
    applied_adjustments,
    conditioned_prior,
    expected_entropy_after,
    load_likelihood_tables,
    load_prior_config,
    score_contact,
)
from soundings.schemas.config import Priors
from soundings.schemas.enums import EvidenceKind, FeatureName, Rung, Unit
from soundings.schemas.evidence import UNKNOWN, EvidenceRow, EvidenceValue

H = {h.name: h for h in HYPOTHESES}
FOP = H["FILED_ON_PLAN"]
FOFF = H["FILED_OFF_PLAN"]
UKO = H["UNFILED_KNOWN_OPERATOR"]
BIO = H["BIOLOGIC"]
DEB = H["DEBRIS_OR_DRIFT"]
SURF = H["SURFACE_VESSEL_AIS_OFF"]
UUV = H["UNAUTHORIZED_UUV"]
DIV = H["UNAUTHORIZED_DIVER"]

CID = "c-0001"

TABLES = load_likelihood_tables()
PRIORS = load_prior_config()


# --- helpers -----------------------------------------------------------------------------


def _unit(value: EvidenceValue) -> Unit:
    if isinstance(value, bool):
        return Unit.BOOL
    if isinstance(value, str):
        return Unit.UNKNOWN if value == UNKNOWN else Unit.CATEGORY
    return Unit.RATIO


def row(
    feature: str,
    value: EvidenceValue,
    confidence: float | str = 1.0,
    *,
    t: int = 10,
    contact_id: str = CID,
    source: str = "test",
) -> EvidenceRow:
    return EvidenceRow(
        contact_id=contact_id,
        t=t,
        feature=FeatureName(feature),
        value=value,
        unit=_unit(value),
        source=source,
        confidence=confidence,
        kind=EvidenceKind.FACT,
        probe_id=None,
    )


def table_of(rows: Iterable[EvidenceRow]) -> EvidenceTable:
    table = EvidenceTable()
    table.extend(list(rows))
    return table


def score(rows: Iterable[EvidenceRow], prior_cfg: PriorConfig = PRIORS) -> PosteriorReport:
    return score_contact(table_of(rows), CID, TABLES, prior_cfg)


def base() -> Posterior:
    return conditioned_prior(PRIORS, None, None)


def mass(post: Posterior, hyps: Iterable[Any]) -> float:
    return math.fsum(post.probs[h] for h in hyps)


def kl(p: Posterior, q: Posterior) -> float:
    return math.fsum(
        p.probs[h] * math.log(p.probs[h] / q.probs[h]) for h in HYPOTHESES if p.probs[h] > 0
    )


def walk(obj: Any) -> Iterator[Any]:
    yield obj
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield k
            yield from walk(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            yield from walk(v)


# --- normalization -----------------------------------------------------------------------

_FEATURES = TABLES.features()


@st.composite
def _rows(draw: st.DrawFn) -> list[tuple[FeatureName, str, float]]:
    chosen = draw(st.lists(st.sampled_from(_FEATURES), unique=True, max_size=len(_FEATURES)))
    out: list[tuple[FeatureName, str, float]] = []
    for feature in chosen:
        bucket = draw(st.sampled_from(TABLES.buckets(feature)))
        c = draw(st.floats(min_value=0.0, max_value=1.0, allow_nan=False))
        out.append((feature, bucket, c))
    return out


@settings(derandomize=True, max_examples=200, deadline=None)
@given(
    updates=_rows(),
    hour=st.one_of(st.none(), st.integers(0, 23)),
    crit=st.one_of(st.none(), st.just(UNKNOWN), st.floats(0.0, 1.0, allow_nan=False)),
)
def test_sums_to_one_property(
    updates: list[tuple[FeatureName, str, float]], hour: int | None, crit: float | str | None
) -> None:
    post = conditioned_prior(PRIORS, hour, crit)
    assert abs(math.fsum(post.probs.values()) - 1.0) <= 1e-9
    for feature, bucket, c in updates:
        try:
            post = post.update(feature, bucket, c, TABLES)
        except DegenerateUpdateError:
            continue
        assert abs(math.fsum(post.probs.values()) - 1.0) <= 1e-9
        assert all(p >= 0.0 for p in post.probs.values())
    assert list(post.probs) == list(HYPOTHESES)


def test_sums_to_one_report_and_rounded_output() -> None:
    report = score([row("depth", 15.0), row("hail_reply", "none", 0.9), row("loiter_score", 0.8)])
    assert abs(math.fsum(report.posterior.probs.values()) - 1.0) <= 1e-9
    out = report.to_dict()
    assert abs(sum(out["posterior"].values()) - 1.0) <= 1e-5  # 8 cells rounded to 6 dp
    assert all(round(v, 6) == v for v in out["posterior"].values())


def test_posterior_constructor_rejects_bad_vectors() -> None:
    good = dict(PRIORS.base)
    Posterior(probs=good)
    with pytest.raises(ValueError):
        Posterior(probs={**good, FOP: good[FOP] + 0.01})
    with pytest.raises(ValueError):
        Posterior(probs={**good, FOP: -0.01, FOFF: good[FOFF] + good[FOP] + 0.01})
    with pytest.raises(ValueError):
        Posterior(probs={h: p for h, p in good.items() if h is not UUV})
    with pytest.raises(ValueError):
        base().update("depth", "deep", 1.5, TABLES)


# --- direction (SPEC-INVESTIGATION section 3) -------------------------------------------

FILED_ALL = (FOP, FOFF, UKO)
UUV_OR_FILED = (UUV, FOP, FOFF)

# (case id, rows added at c = 1, hypotheses whose mass must change, +1 rise / -1 fall)
DIRECTION_CASES: list[tuple[str, list[tuple[str, EvidenceValue]], tuple[Any, ...], int]] = [
    ("01_hail_valid_filed_or_known", [("hail_reply", "valid")], FILED_ALL, +1),
    ("02_drift_high_debris", [("drift_consistency", 0.9)], (DEB,), +1),
    (
        "03_tonal_and_low_drift_uuv_or_filed",
        [("propeller_tonal_present", True), ("drift_consistency", 0.1)],
        UUV_OR_FILED,
        +1,
    ),
    ("04_breathing_biologic", [("breathing_or_click_present", True)], (BIO,), +1),
    ("05_registry_exact_on_plan", [("registry_match", "exact")], (FOP,), +1),
    ("06_registry_area_mismatch_off_plan", [("registry_match", "area_mismatch")], (FOFF,), +1),
    ("07_ais_strong_surface", [("ais_surface_correlation", 0.9)], (SURF,), +1),
    ("08_depth_deep_uuv", [("depth", 15.0)], (UUV,), +1),
    ("09_orbit_uuv", [("transit_pattern", "orbit")], (UUV,), +1),
    ("10_pol_anomalous_threat", [("pattern_of_life_anomaly", 0.95)], THREAT_HYPOTHESES, +1),
    ("11_speed_drift_debris", [("speed_over_ground", 0.1)], (DEB,), +1),
    ("12_lawnmower_on_plan", [("transit_pattern", "lawnmower")], (FOP,), +1),
    ("13_hail_none_lowers_on_plan", [("hail_reply", "none")], (FOP,), -1),
    ("14_loiter_high_threat", [("loiter_score", 0.9)], THREAT_HYPOTHESES, +1),
]


@pytest.mark.parametrize(
    ("rows", "hyps", "sign"),
    [pytest.param(r, h, s, id=i) for i, r, h, s in DIRECTION_CASES],
)
def test_direction(rows: list[tuple[str, EvidenceValue]], hyps: tuple[Any, ...], sign: int) -> None:
    prior = base()
    assert prior.probs == pytest.approx(dict(PRIORS.base), abs=1e-12)  # no adjustments
    report = score(row(f, v, 1.0, t=10 + i) for i, (f, v) in enumerate(rows))
    assert report.prior_adjustments == ()
    assert len(report.used_row_ids) == len(rows)
    before, after = mass(prior, hyps), mass(report.posterior, hyps)
    if sign > 0:
        assert after > before, (before, after)
    else:
        assert after < before, (before, after)


def test_direction_has_fourteen_cases() -> None:
    assert len(DIRECTION_CASES) == 14


def test_direction_aggregates_match_spec_sums() -> None:
    post = score([row("depth", 15.0), row("loiter_score", 0.9)]).posterior
    assert post.threat_mass() == pytest.approx(post.probs[UUV] + post.probs[DIV], abs=1e-15)
    assert post.benign_mass() == pytest.approx(
        post.probs[FOP] + post.probs[BIO] + post.probs[DEB] + post.probs[SURF], abs=1e-15
    )
    assert set(THREAT_HYPOTHESES) | set(BENIGN_HYPOTHESES) == set(HYPOTHESES) - {FOFF, UKO}


# --- conflict widens entropy -------------------------------------------------------------


def _chain(*rows: tuple[str, str]) -> Posterior:
    post = base()
    for feature, bucket in rows:
        post = post.update(feature, bucket, 1.0, TABLES)
    return post


@pytest.mark.parametrize(
    ("first", "second"),
    [
        (("propeller_tonal_present", "true"), ("drift_consistency", "high")),
        (("breathing_or_click_present", "true"), ("depth", "deep")),
    ],
)
def test_conflict_widens_entropy(first: tuple[str, str], second: tuple[str, str]) -> None:
    alone = _chain(first)
    both = _chain(first, second)
    assert both.entropy() > alone.entropy()
    assert both.top2_margin() < alone.top2_margin()


def test_conflict_widens_entropy_wo_order_drift_then_tonal() -> None:
    """The WO's literal order: drift high alone, then + tonal true.

    From the base prior the margin collapses (0.255 -> 0.022) but entropy dips 0.09 bits
    because tonal=true also prunes the BIOLOGIC/DEBRIS tail; the posterior stays wide
    (about 1.95 bits) and the two leaders end nearly tied. See DECISIONS 2026-10-16 23:40 (WO-030).
    """
    alone = _chain(("drift_consistency", "high"))
    both = _chain(("drift_consistency", "high"), ("propeller_tonal_present", "true"))
    assert both.top2_margin() < alone.top2_margin()
    assert both.top2_margin() < 0.05
    assert both.entropy() > 1.9


def test_conflict_widens_entropy_more_than_concordant_evidence() -> None:
    conflict = _chain(("propeller_tonal_present", "true"), ("drift_consistency", "high"))
    concord = _chain(("propeller_tonal_present", "true"), ("drift_consistency", "low"))
    assert conflict.entropy() > concord.entropy()
    conflict2 = _chain(("breathing_or_click_present", "true"), ("depth", "deep"))
    concord2 = _chain(("breathing_or_click_present", "true"), ("depth", "surface"))
    assert conflict2.entropy() > concord2.entropy()


# --- missing rows are skipped, never imputed --------------------------------------------


def test_missing_skipped() -> None:
    known = [row("depth", 15.0), row("drift_consistency", 0.1)]
    with_missing = score([*known, row("hail_reply", UNKNOWN, 1.0, source="placeholder:phase2")])
    without = score(known)
    assert with_missing.posterior.probs == without.posterior.probs
    assert with_missing.skipped["hail_reply"] == SKIP_UNKNOWN
    assert with_missing.skipped["registry_match"] == SKIP_ABSENT
    assert without.skipped["hail_reply"] == SKIP_ABSENT
    assert "hail_reply" not in with_missing.buckets


def test_missing_skipped_unknown_confidence() -> None:
    known = [row("depth", 15.0)]
    report = score([*known, row("hail_reply", "none", UNKNOWN)])
    assert report.posterior.probs == score(known).posterior.probs
    assert report.skipped["hail_reply"] == SKIP_UNKNOWN_CONFIDENCE


def test_missing_skipped_no_table_and_latest_only() -> None:
    """zone_id has no table; repeated acoustic readings count once (the latest row)."""
    readings = [row("propeller_tonal_present", v, 0.8, t=t) for t, v in [(1, True), (2, False)]]
    report = score([row("zone_id", "fuel_berth", 1.0), *readings])
    single = score([row("propeller_tonal_present", False, 0.8, t=2)])
    assert report.posterior.probs == single.posterior.probs
    assert report.skipped["zone_id"] == SKIP_NO_TABLE
    assert report.used_row_ids == (readings[1].row_id,)
    assert report.buckets == {"propeller_tonal_present": "false"}


# --- confidence weighting (SPEC-INVESTIGATION 9.10) -------------------------------------


def test_confidence_weighting() -> None:
    prior = base()
    p0 = score([row("depth", 15.0, 0.0)]).posterior
    p1 = score([row("depth", 15.0, 1.0)]).posterior
    ph = score([row("depth", 15.0, 0.5)]).posterior
    assert p0.probs == pytest.approx(prior.probs, abs=1e-12)
    # c = 1 by hand: base x P(deep | h) from config/likelihoods.yaml, renormalized.
    by_hand = {
        FOP: 0.45 * 0.20,
        FOFF: 0.08 * 0.10,
        UKO: 0.07 * 0.20,
        BIO: 0.20 * 0.02,
        DEB: 0.10 * 0.01,
        SURF: 0.05 * 0.00,
        UUV: 0.03 * 0.30,
        DIV: 0.02 * 0.05,
    }
    total = sum(by_hand.values())
    assert p1.probs == pytest.approx({h: w / total for h, w in by_hand.items()}, abs=1e-12)
    assert 0.0 < kl(ph, prior) < kl(p1, prior)
    # Every pairwise odds ratio moves exactly halfway in log space at c = 0.5.
    log_odds = math.log(ph.probs[UUV] / ph.probs[FOP])
    halfway = 0.5 * (
        math.log(p0.probs[UUV] / p0.probs[FOP]) + math.log(p1.probs[UUV] / p1.probs[FOP])
    )
    assert log_odds == pytest.approx(halfway, abs=1e-12)


def test_confidence_weighting_rule_is_in_docstring() -> None:
    import soundings.investigation.posterior as mod

    doc = " ".join((mod.__doc__ or "").split())
    rule = (
        "A row with confidence `c ∈ [0,1]` contributes `P(bucket | h) ** c`. c = 1 is a "
        'full naive-Bayes factor. c = 0 contributes nothing. A confidence of `"unknown"` '
        "is treated as c = 0, and the row is recorded as skipped with reason "
        "`unknown_confidence`."
    )
    assert rule in doc


# --- degenerate update -------------------------------------------------------------------


def _surface_only() -> PriorConfig:
    probs = {h: (1.0 if h is SURF else 0.0) for h in HYPOTHESES}
    return PriorConfig(Priors(base=probs, adjustments={}))


def test_degenerate() -> None:
    depth_mid = row("depth", 6.0)
    report = score([depth_mid], prior_cfg=_surface_only())
    assert report.posterior.probs == report.prior.probs
    assert report.posterior.probs[SURF] == 1.0
    assert report.degenerate_row_ids == (depth_mid.row_id,)
    assert report.used_row_ids == ()
    assert report.to_dict()["degenerate_row_ids"] == [depth_mid.row_id]
    with pytest.raises(DegenerateUpdateError):
        report.prior.update("depth", "mid", 1.0, TABLES)


def test_degenerate_zero_confidence_is_not_degenerate() -> None:
    report = score([row("depth", 6.0, 0.0)], prior_cfg=_surface_only())
    assert report.degenerate_row_ids == ()
    assert report.posterior.probs[SURF] == 1.0


# --- prior adjustments -------------------------------------------------------------------


def test_prior_adjustments_night_and_critical() -> None:
    # Hand-computed: base x night_hours (FOP 0.4, BIO 1.2, UUV 1.5, DIV 1.5)
    # x critical_zone (UUV 1.3, DEB 0.8), then divided by the sum 0.7885.
    weights = {
        FOP: 0.18,
        FOFF: 0.08,
        UKO: 0.07,
        BIO: 0.24,
        DEB: 0.08,
        SURF: 0.05,
        UUV: 0.0585,
        DIV: 0.03,
    }
    assert sum(weights.values()) == pytest.approx(0.7885, abs=1e-12)
    expected = {h: w / 0.7885 for h, w in weights.items()}
    prior = conditioned_prior(PRIORS, 2, 0.8)
    assert prior.probs == pytest.approx(expected, abs=1e-12)
    assert applied_adjustments(PRIORS, 2, 0.8) == ("night_hours", "critical_zone")


def test_prior_adjustments_unknown_applies_neither() -> None:
    prior = conditioned_prior(PRIORS, 12, UNKNOWN)
    assert prior.probs == pytest.approx(dict(PRIORS.base), abs=1e-12)
    assert applied_adjustments(PRIORS, 12, UNKNOWN) == ()
    assert applied_adjustments(PRIORS, UNKNOWN, UNKNOWN) == ()
    assert applied_adjustments(PRIORS, None, 0.7) == ("critical_zone",)  # boundary: >=
    assert applied_adjustments(PRIORS, 22, 0.69) == ("night_hours",)


def test_prior_adjustments_read_from_evidence_rows() -> None:
    rows = [row("hour_of_day", 2), row("zone_criticality", 0.8), row("depth", 15.0)]
    report = score(rows)
    assert report.prior_adjustments == ("night_hours", "critical_zone")
    assert report.prior.probs == pytest.approx(conditioned_prior(PRIORS, 2, 0.8).probs)
    unknown = score([row("hour_of_day", 12), row("zone_criticality", UNKNOWN)])
    assert unknown.prior_adjustments == ()


# --- expected entropy --------------------------------------------------------------------


def _fixed_posteriors() -> list[Posterior]:
    return [
        base(),
        Posterior.uniform(),
        conditioned_prior(PRIORS, 2, 0.8),
        _chain(("drift_consistency", "high")),
        _chain(("propeller_tonal_present", "true"), ("depth", "mid")),
    ]


def test_expected_entropy_never_exceeds_current() -> None:
    for post in _fixed_posteriors():
        h_now = post.entropy()
        for feature in TABLES.features():
            assert expected_entropy_after(post, feature, TABLES) <= h_now + 1e-12, feature


def test_expected_entropy_equals_current_at_zero_delta() -> None:
    for post in _fixed_posteriors():
        for feature in TABLES.features():
            got = expected_entropy_after(post, feature, TABLES, delta_c=0.0)
            assert got == pytest.approx(post.entropy(), abs=1e-12)


def test_expected_entropy_is_informative_somewhere() -> None:
    post = base()
    gains = {f: post.entropy() - expected_entropy_after(post, f, TABLES) for f in _FEATURES}
    assert max(gains.values()) > 0.1


# --- outputs, determinism, hard rules ---------------------------------------------------


def test_entropy_and_margin_definitions() -> None:
    assert Posterior.uniform().entropy() == pytest.approx(3.0)
    assert Posterior.uniform().top2_margin() == pytest.approx(0.0)
    assert Posterior.uniform().top() is FOP  # ties keep spec order
    certain = _surface_only()
    post = conditioned_prior(certain, None, None)
    assert post.entropy() == 0.0
    assert post.top2_margin() == 1.0
    assert post.top() is SURF


def test_to_dict_keys_and_rounding() -> None:
    out = score([row("depth", 15.0)]).posterior.to_dict()
    assert set(out) == {
        "posterior",
        "entropy_bits",
        "top2_margin",
        "threat_mass",
        "benign_mass",
        "top",
        "label",
    }
    assert list(out["posterior"]) == [h.value for h in HYPOTHESES]
    for key in ("entropy_bits", "top2_margin", "threat_mass", "benign_mass"):
        assert round(out[key], 6) == out[key]


def test_determinism_same_rows_any_append_order() -> None:
    rows = [
        row("loiter_score", 0.8, 0.4),
        row("depth", 15.0, 0.7),
        row("propeller_tonal_present", True, 0.9),
        row("hour_of_day", 2),
    ]
    a = score_contact(table_of(rows), CID, TABLES, PRIORS)
    b = score_contact(table_of(reversed(rows)), CID, TABLES, PRIORS)
    c = score_contact(table_of(rows), CID, load_likelihood_tables(), load_prior_config())
    assert canonical_json(a.to_dict()) == canonical_json(c.to_dict())
    assert a.posterior.probs == b.posterior.probs  # fixed application order: byte-stable
    assert canonical_json(a.posterior.to_dict()) == canonical_json(b.posterior.to_dict())


def test_no_rung_in_output() -> None:
    report = score([row("depth", 15.0), row("hail_reply", "none", 0.9), row("hour_of_day", 2)])
    rung_values = {r.value for r in Rung}
    for out in (report.to_dict(), report.posterior.to_dict()):
        for item in walk(out):
            assert not isinstance(item, Rung)
            assert not (isinstance(item, str) and item in rung_values), item


def test_stress_test_label() -> None:
    report = score([row("depth", 15.0)])
    assert report.label == STRESS_TEST_LABEL == "stress-test estimate"
    assert report.to_dict()["label"] == STRESS_TEST_LABEL
    assert report.posterior.to_dict()["label"] == STRESS_TEST_LABEL
    assert score([]).to_dict()["label"] == STRESS_TEST_LABEL


def test_empty_table_returns_prior() -> None:
    report = score_contact(EvidenceTable(), CID, TABLES, PRIORS)
    assert report.posterior.probs == report.prior.probs
    assert set(report.skipped.values()) == {SKIP_ABSENT}
    assert len(report.skipped) == len(TABLES.features())


def test_tables_type() -> None:
    assert isinstance(TABLES, LikelihoodTables)
