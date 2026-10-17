"""WO-020 unit tests: the registry store, its rules, and the sim/live time bridge.

BUILD-PLAN Phase 2: create/read/expire plans; overlapping-window query returns correct
plans; plans outside the security zone are rejected.

Every test uses ``tmp_path / "reg.db"`` (or a numbered sibling) and a fixed clock, so no
test touches the developer's database or reads the wall clock.

Hard rules exercised: CLAUDE.md section 6 (declared security zones only, 30-day
retention) by ``-k reject`` and ``-k retention``; 2.5 (denylisted free text refused) by
``-k reject``.
"""

from __future__ import annotations

import itertools
import re
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from soundings.registry.models import (
    PlanCreate,
    PlanRejected,
    PlanSource,
    PlanStatus,
    PlanView,
    RejectReason,
    datetime_to_tick,
    load_registry_config,
    parse_utc_offset,
    scenario_epoch,
    tick_to_datetime,
)
from soundings.registry.store import (
    DEFAULT_DB_PATH,
    RegistryStore,
    db_path_from_env,
    overlaps,
)
from soundings.schemas import DENYLIST, load_harbor
from soundings.schemas._base import REPO_ROOT
from soundings.schemas.scenario import load_scenario

NOW = datetime(2026, 10, 17, 20, 0, 0, tzinfo=UTC)
PDT = timezone(timedelta(hours=-7))
S02_PATH = REPO_ROOT / "scenarios" / "S02-filed-vehicle-gone-silent.yaml"
HARBOR = load_harbor()


def fixed_now() -> datetime:
    return NOW


@pytest.fixture
def store(tmp_path: Path) -> RegistryStore:
    return RegistryStore(tmp_path / "reg.db", harbor=HARBOR, now=fixed_now)


def plan_data(**overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "operator": "Harbor Survey Co (demo)",
        "vehicle": "ROV-Blue-1",
        "modem_id": "MOD-1234",
        "area": "inner_basin_research",
        "start_tick": 100,
        "end_tick": 200,
        "start_at": datetime(2026, 10, 17, 14, 0, tzinfo=PDT),
        "end_at": datetime(2026, 10, 17, 16, 0, tzinfo=PDT),
        "contact_channel": "radio ch 16",
    }
    data.update(overrides)
    return data


def _sample_from_pattern(pattern: str) -> str:
    """A word matching a denylist pattern, built at runtime (no literal in this file)."""
    s = pattern.replace(r"\b", "")
    s = re.sub(r"\([^()]*\)\?", "", s)
    s = re.sub(r"\[([^\]])[^\]]*\]", r"\1", s)
    s = re.sub(r"\(([^()|]*)(?:\|[^()]*)?\)", r"\1", s)
    s = re.sub(r".\?", "", s)
    return s


# --------------------------------------------------------------- BUILD-PLAN items


def test_buildplan_create_read_expire_plans(store: RegistryStore) -> None:
    created = store.create(plan_data(plan_id="P-BP-001"))
    assert store.get("P-BP-001") == created
    expired = store.expire("P-BP-001")
    assert expired.status is PlanStatus.EXPIRED
    assert store.get("P-BP-001") == expired


def test_buildplan_overlapping_window_query_returns_correct_plans(store: RegistryStore) -> None:
    store.create(plan_data(plan_id="P-A", start_tick=0, end_tick=99))
    store.create(plan_data(plan_id="P-B", start_tick=100, end_tick=200))
    store.create(plan_data(plan_id="P-C", start_tick=201, end_tick=300))
    assert [p.plan_id for p in store.overlapping_ticks(150, 200)] == ["P-B"]
    assert [p.plan_id for p in store.overlapping_ticks(99, 201)] == ["P-A", "P-B", "P-C"]


def test_buildplan_plans_outside_the_security_zone_are_rejected(store: RegistryStore) -> None:
    with pytest.raises(PlanRejected) as info:
        store.create(plan_data(area="open_ocean"))
    assert info.value.reason is RejectReason.AREA_NOT_SECURITY_ZONE
    assert store.count() == 0


# --------------------------------------------------------------- crud


