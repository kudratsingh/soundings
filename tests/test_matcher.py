"""WO-022 matcher unit tests: the decision table, its edges, purity and registry_lookup.

Exit tests: ``pytest tests/test_matcher.py`` (table), ``-k extended`` (edges),
``-k pure`` (Hypothesis purity), ``-k registry_lookup`` (buckets).
"""

from __future__ import annotations

import builtins
import socket
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from soundings.canonical import round6
from soundings.challenge.hail import HailResult, hail_evidence_rows
from soundings.investigation.evidence import EvidenceTable, carry_over_reacquired
from soundings.investigation.features import track_segments
from soundings.matcher import (
    PLAN_POSITION_SOURCE_PREFIX,
    PLAN_SOURCE_PREFIX,
    EvidenceView,
    MatchCode,
    MatchConfig,
    MatchOutcome,
    PositionFix,
    distance_to_zone_m,
    evidence_view,
    load_match_config,
    match,
    registry_lookup,
    registry_lookup_rows,
)
from soundings.probe_keys import idempotency_key
from soundings.registry.models import PlanStatus, PlanView
from soundings.schemas import (
    EvidenceKind,
    FeatureName,
    HailReply,
    RegistryMatch,
    Track,
    TrackPoint,
    load_harbor,
    load_likelihood_spec,
)
from soundings.schemas.geo import from_local_m, to_local_m

HARBOR = load_harbor()
CFG = load_match_config()
TICK_S = 2.0

POS_ROW = "00000000000000a1"
HAIL_ROW = "00000000000000a2"
MODEM_ROW = "00000000000000a3"
OPERATOR_ROW = "00000000000000a4"
PLAN_ROW = "00000000000000a5"
HEARD_ROW = "00000000000000a6"

BASIN = "inner_basin_research"  # lat 33.737..33.744, lon -118.276..-118.266
IN_BASIN = (33.7405, -118.271)
EPOCH = datetime(2026, 10, 17, 10, 0, tzinfo=UTC)


def plan(
    plan_id: str = "P-T-001",
    modem: str = "MOD-3310",
    area: str = BASIN,
    start: int | None = 0,
    end: int | None = 600,
    status: PlanStatus = PlanStatus.ACTIVE,
) -> PlanView:
    return PlanView(
        plan_id=plan_id,
        operator="Test Lab (demo)",
        vehicle="AUV-Test",
        modem_id=modem,
        area=area,
        start_tick=start,
        end_tick=end,
        start_at=EPOCH + timedelta(seconds=(start or 0) * TICK_S),
        end_at=EPOCH + timedelta(seconds=(end or 0) * TICK_S),
        contact_channel="sim-phone-test",
        status=status,
    )


def view(
    lat: float = IN_BASIN[0],
    lon: float = IN_BASIN[1],
    tick: int = 100,
    *,
    reply: HailReply | None = None,
    modem: str | None = None,
    operator_known: bool | None = None,
    lineage_plan: str | None = None,
    last_heard: int | None = None,
    replied: bool = False,
) -> EvidenceView:
    return EvidenceView(
        contact_id="c-0001",
        tick=tick,
        tick_s=TICK_S,
        position=PositionFix(lat=lat, lon=lon, tick=tick, sigma_m=10.0, row_id=POS_ROW),
        hail_reply=reply,
        hail_row_id=HAIL_ROW if reply is not None else None,
        modem_id=modem,
        modem_row_id=MODEM_ROW if reply is not None else None,
        operator_known=operator_known,
        operator_row_id=OPERATOR_ROW if operator_known is not None else None,
        lineage_plan_id=lineage_plan,
        lineage_plan_row_id=PLAN_ROW if lineage_plan is not None else None,
        lineage_replied=replied,
        last_heard_tick=last_heard,
        last_heard_row_id=HEARD_ROW if last_heard is not None else None,
    )


def south_of_basin(d_m: float) -> tuple[float, float]:
    """A point ``d_m`` metres due south of the basin's south edge, mid-edge."""
    x, _ = to_local_m(33.737, -118.271)
    _, y = to_local_m(33.737, -118.271)
    return from_local_m(x, y - d_m)


def assert_well_formed(out: MatchOutcome, v: EvidenceView) -> None:
    assert isinstance(out.code, MatchCode)
    assert out.reason.strip()
    assert out.evidence_row_ids
    assert list(out.evidence_row_ids) == sorted(set(out.evidence_row_ids))
    assert set(out.evidence_row_ids) <= v.row_ids()


