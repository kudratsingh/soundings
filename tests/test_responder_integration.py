"""WO-088 part D: the real responder (part A sim) through the runner, the stream and the demo.

Every run here uses ``soundings.sim.responder.Responder`` built from
``config/responder.yaml`` (``DEFAULT_RESPONDER``), never the test double. Ground truth is
read only in these assertions (grading), never by the platform. Deterministic.
"""

from __future__ import annotations

import math
import re
import subprocess
import sys
from functools import lru_cache

import pytest

from soundings.canonical import canonical_json, round6
from soundings.investigation.probes import (
    CLOSE_ACOUSTIC_CONFIDENCE,
    RESPONDER_PROBES,
    range_factor,
    search_detect_radius_m,
    search_position,
)
from soundings.runner import (
    DEFAULT_RESPONDER,
    RunResultV2,
    ScenarioRunnerV2,
    ScenarioRunnerV3,
    make_responder,
)
from soundings.schemas.enums import FeatureName, LadderState, ProbeId
from soundings.schemas.geo import distance_m, to_local_m
from soundings.schemas.harbor import Harbor, load_harbor
from soundings.sim.responder import MIN_STANDOFF_M, load_responder_config

CATALOG = [("S01", 1), ("S02", 3), ("S03", 5), ("S04", 7), ("S05", 11), ("S06", 13)]
CATALOG += [("S07", 17), ("S08", 19)]


@lru_cache(maxsize=1)
def _harbor() -> Harbor:
    return load_harbor()


@lru_cache(maxsize=16)
def _run(sid: str, seed: int) -> RunResultV2:
    return ScenarioRunnerV2(sid, seed=seed).run(responder=DEFAULT_RESPONDER)


def _inside_geofence(lat: float, lon: float) -> bool:
    port = make_responder(_harbor(), 2.0)
    return bool(port.inside_geofence(lat, lon))


# --- the dock and the config ------------------------------------------------------------


def test_dock_is_inside_a_declared_zone_near_the_inner_harbor() -> None:
    cfg = load_responder_config()
    zone = _harbor().zone_at(cfg.dock.lat, cfg.dock.lon)
    assert zone is not None and zone.zone_id in cfg.geofence_zone_ids
    assert zone.zone_id == "inner_basin_research"
    fuel = min(distance_m(cfg.dock.lat, cfg.dock.lon, a.lat, a.lon) for a in _harbor().assets)
    assert fuel < 400.0  # the old Main Channel pier was about 1 km from the fuel berths


def test_runner_v3_defaults_to_the_real_responder_and_none_opts_out() -> None:
    with_it = ScenarioRunnerV3("S02", seed=3).run()
    without = ScenarioRunnerV3("S02", seed=3).run(responder=None)
    assert with_it.responder_trail and with_it.responder_dispatches
    assert not without.responder_trail and not without.responder_dispatches
    assert ScenarioRunnerV2("S02", seed=3).run().responder_trail == ()  # v2 default: none


# --- S02: the search beat ---------------------------------------------------------------


def test_s02_search_finds_the_glider_inside_the_box_it_flew() -> None:
    result = _run("S02", 3)
    searches = [d for d in result.responder_dispatches if d[0] == "search"]
    assert len(searches) == 1 and searches[0][3]
    _, cid, tick, _ = searches[0]
    first = next(n for n in result.notices if n.contact_id == cid)
    assert first.tick == tick
    # the scenario's own premise: the drift box holds the vehicle's true position
    gt = result.sim.contact_truth[cid].gt_id
    truth = {p.tick: (p.lat, p.lon) for p in result.sim.truth_paths[gt]}
    assert first.search_box.contains(*truth[tick])
    follow = [n for n in result.notices if n.case_id == first.case_id and n.tick > first.tick]
    assert len(follow) == 1
    text = next(line.text for line in follow[0].lines if "responder searched" in line.text)
    assert "found the vehicle at" in text
    rows = [
        r
        for r in result.evidence
        if r.probe_id is ProbeId.RESPONDER_SEARCH_BOX and r.contact_id == cid
    ]
    found = search_position(rows)
    assert found is not None
    assert {r.row_id for r in rows} <= set(follow[0].evidence_row_ids)
    m = re.search(r"centre ([\d.-]+), ([\d.-]+), radius (\d+) m", text)
    assert m is not None
    cx, cy = to_local_m(float(m.group(1)), float(m.group(2)))
    fx, fy = to_local_m(*found)
    radius = float(m.group(3)) + 1.0
    assert abs(fx - cx) <= radius and abs(fy - cy) <= radius  # inside the box it flew
    found_tick = follow[0].tick
    assert distance_m(*found, *truth[found_tick]) < 1.0
    near = [s for s in result.responder_trail if s.tick == found_tick][0]
    assert distance_m(near.lat, near.lon, *found) <= search_detect_radius_m(
        make_responder(_harbor(), 2.0)
    )
    # the result lands inside the scenario's own time
    assert found_tick < result.sim.n_ticks


