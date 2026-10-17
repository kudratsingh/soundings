"""WO-031: the allowlisted, costed, idempotent probes (SPEC-INVESTIGATION section 5)."""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

import pytest

import soundings.probe_keys as probe_keys
from soundings.challenge.hail import HailSimulator
from soundings.investigation import probes as P
from soundings.investigation.evidence import EvidenceTable
from soundings.investigation.features import build_evidence
from soundings.investigation.pol import load_pol_table
from soundings.investigation.probes import (
    ALLOWLIST,
    ProbeCache,
    ProbeContext,
    ProbeNotAllowed,
    ProbeResult,
    idempotency_key,
    run_probe,
    tick_bucket,
)
from soundings.registry.store import plan_views_from_scenario
from soundings.schemas._base import from_file_data
from soundings.schemas.config import load_ladder_config
from soundings.schemas.enums import (
    EvidenceKind,
    FeatureName,
    LadderState,
    ProbeId,
    Rung,
    ToggleName,
)
from soundings.schemas.evidence import UNKNOWN, EvidenceRow
from soundings.schemas.harbor import Harbor, load_harbor
from soundings.schemas.scenario import Scenario, Toggle
from soundings.sim.current import CurrentField, load_current_field
from soundings.sim.run import run_scenario
from soundings.sim.scenario import resolve_scenario
from soundings.sim.types import SimRun

REPO = Path(__file__).resolve().parent.parent
PROBES_PY = REPO / "soundings" / "investigation" / "probes.py"
PLANNER_PY = REPO / "soundings" / "investigation" / "planner.py"

# SPEC-INVESTIGATION section 5, copied by hand: (rung, cost_usd, latency_s, interruption).
SECTION_5: dict[str, tuple[str, float, float, int]] = {
    "registry_lookup": ("watch", 0.0, 0.0, 0),
    "kinematics_profile": ("watch", 0.0, 0.0, 0),
    "drift_check": ("watch", 0.0, 0.0, 0),
    "pattern_of_life_check": ("watch", 0.0, 0.0, 0),
    "ais_surface_correlate": ("watch", 0.0, 1.0, 0),
    "acoustic_feature_read": ("watch", 0.0, 2.0, 0),
    "acoustic_hail": ("ask", 0.01, 20.0, 0),
    "camera_cue": ("ask", 0.05, 30.0, 0),
    "second_sensor_dwell": ("ask", 0.10, 60.0, 0),
    "ask_operator_by_phone": ("ask", 0.0, 120.0, 0),
    "request_watch_officer_look": ("ask", 0.0, 60.0, 1),
}
ASK_PROBES = [pid for pid, spec in ALLOWLIST.items() if spec.rung_required is Rung.ASK]


# --- fixtures -----------------------------------------------------------------------------


@lru_cache(maxsize=1)
def _harbor() -> Harbor:
    return load_harbor()


@lru_cache(maxsize=1)
def _field() -> CurrentField:
    return load_current_field()


@lru_cache(maxsize=4)
def _world(scenario_id: str, seed: int, ticks: int) -> tuple[Scenario, SimRun]:
    scenario = resolve_scenario(scenario_id, _harbor())
    return scenario, run_scenario(scenario, seed=seed, ticks=ticks, harbor=_harbor())


def _main_contact(run: SimRun) -> str:
    ids = [c for c, t in run.contact_truth.items() if t.gt_id == "gt-1"]
    return sorted(ids, key=lambda c: (-len(run.tracks[c].points), c))[0]


def _table(scenario: Scenario, run: SimRun) -> EvidenceTable:
    field = _field()
    return build_evidence(
        run,
        _harbor(),
        field,
        load_pol_table(),
        hour_of_day=scenario.hour_of_day,
        tide_phase_for_hour=field.phase_for_hour,
    )


def _ctx(
    scenario: Scenario,
    run: SimRun,
    *,
    rung: Rung = Rung.ASK,
    tick: int | None = None,
    contact_id: str | None = None,
) -> ProbeContext:
    cid = contact_id if contact_id is not None else _main_contact(run)
    last = run.tracks[cid].points[-1].tick if cid in run.tracks else 0
    return ProbeContext(
        run=run,
        scenario=scenario,
        harbor=_harbor(),
        current_field=_field(),
        pol=load_pol_table(),
        registry=plan_views_from_scenario(scenario),
        tick=last if tick is None else tick,
        current_rung=rung,
        contact_id=cid,
    )


def _s05() -> tuple[Scenario, SimRun]:
    return _world("S05", 11, 240)


