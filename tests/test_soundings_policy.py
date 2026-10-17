"""WO-051 part B: the soundings policy over runner v3, on the real catalog.

S04's re-acquired track ends in an uncertain budget handoff on main (DECISIONS 2026-10-17
07:31 and 08:30; strict xfails in tests/test_investigation.py and
tests/test_catalog_expected.py). The S04 clause here is a strict xfail for the same open
question; nothing was tuned to move it.
"""

from __future__ import annotations

from functools import cache
from pathlib import Path

import pytest

import soundings.runner as runner_module
from soundings.eval import soundings_policy as sp
from soundings.eval.perturb import perturb_likelihoods, unpin_acoustics
from soundings.eval.policies import (
    PlannerStats,
    PolicyName,
    PolicyRun,
    make_policy,
    outcome_for_gt,
    rung_index,
)
from soundings.eval.soundings_policy import SoundingsPolicy
from soundings.investigation.posterior import load_likelihood_tables
from soundings.llm.client import FixtureStore
from soundings.runner import ScenarioRunner, ScenarioRunnerV3
from soundings.schemas import LadderState, Rung, Scenario, load_likelihood_spec
from soundings.sim.scenario import resolve_scenario

SEEDS = (1, 2, 3, 4, 5)
S04_OPEN_QUESTION = (
    "S04's re-acquired track ends in an uncertain C_BUDGET handoff under runner v3 "
    "(DECISIONS 2026-10-17 07:31/08:30; strict xfail upstream); open question for the "
    "coordinator, no table or threshold changed"
)


@cache
def _scenario(sid: str) -> Scenario:
    return resolve_scenario(sid)


@cache
def _run(sid: str, seed: int, policy: str = "soundings") -> PolicyRun:
    if policy == "soundings":
        return SoundingsPolicy().run(_scenario(sid), seed)
    return make_policy(policy).run(_scenario(sid), seed)


def _gt_ids(run: PolicyRun) -> list[str]:
    return sorted(g.gt_id for g in run.gt_contacts if not g.is_false_alarm)


def test_policy_protocol_and_name() -> None:
    p = make_policy(PolicyName.SOUNDINGS)
    assert isinstance(p, SoundingsPolicy)
    assert p.name is PolicyName.SOUNDINGS and p.planner == "fallback"
    with pytest.raises(ValueError):
        SoundingsPolicy(planner="random")  # type: ignore[arg-type]


def test_s05_handed_off_through_holding() -> None:
    run = _run("S05", 1)
    assert run.policy is PolicyName.SOUNDINGS and run.planner == "fallback"
    assert run.terminal_state is LadderState.HANDED_OFF
    gt = outcome_for_gt(run, "gt-1")
    ids = set(gt.observed_contact_ids)
    assert any(t.to_state is LadderState.HOLDING for t in run.transition_log if t.contact_id in ids)
    assert not gt.missed and not run.missed_threat
    assert run.shutdown_minutes > 0.0  # the hold is a shutdown interval (hold_shutdowns)
    assert {s.kind for s in run.shutdowns} == {"hold"}
    bang = _run("S05", 1, "bang")
    assert bang.shutdown_minutes > run.shutdown_minutes


@pytest.mark.parametrize("seed", SEEDS)
def test_s01_s03_cleared_at_ask(seed: int) -> None:
    for sid, expected_gts in (("S01", 1), ("S03", 2)):
        run = _run(sid, seed)
        gts = _gt_ids(run)
        assert len(gts) == expected_gts
        for gid in gts:
            g = outcome_for_gt(run, gid)
            assert g.terminal_state is LadderState.CLEARED_AT_ASK, (sid, gid)
            assert g.max_rung is not None and rung_index(g.max_rung) <= rung_index(Rung.ASK)
            assert g.interruptions == 0, (sid, gid)


@pytest.mark.xfail(strict=True, reason=S04_OPEN_QUESTION)
@pytest.mark.parametrize("seed", SEEDS)
def test_s04_resolved_benign(seed: int) -> None:
    g = outcome_for_gt(_run("S04", seed), "gt-1")
    assert g.terminal_state is LadderState.RESOLVED_BENIGN
    assert g.max_rung is not None and rung_index(g.max_rung) <= rung_index(Rung.ASK)
    assert g.interruptions == 0


