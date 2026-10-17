"""Runs: start a catalog scenario, list held runs, stream a run over a WebSocket (WO-060).

CLAUDE.md 2.2: no request model here has a field that names a rung, a state, a threshold
or a scope; a run is started and watched, never steered. ``tests/test_api_runs.py`` walks
the routes and the OpenAPI schema to hold that line.
"""

from __future__ import annotations

import re
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, ConfigDict, Field

from soundings.schemas.enums import ToggleName
from soundings.sim.scenario import UnknownScenarioError
from soundings.stream.engine import UnknownRun, UnsupportedOption, get_engine
from soundings.stream.events import ErrorEvent, ErrorPayload, event_json

router = APIRouter(tags=["runs"])

CATALOG_ID = re.compile(r"^S\d{2}$")


class StartRunBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scenario: str
    seed: int | None = None
    speed: Annotated[float, Field(ge=0.0)] | None = None
    toggles: list[ToggleName] = Field(default_factory=list)
    use_llm: bool = False
    source: str = "batch"


@router.post("/api/runs")
def start_run(body: StartRunBody) -> dict[str, Any]:
    if not CATALOG_ID.match(body.scenario):
        raise HTTPException(status_code=400, detail="scenario must be a catalog id like S02")
    try:
        handle = get_engine().start(
            body.scenario,
            body.seed,
            speed=body.speed,
            toggles=body.toggles,
            use_llm=body.use_llm,
            source=body.source,
        )
    except UnknownScenarioError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except UnsupportedOption as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return handle.summary()


@router.get("/api/runs")
def list_runs() -> list[dict[str, object]]:
    return [h.summary() for h in get_engine().list()]


@router.websocket("/ws/runs/{run_id}")
async def run_socket(websocket: WebSocket, run_id: str, after: int = Query(-1)) -> None:
    await websocket.accept()
    engine = get_engine()
    try:
        engine.get(run_id)
    except UnknownRun:
        err = ErrorEvent(
            run_id=run_id, seq=0, tick=0, t_s=0.0, payload=ErrorPayload(message="unknown run")
        )
        await websocket.send_text(event_json(err))
        await websocket.close(code=4404)
        return
    try:
        async for event in engine.subscribe(run_id, after):
            await websocket.send_text(event_json(event))
    except WebSocketDisconnect:
        return
    await websocket.close()