def test_crud_lifecycle(store: RegistryStore) -> None:
    data = plan_data()
    view = store.create(data)
    assert isinstance(view, PlanView)
    assert view.plan_id == "P-20261017-001"  # assigned: prefix, filed start date, sequence
    for key, value in data.items():
        assert getattr(view, key) == value, key
    assert view.start_at.utcoffset() == timedelta(hours=-7)  # offset kept as filed
    assert view.status is PlanStatus.ACTIVE
    assert view.source is PlanSource.API
    assert view.created_at == NOW
    assert view.expired_at is None

    assert store.get(view.plan_id) == view
    assert store.get("P-missing") is None
    assert store.list(status=PlanStatus.ACTIVE) == [view]
    assert store.list(status="expired") == []
    assert store.overlapping_ticks(150, 150) == [view]
    assert store.overlapping_times(view.start_at, view.start_at) == [view]

    at = NOW + timedelta(minutes=5)
    expired = store.expire(view.plan_id, at=at)
    assert expired.status is PlanStatus.EXPIRED
    assert expired.expired_at == at
    assert store.list(status=PlanStatus.ACTIVE) == []
    assert store.list(status=PlanStatus.EXPIRED) == [expired]
    assert store.overlapping_ticks(150, 150) == []
    assert store.overlapping_times(view.start_at, view.end_at) == []
    assert store.overlapping_ticks(150, 150, include_expired=True) == [expired]
    assert store.overlapping_times(view.start_at, view.end_at, include_expired=True) == [expired]

    # Expiry is idempotent and keeps the first expired_at; unknown ids raise KeyError.
    assert store.expire(view.plan_id, at=at + timedelta(hours=1)) == expired
    with pytest.raises(KeyError):
        store.expire("P-missing")


def test_crud_assigned_ids_are_sequential_per_date(store: RegistryStore) -> None:
    a = store.create(plan_data())
    b = store.create(plan_data())
    nxt = plan_data(
        start_at=datetime(2026, 10, 18, 9, 0, tzinfo=PDT),
        end_at=datetime(2026, 10, 18, 10, 0, tzinfo=PDT),
    )
    c = store.create(nxt)
    assert [a.plan_id, b.plan_id, c.plan_id] == [
        "P-20261017-001",
        "P-20261017-002",
        "P-20261018-001",
    ]


def test_crud_window_derivation(store: RegistryStore) -> None:
    epoch = datetime(2026, 10, 17, 14, 0, tzinfo=PDT)
    tick_only = store.create(
        plan_data(plan_id="P-TICKS", start_at=None, end_at=None, start_tick=10, end_tick=20),
        epoch=epoch,
        tick_s=2.0,
    )
    assert tick_only.start_at == epoch + timedelta(seconds=20)
    assert tick_only.end_at == epoch + timedelta(seconds=40)

    live = store.create(plan_data(plan_id="P-LIVE", start_tick=None, end_tick=None))
    assert (live.start_tick, live.end_tick) == (None, None)
    assert store.overlapping_ticks(0, 10_000) == [tick_only]  # live-only plans never tick-match

    bridged = store.create(
        plan_data(plan_id="P-BRIDGED", start_tick=None, end_tick=None), epoch=epoch, tick_s=2.0
    )
    assert (bridged.start_tick, bridged.end_tick) == (0, 3600)


def test_crud_accepts_plan_create_models(store: RegistryStore) -> None:
    model = PlanCreate.model_validate(plan_data(plan_id="P-MODEL"))
    assert store.create(model).plan_id == "P-MODEL"


# --------------------------------------------------------------- overlap


def _brute_force(windows: list[tuple[str, int, int]], lo: int, hi: int) -> list[tuple[int, str]]:
    return sorted((s, pid) for pid, s, e in windows if overlaps(s, e, lo, hi))


_db_counter = itertools.count()


@st.composite
def tick_cases(draw: st.DrawFn) -> tuple[list[tuple[str, int, int]], list[bool], int, int]:
    n = draw(st.integers(min_value=0, max_value=30))
    ids = draw(st.permutations([f"P-T-{i:02d}" for i in range(n)]))
    windows = []
    for pid in ids:
        start = draw(st.integers(min_value=0, max_value=120))
        length = draw(st.integers(min_value=0, max_value=40))
        windows.append((pid, start, start + length))
    expired = draw(st.lists(st.booleans(), min_size=n, max_size=n))
    q_start = draw(st.integers(min_value=0, max_value=170))
    q_len = draw(st.integers(min_value=0, max_value=40))
    return windows, expired, q_start, q_start + q_len


