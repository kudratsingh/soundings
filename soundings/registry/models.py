"""Registry types: the SQLModel table, the input and view models, reasons, time helpers.

``Plan`` is the storage row. Datetimes are stored as UTC instants (SQLite keeps no offset)
so the overlap query compares instants; each end also stores the filer's UTC offset in
minutes so ``PlanView`` hands back the datetime exactly as it was filed. ``PlanView`` is
the only type the matcher (WO-022) sees.

Time bridge (sim <-> live): tick 0 of a scenario is ``registry.sim_epoch_date`` at the
scenario's ``hour_of_day``, at the fixed offset ``registry.sim_epoch_utc_offset``. Fixed
offsets mean no tz database is needed.
"""

from __future__ import annotations

import enum
import math
import re
from datetime import UTC, date, datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any

import yaml
from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
)
from sqlalchemy import Index
from sqlmodel import Field as SQLField
from sqlmodel import SQLModel

from soundings.schemas._base import CONFIG_DIR
from soundings.schemas.denylist import FreeText
from soundings.schemas.scenario import Scenario

REGISTRY_CONFIG_PATH: Path = CONFIG_DIR / "registry.yaml"

_OFFSET_RE = re.compile(r"^([+-])(\d{2}):(\d{2})$")


# --- vocabularies ------------------------------------------------------------------------


class PlanStatus(enum.StrEnum):
    ACTIVE = "active"
    EXPIRED = "expired"


class PlanSource(enum.StrEnum):
    SCENARIO = "scenario"
    FORM = "form"
    API = "api"


class RejectReason(enum.StrEnum):
    """Why a plan never entered the store. Checked in this order."""

    AREA_NOT_SECURITY_ZONE = "area_not_security_zone"
    WINDOW_MISSING = "window_missing"
    WINDOW_INVERTED = "window_inverted"
    BAD_MODEM_ID = "bad_modem_id"
    DUPLICATE_PLAN_ID = "duplicate_plan_id"
    DENYLISTED_TEXT = "denylisted_text"


class PlanRejected(Exception):
    """A plan failed validation; nothing was written."""

    def __init__(self, reason: RejectReason, detail: str) -> None:
        super().__init__(f"{reason.value}: {detail}")
        self.reason = reason
        self.detail = detail


# --- config ------------------------------------------------------------------------------


class RegistryConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    modem_id_pattern: str
    sim_epoch_date: date
    sim_epoch_utc_offset: Annotated[str, StringConstraints(pattern=_OFFSET_RE.pattern)]
    retention_days: Annotated[int, Field(gt=0)]
    plan_id_prefix: Annotated[str, StringConstraints(pattern=r"^[A-Z][A-Z0-9]*$")]

    @field_validator("modem_id_pattern")
    @classmethod
    def _compiles(cls, v: str) -> str:
        re.compile(v)
        return v

    @property
    def epoch_tz(self) -> timezone:
        return parse_utc_offset(self.sim_epoch_utc_offset)


def parse_utc_offset(text: str) -> timezone:
    """``"-07:00"`` -> ``timezone(timedelta(hours=-7))``."""
    m = _OFFSET_RE.match(text)
    if m is None:
        raise ValueError(f"bad UTC offset {text!r}; expected +HH:MM or -HH:MM")
    sign = -1 if m.group(1) == "-" else 1
    return timezone(sign * timedelta(hours=int(m.group(2)), minutes=int(m.group(3))))


@lru_cache(maxsize=8)
def _load_registry_config_cached(path: str) -> RegistryConfig:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    return RegistryConfig.model_validate(data["registry"])


def load_registry_config(path: str | Path = REGISTRY_CONFIG_PATH) -> RegistryConfig:
    """Load the ``registry:`` block of ``config/registry.yaml`` (cached per path)."""
    return _load_registry_config_cached(str(Path(path).resolve()))


# --- time helpers ------------------------------------------------------------------------


def scenario_epoch(scenario: Scenario, config: RegistryConfig | None = None) -> datetime:
    """Tick 0 of ``scenario``: the sim epoch date at ``hour_of_day:00`` at the fixed offset."""
    cfg = config if config is not None else load_registry_config()
    d = cfg.sim_epoch_date
    return datetime(d.year, d.month, d.day, scenario.hour_of_day, 0, 0, tzinfo=cfg.epoch_tz)


def tick_to_datetime(epoch: datetime, tick: int, tick_s: float) -> datetime:
    """The datetime of ``tick`` (same offset as ``epoch``)."""
    return epoch + timedelta(seconds=tick * tick_s)


def datetime_to_tick(epoch: datetime, dt: datetime, tick_s: float) -> int:
    """The tick containing ``dt`` (floor). Both datetimes must be timezone-aware."""
    if epoch.tzinfo is None or dt.tzinfo is None:
        raise ValueError("datetime_to_tick needs timezone-aware datetimes")
    ticks = (dt - epoch).total_seconds() / tick_s
    return math.floor(round(ticks, 6))  # 6 dp first so float noise cannot drop a tick


