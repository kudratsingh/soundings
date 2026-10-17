"""WO-033 part A: the investigation machine on synthetic inputs.

Transitions (exhaustive table plus a derandomized property test), checkpoint chain and
resume, idempotency keys, budgets, the frozen recommendation, and determinism. The
planner and probe runner are scripted test doubles of the ``Planner`` and
``ProbeRunner`` Protocols; WO-033 part B wires the WO-031/032 implementations.
"""

from __future__ import annotations

import hashlib
import itertools
from collections import Counter
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import ValidationError

from soundings.canonical import canonical_json
from soundings.investigation.evidence import EvidenceTable
from soundings.investigation.machine import (
    GENESIS_HASH,
    INITIAL_STATE,
    RECOMMENDED_RUNG,
    TERMINAL_INVESTIGATION_STATES,
    TRANSITIONS,
    BudgetLimits,
    BudgetView,
    CheckpointChainError,
    IllegalTransition,
    InvestigationCheckpoint,
    InvestigationError,
    InvestigationEvent,
    InvestigationLedger,
    InvestigationMachine,
    InvestigationResult,
    InvestigationState,
    NoProbePlanner,
    NoRowsProbeRunner,
    ProbeChoice,
    ProbeError,
    Recommendation,
    StopDecision,
    StopOutcome,
    StopRuleId,
    action_key,
    checkpoint_line,
    latency_ticks,
    load_budgets,
    load_checkpoints,
    load_stopping_config,
    next_state,
    verify_checkpoint_chain,
)
from soundings.investigation.posterior import (
    LikelihoodTables,
    PosteriorReport,
    load_likelihood_tables,
    load_prior_config,
)
from soundings.probe_keys import idempotency_key, tick_bucket
from soundings.schemas.enums import (
    EvidenceKind,
    FeatureName,
    Hypothesis,
    LadderState,
    ProbeId,
    Rung,
    Unit,
)
from soundings.schemas.evidence import EvidenceRow

CID = "c-0001"
TICK_S = 2.0
TABLES = load_likelihood_tables()
PRIORS = load_prior_config()
STOPPING = load_stopping_config()
BUDGETS = load_budgets()

S = InvestigationState
E = InvestigationEvent


# --- test doubles ----------------------------------------------------------------------

FREE = {"cost_usd": 0.0, "latency_s": 0.0, "rung_required": Rung.WATCH}


def _choice(probe: ProbeId, **kw: Any) -> ProbeChoice:
    return ProbeChoice(probe_id=probe, **{**FREE, **kw})


class ScriptedPlanner:
    """Proposes ``script[probes_used]``; a pure function of the ledger, so a replay
    after a crash proposes the same probe."""

    def __init__(self, script: Sequence[ProbeChoice]) -> None:
        self.script = tuple(script)
        self.calls = 0

    def next_probe(
        self, table: EvidenceTable, posterior: PosteriorReport, budget: BudgetView, rung: Rung
    ) -> ProbeChoice | None:
        self.calls += 1
        i = budget.ledger.probes_used
        return self.script[i] if i < len(self.script) else None


ROW_FOR: dict[ProbeId, tuple[FeatureName, Any, Unit]] = {
    ProbeId.ACOUSTIC_FEATURE_READ: (FeatureName.PROPELLER_TONAL_PRESENT, False, Unit.BOOL),
    ProbeId.DRIFT_CHECK: (FeatureName.DRIFT_CONSISTENCY, 0.2, Unit.RATIO),
    ProbeId.KINEMATICS_PROFILE: (FeatureName.DEPTH, 4.0, Unit.M),
    ProbeId.PATTERN_OF_LIFE_CHECK: (FeatureName.PATTERN_OF_LIFE_ANOMALY, 0.5, Unit.RATIO),
    ProbeId.CAMERA_CUE: (FeatureName.VISIBILITY_FOR_CAMERA, 0.8, Unit.RATIO),
    ProbeId.SECOND_SENSOR_DWELL: (FeatureName.SPEED_OVER_GROUND, 0.4, Unit.M_PER_S),
}


