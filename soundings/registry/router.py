"""REST surface for the registry, mounted under ``/api/plans`` by ``soundings.api``.

There is no DELETE route: expiry is the only way a plan leaves the active set, and the
30-day retention purge is the only real delete. A rejected plan returns 422 with
``{"reason": <RejectReason>, "detail": ...}``; malformed JSON returns FastAPI's usual 422.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from pydantic import AwareDatetime, BaseModel, ValidationError

from soundings.registry.models import (
    IN_FORCE,
    ConflictView,
    PlanRejected,
    PlanStatus,
    PlanView,
    RejectReason,
)
from soundings.registry.slots import SlotProposal  # WO-082
from soundings.registry.store import (
    ProposalNotAcceptable,  # WO-082
    ProposalStale,  # WO-082
    RegistryStore,
    StoredProposal,  # WO-082
    db_path_from_env,
)

STATIC_DIR: Path = Path(__file__).resolve().parent / "static"
REGISTRY_HTML: Path = STATIC_DIR / "registry.html"

router = APIRouter(tags=["registry"])

_STORES: dict[str, RegistryStore] = {}


def get_store() -> RegistryStore:
    """The store at ``SOUNDINGS_DB``, read at request time (tests override this dependency)."""
    key = str(db_path_from_env())
    store = _STORES.get(key)
    if store is None:
        store = RegistryStore(key)
        _STORES[key] = store
    return store


StoreDep = Annotated[RegistryStore, Depends(get_store)]


class ZoneOut(BaseModel):
    zone_id: str
    name: str
    criticality: float


def _rejected(exc: PlanRejected) -> JSONResponse:
    return JSONResponse(status_code=422, content={"reason": exc.reason.value, "detail": exc.detail})


@router.post("", status_code=201, response_model=PlanView)
def create_plan(
    store: StoreDep,
    body: Annotated[dict[str, Any], Body()],
    epoch: Annotated[AwareDatetime | None, Query()] = None,
    tick_s: Annotated[float | None, Query(gt=0)] = None,
) -> Any:
    """File a plan. ``epoch`` and ``tick_s`` (optional) bridge a tick-only window to time."""
    try:
        return store.create(body, epoch=epoch, tick_s=tick_s)
    except PlanRejected as exc:
        return _rejected(exc)
    except ValidationError as exc:
        if any("denylisted" in str(err.get("msg", "")) for err in exc.errors()):
            return _rejected(
                PlanRejected(RejectReason.DENYLISTED_TEXT, "free text holds a denylisted term")
            )
        errors = exc.errors(include_url=False, include_context=False, include_input=False)
        return JSONResponse(status_code=422, content={"detail": errors})


# --- WO-081 begin: conflicts (status=conflicted; active means in force) ---
@router.get("", response_model=list[PlanView])
def list_plans(
    store: StoreDep,
    status: Literal["active", "conflicted", "expired", "all"] = "active",
    start_tick: Annotated[int | None, Query(ge=0)] = None,
    end_tick: Annotated[int | None, Query(ge=0)] = None,
    start_at: AwareDatetime | None = None,
    end_at: AwareDatetime | None = None,
) -> Any:
    """Plans with ``status`` (default active, meaning in force: active or conflicted;
    ``conflicted`` for conflicted plans only; ``all`` for every plan), optionally limited
    to those overlapping a tick range or a datetime range (not both). Every plan carries
    its ``conflicts`` array."""
    tick_range = (start_tick, end_tick)
    time_range = (start_at, end_at)
    for name, pair in (("start_tick/end_tick", tick_range), ("start_at/end_at", time_range)):
        if (pair[0] is None) != (pair[1] is None):
            raise HTTPException(status_code=422, detail=f"give both ends of {name}")
    if start_tick is not None and start_at is not None:
        raise HTTPException(status_code=422, detail="give a tick range or a datetime range")
    include_expired = status in ("expired", "all")
    try:
        if start_tick is not None and end_tick is not None:
            plans = store.overlapping_ticks(start_tick, end_tick, include_expired=include_expired)
        elif start_at is not None and end_at is not None:
            plans = store.overlapping_times(start_at, end_at, include_expired=include_expired)
        else:
            plans = store.list(None if status == "all" else PlanStatus(status))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if status == "active":
        plans = [p for p in plans if p.status in IN_FORCE]
    elif status in ("conflicted", "expired"):
        plans = [p for p in plans if p.status == PlanStatus(status)]
    return plans


@router.get("/{plan_id}/conflicts", response_model=list[ConflictView])
def plan_conflicts(store: StoreDep, plan_id: str) -> list[ConflictView]:
    """The plan's conflicts alone, ordered by ``(other start_at, other_plan_id)``."""
    if store.get(plan_id) is None:
        raise HTTPException(status_code=404, detail=f"no plan {plan_id!r}")
    return store.conflicts_of(plan_id)