# --- the BUILD-PLAN table ------------------------------------------------------------------

FILED = [plan()]
TABLE = [
    (
        "filed_in_window_in_area_replies",
        view(reply=HailReply.VALID, modem="MOD-3310", operator_known=False),
        FILED,
        MatchCode.CLEARED_AT_ASK,
    ),
    (
        "filed_in_window_out_of_area",
        view(*south_of_basin(400.0), reply=HailReply.VALID, modem="MOD-3310"),
        FILED,
        MatchCode.MISMATCH_AREA,
    ),
    (
        "filed_gone_silent",
        view(
            tick=500,
            reply=HailReply.TIMEOUT,
            lineage_plan="P-T-001",
            last_heard=150,
            replied=True,
        ),
        FILED,
        MatchCode.LOST_VEHICLE,
    ),
    ("unfiled_no_reply", view(reply=HailReply.NONE), [], MatchCode.ESCALATE),
    (
        "unfiled_known_modem",
        view(reply=HailReply.VALID, modem="MOD-2290", operator_known=True),
        FILED,
        MatchCode.UNFILED_KNOWN_OPERATOR,
    ),
]


@pytest.mark.parametrize(("name", "v", "plans", "code"), TABLE, ids=[t[0] for t in TABLE])
def test_decision_table(name: str, v: EvidenceView, plans: list[PlanView], code: MatchCode) -> None:
    out = match(v, plans, v.tick, harbor=HARBOR, cfg=CFG)
    assert out.code is code, out.reason
    assert_well_formed(out, v)
    if code in (MatchCode.CLEARED_AT_ASK, MatchCode.MISMATCH_AREA, MatchCode.LOST_VEHICLE):
        assert out.plan_id == "P-T-001"


def test_lost_vehicle_reason_names_numbers() -> None:
    v = view(tick=500, reply=HailReply.TIMEOUT, lineage_plan="P-T-001", last_heard=150)
    out = match(v, FILED, 500, harbor=HARBOR, cfg=CFG)
    assert out.silence_s == 700.0
    assert "700 s > 600 s" in out.reason and HEARD_ROW in out.reason


def test_silence_threshold_comes_from_ladder_yaml(tmp_path: Path) -> None:
    ladder = Path("config/ladder.yaml").read_text(encoding="utf-8")
    alt = tmp_path / "ladder.yaml"
    alt.write_text(
        ladder.replace("lost_vehicle_silence_min: 10", "lost_vehicle_silence_min: 20"), "utf-8"
    )
    cfg = load_match_config(ladder_path=alt)
    assert cfg.lost_vehicle_silence_s == 1200.0
    assert CFG.lost_vehicle_silence_s == 600.0
    bad = tmp_path / "matcher.yaml"
    bad.write_text("lost_vehicle_silence_min: 5\n", "utf-8")
    with pytest.raises(ValueError, match=r"ladder\.yaml"):
        load_match_config(bad)


# --- extended: the table at its edges --------------------------------------------------------


def test_extended_mismatch_window() -> None:
    v = view(tick=700, reply=HailReply.VALID, modem="MOD-3310")
    out = match(v, FILED, 700, harbor=HARBOR, cfg=CFG)
    assert out.code is MatchCode.MISMATCH_WINDOW
    assert "0..600" in out.reason
    assert_well_formed(out, v)


def test_extended_invalid_reply_escalates() -> None:
    v = view(reply=HailReply.INVALID, modem=None)
    out = match(v, FILED, 100, harbor=HARBOR, cfg=CFG)
    assert out.code is MatchCode.ESCALATE
    assert_well_formed(out, v)


def test_extended_valid_unknown_modem_escalates() -> None:
    v = view(reply=HailReply.VALID, modem="MOD-9999", operator_known=False)
    out = match(v, FILED, 100, harbor=HARBOR, cfg=CFG)
    assert out.code is MatchCode.ESCALATE
    assert_well_formed(out, v)


def test_extended_area_boundary() -> None:
    tol = CFG.area_tolerance_m
    at = south_of_basin(tol)
    beyond = south_of_basin(tol + 1.0)
    assert distance_to_zone_m(HARBOR, BASIN, *at) == tol
    assert distance_to_zone_m(HARBOR, BASIN, *beyond) == tol + 1.0
    v_at = view(*at, reply=HailReply.VALID, modem="MOD-3310")
    v_beyond = view(*beyond, reply=HailReply.VALID, modem="MOD-3310")
    assert match(v_at, FILED, 100, harbor=HARBOR, cfg=CFG).code is MatchCode.CLEARED_AT_ASK
    out = match(v_beyond, FILED, 100, harbor=HARBOR, cfg=CFG)
    assert out.code is MatchCode.MISMATCH_AREA
    assert_well_formed(out, v_beyond)


