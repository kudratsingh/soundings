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
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any, cast

from sqlalchemy import Engine, event
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, col, create_engine, func, select, update

from soundings.registry.conflicts import (
    Conflict,
    PlanFootprint,
    conflict_view,
    conflicts_among,
    detect_conflicts,
    footprint_of,
    mirror,
)
from soundings.registry.models import (
    DEFAULT_JOB_TYPE,
    IN_FORCE,
    JOB_TYPE_NAMES,
    ConflictConfig,
    ConflictView,
    JobTypeName,
    Plan,
    PlanConflict,
    PlanCreate,
    PlanDraft,
    PlanRejected,
    PlanSource,
    PlanStatus,
    PlanView,
    RegistryConfig,
    RejectReason,
    datetime_to_tick,
    from_storage,
    load_conflict_config,
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
_IN_FORCE_VALUES = sorted(s.value for s in IN_FORCE)
_PLAN_TABLE: Any = Plan.__table__  # type: ignore[attr-defined]
_CONFLICT_TABLE: Any = PlanConflict.__table__  # type: ignore[attr-defined]

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
        conflict_config: ConflictConfig | None = None,
    ) -> None:
        self.db_path: Path | str = db_path if db_path is not None else db_path_from_env()
        self.harbor = harbor if harbor is not None else _default_harbor()
        self.config = config if config is not None else load_registry_config()
        self.conflict_config = (
            conflict_config if conflict_config is not None else load_conflict_config()
        )
        self._now: Clock = now if now is not None else _wall_clock
        self._modem_re = re.compile(self.config.modem_id_pattern)
        self.engine = _make_engine(self.db_path)
        SQLModel.metadata.create_all(
            self.engine,
            tables=[Plan.__table__, PlanConflict.__table__],  # type: ignore[attr-defined]
        )

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
        # --- WO-081 begin: depth band (both or neither; min <= max) ---
        if (draft.depth_min_m is None) != (draft.depth_max_m is None):
            raise PlanRejected(
                RejectReason.DEPTH_BAND_INVERTED,
                "depth band has only one end; give depth_min_m and depth_max_m, or neither",
            )
        if (
            draft.depth_min_m is not None
            and draft.depth_max_m is not None
            and draft.depth_min_m > draft.depth_max_m
        ):
            raise PlanRejected(
                RejectReason.DEPTH_BAND_INVERTED,
                f"depth_min_m {draft.depth_min_m} is below depth_max_m {draft.depth_max_m}",
            )
        # --- WO-081 end ---
        if self._modem_re.fullmatch(draft.modem_id) is None:
            raise PlanRejected(
                RejectReason.BAD_MODEM_ID,
                f"modem_id {draft.modem_id!r} does not match {self.config.modem_id_pattern}",
            )
        if draft.plan_id is not None and self._exists(draft.plan_id):
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

    def _exists(self, plan_id: str) -> bool:
        with self._session() as session:
            return session.get(Plan, plan_id) is not None

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
        stored_tick_s = clean.tick_s if clean.tick_s is not None else (tick_s if bridge else None)
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
                job_type=clean.job_type,
                depth_min_m=clean.depth_min_m,
                depth_max_m=clean.depth_max_m,
                tick_s=stored_tick_s,
            )
            # --- WO-081 begin: conflicts at filing (the plan is still accepted) ---
            cfg = self.conflict_config
            candidates = self._in_force_overlapping(session, start_at, end_at)
            found = detect_conflicts(
                _row_footprint(row, cfg),
                [_row_footprint(c, cfg) for c in candidates],
                self.harbor,
                cfg,
            )
            if found:
                row.status = PlanStatus.CONFLICTED.value
            session.add(row)
            if found:
                # Core statements on the session's connection: same transaction, no ORM
                # bookkeeping (the filing path must stay well under 200 ms for 40 plans)
                partners = {c.plan_id: c for c in candidates}
                records = []
                for conflict in found:
                    other = partners[conflict.other_plan_id]
                    records.append(_conflict_record(conflict, created_at))
                    records.append(_conflict_record(mirror(conflict, _tz_of(other)), created_at))
                conn = session.connection()
                conn.execute(_CONFLICT_TABLE.insert(), records)
                conn.execute(
                    update(_PLAN_TABLE)
                    .where(
                        _PLAN_TABLE.c.plan_id.in_(sorted({c.other_plan_id for c in found})),
                        _PLAN_TABLE.c.status == PlanStatus.ACTIVE.value,
                    )
                    .values(status=PlanStatus.CONFLICTED.value)
                )
            session.commit()
            # the new plan's conflicts are exactly ``found``, already in view order
            return PlanView.from_row(row, tuple(conflict_view(c) for c in found))
            # --- WO-081 end ---

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
                # WO-081: drop the plan's conflicts; partners left clear go back to active
                partners = self._delete_conflicts(session, [plan_id])
                session.flush()
                self._refresh_status(session, partners)
                session.commit()
            return self._view_of(session, row)

    def purge_older_than(self, days: int | None = None, *, now: datetime | None = None) -> int:
        """Delete plans whose ``end_at`` is more than ``days`` (default 30) before ``now``.

        This is the only real delete (CLAUDE.md section 6: no retention beyond 30 days).
        """
        span = days if days is not None else self.config.retention_days
        cutoff = to_utc((now if now is not None else self.now()) - timedelta(days=span))
        with self._session() as session:
            rows = session.exec(select(Plan).where(col(Plan.end_at) < cutoff)).all()
            # WO-081: conflict rows go with their plan (CLAUDE.md section 6)
            purged = sorted(row.plan_id for row in rows)
            partners = self._delete_conflicts(session, purged) - set(purged)
            for row in rows:
                session.delete(row)
            session.flush()
            self._refresh_status(session, partners)
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
            return None if row is None else self._view_of(session, row)

    def list(self, status: PlanStatus | str | None = None) -> builtins.list[PlanView]:
        """All plans (or those with ``status``), ordered by ``(start_at, plan_id)``.

        ``active`` means in force (active or conflicted, WO-081); ``conflicted`` returns
        only conflicted plans; ``"all"`` (like ``None``) returns every plan.
        """
        stmt = select(Plan)
        if status is not None and status != "all":  # WO-081: "all" is every plan
            wanted = PlanStatus(status)
            if wanted is PlanStatus.ACTIVE:
                stmt = stmt.where(col(Plan.status).in_(_IN_FORCE_VALUES))
            else:
                stmt = stmt.where(col(Plan.status) == wanted.value)
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
            stmt = stmt.where(col(Plan.status).in_(_IN_FORCE_VALUES))
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
            stmt = stmt.where(col(Plan.status).in_(_IN_FORCE_VALUES))
        stmt = stmt.order_by(col(Plan.start_at), col(Plan.plan_id))
        return self._views(stmt)

    def _views(self, stmt: Any) -> builtins.list[PlanView]:
        with self._session() as session:
            rows = session.exec(stmt).all()
            attached = self._conflict_views(session, [row.plan_id for row in rows])
            return [PlanView.from_row(row, attached.get(row.plan_id, ())) for row in rows]

    # --- WO-081 begin: conflicts ------------------------------------------------------------

    def conflicts_of(self, plan_id: str) -> builtins.list[ConflictView]:
        """The plan's conflicts, ordered by ``(other start_at, other_plan_id)``."""
        with self._session() as session:
            return builtins.list(self._conflict_views(session, [plan_id]).get(plan_id, ()))

    def set_conflict_resolution(
        self, plan_ids: Sequence[str], proposal_id: str
    ) -> builtins.list[PlanView]:
        """Mark every unresolved conflict among ``plan_ids`` resolved by ``proposal_id``.

        A plan is ``conflicted`` iff it keeps at least one unresolved conflict, so plans
        whose conflicts are all resolved return to ``active``. Returns the plans' views,
        ordered by ``(start_at, plan_id)``.
        """
        ids = sorted(set(plan_ids))
        with self._session() as session:
            rows = session.exec(
                select(PlanConflict).where(
                    col(PlanConflict.plan_id).in_(ids),
                    col(PlanConflict.other_plan_id).in_(ids),
                    col(PlanConflict.resolved_by).is_(None),
                )
            ).all()
            for row in rows:
                row.resolved_by = proposal_id
                session.add(row)
            session.flush()
            self._refresh_status(session, set(ids))
            session.commit()
        return self._views(self._by_ids(ids))

    def recompute_conflicts(self, plan_ids: Sequence[str]) -> builtins.list[PlanView]:
        """Re-run detection for ``plan_ids`` against every in-force plan (after a change).

        Old conflict rows touching these plans are dropped; plans are re-filed in
        ``(start_at, plan_id)`` order; statuses of the plans and of every former or new
        partner are refreshed. Returns the plans' views, ordered by ``(start_at, plan_id)``.
        """
        ids = sorted(set(plan_ids))
        cfg = self.conflict_config
        with self._session() as session:
            touched = self._delete_conflicts(session, ids) | set(ids)
            session.flush()
            in_force = session.exec(
                select(Plan)
                .where(col(Plan.status).in_(_IN_FORCE_VALUES))
                .order_by(col(Plan.start_at), col(Plan.plan_id))
            ).all()
            by_id = {row.plan_id: row for row in in_force}
            fps = {pid: footprint_of(PlanView.from_row(r), cfg) for pid, r in by_id.items()}
            seen: set[tuple[str, str]] = set()
            for pid in [r.plan_id for r in in_force if r.plan_id in set(ids)]:
                others = [fp for other, fp in fps.items() if other != pid]
                for conflict in detect_conflicts(fps[pid], others, self.harbor, cfg):
                    pair = (conflict.plan_id, conflict.other_plan_id)
                    if pair in seen:
                        continue
                    seen.add(pair)
                    seen.add((pair[1], pair[0]))
                    at = by_id[pid].created_at
                    session.add(_conflict_row(conflict, at))
                    other_row = by_id[conflict.other_plan_id]
                    session.add(_conflict_row(mirror(conflict, _tz_of(other_row)), at))
                    touched.add(conflict.other_plan_id)
            session.flush()
            self._refresh_status(session, touched)
            session.commit()
        return self._views(self._by_ids(ids))

    @staticmethod
    def _by_ids(ids: Sequence[str]) -> Any:
        return (
            select(Plan)
            .where(col(Plan.plan_id).in_(builtins.list(ids)))
            .order_by(col(Plan.start_at), col(Plan.plan_id))
        )

    @staticmethod
    def _in_force_overlapping(
        session: Session, start_at: datetime, end_at: datetime
    ) -> builtins.list[Plan]:
        """In-force rows whose closed window meets ``[start_at, end_at]`` (the pre-filter)."""
        lo, hi = to_utc(start_at), to_utc(end_at)
        stmt = (
            select(Plan)
            .where(
                col(Plan.start_at) <= hi,
                col(Plan.end_at) >= lo,
                col(Plan.status).in_(_IN_FORCE_VALUES),
            )
            .order_by(col(Plan.start_at), col(Plan.plan_id))
        )
        return builtins.list(session.exec(stmt).all())

    @staticmethod
    def _delete_conflicts(session: Session, plan_ids: Sequence[str]) -> set[str]:
        """Delete every conflict row touching ``plan_ids``; returns the partners' ids."""
        ids = builtins.list(plan_ids)
        if not ids:
            return set()
        rows = session.exec(
            select(PlanConflict).where(
                col(PlanConflict.plan_id).in_(ids) | col(PlanConflict.other_plan_id).in_(ids)
            )
        ).all()
        partners: set[str] = set()
        for row in rows:
            partners.add(row.plan_id)
            partners.add(row.other_plan_id)
            session.delete(row)
        return partners - set(ids)

    @staticmethod
    def _refresh_status(session: Session, plan_ids: set[str]) -> None:
        """In-force plans: ``conflicted`` iff an unresolved conflict row remains."""
        if not plan_ids:
            return
        ids = sorted(plan_ids)
        open_ids = set(
            session.exec(
                select(PlanConflict.plan_id).where(
                    col(PlanConflict.plan_id).in_(ids), col(PlanConflict.resolved_by).is_(None)
                )
            ).all()
        )
        for row in session.exec(select(Plan).where(col(Plan.plan_id).in_(ids))).all():
            if row.status not in _IN_FORCE_VALUES:
                continue
            wanted = PlanStatus.CONFLICTED if row.plan_id in open_ids else PlanStatus.ACTIVE
            if row.status != wanted.value:
                row.status = wanted.value
                session.add(row)

    @staticmethod
    def _conflict_views(
        session: Session, plan_ids: Sequence[str]
    ) -> dict[str, tuple[ConflictView, ...]]:
        """Conflict views per plan id, each ordered by ``(other start_at, other_plan_id)``."""
        ids = builtins.list(plan_ids)
        if not ids:
            return {}
        rows = session.exec(
            select(PlanConflict, Plan)
            .join(Plan, col(Plan.plan_id) == col(PlanConflict.other_plan_id))
            .where(col(PlanConflict.plan_id).in_(ids))
            .order_by(
                col(PlanConflict.plan_id), col(Plan.start_at), col(PlanConflict.other_plan_id)
            )
        ).all()
        if not rows:
            return {}
        offsets = dict(
            session.exec(
                select(Plan.plan_id, Plan.start_offset_min).where(col(Plan.plan_id).in_(ids))
            ).all()
        )
        out: dict[str, builtins.list[ConflictView]] = {}
        for c, _other in rows:
            minutes = offsets[c.plan_id]
            out.setdefault(c.plan_id, []).append(
                ConflictView(
                    other_plan_id=c.other_plan_id,
                    overlap_start=from_storage(c.overlap_start, minutes),
                    overlap_end=from_storage(c.overlap_end, minutes),
                    overlap_minutes=c.overlap_minutes,
                    reasons=tuple(c.reasons.split(",")),
                    resolved_by=c.resolved_by,
                )
            )
        return {k: tuple(v) for k, v in out.items()}

    def _view_of(self, session: Session, row: Plan) -> PlanView:
        attached = self._conflict_views(session, [row.plan_id])
        return PlanView.from_row(row, attached.get(row.plan_id, ()))

    # --- WO-081 end ---


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
                job_type=_scenario_job_type(p),  # WO-081
                tick_s=scenario.tick_s,  # WO-081
            )
        )
    return out


