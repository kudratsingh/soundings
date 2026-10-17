"""WO-088 part A: the simulated responder platform (a mobile probe at rung ask).

Proves the state machine (DOCKED -> TRANSIT -> ON_STATION -> RETURNING -> DOCKED), travel
time from real distance, the dispatch rules (busy, one dispatch per contact, no station
point), every station-keeping invariant on the trail (standoff, geofence, corridor, away
side, no following, never closing faster than the contact) including a hypothesis
property test over random contact paths, the cubic battery law and the return to dock,
the lawnmower search, determinism, and the vocabulary (CLAUDE.md 2.1, 2.4, 2.5, 8 A2).
"""

from __future__ import annotations

import ast
import math
import re
from dataclasses import dataclass
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import ValidationError

from soundings.canonical import canonical_json
from soundings.schemas._base import LAT_MAX, LAT_MIN, LON_MAX, LON_MIN, REPO_ROOT
from soundings.schemas.geo import distance_m, from_local_m, to_local_m
from soundings.schemas.harbor import Harbor, load_harbor
from soundings.sim import responder as responder_mod
from soundings.sim.responder import (
    DEFAULT_RESPONDER_PATH,
    MIN_STANDOFF_M,
    Responder,
    ResponderConfig,
    ResponderSnapshot,
    ResponderState,
    battery_drain_min,
    in_corridor,
    load_responder_config,
)

TICK_S = 2.0
HARBOR = load_harbor()
CFG = load_responder_config()

# A contact in the Main Channel, about 600 m north of the dock; nearest asset is a fuel berth.
C_LAT, C_LON = 33.7395, -118.2605


def _responder(cfg: ResponderConfig = CFG, harbor: Harbor = HARBOR) -> Responder:
    return Responder(cfg, harbor, tick_s=TICK_S)


@dataclass(frozen=True)
class Fix:
    tick: int
    lat: float
    lon: float
    speed: float


def _drive(resp: Responder, path: list[Fix]) -> list[tuple[ResponderSnapshot, Fix]]:
    out: list[tuple[ResponderSnapshot, Fix]] = []
    for f in path:
        snap = resp.step(f.tick, contact_lat=f.lat, contact_lon=f.lon, contact_speed_mps=f.speed)
        out.append((snap, f))
    return out


def _still(n: int, lat: float = C_LAT, lon: float = C_LON, start: int = 1) -> list[Fix]:
    return [Fix(t, lat, lon, 0.0) for t in range(start, start + n)]


def _violations(
    pairs: list[tuple[ResponderSnapshot, Fix]], resp: Responder, harbor: Harbor = HARBOR
) -> list[str]:
    """Every rule the trail must keep, checked from the outside on reported positions."""
    bad: list[str] = []
    for snap, f in pairs:
        if not resp.inside_geofence(snap.lat, snap.lon):
            bad.append(f"tick {snap.tick}: outside geofence")
        if snap.state is ResponderState.DOCKED:
            continue
        if snap.target_contact_id is None:
            continue
        rng = distance_m(snap.lat, snap.lon, f.lat, f.lon)
        if rng < CFG.standoff_m:
            bad.append(f"tick {snap.tick}: inside standoff ({rng:.2f} m)")
        asset = harbor.nearest_asset(f.lat, f.lon)
        assert asset is not None
        if in_corridor(
            to_local_m(snap.lat, snap.lon),
            to_local_m(f.lat, f.lon),
            to_local_m(asset.lat, asset.lon),
            CFG.corridor_half_width_m,
        ):
            bad.append(f"tick {snap.tick}: inside the corridor to {asset.asset_id}")
    return bad


# ----------------------------------------------------------------------------- config