@pytest.mark.parametrize(
    ("silence_ticks", "code"),
    [(300, MatchCode.PENDING_SILENCE), (301, MatchCode.LOST_VEHICLE)],
)
def test_extended_silence_boundary(silence_ticks: int, code: MatchCode) -> None:
    v = view(
        tick=100 + silence_ticks,
        reply=HailReply.NONE,
        lineage_plan="P-T-001",
        last_heard=100,
    )
    out = match(v, FILED, v.tick, harbor=HARBOR, cfg=CFG)
    assert out.code is code
    assert out.silence_s == silence_ticks * TICK_S  # 600 s pending, 602 s lost
    assert_well_formed(out, v)


def test_extended_window_ends_are_inclusive() -> None:
    for tick in (0, 600):
        v = view(tick=tick, reply=HailReply.VALID, modem="MOD-3310")
        assert match(v, FILED, tick, harbor=HARBOR, cfg=CFG).code is MatchCode.CLEARED_AT_ASK


def test_extended_expired_plan_is_not_filed() -> None:
    expired = [plan(status=PlanStatus.EXPIRED)]
    v = view(reply=HailReply.VALID, modem="MOD-3310", operator_known=False)
    assert match(v, expired, 100, harbor=HARBOR, cfg=CFG).code is MatchCode.ESCALATE


def test_extended_no_hail_and_no_lineage_escalates() -> None:
    v = view()
    out = match(v, FILED, 100, harbor=HARBOR, cfg=CFG)
    assert out.code is MatchCode.ESCALATE
    assert out.evidence_row_ids == (POS_ROW,)


def test_extended_filed_silent_without_last_heard_escalates() -> None:
    v = view(reply=HailReply.NONE, lineage_plan="P-T-001")
    out = match(v, FILED, 100, harbor=HARBOR, cfg=CFG)
    assert out.code is MatchCode.ESCALATE
    assert_well_formed(out, v)


def test_extended_outcome_model_rejects_bad_shapes() -> None:
    with pytest.raises(ValueError):
        MatchOutcome(code=MatchCode.ESCALATE, reason="x", evidence_row_ids=())
    with pytest.raises(ValueError):
        MatchOutcome(code=MatchCode.ESCALATE, reason=" ", evidence_row_ids=(POS_ROW,))
    with pytest.raises(ValueError):
        MatchOutcome(code=MatchCode.ESCALATE, reason="x", evidence_row_ids=(HAIL_ROW, POS_ROW))


# --- pure: Hypothesis over generated views and plans -----------------------------------------

ZONES = sorted(z.zone_id for z in HARBOR.zones)
MODEMS = ("MOD-3310", "MOD-4471", "MOD-2290", "MOD-9999")

plans_st = st.lists(
    st.builds(
        plan,
        plan_id=st.sampled_from(("P-A", "P-B", "P-C")),
        modem=st.sampled_from(MODEMS),
        area=st.sampled_from(ZONES),
        start=st.integers(0, 400),
        end=st.integers(400, 900),
        status=st.sampled_from(tuple(PlanStatus)),
    ),
    max_size=4,
    unique_by=lambda p: p.plan_id,
)


@st.composite
def views_st(draw: st.DrawFn) -> EvidenceView:
    reply = draw(st.one_of(st.none(), st.sampled_from(tuple(HailReply))))
    tick = draw(st.integers(0, 900))
    heard = draw(st.one_of(st.none(), st.integers(0, tick)))
    return view(
        lat=draw(st.floats(33.71, 33.77)),
        lon=draw(st.floats(-118.29, -118.23)),
        tick=tick,
        reply=reply,
        modem=draw(st.one_of(st.none(), st.sampled_from(MODEMS))) if reply else None,
        operator_known=draw(st.one_of(st.none(), st.booleans())) if reply else None,
        lineage_plan=draw(st.one_of(st.none(), st.sampled_from(("P-A", "P-B")))),
        last_heard=heard,
        replied=draw(st.booleans()),
    )


