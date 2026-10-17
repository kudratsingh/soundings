"""Busy Tuesday: a seeded generator for a working day in the harbor (WO-080).

``generate_busy_day`` is pure: the only randomness is ``numpy.random.default_rng(seed)``,
drawn in a fixed order (the job-type sequence, then per job its start, duration and
position, then the vessels), and it reads no clock. ``main`` is the CLI::

    python -m soundings.sim.generate --jobs 40 --vessels 3 --seed 7 --out scenarios/BT-01.yaml

The day holds filed jobs (one plan each; the last ``no_shows`` plans never enter the
water), surface vessel transits that only the AIS feed reports, and one unfiled contact
that is S05's ``gt-1`` read from its file, entering at the earliest tick of peak job
load. Nothing here sends anything anywhere: the output is a ``Scenario``.
"""

from __future__ import annotations

import argparse
import math
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any

import numpy as np
import yaml

from soundings.canonical import round6
from soundings.schemas.enums import BehaviorType, Hypothesis, JobType, LadderState, TonalBand
from soundings.schemas.geo import from_local_m, to_local_m
from soundings.schemas.harbor import Harbor, SensorNode, load_harbor
from soundings.schemas.scenario import (
    AcousticSpec,
    BehaviorSpec,
    ContactRegistrySpec,
    Expected,
    ExpectedContact,
    PlanWindow,
    RegistryPlan,
    Scenario,
    ScenarioContact,
    StartPoint,
    Waypoint,
    load_scenario,
)
from soundings.sim.run import _survey
from soundings.sim.scenario import SCENARIO_ROOT

JOB_TYPES: tuple[JobType, ...] = (
    JobType.SURVEY,
    JobType.HULL_CLEANING,
    JobType.PILE_INSPECTION,
    JobType.DREDGE,
)
"""The four generated job types (``JobType.OTHER`` is never generated)."""

WINDOW_PAD_S = 300.0
"""Plan windows pad the in-water interval on both sides (covers a re-issued id's hail)."""

START_MAX_S = 2.5 * 3600.0
QUANTUM_S = 60.0
HEARD_FRACTION = 0.6  # a job starts within 0.6 x range_m of its nearest node (WO-011 rule)
ROGUE_CLEARANCE_M = 250.0  # re-acquisition gate 75 m + 0.15 m/s x 100 s, plus margin
MAX_TRIES = 20_000
VESSEL_SPEED_RANGE_MPS = (3.0, 6.0)
S05_PATH = SCENARIO_ROOT / "S05-unfiled-closing-fuel-berth.yaml"
DEFAULT_OUT = "scenarios/BT-01.yaml"

# Vessel routes (lat, lon): the length of main_channel (ends within 2 x range of a node,
# like every scenario start), and one inbound route from the
# channel's south end into inner_harbor_west (DECISIONS 2026-10-17 06:37, WO-083).
CHANNEL_SOUTH = (33.7193, -118.2601)
CHANNEL_NORTH = (33.747, -118.2618)
WEST_BASIN = (33.7454, -118.2726)


@dataclass(frozen=True, slots=True)
class JobSpec:
    """One job type: where it works, how long, and how it moves."""

    zones: tuple[str, ...]
    hours: tuple[float, float]
    behavior: BehaviorType
    speed_mps: float | None
    depth_m: float
    acoustic: AcousticSpec
    vehicle: str
    operator: str
    lane_spacing_m: float | None = None
    radius_m: float | None = None
    jitter_mps: float | None = None
    pilings: tuple[str, ...] = ()
    clear_whole_path: bool = False
    """Check every lawnmower leg (not only the start) against the rogue's path."""


