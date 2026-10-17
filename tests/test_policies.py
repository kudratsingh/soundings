"""WO-050 unit tests: PolicyRun records and the counting rules shared by every policy."""

from __future__ import annotations

import ast
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

from soundings.canonical import canonical_json, round6
from soundings.eval import policies as P
from soundings.eval.policies import (
    BASELINE_EDGES,
    CLOSURE_ACTION,
    POLICIES,
    BangPolicy,
    ContactOutcome,
    ContactSpan,
    ForgedTransitionError,
    GtContact,
    HumanQueuePolicy,
    InterruptionKind,
    PolicyEvent,
    PolicyName,
    PolicyRun,
    ShutdownInterval,
    Transition,
    build_policy_run,
    count_interruptions,
    hold_shutdowns,
    interruption_kind,
    load_eval_config,
    make_policy,
    max_rung,
    merge_intervals,
    outcome_for_gt,
    serial_queue,
    shutdown_minutes,
)
from soundings.schemas import (
    AcousticSpec,
    FeatureName,
    Hypothesis,
    LadderState,
    ProbeId,
    Rung,
    find_denylisted,
    load_ladder_config,
    load_likelihood_spec,
)
from soundings.sim.hashing import track_hash
from soundings.sim.run import run_scenario
from soundings.sim.scenario import resolve_scenario
from tests.support.policy_runs import BASELINES, SEEDS, catalog, catalog_runs

S = LadderState
E = PolicyEvent
REPO = Path(__file__).resolve().parent.parent
POLICIES_PY = REPO / "soundings" / "eval" / "policies.py"