def test_config_values_and_labels() -> None:
    assert CFG.id == "resp_1"
    assert CFG.speed_mps == 1.5
    assert CFG.standoff_m == 25.0
    assert CFG.battery_min == 90.0
    assert CFG.restation_hysteresis_m == 5.0
    assert CFG.orbit_period_s == 60.0
    assert set(CFG.geofence_zone_ids) == {z.zone_id for z in HARBOR.zones}
    dock_zone = HARBOR.zone_at(CFG.dock.lat, CFG.dock.lon)
    assert dock_zone is not None and dock_zone.zone_id in CFG.geofence_zone_ids
    text = DEFAULT_RESPONDER_PATH.read_text(encoding="utf-8")
    keyed = [ln for ln in text.splitlines() if re.match(r"^  [a-z_]+:", ln)]
    assert keyed, "no keys found"
    for line in keyed:
        assert "demo value" in line, line


def test_config_is_strict(tmp_path: Path) -> None:
    raw = DEFAULT_RESPONDER_PATH.read_text(encoding="utf-8")
    low = tmp_path / "low.yaml"
    low.write_text(raw.replace("standoff_m: 25.0", "standoff_m: 20.0"), encoding="utf-8")
    with pytest.raises(ValidationError):
        load_responder_config(low)
    extra = tmp_path / "extra.yaml"
    extra.write_text(raw + "  max_closing_speed_mps: 3.0\n", encoding="utf-8")
    with pytest.raises(ValidationError):
        load_responder_config(extra)
    assert MIN_STANDOFF_M == 25.0


def test_unknown_geofence_zone_rejected() -> None:
    cfg = CFG.model_copy(update={"geofence_zone_ids": ("main_channel", "open_sea")})
    with pytest.raises(ValueError, match="open_sea"):
        _responder(cfg)


def test_dock_outside_geofence_rejected() -> None:
    cfg = CFG.model_copy(update={"geofence_zone_ids": ("vincent_thomas_bridge",)})
    with pytest.raises(ValueError, match="dock"):
        _responder(cfg)


# ----------------------------------------------------------------------- state machine


def test_state_machine_round_trip() -> None:
    resp = _responder()
    assert resp.snapshot.state is ResponderState.DOCKED
    assert resp.snapshot.target_contact_id is None
    res = resp.dispatch_to_look("c-0001", C_LAT, C_LON, 0)
    assert res.accepted, res.reason
    assert res.eta_s > 0.0
    pairs = _drive(resp, _still(600))
    states = [s.state for s, _ in pairs]
    assert states[0] is ResponderState.TRANSIT
    first_on = states.index(ResponderState.ON_STATION)
    assert all(st_ is ResponderState.TRANSIT for st_ in states[:first_on])
    # arrival within the ETA (plus one tick and the hysteresis margin)
    assert pairs[first_on][0].t_s <= res.eta_s + 2 * TICK_S + CFG.restation_hysteresis_m
    assert all(st_ is ResponderState.ON_STATION for st_ in states[first_on:])
    snap = resp.snapshot
    assert snap.target_contact_id == "c-0001"
    assert snap.distance_to_target_m is not None
    assert snap.distance_to_target_m >= CFG.standoff_m
    resp.recall(600)
    assert resp.state is ResponderState.RETURNING
    back = _drive(resp, _still(800, start=601))
    tail_states = [s.state for s, _ in back]
    assert ResponderState.RETURNING in tail_states
    assert resp.state is ResponderState.DOCKED
    assert resp.snapshot.target_contact_id is None
    assert (resp.snapshot.lat, resp.snapshot.lon) == (CFG.dock.lat, CFG.dock.lon)
    assert _violations(pairs + back, resp) == []


def test_on_station_point_is_on_the_away_side() -> None:
    resp = _responder()
    assert resp.dispatch_to_look("c-0001", C_LAT, C_LON, 0).accepted
    pairs = _drive(resp, _still(500))
    asset = HARBOR.nearest_asset(C_LAT, C_LON)
    assert asset is not None
    cx, cy = to_local_m(C_LAT, C_LON)
    ax, ay = to_local_m(asset.lat, asset.lon)
    on = [s for s, _ in pairs if s.state is ResponderState.ON_STATION]
    assert on
    for s in on:
        rx, ry = to_local_m(s.lat, s.lon)
        # positive projection on the contact-minus-asset direction: away from the asset
        assert (rx - cx) * (cx - ax) + (ry - cy) * (cy - ay) > 0.0
        assert CFG.standoff_m <= distance_m(s.lat, s.lon, C_LAT, C_LON) <= CFG.look_range_max_m