JOB_SPECS: dict[JobType, JobSpec] = {
    JobType.SURVEY: JobSpec(
        zones=("inner_basin_research",),
        hours=(2.0, 4.0),
        behavior=BehaviorType.LAWNMOWER,
        speed_mps=1.0,
        depth_m=8.0,
        acoustic=AcousticSpec(
            propeller_tonal=True, tonal_band=TonalBand.LOW, biologic_signature=False
        ),
        vehicle="AUV survey",
        operator="Harbor Survey Group",
        lane_spacing_m=50.0,
    ),
    JobType.HULL_CLEANING: JobSpec(
        zones=("fuel_berths", "pier_400_approach"),
        hours=(1.0, 2.0),
        behavior=BehaviorType.STATIONARY,
        speed_mps=None,
        depth_m=3.0,
        acoustic=AcousticSpec(
            propeller_tonal=False, tonal_band=TonalBand.NONE, biologic_signature=True
        ),
        vehicle="Divers, hull cleaning",
        operator="Harbor Hull Care",
        jitter_mps=0.2,
    ),
    JobType.PILE_INSPECTION: JobSpec(
        zones=("vincent_thomas_bridge",),
        hours=(1.0, 3.0),
        behavior=BehaviorType.ORBIT,
        speed_mps=0.3,
        depth_m=6.0,
        acoustic=AcousticSpec(
            propeller_tonal=True, tonal_band=TonalBand.HIGH, biologic_signature=False
        ),
        vehicle="ROV pile inspection",
        operator="Bridge Structures Survey",
        radius_m=15.0,
        pilings=("vtb_piling_west", "vtb_piling_east"),
    ),
    JobType.DREDGE: JobSpec(
        zones=("main_channel",),
        hours=(4.0, 6.0),
        behavior=BehaviorType.LAWNMOWER,
        speed_mps=0.5,
        depth_m=10.0,
        acoustic=AcousticSpec(
            propeller_tonal=True, tonal_band=TonalBand.LOW, biologic_signature=False
        ),
        vehicle="Dredge survey sled",
        operator="Channel Maintenance",
        lane_spacing_m=30.0,
        clear_whole_path=True,
    ),
}


# ---------------------------------------------------------------------------
# Geometry helpers (local metres)
# ---------------------------------------------------------------------------


def _quantize_ticks(seconds: float, tick_s: float) -> int:
    q = math.floor(seconds / QUANTUM_S) * QUANTUM_S
    return int(round(q / tick_s))


def _nearest_node(harbor: Harbor, x: float, y: float) -> tuple[SensorNode, float]:
    best: tuple[float, str, SensorNode] | None = None
    for n in sorted(harbor.sensor_nodes, key=lambda n: n.node_id):
        nx, ny = to_local_m(n.lat, n.lon)
        d = math.hypot(nx - x, ny - y)
        if best is None or (round6(d), n.node_id) < (round6(best[0]), best[1]):
            best = (d, n.node_id, n)
    assert best is not None
    return best[2], best[0]


def point_segment_m(
    p: tuple[float, float], a: tuple[float, float], b: tuple[float, float]
) -> float:
    """Distance from ``p`` to the segment ``a``-``b`` in local metres."""
    dx, dy = b[0] - a[0], b[1] - a[1]
    n2 = dx * dx + dy * dy
    t = 0.0 if n2 == 0.0 else max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / n2))
    return math.hypot(p[0] - (a[0] + t * dx), p[1] - (a[1] + t * dy))


def segment_segment_m(
    a: tuple[float, float], b: tuple[float, float], c: tuple[float, float], d: tuple[float, float]
) -> float:
    """Minimum distance between two segments (0 if they cross)."""

    def orient(p: tuple[float, float], q: tuple[float, float], r: tuple[float, float]) -> float:
        return (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])

    o1, o2, o3, o4 = orient(a, b, c), orient(a, b, d), orient(c, d, a), orient(c, d, b)
    if (o1 > 0) != (o2 > 0) and (o3 > 0) != (o4 > 0) and 0 not in (o1, o2, o3, o4):
        return 0.0
    return min(
        point_segment_m(a, c, d),
        point_segment_m(b, c, d),
        point_segment_m(c, a, b),
        point_segment_m(d, a, b),
    )


def rogue_segment(rogue: ScenarioContact, harbor: Harbor) -> tuple[Any, Any]:
    """The rogue's straight-line path in local metres: start to its transit target."""
    start = to_local_m(rogue.start.lat, rogue.start.lon)
    b = rogue.behavior
    if b.target_asset is not None:
        a = harbor.asset(b.target_asset)
        end = to_local_m(a.lat, a.lon)
    else:
        assert b.waypoint is not None
        end = to_local_m(b.waypoint.lat, b.waypoint.lon)
    return start, end


