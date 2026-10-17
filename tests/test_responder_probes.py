"""WO-088 part B: the responder probes (``responder_look``, ``responder_search_box``).

The responder simulation is agent A's; these tests drive the probes through the
``ResponderPort`` Protocol with the deterministic double in ``tests/responder_double.py``.
"""

from __future__ import annotations

from functools import lru_cache

import pytest

from soundings.investigation import probes as P
from soundings.investigation.evidence import EvidenceTable
from soundings.investigation.features import build_evidence
from soundings.investigation.planner import (
    BudgetLedger,
    DeterministicPlanner,
    state_from_table,
)
from soundings.investigation.pol import load_pol_table
from soundings.investigation.posterior import load_likelihood_tables, load_prior_config
from soundings.investigation.probes import ProbeCache, ProbeContext, run_probe
from soundings.matcher import PositionFix, SearchBox
from soundings.registry.store import plan_views_from_scenario
from soundings.schemas.enums import FeatureName, ProbeId, Rung, ToggleName
from soundings.schemas.evidence import UNKNOWN
from soundings.schemas.geo import distance_m, from_local_m, to_local_m
from soundings.schemas.harbor import Harbor, load_harbor
from soundings.schemas.scenario import Scenario, Toggle
from soundings.sim.current import CurrentField, load_current_field
from soundings.sim.run import run_scenario
from soundings.sim.scenario import resolve_scenario
from soundings.sim.types import SimRun
from tests.responder_double import STANDOFF_M, FakeResponder

LOOK = ProbeId.RESPONDER_LOOK
SEARCH = ProbeId.RESPONDER_SEARCH_BOX
WATCH = tuple(p for p, s in P.ALLOWLIST.items() if s.rung_required is Rung.WATCH)


@lru_cache(maxsize=1)
def _harbor() -> Harbor:
    return load_harbor()


@lru_cache(maxsize=1)
def _field() -> CurrentField:
    return load_current_field()


@lru_cache(maxsize=8)
def _world(scenario_id: str, seed: int, ticks: int, fog: bool = False) -> tuple[Scenario, SimRun]:
    scenario = resolve_scenario(scenario_id, _harbor())
    if fog:
        fog_toggle = Toggle.model_validate("fog_no_camera")
        scenario = scenario.model_copy(update={"toggles": (*scenario.toggles, fog_toggle)})
    return scenario, run_scenario(scenario, seed=seed, ticks=ticks, harbor=_harbor())


def _main_contact(run: SimRun) -> str:
    ids = [c for c, t in run.contact_truth.items() if t.gt_id == "gt-1"]
    return sorted(ids, key=lambda c: (-len(run.tracks[c].points), c))[0]


def _table(scenario: Scenario, run: SimRun) -> EvidenceTable:
    field = _field()
    return build_evidence(
        run,
        _harbor(),
        field,
        load_pol_table(),
        hour_of_day=scenario.hour_of_day,
        tide_phase_for_hour=field.phase_for_hour,
    )


def _dock_near(lat: float, lon: float) -> tuple[float, float]:
    """A pier point inside the contact's declared zone, ~300 m from it (or the contact)."""
    harbor = _harbor()
    x, y = to_local_m(lat, lon)
    for dx, dy in ((300, 0), (-300, 0), (0, 300), (0, -300), (200, 200), (-200, -200)):
        cand = from_local_m(x + dx, y + dy)
        if harbor.zone_at(*cand) is not None:
            return cand
    return lat, lon


def _ctx(
    scenario: Scenario,
    run: SimRun,
    *,
    responder: FakeResponder | None,
    tick: int | None = None,
    search_box: SearchBox | None = None,
    rung: Rung = Rung.ASK,
) -> ProbeContext:
    cid = _main_contact(run)
    last = run.tracks[cid].points[-1].tick
    return ProbeContext(
        run=run,
        scenario=scenario,
        harbor=_harbor(),
        current_field=_field(),
        pol=load_pol_table(),
        registry=plan_views_from_scenario(scenario),
        tick=last if tick is None else tick,
        current_rung=rung,
        contact_id=cid,
        responder=responder,
        search_box=search_box,
    )


