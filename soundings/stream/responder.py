"""The responder stream (WO-088 part C): one ``responder`` event per tick when a run has one.

The responder is the harbor's own small sensor carrier, a mobile probe at rung ask
(CLAUDE.md section 8 A2): it is dispatched to look, holds station at standoff (25 m) and
returns to dock. This module only turns snapshots the run already produced into events. It
never dispatches, positions or recalls anything (CLAUDE.md 2.2): the snapshots come from the
run (``RunResult.responder_trail`` when the runner carries one, or a registered provider),
so the stream is a replay like every other event.

A snapshot is anything with the ``ResponderSnapshot`` fields of the WO-088 contract
(``soundings.sim.responder``); the shape is structural so the stream does not depend on how
the sim builds it.
"""

from __future__ import annotations

import importlib
from collections.abc import Callable, Sequence
from enum import Enum
from functools import cache
from typing import Protocol, cast

from soundings.canonical import round6
from soundings.runner import RunResult
from soundings.schemas.scenario import Scenario
from soundings.stream.events import ResponderPayload, ResponderStateName, ResponderTrailPoint

TRAIL_POINTS = 60
"""How many trail positions each ``responder`` event carries (the UI draws them)."""

DEFAULT_RESPONDER_ID = "resp_1"
DEFAULT_BATTERY_CAPACITY_MIN = 90.0
"""Demo value; ``config/responder.yaml`` ``battery_min`` wins when the sim is present."""


@cache
def config_defaults() -> tuple[str, float]:
    """``(id, battery_min)`` from ``config/responder.yaml`` via the sim's loader, when the
    WO-088 sim is installed; the demo defaults above otherwise."""
    try:
        sim = importlib.import_module("soundings.sim.responder")
        cfg = sim.load_responder_config()
    except (ImportError, AttributeError, OSError, ValueError):
        return DEFAULT_RESPONDER_ID, DEFAULT_BATTERY_CAPACITY_MIN
    return str(getattr(cfg, "id", DEFAULT_RESPONDER_ID)), float(
        getattr(cfg, "battery_min", DEFAULT_BATTERY_CAPACITY_MIN)
    )


STATE_NAMES: tuple[ResponderStateName, ...] = ("DOCKED", "TRANSIT", "ON_STATION", "RETURNING")


class ResponderSnapshotLike(Protocol):
    """The fields of ``soundings.sim.responder.ResponderSnapshot`` the stream reads."""

    @property
    def tick(self) -> int: ...
    @property
    def lat(self) -> float: ...
    @property
    def lon(self) -> float: ...
    @property
    def heading_deg(self) -> float: ...
    @property
    def speed_mps(self) -> float: ...
    @property
    def battery_min(self) -> float: ...
    @property
    def state(self) -> object: ...
    @property
    def target_contact_id(self) -> str | None: ...
    @property
    def standoff_m(self) -> float: ...
    @property
    def distance_to_target_m(self) -> float | None: ...


ResponderProvider = Callable[[RunResult, Scenario], Sequence[ResponderSnapshotLike] | None]


def state_name(state: object) -> ResponderStateName:
    """``ResponderState.ON_STATION`` (or ``"on_station"``) -> ``"ON_STATION"``."""
    raw = state.name if isinstance(state, Enum) else str(state)
    name = raw.rsplit(".", 1)[-1].upper()
    if name not in STATE_NAMES:
        raise ValueError(f"unknown responder state {state!r}")
    return cast(ResponderStateName, name)


def trail_from_result(
    result: RunResult, scenario: Scenario
) -> Sequence[ResponderSnapshotLike] | None:
    """The trail the runner recorded, if it carries one (``responder_trail``)."""
    trail = getattr(result, "responder_trail", None)
    if not trail:
        return None
    return tuple(cast(Sequence[ResponderSnapshotLike], trail))


_PROVIDERS: list[ResponderProvider] = [trail_from_result]


