"""WO-031: the deterministic probe planner and its budgets (SPEC-INVESTIGATION section 6).

The LLM clause of the BUILD-PLAN line lives in WO-032's tests/test_probe_planner_llm.py.
Likelihood tables come from config/likelihoods.yaml; the posteriors, observed
confidences, ledgers and run keys are hand-built per case.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from pathlib import Path

import pytest
import yaml
from hypothesis import given, settings
from hypothesis import strategies as st

from soundings.investigation.planner import (
    DENOM_FLOOR,
    GAIN_EPS,
    REASON_NO_PROBE,
    BudgetExceeded,
    BudgetLedger,
    BudgetLimits,
    CostWeights,
    DeterministicPlanner,
    Planner,
    PlannerChoice,
    PlannerState,
    expected_information_gain,
    load_budget_limits,
    load_cost_weights,
    probe_cost,
    probe_score,
)
from soundings.investigation.posterior import (
    HYPOTHESES,
    LikelihoodTables,
    Posterior,
    conditioned_prior,
    expected_entropy_after,
    load_likelihood_tables,
    load_prior_config,
)
from soundings.investigation.probes import ALLOWLIST, idempotency_key, rung_index, tick_bucket
from soundings.schemas.enums import FeatureName, ProbeId, Rung

REPO = Path(__file__).resolve().parent.parent
LADDER = REPO / "config" / "ladder.yaml"
CID = "c-0001"
TICK = 100
TABLES: LikelihoodTables = load_likelihood_tables()
PRIOR: Posterior = conditioned_prior(load_prior_config(), None, None)
WATCH = tuple(p for p, s in ALLOWLIST.items() if s.rung_required is Rung.WATCH)
ALL_SEEN = {f: 1.0 for f in TABLES.features()}
F = FeatureName
P = ProbeId


def _keys(probes: tuple[ProbeId, ...] | list[ProbeId]) -> frozenset[str]:
    bucket = tick_bucket(TICK, 2.0)
    return frozenset(idempotency_key(CID, p.value, None, bucket) for p in probes)


def _ledger(*, probes: int = 0, paid: int = 0, interruptions: int = 0) -> BudgetLedger:
    ledger = BudgetLedger(load_budget_limits())
    for _ in range(paid):
        ledger.record_ask_hail(CID)
    for _ in range(interruptions):
        ledger.record(CID, P.REQUEST_WATCH_OFFICER_LOOK, planner_chosen=False)
    for _ in range(probes):
        ledger._counts[CID] = ledger.counts(CID).model_copy(
            update={"probes": ledger.counts(CID).probes + 1}
        )
    return ledger


@dataclass(frozen=True)
class Case:
    name: str
    observed: dict[FeatureName, float]
    expected: ProbeId | None
    ran: tuple[ProbeId, ...] = ()
    ledger: dict[str, int] = field(default_factory=dict)
    rung: Rung = Rung.ASK
    probe_confidence: dict[ProbeId, float] = field(default_factory=dict)
    must_not_rank: tuple[ProbeId, ...] = ()


def _state(case: Case) -> PlannerState:
    return PlannerState(
        contact_id=CID,
        posterior=PRIOR,
        observed_confidence=case.observed,
        current_rung=case.rung,
        ledger=_ledger(**case.ledger),
        tick=TICK,
        run_keys=_keys(case.ran),
        probe_confidence=case.probe_confidence,
    )


ONLY_PAID_LEFT = (*WATCH, P.ASK_OPERATOR_BY_PHONE, P.REQUEST_WATCH_OFFICER_LOOK)

EIG_CASES = [
    # (1) nothing observed yet: a zero-latency watch probe wins
    Case("1_unobserved_free_watch_wins", {}, P.REGISTRY_LOOKUP),
    # (2) only paid probes remain, hail_reply unobserved: the hail beats the camera (gain 0)
    Case(
        "2_hail_beats_camera",
        {**ALL_SEEN, F.HAIL_REPLY: 0.0},
        P.ACOUSTIC_HAIL,
        ran=ONLY_PAID_LEFT,
    ),
    # (3) depth seen at c=0.2: equal gain, the dwell wins on the interruption weight
    Case(
        "3_dwell_beats_look_on_interruption",
        {**ALL_SEEN, F.DEPTH: 0.2},
        P.SECOND_SENSOR_DWELL,
        ran=WATCH,
        probe_confidence={P.REQUEST_WATCH_OFFICER_LOOK: 0.75, P.SECOND_SENSOR_DWELL: 0.75},
    ),
    # (4) every likelihood feature seen at c=1: nothing to gain
    Case("4_all_seen_none", dict(ALL_SEEN), None),
    # (5) kinematics_profile and drift_check tie on drift alone: allowlist order wins
    Case("5_tie_allowlist_order", {**ALL_SEEN, F.DRIFT_CONSISTENCY: 0.0}, P.KINEMATICS_PROFILE),
    # (6) the interruption limit is reached: the look is filtered (it would win otherwise)
    Case(
        "6_interruption_limit_filters_look",
        {**ALL_SEEN, F.TRANSIT_PATTERN: 0.0},
        None,
        ran=WATCH,
        ledger={"interruptions": 1},
        must_not_rank=(P.REQUEST_WATCH_OFFICER_LOOK,),
    ),
    # (7) the paid limit is reached: only free probes rank
    Case(
        "7_paid_limit_only_free_rank",
        {},
        P.REGISTRY_LOOKUP,
        ledger={"paid": 2},
        must_not_rank=(P.ACOUSTIC_HAIL, P.CAMERA_CUE, P.SECOND_SENSOR_DWELL),
    ),
]


@pytest.mark.parametrize("case", EIG_CASES, ids=[c.name for c in EIG_CASES])
def test_eig_cases(case: Case) -> None:
    choice = DeterministicPlanner().choose(_state(case), TABLES)
    assert choice.probe_id is case.expected
    assert choice.source == "deterministic"
    ranked_ids = [r.probe_id for r in choice.ranked]
    for pid in case.must_not_rank:
        assert pid not in ranked_ids
    if case.expected is None:
        assert choice.reason == REASON_NO_PROBE and choice.gain == 0.0
        assert all(r.gain <= GAIN_EPS for r in choice.ranked)
        return
    assert choice.score == max(r.score for r in choice.ranked)
    assert choice.ranked[0].probe_id is case.expected
    assert choice.gain > GAIN_EPS and choice.why is not None


def test_eig_case1_winner_is_zero_latency_watch() -> None:
    choice = DeterministicPlanner().choose(_state(EIG_CASES[0]), TABLES)
    assert choice.probe_id is not None
    spec = ALLOWLIST[choice.probe_id]
    assert spec.rung_required is Rung.WATCH and spec.latency_s == 0 and spec.cost_usd == 0


def test_eig_case2_camera_gain_zero() -> None:
    choice = DeterministicPlanner().choose(_state(EIG_CASES[1]), TABLES)
    by = {r.probe_id: r for r in choice.ranked}
    assert set(by) == {P.ACOUSTIC_HAIL, P.CAMERA_CUE, P.SECOND_SENSOR_DWELL}
    assert by[P.CAMERA_CUE].gain == 0.0 and by[P.ACOUSTIC_HAIL].gain > 0


def test_eig_case3_interruption_weight_decides() -> None:
    state = _state(EIG_CASES[2])
    by = {r.probe_id: r for r in DeterministicPlanner().choose(state, TABLES).ranked}
    dwell, look = by[P.SECOND_SENSOR_DWELL], by[P.REQUEST_WATCH_OFFICER_LOOK]
    assert dwell.gain == look.gain > 0  # same depth gain; only the cost differs
    no_interruption = CostWeights(latency_cost_weight_per_s=0.001, interruption_cost_weight=0.0)
    flipped = DeterministicPlanner(no_interruption).choose(state, TABLES)
    assert flipped.probe_id is P.REQUEST_WATCH_OFFICER_LOOK


def test_eig_case5_exact_tie() -> None:
    by = {r.probe_id: r for r in DeterministicPlanner().choose(_state(EIG_CASES[4]), TABLES).ranked}
    assert by[P.KINEMATICS_PROFILE].score == by[P.DRIFT_CHECK].score > 0


def test_eig_case6_look_would_win_without_the_limit() -> None:
    free = replace(EIG_CASES[5], ledger={})
    assert DeterministicPlanner().choose(_state(free), TABLES).probe_id is (
        P.REQUEST_WATCH_OFFICER_LOOK
    )


def test_eig_case7_all_ranked_are_free() -> None:
    choice = DeterministicPlanner().choose(_state(EIG_CASES[6]), TABLES)
    assert choice.ranked and all(ALLOWLIST[r.probe_id].cost_usd == 0 for r in choice.ranked)


# --- information gain and score ---------------------------------------------------------------


def test_information_gain_matches_entropy_helper() -> None:
    spec = ALLOWLIST[P.DRIFT_CHECK]
    observed = {F.DRIFT_CONSISTENCY: 0.1}
    gain = expected_information_gain(PRIOR, spec, observed, TABLES)
    want = PRIOR.entropy() - expected_entropy_after(PRIOR, F.DRIFT_CONSISTENCY, TABLES, 0.4)
    assert gain == pytest.approx(want, abs=1e-12)
    # fully observed at the probe's confidence: nothing to add
    assert expected_information_gain(PRIOR, spec, {F.DRIFT_CONSISTENCY: 0.5}, TABLES) == 0.0
    # features without a likelihood table add 0
    assert expected_information_gain(PRIOR, ALLOWLIST[P.CAMERA_CUE], {}, TABLES) == 0.0
    assert expected_information_gain(PRIOR, ALLOWLIST[P.ASK_OPERATOR_BY_PHONE], {}, TABLES) == 0


def test_information_gain_sums_features() -> None:
    spec = ALLOWLIST[P.KINEMATICS_PROFILE]
    tabled = [f for f in spec.returns if TABLES.has(f)]
    assert len(tabled) >= 4
    total = expected_information_gain(PRIOR, spec, {}, TABLES)
    parts = [
        PRIOR.entropy() - expected_entropy_after(PRIOR, f, TABLES, spec.nominal_confidence)
        for f in tabled
    ]
    assert total == pytest.approx(math.fsum(max(0.0, p) for p in parts), abs=1e-12)


def test_score_formula_and_floor() -> None:
    w = load_cost_weights()
    hail = ALLOWLIST[P.ACOUSTIC_HAIL]
    assert probe_cost(hail, w) == pytest.approx(0.01 + 0.001 * 20)
    assert probe_score(0.6, hail, w) == pytest.approx(0.6 / 0.03)
    free = ALLOWLIST[P.REGISTRY_LOOKUP]
    assert probe_cost(free, w) == 0.0
    assert probe_score(0.5, free, w) == pytest.approx(0.5 / DENOM_FLOOR)
    look = ALLOWLIST[P.REQUEST_WATCH_OFFICER_LOOK]
    assert probe_cost(look, w) == pytest.approx(0.06 + 0.5)


def test_rung_watch_never_proposes_ask() -> None:
    state = replace(_state(EIG_CASES[1]), current_rung=Rung.WATCH)
    choice = DeterministicPlanner().choose(state, TABLES)
    assert all(ALLOWLIST[r.probe_id].rung_required is Rung.WATCH for r in choice.ranked)
    assert choice.probe_id is None  # every watch probe already ran in this bucket


def test_run_keys_skip_only_the_current_bucket() -> None:
    state = _state(Case("x", {}, None, ran=(P.REGISTRY_LOOKUP,)))
    choice = DeterministicPlanner().choose(state, TABLES)
    assert P.REGISTRY_LOOKUP not in [r.probe_id for r in choice.ranked]
    later = replace(state, tick=TICK + 15)
    assert DeterministicPlanner().choose(later, TABLES).probe_id is P.REGISTRY_LOOKUP


def test_ran_probes_skip_at_any_tick() -> None:
    state = replace(_state(Case("x", {}, None)), ran_probes=frozenset({P.REGISTRY_LOOKUP}))
    for tick in (TICK, TICK + 15, TICK + 600):
        choice = DeterministicPlanner().choose(replace(state, tick=tick), TABLES)
        assert P.REGISTRY_LOOKUP not in [r.probe_id for r in choice.ranked]
        assert choice.probe_id is P.KINEMATICS_PROFILE


def test_planner_protocol_and_choice_fields() -> None:
    planner: Planner = DeterministicPlanner()
    choice = planner.choose(_state(EIG_CASES[0]), TABLES)
    assert isinstance(choice, PlannerChoice)
    assert choice.fallback_reason is None and choice.question_for_human is None
    llm = choice.model_copy(update={"source": "llm", "why": "check the hail first"})
    assert llm.source == "llm"
    with pytest.raises(ValueError):
        PlannerChoice(probe_id=None, reason="x", source="model")  # type: ignore[arg-type]


# --- budgets ----------------------------------------------------------------------------------


def test_budget_limits_and_weights_from_config() -> None:
    limits = load_budget_limits()
    assert limits == BudgetLimits(
        probes_per_contact=4, paid_probes_per_contact=2, human_interruptions_per_contact=1
    )
    assert load_cost_weights() == CostWeights(
        latency_cost_weight_per_s=0.001, interruption_cost_weight=0.5
    )


def test_ledger_counts_and_ask_hail_is_paid() -> None:
    ledger = BudgetLedger()
    ledger.record_ask_hail(CID)
    assert ledger.counts(CID).model_dump() == {"probes": 0, "paid": 1, "interruptions": 0}
    ledger.record(CID, P.CAMERA_CUE)
    assert ledger.counts(CID).model_dump() == {"probes": 1, "paid": 2, "interruptions": 0}
    assert not ledger.allows(CID, P.SECOND_SENSOR_DWELL)
    with pytest.raises(BudgetExceeded):
        ledger.record(CID, P.SECOND_SENSOR_DWELL)
    assert ledger.counts(CID).paid == 2
    ledger.record(CID, P.REQUEST_WATCH_OFFICER_LOOK)
    assert not ledger.allows(CID, P.REQUEST_WATCH_OFFICER_LOOK)
    ledger.record(CID, P.DRIFT_CHECK).record(CID, P.KINEMATICS_PROFILE)
    assert ledger.remaining(CID).probes == 0 and not ledger.allows(CID, P.DRIFT_CHECK)
    assert ledger.counts("c-0002").probes == 0  # per contact
    snapshot = ledger.copy()
    ledger.record_ask_hail(CID)
    assert snapshot.counts(CID).paid == 2 and ledger.counts(CID).paid == 3
    assert list(ledger.to_dict()) == [CID]


_posteriors = st.lists(
    st.floats(min_value=0.001, max_value=1.0), min_size=len(HYPOTHESES), max_size=len(HYPOTHESES)
).map(lambda ws: Posterior.from_weights(dict(zip(HYPOTHESES, ws, strict=True))))
_observed = st.dictionaries(
    st.sampled_from(TABLES.features()), st.floats(min_value=0.0, max_value=1.0), max_size=11
)


@settings(derandomize=True, max_examples=150, deadline=None)
@given(
    post=_posteriors,
    observed=_observed,
    rung=st.sampled_from([Rung.WATCH, Rung.ASK]),
    probes=st.integers(0, 4),
    paid=st.integers(0, 2),
    interruptions=st.integers(0, 1),
    ran=st.lists(st.sampled_from(list(ProbeId)), unique=True, max_size=6),
)
def test_budget_property(
    post: Posterior,
    observed: dict[FeatureName, float],
    rung: Rung,
    probes: int,
    paid: int,
    interruptions: int,
    ran: list[ProbeId],
) -> None:
    limits = load_budget_limits()
    ledger = _ledger(probes=probes, paid=paid, interruptions=interruptions)
    planner = DeterministicPlanner()
    seen = dict(observed)
    keys = set(_keys(ran))
    for step in range(20):
        state = PlannerState(
            contact_id=CID,
            posterior=post,
            observed_confidence=seen,
            current_rung=rung,
            ledger=ledger,
            tick=TICK + step,
            run_keys=frozenset(keys),
        )
        choice = planner.choose(state, TABLES)
        for r in choice.ranked:
            assert rung_index(ALLOWLIST[r.probe_id].rung_required) <= rung_index(rung)
        if choice.probe_id is None:
            break
        spec = ALLOWLIST[choice.probe_id]
        assert rung_index(spec.rung_required) <= rung_index(rung)
        ledger.record(CID, choice.probe_id)
        counts = ledger.counts(CID)
        assert counts.probes <= limits.probes_per_contact
        assert counts.paid <= limits.paid_probes_per_contact
        assert counts.interruptions <= limits.human_interruptions_per_contact
        for f in spec.returns:  # the probe delivered its nominal confidence
            seen[f] = max(seen.get(f, 0.0), spec.nominal_confidence)
        keys |= {state.key_for(choice.probe_id)}
    else:
        pytest.fail("the planner did not stop within 20 steps")
    assert ledger.counts(CID).probes <= limits.probes_per_contact


def test_budget_exhausted_returns_none() -> None:
    choice = DeterministicPlanner().choose(
        _state(Case("x", {}, None, ledger={"probes": 4})), TABLES
    )
    assert choice.probe_id is None and choice.ranked == () and choice.reason == REASON_NO_PROBE


# --- weights from config ----------------------------------------------------------------------


def _ladder_copy(tmp_path: Path, factor: float) -> Path:
    doc = yaml.safe_load(LADDER.read_text(encoding="utf-8"))
    doc["costs"]["interruption_cost_weight"] = doc["costs"]["interruption_cost_weight"] * factor
    out = tmp_path / "ladder.yaml"
    out.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    return out


def test_weights_from_config(tmp_path: Path) -> None:
    base = load_cost_weights()
    doubled = load_cost_weights(_ladder_copy(tmp_path, 2.0))
    assert doubled.interruption_cost_weight == 2 * base.interruption_cost_weight == 1.0
    assert doubled.latency_cost_weight_per_s == base.latency_cost_weight_per_s
    state = _state(EIG_CASES[2])

    def scores(weights: CostWeights) -> dict[ProbeId, float]:
        planner = DeterministicPlanner(weights)
        return {r.probe_id: r.score for r in planner.candidates(state, TABLES)}

    s1, s2 = scores(base), scores(doubled)
    dwell, look = P.SECOND_SENSOR_DWELL, P.REQUEST_WATCH_OFFICER_LOOK
    gain = next(
        r.gain for r in DeterministicPlanner(base).candidates(state, TABLES) if r.probe_id is look
    )
    # predicted: only the look's denominator changes, 0.06 + 0.5 -> 0.06 + 1.0
    assert s1[look] == pytest.approx(gain / 0.56) and s2[look] == pytest.approx(gain / 1.06)
    assert s2[dwell] == s1[dwell]
    assert s1[dwell] / s1[look] == pytest.approx(0.56 / 0.16)
    assert s2[dwell] / s2[look] == pytest.approx(1.06 / 0.16)

    # case 3 with the look expected to deliver c=1.0 and the dwell c=0.25: the look wins
    # at mu=0.5 (gain ratio ~3.6 > 0.56/0.16) and loses at mu=1.0 (< 1.06/0.16)
    tilted = replace(state, probe_confidence={look: 1.0, dwell: 0.25})
    assert DeterministicPlanner(base).choose(tilted, TABLES).probe_id is look
    assert DeterministicPlanner(doubled).choose(tilted, TABLES).probe_id is dwell


def test_determinism_same_state_same_choice() -> None:
    for case in EIG_CASES:
        a = DeterministicPlanner().choose(_state(case), TABLES)
        b = DeterministicPlanner().choose(_state(case), TABLES)
        assert a == b and a.model_dump_json() == b.model_dump_json()