class CountingRunner:
    """Returns one fact row per probe; counts completed executions per action and can
    crash (a plain exception, not a ``ProbeError``) on chosen call numbers."""

    def __init__(self, crash_on: Sequence[int] = (), fail_on: Sequence[int] = ()) -> None:
        self.crash_on = set(crash_on)
        self.fail_on = set(fail_on)
        self.calls = 0
        self.completed: Counter[tuple[ProbeId, int]] = Counter()

    def run(
        self, probe_id: ProbeId, contact_id: str, tick: int, args: Mapping[str, Any]
    ) -> Sequence[EvidenceRow]:
        self.calls += 1
        if self.calls in self.crash_on:
            raise RuntimeError("simulated crash")
        if self.calls in self.fail_on:
            raise ProbeError("sensor did not answer")
        self.completed[(probe_id, tick)] += 1
        feature, value, unit = ROW_FOR[probe_id]
        return (
            EvidenceRow(
                contact_id=contact_id,
                t=tick,
                feature=feature,
                value=value,
                unit=unit,
                source=f"probe:{probe_id.value}",
                confidence=0.9,
                kind=EvidenceKind.FACT,
                probe_id=probe_id,
            ),
        )


def _base_table() -> EvidenceTable:
    table = EvidenceTable()
    table.append(
        EvidenceRow(
            contact_id=CID,
            t=10,
            feature=FeatureName.BROADBAND_LEVEL,
            value=105.0,
            unit=Unit.DB,
            source="acoustic_detector:hyd_1",
            confidence=0.8,
            kind=EvidenceKind.FACT,
            probe_id=None,
        )
    )
    return table


SCRIPT = (
    _choice(ProbeId.ACOUSTIC_FEATURE_READ, latency_s=2.0),
    _choice(ProbeId.DRIFT_CHECK),
    _choice(ProbeId.KINEMATICS_PROFILE),
    _choice(ProbeId.PATTERN_OF_LIFE_CHECK),
    _choice(ProbeId.CAMERA_CUE, cost_usd=0.05, latency_s=30.0, rung_required=Rung.ASK),
)


def _machine(
    planner: Any = None,
    runner: Any = None,
    *,
    table: EvidenceTable | None = None,
    budgets: BudgetLimits = BUDGETS,
    ledger: InvestigationLedger | None = None,
    tick: int = 20,
    max_tick: int = 599,
    checkpoint_path: Path | None = None,
    tables: LikelihoodTables = TABLES,
) -> InvestigationMachine:
    return InvestigationMachine(
        CID,
        _base_table() if table is None else table,
        planner=ScriptedPlanner(SCRIPT) if planner is None else planner,
        probe_runner=CountingRunner() if runner is None else runner,
        tables=tables,
        prior_cfg=PRIORS,
        stopping=STOPPING,
        budgets=budgets,
        tick=tick,
        tick_s=TICK_S,
        max_tick=max_tick,
        ledger=ledger,
        checkpoint_path=checkpoint_path,
    )


# --- transitions -----------------------------------------------------------------------


def test_transitions_table_is_exactly_the_wo_rows() -> None:
    non_terminal = [s for s in S if s not in TERMINAL_INVESTIGATION_STATES]
    expected = {
        (S.SCORING, E.POSTERIOR_UPDATED): S.DECIDING,
        (S.DECIDING, E.STOP_RULE_FIRED): S.STOPPED,
        (S.DECIDING, E.PROBE_SELECTED): S.PROBING,
        (S.PROBING, E.PROBE_RESULT): S.SCORING,
        **{(s, E.ERROR): S.ERRORED for s in non_terminal},
    }
    assert dict(TRANSITIONS) == expected
    assert INITIAL_STATE is S.SCORING
    assert TERMINAL_INVESTIGATION_STATES == {S.STOPPED, S.ERRORED}


