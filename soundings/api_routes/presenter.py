"""Presenter mode routes (WO-062P): the shot list as JSON and the in-memory presenter log.

``GET /api/demo-script`` serves ``docs/demo-script.yaml``, validated, as JSON.
``POST /api/presenter-log`` records that presenter mode landed on a step (``step``),
resumed after a ``pause_for_click`` (``resume``) or went back to step 0 (``reset``); the
server stamps it and copies the caption from the script. ``GET`` returns the entries,
``DELETE`` clears them. CLAUDE.md 2.2: nothing here names a rung, a state, a threshold or
a scope; presenting a step never steers a run.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from soundings.demo import presenter
from soundings.demo.presenter import PresenterEventKind, get_presenter_log, script_payload
from soundings.demo.script import DemoScript

router = APIRouter(tags=["presenter"])


class PresenterLogBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    index: Annotated[int, Field(ge=0)]
    kind: PresenterEventKind = PresenterEventKind.STEP


def _script() -> DemoScript:
    try:
        return presenter.load_script(presenter.DEFAULT_SCRIPT)
    except (OSError, ValueError, ValidationError) as exc:
        raise HTTPException(status_code=500, detail=f"demo script invalid: {exc}") from exc


@router.get("/api/demo-script")
def demo_script() -> dict[str, Any]:
    return script_payload(_script())


@router.get("/api/presenter-log")
def presenter_log() -> list[dict[str, Any]]:
    return [e.as_json() for e in get_presenter_log().entries()]


@router.post("/api/presenter-log")
def presenter_log_append(body: PresenterLogBody) -> dict[str, Any]:
    script = _script()
    if body.index >= len(script.steps):
        raise HTTPException(status_code=404, detail=f"no step {body.index}")
    entry = get_presenter_log().record(body.kind, body.index, script.steps[body.index])
    return entry.as_json()


@router.delete("/api/presenter-log")
def presenter_log_clear() -> dict[str, bool]:
    get_presenter_log().clear()
    return {"ok": True}