def job_clearance_m(
    contact: ScenarioContact, spec: JobSpec, harbor: Harbor, seg: tuple[Any, Any]
) -> float:
    """How far the job keeps from the rogue's path (start, anchor, and lawnmower legs)."""
    x0, y0 = to_local_m(contact.start.lat, contact.start.lon)
    d = point_segment_m((x0, y0), *seg)
    b = contact.behavior
    if b.type is BehaviorType.ORBIT and b.target_asset is not None:
        a = harbor.asset(b.target_asset)
        center = to_local_m(a.lat, a.lon)
        d = min(d, point_segment_m(center, *seg) - (b.radius_m or 0.0))
    if spec.clear_whole_path and b.type is BehaviorType.LAWNMOWER and b.area is not None:
        mower = _survey(harbor, b.area, x0, y0, b.speed_mps or 1.0, b.lane_spacing_m or 50.0)
        for p, q in zip(mower.waypoints, mower.waypoints[1:], strict=False):
            d = min(d, segment_segment_m(p, q, *seg))
    return d


# ---------------------------------------------------------------------------
# The generator
# ---------------------------------------------------------------------------


def _read_s05_rogue() -> ScenarioContact:
    s05 = load_scenario(S05_PATH)
    return s05.contacts[0]


def _job_contact(
    job_id: str,
    jt: JobType,
    spec: JobSpec,
    x: float,
    y: float,
    zone: str,
    *,
    harbor: Harbor,
    plan_id: str,
    modem: str,
    start_tick: int,
    end_tick: int,
) -> ScenarioContact:
    lat, lon = from_local_m(x, y)
    target: str | None = None
    if spec.pilings:

        def dist(asset_id: str) -> tuple[float, str]:
            ax, ay = to_local_m(*_asset_ll(harbor, asset_id))
            return round6(math.hypot(ax - x, ay - y)), asset_id

        target = min(spec.pilings, key=dist)
    behavior = BehaviorSpec(
        type=spec.behavior,
        area=zone if spec.behavior is BehaviorType.LAWNMOWER else None,
        target_asset=target,
        speed_mps=spec.speed_mps,
        lane_spacing_m=spec.lane_spacing_m,
        radius_m=spec.radius_m,
        jitter_mps=spec.jitter_mps,
    )
    return ScenarioContact(
        id=job_id,
        start=StartPoint(lat=round6(lat), lon=round6(lon), depth_m=spec.depth_m),
        behavior=behavior,
        acoustic=spec.acoustic,
        registry=ContactRegistrySpec(
            filed=True, plan_id=plan_id, modem_id=modem, replies_to_hail=True
        ),
        start_tick=start_tick,
        end_tick=end_tick,
    )


def _asset_ll(harbor: Harbor, asset_id: str) -> tuple[float, float]:
    a = harbor.asset(asset_id)
    return a.lat, a.lon


def _place_job(
    rng: np.random.Generator,
    jt: JobType,
    spec: JobSpec,
    harbor: Harbor,
    seg: tuple[Any, Any],
    make: Any,
) -> tuple[ScenarioContact, str]:
    """Rejection-sample a start: inside the zone, heard, and clear of the rogue's path."""
    for _ in range(MAX_TRIES):
        zone_id = spec.zones[int(rng.integers(len(spec.zones)))]
        zone = harbor.zone(zone_id)
        pts = [to_local_m(lat, lon) for lat, lon in zone.polygon]
        x = float(rng.uniform(min(p[0] for p in pts), max(p[0] for p in pts)))
        y = float(rng.uniform(min(p[1] for p in pts), max(p[1] for p in pts)))
        lat, lon = from_local_m(x, y)
        lat, lon = round6(lat), round6(lon)
        if not zone.contains(lat, lon):
            continue
        x, y = to_local_m(lat, lon)
        node, d_node = _nearest_node(harbor, x, y)
        if d_node > HEARD_FRACTION * node.range_m:
            continue
        contact: ScenarioContact = make(x, y, zone_id)
        if job_clearance_m(contact, spec, harbor, seg) < ROGUE_CLEARANCE_M:
            continue
        return contact, zone_id
    raise RuntimeError(f"no feasible start for job type {jt.value} after {MAX_TRIES} tries")


def peak_tick(intervals: Sequence[tuple[int, int]], n_ticks: int) -> tuple[int, int]:
    """(earliest tick of peak concurrency, peak count) over ``[start, end)`` intervals."""
    delta = [0] * (n_ticks + 1)
    for s, e in intervals:
        delta[s] += 1
        delta[min(e, n_ticks)] -= 1
    best_t, best_n, cur = 0, -1, 0
    for t in range(n_ticks):
        cur += delta[t]
        if cur > best_n:
            best_t, best_n = t, cur
    return best_t, best_n