@pytest.mark.parametrize(("state", "event"), list(itertools.product(S, E)))
def test_transitions_every_other_pair_raises(
    state: InvestigationState, event: InvestigationEvent
) -> None:
    if (state, event) in TRANSITIONS:
        assert next_state(state, event) is TRANSITIONS[(state, event)]
    else:
        with pytest.raises(IllegalTransition):
            next_state(state, event)


def test_transitions_state_names_do_not_collide_with_ladder_states() -> None:
    assert not {s.name for s in S} & {s.name for s in LadderState}
    assert not {s.value for s in S} & {s.value for s in LadderState}


class _BadTables(LikelihoodTables):
    def __init__(self) -> None:  # no super(): every read fails
        pass

    def features(self) -> tuple[FeatureName, ...]:
        raise ValueError("table unreadable")


class _RaisingPlanner:
    def next_probe(self, *_: Any) -> ProbeChoice | None:
        raise InvestigationError("planner unavailable")


def _pairs(result: InvestigationResult) -> set[tuple[InvestigationState, InvestigationEvent]]:
    return {(cp.from_state, cp.event) for cp in result.checkpoints}


def test_transitions_every_row_reachable_on_synthetic_inputs() -> None:
    seen: set[tuple[InvestigationState, InvestigationEvent]] = set()
    full = _machine().run()
    assert full.state is S.STOPPED
    seen |= _pairs(full)
    seen |= _pairs(_machine(tables=_BadTables()).run())
    seen |= _pairs(_machine(planner=_RaisingPlanner()).run())
    failed = _machine(runner=CountingRunner(fail_on=[1])).run()
    assert failed.state is S.ERRORED and failed.recommendation is None and failed.error
    seen |= _pairs(failed)
    assert seen == set(TRANSITIONS)


@settings(derandomize=True, max_examples=300, deadline=None)
@given(st.lists(st.sampled_from(list(E)), max_size=40))
def test_transitions_property_never_leaves_terminal(events: list[InvestigationEvent]) -> None:
    state = INITIAL_STATE
    for event in events:
        was_terminal = state in TERMINAL_INVESTIGATION_STATES
        try:
            state = next_state(state, event)
        except IllegalTransition:
            continue
        assert not was_terminal, "a terminal state moved"
        assert state in set(S)
    if state in TERMINAL_INVESTIGATION_STATES:
        for event in E:
            with pytest.raises(IllegalTransition):
                next_state(state, event)


# --- budgets ---------------------------------------------------------------------------


def test_budget_probes_per_contact_bounds_the_loop() -> None:
    planner = ScriptedPlanner([_choice(ProbeId.DRIFT_CHECK)] * 20)
    result = _machine(planner).run()
    assert result.ledger.probes_used == BUDGETS.probes_per_contact
    rec = result.recommendation
    assert rec is not None
    assert (rec.outcome, rec.rule_id, rec.uncertain) == (
        StopOutcome.HANDOFF,
        StopRuleId.C_BUDGET,
        True,
    )
    assert rec.recommended_rung is Rung.HAND_TO_PERSON
    assert rec.question_for_human and rec.question_for_human.startswith("Evidence is inconclusive")


def test_budget_planner_none_is_exhaustion() -> None:
    result = _machine(NoProbePlanner()).run()
    assert result.events == ("posterior_updated", "stop_rule_fired(HANDOFF)")
    assert result.recommendation is not None and result.recommendation.uncertain


def test_budget_hail_counts_as_paid() -> None:
    paid = _choice(ProbeId.CAMERA_CUE, cost_usd=0.05, latency_s=30.0, rung_required=Rung.ASK)
    planner = ScriptedPlanner([paid, paid, paid])
    # Ask-rung hail already spent one of the two paid probes: one more is allowed.
    result = _machine(planner, ledger=InvestigationLedger(paid_used=1)).run()
    assert result.ledger.paid_used == BUDGETS.paid_probes_per_contact
    assert result.ledger.probes_used == 1
    assert result.recommendation is not None
    assert result.recommendation.rule_id is StopRuleId.C_BUDGET