def register_responder_provider(provider: ResponderProvider) -> None:
    """Add a snapshot provider (tried in registration order after the runner's own trail)."""
    if provider not in _PROVIDERS:
        _PROVIDERS.append(provider)


def unregister_responder_provider(provider: ResponderProvider) -> None:
    if provider in _PROVIDERS and provider is not trail_from_result:
        _PROVIDERS.remove(provider)


def responder_snapshots(
    result: RunResult, scenario: Scenario
) -> Sequence[ResponderSnapshotLike] | None:
    """The run's responder snapshots from the first provider that has any, else None."""
    for provider in _PROVIDERS:
        snaps = provider(result, scenario)
        if snaps:
            return snaps
    return None


# --- WO-088D begin ---
def real_responder_trail(scenario: Scenario, seed: int) -> tuple[ResponderSnapshotLike, ...]:
    """The harbor's real responder over this run: runner v2 with ``DEFAULT_RESPONDER``
    (``config/responder.yaml``), one snapshot per tick. Deterministic (same scenario and
    seed, same trail); the stream still only replays what the run produced."""
    from soundings.runner import DEFAULT_RESPONDER, ScenarioRunnerV2

    result = ScenarioRunnerV2(scenario, seed=seed).run(responder=DEFAULT_RESPONDER)
    return tuple(cast(Sequence[ResponderSnapshotLike], result.responder_trail))


# --- WO-088D end ---


def _round_opt(value: float | None) -> float | None:
    return None if value is None else round6(value)


def responder_payloads(
    snapshots: Sequence[ResponderSnapshotLike],
    *,
    responder_id: str | None = None,
    battery_capacity_min: float | None = None,
    trail_points: int = TRAIL_POINTS,
) -> dict[int, ResponderPayload]:
    """One payload per tick (the last snapshot of a tick wins), with the trailing window.

    ``dispatched`` is true on the first snapshot of each dispatch: a ``TRANSIT`` toward a
    target that comes after any other state (or opens the run). Pure and deterministic.
    """
    cfg_id, cfg_capacity = config_defaults()
    rid = cfg_id if responder_id is None else responder_id
    capacity = cfg_capacity if battery_capacity_min is None else battery_capacity_min
    ordered = sorted(snapshots, key=lambda s: s.tick)
    out: dict[int, ResponderPayload] = {}
    trail: list[ResponderTrailPoint] = []
    prev_state: ResponderStateName | None = None
    for snap in ordered:
        state = state_name(snap.state)
        point = ResponderTrailPoint(tick=snap.tick, lat=round6(snap.lat), lon=round6(snap.lon))
        if trail and trail[-1].tick == snap.tick:
            trail[-1] = point
        else:
            trail.append(point)
        del trail[:-trail_points]
        dispatched = (
            state == "TRANSIT" and snap.target_contact_id is not None and prev_state != "TRANSIT"
        )
        earlier = out.get(snap.tick)
        out[snap.tick] = ResponderPayload(
            id=rid,
            state=state,
            lat=round6(snap.lat),
            lon=round6(snap.lon),
            heading_deg=round6(snap.heading_deg % 360.0),
            speed_mps=round6(snap.speed_mps),
            battery_min=round6(snap.battery_min),
            battery_capacity_min=round6(capacity),
            standoff_m=round6(snap.standoff_m),
            target_contact_id=snap.target_contact_id,
            distance_to_target_m=_round_opt(snap.distance_to_target_m),
            dispatched=dispatched or (earlier is not None and earlier.dispatched),
            trail=list(trail),
        )
        prev_state = state
    return out


__all__ = [
    "DEFAULT_RESPONDER_ID",
    "TRAIL_POINTS",
    "config_defaults",
    "ResponderProvider",
    "ResponderSnapshotLike",
    "real_responder_trail",  # WO-088D
    "register_responder_provider",
    "responder_payloads",
    "responder_snapshots",
    "state_name",
    "trail_from_result",
    "unregister_responder_provider",
]
