"""WO-033 integration (part A): the machine over real catalog evidence.

Sim -> WO-012 evidence table -> WO-030 posterior -> ``InvestigationMachine`` with a
scripted planner of free watch probes and a probe runner that adds nothing, so only
the free watch evidence is scored. Each case must reach a terminal recommendation
within budget. Final outcomes that need the paid probes (hail, camera, dwell) are
asserted by part B through runner v2 (``-k catalog``), not here.

Ground truth is read only to pick which observed contact to investigate.
"""

from __future__ import annotations

import re
from functools import cache
from pathlib import Path

import pytest

import soundings.investigation.machine as machine_mod
from soundings.investigation.evidence import EvidenceTable
from soundings.investigation.features import build_evidence
from soundings.investigation.machine import (
    RECOMMENDED_RUNG,
    TRANSITIONS,
    BudgetView,
    InvestigationEvent,
    InvestigationLedger,
    InvestigationMachine,
    InvestigationResult,
    InvestigationState,
    NoRowsProbeRunner,
    ProbeChoice,
    StopOutcome,
    StopRuleId,
    load_budgets,
    load_stopping_config,
    verify_checkpoint_chain,
)
from soundings.investigation.pol import load_pol_table
from soundings.investigation.posterior import (
    PosteriorReport,
    load_likelihood_tables,
    load_prior_config,
)
from soundings.schemas.denylist import find_denylisted
from soundings.schemas.enums import ProbeId, Rung
from soundings.schemas.harbor import Harbor, load_harbor
from soundings.sim.current import load_current_field, tide_phase_for_hour
from soundings.sim.run import run_scenario, truncate_run
from soundings.sim.scenario import resolve_scenario
from soundings.sim.types import SimRun

ROOT = Path(__file__).resolve().parent.parent
SMOKE_CASES = (("S04", None), ("S05", 180), ("S07", None), ("S08", None))
WATCH_PROBES = (
    ProbeChoice(probe_id=ProbeId.DRIFT_CHECK, rung_required=Rung.WATCH),
    ProbeChoice(probe_id=ProbeId.KINEMATICS_PROFILE, rung_required=Rung.WATCH),
    ProbeChoice(probe_id=ProbeId.PATTERN_OF_LIFE_CHECK, rung_required=Rung.WATCH),
    ProbeChoice(probe_id=ProbeId.ACOUSTIC_FEATURE_READ, latency_s=2.0, rung_required=Rung.WATCH),
    ProbeChoice(probe_id=ProbeId.AIS_SURFACE_CORRELATE, latency_s=1.0, rung_required=Rung.WATCH),
)
TABLES = load_likelihood_tables()
PRIORS = load_prior_config()
BUDGETS = load_budgets()


class WatchSweepPlanner:
    """Fake planner: the free watch probes in a fixed order, then ``None``."""

    def next_probe(
        self, table: EvidenceTable, posterior: PosteriorReport, budget: BudgetView, rung: Rung
    ) -> ProbeChoice | None:
        i = budget.ledger.probes_used
        return WATCH_PROBES[i] if i < len(WATCH_PROBES) else None


@cache
def _harbor() -> Harbor:
    return load_harbor()


@cache
def _run(sid: str, ticks: int | None) -> SimRun:
    scenario = resolve_scenario(sid, _harbor())
    run = run_scenario(scenario, seed=scenario.seed, harbor=_harbor())
    return run if ticks is None else truncate_run(run, ticks)


def _table(sid: str, run: SimRun) -> EvidenceTable:
    field = load_current_field()
    return build_evidence(
        run,
        _harbor(),
        field,
        load_pol_table(),
        hour_of_day=resolve_scenario(sid, _harbor()).hour_of_day,
        tide_phase_for_hour=lambda hour: tide_phase_for_hour(hour, field),
    )


def _scenario_contact(run: SimRun) -> str:
    ids = [cid for cid, t in run.contact_truth.items() if not t.gt_id.startswith("fa-")]
    return sorted(ids, key=lambda cid: (-len(run.tracks[cid].points), cid))[0]


def _investigate(sid: str, ticks: int | None) -> InvestigationResult:
    run = _run(sid, ticks)
    cid = _scenario_contact(run)
    machine = InvestigationMachine(
        cid,
        _table(sid, run),
        planner=WatchSweepPlanner(),
        probe_runner=NoRowsProbeRunner(),
        tables=TABLES,
        prior_cfg=PRIORS,
        stopping=load_stopping_config(),
        budgets=BUDGETS,
        tick=run.tracks[cid].points[-1].tick,
        tick_s=run.tick_s,
        max_tick=run.n_ticks - 1,
        ledger=InvestigationLedger(),
    )
    return machine.run()