@settings(
    derandomize=True,
    max_examples=200,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)
@given(case=tick_cases())
def test_overlap_property(
    tmp_path: Path, case: tuple[list[tuple[str, int, int]], list[bool], int, int]
) -> None:
    windows, expired, lo, hi = case
    store = RegistryStore(tmp_path / f"reg-{next(_db_counter)}.db", harbor=HARBOR, now=fixed_now)
    try:
        for pid, s, e in windows:
            store.create(plan_data(plan_id=pid, start_tick=s, end_tick=e))
        gone = {pid for (pid, _, _), x in zip(windows, expired, strict=True) if x}
        for pid in sorted(gone):
            store.expire(pid)
        active = [w for w in windows if w[0] not in gone]

        got_all = store.overlapping_ticks(lo, hi, include_expired=True)
        assert [(p.start_tick, p.plan_id) for p in got_all] == _brute_force(windows, lo, hi)
        got_active = store.overlapping_ticks(lo, hi)
        assert [(p.start_tick, p.plan_id) for p in got_active] == _brute_force(active, lo, hi)
    finally:
        store.close()


def test_overlap_touching_endpoints_count(store: RegistryStore) -> None:
    store.create(plan_data(plan_id="P-EDGE", start_tick=100, end_tick=200))
    assert [p.plan_id for p in store.overlapping_ticks(200, 300)] == ["P-EDGE"]
    assert [p.plan_id for p in store.overlapping_ticks(0, 100)] == ["P-EDGE"]
    assert store.overlapping_ticks(201, 300) == []
    assert store.overlapping_ticks(0, 99) == []
    with pytest.raises(ValueError):
        store.overlapping_ticks(5, 4)


OFFSETS = [
    timezone(timedelta(hours=-7)),
    UTC,
    timezone(timedelta(hours=5, minutes=30)),
    timezone(timedelta(hours=-10)),
    timezone(timedelta(hours=14)),
]
# 23:00 at -07:00 is 06:00 UTC the next day: windows straddle a date and offset boundary.
BASE = datetime(2026, 10, 17, 23, 0, tzinfo=PDT)


@st.composite
def time_cases(
    draw: st.DrawFn,
) -> tuple[list[tuple[str, datetime, datetime]], datetime, datetime]:
    n = draw(st.integers(min_value=0, max_value=30))
    ids = draw(st.permutations([f"P-Z-{i:02d}" for i in range(n)]))
    windows = []
    for pid in ids:
        start = BASE + timedelta(minutes=draw(st.integers(min_value=-180, max_value=180)))
        end = start + timedelta(minutes=draw(st.integers(min_value=0, max_value=120)))
        windows.append(
            (
                pid,
                start.astimezone(draw(st.sampled_from(OFFSETS))),
                end.astimezone(draw(st.sampled_from(OFFSETS))),
            )
        )
    q_start = BASE + timedelta(minutes=draw(st.integers(min_value=-240, max_value=240)))
    q_end = q_start + timedelta(minutes=draw(st.integers(min_value=0, max_value=120)))
    return (
        windows,
        q_start.astimezone(draw(st.sampled_from(OFFSETS))),
        q_end.astimezone(draw(st.sampled_from(OFFSETS))),
    )


@settings(
    derandomize=True,
    max_examples=200,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)
@given(case=time_cases())
def test_overlap_times_property(
    tmp_path: Path, case: tuple[list[tuple[str, datetime, datetime]], datetime, datetime]
) -> None:
    windows, lo, hi = case
    store = RegistryStore(tmp_path / f"reg-{next(_db_counter)}.db", harbor=HARBOR, now=fixed_now)
    try:
        for pid, s, e in windows:
            store.create(
                plan_data(plan_id=pid, start_tick=None, end_tick=None, start_at=s, end_at=e)
            )
        expected = sorted((s, pid) for pid, s, e in windows if overlaps(s, e, lo, hi))
        got = store.overlapping_times(lo, hi)
        assert [(p.start_at, p.plan_id) for p in got] == expected
        filed = {pid: (s, e) for pid, s, e in windows}
        for p in got:  # the view hands back each datetime with the offset it was filed with
            assert p.start_at.utcoffset() == filed[p.plan_id][0].utcoffset()
            assert p.end_at.utcoffset() == filed[p.plan_id][1].utcoffset()
    finally:
        store.close()