def _fake_for(run: SimRun, tick: int) -> FakeResponder:
    cid = _main_contact(run)
    pts = [p for p in run.tracks[cid].points if p.tick <= tick]
    dock = _dock_near(pts[-1].lat, pts[-1].lon)
    return FakeResponder(_harbor(), dock, tick_s=float(run.tick_s), tick=tick)


def _mid_tick(run: SimRun) -> int:
    cid = _main_contact(run)
    pts = run.tracks[cid].points
    return pts[len(pts) // 2].tick


# --- allowlist and the no-responder case ----------------------------------------------------


def test_responder_probes_allowlisted_at_ask() -> None:
    for pid in (LOOK, SEARCH):
        spec = P.ALLOWLIST[pid]
        assert spec.rung_required is Rung.ASK and spec.cost_usd == 0.0 and spec.interruption == 0
        assert pid in P.RESPONDER_PROBES and pid not in P.SECTION_5_ALLOWLIST
    assert P.ALLOWLIST[LOOK].returns[0] is FeatureName.VISUAL_CLASS


@pytest.mark.parametrize("pid", [LOOK, SEARCH])
def test_rung_watch_never_dispatches(pid: ProbeId) -> None:
    scenario, run = _world("S05", 11, 240)
    tick = _mid_tick(run)
    fake = _fake_for(run, tick)
    with pytest.raises(P.ProbeNotAllowed):
        run_probe(
            pid,
            _ctx(scenario, run, responder=fake, tick=tick, rung=Rung.WATCH),
            _table(scenario, run),
            cache=ProbeCache(),
        )
    assert fake.dispatches == [] and len(fake.trail) == 1


@pytest.mark.parametrize("pid", [LOOK, SEARCH])
def test_no_responder_gives_unknown_rows(pid: ProbeId) -> None:
    scenario, run = _world("S05", 11, 240)
    res = run_probe(
        pid, _ctx(scenario, run, responder=None), _table(scenario, run), cache=ProbeCache()
    )
    assert res.rows and all(r.value == UNKNOWN for r in res.rows)
    assert {r.feature for r in res.rows} == set(P.ALLOWLIST[pid].returns)


# --- responder_look ------------------------------------------------------------------------


def test_look_returns_nothing_before_on_station() -> None:
    scenario, run = _world("S05", 11, 240)
    tick = _mid_tick(run)
    fake = _fake_for(run, tick)
    table = _table(scenario, run)
    n = len(table)
    res = run_probe(
        LOOK, _ctx(scenario, run, responder=fake, tick=tick), table,
        args={"eta_ticks": 1}, cache=ProbeCache(),
    )  # fmt: skip
    assert fake.snapshot.state != "ON_STATION"
    assert res.rows == () and len(table) == n
    assert res.landed_tick == tick + 1


def test_look_on_station_returns_visual_and_close_acoustics() -> None:
    scenario, run = _world("S05", 11, 240)
    tick = _mid_tick(run)
    fake = _fake_for(run, tick)
    table = _table(scenario, run)
    ctx = _ctx(scenario, run, responder=fake, tick=tick)
    args = P.resolve_args(LOOK, ctx, table)
    eta = args["eta_ticks"]
    assert isinstance(eta, int) and eta > 0
    res = run_probe(LOOK, ctx, table, cache=ProbeCache())
    assert res.args == args and res.landed_tick == tick + eta
    assert any(s.state == "ON_STATION" for s in fake.trail)
    assert fake.snapshot.state == "RETURNING"  # the look is done: return to dock
    by_feature = {r.feature: r for r in res.rows}
    assert set(by_feature) == set(P.ALLOWLIST[LOOK].returns)
    visual = by_feature[FeatureName.VISUAL_CLASS]
    assert visual.value == "uuv"  # S05 is an unauthorized UUV
    vis = P.camera_visibility(ctx, res.landed_tick)
    factor = P.range_factor(fake.snapshot.distance_to_target_m)
    assert visual.confidence == pytest.approx(P.LOOK_BASE_CONFIDENCE * vis * factor, abs=1e-6)
    assert by_feature[FeatureName.PROPELLER_TONAL_PRESENT].value is True
    for row in res.rows:
        assert row.t == res.landed_tick and row.probe_id is LOOK
        assert row.source == "probe:responder_look"
    # the trail never went inside the standoff
    for snap in fake.trail:
        if snap.distance_to_target_m is not None:
            assert snap.distance_to_target_m >= STANDOFF_M - 1e-6


def test_look_under_fog_has_no_visual_but_hears() -> None:
    scenario, run = _world("S05", 11, 240, fog=True)
    assert any(t.name is ToggleName.FOG_NO_CAMERA for t in scenario.toggles)
    tick = _mid_tick(run)
    fake = _fake_for(run, tick)
    res = run_probe(
        LOOK, _ctx(scenario, run, responder=fake, tick=tick), _table(scenario, run),
        cache=ProbeCache(),
    )  # fmt: skip
    rows = {r.feature: r for r in res.rows}
    assert rows[FeatureName.VISUAL_CLASS].value == UNKNOWN
    assert rows[FeatureName.PROPELLER_TONAL_PRESENT].value is True
    assert rows[FeatureName.PROPELLER_TONAL_PRESENT].confidence > 0.5


def test_declined_dispatch_is_unknown_and_second_call_is_cached() -> None:
    scenario, run = _world("S05", 11, 240)
    tick = _mid_tick(run)
    fake = _fake_for(run, tick)
    table = _table(scenario, run)
    cache = ProbeCache()
    ctx = _ctx(scenario, run, responder=fake, tick=tick)
    first = run_probe(LOOK, ctx, table, cache=cache)
    again = run_probe(LOOK, ctx, table, cache=cache)
    assert again.cached and again.rows == first.rows and len(fake.dispatches) == 1
    # a fresh bucket: the responder is busy, so the look is declined -> unknown rows
    later = _ctx(scenario, run, responder=fake, tick=tick + 30)
    third = run_probe(LOOK, later, table, cache=cache)
    assert all(r.value == UNKNOWN for r in third.rows) and len(fake.dispatches) == 1


def test_look_is_deterministic() -> None:
    scenario, run = _world("S06", 13, 300)
    tick = _mid_tick(run)
    out = []
    for _ in range(2):
        fake = _fake_for(run, tick)
        res = run_probe(
            LOOK, _ctx(scenario, run, responder=fake, tick=tick), _table(scenario, run),
            cache=ProbeCache(),
        )  # fmt: skip
        out.append((res.key, [r.row_id for r in res.rows], [(s.lat, s.lon) for s in fake.trail]))
    assert out[0] == out[1]


# --- responder_search_box (the S02 search beat) ---------------------------------------------


def _truth_at(run: SimRun, cid: str, tick: int) -> tuple[float, float]:
    gt = run.contact_truth[cid].gt_id
    pts = [p for p in run.truth_paths[gt] if p.tick <= tick]
    return pts[-1].lat, pts[-1].lon


def _box(center: tuple[float, float], tick: int, radius: float = 120.0) -> SearchBox:
    cx, cy = to_local_m(*center)
    lo = from_local_m(cx - radius, cy - radius)
    hi = from_local_m(cx + radius, cy + radius)
    return SearchBox(
        center_lat=center[0],
        center_lon=center[1],
        radius_m=radius,
        min_lat=lo[0],
        max_lat=hi[0],
        min_lon=lo[1],
        max_lon=hi[1],
        anchor=PositionFix(lat=center[0], lon=center[1], tick=tick, sigma_m=10.0),
        to_tick=tick,
        assumption_row_id="0" * 16,
    )


def _s02_search(offset_m: float) -> tuple[P.ProbeResult, tuple[float, float], int]:
    scenario, run = _world("S02", 3, 700)
    cid = _main_contact(run)
    tick = run.tracks[cid].points[-1].tick + 150  # after the vehicle fell silent
    truth = _truth_at(run, cid, tick)
    x, y = to_local_m(*truth)
    box = _box(from_local_m(x + offset_m, y + 0.4 * offset_m), tick)
    dock = _dock_near(*truth)
    fake = FakeResponder(_harbor(), dock, tick_s=float(run.tick_s), tick=tick)
    ctx = _ctx(scenario, run, responder=fake, tick=tick, search_box=box)
    res = run_probe(SEARCH, ctx, _table(scenario, run), cache=ProbeCache())
    return res, _truth_at(run, cid, res.landed_tick), tick


def test_s02_search_finds_the_vehicle_when_the_box_contains_truth() -> None:
    res, truth, _ = _s02_search(40.0)
    rows = {r.feature: r for r in res.rows}
    assert rows[FeatureName.SEARCH_RESULT].value == "found"
    found = P.search_position(res.rows)
    assert found is not None
    assert distance_m(*found, *truth) < 2 * P.SEARCH_DETECT_RADIUS_M


def test_search_reports_not_found_when_the_box_misses() -> None:
    res, _, _ = _s02_search(900.0)
    rows = {r.feature: r for r in res.rows}
    if rows[FeatureName.SEARCH_RESULT].value != UNKNOWN:  # box may fall outside the geofence
        assert rows[FeatureName.SEARCH_RESULT].value == "not_found"
        assert P.search_position(res.rows) is None


# --- the planner -----------------------------------------------------------------------------


def _planner_choice(
    scenario_id: str, seed: int, ticks: int, *, fog: bool = False, available: bool = True
) -> ProbeId | None:
    scenario, run = _world(scenario_id, seed, ticks, fog)
    table = _table(scenario, run)
    cid = _main_contact(run)
    tick = run.tracks[cid].points[-1].tick
    fake = _fake_for(run, tick)
    ctx = _ctx(scenario, run, responder=fake, tick=tick)
    eta = P.resolve_args(LOOK, ctx, table)["eta_ticks"]
    assert isinstance(eta, int)
    tables = load_likelihood_tables()
    state = state_from_table(
        table,
        cid,
        tables,
        load_prior_config(),
        current_rung=Rung.ASK,
        ledger=BudgetLedger(),
        tick=tick,
        tick_s=float(run.tick_s),
        ran_probes=frozenset((*WATCH, ProbeId.ACOUSTIC_HAIL, ProbeId.REGISTRY_LOOKUP)),
    )
    from dataclasses import replace

    state = replace(
        state,
        responder_available=available,
        probe_latency_s={LOOK: eta * float(run.tick_s), SEARCH: 10_000.0},
    )
    return DeterministicPlanner().choose(state, tables).probe_id


@pytest.mark.parametrize(
    ("scenario_id", "seed", "ticks", "fog"),
    [("S05", 11, 240, False), ("S06", 13, 300, False), ("S05", 11, 240, True)],
    ids=["S05", "S06", "S05-fog_no_camera"],
)
def test_planner_picks_look_when_it_beats_static_probes(
    scenario_id: str, seed: int, ticks: int, fog: bool
) -> None:
    assert _planner_choice(scenario_id, seed, ticks, fog=fog) is LOOK
    assert _planner_choice(scenario_id, seed, ticks, fog=fog, available=False) is not LOOK


def test_one_dispatch_per_contact_in_the_ledger() -> None:
    ledger = BudgetLedger()
    assert ledger.limits.responder_dispatches_per_contact == 1
    assert ledger.allows("c-0001", LOOK) and ledger.allows("c-0001", SEARCH)
    ledger.record("c-0001", LOOK)
    assert not ledger.allows("c-0001", LOOK) and not ledger.allows("c-0001", SEARCH)
    assert ledger.allows("c-0002", LOOK)  # per contact
    assert ledger.allows("c-0001", ProbeId.CAMERA_CUE)  # static probes unaffected
    copy = ledger.copy()
    assert copy.dispatches("c-0001") == 1