def test_budget_interruption_and_rung_and_clock_refused() -> None:
    look = _choice(ProbeId.KINEMATICS_PROFILE, interruption=1, rung_required=Rung.ASK)
    over = _machine(ScriptedPlanner([look, look])).run()
    assert over.ledger.interruptions == BUDGETS.human_interruptions_per_contact
    above_rung = _machine(ScriptedPlanner([_choice(ProbeId.DRIFT_CHECK, rung_required=Rung.HOLD)]))
    assert above_rung.run().ledger.probes_used == 0
    late = _choice(ProbeId.SECOND_SENSOR_DWELL, latency_s=60.0, rung_required=Rung.ASK)
    out_of_ticks = _machine(ScriptedPlanner([late]), tick=590, max_tick=599).run()
    assert out_of_ticks.ledger.probes_used == 0
    assert out_of_ticks.recommendation is not None
    assert out_of_ticks.recommendation.rule_id is StopRuleId.C_BUDGET


def test_budget_limits_from_config() -> None:
    assert BUDGETS == BudgetLimits(
        probes_per_contact=4,
        paid_probes_per_contact=2,
        human_interruptions_per_contact=1,
        llm_calls_per_contact=4,
    )


def test_clock_latency_ticks() -> None:
    assert latency_ticks(0.0, 2.0) == 0
    assert latency_ticks(1.0, 2.0) == 1
    assert latency_ticks(20.0, 2.0) == 10
    assert latency_ticks(21.0, 2.0) == 11
    result = _machine().run()
    landed = [c.tick for c in result.probe_calls]
    assert landed[0] == 21  # acoustic_feature_read: 2 s at 2 s per tick
    assert result.tick == max(landed)


# --- idempotency -----------------------------------------------------------------------


def test_idempotency_action_key_formula() -> None:
    expected = hashlib.sha256(
        canonical_json([CID, 3, "probe_selected", "abc"]).encode("utf-8")
    ).hexdigest()
    assert action_key(CID, 3, E.PROBE_SELECTED, "abc") == expected
    assert action_key(CID, 3, E.PROBE_SELECTED, "abc") != action_key(
        CID, 4, E.PROBE_SELECTED, "abc"
    )


def test_idempotency_probe_payload_is_probe_key() -> None:
    result = _machine().run()
    selected = [cp for cp in result.checkpoints if cp.event is E.PROBE_SELECTED]
    assert selected
    for cp in selected:
        assert cp.event_arg is not None
        probe_key = idempotency_key(CID, cp.event_arg, {}, tick_bucket(cp.tick, TICK_S))
        assert cp.probe_key == probe_key
        assert cp.action_key == action_key(CID, cp.seq, E.PROBE_SELECTED, probe_key)
    keys = [cp.action_key for cp in result.checkpoints]
    assert len(keys) == len(set(keys))


def test_idempotency_same_probe_same_bucket_runs_once() -> None:
    runner = CountingRunner()
    planner = ScriptedPlanner([_choice(ProbeId.DRIFT_CHECK)] * 3)
    result = _machine(planner, runner).run()
    assert runner.calls == 1
    assert [c.cached for c in result.probe_calls] == [False, True, True]


# --- checkpoints -----------------------------------------------------------------------


def test_checkpoint_chain_links_and_jsonl_equals_memory(tmp_path: Path) -> None:
    path = tmp_path / "inv.jsonl"
    result = _machine(checkpoint_path=path).run()
    cps = result.checkpoints
    assert cps[0].prev_hash == GENESIS_HASH
    for prev, cp in itertools.pairwise(cps):
        assert cp.prev_hash == prev.hash
        assert cp.seq == prev.seq + 1
    verify_checkpoint_chain(cps)
    assert load_checkpoints(path) == cps
    assert path.read_text(encoding="utf-8") == "".join(checkpoint_line(c) + "\n" for c in cps)
    assert all(cp.verify_hash() for cp in cps)
    stop = cps[-1]
    assert stop.event is E.STOP_RULE_FIRED and stop.recommendation == result.recommendation


