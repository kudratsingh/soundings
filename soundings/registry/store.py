"""SQLite-backed registry store (SQLModel). The overlap queries are the matcher's hot path.

Overlap is closed-interval: ``a.start <= b.end and b.start <= a.end``, evaluated in SQL on
the ``(status, start, end)`` indexes. Tick queries order by ``(start_tick, plan_id)``;
datetime queries, ``list`` and ``by_modem`` order by ``(start_at, plan_id)``. Every order
ends on the primary key, so results never depend on insertion or hash order.

The clock is injected (``now``); seeding a scenario sets ``created_at`` to the scenario
epoch, so seeded plans are byte-identical across runs (CLAUDE.md 2.4).
"""

from __future__ import annotations

import builtins
import os
import re
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, event
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, col, create_engine, func, select

from soundings.registry.models import (
    Plan,
    PlanCreate,
    PlanDraft,
    PlanRejected,
    PlanSource,
    PlanStatus,
    PlanView,
    RegistryConfig,
    RejectReason,
    datetime_to_tick,
    load_registry_config,
    offset_minutes,
    scenario_epoch,
    tick_to_datetime,
    to_utc,
)
from soundings.schemas.denylist import find_denylisted
from soundings.schemas.harbor import Harbor, load_harbor
from soundings.schemas.scenario import Scenario

DEFAULT_DB_PATH: Path = Path("soundings.db")
MEMORY = ":memory:"
FREE_TEXT_FIELDS = ("operator", "vehicle", "contact_channel")

Clock = Callable[[], datetime]


def db_path_from_env() -> Path:
    """``SOUNDINGS_DB`` if set and non-empty, else ``soundings.db`` (relative to the cwd)."""
    value = os.environ.get("SOUNDINGS_DB", "").strip()
    return Path(value) if value else DEFAULT_DB_PATH


@lru_cache(maxsize=1)
def _default_harbor() -> Harbor:
    return load_harbor()


def _wall_clock() -> datetime:
    return datetime.now(UTC)


def _make_engine(db_path: Path | str) -> Engine:
    if str(db_path) == MEMORY:
        engine = create_engine(
            "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
        )
    else:
        path = Path(db_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        engine = create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False})

    @event.listens_for(engine, "connect")
    def _pragmas(dbapi_conn: Any, _record: Any) -> None:
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.close()

    return engine