@pytest.mark.parametrize("sid", ["S01", "S02", "S05", "S08"])
def test_same_track_hash_for_all_policies(sid: str) -> None:
    for seed in (1, 2):
        hashes = {_run(sid, seed, p).track_hash for p in ("soundings", "bang", "human_queue")}
        assert len(hashes) == 1


def test_llm_without_fixtures_equals_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("SOUNDINGS_CI", raising=False)
    log = tmp_path / "violations.md"
    llm = SoundingsPolicy(
        planner="llm", fixture_store=FixtureStore(tmp_path / "empty"), violations_log=log
    ).run(_scenario("S04"), 1)
    fb = _run("S04", 1)
    assert llm.planner == "llm"
    assert llm.transition_log == fb.transition_log
    assert llm.contacts == fb.contacts
    assert llm.terminal_state == fb.terminal_state
    stats = llm.planner_stats
    assert stats is not None and stats.calls > 0
    assert stats.fixture_hits == 0 and stats.fallbacks == stats.calls
    assert fb.planner_stats == PlannerStats()
    assert not log.exists()


def test_sim_likelihoods_none_leaves_runner_unchanged() -> None:
    sc = _scenario("S05")
    v1 = ScenarioRunner(sc, seed=3).run()
    v1n = ScenarioRunner(sc, seed=3, sim_likelihoods=None, stopping=None).run()
    assert v1n.transition_log_hash == v1.transition_log_hash
    a = ScenarioRunnerV3(sc, seed=3).run()
    b = ScenarioRunnerV3(sc, seed=3, sim_likelihoods=None).run()
    assert a.transition_log_hash == b.transition_log_hash
    assert a.ladder_log_hash == b.ladder_log_hash


def test_sim_likelihoods_reach_sim_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """The perturbed tables change the sim's acoustic draws; the scorer keeps
    config/likelihoods.yaml (the investigation kit is built from the real tables)."""
    real = load_likelihood_spec()
    perturbed = perturb_likelihoods(real, 1.0, 17)
    sc = unpin_acoustics(_scenario("S05"), real)
    seen: list[object] = []
    original = runner_module.make_kit

    def spy(*args: object, **kw: object) -> object:
        seen.append(kw["tables"])
        return original(*args, **kw)

    monkeypatch.setattr("soundings.runner.make_kit", spy)
    nominal = ScenarioRunnerV3(sc, seed=1).run()
    mis = ScenarioRunnerV3(sc, seed=1, sim_likelihoods=perturbed).run()
    assert len(seen) == 2
    real_rows = load_likelihood_tables()._rows
    assert [t._rows for t in seen] == [real_rows, real_rows]  # type: ignore[attr-defined]
    acoustic_nominal = nominal.sim.detections
    acoustic_mis = mis.sim.detections
    assert acoustic_nominal != acoustic_mis
    assert mis.sim_extended is not None and nominal.sim_extended is not None


@pytest.mark.parametrize("seed", [2, 5])
def test_s05_refused_step_is_audited(seed: int) -> None:
    """A refused ladder step's guard rows are keyed by the contact the guard read (or
    ``unattributed``), so runner v3 completes S05 at seeds 2 and 5 under the signed
    orders (it raised 'malformed idempotency key' before WO-051)."""
    result = ScenarioRunnerV3(_scenario("S05"), seed=seed).run()
    assert result.audit_ok
    run = sp.SoundingsPolicy()._from_run_result(_scenario("S05"), result, False)
    assert run.terminal_state is LadderState.HANDED_OFF
    assert not run.missed_threat


def test_planner_stats_counting() -> None:
    result = ScenarioRunnerV3(_scenario("S05"), seed=1).run()
    assert sp.planner_stats(result) == PlannerStats()  # deterministic planner: no calls


def test_post_handoff_watch_not_in_log() -> None:
    run = _run("S05", 1)
    assert any(t.to_state is LadderState.HANDED_OFF for t in run.transition_log)
    for t in run.transition_log:
        assert t.from_state not in sp.POST_HANDOFF_STATES
        assert t.to_state is not LadderState.WATCH_AFTER_HANDOFF