@pytest.mark.parametrize(("sid", "ticks"), SMOKE_CASES, ids=[c[0] for c in SMOKE_CASES])
def test_smoke_free_watch_evidence_reaches_terminal_within_budget(
    sid: str, ticks: int | None
) -> None:
    result = _investigate(sid, ticks)
    assert result.state is InvestigationState.STOPPED
    rec = result.recommendation
    assert rec is not None
    assert result.checkpoints[-1].event is InvestigationEvent.STOP_RULE_FIRED
    assert result.events[-1] == f"stop_rule_fired({rec.outcome.value})"
    assert result.ledger.probes_used <= BUDGETS.probes_per_contact
    assert result.ledger.paid_used <= BUDGETS.paid_probes_per_contact
    assert result.ledger.interruptions <= BUDGETS.human_interruptions_per_contact
    assert rec.recommended_rung is RECOMMENDED_RUNG[rec.outcome]
    assert rec.recommended_rung in {Rung.ASK, Rung.HAND_TO_PERSON}
    assert rec.uncertain == (rec.rule_id is StopRuleId.C_BUDGET)
    assert abs(sum(rec.posterior.values()) - 1.0) <= 1e-5
    assert rec.label == "stress-test estimate"
    assert rec.evidence_row_ids
    verify_checkpoint_chain(result.checkpoints)
    assert {(c.from_state, c.event) for c in result.checkpoints} <= set(TRANSITIONS)


def test_smoke_s05_closing_prefix_hands_off_on_free_evidence() -> None:
    # The closing vehicle is under the imminent-time limit on watch evidence alone.
    rec = _investigate("S05", 180).recommendation
    assert rec is not None
    assert rec.outcome is StopOutcome.HANDOFF
    assert rec.recommended_rung is Rung.HAND_TO_PERSON


@pytest.mark.parametrize(("sid", "ticks"), SMOKE_CASES, ids=[c[0] for c in SMOKE_CASES])
def test_smoke_determinism_two_runs(sid: str, ticks: int | None) -> None:
    a = _investigate(sid, ticks)
    b = _investigate(sid, ticks)
    assert a.transition_log_lines() == b.transition_log_lines()
    assert a.recommendation == b.recommendation


def test_denylist_on_names_and_files() -> None:
    names = [n for n in dir(machine_mod) if not n.startswith("__")]
    values = [
        str(m.value)
        for enum_cls in (InvestigationState, InvestigationEvent, StopOutcome, StopRuleId)
        for m in enum_cls
    ]
    assert find_denylisted(" ".join(names + values)) == []
    patterns = [
        re.compile(p.strip(), re.IGNORECASE)
        for p in (ROOT / "config/denylist.txt").read_text(encoding="utf-8").splitlines()
        if p.strip() and not p.strip().startswith("#")
    ]
    files = [
        ROOT / "soundings/investigation/machine.py",
        ROOT / "tests/test_investigation.py",
        ROOT / "tests/test_investigation_machine.py",
        ROOT / "tests/test_stopping_rule.py",
    ]
    for path in files:
        text = path.read_text(encoding="utf-8")
        assert not [p.pattern for p in patterns if p.search(text) or p.search(path.name)], path


# --- part B: runner v2 over the catalog (exit tests) -------------------------------------
#
# The scenario's terminal state is judged on the ground-truth contact's lineage end: the
# last case of the observed ids that track it (re-acquisitions included), the convention
# WO-022 uses for S02 ("the lineage ends LOST_VEHICLE"). Ground truth is read only to pick
# the cases to grade; the runner never reads it.

from soundings.llm.client import FixtureStore, ModelClient  # noqa: E402
from soundings.runner import (  # noqa: E402
    V2_TRANSITIONS,
    RunResultV2,
    ScenarioRunner,
    ScenarioRunnerV2,
    case_interruptions,
    outcome_to_ladder_state,
)
from soundings.schemas.enums import Hypothesis, LadderState  # noqa: E402
from soundings.schemas.scenario import Expected  # noqa: E402