def test_search_lane_spacing_has_one_source() -> None:
    port = make_responder(_harbor(), 2.0)
    spacing = load_responder_config().search_track_spacing_m
    assert port.search_track_spacing_m == spacing
    assert search_detect_radius_m(port) >= spacing / 2.0  # adjacent legs overlap


# --- the look: on station, at standoff, scaled confidence -------------------------------


def test_look_lands_on_station_with_range_scaled_rows() -> None:
    result = _run("S03", 5)
    looks = [d for d in result.responder_dispatches if d[0] == "look" and d[3]]
    assert looks
    _, cid, tick, _ = looks[0]
    calls = [c for c in result.probe_calls if c.probe_id is ProbeId.RESPONDER_LOOK]
    assert [c.contact_id for c in calls] == [cid]
    landed = calls[0].tick
    snap = [s for s in result.responder_trail if s.tick == landed][0]
    assert str(snap.state.value) == "ON_STATION" and snap.target_contact_id == cid
    rows = {r.feature: r for r in result.evidence if r.probe_id is ProbeId.RESPONDER_LOOK}
    assert FeatureName.VISUAL_CLASS in rows  # the visual row is always written
    assert snap.distance_to_target_m is not None
    expected = round6(CLOSE_ACOUSTIC_CONFIDENCE * range_factor(snap.distance_to_target_m))
    assert rows[FeatureName.DEPTH].confidence == expected
    assert all(r.t == landed for r in rows.values())
    # nothing before ON_STATION: no responder row earlier than the first station tick
    first_station = min(
        s.tick for s in result.responder_trail if str(s.state.value) == "ON_STATION"
    )
    assert first_station <= landed
    assert first_station > tick


@pytest.mark.parametrize(("sid", "seed"), CATALOG, ids=[c[0] for c in CATALOG])
def test_trail_keeps_standoff_geofence_and_one_dispatch_per_contact(sid: str, seed: int) -> None:
    result = _run(sid, seed)
    trail = result.responder_trail
    assert [s.tick for s in trail[1:]] == sorted({s.tick for s in trail[1:]})
    for s in trail:
        assert _inside_geofence(s.lat, s.lon), (sid, s.tick)
        if s.distance_to_target_m is not None:
            assert s.distance_to_target_m >= MIN_STANDOFF_M - 1e-6, (sid, s.tick)
    accepted = [d[1] for d in result.responder_dispatches if d[3]]
    assert len(accepted) == len(set(accepted))
    # CLAUDE.md 2.1: no transit or station toward a contact after its case handed off
    for case in result.cases:
        if case.state is LadderState.HANDED_OFF and case.closed_tick is not None:
            late = [
                s
                for s in trail
                if s.target_contact_id == case.contact_id
                and s.tick > case.closed_tick
                and str(s.state.value) in ("TRANSIT", "ON_STATION")
            ]
            assert not late, (sid, case.case_id)


# --- the catalog is unchanged with the responder active ---------------------------------


def _graded(sid: str, responder: str | None) -> dict[str, object]:
    from soundings.eval.catalog_verify import check_expected, grade

    runner = ScenarioRunnerV3(sid)
    result = runner.run(responder=responder)
    g = grade(result)
    return {
        "terminal": None if g.terminal_state is None else g.terminal_state.value,
        "interruptions": list(g.interruptions),
        "cases": {c.case_id: c.state.value for c in result.cases},
        "handling": {k: v.value for k, v in result.handling_outcome.items()},
        "failures": sorted(f.key for f in check_expected(g, runner.scenario)),
    }


@pytest.mark.parametrize("sid", [c[0] for c in CATALOG])
def test_catalog_outcomes_unchanged_with_the_responder(sid: str) -> None:
    assert _graded(sid, DEFAULT_RESPONDER) == _graded(sid, None)


# --- S05 / S06: why the look beat cannot land before the call ---------------------------


