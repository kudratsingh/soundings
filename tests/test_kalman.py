"""WO-084 unit tests: the 2-D constant-velocity Kalman filter, its projection and views.

Owner directive (verbatim): on a straight synthetic track with sigma=15 m noise, smoothed
RMSE < raw RMSE; projection error at 60 s < 30 m; determinism; features tests still pass.
"""

from __future__ import annotations

import ast
import math
import os
import re
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from soundings.canonical import canonical_json
from soundings.investigation import features as F
from soundings.investigation.kalman import (
    KalmanConfig,
    KalmanState,
    chi2_2dof,
    cov_eigenvalues,
    ellipse_from_cov,
    filter_track,
    initial_state,
    kalman_state_xyv,
    load_kalman_config,
    load_tracking_config,
    predict,
    project,
    projection_view,
    reading_sigmas,
    update,
)
from soundings.investigation.velocity import TrackArrays
from soundings.schemas._base import LAT_MAX, LAT_MIN, LON_MAX, LON_MIN
from soundings.schemas.harbor import load_harbor
from soundings.schemas.track import Track
from soundings.sim.run import run_scenario, truncate_run
from soundings.sim.scenario import resolve_scenario
from tests.support.straight_track import StraightTrack, straight_track

REPO = Path(__file__).resolve().parent.parent
KALMAN_PY = REPO / "soundings" / "investigation" / "kalman.py"
CFG = load_kalman_config()
SEEDS = range(1, 21)


def _track(seed: int) -> StraightTrack:
    return straight_track(np.random.default_rng(seed))


def _rmse(dx: np.ndarray, dy: np.ndarray) -> float:
    return float(np.sqrt(np.mean(dx * dx + dy * dy)))


def _rmse_pair(tr: StraightTrack) -> tuple[float, float]:
    kt = filter_track(tr.readings, CFG)
    raw = _rmse(tr.readings.x_m - tr.true_x, tr.readings.y_m - tr.true_y)
    smooth = _rmse(np.array(kt.x_m) - tr.true_x, np.array(kt.y_m) - tr.true_y)
    return raw, smooth


def _p_matrix(state: KalmanState) -> np.ndarray:
    return np.array(state.P, dtype=np.float64).reshape(4, 4)


def test_config_matches_wo_defaults() -> None:
    assert CFG == KalmanConfig()
    assert load_tracking_config().kalman == CFG
    with pytest.raises(ValueError):
        KalmanConfig.model_validate({"q_m2_s3": 0.002, "typo": 1})


def test_straight_track_rmse() -> None:
    """Directive: sigma=15 m straight track, smoothed RMSE < raw RMSE (seed 7, seeds 1-20)."""
    tr = _track(7)
    assert float(tr.readings.sigma_m[0]) == 15.0  # type: ignore[index]
    raw, smooth = _rmse_pair(tr)
    assert smooth < raw
    assert 15.0 < raw < 25.0 and smooth < 10.0, (raw, smooth)
    for seed in SEEDS:
        raw, smooth = _rmse_pair(_track(seed))
        assert smooth < 0.6 * raw, (seed, raw, smooth)


def test_projection_60s() -> None:
    """Directive: projection error at 60 s < 30 m (seed 7 and every seed 1-20)."""
    for seed in (7, *SEEDS):
        tr = _track(seed)
        last = filter_track(tr.readings, CFG).last
        proj = project(last, 60.0, CFG)
        tx, ty = tr.truth_at(last.t_s + 60.0)
        err = math.hypot(proj.x_m - tx, proj.y_m - ty)
        assert err < 30.0, (seed, err)
        assert proj.t_s == pytest.approx(last.t_s + 60.0)
        assert proj.ellipse.p == 0.95


def _s05_track_at_109() -> Track:
    harbor = load_harbor()
    run = truncate_run(run_scenario(resolve_scenario("S05", harbor), seed=11, harbor=harbor), 110)
    own = {"gt-1"}
    ids = [cid for cid, t in run.contact_truth.items() if t.gt_id in own]
    cid = max(sorted(ids), key=lambda c: len(run.tracks[c].points))
    return run.tracks[cid]


def _determinism_payload() -> str:
    tr = _track(7)
    kt = filter_track(tr.readings, CFG)
    view = projection_view(_s05_track_at_109(), CFG)
    assert view is not None
    return canonical_json({"filter": kt.as_dict(), "view": view})