def test_overlap_times_across_offsets_example(store: RegistryStore) -> None:
    # 16:30-17:30 at -07:00 is 23:30-00:30 UTC; a UTC query on the next date must find it.
    store.create(
        plan_data(
            plan_id="P-NIGHT",
            start_at=datetime(2026, 10, 17, 16, 30, tzinfo=PDT),
            end_at=datetime(2026, 10, 17, 17, 30, tzinfo=PDT),
        )
    )
    q = datetime(2026, 10, 18, 0, 30, tzinfo=UTC)
    assert [p.plan_id for p in store.overlapping_times(q, q)] == ["P-NIGHT"]
    q2 = datetime(2026, 10, 18, 0, 31, tzinfo=UTC)
    assert store.overlapping_times(q2, q2) == []
    with pytest.raises(ValueError):
        store.overlapping_times(datetime(2026, 10, 18, 0, 0), datetime(2026, 10, 18, 1, 0))


# --------------------------------------------------------------- reject


def _denylisted_word() -> str:
    return _sample_from_pattern(DENYLIST[0].pattern)


REJECT_CASES: list[tuple[str, dict[str, Any], RejectReason]] = [
    ("open_ocean", {"area": "open_ocean"}, RejectReason.AREA_NOT_SECURITY_ZONE),
    ("empty_area", {"area": ""}, RejectReason.AREA_NOT_SECURITY_ZONE),
    ("asset_not_zone", {"area": "fuel_berth_1"}, RejectReason.AREA_NOT_SECURITY_ZONE),
    (
        "no_window",
        {"start_tick": None, "end_tick": None, "start_at": None, "end_at": None},
        RejectReason.WINDOW_MISSING,
    ),
    ("half_tick_window", {"end_tick": None}, RejectReason.WINDOW_MISSING),
    ("half_time_window", {"start_at": None}, RejectReason.WINDOW_MISSING),
    (
        "tick_only_without_epoch",
        {"start_at": None, "end_at": None},
        RejectReason.WINDOW_MISSING,
    ),
    ("ticks_inverted", {"start_tick": 300, "end_tick": 200}, RejectReason.WINDOW_INVERTED),
    (
        "times_inverted",
        {
            "start_at": datetime(2026, 10, 17, 16, 0, tzinfo=PDT),
            "end_at": datetime(2026, 10, 17, 22, 59, tzinfo=UTC),  # 15:59 PDT
        },
        RejectReason.WINDOW_INVERTED,
    ),
    # WO-081: new reason, checked after WINDOW_INVERTED
    (
        "depth_inverted",
        {"depth_min_m": 20.0, "depth_max_m": 5.0},
        RejectReason.DEPTH_BAND_INVERTED,
    ),
    ("depth_half_band", {"depth_min_m": 5.0}, RejectReason.DEPTH_BAND_INVERTED),
    # WO-082: start bounds, checked after the depth band
    (
        "start_bounds_inverted",
        {
            "earliest_start_at": datetime(2026, 10, 17, 15, 0, tzinfo=PDT),
            "latest_start_at": datetime(2026, 10, 17, 14, 0, tzinfo=PDT),
        },
        RejectReason.START_BOUNDS_INVALID,
    ),
    (
        "start_after_latest",
        {"latest_start_at": datetime(2026, 10, 17, 13, 0, tzinfo=PDT)},
        RejectReason.START_BOUNDS_INVALID,
    ),
    (
        "start_before_earliest",
        {"earliest_start_at": datetime(2026, 10, 17, 14, 15, tzinfo=PDT)},
        RejectReason.START_BOUNDS_INVALID,
    ),
    ("bad_modem", {"modem_id": "4471"}, RejectReason.BAD_MODEM_ID),
    ("bad_modem_short", {"modem_id": "MOD-12"}, RejectReason.BAD_MODEM_ID),
    ("duplicate", {"plan_id": "P-EXISTING"}, RejectReason.DUPLICATE_PLAN_ID),
    ("denylisted_operator", {"operator": "__DENY__"}, RejectReason.DENYLISTED_TEXT),
    ("denylisted_channel", {"contact_channel": "Ch 16 __DENY__"}, RejectReason.DENYLISTED_TEXT),
]


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [(o, r) for _, o, r in REJECT_CASES],
    ids=[name for name, _, _ in REJECT_CASES],
)
def test_reject(store: RegistryStore, overrides: dict[str, Any], reason: RejectReason) -> None:
    store.create(plan_data(plan_id="P-EXISTING"))
    word = _denylisted_word()
    fixed = {
        k: v.replace("__DENY__", word.upper()) if isinstance(v, str) else v
        for k, v in overrides.items()
    }
    before = store.count()
    with pytest.raises(PlanRejected) as info:
        store.create(plan_data(**fixed))
    assert info.value.reason is reason
    assert info.value.detail
    assert store.count() == before == 1


