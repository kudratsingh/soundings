"""WO-051 unit tests: per-policy eval metrics over hand-built PolicyRuns."""

from __future__ import annotations

from collections.abc import Sequence

import pytest

from soundings.eval.metrics import (
    AT_OR_BELOW_ASK_TERMINALS,
    at_or_below_ask,
    compute_false_alarms,
    compute_metrics,
    diff_planners,
    median_seconds,
    rolled_outcomes,
)
from soundings.eval.policies import (
    ContactSpan,
    GtContact,
    PlannerStats,
    PolicyEvent,
    PolicyName,
    PolicyRun,
    ShutdownInterval,
    Transition,
    build_policy_run,
    outcome_for_gt,
)
from soundings.eval.report import NO_LLM_EFFECT_SENTENCE, SAME_PROBES_SENTENCE, llm_section
from soundings.schemas import (
    Hypothesis,
    LadderConfig,
    LadderState,
    ProbeId,
    Rung,
    load_ladder_config,
)

S = LadderState
E = PolicyEvent
H = Hypothesis


def tr(
    cid: str,
    t: float,
    frm: LadderState | None,
    to: LadderState,
    event: PolicyEvent = E.STATE_CHANGE,
    probe: ProbeId | None = None,
) -> Transition:
    return Transition(
        seq=0,
        contact_id=cid,
        tick=int(t // 2.0),
        t_s=t,
        from_state=frm,
        to_state=to,
        event=event,
        probe_id=probe,
    )


def path(
    cid: str, states: Sequence[LadderState], *, t0: float = 0.0, dt: float = 10.0
) -> list[Transition]:
    out: list[Transition] = []
    prev: LadderState | None = None
    for i, st in enumerate(states):
        ev = E.CONTACT_DETECTED if prev is None else E.STATE_CHANGE
        out.append(tr(cid, t0 + i * dt, prev, st, ev))
        prev = st
    return out


def span(
    cid: str, gt: str, truth: Hypothesis, *, fa: bool = False, first: float = 0.0
) -> ContactSpan:
    return ContactSpan(cid, first, first + 300.0, 33.741, -118.2582, gt, truth, fa)


def mkrun(
    contacts: Sequence[tuple[str, str, Hypothesis, bool]],
    transitions: Sequence[Transition],
    *,
    policy: PolicyName = PolicyName.SOUNDINGS,
    scenario: str = "S01",
    seed: int = 1,
    end_s: float = 1800.0,
    shutdowns: Sequence[ShutdownInterval] = (),
    planner: str = "fallback",
    stats: PlannerStats | None = None,
) -> PolicyRun:
    spans = [span(cid, gt, truth, fa=fa) for cid, gt, truth, fa in contacts]
    gts = [GtContact(gt_id=gt, truth=truth, is_false_alarm=fa) for _, gt, truth, fa in contacts]
    ordered = sorted(transitions, key=lambda t: (t.t_s, t.contact_id))
    steps = [t.model_copy(update={"seq": i}) for i, t in enumerate(ordered)]
    return build_policy_run(
        policy=policy,
        scenario_id=scenario,
        seed=seed,
        first_gt_id=gts[0].gt_id,
        spans=spans,
        gts=gts,
        transitions=steps,
        shutdowns=shutdowns,
        end_s=end_s,
        track_hash="0" * 64,
        variant="nominal",
        planner=planner,  # type: ignore[arg-type]
        planner_stats=stats,
    )


@pytest.fixture(scope="module")
def ladder() -> LadderConfig:
    return load_ladder_config()


def test_terminal_set_is_the_addenda_rule() -> None:
    assert AT_OR_BELOW_ASK_TERMINALS == {
        S.CLEARED_AT_ASK,
        S.RESOLVED_BENIGN,
        S.HAZARD_NOTICE,
        S.LOST_VEHICLE,
    }


def test_at_or_below_ask_rule(ladder: LadderConfig) -> None:
    lost = path("c-0001", [S.DETECTED, S.WATCHING, S.ASKING, S.LOST_VEHICLE])
    slowed = path("c-0002", [S.DETECTED, S.ASKING, S.ADVISING_SLOW, S.CLEARED_AT_ASK])
    fa = path("c-0003", [S.DETECTED, S.ASKING, S.CLEARED_AT_ASK])
    run = mkrun(
        [
            ("c-0001", "gt-1", H.FILED_ON_PLAN, False),
            ("c-0002", "gt-2", H.FILED_ON_PLAN, False),
            ("c-0003", "fa-0001", H.BIOLOGIC, True),
        ],
        lost + slowed + fa,
    )
    lost_o = outcome_for_gt(run, "gt-1")
    assert lost_o.terminal_state is S.LOST_VEHICLE and lost_o.max_rung is Rung.ASK
    assert at_or_below_ask(lost_o)
    slowed_o = outcome_for_gt(run, "gt-2")
    assert slowed_o.terminal_state is S.CLEARED_AT_ASK and slowed_o.max_rung is Rung.SLOW
    assert not at_or_below_ask(slowed_o)
    fa_o = outcome_for_gt(run, "fa-0001")
    assert not at_or_below_ask(fa_o)

    m = compute_metrics([run], ladder_cfg=ladder)[PolicyName.SOUNDINGS]
    assert m.gt_contacts == 2  # the false alarm is not in the denominator
    assert m.at_or_below_ask == 1
    assert m.at_or_below_ask_pct == 50.0
    assert (m.watch, m.ask, m.slow, m.hold, m.hand_to_person) == (0, 1, 1, 0, 0)
    fa_m = compute_false_alarms([run])[PolicyName.SOUNDINGS]
    assert fa_m.false_alarms == 1 and fa_m.detected == 1


def test_median_ignores_unhandled_and_reports_them(ladder: LadderConfig) -> None:
    fast = path("c-0001", [S.DETECTED, S.CLEARED_AT_ASK], dt=10.0)
    slow = path("c-0002", [S.DETECTED, S.ASKING, S.CLEARED_AT_ASK], dt=15.0)
    never = path("c-0003", [S.DETECTED, S.WATCHING], dt=10.0)
    run = mkrun(
        [
            ("c-0001", "gt-1", H.FILED_ON_PLAN, False),
            ("c-0002", "gt-2", H.FILED_ON_PLAN, False),
            ("c-0003", "gt-3", H.FILED_ON_PLAN, False),
        ],
        fast + slow + never,
    )
    m = compute_metrics([run], ladder_cfg=ladder)[PolicyName.SOUNDINGS]
    assert m.median_s_seen_to_handled == 20.0  # median of 10 and 30
    assert m.unhandled == 1
    assert median_seconds([None, None]) == (None, 2)
    assert median_seconds([5.0, None, 1.0, 3.0]) == (3.0, 1)


def test_interruptions_per_hour_use_summed_sim_hours(ladder: LadderConfig) -> None:
    def handed(cid: str) -> list[Transition]:
        return [
            tr(cid, 0.0, None, S.DETECTED, E.CONTACT_DETECTED),
            tr(cid, 10.0, S.DETECTED, S.HANDED_OFF, E.HANDED_TO_PERSON),
        ]

    a = mkrun([("c-0001", "gt-1", H.UNAUTHORIZED_UUV, False)], handed("c-0001"), end_s=1800.0)
    b = mkrun(
        [
            ("c-0001", "gt-1", H.UNAUTHORIZED_UUV, False),
            ("c-0002", "gt-2", H.UNAUTHORIZED_UUV, False),
        ],
        handed("c-0001") + handed("c-0002"),
        seed=2,
        end_s=5400.0,
    )
    assert (a.human_interruptions, b.human_interruptions) == (1, 2)
    m = compute_metrics([a, b], ladder_cfg=ladder)[PolicyName.SOUNDINGS]
    assert m.sim_hours == 2.0
    assert m.interruptions == 3
    assert m.interruptions_per_hour == 1.5
    assert m.missed_threats == 0


def test_missed_threat_counted(ladder: LadderConfig) -> None:
    run = mkrun(
        [("c-0001", "gt-1", H.UNAUTHORIZED_DIVER, False)],
        path("c-0001", [S.DETECTED, S.ASKING, S.CLEARED_AT_ASK]),
    )
    assert compute_metrics([run], ladder_cfg=ladder)[PolicyName.SOUNDINGS].missed_threats == 1


def _held_run() -> PolicyRun:
    steps = path("c-0001", [S.DETECTED, S.ASKING, S.HOLDING])
    hold = ShutdownInterval(
        contact_id="c-0001", asset_id="fuel_berth_1", kind="hold", start_s=20.0, end_s=320.0
    )
    return mkrun([("c-0001", "gt-1", H.BIOLOGIC, False)], steps, shutdowns=[hold])


def test_usd_zero_by_default_and_minutes_times_rate(
    ladder: LadderConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = _held_run()
    m = compute_metrics([run], ladder_cfg=ladder)[PolicyName.SOUNDINGS]
    assert m.shutdown_minutes == 5.0
    assert ladder.costs["shutdown_usd_per_min_default"] == 0
    assert m.shutdown_usd == 0.0
    assert m.false_shutdowns == 1  # benign contact caused the hold

    costs = dict(ladder.costs)
    monkeypatch.setitem(costs, "shutdown_usd_per_min_default", 12.5)
    priced = ladder.model_copy(update={"costs": costs})
    m2 = compute_metrics([run], ladder_cfg=priced)[PolicyName.SOUNDINGS]
    assert m2.shutdown_usd == 62.5


def test_compute_metrics_groups_in_policy_order(ladder: LadderConfig) -> None:
    steps = path("c-0001", [S.DETECTED, S.HANDED_OFF])
    runs = [
        mkrun([("c-0001", "gt-1", H.BIOLOGIC, False)], steps, policy=PolicyName.HUMAN_QUEUE),
        mkrun([("c-0001", "gt-1", H.BIOLOGIC, False)], steps, policy=PolicyName.BANG),
    ]
    assert list(compute_metrics(runs, ladder_cfg=ladder)) == [
        PolicyName.BANG,
        PolicyName.HUMAN_QUEUE,
    ]


# ---------------------------------------------------------------------------
# diff_planners
# ---------------------------------------------------------------------------


def _probe_run(probes: Sequence[ProbeId], *, planner: str, stats: PlannerStats | None) -> PolicyRun:
    steps = [tr("c-0001", 0.0, None, S.DETECTED, E.CONTACT_DETECTED)]
    steps.append(tr("c-0001", 2.0, S.DETECTED, S.INVESTIGATING))
    for i, p in enumerate(probes):
        steps.append(tr("c-0001", 4.0 + i, S.INVESTIGATING, S.INVESTIGATING, E.PROBE_CALL, probe=p))
    steps.append(tr("c-0001", 4.0 + len(probes) * 10.0, S.INVESTIGATING, S.RESOLVED_BENIGN))
    return mkrun(
        [("c-0001", "gt-1", H.UNFILED_KNOWN_OPERATOR, False)],
        steps,
        scenario="S04",
        planner=planner,
        stats=stats,
    )


def test_diff_planners_identical_runs() -> None:
    probes = [ProbeId.REGISTRY_LOOKUP, ProbeId.ACOUSTIC_HAIL]
    fb = _probe_run(probes, planner="fallback", stats=PlannerStats(calls=2, fallbacks=2))
    llm = _probe_run(probes, planner="llm", stats=PlannerStats(calls=2, fallbacks=2))
    diff = diff_planners([fb], [llm])
    assert diff.runs_compared == 1 and diff.contacts_compared == 1
    assert diff.differences == ()
    assert diff.stats == PlannerStats(calls=2, fallbacks=2)
    section = llm_section(diff)
    assert section.sentence == NO_LLM_EFFECT_SENTENCE

    hit = llm.model_copy(update={"planner_stats": PlannerStats(calls=2, fixture_hits=2)})
    assert llm_section(diff_planners([fb], [hit])).sentence == SAME_PROBES_SENTENCE


def test_diff_planners_lists_difference_with_verdict() -> None:
    fb = _probe_run(
        [ProbeId.REGISTRY_LOOKUP, ProbeId.ACOUSTIC_HAIL, ProbeId.KINEMATICS_PROFILE],
        planner="fallback",
        stats=None,
    )
    llm = _probe_run(
        [ProbeId.CAMERA_CUE], planner="llm", stats=PlannerStats(calls=1, fixture_hits=1)
    )
    diff = diff_planners([fb], [llm])
    assert len(diff.differences) == 1
    d = diff.differences[0]
    assert (d.scenario_id, d.seed, d.gt_id) == ("S04", 1, "gt-1")
    assert "probe_sequence" in d.fields and "probes_used" in d.fields
    assert d.llm_probes == (ProbeId.CAMERA_CUE,)
    assert d.fallback_terminal is d.llm_terminal is S.RESOLVED_BENIGN
    assert d.verdict.startswith("helped")
    assert "fewer probes" in d.verdict and "faster outcome" in d.verdict
    section = llm_section(diff)
    assert section.sentence not in (NO_LLM_EFFECT_SENTENCE, SAME_PROBES_SENTENCE)


def test_rolled_outcomes_cover_every_gt() -> None:
    run = _held_run()
    assert [o.gt_id for o in rolled_outcomes(run)] == ["gt-1"]
