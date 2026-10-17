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