@settings(derandomize=True, max_examples=200, deadline=None)
@given(v=views_st(), plans=plans_st)
def test_pure_repeatable_and_inputs_unchanged(v: EvidenceView, plans: list[PlanView]) -> None:
    before = (v.model_dump(), [p.model_dump() for p in plans])
    first = match(v, plans, v.tick, harbor=HARBOR, cfg=CFG)
    second = match(v, plans, v.tick, harbor=HARBOR, cfg=CFG)
    assert first == second
    assert (v.model_dump(), [p.model_dump() for p in plans]) == before
    assert_well_formed(first, v)


def _forbidden(*_a: object, **_k: object) -> object:
    raise AssertionError("match touched I/O or the clock")


@settings(derandomize=True, max_examples=100, deadline=None)
@given(v=views_st(), plans=plans_st)
def test_pure_without_io_or_clock(v: EvidenceView, plans: list[PlanView]) -> None:
    expected = match(v, plans, v.tick, harbor=HARBOR, cfg=CFG)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(builtins, "open", _forbidden)
        mp.setattr(socket, "socket", _forbidden)
        mp.setattr(time, "time", _forbidden)
        got = match(v, plans, v.tick, harbor=HARBOR, cfg=CFG)
    assert got == expected


# --- registry_lookup ---------------------------------------------------------------------------


def test_registry_lookup_bucket_names_equal_likelihoods() -> None:
    spec = load_likelihood_spec()
    assert spec.features[FeatureName.REGISTRY_MATCH].buckets == tuple(
        m.value for m in RegistryMatch
    )


@settings(derandomize=True, max_examples=200, deadline=None)
@given(
    plans=plans_st,
    lat=st.floats(33.71, 33.77),
    lon=st.floats(-118.29, -118.23),
    tick=st.integers(0, 900),
)
def test_registry_lookup_position_only_never_area_mismatch(
    plans: list[PlanView], lat: float, lon: float, tick: int
) -> None:
    out = registry_lookup(plans, HARBOR, lat, lon, tick, CFG)
    assert out.match is not RegistryMatch.AREA_MISMATCH
    assert (out.plan_id is None) == (out.match is RegistryMatch.NONE)


def test_registry_lookup_position_only_buckets() -> None:
    lat, lon = IN_BASIN
    assert registry_lookup(FILED, HARBOR, lat, lon, 100, CFG).match is RegistryMatch.EXACT
    assert registry_lookup(FILED, HARBOR, lat, lon, 700, CFG).match is (
        RegistryMatch.WINDOW_MISMATCH
    )
    far = south_of_basin(1000.0)
    assert registry_lookup(FILED, HARBOR, *far, 100, CFG).match is RegistryMatch.NONE


def test_registry_lookup_with_modem_returns_all_four_buckets() -> None:
    lat, lon = IN_BASIN
    far = south_of_basin(400.0)
    got = {
        registry_lookup(FILED, HARBOR, lat, lon, 100, CFG, modem_id="MOD-3310").match,
        registry_lookup(FILED, HARBOR, *far, 100, CFG, modem_id="MOD-3310").match,
        registry_lookup(FILED, HARBOR, lat, lon, 700, CFG, modem_id="MOD-3310").match,
        registry_lookup(FILED, HARBOR, lat, lon, 100, CFG, modem_id="MOD-2290").match,
    }
    assert got == set(RegistryMatch)


def test_registry_lookup_prefers_in_window_plan_then_lowest_id() -> None:
    plans = [
        plan("P-B", start=0, end=50),
        plan("P-C", start=0, end=600),
        plan("P-A", start=0, end=600),
    ]
    out = registry_lookup(plans, HARBOR, *IN_BASIN, 100, CFG, modem_id="MOD-3310")
    assert (out.match, out.plan_id) == (RegistryMatch.EXACT, "P-A")


def test_registry_lookup_agrees_with_match_on_clearance() -> None:
    """CLEARED_AT_ASK holds exactly when a modem lookup is exact (the ladder guard)."""
    for lat, lon in (IN_BASIN, south_of_basin(150.0), south_of_basin(151.0)):
        for tick in (100, 700):
            v = view(lat, lon, tick, reply=HailReply.VALID, modem="MOD-3310")
            lk = registry_lookup(FILED, HARBOR, lat, lon, tick, CFG, modem_id="MOD-3310")
            out = match(v, FILED, tick, harbor=HARBOR, cfg=CFG)
            assert (out.code is MatchCode.CLEARED_AT_ASK) == (lk.match is RegistryMatch.EXACT)


# --- rows and the evidence view on a real table ------------------------------------------------


