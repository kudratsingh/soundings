"""WO-084 integration: Kalman against the WO-012 window fit on S05 and S06, and the flag.

Truth is read here only (``contact_truth[cid].gt_id`` -> ``truth_paths``), to grade the
two velocity sources; the package never sees it. The comparison table is written to
``artifacts/wo-084/kalman_vs_window.json`` as canonical JSON, and
``config/tracking.yaml`` ``closing_velocity_source`` must agree with it:
``kalman`` if and only if Kalman closing-rate RMSE < window RMSE on both S05 and S06 and
the S05 time-to-asset MAE is not worse (ties at 6 dp count as not lower).
"""

from __future__ import annotations

import math
from functools import cache
from pathlib import Path
from typing import Any

import pytest

from soundings.canonical import canonical_json, round6
from soundings.investigation import features as F
from soundings.investigation.evidence import EvidenceTable
from soundings.investigation.kalman import (
    filter_track,
    kalman_state_xyv,
    load_kalman_config,
    load_tracking_config,
)
from soundings.investigation.pol import PolTable, load_pol_table
from soundings.schemas.enums import FeatureName
from soundings.schemas.evidence import EvidenceRow
from soundings.schemas.geo import to_local_m
from soundings.schemas.harbor import Harbor, load_harbor
from soundings.schemas.scenario import Scenario
from soundings.sim.current import CurrentField, load_current_field, tide_phase_for_hour
from soundings.sim.run import run_scenario
from soundings.sim.scenario import resolve_scenario
from soundings.sim.types import SimRun, TruthPoint

REPO = Path(__file__).resolve().parent.parent
ARTIFACT = REPO / "artifacts" / "wo-084" / "kalman_vs_window.json"
COMPARE = (("S05", 11), ("S06", 13))
CATALOG = ("S01", "S02", "S03", "S04", "S05", "S06", "S07", "S08")
TIME_TRUTH_MAX_S = 1200.0
"""Time-to-asset MAE only counts ticks whose truth time is in (0, this]: a contact
sitting on the asset point (truth range 0, time 0) has no approach to time."""
TIME_CAP_S = 3600.0
"""An estimate of ``NOT_CLOSING_S`` (1e9) is capped here before the time error is taken."""
CLOSING = F.CLOSING_FEATURES


@cache
def _harbor() -> Harbor:
    return load_harbor()


@cache
def _field() -> CurrentField:
    return load_current_field()


@cache
def _pol() -> PolTable:
    return load_pol_table()


@cache
def _scenario(sid: str) -> Scenario:
    return resolve_scenario(sid, _harbor())


@cache
def _run(sid: str, seed: int | None = None) -> SimRun:
    return run_scenario(_scenario(sid), seed=seed, harbor=_harbor())


def _evidence(sid: str, velocity_source: F.VelocitySource | None) -> EvidenceTable:
    field = _field()
    return F.build_evidence(
        _run(sid),
        _harbor(),
        field,
        _pol(),
        hour_of_day=_scenario(sid).hour_of_day,
        tide_phase_for_hour=lambda hour: tide_phase_for_hour(hour, field),
        velocity_source=velocity_source,
    )


def _truth_state(tp: TruthPoint) -> tuple[float, float, float, float]:
    x, y = to_local_m(tp.lat, tp.lon)
    h = math.radians(tp.heading_deg)
    return x, y, tp.speed_mps * math.sin(h), tp.speed_mps * math.cos(h)


def _rmse(errors: list[float]) -> float:
    return round6(math.sqrt(sum(e * e for e in errors) / len(errors)))


def _mae(errors: list[float]) -> float:
    return round6(sum(abs(e) for e in errors) / len(errors))


@cache
def _compare(sid: str, seed: int) -> dict[str, Any]:
    """Closing-rate RMSE and time MAE of both sources against truth over one run."""
    run = _run(sid, seed)
    harbor = _harbor()
    cfg = load_kalman_config()
    closing_err: dict[str, list[float]] = {"window": [], "kalman": []}
    time_err: dict[str, list[float]] = {"window": [], "kalman": []}
    for cid in sorted(run.tracks):
        gt = run.contact_truth[cid].gt_id
        if gt.startswith("fa-"):
            continue  # a false alarm has no motion to grade
        truth = {tp.tick: tp for tp in run.truth_paths[gt]}
        track = run.tracks[cid]
        arrays = F.track_arrays(track)
        kt = filter_track(arrays, cfg)
        for k, point in enumerate(track.points):
            prefix = arrays.select(slice(0, k + 1))
            if len(prefix.tail(F.KIN_WINDOW_S)) < 2:
                continue
            asset = harbor.nearest_asset(point.lat, point.lon, F.CRITICAL_ASSET_MIN)
            tp = truth.get(point.tick)
            if asset is None or tp is None:
                continue
            ax, ay = to_local_m(asset.lat, asset.lon)
            window = F.closing_to_asset(prefix, ax, ay)
            assert window is not None
            kalman = F.closing_from_state((kt.x_m[k], kt.y_m[k], kt.vx[k], kt.vy[k]), ax, ay)
            true_rate, true_time = F.closing_from_state(_truth_state(tp), ax, ay)
            for name, est in (("window", window), ("kalman", kalman)):
                closing_err[name].append(est[0] - true_rate)
                if 0.0 < true_time <= TIME_TRUTH_MAX_S:
                    time_err[name].append(min(est[1], TIME_CAP_S) - true_time)
    return {
        "scenario": sid,
        "seed": seed,
        "n_closing": len(closing_err["window"]),
        "n_time": len(time_err["window"]),
        "closing_rmse_mps": {m: _rmse(e) for m, e in sorted(closing_err.items())},
        "time_mae_s": {m: _mae(e) if e else None for m, e in sorted(time_err.items())},
    }