def _s02() -> tuple[Scenario, SimRun]:
    return _world("S02", 1, 200)


# --- allowlist ------------------------------------------------------------------------------


def test_allowlist_exact() -> None:
    assert list(ALLOWLIST) == list(ProbeId)
    # WO-088: section 5 first, in section 5 order, then the two responder probes (rung ask).
    assert [p.value for p in P.SECTION_5_ALLOWLIST] == list(SECTION_5)
    assert [p.value for p in ALLOWLIST] == [*SECTION_5, "responder_look", "responder_search_box"]
    for pid in P.RESPONDER_PROBES:
        assert ALLOWLIST[pid].rung_required is Rung.ASK and ALLOWLIST[pid].cost_usd == 0.0
    for pid, spec in P.SECTION_5_ALLOWLIST.items():
        rung, cost, latency, interruption = SECTION_5[pid.value]
        assert spec.probe_id is pid
        assert spec.rung_required.value == rung, pid
        assert spec.cost_usd == cost, pid
        assert spec.latency_s == latency, pid
        assert spec.interruption == interruption, pid
        assert spec.paid == (cost > 0)


def test_allowlist_every_probe_has_cost_latency_returns() -> None:
    for pid, spec in ALLOWLIST.items():
        assert isinstance(spec.cost_usd, float) and spec.cost_usd >= 0, pid
        assert isinstance(spec.latency_s, float) and spec.latency_s >= 0, pid
        assert spec.returns and all(isinstance(f, FeatureName) for f in spec.returns), pid
        assert 0.0 <= spec.nominal_confidence <= 1.0
    assert ALLOWLIST[ProbeId.ACOUSTIC_HAIL].returns == (
        FeatureName.HAIL_SENT_AT,
        FeatureName.HAIL_REPLY,
        FeatureName.MODEM_ID,
        FeatureName.OPERATOR_KNOWN,
    )
    assert ALLOWLIST[ProbeId.REGISTRY_LOOKUP].returns == (
        FeatureName.REGISTRY_MATCH,
        FeatureName.PLAN_ID,
    )


def test_nothing_at_or_above_slow() -> None:
    ladder = load_ladder_config()
    rungs = [r.value for r in ladder.rungs]
    slow = rungs.index("slow")
    for pid, spec in ALLOWLIST.items():
        assert rungs.index(spec.rung_required.value) < slow, pid
        assert P.rung_index(spec.rung_required) < slow, pid
    # No probe is named like a rung, a ladder state, or an action at or above slow.
    names = {p.value.lower() for p in ALLOWLIST}
    assert not names & {r.lower() for r in rungs}
    assert not names & {s.value.lower() for s in LadderState}
    above: set[str] = set()
    for rung in ladder.rungs[slow:]:
        cell = ladder.boundary[rung]
        above |= set(cell.platform_alone) | set(cell.standing_orders) | set(cell.person_approves)
    assert above and not names & above


def test_rung_index_reads_config(tmp_path: Path) -> None:
    assert [P.rung_index(r) for r in Rung] == [0, 1, 2, 3, 4]
    text = (REPO / "config" / "ladder.yaml").read_text(encoding="utf-8")
    tmp = tmp_path / "ladder.yaml"
    tmp.write_text(text, encoding="utf-8")
    assert P.rung_index(Rung.ASK, ladder_path=tmp) == 1


# --- idempotency -----------------------------------------------------------------------------


def test_idempotency_key_reexported() -> None:
    assert P.idempotency_key is probe_keys.idempotency_key
    assert P.tick_bucket is probe_keys.tick_bucket
    assert P.TICK_BUCKET_S == probe_keys.TICK_BUCKET_S == 30


def test_idempotency_key_buckets_and_args() -> None:
    k = idempotency_key("c-0001", "drift_check", {"a": 1}, tick_bucket(0, 2.0))
    assert re.fullmatch(r"[0-9a-f]{64}", k)
    assert k == idempotency_key("c-0001", "drift_check", {"a": 1}, tick_bucket(14, 2.0))
    assert k != idempotency_key("c-0001", "drift_check", {"a": 2}, tick_bucket(14, 2.0))
    assert k != idempotency_key("c-0001", "drift_check", {"a": 1}, tick_bucket(15, 2.0))
    assert k != idempotency_key("c-0002", "drift_check", {"a": 1}, tick_bucket(0, 2.0))
    assert k != idempotency_key("c-0001", "camera_cue", {"a": 1}, tick_bucket(0, 2.0))
    assert idempotency_key("c-0001", "x", None, 3) == idempotency_key("c-0001", "x", {}, 3)