def tr(
    frm: LadderState | None,
    to: LadderState,
    event: PolicyEvent = E.STATE_CHANGE,
    *,
    cid: str = "c-0001",
    t: float = 0.0,
    seq: int = 0,
    probe: ProbeId | None = None,
    action: str | None = None,
    asset: str | None = None,
) -> Transition:
    return Transition(
        seq=seq,
        contact_id=cid,
        tick=int(t // 2.0),
        t_s=t,
        from_state=frm,
        to_state=to,
        event=event,
        action=action,
        asset_id=asset,
        probe_id=probe,
    )


def log(*steps: Transition) -> list[Transition]:
    """Renumber steps 0..n-1."""
    return [s.model_copy(update={"seq": i}) for i, s in enumerate(steps)]


def span(
    cid: str,
    first: float,
    last: float,
    *,
    gt: str | None = None,
    truth: Hypothesis | None = None,
    fa: bool = False,
    lat: float = 33.7410,
    lon: float = -118.2582,
) -> ContactSpan:
    return ContactSpan(cid, first, last, lat, lon, gt, truth, fa)


def run_from(
    spans: list[ContactSpan],
    gts: list[GtContact],
    transitions: list[Transition],
    shutdowns: tuple[ShutdownInterval, ...] = (),
    *,
    end_s: float = 1200.0,
) -> PolicyRun:
    return build_policy_run(
        policy=PolicyName.BANG,
        scenario_id="S05",
        seed=1,
        first_gt_id=gts[0].gt_id if gts else None,
        spans=spans,
        gts=gts,
        transitions=transitions,
        shutdowns=shutdowns,
        end_s=end_s,
        track_hash="0" * 64,
        variant="nominal",
        planner="none",
    )


def _floats(obj: Any) -> list[float]:
    if isinstance(obj, float):
        return [obj]
    if isinstance(obj, dict):
        return [f for v in obj.values() for f in _floats(v)]
    if isinstance(obj, list):
        return [f for v in obj for f in _floats(v)]
    return []


# ---------------------------------------------------------------------------


def test_policy_names_and_order() -> None:
    assert [p.value for p in POLICIES] == ["soundings", "bang", "human_queue"]
    assert make_policy("bang").name is PolicyName.BANG
    assert make_policy(PolicyName.HUMAN_QUEUE).name is PolicyName.HUMAN_QUEUE
    assert isinstance(make_policy("bang"), P.Policy)


def test_eval_config_values() -> None:
    cfg = load_eval_config()
    assert cfg.baselines.human_review_s == 120.0
    assert cfg.baselines.berth_reopen_after_s == 600.0
    assert "human_queue_shutdown" in cfg.baselines.assumptions
    assert "not simulated" in cfg.baselines.assumptions["human_queue_shutdown"]
    assert cfg.baselines.assumption_note
    assert cfg.eval.seeds == (1, 2, 3, 4, 5)
    assert cfg.eval.perturb_magnitude == 0.5
    assert cfg.eval.perturb_seed == 17


def test_policyrun_roundtrip() -> None:
    run = BangPolicy().run(resolve_scenario("S05"), 1)
    again = PolicyRun.model_validate_json(run.model_dump_json())
    assert again == run
    assert canonical_json(again) == canonical_json(run)
    for c in run.contacts:
        assert ContactOutcome.model_validate_json(c.model_dump_json()) == c
    # Every float in the record is already at 6 dp.
    for f in _floats(run.model_dump(mode="json")):
        assert f == round6(f)
    # Floats given at full precision come out through round6.
    c = ContactOutcome(
        observed_contact_id="c-0001",
        gt_id="gt-1",
        truth=Hypothesis.UNAUTHORIZED_UUV,
        is_false_alarm=False,
        first_seen_s=1 / 3,
        handled_s=2 / 3,
        terminal_state=S.HANDED_OFF,
        max_rung=Rung.HAND_TO_PERSON,
        interruptions=2,
        shutdown_minutes=10 / 7,
        false_shutdown=False,
        probes_used=0,
        probe_sequence=(),
    )
    assert c.first_seen_s == 0.333333
    assert c.handled_s == 0.666667
    assert c.shutdown_minutes == 1.428571
    assert ContactOutcome.model_validate_json(c.model_dump_json()) == c
    rerounded = run.model_copy(update={"shutdown_minutes": 0.0}).model_dump()
    rerounded["shutdown_minutes"] = 12.3456789
    assert PolicyRun.model_validate(rerounded).shutdown_minutes == 12.345679


INTERRUPTION_TABLE: list[tuple[str, list[Transition], int]] = [
    (
        "approval request counts once",
        log(
            tr(None, S.DETECTED, E.CONTACT_DETECTED),
            tr(S.DETECTED, S.AWAITING_APPROVAL, E.APPROVAL_REQUESTED, action=CLOSURE_ACTION),
            tr(S.AWAITING_APPROVAL, S.AWAITING_APPROVAL, E.STATE_CHANGE),
        ),
        1,
    ),
    (
        "handoff counts once",
        log(
            tr(None, S.DETECTED, E.CONTACT_DETECTED),
            tr(S.DETECTED, S.HANDED_OFF, E.HANDED_TO_PERSON),
            tr(S.HANDED_OFF, S.WATCH_AFTER_HANDOFF, E.WATCH_STARTED),
        ),
        1,
    ),
    (
        "request_watch_officer_look counts once",
        log(
            tr(None, S.DETECTED, E.CONTACT_DETECTED),
            tr(S.DETECTED, S.INVESTIGATING, E.STATE_CHANGE),
            tr(
                S.INVESTIGATING,
                S.INVESTIGATING,
                E.PROBE_CALL,
                probe=ProbeId.REQUEST_WATCH_OFFICER_LOOK,
            ),
        ),
        1,
    ),
    (
        "backup page counts once",
        log(
            tr(None, S.DETECTED, E.CONTACT_DETECTED),
            tr(S.HOLDING, S.HOLDING, E.BACKUP_PAGED),
        ),
        1,
    ),
    (
        "platform SITREP updates after the handoff do not count",
        log(
            tr(S.WATCH_AFTER_HANDOFF, S.WATCH_AFTER_HANDOFF, E.SITREP_UPDATE),
            tr(S.WATCH_AFTER_HANDOFF, S.WATCH_AFTER_HANDOFF, E.SITREP_UPDATE, t=300.0),
            tr(S.WATCH_AFTER_HANDOFF, S.RESOLVED_DEPARTED, E.CONTACT_DEPARTED, t=600.0),
        ),
        0,
    ),
    (
        "platform probes, operator notices and ask-rung outcomes do not count",
        log(
            tr(None, S.DETECTED, E.CONTACT_DETECTED),
            tr(S.DETECTED, S.WATCHING, E.STATE_CHANGE),
            tr(S.WATCHING, S.ASKING, E.STATE_CHANGE),
            tr(S.ASKING, S.ASKING, E.PROBE_CALL, probe=ProbeId.ACOUSTIC_HAIL),
            tr(S.ASKING, S.ASKING, E.PROBE_CALL, probe=ProbeId.CAMERA_CUE),
            tr(S.ASKING, S.LOST_VEHICLE, E.OPERATOR_NOTICE),
        ),
        0,
    ),
    (
        "a hold under standing orders alone does not count",
        log(
            tr(None, S.DETECTED, E.CONTACT_DETECTED),
            tr(S.DETECTED, S.INVESTIGATING, E.STATE_CHANGE),
            tr(S.INVESTIGATING, S.ADVISING_SLOW, E.STATE_CHANGE),
            tr(S.ADVISING_SLOW, S.HOLDING, E.STATE_CHANGE),
            tr(S.HOLDING, S.RESOLVED_DEPARTED, E.CONTACT_DEPARTED),
        ),
        0,
    ),
    (
        "all four sources in one log count four",
        log(
            tr(None, S.DETECTED, E.CONTACT_DETECTED),
            tr(S.DETECTED, S.INVESTIGATING, E.STATE_CHANGE),
            tr(
                S.INVESTIGATING,
                S.INVESTIGATING,
                E.PROBE_CALL,
                probe=ProbeId.REQUEST_WATCH_OFFICER_LOOK,
            ),
            tr(S.INVESTIGATING, S.HOLDING, E.STATE_CHANGE),
            tr(S.HOLDING, S.AWAITING_APPROVAL, E.APPROVAL_REQUESTED, action="hold_longer"),
            tr(S.AWAITING_APPROVAL, S.AWAITING_APPROVAL, E.BACKUP_PAGED),
            tr(S.AWAITING_APPROVAL, S.HANDED_OFF, E.APPROVAL_GRANTED),
            tr(S.HANDED_OFF, S.WATCH_AFTER_HANDOFF, E.WATCH_STARTED),
            tr(S.WATCH_AFTER_HANDOFF, S.WATCH_AFTER_HANDOFF, E.SITREP_UPDATE),
        ),
        4,
    ),
    (
        "bang: approval, handoff and the berth reopening",
        log(
            tr(None, S.DETECTED, E.CONTACT_DETECTED),
            tr(S.DETECTED, S.AWAITING_APPROVAL, E.APPROVAL_REQUESTED, action=CLOSURE_ACTION),
            tr(S.AWAITING_APPROVAL, S.HANDED_OFF, E.APPROVAL_GRANTED, action=CLOSURE_ACTION),
            tr(S.HANDED_OFF, S.WATCH_AFTER_HANDOFF, E.WATCH_STARTED),
            tr(S.WATCH_AFTER_HANDOFF, S.WATCH_AFTER_HANDOFF, E.BERTH_REOPENED),
            tr(S.WATCH_AFTER_HANDOFF, S.RESOLVED_DEPARTED, E.CONTACT_DEPARTED),
        ),
        3,
    ),
    (
        "human_queue: handoff plus two person re-checks",
        log(
            tr(None, S.DETECTED, E.CONTACT_DETECTED),
            tr(S.DETECTED, S.HANDED_OFF, E.HANDED_TO_PERSON),
            tr(S.HANDED_OFF, S.WATCH_AFTER_HANDOFF, E.WATCH_STARTED),
            tr(S.WATCH_AFTER_HANDOFF, S.WATCH_AFTER_HANDOFF, E.PERSON_RECHECK),
            tr(S.WATCH_AFTER_HANDOFF, S.WATCH_AFTER_HANDOFF, E.PERSON_RECHECK),
        ),
        3,
    ),
]


@pytest.mark.parametrize(
    ("label", "steps", "expected"), INTERRUPTION_TABLE, ids=[r[0] for r in INTERRUPTION_TABLE]
)
def test_count_interruptions_table(label: str, steps: list[Transition], expected: int) -> None:
    assert count_interruptions(steps) == expected, label


def test_interruption_sources_each_count_one() -> None:
    sources = {
        InterruptionKind.APPROVAL_REQUEST: tr(S.DETECTED, S.AWAITING_APPROVAL),
        InterruptionKind.HANDOFF: tr(S.DETECTED, S.HANDED_OFF),
        InterruptionKind.EYES_ON: tr(
            S.ASKING, S.ASKING, E.PROBE_CALL, probe=ProbeId.REQUEST_WATCH_OFFICER_LOOK
        ),
        InterruptionKind.BACKUP_PAGE: tr(S.AWAITING_APPROVAL, S.AWAITING_APPROVAL, E.BACKUP_PAGED),
    }
    assert set(sources) == set(InterruptionKind)
    for kind, t in sources.items():
        assert interruption_kind(t) is kind
        assert count_interruptions([t]) == 1
    # Nothing else counts: every event that is not a source, on a non-entering step.
    quiet = {E.APPROVAL_REQUESTED, E.APPROVAL_GRANTED, E.HANDED_TO_PERSON}
    sourced = {E.BACKUP_PAGED, E.BERTH_REOPENED, E.PERSON_RECHECK}
    for ev in PolicyEvent:
        if ev in sourced:
            continue
        t = tr(S.WATCHING, S.WATCHING, ev, probe=ProbeId.REGISTRY_LOOKUP)
        assert interruption_kind(t) is None, ev
        if ev in quiet:
            # an event name alone never counts; entering the state does
            assert interruption_kind(tr(S.AWAITING_APPROVAL, S.AWAITING_APPROVAL, ev)) is None
            assert interruption_kind(tr(S.HANDED_OFF, S.HANDED_OFF, ev)) is None


def test_shutdown_interval_union() -> None:
    assert merge_intervals([(0.0, 600.0), (300.0, 900.0)]) == [(0.0, 900.0)]
    assert merge_intervals([(300.0, 900.0), (0.0, 600.0), (950.0, 1000.0)]) == [
        (0.0, 900.0),
        (950.0, 1000.0),
    ]
    assert merge_intervals([(0.0, 600.0), (100.0, 200.0), (600.0, 660.0)]) == [(0.0, 660.0)]
    assert merge_intervals([(5.0, 5.0)]) == []

    def sd(cid: str, asset: str, a: float, b: float) -> ShutdownInterval:
        return ShutdownInterval(contact_id=cid, asset_id=asset, kind="closure", start_s=a, end_s=b)

    one_berth = [sd("c-0001", "fuel_berth_1", 0.0, 600.0), sd("c-0002", "fuel_berth_1", 300, 900)]
    assert shutdown_minutes(one_berth) == 15.0  # 900 s, not 1200 s
    two_berths = [sd("c-0001", "fuel_berth_1", 0.0, 600.0), sd("c-0002", "fuel_berth_2", 0, 600)]
    assert shutdown_minutes(two_berths) == 20.0  # 10 + 10
    mixed = one_berth + [sd("c-0003", "fuel_berth_2", 120.0, 240.0)]
    assert shutdown_minutes(mixed) == 17.0

    # The bang planner merges two overlapping contacts on one berth into one closure.
    bang = BangPolicy()
    spans = [span("c-0001", 0.0, 300.0), span("c-0002", 60.0, 600.0)]
    transitions, shutdowns = bang.plan(spans, end_s=3600.0, tick_s=2.0)
    assert {s.asset_id for s in shutdowns} == {"fuel_berth_1"}
    # approval at 120; closure until max(300, 600) + 600 = 1200
    assert shutdown_minutes(shutdowns) == round6((1200.0 - 120.0) / 60.0)
    assert sum(s.minutes for s in shutdowns) > shutdown_minutes(shutdowns)
    requests = [t for t in transitions if t.to_state is S.AWAITING_APPROVAL]
    assert [t.contact_id for t in requests] == ["c-0001"]  # merged: no second request
    reopen = [t for t in transitions if t.event is E.BERTH_REOPENED]
    assert len(reopen) == 1 and reopen[0].t_s == 1200.0


def test_missed_threat_rules() -> None:
    uuv = Hypothesis.UNAUTHORIZED_UUV
    gts = [GtContact(gt_id="gt-1", truth=uuv, is_false_alarm=False)]

    # Lost under c-0001, re-acquired as c-0002, which reaches HANDED_OFF: not missed.
    spans = [
        span("c-0001", 0.0, 100.0, gt="gt-1", truth=uuv),
        span("c-0002", 140.0, 600.0, gt="gt-1", truth=uuv),
    ]
    steps = log(
        tr(None, S.DETECTED, E.CONTACT_DETECTED, cid="c-0001"),
        tr(S.DETECTED, S.WATCHING, cid="c-0001", t=10.0),
        tr(None, S.DETECTED, E.CONTACT_DETECTED, cid="c-0002", t=140.0),
        tr(S.DETECTED, S.INVESTIGATING, cid="c-0002", t=150.0),
        tr(S.INVESTIGATING, S.HANDED_OFF, cid="c-0002", t=200.0),
    )
    run = run_from(spans, gts, steps)
    o = outcome_for_gt(run, "gt-1")
    assert o.observed_contact_ids == ("c-0001", "c-0002")
    assert not o.missed and o.reason is None and not run.missed_threat
    assert o.max_rung is Rung.HAND_TO_PERSON
    assert o.terminal_state is S.HANDED_OFF
    assert o.seconds_seen_to_handled == 200.0

    # Ends RESOLVED_BENIGN without a hold or handoff: missed.
    spans = [span("c-0001", 0.0, 600.0, gt="gt-1", truth=uuv)]
    steps = log(
        tr(None, S.DETECTED, E.CONTACT_DETECTED),
        tr(S.DETECTED, S.INVESTIGATING, t=10.0),
        tr(S.INVESTIGATING, S.RESOLVED_BENIGN, t=400.0),
    )
    run = run_from(spans, gts, steps)
    o = outcome_for_gt(run, "gt-1")
    assert o.missed and o.reason == "never_held_or_handed_off" and run.missed_threat
    assert o.terminal_state is S.RESOLVED_BENIGN

    # A hold counts as caught even if the contact later departs.
    steps = log(
        tr(None, S.DETECTED, E.CONTACT_DETECTED),
        tr(S.DETECTED, S.HOLDING, t=10.0, asset="fuel_berth_1"),
        tr(S.HOLDING, S.RESOLVED_DEPARTED, t=400.0),
    )
    assert not outcome_for_gt(run_from(spans, gts, steps), "gt-1").missed

    # Never detected: missed with reason "undetected". A benign undetected one is not.
    gts2 = gts + [GtContact(gt_id="gt-2", truth=Hypothesis.BIOLOGIC, is_false_alarm=False)]
    run = run_from([], gts2, [])
    o = outcome_for_gt(run, "gt-1")
    assert o.missed and o.reason == "undetected" and not o.detected
    assert o.terminal_state is None and run.terminal_state is None
    assert not outcome_for_gt(run, "gt-2").missed
    assert run.missed_threat
    # Diver counts as a threat too.
    assert P.is_threat(Hypothesis.UNAUTHORIZED_DIVER) and not P.is_threat(Hypothesis.BIOLOGIC)


def test_false_shutdown_rules() -> None:
    bio, uuv = Hypothesis.BIOLOGIC, Hypothesis.UNAUTHORIZED_UUV
    gts = [
        GtContact(gt_id="gt-1", truth=uuv, is_false_alarm=False),
        GtContact(gt_id="gt-2", truth=bio, is_false_alarm=False),
        GtContact(gt_id="fa-0001", truth=bio, is_false_alarm=True),
    ]
    spans = [
        span("c-0001", 0.0, 600.0, gt="gt-1", truth=uuv),
        span("c-0002", 0.0, 600.0, gt="gt-2", truth=bio),
        span("c-0003", 0.0, 100.0, gt="fa-0001", truth=bio, fa=True),
    ]
    steps = log(
        *[
            s
            for cid, asset in (
                ("c-0001", "fuel_berth_2"),
                ("c-0002", "fuel_berth_1"),
                ("c-0003", "pier_400_gate"),
            )
            for s in (
                tr(None, S.DETECTED, E.CONTACT_DETECTED, cid=cid),
                tr(S.DETECTED, S.HOLDING, cid=cid, t=60.0, asset=asset),
                tr(S.HOLDING, S.RESOLVED_DEPARTED, cid=cid, t=360.0),
            )
        ]
    )
    holds = hold_shutdowns(steps, 1200.0, {})
    assert [(h.contact_id, h.asset_id, h.start_s, h.end_s) for h in holds] == [
        ("c-0001", "fuel_berth_2", 60.0, 360.0),
        ("c-0002", "fuel_berth_1", 60.0, 360.0),
        ("c-0003", "pier_400_gate", 60.0, 360.0),
    ]
    run = run_from(spans, gts, steps, tuple(holds))
    by_id = {c.observed_contact_id: c for c in run.contacts}
    assert not by_id["c-0001"].false_shutdown  # hold on UNAUTHORIZED_UUV
    assert by_id["c-0002"].false_shutdown  # hold on BIOLOGIC
    assert by_id["c-0003"].false_shutdown and by_id["c-0003"].is_false_alarm  # sim false alarm
    assert run.false_shutdown
    assert run.shutdown_minutes == 15.0  # three assets x 5 min
    assert outcome_for_gt(run, "gt-2").false_shutdown
    assert not outcome_for_gt(run, "gt-1").false_shutdown

    only_threat = run_from(spans[:1], gts[:1], steps[:3], tuple(holds[:1]))
    assert not only_threat.false_shutdown and only_threat.shutdown_minutes == 5.0


def test_human_queue_serial() -> None:
    assert serial_queue([0.0, 0.0, 0.0], 120.0) == [120.0, 240.0, 360.0]
    assert serial_queue([0.0, 500.0], 120.0) == [120.0, 620.0]
    spans = [span(f"c-000{i}", 0.0, 1000.0) for i in (1, 2, 3)]
    transitions = HumanQueuePolicy().plan(spans, end_s=1200.0, tick_s=2.0)
    handoffs = {t.contact_id: t.t_s for t in transitions if t.to_state is S.HANDED_OFF}
    assert handoffs == {"c-0001": 120.0, "c-0002": 240.0, "c-0003": 360.0}
    assert all(t.from_state is S.DETECTED for t in transitions if t.to_state is S.HANDED_OFF)
    rechecks = [t for t in transitions if t.event is E.PERSON_RECHECK]
    # every 300 s after the handoff while still detected (last seen 1000 s)
    assert sorted((t.contact_id, t.t_s) for t in rechecks) == [
        ("c-0001", 420.0),
        ("c-0001", 720.0),
        ("c-0002", 540.0),
        ("c-0002", 840.0),
        ("c-0003", 660.0),
        ("c-0003", 960.0),
    ]
    assert not any(t.probe_id for t in transitions)
    # A queue that runs past the end of the run leaves the late contacts unhandled.
    late = HumanQueuePolicy().plan(spans, end_s=300.0, tick_s=2.0)
    assert {t.contact_id for t in late if t.to_state is S.HANDED_OFF} == {"c-0001", "c-0002"}


def test_sim_likelihoods_kwarg() -> None:
    s05 = resolve_scenario("S05")
    unpinned = s05.model_copy(
        update={
            "contacts": tuple(
                c.model_copy(update={"acoustic": AcousticSpec()}) for c in s05.contacts
            )
        }
    )
    spec = load_likelihood_spec()
    feat = spec.features[FeatureName.PROPELLER_TONAL_PRESENT]
    assert feat.buckets == ("false", "true")
    rows = dict(feat.rows)
    rows[Hypothesis.UNAUTHORIZED_UUV] = (1.0, 0.0)
    features = dict(spec.features)
    features[FeatureName.PROPELLER_TONAL_PRESENT] = feat.model_copy(update={"rows": rows})
    never_tonal = spec.model_copy(update={"features": features})
    for seed in (1, 2, 3):
        run = run_scenario(unpinned, seed=seed, likelihoods=never_tonal)
        uuv = [d for d in run.detections if d.gt_id == "gt-1"]
        assert uuv, "S05's contact must be detected"
        assert not any(d.propeller_tonal_present for d in uuv)
    # The pinned S05 stays tonal: the kwarg only feeds unset fields.
    pinned = run_scenario(s05, seed=1, likelihoods=never_tonal)
    assert any(d.propeller_tonal_present for d in pinned.detections if d.gt_id == "gt-1")
    # The default path is unchanged.
    base = track_hash(run_scenario(s05, seed=1))
    assert track_hash(run_scenario(s05, seed=1, likelihoods=None)) == base
    assert track_hash(run_scenario(s05, seed=1, likelihoods=spec)) == base


def test_bang_closure_is_person_approved() -> None:
    raw = yaml.safe_load((REPO / "config" / "ladder.yaml").read_text(encoding="utf-8"))
    person_approves = set(raw["boundary"]["hand_to_person"]["person_approves"])
    assert CLOSURE_ACTION in person_approves
    runs = catalog_runs()
    n_closures = 0
    for sc in catalog():
        for seed in SEEDS:
            run = runs[(sc.id, seed, PolicyName.BANG)]
            by_seq = {t.seq: t for t in run.transition_log}
            for t in run.transition_log:
                if t.action is not None:
                    assert t.action in person_approves
            for sd in run.shutdowns:
                n_closures += 1
                assert sd.kind == "closure"
                assert sd.approval_seq is not None
                grant = by_seq[sd.approval_seq]
                assert grant.from_state is S.AWAITING_APPROVAL
                assert grant.to_state is S.HANDED_OFF
                assert grant.action in person_approves
                assert grant.asset_id == sd.asset_id
                assert grant.t_s <= sd.start_s
                request = [
                    t
                    for t in run.transition_log
                    if t.contact_id == grant.contact_id and t.to_state is S.AWAITING_APPROVAL
                ]
                assert len(request) == 1 and request[0].seq < grant.seq
                assert request[0].action == grant.action
            # Nothing ever enters HOLDING or HANDED_OFF on a berth without an approval.
            granted = {by_seq[s.approval_seq].asset_id for s in run.shutdowns if s.approval_seq}
            for t in run.transition_log:
                if t.to_state is S.HOLDING:
                    assert t.asset_id in granted
    assert n_closures > 0

    # A ladder config without a person-approved closure leaves bang nothing to ask for.
    ladder = load_ladder_config()
    h2p = ladder.boundary[Rung.HAND_TO_PERSON]
    stripped = h2p.model_copy(
        update={"person_approves": tuple(a for a in h2p.person_approves if a != CLOSURE_ACTION)}
    )
    boundary = dict(ladder.boundary)
    boundary[Rung.HAND_TO_PERSON] = stripped
    with pytest.raises(ValueError, match="person-approved"):
        BangPolicy(ladder=ladder.model_copy(update={"boundary": boundary}))


def test_forged_transition_rejected() -> None:
    builder = P._LogBuilder(end_s=1200.0, tick_s=2.0)
    builder.add("c-0001", 0.0, S.DETECTED, E.CONTACT_DETECTED)
    builder.add("c-0001", 10.0, S.HANDED_OFF, E.HANDED_TO_PERSON)
    with pytest.raises(ForgedTransitionError):
        builder.add("c-0001", 20.0, S.HOLDING, E.STATE_CHANGE)
    with pytest.raises(ForgedTransitionError):
        builder.add("c-0002", 0.0, S.HOLDING, E.STATE_CHANGE)
    # Nothing after the handoff but watching and departure.
    after = {to for frm, to in BASELINE_EDGES if frm in (S.HANDED_OFF, S.WATCH_AFTER_HANDOFF)}
    assert after == {S.WATCH_AFTER_HANDOFF, S.RESOLVED_DEPARTED}


def test_max_rung_uses_ladder_table() -> None:
    steps = log(
        tr(None, S.DETECTED),
        tr(S.DETECTED, S.AWAITING_APPROVAL),
    )
    assert max_rung(steps) is Rung.WATCH  # AWAITING_APPROVAL keeps the incoming rung
    steps.append(tr(S.AWAITING_APPROVAL, S.HANDED_OFF))
    assert max_rung(steps) is Rung.HAND_TO_PERSON
    assert max_rung([]) is None
    assert max_rung(log(tr(None, S.DETECTED), tr(S.DETECTED, S.HOLDING))) is Rung.HOLD


def test_baselines_import_no_llm() -> None:
    tree = ast.parse(POLICIES_PY.read_text(encoding="utf-8"))
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            imported.append(node.module or "")
    assert imported
    for mod in imported:
        assert not mod.startswith("soundings.llm"), mod
        assert not mod.startswith("soundings.runner"), mod
        assert mod.split(".")[0] not in {"time", "random", "anthropic"}, mod
        assert mod != "datetime", mod
    # The only dynamic import is the lazy soundings adapter (WO-051).
    assert P.SOUNDINGS_POLICY_MODULE == "soundings.eval.soundings_policy"


def test_policy_vocabulary_clean() -> None:
    names = [p.value for p in POLICIES]
    names += [e.value for e in PolicyEvent] + [k.value for k in InterruptionKind]
    names += [CLOSURE_ACTION]
    for text in names:
        assert find_denylisted(text) == [], text
    eval_yaml = (REPO / "config" / "eval.yaml").read_text(encoding="utf-8")
    assert find_denylisted(eval_yaml) == []
    runs = catalog_runs()
    assert len(runs) == len(catalog()) * len(SEEDS) * len(BASELINES)
    for key in sorted(runs, key=str):
        assert find_denylisted(canonical_json(runs[key])) == [], key
    proc = subprocess.run(
        ["grep", "-riEf", "config/denylist.txt", "soundings/eval", "config/eval.yaml"],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 1 and proc.stdout == "", proc.stdout