def test_orbit_moves_on_station() -> None:
    resp = _responder()
    assert resp.dispatch_to_look("c-0001", C_LAT, C_LON, 0).accepted
    pairs = _drive(resp, _still(500))
    on = [s for s, _ in pairs if s.state is ResponderState.ON_STATION]
    positions = {(s.lat, s.lon) for s in on}
    assert len(positions) > 5, "the responder should sweep its station arc"


def test_step_same_tick_is_noop_and_backwards_rejected() -> None:
    resp = _responder()
    assert resp.dispatch_to_look("c-0001", C_LAT, C_LON, 0).accepted
    a = resp.step(3, contact_lat=C_LAT, contact_lon=C_LON)
    b = resp.step(3, contact_lat=C_LAT, contact_lon=C_LON)
    assert a == b
    assert len(resp.trail) == 2
    with pytest.raises(ValueError):
        resp.step(2)


def test_late_dispatch_moves_one_tick_at_a_time() -> None:
    resp = _responder()
    assert resp.dispatch_to_look("c-0001", C_LAT, C_LON, 100).accepted
    snap = resp.step(101, contact_lat=C_LAT, contact_lon=C_LON)
    moved = distance_m(CFG.dock.lat, CFG.dock.lon, snap.lat, snap.lon)
    assert moved <= CFG.speed_mps * TICK_S + 0.2
    assert snap.battery_min < CFG.battery_min


# ------------------------------------------------------------------------ travel time


def test_travel_time_from_real_distance() -> None:
    resp = _responder()
    lat2, lon2 = 33.7300, -118.2600  # straight leg down the Main Channel
    want = distance_m(CFG.dock.lat, CFG.dock.lon, lat2, lon2) / CFG.speed_mps
    assert resp.travel_time_s(CFG.dock.lat, CFG.dock.lon, lat2, lon2) == pytest.approx(
        want, abs=1e-5
    )
    # a leg that would cut across land routes around it: longer than the straight line
    west = (33.7460, -118.2780)  # Inner Harbor West
    east = (33.7150, -118.2400)  # Pier 400 Approach
    straight = distance_m(*west, *east) / CFG.speed_mps
    routed = resp.travel_time_s(*west, *east)
    assert routed >= straight - 1e-6


def test_dispatch_eta_matches_travel_time() -> None:
    resp = _responder()
    res = resp.dispatch_to_look("c-0001", C_LAT, C_LON, 0)
    direct = resp.travel_time_s(CFG.dock.lat, CFG.dock.lon, C_LAT, C_LON)
    # the station is about 30 m short of the contact, on the far side at most
    assert abs(res.eta_s - direct) <= (CFG.look_range_max_m * 2) / CFG.speed_mps


# --------------------------------------------------------------------- dispatch rules


def test_second_dispatch_while_busy_is_rejected() -> None:
    resp = _responder()
    assert resp.dispatch_to_look("c-0001", C_LAT, C_LON, 0).accepted
    second = resp.dispatch_to_look("c-0002", 33.733, -118.261, 0)
    assert not second.accepted
    assert "person" in second.reason


def test_one_dispatch_per_contact() -> None:
    resp = _responder()
    assert resp.dispatch_to_look("c-0001", C_LAT, C_LON, 0).accepted
    _drive(resp, _still(3))
    resp.recall(3)
    _drive(resp, _still(400, start=4))
    assert resp.state is ResponderState.DOCKED
    again = resp.dispatch_to_look("c-0001", C_LAT, C_LON, 404)
    assert not again.accepted
    assert "one dispatch per contact" in again.reason
    assert resp.dispatched_contact_ids == ("c-0001",)
    other = resp.dispatch_to_look("c-0002", C_LAT, C_LON, 404)
    assert other.accepted