def test_reject_cases_cover_every_reason() -> None:
    assert {r for _, _, r in REJECT_CASES} == set(RejectReason)


def test_reject_order_area_first(store: RegistryStore) -> None:
    bad = plan_data(area="open_ocean", modem_id="nope", start_tick=9, end_tick=1)
    with pytest.raises(PlanRejected) as info:
        store.create(bad)
    assert info.value.reason is RejectReason.AREA_NOT_SECURITY_ZONE
    for zone_id in sorted(z.zone_id for z in HARBOR.zones):
        assert zone_id in info.value.detail
    assert store.count() == 0


def test_reject_denylisted_text_in_plan_create_model() -> None:
    with pytest.raises(ValueError, match="denylisted"):
        PlanCreate.model_validate(plan_data(vehicle=_denylisted_word()))


# --------------------------------------------------------------- env_path


def test_env_path_from_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "nested" / "env.db"
    monkeypatch.setenv("SOUNDINGS_DB", str(target))
    assert db_path_from_env() == target
    store = RegistryStore(harbor=HARBOR, now=fixed_now)
    try:
        assert store.db_path == target
        assert target.is_file()
        store.create(plan_data(plan_id="P-ENV"))
        assert RegistryStore(harbor=HARBOR, now=fixed_now).get("P-ENV") is not None
    finally:
        store.close()


def test_env_path_default_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SOUNDINGS_DB", raising=False)
    assert db_path_from_env() == Path("soundings.db") == DEFAULT_DB_PATH
    monkeypatch.setenv("SOUNDINGS_DB", "  ")
    assert db_path_from_env() == DEFAULT_DB_PATH


# --------------------------------------------------------------- retention


def test_retention_30_day_line(store: RegistryStore) -> None:
    def ending(days: int, pid: str) -> dict[str, Any]:
        end = NOW - timedelta(days=days)
        return plan_data(plan_id=pid, start_at=end - timedelta(hours=2), end_at=end)

    store.create(ending(31, "P-OLD"))
    store.create(ending(29, "P-RECENT"))
    assert load_registry_config().retention_days == 30
    assert store.purge_older_than() == 1  # default: the 30 days in config/registry.yaml
    assert store.get("P-OLD") is None
    assert store.get("P-RECENT") is not None
    assert store.purge_older_than(days=30, now=NOW) == 0


# --------------------------------------------------------------- epoch


def test_epoch_and_tick_bridge() -> None:
    s02 = load_scenario(S02_PATH)
    assert s02.hour_of_day == 14
    epoch = scenario_epoch(s02)
    assert epoch == datetime(2026, 10, 17, 14, 0, tzinfo=PDT)
    assert epoch.isoformat() == "2026-10-17T14:00:00-07:00"
    for tick in (0, 1, 899):
        dt = tick_to_datetime(epoch, tick, 2)
        assert dt == epoch + timedelta(seconds=2 * tick)
        assert datetime_to_tick(epoch, dt, 2) == tick
        assert datetime_to_tick(epoch, dt + timedelta(seconds=1.999), 2) == tick  # floor
        assert datetime_to_tick(epoch, dt.astimezone(UTC), 2) == tick
    assert datetime_to_tick(epoch, epoch - timedelta(seconds=1), 2) == -1


def test_epoch_config_values() -> None:
    cfg = load_registry_config()
    assert cfg.modem_id_pattern == "^MOD-[0-9]{4,6}$"
    assert cfg.sim_epoch_date.isoformat() == "2026-10-17"
    assert cfg.epoch_tz == PDT == parse_utc_offset("-07:00")
    assert cfg.plan_id_prefix == "P"
    with pytest.raises(ValueError):
        parse_utc_offset("PDT")