def _track(n: int = 12, start_tick: int = 10) -> Track:
    points = []
    for i in range(n):
        lat, lon = from_local_m(*to_local_m(*IN_BASIN))
        x, y = to_local_m(lat, lon)
        plat, plon = from_local_m(x + 2.0 * i, y)
        points.append(
            TrackPoint(
                tick=start_tick + i,
                lat=round6(plat),
                lon=round6(plon),
                depth_m=8.0,
                sigma_m=8.0,
                sensor_id="hyd_1",
                confidence=0.8,
            )
        )
    return Track(contact_id="c-0007", tick_s=TICK_S, points=tuple(points))


def _hail(tick: int, reply: HailReply, modem: str | None) -> HailResult:
    replied = reply in (HailReply.VALID, HailReply.INVALID)
    return HailResult(
        contact_id="c-0007",
        sent_tick=tick,
        reply_tick=tick + 10,
        replied=replied,
        hail_reply=reply,
        modem_id=modem if replied else None,
        operator_known=False,
        operator=None,
        idempotency_key=idempotency_key("c-0007", "acoustic_hail", {}, 0),
        cost_usd=0.01,
    )


def test_registry_lookup_rows_append_and_feed_the_view() -> None:
    track = _track()
    table = EvidenceTable()
    segs = track_segments(track)
    table.extend(list(dict.fromkeys(segs.values())))
    hail_rows = hail_evidence_rows(_hail(21, HailReply.VALID, "MOD-3310"), tick_s=TICK_S)
    table.extend(hail_rows)
    v = evidence_view(table, "c-0007", track, 31, TICK_S)
    assert v.hail_reply is HailReply.VALID and v.modem_id == "MOD-3310"
    assert v.lineage_replied and v.last_heard_tick == 31 and v.lineage_plan_id is None
    lk = registry_lookup(
        FILED, HARBOR, v.position.lat, v.position.lon, 31, CFG, modem_id="MOD-3310"
    )
    rows = registry_lookup_rows(
        lk, contact_id="c-0007", track_segment=table.get(v.position.row_id), hail_rows=hail_rows
    )
    table.extend(rows)
    reg, plan_row = rows
    assert reg.kind is EvidenceKind.DERIVED and reg.value == "exact"
    assert set(reg.derived_from) == {v.position.row_id, *(r.row_id for r in hail_rows)}
    assert plan_row.kind is EvidenceKind.FACT and plan_row.value == "P-T-001"
    assert plan_row.source == f"{PLAN_SOURCE_PREFIX}p-t-001"
    after = evidence_view(table, "c-0007", track, 31, TICK_S)
    assert after.lineage_plan_id == "P-T-001" and after.lineage_plan_row_id == plan_row.row_id


def test_position_only_plan_rows_do_not_make_a_lineage() -> None:
    track = _track()
    table = EvidenceTable()
    table.extend(list(dict.fromkeys(track_segments(track).values())))
    v = evidence_view(table, "c-0007", track, 21, TICK_S)
    lk = registry_lookup(FILED, HARBOR, v.position.lat, v.position.lon, 21, CFG)
    rows = registry_lookup_rows(lk, contact_id="c-0007", track_segment=table.get(v.position.row_id))
    table.extend(rows)
    assert rows[1].source.startswith(PLAN_POSITION_SOURCE_PREFIX)
    assert evidence_view(table, "c-0007", track, 21, TICK_S).lineage_plan_id is None


def test_evidence_view_reads_the_lineage_through_a_carry_over() -> None:
    old = _track()
    table = EvidenceTable()
    table.extend(list(dict.fromkeys(track_segments(old).values())))
    table.extend(hail_evidence_rows(_hail(15, HailReply.VALID, "MOD-3310"), tick_s=TICK_S))
    new_points = tuple(p.model_copy(update={"tick": p.tick + 40}) for p in old.points)
    new = Track(contact_id="c-0009", tick_s=TICK_S, points=new_points)
    carry_over_reacquired(table, "c-0007", "c-0009", t=50, match_confidence=0.8, reason="test")
    table.extend(list(dict.fromkeys(track_segments(new).values())))
    v = evidence_view(table, "c-0009", new, 61, TICK_S)
    assert v.reacquired_from == "c-0007"
    assert v.lineage_replied and v.last_heard_tick == 25
    assert v.position.tick == 61


def test_evidence_view_needs_a_segment_row() -> None:
    with pytest.raises(ValueError, match="track_segment"):
        evidence_view(EvidenceTable(), "c-0007", _track(), 30, TICK_S)


def test_match_config_defaults_equal_file() -> None:
    assert MatchConfig() == CFG