# --- WO-081 end ---


# --- WO-082: slot proposals ---
# A proposal changes nothing until an operator accepts it here; nothing is sent to any
# vehicle (the operator tells their own crew). Accept is the only caller of
# ``RegistryStore.accept_proposal`` (tests/test_slots.py::test_proposal_is_not_control).


class ProposalChoice(BaseModel):
    proposal_id: str


def _reason(status: int, reason: str, detail: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"reason": reason, "detail": detail})


def _own_proposal(store: RegistryStore, plan_id: str, proposal_id: str) -> StoredProposal:
    stored = store.get_proposal(proposal_id)
    if stored is None or stored.proposal.target_plan_id != plan_id:
        raise HTTPException(status_code=404, detail=f"no proposal {proposal_id!r} for {plan_id!r}")
    return stored


@router.post("/{plan_id}/proposal", response_model=SlotProposal)
def propose_slot(
    store: StoreDep, plan_id: str, now: Annotated[AwareDatetime | None, Query()] = None
) -> Any:
    """Compute and store a pending slot proposal for a conflicted plan (changes no plan)."""
    plan = store.get(plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail=f"no plan {plan_id!r}")
    if plan.status is not PlanStatus.CONFLICTED:
        return _reason(409, "not_conflicted", f"{plan_id} has no open conflict")
    proposal = store.propose(plan_id, now=now)
    return store.save_proposal(proposal).proposal


@router.get("/{plan_id}/proposal", response_model=SlotProposal)
def latest_proposal(store: StoreDep, plan_id: str) -> Any:
    """The newest pending proposal for the plan, or 404."""
    stored = store.latest_pending_proposal(plan_id)
    if stored is None:
        raise HTTPException(status_code=404, detail=f"no pending proposal for {plan_id!r}")
    return stored.proposal


@router.post("/{plan_id}/proposal/accept")
def accept_slot(store: StoreDep, plan_id: str, body: ProposalChoice) -> Any:
    """An operator accepts: the plans move and the conflict is marked resolved."""
    _own_proposal(store, plan_id, body.proposal_id)
    try:
        stored, plans = store.accept_proposal(body.proposal_id)
    except ProposalStale:
        return _reason(409, "stale_proposal", "the plans changed; compute a new proposal")
    except ProposalNotAcceptable as exc:
        return _reason(409 if exc.reason == "not_pending" else 422, exc.reason, exc.detail)
    return {
        "proposal": stored.proposal.model_dump(mode="json"),
        "status": stored.status.value,
        "plans": [p.model_dump(mode="json") for p in plans],
    }


@router.post("/{plan_id}/proposal/decline")
def decline_slot(store: StoreDep, plan_id: str, body: ProposalChoice) -> Any:
    """An operator declines: only the proposal's status changes."""
    _own_proposal(store, plan_id, body.proposal_id)
    try:
        stored = store.decline_proposal(body.proposal_id)
    except ProposalNotAcceptable as exc:
        return _reason(409, exc.reason, exc.detail)
    return {"proposal": stored.proposal.model_dump(mode="json"), "status": stored.status.value}


# --- WO-082 end ---


@router.get("/zones", response_model=list[ZoneOut])
def list_zones(store: StoreDep) -> list[ZoneOut]:
    """The declared security zones a plan may name, sorted by ``zone_id``."""
    zones = sorted(store.harbor.zones, key=lambda z: z.zone_id)
    return [ZoneOut(zone_id=z.zone_id, name=z.name, criticality=z.criticality) for z in zones]


@router.get("/{plan_id}", response_model=PlanView)
def get_plan(store: StoreDep, plan_id: str) -> PlanView:
    plan = store.get(plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail=f"no plan {plan_id!r}")
    return plan


@router.post("/{plan_id}/expire", response_model=PlanView)
def expire_plan(store: StoreDep, plan_id: str) -> PlanView:
    try:
        return store.expire(plan_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"no plan {plan_id!r}") from exc


def registry_page() -> FileResponse:
    """The one-page filing form (plain HTML + vanilla JS, no build step)."""
    return FileResponse(REGISTRY_HTML, media_type="text/html")
