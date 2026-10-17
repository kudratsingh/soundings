"""WO-051 part B1: the deterministic grader, ``grade(run, expected) -> GradeResult``."""

from __future__ import annotations

from functools import cache

import pytest

from soundings.canonical import canonical_json
from soundings.eval.grader import (
    NEEDS_OBSERVATION,
    RULES,
    KeyStatus,
    Observations,
    grade,
)
from soundings.eval.policies import PolicyName, PolicyRun, make_policy
from soundings.schemas import Hypothesis, LadderState
from soundings.schemas.scenario import Expected, Scenario
from soundings.sim.scenario import resolve_scenario


@cache
def _scenario(sid: str) -> Scenario:
    return resolve_scenario(sid)


@cache
def _run(policy: PolicyName, sid: str, seed: int = 1) -> PolicyRun:
    return make_policy(policy).run(_scenario(sid), seed)


def test_every_expected_key_has_a_rule() -> None:
    assert set(RULES) == set(Expected.model_fields)


def test_only_declared_keys_are_listed_in_block_order() -> None:
    sc = _scenario("S05")
    result = grade(_run(PolicyName.BANG, "S05"), sc.expected)
    assert [c.key for c in result.checks] == [
        "terminal_state",
        "passes_through",
        "min_threat_mass",
        "max_human_interruptions",
        "max_probes",
    ]
    assert result.gt_id == "gt-1" and result.scenario_id == "S05" and result.policy == "bang"


def test_terminal_state_matches_the_run_rollup() -> None:
    for policy in (PolicyName.BANG, PolicyName.HUMAN_QUEUE):
        run = _run(policy, "S01")
        check = grade(run, _scenario("S01").expected).check("terminal_state")
        assert check.observed == (run.terminal_state.value if run.terminal_state else "UNDETECTED")
        assert check.status is KeyStatus.FAIL
        assert "scenario expects CLEARED_AT_ASK" in check.reason


def test_passes_through_names_the_missing_state() -> None:
    expected = _scenario("S05").expected
    hq = grade(_run(PolicyName.HUMAN_QUEUE, "S05"), expected).check("passes_through")
    assert hq.status is KeyStatus.FAIL and hq.reason == "never entered HOLDING"
    bang = grade(_run(PolicyName.BANG, "S05"), expected).check("passes_through")
    assert bang.status is KeyStatus.PASS


def test_interruptions_use_the_one_definition() -> None:
    hq = _run(PolicyName.HUMAN_QUEUE, "S01")
    check = grade(hq, _scenario("S01").expected).check("max_human_interruptions")
    assert check.status is KeyStatus.FAIL and int(check.observed) >= 1
    assert "handoff" in check.reason and "limit of 0" in check.reason
    bang = grade(_run(PolicyName.BANG, "S01"), _scenario("S01").expected)
    bang_check = bang.check("max_human_interruptions")
    assert bang_check.status is KeyStatus.FAIL and "approval_request" in bang_check.reason
    assert "max_human_interruptions" in bang.failed_keys and not bang.passed


def test_observation_keys_are_not_graded_without_observations() -> None:
    run = _run(PolicyName.BANG, "S05")
    check = grade(run, _scenario("S05").expected).check("min_threat_mass")
    assert check.status is KeyStatus.NOT_GRADED and check.reason == NEEDS_OBSERVATION


def test_observations_are_graded_when_supplied() -> None:
    run = _run(PolicyName.BANG, "S05")
    exp = Expected(
        terminal_state=LadderState.HANDED_OFF,
        top_hypothesis=Hypothesis.UNAUTHORIZED_UUV,
        min_threat_mass=0.6,
        sitrep_has_search_box=False,
    )
    ok = grade(
        run,
        exp,
        observations=Observations(
            threat_mass=0.7, top_hypothesis=Hypothesis.UNAUTHORIZED_UUV, sitrep_has_search_box=False
        ),
    )
    assert ok.check("min_threat_mass").status is KeyStatus.PASS
    assert ok.check("top_hypothesis").status is KeyStatus.PASS
    assert ok.check("sitrep_has_search_box").status is KeyStatus.PASS
    low = grade(
        run,
        exp,
        observations=Observations(threat_mass=0.5, top_hypothesis=Hypothesis.BIOLOGIC),
    )
    assert low.check("min_threat_mass").status is KeyStatus.FAIL
    assert "is below" in low.check("min_threat_mass").reason
    assert low.check("top_hypothesis").reason == "top hypothesis was BIOLOGIC"
    assert low.check("sitrep_has_search_box").status is KeyStatus.NOT_GRADED


def test_max_probes_and_passing_run() -> None:
    run = _run(PolicyName.BANG, "S05")
    exp = Expected(
        terminal_state=run.terminal_state or LadderState.DETECTED,
        max_probes=0,
        max_human_interruptions=5,
    )
    result = grade(run, exp)
    assert result.passed and result.failed_keys == () and result.graded == 3
    assert result.check("max_probes").reason == "0 probes used, limit 0"


@pytest.mark.parametrize("sid", ["S02", "S05", "S07"])
def test_grade_is_deterministic(sid: str) -> None:
    run = _run(PolicyName.BANG, sid)
    again = make_policy(PolicyName.BANG).run(_scenario(sid), 1)
    a = grade(run, _scenario(sid).expected)
    b = grade(again, _scenario(sid).expected)
    assert canonical_json(a) == canonical_json(b)