def _scenario_job_type(plan: Any) -> JobTypeName:
    """``RegistryPlan.job_type`` (WO-080 part A) or ``other`` when absent or None."""
    value = getattr(plan, "job_type", None)
    if value is None:
        return DEFAULT_JOB_TYPE
    text = str(getattr(value, "value", value))
    if text not in JOB_TYPE_NAMES:
        raise ValueError(f"unknown job type {text!r}")
    return cast(JobTypeName, text)


def plan_views_from_scenario(
    scenario: Scenario,
    config: RegistryConfig | None = None,
    *,
    harbor: Harbor | None = None,
    conflict_config: ConflictConfig | None = None,
) -> list[PlanView]:
    """The scenario's plans as in-force ``PlanView`` objects, without a database.

    Same content as seeding a fresh store and reading it back (``created_at`` in UTC,
    conflicts from ``conflicts_among`` in filing order), ordered by
    ``(start_tick, plan_id)``.
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
                job_type=p.job_type,
                depth_min_m=p.depth_min_m,
                depth_max_m=p.depth_max_m,
                tick_s=p.tick_s,
            )
        )
    # --- WO-081 begin: conflicts, as sequential create() would record them ---
    cfg = conflict_config if conflict_config is not None else load_conflict_config()
    found = conflicts_among([footprint_of(v, cfg) for v in views], harbor or _default_harbor(), cfg)
    per_plan: dict[str, list[Conflict]] = {}
    for conflict in found:
        per_plan.setdefault(conflict.plan_id, []).append(conflict)
    start_of = {v.plan_id: v.start_at for v in views}
    attached: list[PlanView] = []
    for v in views:
        mine = sorted(
            per_plan.get(v.plan_id, []), key=lambda c: (start_of[c.other_plan_id], c.other_plan_id)
        )
        if mine:
            v = v.model_copy(
                update={
                    "status": PlanStatus.CONFLICTED,
                    "conflicts": tuple(conflict_view(c) for c in mine),
                }
            )
        attached.append(v)
    # --- WO-081 end ---
    return sorted(attached, key=lambda v: (v.start_tick or 0, v.plan_id))


def _conflict_record(conflict: Conflict, detected_at: datetime) -> dict[str, Any]:
    """Column values for one direction of a conflict (WO-081)."""
    return {
        "plan_id": conflict.plan_id,
        "other_plan_id": conflict.other_plan_id,
        "overlap_start": to_utc(conflict.overlap_start),
        "overlap_end": to_utc(conflict.overlap_end),
        "overlap_minutes": conflict.overlap_minutes,
        "reasons": ",".join(sorted(r.value for r in conflict.reasons)),
        "resolved_by": None,
        "detected_at": to_utc(detected_at),
    }


def _conflict_row(conflict: Conflict, detected_at: datetime) -> PlanConflict:
    """Storage row for one direction of a conflict (WO-081)."""
    return PlanConflict(**_conflict_record(conflict, detected_at))


def _row_footprint(row: Plan, cfg: ConflictConfig) -> PlanFootprint:
    """``footprint_of`` straight from a stored row (no ``PlanView`` on the filing path)."""
    if row.depth_min_m is not None and row.depth_max_m is not None:
        lo, hi = float(row.depth_min_m), float(row.depth_max_m)
    else:
        lo, hi = cfg.depth_band(row.job_type)
    return PlanFootprint(
        plan_id=row.plan_id,
        area=row.area,
        start_at=from_storage(row.start_at, row.start_offset_min),
        end_at=from_storage(row.end_at, row.end_offset_min),
        depth_min_m=lo,
        depth_max_m=hi,
    )


def _tz_of(row: Plan) -> Any:
    """The filed offset of a stored plan's start, as a tzinfo."""
    return from_storage(row.start_at, row.start_offset_min).tzinfo


def overlaps(a_start: Any, a_end: Any, b_start: Any, b_end: Any) -> bool:
    """Closed-interval overlap, the rule the SQL queries implement."""
    return bool(a_start <= b_end and b_start <= a_end)