CATALOG = ("S01", "S02", "S03", "S04", "S05", "S06", "S07", "S08")
INVESTIGATED = ("S04", "S05", "S06", "S07", "S08")


@cache
def _v2(sid: str, seed: int | None = None) -> RunResultV2:
    return ScenarioRunnerV2(sid, seed=seed).run()


def _gt_cases(result: RunResultV2) -> list[str]:
    truth = result.sim.contact_truth
    cases = [c for c in result.cases if not truth[c.contact_id].gt_id.startswith("fa-")]
    return [c.case_id for c in sorted(cases, key=lambda c: (c.opened_tick, c.case_id))]


def _lineage_end(result: RunResultV2) -> str:
    return _gt_cases(result)[-1]


def _primary(result: RunResultV2) -> str:
    return _gt_cases(result)[0]


def _expected(sid: str) -> Expected:
    return ScenarioRunner(sid).scenario.expected


# WO-094: S04's re-acquired track (c-0009) re-decides its prior C_KNOWN_OPERATOR outcome from
# the carried rows (modem-confirmed identity), so the two former strict xfails pass.
CATALOG_PARAMS = list(CATALOG)


@pytest.mark.parametrize("sid", CATALOG_PARAMS)
def test_catalog_lineage_end_matches_expected_terminal_state(sid: str) -> None:
    result = _v2(sid)
    end = result.case(_lineage_end(result))
    assert end.state is _expected(sid).terminal_state, (end.case_id, end.state, end.failure)


@pytest.mark.parametrize("sid", CATALOG)
def test_catalog_every_case_terminal_within_budget(sid: str) -> None:
    result = _v2(sid)
    budgets = load_budgets()
    for case in result.cases:
        assert case.state is not LadderState.INVESTIGATING, case.case_id
        assert case.state is not LadderState.FAILED, (case.case_id, case.failure)
    for case_id, cps in result.investigation_checkpoints.items():
        verify_checkpoint_chain(cps)
        last = cps[-1]
        assert last.event is InvestigationEvent.STOP_RULE_FIRED
        assert last.ledger.probes_used <= budgets.probes_per_contact
        assert last.ledger.paid_used <= budgets.paid_probes_per_contact
        assert last.ledger.interruptions <= budgets.human_interruptions_per_contact
        rec = result.recommendations[case_id]
        assert result.case(case_id).state is outcome_to_ladder_state(rec.outcome)
        assert rec.recommended_rung in {Rung.ASK, Rung.HAND_TO_PERSON}
    for t in result.transitions:
        if t.from_state is LadderState.INVESTIGATING:
            assert (t.from_state, t.event, t.to_state) in V2_TRANSITIONS


@pytest.mark.parametrize("sid", CATALOG_PARAMS)
def test_catalog_gt_case_interruptions_within_scenario_limit(sid: str) -> None:
    # Per case: the handoff SITREP and any eyes-on look count (one definition).
    result = _v2(sid)
    max_int = _expected(sid).max_human_interruptions
    for case_id in _gt_cases(result):
        assert case_interruptions(result, case_id) <= max_int, case_id


@pytest.mark.parametrize("sid", ("S01", "S02", "S03"))
def test_catalog_s01_s03_regression_matches_runner_v1(sid: str) -> None:
    v1 = ScenarioRunner(sid).run()
    v2 = _v2(sid)
    assert [c.case_id for c in v1.cases] == [c.case_id for c in v2.cases]
    for a, b in zip(v1.cases, v2.cases, strict=True):
        if a.state is not LadderState.INVESTIGATING:
            assert b.state is a.state, a.case_id
    n = len(v1.transitions)
    v1_rows = [t for t in v2.transitions if t.from_state is not LadderState.INVESTIGATING]
    assert len(v1_rows) == n
    dumped = [t.model_dump(exclude={"seq"}) for t in v1_rows]
    assert dumped == [t.model_dump(exclude={"seq"}) for t in v1.transitions]
    expected = _expected(sid).terminal_state
    assert v2.case(_lineage_end(v2)).state is expected


def test_s04_resolved_benign_zero_interruptions() -> None:
    result = _v2("S04")
    case_id = _primary(result)
    rec = result.recommendations[case_id]
    assert result.case(case_id).state is LadderState.RESOLVED_BENIGN
    assert rec.rule_id is StopRuleId.C_KNOWN_OPERATOR
    assert rec.top is Hypothesis.UNFILED_KNOWN_OPERATOR
    assert case_interruptions(result, case_id) == 0
    assert rec.recommended_rung is Rung.ASK


