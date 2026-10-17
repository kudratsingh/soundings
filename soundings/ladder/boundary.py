"""The boundary table and the standing-orders criteria, read from ``config/ladder.yaml``.

SPEC-LADDER section 4 is data: the guards read this table, the UI shows it, and nothing
here holds a threshold literal (CLAUDE.md section 3). ``load_boundary`` reads the
``boundary`` and ``criteria`` blocks through WO-010's ``load_ladder_config``.

CLAUDE.md 2.1: the ``never`` cell is absolute for the platform. The YAML lists the call to
USCG / EOD, any closure and anything after the handoff under ``person_approves`` because a
person makes those calls; the platform never executes them, even with a valid approval
token. ``NEVER_ACTIONS`` maps the standing-orders ``never`` names onto those actions.
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path

from pydantic import Field, model_validator

from soundings.schemas import Rung, load_ladder_config
from soundings.schemas._base import HarborId, NonNegFloat, NonNegInt, Probability, StrictModel
from soundings.schemas.config import LADDER_PATH


class BoundaryCell(StrEnum):
    """Who decides an action: the platform alone, standing orders, a person, or nobody here."""

    PLATFORM_ALONE = "platform_alone"
    STANDING_ORDERS = "standing_orders"
    PERSON_APPROVES = "person_approves"
    NEVER = "never"


ANYTHING_AFTER_HANDOFF = "anything_after_handoff"

NEVER_ACTIONS: dict[str, str] = {
    "harbor_closure": "any_closure",
    "external_call": "external_call_uscg_eod",
}
"""Standing-orders ``degraded_mode.never`` names -> boundary action names."""

ABSOLUTE_NEVER: frozenset[str] = frozenset(NEVER_ACTIONS.values()) | {ANYTHING_AFTER_HANDOFF}
"""Actions the platform never executes, whatever the orders, tokens or degraded mode."""

_YAML_CELLS: tuple[BoundaryCell, ...] = (
    BoundaryCell.PLATFORM_ALONE,
    BoundaryCell.STANDING_ORDERS,
    BoundaryCell.PERSON_APPROVES,
)


class SlowCriteria(StrictModel):
    """``criteria.slow`` plus the slow rung's boundary actions."""

    threat_mass_min: Probability
    time_to_critical_asset_max_s: NonNegFloat
    action: HarborId
    """The single ``standing_orders`` action of the slow rung (``advisory_one_basin``)."""
    wider_actions: tuple[HarborId, ...] = ()
    """The slow rung's ``person_approves`` actions."""


class HoldCriteria(StrictModel):
    """``criteria.hold`` plus the hold rung's boundary actions."""

    threat_mass_min: Probability
    time_to_critical_asset_max_s: NonNegFloat
    zone_criticality_min: Probability
    active_holds_max: NonNegInt
    action: HarborId
    """The single ``standing_orders`` action of the hold rung (``hold_one_gate_or_berth``)."""
    wider_actions: tuple[HarborId, ...] = ()
    """The hold rung's ``person_approves`` actions (longer, wider, a second hold)."""


class LadderCriteria(StrictModel):
    slow: SlowCriteria
    hold: HoldCriteria


class BoundaryRow(StrictModel):
    """One (rung, action) entry: the cell the YAML names and the cell the platform obeys."""

    rung: Rung
    action: HarborId
    yaml_cell: BoundaryCell
    cell: BoundaryCell


class BoundaryTable(StrictModel):
    """Every boundary action with its rung and cell, in YAML order, plus the criteria."""

    entries: tuple[BoundaryRow, ...]
    criteria: LadderCriteria
    never: tuple[HarborId, ...] = Field(default_factory=lambda: tuple(sorted(ABSOLUTE_NEVER)))

    @model_validator(mode="after")
    def _unique(self) -> BoundaryTable:
        names = [e.action for e in self.entries]
        dupes = sorted({n for n in names if names.count(n) > 1})
        if dupes:
            raise ValueError(f"boundary actions must be unique: {dupes}")
        return self

    def _entry(self, action: str) -> BoundaryRow:
        for e in self.entries:
            if e.action == action:
                return e
        raise KeyError(action)

    def __contains__(self, action: object) -> bool:
        return any(e.action == action for e in self.entries)

    def cell_of(self, action: str) -> BoundaryCell:
        """The cell the platform obeys (``never`` for the absolute list). KeyError if unknown."""
        return self._entry(action).cell

    def rung_of(self, action: str) -> Rung:
        return self._entry(action).rung

    def actions(self) -> tuple[str, ...]:
        return tuple(e.action for e in self.entries)

    def rows(self) -> list[dict[str, str]]:
        """JSON rows for ``GET /api/boundary`` (WO-060): rung, action, yaml_cell, cell."""
        return [e.model_dump(mode="json") for e in self.entries]


def _single(actions: tuple[str, ...], rung: Rung) -> str:
    if len(actions) != 1:
        raise ValueError(f"boundary.{rung.value}.standing_orders must name exactly one action")
    return actions[0]


def load_boundary(path: str | Path = LADDER_PATH) -> BoundaryTable:
    """Read ``boundary`` and ``criteria`` from ``config/ladder.yaml`` into a ``BoundaryTable``."""
    cfg = load_ladder_config(path)
    entries: list[BoundaryRow] = []
    for rung in cfg.rungs:
        block = cfg.boundary[rung]
        for yaml_cell in _YAML_CELLS:
            for action in getattr(block, yaml_cell.value):
                cell = BoundaryCell.NEVER if action in ABSOLUTE_NEVER else yaml_cell
                entries.append(
                    BoundaryRow(rung=rung, action=action, yaml_cell=yaml_cell, cell=cell)
                )
    missing = sorted(ABSOLUTE_NEVER - {e.action for e in entries})
    if missing:
        raise ValueError(f"boundary table lacks the never actions {missing}")
    slow_c = cfg.criteria[Rung.SLOW]
    hold_c = cfg.criteria[Rung.HOLD]
    active_max = hold_c["active_holds_max"]
    if float(active_max) != int(active_max):
        raise ValueError("criteria.hold.active_holds_max must be a whole number")
    slow_b = cfg.boundary[Rung.SLOW]
    hold_b = cfg.boundary[Rung.HOLD]
    criteria = LadderCriteria(
        slow=SlowCriteria(
            threat_mass_min=float(slow_c["threat_mass_min"]),
            time_to_critical_asset_max_s=float(slow_c["time_to_critical_asset_max_s"]),
            action=_single(slow_b.standing_orders, Rung.SLOW),
            wider_actions=slow_b.person_approves,
        ),
        hold=HoldCriteria(
            threat_mass_min=float(hold_c["threat_mass_min"]),
            time_to_critical_asset_max_s=float(hold_c["time_to_critical_asset_max_s"]),
            zone_criticality_min=float(hold_c["zone_criticality_min"]),
            active_holds_max=int(active_max),
            action=_single(hold_b.standing_orders, Rung.HOLD),
            wider_actions=hold_b.person_approves,
        ),
    )
    return BoundaryTable(entries=tuple(entries), criteria=criteria)


def load_criteria(path: str | Path = LADDER_PATH) -> LadderCriteria:
    """The ``criteria`` block alone (with each rung's boundary actions)."""
    return load_boundary(path).criteria