def test_checkpoint_chain_detects_tampering() -> None:
    cps = list(_machine().run().checkpoints)
    tampered = cps[1].model_copy(update={"tick": cps[1].tick + 1})
    with pytest.raises(CheckpointChainError):
        verify_checkpoint_chain([cps[0], tampered, *cps[2:]])
    with pytest.raises(CheckpointChainError):
        verify_checkpoint_chain([cps[0], *cps[2:]])


def test_checkpoint_posterior_is_six_dp() -> None:
    for cp in _machine().run().checkpoints:
        for p in cp.posterior.values():
            assert round(p, 6) == p


@pytest.mark.parametrize("crash_call", [1, 2, 3])
def test_checkpoint_resume_equals_uninterrupted(crash_call: int, tmp_path: Path) -> None:
    reference_runner = CountingRunner()
    reference = _machine(runner=reference_runner).run()
    assert reference.ledger.probes_used >= 3

    runner = CountingRunner(crash_on=[crash_call])
    path = tmp_path / "inv.jsonl"
    crashed = _machine(runner=runner, checkpoint_path=path)
    with pytest.raises(RuntimeError, match="simulated crash"):
        crashed.run()
    saved = load_checkpoints(path)
    assert saved == tuple(crashed.checkpoints)
    assert saved[-1].event is E.PROBE_SELECTED
    if crash_call == 1:
        assert len(saved) == 2  # interrupted after two transitions

    resumed = _machine(runner=runner, checkpoint_path=path).resume(saved)
    assert resumed.transition_log_lines() == reference.transition_log_lines()
    assert resumed.recommendation == reference.recommendation
    assert resumed.recommendation is not None
    assert reference.recommendation is not None
    assert (
        resumed.recommendation.recommendation_hash == reference.recommendation.recommendation_hash
    )
    assert load_checkpoints(path) == resumed.checkpoints
    # No action ran twice: each (probe, tick) completed exactly once across both machines.
    assert set(runner.completed.values()) == {1}
    assert runner.completed == reference_runner.completed


def test_checkpoint_resume_of_finished_run_is_a_replay() -> None:
    runner = CountingRunner()
    done = _machine(runner=runner).run()
    again = _machine(runner=runner).resume(done.checkpoints)
    assert again.checkpoints == done.checkpoints
    assert again.recommendation == done.recommendation
    assert runner.calls == done.ledger.probes_used  # nothing re-executed


def test_checkpoint_resume_rejects_another_contact() -> None:
    done = _machine().run()
    other = InvestigationMachine(
        "c-0002",
        EvidenceTable(),
        planner=NoProbePlanner(),
        probe_runner=NoRowsProbeRunner(),
        tables=TABLES,
        prior_cfg=PRIORS,
        stopping=STOPPING,
        budgets=BUDGETS,
        tick=0,
        tick_s=TICK_S,
        max_tick=10,
    )
    with pytest.raises(CheckpointChainError):
        other.resume(done.checkpoints)


def test_checkpoint_round_trips_json() -> None:
    for cp in _machine().run().checkpoints:
        assert InvestigationCheckpoint.model_validate_json(checkpoint_line(cp)) == cp


# --- recommendation --------------------------------------------------------------------


def _rec(**over: Any) -> Recommendation:
    fields: dict[str, Any] = {
        "contact_id": CID,
        "outcome": StopOutcome.HANDOFF,
        "rule_id": StopRuleId.C_THREAT,
        "posterior": {
            h.value: (1.0 if h is Hypothesis.UNAUTHORIZED_UUV else 0.0) for h in Hypothesis
        },
        "entropy_bits": 0.0,
        "threat_mass": 1.0,
        "benign_mass": 0.0,
        "top": Hypothesis.UNAUTHORIZED_UUV,
        "uncertain": False,
        "question_for_human": "Is this contact expected?",
        "evidence_row_ids": ("0123456789abcdef",),
        "probes_used": 2,
        "interruptions": 0,
        "tick": 100,
    }
    return Recommendation(**{**fields, **over})


