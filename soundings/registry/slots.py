"""Slot proposals for conflicted plans (WO-082). Pure: no I/O beyond config, no clock.

A proposal is a set of start-time shifts, in minutes, that keeps a zone within its
capacity of concurrent plans at the least total delay. It is advice for a person: the
registry changes only when an operator accepts it in the form, and nothing is ever sent
to a vehicle (the operator tells their own crew).

The model (OR-Tools CP-SAT, imported lazily inside :func:`solve`):

- the plans covered are :func:`model_set`: every in-force plan whose footprint shares
  buffered area and depth with the target's zone and whose window meets the target's
  local day, sorted by ``(start_at, plan_id)``;
- plan ``i`` moves by an integer number of steps ``k_i``; its start stays inside its
  earliest/latest bounds (snapped to the step grid toward the filed start), never before
  ``now`` and never past the day's horizon; plans already in the water or without a
  conflict keep ``k_i = 0``;
- one ``AddCumulative`` (demand 1 per plan) caps concurrency at the zone's capacity;
- the objective is ``M * sum|k_i| + sum (n - rank_i) * |k_i|``: least total delay first,
  and among equal delays the later-filed plans move, so the optimum is unique.

Determinism (CLAUDE.md 2.4): variables and constraints are added in sorted plan order
only, the solver runs one worker with a fixed seed, and a deterministic-time limit
backs the wall limit. ``now`` is always a parameter.
"""

from __future__ import annotations

import enum
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator

from soundings.canonical import stable_hash
from soundings.registry.conflicts import footprint_of, zone_geometry
from soundings.registry.models import (
    IN_FORCE,
    REGISTRY_CONFIG_PATH,
    ConflictConfig,
    PlanStatus,
    PlanView,
    load_conflict_config,
)
from soundings.schemas.harbor import Harbor

__all__ = [
    "WITHIN_CAPACITY_DETAIL",
    "BindingConstraint",
    "PinReason",
    "PlanShift",
    "SlotConfig",
    "SlotProposal",
    "SlotSpec",
    "build_model",
    "build_specs",
    "diagnose",
    "load_slot_config",
    "make_solver",
    "model_set",
    "propose_slots",
    "solve",
]

WITHIN_CAPACITY_DETAIL = "within zone capacity; accept to confirm shared slot"
NO_SLOT = "no feasible slot"


# --- config ------------------------------------------------------------------------------


class SlotConfig(BaseModel):
    """The top-level ``slots:`` block of ``config/registry.yaml``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    zone_capacity_default: Annotated[int, Field(ge=1)] = 2
    zone_capacity: dict[str, Annotated[int, Field(ge=1)]] = Field(default_factory=dict)
    slot_step_min: Annotated[int, Field(ge=1, le=240)] = 15
    latest_start_slack_min: Annotated[int, Field(ge=0)] = 120
    horizon_end_local: str = "24:00"
    random_seed: int = 7
    max_time_in_seconds: Annotated[float, Field(gt=0)] = 2.0

    @field_validator("horizon_end_local")
    @classmethod
    def _hhmm(cls, v: str) -> str:
        _horizon_minutes(v)
        return v

    def capacity_for(self, zone_id: str) -> int:
        """``zone_capacity[zone_id]`` when set, else ``zone_capacity_default``."""
        return int(self.zone_capacity.get(zone_id, self.zone_capacity_default))

    @property
    def horizon_minutes(self) -> int:
        return _horizon_minutes(self.horizon_end_local)


def _horizon_minutes(text: str) -> int:
    parts = text.split(":")
    if len(parts) != 2 or not all(p.isdigit() and len(p) == 2 for p in parts):
        raise ValueError(f"horizon_end_local must be HH:MM, got {text!r}")
    minutes = int(parts[0]) * 60 + int(parts[1])
    if int(parts[1]) >= 60 or not 0 < minutes <= 24 * 60:
        raise ValueError(f"horizon_end_local must lie in 00:01..24:00, got {text!r}")
    return minutes


@lru_cache(maxsize=8)
def _load_slot_config_cached(path: str) -> SlotConfig:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    return SlotConfig.model_validate(data["slots"])


def load_slot_config(path: str | Path = REGISTRY_CONFIG_PATH) -> SlotConfig:
    """Load the ``slots:`` block of ``config/registry.yaml`` (cached per path)."""
    return _load_slot_config_cached(str(Path(path).resolve()))


# --- output types --------------------------------------------------------------------------


class BindingConstraint(enum.StrEnum):
    CAPACITY = "capacity"
    LATEST_START = "latest_start"
    IN_WATER = "in_water"
    TIME_LIMIT = "time_limit"


class PinReason(enum.StrEnum):
    IN_WATER = "in_water"
    NOT_CONFLICTED = "not_conflicted"


class PlanShift(BaseModel):
    """One plan in a proposal. ``start_at``/``end_at`` are the filed window (for display)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    plan_id: str
    shift_min: int
    new_start_at: AwareDatetime
    new_end_at: AwareDatetime
    pinned: PinReason | None = None
    start_at: AwareDatetime
    end_at: AwareDatetime


