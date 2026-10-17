"""WO-050 integration tests: both baselines end to end on the real scenario catalog."""

from __future__ import annotations

from soundings.canonical import canonical_json
from soundings.eval.policies import (
    BangPolicy,
    HumanQueuePolicy,
    PolicyName,
    make_policy,
    outcome_for_gt,
)
from soundings.schemas import LadderState, Rung, distance_m, load_harbor
from soundings.sim.hashing import track_hash
from soundings.sim.run import run_scenario
from soundings.sim.scenario import resolve_scenario
from tests.support.policy_runs import BASELINES, SEEDS, catalog, catalog_runs

S = LadderState


def test_s05_baselines() -> None:
    s05 = resolve_scenario("S05")
    bang = BangPolicy().run(s05, 1)
    hq = HumanQueuePolicy().run(s05, 1)

    sim = run_scenario(s05, seed=1)
    gt = outcome_for_gt(bang, "gt-1")
    assert gt.detected and gt.truth is not None and gt.truth.value == "UNAUTHORIZED_UUV"
    first_id = gt.observed_contact_ids[0]
    p0 = sim.tracks[first_id].points[0]
    berths = [a for a in load_harbor().assets if a.asset_kind == "berth"]
    nearest = min(berths, key=lambda a: (distance_m(p0.lat, p0.lon, a.lat, a.lon), a.asset_id))

    by_seq = {t.seq: t for t in bang.transition_log}
    covering = [s for s in bang.shutdowns if s.contact_id == first_id]
    assert covering, "bang closes a berth for the S05 contact"
    for sd in covering:
        assert sd.asset_id == nearest.asset_id
        assert sd.approval_seq is not None
        grant = by_seq[sd.approval_seq]
        assert grant.from_state is S.AWAITING_APPROVAL and grant.to_state is S.HANDED_OFF
        assert grant.asset_id == nearest.asset_id
    assert bang.shutdown_minutes > 0
    assert not bang.missed_threat and not gt.missed
    assert gt.max_rung in (Rung.HOLD, Rung.HAND_TO_PERSON)

    hq_gt = outcome_for_gt(hq, "gt-1")
    assert hq.shutdown_minutes == 0.0 and hq.shutdowns == ()
    assert not hq.missed_threat and not hq_gt.missed
    assert hq_gt.max_rung is Rung.HAND_TO_PERSON
    assert hq.probes_used == 0 and bang.probes_used == 0
    assert hq.planner == "none" and bang.variant == "nominal"


def test_catalog_baseline_facts() -> None:
    runs = catalog_runs()
    for sc in catalog():
        for seed in SEEDS:
            bang = runs[(sc.id, seed, PolicyName.BANG)]
            hq = runs[(sc.id, seed, PolicyName.HUMAN_QUEUE)]
            key = f"{sc.id} seed {seed}"
            assert not bang.missed_threat, key
            detected_gts = [
                g.gt_id
                for g in bang.gt_contacts
                if not g.is_false_alarm and outcome_for_gt(bang, g.gt_id).detected
            ]
            if detected_gts:
                assert bang.shutdown_minutes > 0, key
            # human_queue: every detected ground-truth contact reaches a person at least once.
            for gid in detected_gts:
                assert outcome_for_gt(hq, gid).interruptions >= 1, (key, gid)
            assert hq.human_interruptions >= len(detected_gts), key
            assert hq.shutdown_minutes == 0.0 and hq.slow_minutes == 0.0, key
            assert bang.slow_minutes == 0.0, key
            assert sum(bang.rung_histogram.values()) == len(detected_gts), key
    # Over the catalog the person queue is interrupted more than bang, and bang shuts more.
    total = {
        p: sum(runs[(sc.id, s, p)].human_interruptions for sc in catalog() for s in SEEDS)
        for p in BASELINES
    }
    assert total[PolicyName.HUMAN_QUEUE] > total[PolicyName.BANG]


def test_identical_world() -> None:
    runs = catalog_runs()
    for sc in catalog():
        for seed in SEEDS:
            plain = track_hash(run_scenario(sc, seed=seed))
            for p in BASELINES:
                run = runs[(sc.id, seed, p)]
                assert run.track_hash == plain, (sc.id, seed, p)
                assert run.scenario_id == sc.id and run.seed == seed


def test_policy_runs_byte_identical() -> None:
    for sid in ("S02", "S05", "S07"):
        sc = resolve_scenario(sid)
        for seed in (1, 2):
            for p in BASELINES:
                a = canonical_json(make_policy(p).run(sc, seed))
                b = canonical_json(make_policy(p).run(sc, seed))
                assert a == b, (sid, seed, p)


def test_nothing_after_handoff() -> None:
    runs = catalog_runs()
    after_handoff = {S.WATCH_AFTER_HANDOFF}
    after_watch = {S.WATCH_AFTER_HANDOFF, S.RESOLVED_DEPARTED}
    for key, run in sorted(runs.items(), key=lambda kv: str(kv[0])):
        last: dict[str, S] = {}
        for t in run.transition_log:
            assert t.from_state == last.get(t.contact_id), key
            if t.from_state is S.HANDED_OFF:
                assert t.to_state in after_handoff, (key, t)
            if t.from_state is S.WATCH_AFTER_HANDOFF:
                assert t.to_state in after_watch, (key, t)
            last[t.contact_id] = t.to_state
        seqs = [t.seq for t in run.transition_log]
        assert seqs == list(range(len(seqs))), key
        times = [t.t_s for t in run.transition_log]
        assert times == sorted(times), key
        assert all(t < run.sim_hours * 3600.0 for t in times), key