def test_no_station_point_outside_geofence() -> None:
    resp = _responder()
    # open water south of every declared zone
    res = resp.dispatch_to_look("c-0009", 33.7040, -118.2900, 0)
    assert not res.accepted
    assert "geofence" in res.reason
    assert resp.state is ResponderState.DOCKED
    assert resp.dispatched_contact_ids == ()


# ------------------------------------------------------------------------- invariants


def _line_path(
    lat0: float, lon0: float, heading_deg: float, speed: float, n: int, start: int = 1
) -> list[Fix]:
    x0, y0 = to_local_m(lat0, lon0)
    h = math.radians(heading_deg)
    out: list[Fix] = []
    for k in range(n):
        d = speed * TICK_S * (k + 1)
        lat, lon = from_local_m(x0 + d * math.sin(h), y0 + d * math.cos(h))
        out.append(Fix(start + k, lat, lon, speed))
    return out


def test_contact_opening_range_holds_then_returns_no_following() -> None:
    resp = _responder()
    assert resp.dispatch_to_look("c-0001", C_LAT, C_LON, 0).accepted
    settle = _drive(resp, _still(400))
    assert resp.state is ResponderState.ON_STATION
    s0 = resp.snapshot
    # the contact heads straight away from the responder (north-west) at 1 m/s
    bearing = math.degrees(
        math.atan2(
            to_local_m(C_LAT, C_LON)[0] - to_local_m(s0.lat, s0.lon)[0],
            to_local_m(C_LAT, C_LON)[1] - to_local_m(s0.lat, s0.lon)[1],
        )
    )
    away = _line_path(C_LAT, C_LON, bearing, 1.0, 200, start=401)
    pairs = _drive(resp, away)
    on = [s for s, _ in pairs if s.state is ResponderState.ON_STATION]
    assert on and all(s.speed_mps == 0.0 for s in on), "holds station while range opens"
    assert any(s.state is ResponderState.RETURNING for s, _ in pairs), "returns to dock"
    # the range only grows while on station: it never closes the distance behind the contact
    ranges = [s.distance_to_target_m for s in on]
    assert all(r is not None for r in ranges)
    assert ranges == sorted(ranges)  # type: ignore[type-var]
    assert _violations(settle + pairs, resp) == []


def test_contact_closing_restations_and_keeps_standoff() -> None:
    resp = _responder()
    assert resp.dispatch_to_look("c-0001", C_LAT, C_LON, 0).accepted
    settle = _drive(resp, _still(400))
    s0 = resp.snapshot
    bearing = math.degrees(
        math.atan2(
            to_local_m(s0.lat, s0.lon)[0] - to_local_m(C_LAT, C_LON)[0],
            to_local_m(s0.lat, s0.lon)[1] - to_local_m(C_LAT, C_LON)[1],
        )
    )
    toward = _line_path(C_LAT, C_LON, bearing, 1.0, 120, start=401)
    pairs = _drive(resp, toward)
    assert _violations(settle + pairs, resp) == []
    # never closes faster than the contact on station: the range the responder's own
    # move gives up (to the contact's new position) is at most the contact's own travel
    # plus the 0.5 m orbit chord
    seq = settle[-1:] + pairs
    for (a, _), (b, f) in zip(seq, seq[1:], strict=False):
        if b.state is not ResponderState.ON_STATION:
            continue
        before = distance_m(a.lat, a.lon, f.lat, f.lon)
        after = distance_m(b.lat, b.lon, f.lat, f.lon)
        assert after >= before - f.speed * TICK_S - 0.5 - 1e-3, (b.tick, before, after)


_START_LAT = st.floats(min_value=33.7300, max_value=33.7460)
_START_LON = st.floats(min_value=-118.2655, max_value=-118.2565)
_TURNS = st.lists(st.sampled_from([-30.0, -10.0, 0.0, 0.0, 10.0, 30.0]), min_size=8, max_size=8)
_SPEEDS = st.lists(st.sampled_from([0.0, 0.3, 0.6, 1.0]), min_size=8, max_size=8)


