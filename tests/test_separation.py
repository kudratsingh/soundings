"""WO-083: separation_check thresholds, encounter memory, and the in-water job filter."""

from __future__ import annotations

from pathlib import Path

import pytest

from soundings.advisories.separation import (
    AdvisoryConfig,
    SeparationAdvisor,
    in_water_jobs,
    load_advisory_config,
    separation_check,
)
from soundings.ladder.audit import AuditKind, AuditLog
from soundings.schemas.harbor import load_harbor
from tests.support.separation_fixtures import (
    cleared_case,
    guards_for,
    job,
    plan,
    run_result,
    signed_status,
    track,
    vessel,
)
from tests.test_cpa import KNOWN

CFG = load_advisory_config()


def test_config_shape(tmp_path: Path) -> None:
    assert isinstance(CFG, AdvisoryConfig)
    assert CFG.separation_m > 0 and CFG.advisory_window_s > 0
    bad = tmp_path / "advisories.yaml"
    bad.write_text("separation_m: 150\nadvisory_window_s: 600\nextra: 1\n", encoding="utf-8")
    with pytest.raises(ValueError):
        load_advisory_config(bad)


def _fires(
    pos: tuple[float, float],
    vel: tuple[float, float],
    other: tuple[float, float],
    other_vel: tuple[float, float],
) -> bool:
    adv = separation_check([vessel("ais-01", 0, pos, vel)], [job("P-1", 0, other, other_vel)], CFG)
    return len(adv) == 1


def _geometry(cpa: float, tcpa: float, speed: float = 5.0) -> bool:
    """Job still at the origin; the vessel runs east at ``speed`` to pass ``cpa`` north of
    it at ``tcpa`` seconds: CPA = cpa, TCPA = tcpa by construction."""
    return _fires((-speed * tcpa, cpa), (speed, 0.0), (0.0, 0.0), (0.0, 0.0))


def test_thresholds() -> None:
    fired = {name: _fires(op, ov, xp, xv) for name, op, ov, xp, xv, _, _ in KNOWN}
    assert fired == {
        "head_on": True,
        "crossing": True,
        "wide_crossing": False,  # CPA 192 m >= separation_m
        "diverging": False,  # TCPA < 0
        "parallel": False,  # TCPA == 0 (no relative motion)
        "far": False,  # TCPA 3500 s >= advisory_window_s
    }
    sep, win = CFG.separation_m, CFG.advisory_window_s
    mid_t, mid_c = win / 2.0, sep / 2.0
    assert _geometry(sep - 0.5, mid_t)
    assert not _geometry(sep + 0.5, mid_t)
    assert _geometry(mid_c, win - 1.0)
    assert not _geometry(mid_c, win + 1.0)
    assert not _geometry(mid_c, 0.0)
    assert _geometry(mid_c, 1.0)
    # every advisory carries its facts and thresholds
    (adv,) = separation_check(
        [vessel("ais-01", 3, (0.0, 0.0), (5.0, 0.0))],
        [job("P-1", 3, (1000.0, 50.0), (-5.0, 0.0))],
        CFG,
    )
    assert adv.inputs.separation_m == sep and adv.inputs.advisory_window_s == win
    assert adv.cpa_m == pytest.approx(50.0, abs=1e-3) and adv.tcpa_s == pytest.approx(
        100.0, abs=1e-3
    )
    assert (adv.basin, adv.vessel_id, adv.plan_id, adv.tick) == (
        "inner_harbor_west",
        "ais-01",
        "P-1",
        3,
    )


def test_check_order() -> None:
    vs = [vessel(v, 0, (-500.0, 20.0), (5.0, 0.0)) for v in ("ais-02", "ais-01")]
    js = [job(p, 0, (0.0, 0.0)) for p in ("P-2", "P-1")]
    assert [a.key for a in separation_check(vs, js, CFG)] == [
        ("ais-01", "P-1"),
        ("ais-01", "P-2"),
        ("ais-02", "P-1"),
        ("ais-02", "P-2"),
    ]


def test_encounter_once(tmp_path: Path) -> None:
    audit = AuditLog(":memory:")
    advisor = SeparationAdvisor(guards_for(signed_status(tmp_path)), audit, "run-test", CFG)

    def at(tick: int, x: float) -> list:  # vessel heads east 50 m north of the job
        v = vessel("ais-01", tick, (x, 50.0), (5.0, 0.0))
        return advisor.step(tick, 1.0, [v], [job("P-1", tick, (0.0, 0.0))])

    decided = [d for t in range(20) for d in at(t, -500.0 + 5.0 * t)]
    assert len(decided) == 1 and decided[0].advisory.tick == 0
    assert len(audit.rows(kind=AuditKind.GUARD_DECISION)) == 1
    # the pair opens (passed the job, TCPA < 0), then a new approach closes it again
    assert at(20, 100.0) == []
    again = at(21, -400.0)
    assert len(again) == 1 and again[0].advisory.tick == 21
    assert len(audit.rows(kind=AuditKind.GUARD_DECISION)) == 2
    assert audit.verify().ok


def test_in_water_jobs() -> None:
    tick_s, now, timeout = 5.0, 100, 15
    harbor = load_harbor()
    plans = [
        plan("P-000", 0, 200),
        plan("P-001", 0, 200, area="fuel_berths"),
        plan("P-002", 0, 200),  # cleared later than `now`
        plan("P-003", 0, 50),  # window already over at `now`
        plan("P-004", 0, 200),  # stale track
    ]
    tracks = {
        "c-0001": track("c-0001", range(40, 100), -300.0, 100.0, 0.5, tick_s),
        "c-0002": track("c-0002", range(40, 101), 200.0, -100.0, 0.0, tick_s),
        "c-0003": track("c-0003", range(40, 101), 0.0, 0.0, 0.2, tick_s),
        "c-0004": track("c-0004", range(40, 101), 100.0, 300.0, 0.0, tick_s),
        "c-0005": track("c-0005", range(10, 60), 400.0, 0.0, 0.0, tick_s),
    }
    cases = [
        cleared_case("k-1", "c-0001", "P-001", 45),
        cleared_case("k-2", "c-0002", "P-000", 41),
        cleared_case("k-3", "c-0003", "P-002", 150),
        cleared_case("k-4", "c-0004", "P-003", 41),
        cleared_case("k-5", "c-0005", "P-004", 20),
    ]
    result = run_result(tracks, cases, tick_s, 300)
    jobs = in_water_jobs(result, plans, harbor, now, timeout)
    assert [j.plan_id for j in jobs] == ["P-000", "P-001"]
    by = {j.plan_id: j for j in jobs}
    assert by["P-001"].basin == "inner_harbor_west" and by["P-001"].contact_id == "c-0001"
    assert by["P-001"].velocity_source == "window" and by["P-001"].tick == now
    # dead-reckoned from the last reading (tick 99) to tick 100 along +x at 0.5 m/s
    assert by["P-001"].vx == pytest.approx(0.5, abs=0.05)
    assert by["P-001"].x_m == pytest.approx(-300.0 + 0.5 * now * tick_s, abs=2.0)
    # the stale track is back in once its reading is within the timeout
    assert [j.plan_id for j in in_water_jobs(result, plans, harbor, 59, timeout)] == [
        "P-000",
        "P-001",
        "P-004",
    ]
