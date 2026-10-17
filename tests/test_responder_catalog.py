"""WO-088 part A exit test: the responder against every catalog scenario x seeds 1-5.

Each ground-truth contact's truth path (grading only: the investigation never reads truth)
is the contact track. The responder is dispatched to look at the first truth fix and
stepped every tick. On the whole trail: every point inside a declared security zone;
whenever it has a target, never inside the standoff and never in the corridor between the
contact and its nearest asset; it reaches station for every contact; same seed, same
trail.
"""

from __future__ import annotations

from functools import cache
from pathlib import Path

import pytest

from soundings.canonical import canonical_json
from soundings.schemas.geo import distance_m, to_local_m
from soundings.schemas.harbor import load_harbor
from soundings.schemas.scenario import load_scenario
from soundings.sim.responder import Responder, ResponderState, in_corridor, load_responder_config
from soundings.sim.run import run_scenario
from soundings.sim.scenario import catalog_paths
from soundings.sim.types import TruthPoint

SEEDS = (1, 2, 3, 4, 5)
HARBOR = load_harbor()
CFG = load_responder_config()


@cache
def _truth(path: Path, seed: int) -> tuple[float, dict[str, list[TruthPoint]]]:
    scenario = load_scenario(path, HARBOR)
    run = run_scenario(scenario, seed=seed, harbor=HARBOR)
    real = {gid: pts for gid, pts in sorted(run.truth_paths.items()) if not gid.startswith("fa-")}
    return run.tick_s, real


def _drive(tick_s: float, gid: str, pts: list[TruthPoint]) -> tuple[Responder, list[str]]:
    resp = Responder(CFG, HARBOR, tick_s=tick_s)
    res = resp.dispatch_to_look(gid, pts[0].lat, pts[0].lon, pts[0].tick)
    assert res.accepted, f"{gid}: {res.reason}"
    bad: list[str] = []
    for tp in pts[1:]:
        s = resp.step(
            tp.tick, contact_lat=tp.lat, contact_lon=tp.lon, contact_speed_mps=tp.speed_mps
        )
        if not resp.inside_geofence(s.lat, s.lon):
            bad.append(f"t{s.tick} outside geofence")
        if s.state is ResponderState.DOCKED or s.target_contact_id is None:
            continue
        rng = distance_m(s.lat, s.lon, tp.lat, tp.lon)
        if rng < CFG.standoff_m:
            bad.append(f"t{s.tick} inside standoff {rng:.2f} m")
        asset = HARBOR.nearest_asset(tp.lat, tp.lon)
        assert asset is not None
        if in_corridor(
            to_local_m(s.lat, s.lon),
            to_local_m(tp.lat, tp.lon),
            to_local_m(asset.lat, asset.lon),
            CFG.corridor_half_width_m,
        ):
            bad.append(f"t{s.tick} inside corridor to {asset.asset_id}")
    return resp, bad


@pytest.mark.parametrize("seed", SEEDS)
@pytest.mark.parametrize("path", catalog_paths(), ids=lambda p: p.name[:3])
def test_standoff_and_geofence_never_violated(path: Path, seed: int) -> None:
    tick_s, real = _truth(path, seed)
    assert real
    for gid, pts in real.items():
        resp, bad = _drive(tick_s, gid, pts)
        assert bad == [], f"{path.name} seed {seed} {gid}: {bad[:5]}"
        states = {s.state for s in resp.trail}
        assert ResponderState.ON_STATION in states, f"{path.name} seed {seed} {gid}"
        assert all(resp.inside_geofence(s.lat, s.lon) for s in resp.trail)


@pytest.mark.parametrize(
    "path", catalog_paths()[:1] + catalog_paths()[4:6], ids=lambda p: p.name[:3]
)
def test_catalog_trail_is_deterministic(path: Path) -> None:
    tick_s, real = _truth(path, 1)
    gid, pts = next(iter(real.items()))
    a, _ = _drive(tick_s, gid, pts)
    b, _ = _drive(tick_s, gid, pts)
    dump_a = canonical_json([s.model_dump(mode="json") for s in a.trail])
    dump_b = canonical_json([s.model_dump(mode="json") for s in b.trail])
    assert dump_a == dump_b