@pytest.mark.parametrize(("sid", "seed"), [("S05", 11), ("S06", 13)])
def test_primary_handoff_comes_before_any_look_could_land(sid: str, seed: int) -> None:
    """The primary track's stop rule fires at INVESTIGATING entry (C_THREAT), 20 s after
    the hail; the shortest route from the dock is minutes. The platform does not dispatch
    a look it cannot finish before the call (open question in the WO-088 Part D notes)."""
    result = _run(sid, seed)
    port = make_responder(_harbor(), 2.0)
    for case in result.cases:
        if case.state is not LadderState.HANDED_OFF or case.closed_tick is None:
            continue
        track = result.sim.tracks[case.contact_id]
        fix = [p for p in track.points if p.tick <= case.closed_tick][-1]
        transit = port.travel_time_s(port.snapshot.lat, port.snapshot.lon, fix.lat, fix.lon)
        asked = [t.tick for t in result.transitions if t.case_id == case.case_id]
        window_s = (case.closed_tick - min(asked)) * float(result.sim.tick_s)
        if window_s < transit:
            assert not [d for d in result.responder_dispatches if d[1] == case.contact_id]


# --- the stream and the demo ------------------------------------------------------------


def test_run_engine_streams_the_real_responder() -> None:
    from soundings.stream.engine import RunEngine, stream_events

    engine = RunEngine(default_speed=0)
    handle = engine.start("S02", 3, speed=0, responder=True)
    events = [e for e in handle.events if e.type == "responder"]
    assert events and events[0].payload.id == load_responder_config().id
    assert any(e.payload.dispatched for e in events)
    assert all(len(e.payload.trail) <= 60 for e in events)
    assert not [e for e in stream_events("S02", 3) if e.type == "responder"]  # opt-in CLI
    plain = engine.start("S02", 3, speed=0)  # the engine default matches stream_events
    assert not [e for e in plain.events if e.type == "responder"]
    with_r = [
        canonical_json(e.model_dump(mode="json")) for e in stream_events("S02", 3, responder=True)
    ]
    again = [
        canonical_json(e.model_dump(mode="json")) for e in stream_events("S02", 3, responder=True)
    ]
    assert with_r == again


def test_api_run_streams_the_real_responder(monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi.testclient import TestClient

    import soundings.stream.engine as engine_mod
    from soundings.api import create_app

    engine = engine_mod.RunEngine()
    monkeypatch.setattr(engine_mod, "_ENGINE", engine)
    client = TestClient(create_app())
    body = {"scenario": "S02", "seed": 3, "speed": 0}
    run_id = client.post("/api/runs", json=body).json()["run_id"]
    assert [e for e in engine.get(run_id).events if e.type == "responder"]


def test_demo_prints_the_responder_beats() -> None:
    from soundings.demo.headless import responder_note
    from soundings.sim.scenario import resolve_scenario

    note = responder_note(resolve_scenario("S02"), 3)
    assert note.startswith("responder: search c-") and "found at" in note
    look = responder_note(resolve_scenario("S03"), 5)
    assert "responder: look" in look and "on station" in look and "standoff 25 m" in look


def test_demo_cli_replay_prints_responder_lines() -> None:
    out = subprocess.run(
        [sys.executable, "-m", "soundings.demo", "--script", "docs/demo-script.yaml"]
        + ["--mode", "replay", "--pace", "fast"],
        capture_output=True,
        text=True,
        check=True,
        env={"MODEL_MODE": "replay", "PATH": "/usr/bin:/bin"},
    ).stdout
    assert "responder: look" in out
    assert "The platform goes to look" in out


# --- determinism --------------------------------------------------------------------------


@pytest.mark.parametrize(("sid", "seed"), [("S05", 11), ("S03", 5), ("S02", 3)])
def test_two_runs_same_trail_and_rows(sid: str, seed: int) -> None:
    def dump() -> str:
        r = ScenarioRunnerV2(sid, seed=seed).run(responder=DEFAULT_RESPONDER)
        trail = [s.model_dump(mode="json") for s in r.responder_trail]
        rows = [x.model_dump(mode="json") for x in r.evidence if x.probe_id in RESPONDER_PROBES]
        return canonical_json(
            {
                "trail": trail,
                "rows": rows,
                "nested": r.nested_log_hash,
                "ladder": r.transition_log_hash,
            }
        )

    assert dump() == dump()
    assert math.isfinite(len(dump()))
