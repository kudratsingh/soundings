"""WO-080: the Busy Tuesday generator and the committed ``scenarios/BT-01.yaml``.

Exit test: ``uv run pytest tests/test_generate.py``.
"""

from __future__ import annotations

import math
from pathlib import Path

import pytest

from soundings.schemas.enums import BehaviorType, Hypothesis, JobType, LadderState
from soundings.schemas.geo import to_local_m
from soundings.schemas.harbor import load_harbor
from soundings.schemas.scenario import check_scenario_refs, load_scenario
from soundings.sim.generate import (
    HEARD_FRACTION,
    JOB_SPECS,
    JOB_TYPES,
    ROGUE_CLEARANCE_M,
    S05_PATH,
    WINDOW_PAD_S,
    generate_busy_day,
    job_clearance_m,
    main,
    peak_tick,
    rogue_segment,
    scenario_yaml,
)
from soundings.sim.scenario import REPO_ROOT, resolve_scenario, scenario_path

HARBOR = load_harbor()
BT01 = REPO_ROOT / "scenarios" / "BT-01.yaml"
CMD = "python -m soundings.sim.generate --jobs 40 --vessels 3 --seed 7 --out scenarios/BT-01.yaml"


@pytest.fixture(scope="module")
def bt() -> object:
    return generate_busy_day(seed=7)


def test_same_seed_same_bytes_and_seed_8_differs() -> None:
    a = scenario_yaml(generate_busy_day(seed=7), CMD)
    b = scenario_yaml(generate_busy_day(seed=7), CMD)
    c = scenario_yaml(generate_busy_day(seed=8), CMD)
    assert a == b
    assert a != c


def test_committed_file_is_the_generation() -> None:
    assert BT01.read_text(encoding="utf-8") == scenario_yaml(generate_busy_day(seed=7), CMD)