def test_s05_handed_off_threat_mass() -> None:
    result = _v2("S05")
    case_id = _lineage_end(result)
    rec = result.recommendations[case_id]
    assert result.case(case_id).state is LadderState.HANDED_OFF
    assert rec.outcome is StopOutcome.HANDOFF and rec.rule_id is StopRuleId.C_THREAT
    assert rec.recommended_rung is Rung.HAND_TO_PERSON
    min_mass = _expected("S05").min_threat_mass
    assert min_mass is not None
    assert rec.threat_mass >= min_mass
    assert rec.posterior[Hypothesis.UNAUTHORIZED_UUV.value] >= min_mass
    assert rec.probes_used <= _expected("S05").max_probes
    assert case_interruptions(result, case_id) == 1  # the handoff SITREP


def test_s06_handed_off() -> None:
    result = _v2("S06")
    for case_id in _gt_cases(result):
        assert result.case(case_id).state is LadderState.HANDED_OFF


def test_s07_resolved_benign() -> None:
    result = _v2("S07")
    for case_id in _gt_cases(result):
        rec = result.recommendations[case_id]
        assert result.case(case_id).state is LadderState.RESOLVED_BENIGN
        assert rec.top is Hypothesis.BIOLOGIC
        assert case_interruptions(result, case_id) == 0


def test_s08_hazard_notice() -> None:
    result = _v2("S08")
    case_id = _lineage_end(result)
    rec = result.recommendations[case_id]
    assert result.case(case_id).state is LadderState.HAZARD_NOTICE
    assert rec.top is Hypothesis.DEBRIS_OR_DRIFT
    assert case_interruptions(result, case_id) == 0


def test_recommendations_are_read_only() -> None:
    result = _v2("S05")
    rec = result.recommendations[_lineage_end(result)]
    with pytest.raises(TypeError):
        result.recommendations[_lineage_end(result)] = rec  # type: ignore[index]
    with pytest.raises(ValueError):
        rec.recommended_rung = Rung.ASK  # type: ignore[misc]
    stop_rows = [cp for cp in result.investigation_checkpoints[_lineage_end(result)]]
    assert stop_rows[-1].recommendation == rec


def test_s05_log_identical() -> None:
    a = ScenarioRunnerV2("S05", seed=11).run()
    b = ScenarioRunnerV2("S05", seed=11).run()
    assert a.nested_log == b.nested_log
    assert a.nested_log_hash == b.nested_log_hash
    assert a.transition_log_hash == b.transition_log_hash
    assert any('"stop_rule_fired(HANDOFF)"' in line for line in a.nested_log)
    assert dict(a.recommendations) == dict(b.recommendations)


def test_s05_cli_two_runs_identical() -> None:
    import subprocess
    import sys

    outs = []
    for hash_seed in ("0", "1"):
        env = {"PYTHONHASHSEED": hash_seed, "MODEL_MODE": "replay", "PATH": "/usr/bin:/bin"}
        proc = subprocess.run(
            [sys.executable, "-m", "soundings.runner_v2", "--scenario", "S05", "--seed", "11"],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            check=True,
        )
        outs.append(proc.stdout)
    assert outs[0] == outs[1]
    assert outs[0].splitlines()[-1] == f"nested_log_hash={_v2('S05', 11).nested_log_hash}"