def _kalman_wins(rows: dict[str, dict[str, Any]]) -> bool:
    rate_wins = all(
        rows[sid]["closing_rmse_mps"]["kalman"] < rows[sid]["closing_rmse_mps"]["window"]
        for sid, _ in COMPARE
    )
    s05_time = rows["S05"]["time_mae_s"]
    time_ok = s05_time["kalman"] is not None and s05_time["kalman"] <= s05_time["window"]
    return rate_wins and time_ok


def test_kalman_vs_window_decision() -> None:
    rows = {sid: _compare(sid, seed) for sid, seed in COMPARE}
    for row in rows.values():
        assert row["n_closing"] > 50, row
        assert row["n_time"] > 0 or row["scenario"] == "S06", row
    wins = _kalman_wins(rows)
    flag = load_tracking_config().closing_velocity_source
    doc = {
        "wo": "WO-084",
        "q_m2_s3": load_kalman_config().q_m2_s3,
        "time_truth_max_s": TIME_TRUTH_MAX_S,
        "time_cap_s": TIME_CAP_S,
        "scenarios": rows,
        "kalman_lowers_error": wins,
        "closing_velocity_source": flag,
    }
    ARTIFACT.parent.mkdir(parents=True, exist_ok=True)
    ARTIFACT.write_text(canonical_json(doc) + "\n", encoding="utf-8")
    assert flag == ("kalman" if wins else "window"), doc


@pytest.mark.parametrize("sid,seed", COMPARE)
def test_causal_pass_matches_prefix_state(sid: str, seed: int) -> None:
    """The per-reading forward pass equals ``kalman_state_xyv`` of each prefix (causal)."""
    run = _run(sid, seed)
    cfg = load_kalman_config()
    cid = max(sorted(run.tracks), key=lambda c: len(run.tracks[c].points))
    arrays = F.track_arrays(run.tracks[cid])
    kt = filter_track(arrays, cfg)
    for k in (1, len(arrays) // 3, len(arrays) // 2, len(arrays) - 1):
        state = kalman_state_xyv(arrays.select(slice(0, k + 1)), cfg)
        assert state is not None
        got = tuple(round6(v) for v in state)
        assert got == (kt.x_m[k], kt.y_m[k], kt.vx[k], kt.vy[k])


def _strip(row: EvidenceRow, changed: dict[str, str]) -> dict[str, Any]:
    dump = row.model_dump(mode="json")
    dump.pop("row_id", None)
    dump["derived_from"] = [changed.get(i, i) for i in dump.get("derived_from") or []]
    return dump


@pytest.mark.parametrize("sid", CATALOG)
def test_flag_on_catalog(sid: str) -> None:
    """Kalman mode fills every catalog feature and only moves the two closing rows."""
    window = _evidence(sid, "window")
    configured = load_tracking_config().closing_velocity_source
    assert _evidence(sid, None).table_hash() == _evidence(sid, configured).table_hash()
    kalman = _evidence(sid, "kalman")  # contact_evidence raises on a catalog gap
    assert window.contacts() == kalman.contacts()
    for cid in window.contacts():
        w_rows, k_rows = window.for_contact(cid), kalman.for_contact(cid)
        assert [r.feature for r in w_rows] == [r.feature for r in k_rows]
        assert {r.feature for r in k_rows} >= set(F.FEATURE_CATALOG)
        changed: dict[str, str] = {}
        for w, k in zip(w_rows, k_rows, strict=True):
            if w.feature in CLOSING:
                if w.source == f"{F.EXTRACTOR_SOURCE_PREFIX}closing_to_asset":
                    assert k.source == f"{F.EXTRACTOR_SOURCE_PREFIX}closing_to_asset_kalman"
                else:
                    assert k.source == w.source  # unknown keeps its reason
                if w.row_id != k.row_id:
                    changed[w.row_id] = k.row_id
                continue
            assert _strip(w, changed) == _strip(k, changed), (cid, w.feature)


def test_window_rows_name_window_extractor() -> None:
    """Window rows keep ``extractor:closing_to_asset`` and the kinematic segment."""
    table = _evidence("S05", "window")
    seen = 0
    for row in table:
        if row.feature in CLOSING and row.source.startswith(F.EXTRACTOR_SOURCE_PREFIX):
            assert row.source == f"{F.EXTRACTOR_SOURCE_PREFIX}closing_to_asset"
            seg = table.get(row.derived_from[0])
            assert seg.feature is FeatureName.TRACK_SEGMENT
            seen += 1
    assert seen > 0