def test_deterministic() -> None:
    """Directive: determinism, in-process twice and in subprocesses under two hash seeds."""
    first = _determinism_payload()
    assert first == _determinism_payload()
    code = "from tests.test_kalman import _determinism_payload as p; print(p())"
    outs = []
    for hash_seed in ("0", "1"):
        env = {**os.environ, "PYTHONHASHSEED": hash_seed, "MODEL_MODE": "replay"}
        res = subprocess.run(
            [sys.executable, "-c", code],
            cwd=REPO,
            env=env,
            capture_output=True,
            text=True,
            check=True,
        )
        outs.append(res.stdout.strip())
    assert outs[0] == outs[1] == first
    tree = ast.parse(KALMAN_PY.read_text(encoding="utf-8"))
    banned = {"random", "time", "datetime"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name.split(".")[0] not in banned, alias.name
        elif isinstance(node, ast.ImportFrom):
            assert (node.module or "").split(".")[0] not in banned, node.module
            assert not any(a.name == "default_rng" for a in node.names)
        elif isinstance(node, ast.Attribute):
            assert node.attr not in {"default_rng", "random"}, node.attr


@pytest.mark.parametrize(
    "abc,major,minor,orientation,lams",
    [
        ((400.0, 0.0, 100.0), 48.955, 24.477, 90.0, (400.0, 100.0)),
        ((100.0, 0.0, 400.0), 48.955, 24.477, 0.0, (400.0, 100.0)),
        ((250.0, 150.0, 250.0), 48.955, 24.477, 45.0, (400.0, 100.0)),
    ],
)
def test_ellipse_closed_form(
    abc: tuple[float, float, float],
    major: float,
    minor: float,
    orientation: float,
    lams: tuple[float, float],
) -> None:
    a, b, c = abc
    assert chi2_2dof(0.95) == pytest.approx(5.991465, abs=1e-6)
    ell = ellipse_from_cov(a, b, c, 0.95)
    assert ell.semi_major_m == pytest.approx(major, abs=1e-3)
    assert ell.semi_minor_m == pytest.approx(minor, abs=1e-3)
    assert ell.semi_major_m == pytest.approx(math.sqrt(chi2_2dof(0.95) * lams[0]), abs=1e-6)
    assert ell.orientation_deg == pytest.approx(orientation, abs=1e-9)
    assert ell.p == 0.95
    lam_major, lam_minor = cov_eigenvalues(a, b, c)
    ref = np.linalg.eigvalsh(np.array([[a, b], [b, c]]))
    assert lam_major == pytest.approx(float(ref[1]), abs=1e-9)
    assert lam_minor == pytest.approx(float(ref[0]), abs=1e-9)
    assert (lam_major, lam_minor) == pytest.approx(lams, abs=1e-9)


def _arrays(
    t: np.ndarray, x: np.ndarray, y: np.ndarray, sigma: float | None, conf: float
) -> TrackArrays:
    n = t.shape[0]
    return TrackArrays.from_columns(
        t, x, y, np.full(n, 8.0), np.full(n, conf), None if sigma is None else np.full(n, sigma)
    )


def test_filter_basics() -> None:
    # one reading: no state
    one = _arrays(np.array([0.0]), np.array([1.0]), np.array([2.0]), 15.0, 0.9)
    assert kalman_state_xyv(one, CFG) is None
    assert len(filter_track(one, CFG)) == 1

    # noise-free constant velocity converges to the true velocity by 60 s
    clean = straight_track(np.random.default_rng(0), duration_s=60.0, noise=False)
    state = kalman_state_xyv(clean.readings, CFG)
    assert state is not None
    assert abs(state[2] - clean.vx) < 0.01 and abs(state[3] - clean.vy) < 0.01, state

    # a 40 s gap grows position sigma monotonically, the next update shrinks it
    tr = _track(3).readings.select(slice(0, 60))
    last = filter_track(tr, CFG).last
    sigmas = [last.position_sigma_m()]
    s = last
    for _ in range(20):
        s = predict(s, 2.0, CFG)
        sigmas.append(s.position_sigma_m())
    assert all(b > a for a, b in zip(sigmas, sigmas[1:], strict=False)), sigmas
    after = update(s, (s.x[0], s.x[1]), 15.0, CFG)
    assert after.position_sigma_m() < sigmas[-1]

    # missing sigma_m falls back to default_sigma_m / max(confidence, 0.1)
    t = np.arange(5, dtype=np.float64) * 2.0
    no_sig = _arrays(t, t, t, None, 0.5)
    assert np.allclose(reading_sigmas(no_sig, CFG), 30.0)
    low = _arrays(t, t, t, None, 0.01)
    assert np.allclose(reading_sigmas(low, CFG), 150.0)
    # and it is what the filter uses: same result as reporting that sigma explicitly
    explicit = _arrays(t, t, t, 30.0, 0.5)
    assert filter_track(no_sig, CFG).as_dict() == filter_track(explicit, CFG).as_dict()


def _assert_spd(p: np.ndarray) -> None:
    assert np.array_equal(p, p.T)
    assert float(np.linalg.eigvalsh(p).min()) > 0.0


@settings(max_examples=60, deadline=None, derandomize=True)
@given(
    steps=st.lists(
        st.tuples(st.floats(5.0, 30.0), st.floats(1.0, 10.0), st.floats(-50.0, 50.0)),
        min_size=1,
        max_size=40,
    )
)
def test_filter_spd_property(steps: list[tuple[float, float, float]]) -> None:
    """P stays symmetric positive definite for sigma in [5, 30] m and dt in [1, 10] s."""
    s = initial_state(0.0, (0.0, 0.0), steps[0][0], CFG)
    _assert_spd(_p_matrix(s))
    for sigma, dt, offset in steps:
        s = predict(s, dt, CFG)
        _assert_spd(_p_matrix(s))
        s = update(s, (s.x[0] + offset, s.x[1] - offset), sigma, CFG)
        _assert_spd(_p_matrix(s))


def test_projection_view() -> None:
    view = projection_view(_s05_track_at_109(), CFG)
    assert view is not None
    assert view.tick <= 109
    for v in (view.lat, view.lon, view.proj_lat, view.proj_lon):
        assert math.isfinite(v)
    assert LAT_MIN <= view.lat <= LAT_MAX and LON_MIN <= view.lon <= LON_MAX
    assert LAT_MIN <= view.proj_lat <= LAT_MAX and LON_MIN <= view.proj_lon <= LON_MAX
    assert 0.0 <= view.heading_deg < 360.0
    assert view.ellipse.semi_major_m >= view.ellipse.semi_minor_m > 0.0
    assert view.ellipse.p == 0.95
    assert 0.0 <= view.ellipse.orientation_deg < 180.0
    assert view.speed_mps == pytest.approx(math.hypot(view.vx, view.vy), abs=1e-6)
    assert canonical_json(view)  # JSON-ready


def test_no_truth_in_package() -> None:
    """Rule 2.4/2.6: the filter never names truth; a kalman row names its extractor."""
    text = KALMAN_PY.read_text(encoding="utf-8")
    for name in ("truth_paths", "contact_truth", "TruthPoint", "gt_id"):
        assert name not in text, name
    names = {
        n.id if isinstance(n, ast.Name) else n.attr
        for n in ast.walk(ast.parse(text))
        if isinstance(n, (ast.Name, ast.Attribute))
    }
    assert not names & {"truth_paths", "contact_truth", "TruthPoint", "gt_id"}
    harbor = load_harbor()
    run = run_scenario(resolve_scenario("S05", harbor), seed=11, harbor=harbor, ticks=200)
    table = F.build_evidence(
        run, harbor, lambda x, y, t: (0.0, 0.0), None, hour_of_day=2, velocity_source="kalman"
    )
    kalman_rows = [r for r in table if r.source == "extractor:closing_to_asset_kalman"]
    assert kalman_rows
    assert {r.feature for r in kalman_rows} == set(F.CLOSING_FEATURES)
    assert all(r.derived_from for r in kalman_rows)


def test_no_model_imports_or_denylisted_words() -> None:
    """Rule 2.2/2.5: no model client imports; no denylisted vocabulary in WO-084 files."""
    tree = ast.parse(KALMAN_PY.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            mods = [node.module or ""]
        else:
            continue
        for mod in mods:
            assert not mod.startswith(("soundings.llm", "anthropic")), mod
    patterns = [
        re.compile(line.strip(), re.IGNORECASE)
        for line in (REPO / "config" / "denylist.txt").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    files = [
        KALMAN_PY,
        REPO / "config" / "tracking.yaml",
        REPO / "tests" / "test_kalman.py",
        REPO / "tests" / "test_kalman_vs_window.py",
        REPO / "docs" / "work-orders" / "WO-084-projected-tracks.md",
    ]
    for path in files:
        text = path.read_text(encoding="utf-8")
        for pat in patterns:
            assert not pat.search(text), (path.name, pat.pattern)