def test_idempotency_hail_key_matches_simulator() -> None:
    scenario, run = _s05()
    ctx = _ctx(scenario, run)
    hail = HailSimulator(scenario, run, seed=run.seed)
    table = _table(scenario, run)
    assert P.probe_key(ProbeId.ACOUSTIC_HAIL, ctx, table) == hail.key(ctx.contact_id, ctx.tick)


@pytest.mark.parametrize("probe_id", list(ProbeId))
def test_idempotency_second_call_is_cached(probe_id: ProbeId) -> None:
    scenario, run = _s05()
    table = _table(scenario, run)
    cache = ProbeCache()
    ctx = _ctx(scenario, run)
    first = run_probe(probe_id, ctx, table, cache=cache)
    n_rows = len(table)
    assert first.cached is False and cache.count(probe_id) == 1
    second = run_probe(probe_id, ctx, table, cache=cache)
    assert second.cached is True
    assert second.key == first.key and second.rows == first.rows
    assert cache.count(probe_id) == 1 and cache.count() == 1
    assert len(table) == n_rows
    # Another tick in the same 30 s bucket is the same key; the next bucket is not.
    bucket = tick_bucket(ctx.tick, run.tick_s)
    same = next(t for t in range(ctx.tick - 14, ctx.tick + 15) if t != ctx.tick and t >= 0
                and tick_bucket(t, run.tick_s) == bucket)  # fmt: skip
    third = run_probe(probe_id, _ctx(scenario, run, tick=same), table, cache=cache)
    assert third.cached is True and cache.count(probe_id) == 1 and len(table) == n_rows
    later = run_probe(probe_id, _ctx(scenario, run, tick=ctx.tick + 15), table, cache=cache)
    assert later.cached is False and later.key != first.key and cache.count(probe_id) == 2


def test_idempotency_changed_args_new_key() -> None:
    scenario, run = _s02()
    table = _table(scenario, run)
    cache = ProbeCache()
    ctx = _ctx(scenario, run)
    a = run_probe(ProbeId.REGISTRY_LOOKUP, ctx, table, cache=cache)
    b = run_probe(ProbeId.REGISTRY_LOOKUP, ctx, table, args={"modem_id": "MOD-4471"}, cache=cache)
    assert a.key != b.key and not b.cached and cache.count(ProbeId.REGISTRY_LOOKUP) == 2
    assert a.args == {} and b.args == {"modem_id": "MOD-4471"}


# --- rung, allowlist ------------------------------------------------------------------------


@pytest.mark.parametrize("probe_id", ASK_PROBES)
def test_rung_enforced(probe_id: ProbeId) -> None:
    scenario, run = _s05()
    table = _table(scenario, run)
    before = table.table_hash()
    cache = ProbeCache()
    with pytest.raises(ProbeNotAllowed):
        run_probe(probe_id, _ctx(scenario, run, rung=Rung.WATCH), table, cache=cache)
    assert table.table_hash() == before and len(cache) == 0 and cache.count() == 0


def test_rung_enforced_hail_at_watch() -> None:
    scenario, run = _s05()
    table = _table(scenario, run)
    n = len(table)
    with pytest.raises(ProbeNotAllowed):
        run_probe("acoustic_hail", _ctx(scenario, run, rung=Rung.WATCH), table, cache=ProbeCache())
    assert len(table) == n
    # watch probes run at watch
    res = run_probe("drift_check", _ctx(scenario, run, rung=Rung.WATCH), table, cache=ProbeCache())
    assert res.rows


@pytest.mark.parametrize("name", ["slow", "hold", "hand_to_person", "HOLDING", "set_rung", ""])
def test_not_allowlisted_never_runs(name: str) -> None:
    scenario, run = _s05()
    table = _table(scenario, run)
    n = len(table)
    with pytest.raises(ProbeNotAllowed):
        run_probe(name, _ctx(scenario, run), table, cache=ProbeCache())
    assert len(table) == n


# --- typed rows -----------------------------------------------------------------------------