def generate_busy_day(
    *,
    seed: int,
    jobs: int = 40,
    vessels: int = 3,
    no_shows: int = 1,
    rogue: bool = True,
    hours: float = 8,
    tick_s: float = 5.0,
    hour_of_day: int = 7,
    scenario_id: str = "BT-01",
    harbor: Harbor | None = None,
) -> Scenario:
    """A busy working day as a ``Scenario`` (pure: same arguments, same scenario)."""
    if not 0 <= no_shows <= jobs:
        raise ValueError("no_shows must be in [0, jobs]")
    h = harbor if harbor is not None else load_harbor()
    rng = np.random.default_rng(seed)
    n_ticks = int(round(hours * 3600.0 / tick_s))
    pad = int(round(WINDOW_PAD_S / tick_s))
    s05 = _read_s05_rogue()
    seg = rogue_segment(s05, h)

    # 1. Job-type sequence: JOB_TYPES cycled, then a seeded shuffle.
    types = [JOB_TYPES[i % len(JOB_TYPES)] for i in range(jobs)]
    order = rng.permutation(jobs)
    types = [types[int(i)] for i in order]

    plans: list[RegistryPlan] = []
    contacts: list[ScenarioContact] = []
    intervals: list[tuple[int, int]] = []
    per_type_count: dict[JobType, int] = {}
    for k, jt in enumerate(types, start=1):
        spec = JOB_SPECS[jt]
        per_type_count[jt] = per_type_count.get(jt, 0) + 1
        # 2. Start, duration, position (in that order).
        start_s = float(rng.uniform(0.0, START_MAX_S))
        dur_s = float(rng.uniform(spec.hours[0] * 3600.0, spec.hours[1] * 3600.0))
        dur_t = max(_quantize_ticks(dur_s, tick_s), 1)
        start_t = min(_quantize_ticks(start_s, tick_s), n_ticks - dur_t)
        start_t = max(start_t, 0)
        end_t = min(start_t + dur_t, n_ticks)
        plan_id = f"P-BT{scenario_id[-2:]}-{k:03d}"
        modem = f"MOD-7{k:03d}"
        job_id = f"job-{k:02d}"

        make = partial(
            _job_contact,
            job_id,
            jt,
            spec,
            harbor=h,
            plan_id=plan_id,
            modem=modem,
            start_tick=start_t,
            end_tick=end_t,
        )
        contact, zone_id = _place_job(rng, jt, spec, h, seg, make)
        n_type = per_type_count[jt]
        plans.append(
            RegistryPlan(
                plan_id=plan_id,
                operator=f"{spec.operator} {n_type:02d} (demo)",
                vehicle=spec.vehicle,
                modem_id=modem,
                area=zone_id,
                window=PlanWindow(
                    start_tick=max(start_t - pad, 0), end_tick=min(end_t + pad, n_ticks)
                ),
                contact=f"sim-phone-{jt.value}-{n_type:02d}",
                job_type=jt,
            )
        )
        if k <= jobs - no_shows:
            contacts.append(contact)
            intervals.append((start_t, end_t))

    # 3. Vessels: staggered across the day, seeded direction and speed.
    span = max(n_ticks * tick_s - 1800.0, 600.0)
    for i in range(vessels):
        inbound = bool(rng.integers(2))
        speed = round(float(rng.uniform(*VESSEL_SPEED_RANGE_MPS)), 1)
        frac = float(rng.uniform(0.2, 0.8))
        if i == 0:
            a, b = CHANNEL_SOUTH, WEST_BASIN  # through inner_harbor_west
        else:
            a, b = (CHANNEL_SOUTH, CHANNEL_NORTH) if inbound else (CHANNEL_NORTH, CHANNEL_SOUTH)
        start_t = _quantize_ticks((i + frac) * span / max(vessels, 1), tick_s)
        ax, ay = to_local_m(*a)
        bx, by = to_local_m(*b)
        travel_t = int(math.ceil(math.hypot(bx - ax, by - ay) / speed / tick_s)) + 2
        start_t = min(start_t, n_ticks - travel_t - 1)
        contacts.append(
            ScenarioContact(
                id=f"ais-{i + 1:02d}",
                start=StartPoint(lat=a[0], lon=a[1], depth_m=0.0),
                behavior=BehaviorSpec(
                    type=BehaviorType.VESSEL_TRANSIT,
                    waypoint=Waypoint(lat=b[0], lon=b[1]),
                    speed_mps=speed,
                ),
                acoustic=AcousticSpec(
                    propeller_tonal=True, tonal_band=TonalBand.LOW, biologic_signature=False
                ),
                registry=ContactRegistrySpec(filed=False, replies_to_hail=False),
                start_tick=start_t,
                end_tick=min(start_t + travel_t, n_ticks),
                hypothesis=Hypothesis.SURFACE_VESSEL_AIS_OFF,
            )
        )

    expected_contacts = [
        ExpectedContact(id=c.id, terminal_state=LadderState.CLEARED_AT_ASK, max_interruptions=0)
        for c in contacts
        if c.id.startswith("job-")
    ]
    if rogue:
        t_peak, _ = peak_tick(intervals, n_ticks)
        contacts.append(
            s05.model_copy(
                update={
                    "id": "rogue-1",
                    "start_tick": t_peak,
                    "hypothesis": Hypothesis.UNAUTHORIZED_UUV,
                }
            )
        )
        expected_contacts.append(
            ExpectedContact(id="rogue-1", terminal_state=LadderState.HANDED_OFF)
        )
    contacts.sort(key=lambda c: c.id)
    return Scenario(
        id=scenario_id,
        name="Busy Tuesday",
        seed=seed,
        duration_ticks=n_ticks,
        tick_s=tick_s,
        ground_truth=Hypothesis.FILED_ON_PLAN,
        hour_of_day=hour_of_day,
        registry_plans=tuple(plans),
        contacts=tuple(contacts),
        expected=Expected(
            terminal_state=LadderState.HANDED_OFF if rogue else LadderState.CLEARED_AT_ASK,
            contacts=tuple(expected_contacts),
        ),
        notes=(
            f"Generated busy working day: {jobs} filed plans ({no_shows} no-show), "
            f"{vessels} AIS-reporting vessel transits, "
            + ("one unfiled contact on S05's definition at peak load." if rogue else "no rogue.")
        ),
    )