def test_recommendation_frozen_rung_cannot_be_assigned() -> None:
    rec = _rec()
    with pytest.raises(ValidationError):
        rec.recommended_rung = Rung.ASK  # type: ignore[misc]
    with pytest.raises(ValidationError):
        rec.outcome = StopOutcome.RESOLVED_BENIGN  # type: ignore[misc]
    with pytest.raises(ValidationError):
        _rec(recommended_rung="ask")
    with pytest.raises(ValidationError):
        Recommendation.model_validate_json(
            rec.model_dump_json().replace('"hand_to_person"', '"ask"')
        )
    assert Recommendation.model_validate_json(rec.model_dump_json()) == rec


@pytest.mark.parametrize("outcome", list(StopOutcome))
def test_recommendation_frozen_rung_only_ask_or_hand(outcome: StopOutcome) -> None:
    rec = _rec(outcome=outcome)
    assert rec.recommended_rung in {Rung.ASK, Rung.HAND_TO_PERSON}
    assert rec.recommended_rung is RECOMMENDED_RUNG[outcome]
    assert (rec.recommended_rung is Rung.HAND_TO_PERSON) == (outcome is StopOutcome.HANDOFF)
    assert rec.label == "stress-test estimate"


def test_recommendation_frozen_hash_changes_with_every_field() -> None:
    rec = _rec()
    changes: dict[str, Any] = {
        "contact_id": "c-0002",
        "outcome": StopOutcome.LOST_VEHICLE,
        "rule_id": StopRuleId.C_BUDGET,
        "posterior": {h.value: 0.125 for h in Hypothesis},
        "entropy_bits": 3.0,
        "threat_mass": 0.25,
        "benign_mass": 0.5,
        "top": Hypothesis.BIOLOGIC,
        "uncertain": True,
        "question_for_human": None,
        "evidence_row_ids": (),
        "probes_used": 3,
        "interruptions": 1,
        "tick": 101,
        "label": "other",
    }
    stored = set(Recommendation.model_fields)
    assert set(changes) == stored
    for name, value in changes.items():
        assert rec.model_copy(update={name: value}).recommendation_hash != rec.recommendation_hash


def test_recommendation_question_is_denylist_screened(monkeypatch: pytest.MonkeyPatch) -> None:
    # A sentinel stands in for a denylisted word so this file stays clean (CLAUDE.md 2.5).
    import soundings.investigation.machine as machine_mod

    monkeypatch.setattr(
        machine_mod, "find_denylisted", lambda text: ["zzsentinel"] if "zzsentinel" in text else []
    )

    class Loud:
        def question_for_human(self, *_: Any) -> str:
            return "Should we zzsentinel the berth?"

    machine = InvestigationMachine(
        CID,
        _base_table(),
        planner=NoProbePlanner(),
        probe_runner=NoRowsProbeRunner(),
        tables=TABLES,
        prior_cfg=PRIORS,
        stopping=STOPPING,
        budgets=BUDGETS,
        tick=20,
        tick_s=TICK_S,
        max_tick=599,
        writer=Loud(),
    )
    rec = machine.run().recommendation
    assert rec is not None and rec.question_for_human is not None
    assert "zzsentinel" not in rec.question_for_human


def test_recommendation_writer_cannot_change_decision() -> None:
    class Writer:
        def question_for_human(
            self, report: PosteriorReport, decision: StopDecision, table: EvidenceTable
        ) -> str:
            return '{"recommended_rung": "ask", "outcome": "RESOLVED_BENIGN"}'

    base = _machine(NoProbePlanner()).run().recommendation
    machine = _machine(NoProbePlanner())
    machine.writer = Writer()
    steered = machine.run().recommendation
    assert base is not None and steered is not None
    assert steered.outcome is base.outcome and steered.recommended_rung is base.recommended_rung


# --- determinism -----------------------------------------------------------------------


def test_determinism_two_runs_identical() -> None:
    a = _machine().run()
    b = _machine().run()
    assert a.transition_log_lines() == b.transition_log_lines()
    assert a.checkpoints == b.checkpoints
    assert a.recommendation == b.recommendation