class RegistryStore:
    """Plans in SQLite. ``db_path`` defaults to ``db_path_from_env()``; ``":memory:"`` works."""

    def __init__(
        self,
        db_path: Path | str | None = None,
        *,
        harbor: Harbor | None = None,
        now: Clock | None = None,
        config: RegistryConfig | None = None,
    ) -> None:
        self.db_path: Path | str = db_path if db_path is not None else db_path_from_env()
        self.harbor = harbor if harbor is not None else _default_harbor()
        self.config = config if config is not None else load_registry_config()
        self._now: Clock = now if now is not None else _wall_clock
        self._modem_re = re.compile(self.config.modem_id_pattern)
        self.engine = _make_engine(self.db_path)
        SQLModel.metadata.create_all(self.engine, tables=[Plan.__table__])  # type: ignore[attr-defined]

    # --- plumbing -----------------------------------------------------------------------

    @contextmanager
    def _session(self) -> Iterator[Session]:
        with Session(self.engine, expire_on_commit=False) as session:
            yield session

    def now(self) -> datetime:
        current = self._now()
        if current.tzinfo is None:
            raise ValueError("the registry clock must return timezone-aware datetimes")
        return current

    def close(self) -> None:
        self.engine.dispose()

    def zone_ids(self) -> builtins.list[str]:
        return sorted(z.zone_id for z in self.harbor.zones)

    def count(self) -> int:
        with self._session() as s:
            return int(s.exec(select(func.count()).select_from(Plan)).one())

    # --- validation ---------------------------------------------------------------------

    def _check(self, draft: PlanDraft) -> None:
        """Rules 1-6 in ``RejectReason`` order; the first failure raises ``PlanRejected``."""
        zones = self.zone_ids()
        if draft.area not in zones:
            raise PlanRejected(
                RejectReason.AREA_NOT_SECURITY_ZONE,
                f"area {draft.area!r} is not a declared security zone; valid: {', '.join(zones)}",
            )
        ticks = (draft.start_tick, draft.end_tick)
        times = (draft.start_at, draft.end_at)
        for name, pair in (("tick", ticks), ("datetime", times)):
            if (pair[0] is None) != (pair[1] is None):
                raise PlanRejected(
                    RejectReason.WINDOW_MISSING, f"{name} window has only one end; give both"
                )
        has_ticks = ticks[0] is not None
        has_times = times[0] is not None
        if not has_ticks and not has_times:
            raise PlanRejected(
                RejectReason.WINDOW_MISSING,
                "need a complete window: start_tick and end_tick, or start_at and end_at",
            )
        if has_ticks and draft.start_tick is not None and draft.end_tick is not None:
            if draft.start_tick > draft.end_tick:
                raise PlanRejected(
                    RejectReason.WINDOW_INVERTED,
                    f"start_tick {draft.start_tick} is after end_tick {draft.end_tick}",
                )
        if has_times and draft.start_at is not None and draft.end_at is not None:
            if draft.start_at > draft.end_at:
                raise PlanRejected(
                    RejectReason.WINDOW_INVERTED,
                    f"start_at {draft.start_at.isoformat()} is after {draft.end_at.isoformat()}",
                )
        if self._modem_re.fullmatch(draft.modem_id) is None:
            raise PlanRejected(
                RejectReason.BAD_MODEM_ID,
                f"modem_id {draft.modem_id!r} does not match {self.config.modem_id_pattern}",
            )
        if draft.plan_id is not None and self.get(draft.plan_id) is not None:
            raise PlanRejected(
                RejectReason.DUPLICATE_PLAN_ID, f"plan_id {draft.plan_id!r} already exists"
            )
        for field in FREE_TEXT_FIELDS:
            hits = find_denylisted(getattr(draft, field))
            if hits:
                raise PlanRejected(
                    RejectReason.DENYLISTED_TEXT,
                    f"{field} contains {len(hits)} denylisted term(s) (CLAUDE.md 2.5)",
                )

    def _next_plan_id(self, session: Session, start_at: datetime) -> str:
        stem = f"{self.config.plan_id_prefix}-{start_at.strftime('%Y%m%d')}-"
        ids = session.exec(select(Plan.plan_id).where(col(Plan.plan_id).startswith(stem))).all()
        numbers = [int(i[len(stem) :]) for i in ids if i[len(stem) :].isdigit()]
        return f"{stem}{max(numbers, default=0) + 1:03d}"

    # --- writes -------------------------------------------------------------------------

    def create(
        self,
        plan: PlanCreate | PlanDraft | Mapping[str, Any],
        *,
        epoch: datetime | None = None,
        tick_s: float | None = None,
    ) -> PlanView:
        """Validate and insert one plan. Raises ``PlanRejected``; nothing is written then.

        With only a tick window, ``epoch`` and ``tick_s`` (sim callers) derive the datetimes;
        without them the plan is rejected (``window_missing``). With only datetimes, ticks
        are derived when ``epoch`` and ``tick_s`` are given and left null otherwise (live).
        """
        draft = plan if isinstance(plan, PlanDraft) else PlanDraft.model_validate(dict(plan))
        self._check(draft)
        clean = PlanCreate.model_validate(draft.model_dump())
        bridge = epoch is not None and tick_s is not None
        start_tick, end_tick = clean.start_tick, clean.end_tick
        start_at, end_at = clean.start_at, clean.end_at
        if start_at is None or end_at is None:
            if not bridge or start_tick is None or end_tick is None:
                raise PlanRejected(
                    RejectReason.WINDOW_MISSING,
                    "a tick-only plan needs epoch and tick_s to place it in time; "
                    "give start_at and end_at",
                )
            assert epoch is not None and tick_s is not None
            start_at = tick_to_datetime(epoch, start_tick, tick_s)
            end_at = tick_to_datetime(epoch, end_tick, tick_s)
        elif start_tick is None and bridge:
            assert epoch is not None and tick_s is not None
            derived_start = datetime_to_tick(epoch, start_at, tick_s)
            derived_end = datetime_to_tick(epoch, end_at, tick_s)
            if derived_end >= 0:
                start_tick, end_tick = max(0, derived_start), derived_end
        created_at = clean.created_at if clean.created_at is not None else self.now()
        with self._session() as session:
            plan_id = clean.plan_id or self._next_plan_id(session, start_at)
            row = Plan(
                plan_id=plan_id,
                operator=clean.operator,
                vehicle=clean.vehicle,
                modem_id=clean.modem_id,
                area=clean.area,
                start_tick=start_tick,
                end_tick=end_tick,
                start_at=to_utc(start_at),
                end_at=to_utc(end_at),
                start_offset_min=offset_minutes(start_at),
                end_offset_min=offset_minutes(end_at),
                contact_channel=clean.contact_channel,
                status=PlanStatus.ACTIVE.value,
                source=clean.source.value,
                created_at=to_utc(created_at),
                expired_at=None,
            )
            session.add(row)
            session.commit()
            return PlanView.from_row(row)

    def expire(self, plan_id: str, *, at: datetime | None = None) -> PlanView:
        """Mark a plan expired (idempotent: an expired plan keeps its first ``expired_at``)."""
        with self._session() as session:
            row = session.get(Plan, plan_id)
            if row is None:
                raise KeyError(plan_id)
            if row.status != PlanStatus.EXPIRED.value:
                row.status = PlanStatus.EXPIRED.value
                row.expired_at = to_utc(at if at is not None else self.now())
                session.add(row)
                session.commit()
            return PlanView.from_row(row)

    def purge_older_than(self, days: int | None = None, *, now: datetime | None = None) -> int:
        """Delete plans whose ``end_at`` is more than ``days`` (default 30) before ``now``.

        This is the only real delete (CLAUDE.md section 6: no retention beyond 30 days).
        """
        span = days if days is not None else self.config.retention_days
        cutoff = to_utc((now if now is not None else self.now()) - timedelta(days=span))
        with self._session() as session:
            rows = session.exec(select(Plan).where(col(Plan.end_at) < cutoff)).all()
            for row in rows:
                session.delete(row)
            session.commit()
            return len(rows)

    def seed_from_scenario(
        self, scenario: Scenario, *, skip_existing: bool = False
    ) -> builtins.list[PlanView]:
        """Insert every ``registry_plans`` entry of ``scenario`` (ticks and datetimes)."""
        epoch = scenario_epoch(scenario, self.config)
        out: builtins.list[PlanView] = []
        for plan in plans_from_scenario(scenario, self.config):
            if skip_existing and plan.plan_id is not None:
                existing = self.get(plan.plan_id)
                if existing is not None:
                    out.append(existing)
                    continue
            out.append(self.create(plan, epoch=epoch, tick_s=scenario.tick_s))
        return out

    # --- reads --------------------------------------------------------------------------

    def get(self, plan_id: str) -> PlanView | None:
        with self._session() as session:
            row = session.get(Plan, plan_id)
            return None if row is None else PlanView.from_row(row)

    def list(self, status: PlanStatus | str | None = None) -> builtins.list[PlanView]:
        """All plans (or those with ``status``), ordered by ``(start_at, plan_id)``."""
        stmt = select(Plan)
        if status is not None:
            stmt = stmt.where(col(Plan.status) == PlanStatus(status).value)
        stmt = stmt.order_by(col(Plan.start_at), col(Plan.plan_id))
        return self._views(stmt)

    def by_modem(self, modem_id: str) -> builtins.list[PlanView]:
        """Every plan (any status) filed for ``modem_id``, ordered by ``(start_at, plan_id)``."""
        stmt = (
            select(Plan)
            .where(col(Plan.modem_id) == modem_id)
            .order_by(col(Plan.start_at), col(Plan.plan_id))
        )
        return self._views(stmt)

    def overlapping_ticks(
        self, start_tick: int, end_tick: int, *, include_expired: bool = False
    ) -> builtins.list[PlanView]:
        """Plans whose closed tick window meets ``[start_tick, end_tick]``.

        Plans filed without ticks (live-only) never match. Ordered by
        ``(start_tick, plan_id)``.
        """
        if start_tick > end_tick:
            raise ValueError("start_tick must be <= end_tick")
        stmt = select(Plan).where(
            col(Plan.start_tick) <= end_tick,
            col(Plan.end_tick) >= start_tick,
        )
        if not include_expired:
            stmt = stmt.where(col(Plan.status) == PlanStatus.ACTIVE.value)
        stmt = stmt.order_by(col(Plan.start_tick), col(Plan.plan_id))
        return self._views(stmt)

    def overlapping_times(
        self, start_at: datetime, end_at: datetime, *, include_expired: bool = False
    ) -> builtins.list[PlanView]:
        """Plans whose closed datetime window meets ``[start_at, end_at]`` (aware datetimes).

        Compared as instants, so offsets may differ. Ordered by ``(start_at, plan_id)``.
        """
        lo, hi = to_utc(start_at), to_utc(end_at)
        if lo > hi:
            raise ValueError("start_at must be <= end_at")
        stmt = select(Plan).where(col(Plan.start_at) <= hi, col(Plan.end_at) >= lo)
        if not include_expired:
            stmt = stmt.where(col(Plan.status) == PlanStatus.ACTIVE.value)
        stmt = stmt.order_by(col(Plan.start_at), col(Plan.plan_id))
        return self._views(stmt)

    def _views(self, stmt: Any) -> builtins.list[PlanView]:
        with self._session() as session:
            return [PlanView.from_row(row) for row in session.exec(stmt).all()]