# ---------------------------------------------------------------------------
# YAML and CLI
# ---------------------------------------------------------------------------


def generator_invocation(
    *, jobs: int, vessels: int, seed: int, out: str, no_shows: int, hours: float, tick_s: float
) -> str:
    parts = [
        "python -m soundings.sim.generate",
        f"--jobs {jobs}",
        f"--vessels {vessels}",
        f"--seed {seed}",
        f"--out {out}",
    ]
    if no_shows != 1:
        parts.append(f"--no-shows {no_shows}")
    if hours != 8:
        parts.append(f"--hours {hours:g}")
    if tick_s != 5.0:
        parts.append(f"--tick-s {tick_s:g}")
    return " ".join(parts)


def scenario_yaml(scenario: Scenario, command: str | None = None) -> str:
    """Stable YAML for ``scenario`` (field order, defaults omitted) with a header comment."""
    data = scenario.model_dump(mode="json", exclude_defaults=True)
    body = yaml.safe_dump(data, sort_keys=False, width=100, allow_unicode=False)
    header = (
        f"# Generated by: {command or 'soundings.sim.generate.generate_busy_day'}\n"
        "# Do not edit by hand: regenerate, and verify with --check.\n"
    )
    return header + body


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m soundings.sim.generate", description="Write a Busy Tuesday scenario."
    )
    parser.add_argument("--jobs", type=int, default=40)
    parser.add_argument("--vessels", type=int, default=3)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--out", default=DEFAULT_OUT)
    parser.add_argument("--no-shows", type=int, default=1)
    parser.add_argument("--hours", type=float, default=8.0)
    parser.add_argument("--tick-s", type=float, default=5.0)
    parser.add_argument("--check", action="store_true", help="diff against --out; exit 1")
    args = parser.parse_args(argv)
    scenario = generate_busy_day(
        seed=args.seed,
        jobs=args.jobs,
        vessels=args.vessels,
        no_shows=args.no_shows,
        hours=args.hours,
        tick_s=args.tick_s,
        scenario_id=Path(args.out).stem if Path(args.out).stem.startswith("BT-") else "BT-01",
    )
    cmd = generator_invocation(
        jobs=args.jobs,
        vessels=args.vessels,
        seed=args.seed,
        out=args.out,
        no_shows=args.no_shows,
        hours=args.hours,
        tick_s=args.tick_s,
    )
    text = scenario_yaml(scenario, cmd)
    out = Path(args.out)
    if args.check:
        current = out.read_text(encoding="utf-8") if out.is_file() else ""
        if current != text:
            print(f"{out}: differs from a fresh generation", file=sys.stderr)
            return 1
        print(f"{out}: up to date")
        return 0
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
