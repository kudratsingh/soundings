"""Judge injection (WO-061): POST /api/inject places one contact and starts its run.

The body is an ``InjectRequest`` (a position, a behavior, an acoustic profile, an optional
seed; ``extra="forbid"``, so a rung, a state, an ``expected`` block or a path is a 422).
The server writes ``scenarios/live/L-<stamp>.yaml`` (``$SOUNDINGS_LIVE_DIR``; gitignored),
loads it with the catalog's loader and starts it on the ``stepped`` source. A judge can
describe a contact; the ladder decides what happens to it (CLAUDE.md 2.2).

The 2 s follow: runner v3 precomputes the sim and the investigation before its first tick,
which takes longer than 2 s on a laptop. While the judge is still choosing,
``POST /api/inject/prepare`` (same body) builds that run in the background; Go with the
same request then claims it, writes its file and restarts its pacing clock, so the screen
shows the contact at once. Without a matching prepare, Go starts a fresh run. Either way
the stream is the same seeded run (CLAUDE.md 2.4); only when it is released differs.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
from typing import Any

from fastapi import APIRouter, HTTPException

from soundings.canonical import canonical_json
from soundings.inject import (
    InjectRequest,
    OutsideZonesError,
    build_live_scenario,
    live_dir,
    live_seed,
    load_known_ops,
    write_live_scenario,
)
from soundings.schemas.harbor import Harbor, load_harbor
from soundings.schemas.scenario import Scenario, load_scenario
from soundings.stream.engine import RunEngine, RunHandle, UnknownRun, get_engine

router = APIRouter(tags=["inject"])

OUTSIDE_DETAIL = "outside the declared security zones"
SOURCE = "stepped"
PREPARE_TTL_S = 120.0


@dataclass
class _Prepared:
    key: str
    scenario: Scenario
    seed: int
    run_id: str
    made_at: float


_LOCK = threading.Lock()
_PREPARED: dict[str, _Prepared] = {}


@lru_cache(maxsize=1)
def _harbor() -> Harbor:
    return load_harbor()


def _now() -> datetime:
    """The wall clock names the file only; the run itself is seeded (CLAUDE.md 2.4)."""
    return datetime.now().astimezone()


def _key(body: InjectRequest) -> str:
    return canonical_json(body.model_dump(mode="json"))


def _scenario(body: InjectRequest) -> Scenario:
    try:
        return build_live_scenario(body, _now(), _harbor(), load_known_ops())
    except OutsideZonesError as exc:
        raise HTTPException(status_code=422, detail=OUTSIDE_DETAIL) from exc


def _drop(engine: RunEngine, prepared: _Prepared) -> None:
    engine._runs.pop(prepared.run_id, None)


def _check_capacity(engine: RunEngine) -> None:
    unclaimed = {p.run_id for p in _PREPARED.values()}
    active = [h for h in engine.list() if h.run_id not in unclaimed and not h.finished]
    if len(active) >= engine.max_runs:
        raise HTTPException(status_code=429, detail="too many live runs")


def _restart_pacing(engine: RunEngine, handle: RunHandle) -> None:
    """Pacing starts now: the released prefix is recomputed against a fresh clock."""
    handle.clock = engine.clock_factory()
    handle._due = []
    handle._awaiting = set()
    handle._anchor = (0.0, 0.0, handle.speed)
    handle._extend_due()


@router.post("/api/inject/prepare")
def prepare(body: InjectRequest) -> dict[str, Any]:
    """Build the run for this request in the background while the judge decides."""
    engine = get_engine()
    scenario = _scenario(body)
    key = _key(body)
    with _LOCK:
        if key in _PREPARED and time.monotonic() - _PREPARED[key].made_at < PREPARE_TTL_S:
            return {"prepared": True, "scenario_id": _PREPARED[key].scenario.id}
        for old in list(_PREPARED.values()):
            _drop(engine, old)
        _PREPARED.clear()
        _check_capacity(engine)
        seed = live_seed(body)
        handle = engine.start(scenario, seed, speed=None, source=SOURCE)
        _PREPARED[key] = _Prepared(key, scenario, seed, handle.run_id, time.monotonic())
    return {"prepared": True, "scenario_id": scenario.id}


def _claim(engine: RunEngine, body: InjectRequest) -> tuple[_Prepared, RunHandle] | None:
    with _LOCK:
        prepared = _PREPARED.pop(_key(body), None)
    if prepared is None:
        return None
    try:
        handle = engine.get(prepared.run_id)
    except UnknownRun:
        return None
    if time.monotonic() - prepared.made_at >= PREPARE_TTL_S:
        _drop(engine, prepared)
        return None
    return prepared, handle


@router.post("/api/inject")
def inject(body: InjectRequest) -> dict[str, Any]:
    engine = get_engine()
    claimed = _claim(engine, body)
    if claimed is not None:
        prepared, handle = claimed
        path = write_live_scenario(prepared.scenario, live_dir())
        loaded = load_scenario(path, _harbor())  # the file, as the catalog loader reads it
        if loaded == prepared.scenario:
            _restart_pacing(engine, handle)
            return {
                "run_id": handle.run_id,
                "scenario_id": loaded.id,
                "path": str(path),
                "seed": prepared.seed,
            }
        _drop(engine, prepared)  # the name moved (a collision): start this file afresh
    else:
        _check_capacity(engine)
        path = write_live_scenario(_scenario(body), live_dir())
        loaded = load_scenario(path, _harbor())
    seed = live_seed(body)
    handle = engine.start(loaded, seed, speed=None, source=SOURCE)
    return {"run_id": handle.run_id, "scenario_id": loaded.id, "path": str(path), "seed": seed}


__all__ = ["PREPARE_TTL_S", "router"]