def plans_from_scenario(
    scenario: Scenario, config: RegistryConfig | None = None
) -> list[PlanCreate]:
    """Map each WO-010 ``RegistryPlan`` to a ``PlanCreate`` with ticks and datetimes.

    ``contact`` becomes ``contact_channel``; ``created_at`` is the scenario epoch and
    ``source`` is ``scenario``, so the result never depends on the wall clock.
    """
    epoch = scenario_epoch(scenario, config)
    out: list[PlanCreate] = []
    for p in scenario.registry_plans:
        out.append(
            PlanCreate(
                plan_id=p.plan_id,
                operator=p.operator,
                vehicle=p.vehicle,
                modem_id=p.modem_id,
                area=p.area,
                start_tick=p.window.start_tick,
                end_tick=p.window.end_tick,
                start_at=tick_to_datetime(epoch, p.window.start_tick, scenario.tick_s),
                end_at=tick_to_datetime(epoch, p.window.end_tick, scenario.tick_s),
                contact_channel=p.contact,
                source=PlanSource.SCENARIO,
                created_at=epoch,
            )
        )
    return out


def plan_views_from_scenario(
    scenario: Scenario, config: RegistryConfig | None = None
) -> list[PlanView]:
    """The scenario's plans as active ``PlanView`` objects, without a database.

    Same content as seeding a fresh store and reading it back (``created_at`` in UTC),
    ordered by ``(start_tick, plan_id)``.
    """
    views: list[PlanView] = []
    for p in plans_from_scenario(scenario, config):
        assert p.plan_id is not None and p.start_at is not None and p.end_at is not None
        assert p.created_at is not None
        views.append(
            PlanView(
                plan_id=p.plan_id,
                operator=p.operator,
                vehicle=p.vehicle,
                modem_id=p.modem_id,
                area=p.area,
                start_tick=p.start_tick,
                end_tick=p.end_tick,
                start_at=p.start_at,
                end_at=p.end_at,
                contact_channel=p.contact_channel,
                status=PlanStatus.ACTIVE,
                source=PlanSource.SCENARIO,
                created_at=p.created_at.astimezone(UTC),
            )
        )
    return sorted(views, key=lambda v: (v.start_tick or 0, v.plan_id))


def overlaps(a_start: Any, a_end: Any, b_start: Any, b_end: Any) -> bool:
    """Closed-interval overlap, the rule the SQL queries implement."""
    return bool(a_start <= b_end and b_start <= a_end)