@pytest.mark.parametrize("probe_id", list(ProbeId))
def test_every_probe_returns_typed_rows(probe_id: ProbeId) -> None:
    scenario, run = _s05()
    table = _table(scenario, run)
    ctx = _ctx(scenario, run)
    spec = ALLOWLIST[probe_id]
    res = run_probe(probe_id, ctx, table, cache=ProbeCache())
    assert isinstance(res, ProbeResult)
    assert res.landed_tick == ctx.tick + P.latency_ticks(spec.latency_s, run.tick_s)
    assert res.rows
    for row in res.rows:
        assert isinstance(row, EvidenceRow)
        assert row.probe_id is probe_id and row.contact_id == ctx.contact_id
        assert row.t == res.landed_tick
        assert row.row_id in table
        again = EvidenceRow.model_validate(from_file_data(row.model_dump(mode="json")))
        assert again.row_id == row.row_id  # round-trips with its content hash
    produced = {r.feature for r in res.rows}
    for feature in spec.returns:
        if feature is FeatureName.PLAN_ID:
            continue  # only written when a plan was found (WO-022 registry_lookup_rows)
        assert feature in produced, (probe_id, feature)


def test_probe_sources() -> None:
    scenario, run = _s02()
    table = _table(scenario, run)
    ctx = _ctx(scenario, run)
    cache = ProbeCache()
    for pid in ALLOWLIST:
        res = run_probe(pid, ctx, table, cache=cache)
        for row in res.rows:
            if pid is ProbeId.REGISTRY_LOOKUP and row.feature is FeatureName.PLAN_ID:
                assert row.source.startswith(("registry:", "registry.position:"))
            else:
                assert row.source == f"probe:{pid.value}", (pid, row.feature)


def test_unknown_rows_when_contact_has_no_track() -> None:
    scenario, run = _s05()
    table = _table(scenario, run)
    ctx = _ctx(scenario, run, contact_id="c-9999", tick=100)
    cache = ProbeCache()
    for pid in (
        ProbeId.KINEMATICS_PROFILE,
        ProbeId.REGISTRY_LOOKUP,
        ProbeId.SECOND_SENSOR_DWELL,
        ProbeId.REQUEST_WATCH_OFFICER_LOOK,
        ProbeId.ASK_OPERATOR_BY_PHONE,
    ):
        res = run_probe(pid, ctx, table, cache=cache)
        assert res.rows and all(r.value == UNKNOWN for r in res.rows), pid
        assert all(r.kind is EvidenceKind.FACT and r.confidence == 0.0 for r in res.rows)


