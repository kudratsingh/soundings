"""The watch officer's approval (WO-061): POST /api/runs/{run_id}/approve and /decline.

The body names the request and who is answering; it can never carry a rung, a scope or
an action (``extra="forbid"``, 422). The server mints a WO-041 token for exactly the open
request's scope (``soundings.stream.ladder_adapter.grant``, approver ``primary``, channel
``ui``) and hands it to the run through ``RunEngine.deliver_approval``. The ladder's guard
checks it on the next tick and decides (CLAUDE.md 2.2): the button only asks.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict

from soundings.ladder.approval import token_hash
from soundings.stream.engine import NoPendingApproval, UnknownRun, get_engine
from soundings.stream.ladder_adapter import UI_APPROVER, RequestExpired, grant
from soundings.stream.stepped import AlreadyAnswered, SteppedSource, stepped_of
from soundings.stream.v3 import NotManual, TokenRefused

router = APIRouter(tags=["approve"])


class ApproveBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: str
    approver: str = "Watch Officer"


def _source(run_id: str) -> SteppedSource:
    try:
        src = get_engine().get(run_id).source
    except UnknownRun as exc:
        raise HTTPException(status_code=404, detail=f"unknown run {run_id!r}") from exc
    try:
        stepped = stepped_of(src)
    except NoPendingApproval as exc:  # still starting
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if stepped is None:
        raise HTTPException(
            status_code=409,
            detail=f"run {run_id!r} ({src.name}) has no person on the approval path",
        )
    return stepped


@router.post("/api/runs/{run_id}/approve")
def approve(run_id: str, body: ApproveBody) -> dict[str, Any]:
    src = _source(run_id)
    try:
        request = src.open_request(body.request_id)
        token = grant(request, UI_APPROVER, src.now_s())
        contact_id = get_engine().deliver_approval(run_id, token.model_dump(mode="json"))
    except (NoPendingApproval, AlreadyAnswered, NotManual, RequestExpired) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except TokenRefused as exc:  # pragma: no cover - grant() always matches the scope
        raise HTTPException(status_code=409, detail=f"token refused: {exc}") from exc
    src.mark_answered(body.request_id)
    return {
        "run_id": run_id,
        "request_id": body.request_id,
        "contact_id": contact_id,
        "approver": body.approver,
        "token_hash": token_hash(token),
        "status": "delivered; the ladder's guard decides on the next tick",
    }


@router.post("/api/runs/{run_id}/decline")
def decline(run_id: str, body: ApproveBody) -> dict[str, Any]:
    src = _source(run_id)
    try:
        result = src.decline(body.request_id, body.approver)
    except (NoPendingApproval, AlreadyAnswered, KeyError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {
        "run_id": run_id,
        "request_id": body.request_id,
        "outcome": result.payload.outcome,
        "status": "delivered; the ladder takes it on the next tick",
    }


__all__ = ["ApproveBody", "router"]