class SlotProposal(BaseModel):
    """A proposed set of start shifts. It changes nothing until an operator accepts it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    proposal_id: str
    target_plan_id: str
    zone_id: str
    capacity: int
    now: AwareDatetime
    status: Literal["feasible", "infeasible"]
    optimal: bool
    shifts: tuple[PlanShift, ...]
    total_delay_min: int
    binding_constraint: BindingConstraint | None
    detail: str
    inputs_hash: str
    solver_status: str

    @property
    def plan_ids(self) -> tuple[str, ...]:
        return tuple(s.plan_id for s in self.shifts)


# --- the plans a proposal covers -----------------------------------------------------------


def _order(v: PlanView) -> tuple[datetime, str]:
    return (v.start_at, v.plan_id)


def _day_start(target: PlanView) -> datetime:
    local = target.start_at
    return local.replace(hour=0, minute=0, second=0, microsecond=0)


def model_set(
    target: PlanView,
    in_force: Sequence[PlanView],
    harbor: Harbor,
    cfg: ConflictConfig,
) -> tuple[PlanView, ...]:
    """The plans the model covers, target included, sorted by ``(start_at, plan_id)``.

    A plan is in the set when it is in force, its footprint shares buffered area
    (``intersection.area > 0``) and depth band with the target's, and its window meets
    the target's local day.
    """
    day_lo = _day_start(target)
    day_hi = day_lo + timedelta(days=1)
    target_fp = footprint_of(target, cfg)
    target_geom = zone_geometry(harbor, target.area, cfg.area_buffer_m)
    members: dict[str, PlanView] = {target.plan_id: target}
    for view in sorted(in_force, key=_order):
        if view.plan_id in members or view.status not in IN_FORCE:
            continue
        if not (view.start_at < day_hi and view.end_at > day_lo):
            continue
        fp = footprint_of(view, cfg)
        depth = min(fp.depth_max_m, target_fp.depth_max_m) - max(
            fp.depth_min_m, target_fp.depth_min_m
        )
        if depth <= 0 or target_geom.is_empty:
            continue
        geom = zone_geometry(harbor, view.area, cfg.area_buffer_m)
        if float(target_geom.intersection(geom).area) <= 0:
            continue
        members[view.plan_id] = view
    return tuple(sorted(members.values(), key=_order))


# --- model specs ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SlotSpec:
    """One plan as the model sees it. Times are whole seconds from the local day start."""

    plan_id: str
    rank: int
    start_s: int
    duration_s: int
    k_lo: int
    k_hi: int
    k_hi_relaxed: int  # the bound when the plan's latest start is lifted (horizon only)
    pinned: PinReason | None


def _seconds(at: datetime, day_lo: datetime) -> int:
    return int(round((at - day_lo).total_seconds()))


def _ceil_div(a: int, b: int) -> int:
    return -((-a) // b)


def build_specs(
    members: Sequence[PlanView], *, now: datetime, cfg: SlotConfig, day_start: datetime
) -> tuple[SlotSpec, ...]:
    """Per-plan step bounds, in ``(start_at, plan_id)`` order, relative to ``day_start``."""
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    ordered = sorted(members, key=_order)
    if not ordered:
        return ()
    step = cfg.slot_step_min * 60
    slack = timedelta(minutes=cfg.latest_start_slack_min)
    horizon_s = cfg.horizon_minutes * 60
    out: list[SlotSpec] = []
    day_lo = day_start
    for rank, view in enumerate(ordered):
        start = _seconds(view.start_at, day_lo)
        end = _seconds(view.end_at, day_lo)
        pinned: PinReason | None = None
        if view.start_at <= now:
            pinned = PinReason.IN_WATER
        elif view.status is not PlanStatus.CONFLICTED:
            pinned = PinReason.NOT_CONFLICTED
        if pinned is not None:
            out.append(SlotSpec(view.plan_id, rank, start, end - start, 0, 0, 0, pinned))
            continue
        earliest = view.earliest_start_at or view.start_at
        latest = view.latest_start_at or view.start_at + slack
        floor_s = max(_seconds(earliest, day_lo), _seconds(now, day_lo), 0)
        k_lo = min(0, _ceil_div(floor_s - start, step))
        horizon_k = max(0, (horizon_s - end) // step)
        latest_k = max(0, (_seconds(latest, day_lo) - start) // step)
        out.append(
            SlotSpec(
                plan_id=view.plan_id,
                rank=rank,
                start_s=start,
                duration_s=end - start,
                k_lo=k_lo,
                k_hi=min(latest_k, horizon_k),
                k_hi_relaxed=horizon_k,
                pinned=None,
            )
        )
    return tuple(out)


# --- CP-SAT --------------------------------------------------------------------------------


def make_solver(cfg: SlotConfig, time_limit_s: float | None = None) -> Any:
    """A CP-SAT solver with the WO-082 parameters (single worker, fixed seed, 2 s).

    ``time_limit_s`` (default ``cfg.max_time_in_seconds``) sets both the wall and the
    deterministic-time limit.
    """
    limit = float(cfg.max_time_in_seconds if time_limit_s is None else time_limit_s)
    from ortools.sat.python import cp_model

    solver = cp_model.CpSolver()
    params = solver.parameters
    # Owner directive: one search worker. OR-Tools 9.15 rejects a model whose parameters
    # set both num_search_workers and num_workers ("Do not specify both"), so only the
    # directive's field is set; num_workers stays 0 and defers to it.
    params.num_search_workers = 1
    params.random_seed = int(cfg.random_seed)
    params.max_time_in_seconds = limit
    params.max_deterministic_time = limit
    params.log_search_progress = False
    return solver


def build_model(
    specs: Sequence[SlotSpec],
    capacity: int,
    cfg: SlotConfig,
    *,
    relax: frozenset[str] = frozenset(),
    objective: bool = True,
) -> tuple[Any, list[Any]]:
    """The CP-SAT model and its step variables, built in ``specs`` order.

    ``relax`` lifts the latest-start bound of the named plans (used by :func:`diagnose`);
    ``objective=False`` builds the feasibility model only.
    """
    from ortools.sat.python import cp_model

    step = cfg.slot_step_min * 60
    model = cp_model.CpModel()
    steps: list[Any] = []
    intervals: list[Any] = []
    magnitudes: list[Any] = []
    for spec in specs:
        hi = spec.k_hi_relaxed if spec.plan_id in relax else spec.k_hi
        hi = max(hi, spec.k_lo)
        k = model.new_int_var(spec.k_lo, hi, f"k_{spec.rank:04d}")
        steps.append(k)
        intervals.append(
            model.new_fixed_size_interval_var(
                k * step + spec.start_s, spec.duration_s, f"slot_{spec.rank:04d}"
            )
        )
        size = model.new_int_var(0, max(-spec.k_lo, hi), f"abs_{spec.rank:04d}")
        model.add_abs_equality(size, k)
        magnitudes.append(size)
    model.add_cumulative(intervals, [1] * len(intervals), int(capacity))
    if objective and specs:
        n = len(specs)
        reach = sum(max(-s.k_lo, s.k_hi_relaxed, s.k_hi) for s in specs)
        big = n * reach + 1
        model.minimize(
            sum((big + (n - spec.rank)) * m for spec, m in zip(specs, magnitudes, strict=True))
        )
    return model, steps


def solve(
    specs: Sequence[SlotSpec],
    capacity: int,
    cfg: SlotConfig,
    *,
    relax: frozenset[str] = frozenset(),
    objective: bool = True,
    time_limit_s: float | None = None,
) -> tuple[str, tuple[int, ...]]:
    """Solve; returns the CP-SAT status name and the step per plan (empty if none found)."""
    model, steps = build_model(specs, capacity, cfg, relax=relax, objective=objective)
    solver = make_solver(cfg, time_limit_s)
    status = solver.solve(model)
    name = str(solver.status_name(status))
    if name in ("OPTIMAL", "FEASIBLE"):
        return name, tuple(int(solver.value(k)) for k in steps)
    if name == "MODEL_INVALID":
        raise ValueError(f"slot model invalid: {model.validate()}")
    return name, ()


# --- diagnosis -----------------------------------------------------------------------------


def _clock(day_lo: datetime, seconds: int) -> str:
    return (day_lo + timedelta(seconds=seconds)).strftime("%H:%M")


def diagnose(
    specs: Sequence[SlotSpec],
    capacity: int,
    cfg: SlotConfig,
    *,
    day_start: datetime,
    zone_id: str,
) -> tuple[BindingConstraint, str]:
    """Name the binding constraint of an infeasible model, first match in this order.

    1. ``in_water``: plans already in the water exceed capacity on their own;
    2. ``latest_start``: lifting one movable plan's latest start makes the model
       feasible; the plan named is the one whose lift allows the least total delay (ties
       in sorted order); else lifting all of them does (all are named);
    3. ``capacity``: everything else.
    """
    wet = [s for s in specs if s.pinned is PinReason.IN_WATER and s.duration_s > 0]
    for t in sorted({s.start_s for s in wet}):
        covering = [s for s in wet if s.start_s <= t < s.start_s + s.duration_s]
        if len(covering) > capacity:
            names = ", ".join(s.plan_id for s in covering)
            return (
                BindingConstraint.IN_WATER,
                f"{names} in the water at {_clock(day_start, t)} exceed capacity {capacity}",
            )
    step = cfg.slot_step_min * 60
    movable = [s for s in specs if s.pinned is None and s.k_hi_relaxed > s.k_hi]

    def latest_text(spec: SlotSpec) -> str:
        return f"{spec.plan_id} latest_start {_clock(day_start, spec.start_s + spec.k_hi * step)}"

    count = sum(1 for s in specs if s.pinned is None)
    capacity_text = (
        f"{count} movable plan(s) in {zone_id} cannot keep within capacity {capacity} "
        f"before {cfg.horizon_end_local}"
    )
    if not movable:
        return BindingConstraint.CAPACITY, capacity_text
    # Lifting every latest start first: if that is still infeasible, no single lift can
    # help (a single lift is a tighter model), so the diagnosis costs one extra solve.
    every = frozenset(s.plan_id for s in movable)
    status, _ = solve(specs, capacity, cfg, relax=every, objective=False)
    if status == "INFEASIBLE":
        return BindingConstraint.CAPACITY, capacity_text
    if status not in ("OPTIMAL", "FEASIBLE"):
        limit = f"{cfg.max_time_in_seconds:g}"
        return BindingConstraint.TIME_LIMIT, f"diagnosis did not finish within {limit} s"
    # The single lifts share one solve budget, so a diagnosis stays within about 2 x the
    # solve limit however many plans can move (deterministic-time limits keep it repeatable).
    share = cfg.max_time_in_seconds / len(movable)
    best: tuple[int, int, SlotSpec] | None = None
    for spec in movable:
        lift = frozenset({spec.plan_id})
        status, steps = solve(specs, capacity, cfg, relax=lift, time_limit_s=share)
        if status in ("OPTIMAL", "FEASIBLE"):
            delay = sum(abs(k) for k in steps)
            if best is None or (delay, spec.rank) < (best[0], best[1]):
                best = (delay, spec.rank, spec)
    if best is not None:
        return BindingConstraint.LATEST_START, latest_text(best[2])
    return BindingConstraint.LATEST_START, ", ".join(latest_text(s) for s in movable)


# --- the proposal --------------------------------------------------------------------------


def proposal_inputs_hash(
    target_plan_id: str,
    members: Sequence[PlanView],
    now: datetime,
    capacity: int,
    cfg: SlotConfig,
) -> str:
    """Hash of everything a proposal depends on; the store re-checks it on accept."""
    return stable_hash(
        {
            "target_plan_id": target_plan_id,
            "plans": [v.model_dump(mode="json") for v in sorted(members, key=_order)],
            "now": now.astimezone(UTC).isoformat(),
            "capacity": capacity,
            "cfg": cfg.model_dump(mode="json"),
        }
    )


def propose_slots(
    target_plan_id: str,
    plans: Sequence[PlanView],
    *,
    harbor: Harbor,
    now: datetime,
    cfg: SlotConfig | None = None,
    conflict_cfg: ConflictConfig | None = None,
) -> SlotProposal:
    """A slot proposal for the zone of ``target_plan_id`` over the in-force ``plans``.

    Raises ``KeyError`` when the target is not among ``plans``. The input order of
    ``plans`` never matters.
    """
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    slot_cfg = cfg if cfg is not None else load_slot_config()
    ccfg = conflict_cfg if conflict_cfg is not None else load_conflict_config()
    by_id = {p.plan_id: p for p in plans}
    target = by_id[target_plan_id]
    members = model_set(target, sorted(by_id.values(), key=_order), harbor, ccfg)
    capacity = slot_cfg.capacity_for(target.area)
    day_lo = _day_start(target)
    specs = build_specs(members, now=now, cfg=slot_cfg, day_start=day_lo)
    inputs_hash = proposal_inputs_hash(target_plan_id, members, now, capacity, slot_cfg)
    status_name, steps = solve(specs, capacity, slot_cfg)
    common: dict[str, Any] = {
        "proposal_id": "SP-" + inputs_hash[:12],
        "target_plan_id": target_plan_id,
        "zone_id": target.area,
        "capacity": capacity,
        "now": now,
        "inputs_hash": inputs_hash,
        "solver_status": status_name,
    }
    infeasible = not steps
    if infeasible:
        # every plan keeps its filed window; the shifts list the plans covered
        steps = tuple(0 for _ in specs)
    step_min = slot_cfg.slot_step_min
    shifts: list[PlanShift] = []
    for view, spec, k in zip(members, specs, steps, strict=True):
        delta = timedelta(minutes=k * step_min)
        shifts.append(
            PlanShift(
                plan_id=view.plan_id,
                shift_min=k * step_min,
                new_start_at=view.start_at + delta,
                new_end_at=view.end_at + delta,
                pinned=spec.pinned,
                start_at=view.start_at,
                end_at=view.end_at,
            )
        )
    if infeasible:
        if status_name == "INFEASIBLE":
            binding, why = diagnose(
                specs, capacity, slot_cfg, day_start=day_lo, zone_id=target.area
            )
        else:
            limit = f"{slot_cfg.max_time_in_seconds:g}"
            binding, why = BindingConstraint.TIME_LIMIT, f"no slot found within {limit} s"
        return SlotProposal(
            status="infeasible",
            optimal=False,
            shifts=tuple(shifts),
            total_delay_min=0,
            binding_constraint=binding,
            detail=f"{NO_SLOT}: {binding.value} ({why})",
            **common,
        )
    total = sum(abs(k) for k in steps) * step_min
    optimal = status_name == "OPTIMAL"
    if total == 0:
        detail = WITHIN_CAPACITY_DETAIL
    else:
        moved = sum(1 for k in steps if k != 0)
        detail = (
            f"proposed slot: {moved} plan(s) shifted, total delay {total} min, "
            f"zone capacity {capacity}"
        )
    if not optimal:
        detail += f"; best found in {slot_cfg.max_time_in_seconds:g} s"
    return SlotProposal(
        status="feasible",
        optimal=optimal,
        shifts=tuple(shifts),
        total_delay_min=total,
        binding_constraint=None,
        detail=detail,
        **common,
    )