def test_watch_probe_reads_track_truncated_at_tick() -> None:
    scenario, run = _s05()
    cid = _main_contact(run)
    early = run.tracks[cid].points[len(run.tracks[cid].points) // 2].tick
    table = _table(scenario, run)
    res = run_probe(ProbeId.KINEMATICS_PROFILE, _ctx(scenario, run, tick=early), table,
                    cache=ProbeCache())  # fmt: skip
    seg = [r for r in res.rows if r.feature is FeatureName.TRACK_SEGMENT]
    assert seg and all(str(r.value).split(";")[0].endswith(f"..{early}") for r in seg)


def test_acoustic_feature_read_one_row_per_feature() -> None:
    scenario, run = _s05()
    table = _table(scenario, run)
    res = run_probe(ProbeId.ACOUSTIC_FEATURE_READ, _ctx(scenario, run), table, cache=ProbeCache())
    features = [r.feature for r in res.rows]
    assert features == list(ALLOWLIST[ProbeId.ACOUSTIC_FEATURE_READ].returns)
    tonal = next(r for r in res.rows if r.feature is FeatureName.PROPELLER_TONAL_PRESENT)
    assert tonal.value is True


def test_hail_probe_uses_simulator_rows() -> None:
    scenario, run = _s02()
    table = _table(scenario, run)
    ctx = _ctx(scenario, run)
    res = run_probe(ProbeId.ACOUSTIC_HAIL, ctx, table, cache=ProbeCache())
    by = {r.feature: r.value for r in res.rows}
    assert by[FeatureName.HAIL_REPLY] == "valid" and by[FeatureName.MODEM_ID] == "MOD-4471"
    assert res.landed_tick == ctx.tick + 10
    # the registry lookup now resolves the modem from the table and names the plan
    reg = run_probe(ProbeId.REGISTRY_LOOKUP, ctx, table, cache=ProbeCache())
    assert reg.args == {"modem_id": "MOD-4471"}
    match = next(r for r in reg.rows if r.feature is FeatureName.REGISTRY_MATCH)
    assert match.kind is EvidenceKind.DERIVED and len(match.derived_from) == 3
    phone = run_probe(ProbeId.ASK_OPERATOR_BY_PHONE, ctx, table, cache=ProbeCache())
    assert [(r.feature, r.value) for r in phone.rows] == [(FeatureName.OPERATOR_KNOWN, False)]


def test_phone_known_operator() -> None:
    scenario, run = _world("S04", 4, 200)
    table = _table(scenario, run)
    ctx = _ctx(scenario, run)
    res = run_probe(
        ProbeId.ASK_OPERATOR_BY_PHONE, ctx, table, args={"modem_id": "MOD-2290"}, cache=ProbeCache()
    )
    assert [(r.value, r.confidence) for r in res.rows] == [(True, 1.0)]
    nobody = run_probe(ProbeId.ASK_OPERATOR_BY_PHONE, ctx, table, cache=ProbeCache())
    assert [r.value for r in nobody.rows] == [UNKNOWN]


def test_camera_and_eyes_on_under_fog() -> None:
    scenario, run = _s05()
    foggy = scenario.model_copy(update={"toggles": (Toggle(name=ToggleName.FOG_NO_CAMERA),)})
    table = _table(foggy, run)
    ctx = _ctx(foggy, run)
    cam = run_probe(ProbeId.CAMERA_CUE, ctx, table, cache=ProbeCache())
    assert [r.value for r in cam.rows] == [0.0]
    assert cam.rows[0].kind is EvidenceKind.DERIVED  # WO-012 labels visibility an assumption
    look = run_probe(ProbeId.REQUEST_WATCH_OFFICER_LOOK, ctx, table, cache=ProbeCache())
    assert all(r.value == UNKNOWN for r in look.rows)
    clear = run_probe(ProbeId.CAMERA_CUE, _ctx(scenario, run), _table(scenario, run),
                      cache=ProbeCache())  # fmt: skip
    assert clear.rows[0].value == 0.2  # 02:00, dark default


def test_camera_fact_without_prior_visibility_row() -> None:
    scenario, run = _s05()
    res = run_probe(ProbeId.CAMERA_CUE, _ctx(scenario, run), EvidenceTable(), cache=ProbeCache())
    assert res.rows[0].kind is EvidenceKind.FACT and res.rows[0].confidence == 1.0


def test_eyes_on_confidence_is_visibility() -> None:
    scenario, run = _world("S07", 17, 240)
    table = _table(scenario, run)
    res = run_probe(
        ProbeId.REQUEST_WATCH_OFFICER_LOOK, _ctx(scenario, run), table, cache=ProbeCache()
    )
    assert {r.feature for r in res.rows} == {FeatureName.DEPTH, FeatureName.TRANSIT_PATTERN}
    assert all(r.confidence == 0.8 for r in res.rows if r.value != UNKNOWN)  # 11:00 daylight


def test_second_sensor_dwell_combines_two_nodes() -> None:
    combined = P._combine([(4.0, 0.5), (6.0, 0.5)])
    assert combined is not None
    assert combined == pytest.approx((5.0, 0.75))
    one = P._combine([(4.0, 0.6)])
    assert one == pytest.approx((4.0, 0.6))
    assert P._combine([]) is None
    scenario, run = _world("S07", 17, 240)
    res = run_probe(
        ProbeId.SECOND_SENSOR_DWELL, _ctx(scenario, run), _table(scenario, run), cache=ProbeCache()
    )
    assert [r.feature for r in res.rows] == [FeatureName.DEPTH, FeatureName.SPEED_OVER_GROUND]
    assert all(r.kind is EvidenceKind.FACT for r in res.rows)


def test_rows_already_in_table_are_not_appended_twice() -> None:
    scenario, run = _s05()
    table = _table(scenario, run)
    ctx = _ctx(scenario, run)
    run_probe(ProbeId.DRIFT_CHECK, ctx, table, cache=ProbeCache())
    n = len(table)
    # a fresh cache re-executes, but identical rows are skipped rather than duplicated
    again = run_probe(ProbeId.DRIFT_CHECK, ctx, table, cache=ProbeCache())
    assert again.cached is False and len(table) == n


# --- rule greps ---------------------------------------------------------------------------


def test_no_model_path_in_probes_or_planner() -> None:
    for path in (PROBES_PY, PLANNER_PY):
        assert "soundings.llm" not in path.read_text(encoding="utf-8"), path.name


def test_denylist_clean() -> None:
    patterns = [
        re.compile(line.strip(), re.IGNORECASE)
        for line in (REPO / "config" / "denylist.txt").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    paths = [PROBES_PY, PLANNER_PY, *sorted((REPO / "tests").glob("test_probe*.py"))]
    for path in paths:
        text = path.read_text(encoding="utf-8")
        for pattern in patterns:
            assert not pattern.search(text), (path.name, pattern.pattern)