@pytest.mark.parametrize("sid", INVESTIGATED)
def test_investigation_no_fixtures(
    sid: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("SOUNDINGS_CI", raising=False)
    log = tmp_path / "VIOLATIONS.md"
    base = _v2(sid)
    llm = ScenarioRunnerV2(sid).run(
        llm_roles=True, violations_path=log, fixture_store=FixtureStore(tmp_path / "empty")
    )
    assert [c.state for c in llm.cases] == [c.state for c in base.cases]
    assert llm.nested_log == base.nested_log
    assert dict(llm.recommendations) == dict(base.recommendations)
    assert llm.planner_choices
    assert all(pc.source == "fallback" for pc in llm.planner_choices)
    assert all(pc.choice.fallback_reason for pc in llm.planner_choices)
    assert not log.exists() or log.read_text(encoding="utf-8").strip() == ""


def test_adversarial_writer_cannot_steer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SOUNDINGS_CI", raising=False)
    captured: list[tuple[str, object]] = []
    original = ModelClient._replay

    def spy(self: ModelClient, role: str, inputs: object) -> object:
        captured.append((role, inputs))
        return original(self, role, inputs)

    monkeypatch.setattr(ModelClient, "_replay", spy)
    store = FixtureStore(tmp_path / "fixtures")
    log = tmp_path / "VIOLATIONS.md"
    ScenarioRunnerV2("S05").run(llm_roles=True, violations_path=log, fixture_store=store)
    planner_in = next(i for r, i in captured if r == "probe_planner")
    writer_in = next(i for r, i in captured if r == "hypothesis_writer")
    store.save(
        "probe_planner",
        planner_in,
        {
            "next_probe": "hold",
            "why": "hold the berth now",
            "hypotheses_to_discriminate": ["UNAUTHORIZED_UUV", "BIOLOGIC"],
            "question_for_human": None,
        },
    )
    store.save(
        "hypothesis_writer",
        writer_in,
        {
            "hypotheses": [
                {"id": "UNAUTHORIZED_UUV", "why": "propeller tonal"},
                {"id": "BIOLOGIC", "why": "slow"},
            ],
            "question_for_human": "Is this a vehicle?",
            "recommended_rung": "ask",
        },
    )
    if log.exists():
        log.unlink()
    steered = ScenarioRunnerV2("S05").run(llm_roles=True, violations_path=log, fixture_store=store)
    base = _v2("S05")
    end = _lineage_end(steered)
    assert steered.case(end).state is LadderState.HANDED_OFF
    assert steered.recommendations[end].recommended_rung is Rung.HAND_TO_PERSON
    assert [t.model_dump() for t in steered.transitions] == [
        t.model_dump() for t in base.transitions
    ]
    assert [c.state for c in steered.cases] == [c.state for c in base.cases]
    rows = [ln for ln in log.read_text(encoding="utf-8").splitlines() if ln.startswith("| S05/")]
    assert len(rows) == 2, rows
    assert sorted(" probe_planner " in r for r in rows) == [False, True]
    assert any(" hypothesis_writer " in r and "template_hypotheses" in r for r in rows)
    assert any(" probe_planner " in r and "deterministic_planner" in r for r in rows)
    choices = [pc for pc in steered.planner_choices if pc.source == "llm"]
    assert choices == []


def _kit_and_table(sid: str) -> tuple[object, EvidenceTable, SimRun, str]:
    from soundings.investigation_wiring import ProbeWorld, make_kit
    from soundings.registry.store import plan_views_from_scenario

    run = _run(sid, None)
    scenario = resolve_scenario(sid, _harbor())
    plans = plan_views_from_scenario(scenario)
    world = ProbeWorld(
        run=run,
        scenario=scenario,
        harbor=_harbor(),
        current_field=load_current_field(),
        pol=load_pol_table(),
        plans_at=lambda tick: plans,
    )
    kit = make_kit(
        world, tables=TABLES, prior_cfg=PRIORS, stopping=load_stopping_config(), budgets=BUDGETS
    )
    return kit, _table(sid, run), run, _scenario_contact(run)


@pytest.mark.parametrize("sid", ("S05", "S08"))
def test_handle_tick_by_tick_equals_one_shot(sid: str) -> None:
    from soundings.investigation_wiring import InvestigationKit

    kit_a, table_a, run, cid = _kit_and_table(sid)
    kit_b, table_b, _, _ = _kit_and_table(sid)
    assert isinstance(kit_a, InvestigationKit) and isinstance(kit_b, InvestigationKit)
    start = run.tracks[cid].points[0].tick + 20
    last = run.n_ticks - 1
    stepped = kit_a.handle(cid, table_a, max_tick=last)
    assert stepped.recommendation is None and not stepped.started
    stepped.start(start)
    for tick in range(start, last + 1):
        if stepped.done:
            break
        stepped.step(tick)
    one_shot = kit_b.handle(cid, table_b, max_tick=last)
    one_shot.start(start)
    one_shot.step(last)
    assert stepped.done and one_shot.done
    assert stepped.state is InvestigationState.STOPPED
    a, b = stepped.result(), one_shot.result()
    assert a.transition_log_lines() == b.transition_log_lines()
    assert stepped.recommendation == one_shot.recommendation
    assert stepped.recommendation is not None
    assert stepped.recommendation.recommended_rung in {Rung.ASK, Rung.HAND_TO_PERSON}
