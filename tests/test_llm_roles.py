"""WO-032: prompt builders produce canonical, versioned, hashable inputs."""

from __future__ import annotations

from soundings.investigation.probes import ALLOWLIST as PROBE_ALLOWLIST
from soundings.investigation.probes import RESPONDER_PROBES
from soundings.llm.client import fixture_key
from soundings.llm.planner import guard_allowlist, guard_context, planner_inputs
from soundings.llm.roles import (
    HYPOTHESIS_WRITER_PROMPT,
    PROBE_PLANNER_PROMPT,
    PROMPT_VERSION,
    ROLE_HYPOTHESIS_WRITER,
    ROLE_PROBE_PLANNER,
    build_hypothesis_writer_inputs,
    build_probe_planner_inputs,
    evidence_summary,
)
from soundings.schemas.denylist import find_denylisted
from soundings.schemas.enums import Hypothesis
from tests.llm_cases import (
    ALLOWLIST,
    CONTACT,
    PLANNER_CASES,
    asset,
    make_ctx,
    make_table,
    threat_report,
)


def test_planner_inputs_deterministic() -> None:
    a = build_probe_planner_inputs(make_table(), threat_report(), make_ctx(), asset(410.0))
    b = build_probe_planner_inputs(make_table(), threat_report(), make_ctx(), asset(410.0))
    assert a == b
    assert fixture_key(ROLE_PROBE_PLANNER, a) == fixture_key(ROLE_PROBE_PLANNER, b)


def test_planner_inputs_shape() -> None:
    inputs = PLANNER_CASES["valid"].inputs()
    assert set(inputs) == {
        "prompt_version",
        "role",
        "evidence",
        "posterior",
        "allowlist",
        "already_run",
        "remaining_budget",
        "current_rung",
        "asset_context",
    }
    assert inputs["prompt_version"] == PROMPT_VERSION
    assert inputs["role"] == ROLE_PROBE_PLANNER
    assert inputs["current_rung"] == "ask"
    assert inputs["remaining_budget"] == {"probes": 4, "paid_probes": 2, "interruptions": 1}
    assert inputs["allowlist"] == [
        {
            "probe_id": p.value,
            "rung_required": t.rung_required.value,
            "cost_usd": t.cost_usd,
            "latency_s": t.latency_s,
            "interruption": t.interruption,
        }
        for p, t in ALLOWLIST.items()
    ]
    assert inputs["posterior"]["label"]
    assert set(inputs["posterior"]["posterior"]) == {h.value for h in Hypothesis}


def test_evidence_summary_latest_row_6dp() -> None:
    rows = evidence_summary(make_table(), CONTACT)
    assert [r["feature"] for r in rows] == [
        "speed_over_ground",
        "propeller_tonal_present",
        "registry_match",
    ]
    assert rows[0] == {
        "feature": "speed_over_ground",
        "value": 1.3,
        "kind": "fact",
        "confidence": 0.8,
    }
    assert rows[1]["value"] is True


def test_prompt_version_changes_the_key() -> None:
    inputs = PLANNER_CASES["valid"].inputs()
    bumped = {**inputs, "prompt_version": "p3.2"}
    assert fixture_key(ROLE_PROBE_PLANNER, inputs) != fixture_key(ROLE_PROBE_PLANNER, bumped)


def test_writer_inputs() -> None:
    a = build_hypothesis_writer_inputs(threat_report(), make_table())
    b = build_hypothesis_writer_inputs(threat_report(), make_table())
    assert a == b
    assert fixture_key(ROLE_HYPOTHESIS_WRITER, a) == fixture_key(ROLE_HYPOTHESIS_WRITER, b)
    assert a["prompt_version"] == PROMPT_VERSION
    assert a["role"] == ROLE_HYPOTHESIS_WRITER
    assert a["hypotheses"] == [h.value for h in Hypothesis]
    assert len(a["hypotheses"]) == 8


def test_prompts_are_clean_and_state_the_limits() -> None:
    for prompt in (PROBE_PLANNER_PROMPT, HYPOTHESIS_WRITER_PROMPT):
        assert find_denylisted(prompt) == []
        assert "rung" in prompt and "approval" in prompt and "tool call" in prompt
    assert "only one allowlisted probe" in PROBE_PLANNER_PROMPT
    assert "outside the eight" in HYPOTHESIS_WRITER_PROMPT


def test_inputs_allowlist_equals_wo031_allowlist() -> None:
    """The allowlist the model sees is WO-031 ``ALLOWLIST`` with its costs, in order."""
    expected = [
        {
            "probe_id": pid.value,
            "rung_required": spec.rung_required.value,
            "cost_usd": spec.cost_usd,
            "latency_s": spec.latency_s,
            "interruption": spec.interruption,
        }
        for pid, spec in PROBE_ALLOWLIST.items()
        if pid not in RESPONDER_PROBES  # WO-088: the model sees section 5 only
    ]
    state = PLANNER_CASES["valid"].state()
    assert planner_inputs(state)["allowlist"] == expected
    assert PLANNER_CASES["valid"].inputs()["allowlist"] == expected
    assert guard_allowlist() == ALLOWLIST  # the hand-copied section 5 table agrees


def test_llm_planner_inputs_match_the_fixture_cases() -> None:
    """``LlmPlanner`` builds exactly the inputs the hand-written fixtures are keyed on."""
    for name, case in PLANNER_CASES.items():
        state = case.state()
        assert guard_context(state) == case.ctx, name
        inputs = planner_inputs(
            state, table=make_table(), report=threat_report(), asset_context=case.asset_context
        )
        assert inputs == case.inputs(), name
        assert fixture_key(ROLE_PROBE_PLANNER, inputs) == fixture_key(
            ROLE_PROBE_PLANNER, case.inputs()
        )
        assert inputs["prompt_version"] == PROMPT_VERSION


def test_planner_inputs_deterministic_from_state() -> None:
    a = planner_inputs(PLANNER_CASES["valid"].state(), asset_context=asset(1.0))
    b = planner_inputs(PLANNER_CASES["valid"].state(), asset_context=asset(1.0))
    assert fixture_key(ROLE_PROBE_PLANNER, a) == fixture_key(ROLE_PROBE_PLANNER, b)
    assert a["evidence"] == []  # no table given: the model sees no rows, not invented ones