def test_check_flag_passes_on_committed_file(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(REPO_ROOT)
    assert (
        main(
            [
                "--jobs",
                "40",
                "--vessels",
                "3",
                "--seed",
                "7",
                "--out",
                str(Path("scenarios/BT-01.yaml")),
                "--check",
            ]
        )
        == 0
    )


def test_check_flag_fails_on_stale_file(tmp_path: Path) -> None:
    out = tmp_path / "BT-01.yaml"
    out.write_text("stale\n", encoding="utf-8")
    assert main(["--seed", "7", "--out", str(out), "--check"]) == 1


def test_bt01_validates_and_resolves() -> None:
    sc = resolve_scenario("BT-01")
    assert scenario_path("BT-01") == BT01
    check_scenario_refs(sc, HARBOR)  # every zone/asset id resolves
    assert sc == load_scenario(BT01)
    plan_ids = {p.plan_id for p in sc.registry_plans}
    for c in sc.contacts:
        if c.registry.plan_id is not None:
            assert c.registry.plan_id in plan_ids


def test_counts(bt: object) -> None:
    sc = generate_busy_day(seed=7)
    jobs = [c for c in sc.contacts if c.id.startswith("job-")]
    vessels = [c for c in sc.contacts if c.behavior.type is BehaviorType.VESSEL_TRANSIT]
    rogues = [c for c in sc.contacts if c.id.startswith("rogue-")]
    assert len(sc.registry_plans) == 40 and len(jobs) == 39
    assert len(vessels) == 3 and len(rogues) == 1
    assert all(p.job_type in JOB_TYPES for p in sc.registry_plans)
    assert JobType.OTHER not in {p.job_type for p in sc.registry_plans}
    filed = {c.registry.plan_id for c in jobs}
    no_show = [p.plan_id for p in sc.registry_plans if p.plan_id not in filed]
    assert no_show == ["P-BT01-040"]
    assert all(not v.registry.filed and not v.registry.replies_to_hail for v in vessels)
    assert all(v.registry.plan_id is None for v in vessels)
    assert len(sc.expected.contacts) == 40
    assert sc.expected.terminal_state is LadderState.HANDED_OFF


def test_windows_inside_type_ranges() -> None:
    sc = generate_busy_day(seed=7)
    plans = {p.plan_id: p for p in sc.registry_plans}
    pad = WINDOW_PAD_S / sc.tick_s
    for c in sc.contacts:
        if not c.id.startswith("job-"):
            continue
        assert c.registry.plan_id is not None and c.end_tick is not None
        plan = plans[c.registry.plan_id]
        assert plan.job_type is not None
        lo, hi = JOB_SPECS[plan.job_type].hours
        dur_h = (c.end_tick - c.start_tick) * sc.tick_s / 3600.0
        assert lo - 1 / 60 <= dur_h <= hi, (c.id, dur_h)
        assert (c.start_tick * sc.tick_s) % 60 == 0
        assert plan.window.start_tick == max(c.start_tick - pad, 0)
        assert plan.window.end_tick == min(c.end_tick + pad, sc.duration_ticks)
        assert c.end_tick <= sc.duration_ticks


def test_jobs_in_zone_and_heard() -> None:
    sc = generate_busy_day(seed=7)
    plans = {p.plan_id: p for p in sc.registry_plans}
    for c in sc.contacts:
        if not c.id.startswith("job-"):
            continue
        assert c.registry.plan_id is not None
        plan = plans[c.registry.plan_id]
        assert plan.job_type is not None
        assert plan.area in JOB_SPECS[plan.job_type].zones
        assert HARBOR.zone(plan.area).contains(c.start.lat, c.start.lon), c.id
        x, y = to_local_m(c.start.lat, c.start.lon)
        d = min(
            math.hypot(x - to_local_m(n.lat, n.lon)[0], y - to_local_m(n.lat, n.lon)[1]) / n.range_m
            for n in HARBOR.sensor_nodes
        )
        assert d <= HEARD_FRACTION, c.id


def test_jobs_clear_of_rogue_path() -> None:
    sc = generate_busy_day(seed=7)
    rogue = next(c for c in sc.contacts if c.id == "rogue-1")
    seg = rogue_segment(rogue, HARBOR)
    plans = {p.plan_id: p for p in sc.registry_plans}
    for c in sc.contacts:
        if c.id.startswith("job-"):
            assert c.registry.plan_id is not None
            jt = plans[c.registry.plan_id].job_type
            assert jt is not None
            assert job_clearance_m(c, JOB_SPECS[jt], HARBOR, seg) >= ROGUE_CLEARANCE_M, c.id


def test_peak_concurrency_and_rogue_at_peak() -> None:
    sc = generate_busy_day(seed=7)
    iv = [
        (c.start_tick, c.end_tick or sc.duration_ticks)
        for c in sc.contacts
        if c.id.startswith("job-")
    ]
    t, n = peak_tick(iv, sc.duration_ticks)
    assert n >= 30
    rogue = next(c for c in sc.contacts if c.id == "rogue-1")
    assert rogue.start_tick == t


def test_rogue_equals_s05_gt1() -> None:
    sc = generate_busy_day(seed=7)
    s05 = load_scenario(S05_PATH).contacts[0]
    rogue = next(c for c in sc.contacts if c.id == "rogue-1")
    assert rogue.start == s05.start
    assert rogue.behavior == s05.behavior
    assert rogue.acoustic == s05.acoustic
    assert rogue.registry == s05.registry
    assert rogue.hypothesis is Hypothesis.UNAUTHORIZED_UUV


def test_vessels_staggered_one_through_inner_harbor_west() -> None:
    sc = generate_busy_day(seed=7)
    vessels = sorted(
        (c for c in sc.contacts if c.behavior.type is BehaviorType.VESSEL_TRANSIT),
        key=lambda c: c.id,
    )
    starts = [v.start_tick for v in vessels]
    assert starts == sorted(starts) and len(set(starts)) == 3
    assert all(3.0 <= (v.behavior.speed_mps or 0) <= 6.0 for v in vessels)
    ihw = HARBOR.zone("inner_harbor_west")
    wp = vessels[0].behavior.waypoint
    assert wp is not None and ihw.contains(wp.lat, wp.lon)
    for v in vessels:
        assert HARBOR.zone("main_channel").contains(v.start.lat, v.start.lon)


def test_no_shows_and_options() -> None:
    sc = generate_busy_day(seed=3, jobs=8, vessels=1, no_shows=2, rogue=False, hours=4)
    assert len(sc.registry_plans) == 8
    assert len([c for c in sc.contacts if c.id.startswith("job-")]) == 6
    assert not any(c.id.startswith("rogue") for c in sc.contacts)
    with pytest.raises(ValueError):
        generate_busy_day(seed=1, jobs=2, no_shows=3)