def _random_path(
    lat0: float, lon0: float, heading0: float, turns: list[float], speeds: list[float], n: int
) -> list[Fix]:
    x, y = to_local_m(lat0, lon0)
    h = heading0
    out: list[Fix] = []
    seg = max(1, n // len(turns))
    for k in range(n):
        leg = min(len(turns) - 1, k // seg)
        if k % seg == 0:
            h += turns[leg]
        v = speeds[leg]
        x += v * TICK_S * math.sin(math.radians(h))
        y += v * TICK_S * math.cos(math.radians(h))
        lat, lon = from_local_m(x, y)
        lat = min(LAT_MAX, max(LAT_MIN, lat))
        lon = min(LON_MAX, max(LON_MIN, lon))
        x, y = to_local_m(lat, lon)
        out.append(Fix(k + 1, lat, lon, v))
    return out


@settings(derandomize=True, max_examples=40, deadline=None)
@given(
    lat0=_START_LAT,
    lon0=_START_LON,
    heading0=st.floats(min_value=0.0, max_value=359.0),
    turns=_TURNS,
    speeds=_SPEEDS,
)
def test_property_standoff_geofence_corridor_over_random_paths(
    lat0: float, lon0: float, heading0: float, turns: list[float], speeds: list[float]
) -> None:
    resp = _responder()
    res = resp.dispatch_to_look("c-0001", lat0, lon0, 0)
    if not res.accepted:
        assert resp.state is ResponderState.DOCKED
        return
    path = _random_path(lat0, lon0, heading0, turns, speeds, 320)
    pairs = _drive(resp, path)
    assert _violations(pairs, resp) == []
    for s, f in pairs:
        if s.distance_to_target_m is not None:
            assert s.distance_to_target_m >= CFG.standoff_m
            assert s.distance_to_target_m == pytest.approx(
                distance_m(s.lat, s.lon, f.lat, f.lon), abs=1e-3
            )


def test_every_trail_point_inside_geofence_including_dock() -> None:
    resp = _responder()
    assert resp.inside_geofence(CFG.dock.lat, CFG.dock.lon)
    assert not resp.inside_geofence(33.7040, -118.2900)
    assert resp.dispatch_to_look("c-0001", 33.7470, -118.2700, 0).accepted  # Inner Harbor West
    pairs = _drive(resp, _still(900, lat=33.7470, lon=-118.2700))
    assert any(s.state is ResponderState.ON_STATION for s, _ in pairs)
    assert all(resp.inside_geofence(s.lat, s.lon) for s in resp.trail)
    assert _violations(pairs, resp, HARBOR) == []


# --------------------------------------------------------------------------- battery


def test_battery_law_is_cubic() -> None:
    k, hotel = CFG.drain_coeff, CFG.hotel_load_min_per_min
    assert battery_drain_min(0.0, 60.0, k, hotel) == pytest.approx(hotel)
    assert battery_drain_min(1.5, 60.0, k, hotel) == pytest.approx(hotel + k * 1.5**3)
    d1 = battery_drain_min(1.0, 60.0, k, 0.0)
    d2 = battery_drain_min(2.0, 60.0, k, 0.0)
    assert d2 == pytest.approx(8.0 * d1)
    with pytest.raises(ValueError):
        battery_drain_min(-1.0, 60.0, k, hotel)


def test_battery_drains_only_underway_and_returns_before_empty() -> None:
    resp = _responder()
    _drive(resp, _still(10))
    assert resp.snapshot.battery_min == CFG.battery_min  # docked: no drain
    small = CFG.model_copy(update={"battery_min": 20.0})
    resp = _responder(small)
    res = resp.dispatch_to_look("c-0001", C_LAT, C_LON, 0)
    assert res.accepted, res.reason
    pairs = _drive(resp, _still(1500))
    states = [s.state for s, _ in pairs]
    assert ResponderState.ON_STATION in states
    assert resp.last_reason == "docked"
    assert resp.state is ResponderState.DOCKED
    batteries = [s.battery_min for s, _ in pairs]
    assert batteries == sorted(batteries, reverse=True)
    assert min(batteries) > 0.0
    # the first RETURNING tick came from the battery rule, before the battery ran out
    first_ret = states.index(ResponderState.RETURNING)
    assert pairs[first_ret][0].battery_min > 0.5
    assert _violations(pairs, resp) == []


def test_dispatch_rejected_when_battery_cannot_cover_round_trip() -> None:
    resp = _responder(CFG.model_copy(update={"battery_min": 2.0}))
    res = resp.dispatch_to_look("c-0001", C_LAT, C_LON, 0)
    assert not res.accepted
    assert "battery" in res.reason


# ---------------------------------------------------------------------------- search


@dataclass(frozen=True)
class Box:
    min_lat: float
    max_lat: float
    min_lon: float
    max_lon: float


def test_search_flies_the_lawnmower_and_returns() -> None:
    resp = _responder()
    box = Box(min_lat=33.7390, max_lat=33.7420, min_lon=-118.2720, max_lon=-118.2680)
    res = resp.dispatch_search("c-0002", box, 0)
    assert res.accepted, res.reason
    states: list[ResponderState] = []
    for t in range(1, 3000):
        states.append(resp.step(t).state)
        if resp.state is ResponderState.DOCKED:
            break
    assert ResponderState.ON_STATION in states
    assert resp.search_complete
    assert resp.state is ResponderState.DOCKED
    assert all(resp.inside_geofence(s.lat, s.lon) for s in resp.trail)
    # every point of the box was passed within one leg spacing
    for lat in (33.7392, 33.7405, 33.7418):
        for lon in (-118.2718, -118.2700, -118.2682):
            near = resp.nearest_pass_m(lat, lon)
            assert near is not None and near <= CFG.search_track_spacing_m, (lat, lon, near)


def test_search_box_outside_geofence_rejected() -> None:
    resp = _responder()
    box = Box(min_lat=33.7030, max_lat=33.7060, min_lon=-118.2950, max_lon=-118.2900)
    res = resp.dispatch_search("c-0003", box, 0)
    assert not res.accepted


# ----------------------------------------------------------------------- determinism


def _scripted_run() -> str:
    resp = _responder()
    resp.dispatch_to_look("c-0001", C_LAT, C_LON, 0)
    path = _random_path(C_LAT, C_LON, 200.0, [0, 30, -30, 10, 0, -10, 30, 0], [0.6] * 8, 500)
    _drive(resp, path)
    return canonical_json([s.model_dump(mode="json") for s in resp.trail])


def test_determinism_byte_identical_trail() -> None:
    assert _scripted_run() == _scripted_run()


def test_no_wall_clock_or_randomness_in_module() -> None:
    src = Path(responder_mod.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert not imported & {"time", "datetime", "random", "numpy", "secrets", "uuid"}


# ------------------------------------------------------------------------ vocabulary

# The directive's own words to avoid (the denylist file covers the rest).
_FORBIDDEN_NAME = re.compile(r"chase|follow_into|follow-into|close_with|close-with", re.IGNORECASE)


def test_vocabulary_names_and_denylist() -> None:
    patterns = [
        re.compile(ln.strip(), re.IGNORECASE)
        for ln in (REPO_ROOT / "config" / "denylist.txt").read_text(encoding="utf-8").splitlines()
        if ln.strip() and not ln.startswith("#")
    ]
    names = [n for n in dir(responder_mod) if not n.startswith("__")]
    names += [n for n in dir(Responder) if not n.startswith("__")]
    names += [m.value for m in ResponderState]
    names += list(ResponderConfig.model_fields)
    names += list(ResponderSnapshot.model_fields)
    assert [n for n in names if _FORBIDDEN_NAME.search(n)] == []
    spaced = [n.replace("_", " ") for n in names]
    assert [n for n in spaced if any(p.search(n) for p in patterns)] == []
    for path in (Path(responder_mod.__file__), DEFAULT_RESPONDER_PATH):
        text = path.read_text(encoding="utf-8")
        assert [p.pattern for p in patterns if p.search(text)] == [], path
        assert not _FORBIDDEN_NAME.search(text), path
    assert {m.value for m in ResponderState} == {"DOCKED", "TRANSIT", "ON_STATION", "RETURNING"}