def to_utc(dt: datetime) -> datetime:
    """Aware datetime -> the same instant in UTC (the storage form). Naive input raises."""
    if dt.tzinfo is None:
        raise ValueError("registry datetimes must be timezone-aware")
    return dt.astimezone(UTC)


def offset_minutes(dt: datetime) -> int:
    off = dt.utcoffset()
    if off is None:
        raise ValueError("registry datetimes must be timezone-aware")
    return int(off.total_seconds() // 60)


def from_storage(stored: datetime, minutes: int) -> datetime:
    """Stored UTC instant + stored offset -> the aware datetime as it was filed.

    SQLite keeps no offset, so a stored value may come back naive; it is UTC either way.
    """
    utc = stored.replace(tzinfo=UTC) if stored.tzinfo is None else stored
    return utc.astimezone(timezone(timedelta(minutes=minutes)))


# --- storage row -------------------------------------------------------------------------


class Plan(SQLModel, table=True):
    """One filed plan. Datetimes are UTC instants; ``*_offset_min`` keep the filer's offset."""

    __tablename__ = "registry_plan"
    __table_args__ = (
        Index("ix_registry_plan_status_ticks", "status", "start_tick", "end_tick"),
        Index("ix_registry_plan_status_times", "status", "start_at", "end_at"),
    )

    plan_id: str = SQLField(primary_key=True)
    operator: str
    vehicle: str
    modem_id: str = SQLField(index=True)
    area: str
    start_tick: int | None = None
    end_tick: int | None = None
    start_at: datetime
    end_at: datetime
    start_offset_min: int = 0
    end_offset_min: int = 0
    contact_channel: str
    status: str = PlanStatus.ACTIVE.value
    source: str = PlanSource.API.value
    created_at: datetime
    expired_at: datetime | None = None


# --- input and view models ---------------------------------------------------------------

NonNegTick = Annotated[int, Field(ge=0)]
PlanIdText = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$")]
Text = Annotated[str, StringConstraints(min_length=1, max_length=200)]
CleanText = Annotated[FreeText, StringConstraints(min_length=1, max_length=200)]


class PlanDraft(BaseModel):
    """A plan as submitted, before the registry rules run. Free text is not yet checked.

    The store validates a draft in the order of ``RejectReason`` and only then builds the
    ``PlanCreate`` (whose ``FreeText`` fields run the denylist), so an outside-zone plan is
    reported as outside-zone even if its text is also bad.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    plan_id: PlanIdText | None = None
    operator: Text
    vehicle: Text
    modem_id: str
    area: str
    start_tick: NonNegTick | None = None
    end_tick: NonNegTick | None = None
    start_at: AwareDatetime | None = None
    end_at: AwareDatetime | None = None
    contact_channel: Text
    source: PlanSource = PlanSource.API
    created_at: AwareDatetime | None = None

    @field_validator("plan_id", "start_tick", "end_tick", "start_at", "end_at", mode="before")
    @classmethod
    def _blank_is_none(cls, v: Any) -> Any:
        return None if isinstance(v, str) and v.strip() == "" else v


class PlanCreate(PlanDraft):
    """Validated input. ``operator``, ``vehicle`` and ``contact_channel`` are ``FreeText``.

    ``plan_id`` is optional; the store assigns ``P-<yyyymmdd>-<nnn>`` (the date of
    ``start_at`` as filed, sequential per date) when it is missing.
    """

    operator: CleanText
    vehicle: CleanText
    contact_channel: CleanText


class PlanView(BaseModel):
    """The read-only plan the matcher sees. Datetimes carry the offset they were filed with."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    plan_id: str
    operator: str
    vehicle: str
    modem_id: str
    area: str
    start_tick: int | None
    end_tick: int | None
    start_at: AwareDatetime
    end_at: AwareDatetime
    contact_channel: str
    status: PlanStatus
    source: PlanSource = PlanSource.API
    created_at: AwareDatetime | None = None
    expired_at: AwareDatetime | None = None

    @classmethod
    def from_row(cls, row: Plan) -> PlanView:
        return cls(
            plan_id=row.plan_id,
            operator=row.operator,
            vehicle=row.vehicle,
            modem_id=row.modem_id,
            area=row.area,
            start_tick=row.start_tick,
            end_tick=row.end_tick,
            start_at=from_storage(row.start_at, row.start_offset_min),
            end_at=from_storage(row.end_at, row.end_offset_min),
            contact_channel=row.contact_channel,
            status=PlanStatus(row.status),
            source=PlanSource(row.source),
            created_at=from_storage(row.created_at, 0),
            expired_at=None if row.expired_at is None else from_storage(row.expired_at, 0),
        )
