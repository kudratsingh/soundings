# WO-084 — projected-tracks

Phase: 7 · Branch: `wo/084-projected-tracks` · Worktree: `../soundings-worktrees/wo-084-projected-tracks` · Est: 4 h · Owner: coordinator Claude / Kudrat
Depends on: WO-012 (`TrackArrays`, `track_arrays`, `last_fitted_state`, `closing_to_asset`, `extract_kinematics`, `build_evidence`, `FEATURE_CATALOG`, `EXTRACTOR_SOURCE_PREFIX`), WO-011 (`run_scenario`, `SimRun.truth_paths`, `SimRun.contact_truth`, `TruthPoint`), WO-010 (`to_local_m`, `from_local_m`, `Harbor.nearest_asset`). Optional: WO-083 (`JobFix.velocity_source`). Only if the flag flips to `kalman`: WO-043 (`python -m soundings.ladder.verify --suite catalog`; in flight on `origin/wo/043a-ladder-driver-watch`, Wave C). Wave C.

## Goal
Each contact gets a hand-rolled 2-D constant-velocity Kalman filter. Its state is (x, y, vx, vy) in local metres. It exposes:
- a filtered (smoothed) position;
- a velocity vector;
- a 60 s projection with a covariance ellipse (semi-axes and orientation) that the WO-086 radar scope can draw.

The filter can feed `closing_rate_to_nearest_critical_asset` and `time_to_nearest_critical_asset` behind a config flag. The flag defaults to `window`, which keeps the WO-012 fit. It moves to `kalman` only if a committed test shows lower error against truth on both S05 and S06. Truth is read in tests only. With the flag at `window`, every evidence row and row id is byte-identical to main. This is pure numpy with no dependency add: `filterpy` is not in `pyproject.toml`.

## Scope
In:
- `soundings/investigation/kalman.py` (pure, about 80 lines of filter and about 60 of views):
  - `KalmanConfig(q_m2_s3=0.002, init_speed_sigma_mps=2.0, default_sigma_m=15.0, min_confidence=0.1, projection_s=60.0, ellipse_p=0.95)` (strict) and `load_kalman_config(path="config/tracking.yaml")`.
  - `KalmanState(t_s, x: tuple[4 floats], P: tuple[16 floats])` (frozen) and `predict(state, dt, cfg)` / `update(state, z_xy, sigma_m, cfg)`.
  - The process model: F = [[I, dt·I], [0, I]], with white-noise acceleration per axis Q = q·[[dt³/3, dt²/2], [dt²/2, dt]]. H picks (x, y).
  - The measurement noise: R = σ²·I. σ is the reading's `sigma_m` (the range-dependent sigma the sim reports). When `TrackArrays.sigma_m` is `None`, σ = `default_sigma_m / max(confidence, min_confidence)`.
  - Updates use the Joseph form, and P is symmetrised after each step.
  - Initialisation: x0 = first reading, v0 = 0, and P0 = diag(σ0², σ0², s², s²) with s = `init_speed_sigma_mps`.
  - Gaps are crossed by `predict` with the actual dt, so the covariance grows across a gap. No randomness and no clock.
  - `filter_track(track: TrackArrays, cfg) -> KalmanTrack` is the causal forward pass. It returns per-reading `t_s`, `x_m`, `y_m`, `vx`, `vy` and position σ, all rounded to 6 dp, plus the last `KalmanState`.
  - `project(state, horizon_s, cfg) -> Projection(t_s, x_m, y_m, vx, vy, ellipse)`. `ProjectionEllipse(semi_major_m, semi_minor_m, orientation_deg, p)` comes from the 2×2 position block, closed form with no `eigh` (which avoids eigenvector sign ambiguity):
    - λ± = (a+c)/2 ± √(((a−c)/2)² + b²);
    - semi-axes = √(χ²₂(p)·λ±), where χ²₂(0.95) = −2 ln(0.05) = 5.991465;
    - orientation = compass bearing of the major axis in [0, 180), from θ = ½·atan2(2b, a−c) east-of-x, converted as (90 − deg θ) mod 180.
  - `kalman_state_xyv(track) -> (x, y, vx, vy) | None` has the same contract as `features.last_fitted_state` (`None` below 2 points).
  - `ProjectionView(contact_id, tick, lat, lon, vx, vy, speed_mps, heading_deg, proj_lat, proj_lon, ellipse)` and `projection_view(track: Track, cfg) -> ProjectionView | None` are JSON-ready for WO-086.
- `soundings/investigation/features.py` (additive, default-off):
  - `VelocitySource = Literal["window", "kalman"]`.
  - `closing_to_asset(track, ax, ay, *, velocity_source="window")`: `"kalman"` takes the state from `kalman_state_xyv`.
  - `extract_kinematics(..., velocity_source="window")`, `contact_evidence(..., velocity_source=...)` and `build_evidence(..., velocity_source: VelocitySource | None = None)`. `None` reads `closing_velocity_source` from `config/tracking.yaml`.
  - A kalman-derived row's source is `extractor:closing_to_asset_kalman`. Window rows keep `extractor:closing_to_asset`, so default row ids do not move.
- `config/tracking.yaml` holds the `kalman` block (the values above) and `closing_velocity_source: window`, with a stress-test header comment.

Out:
- An RTS backward smoother (the live feed is causal).
- Nearly-constant-turn or IMM models.
- Using the filter in the sim, the posterior, the guards or WO-083 (WO-083 may opt in later).
- Drawing the ellipse (WO-086).
- Wiring `ProjectionView` into the WebSocket feed (WO-086 owns it).
- Changing any threshold in `config/ladder.yaml`.

## Spec references
`docs/SPEC-INVESTIGATION.md §2` (kinematic features, closing rate, time to asset), `§8` (honesty: a projection is a stress-test estimate) · `docs/SPEC-SIM.md` (detection noise σ 10–20 m, 2 s ticks) · WO-012 "Velocity estimation" and DECISIONS 2026-10-16 22:29 · `CLAUDE.md §2.2, §2.4, §2.6, §3`.

## Files expected
- `soundings/investigation/kalman.py`
- `soundings/investigation/features.py` (additive keyword only; WO-080 part B also edits `extract_context` there, so rebase onto it)
- `docs/STATE.md` (WO-084 row only), `docs/log/DECISIONS.md` (the rows listed under Model calls)
- `config/tracking.yaml`
- `tests/test_kalman.py`, `tests/test_kalman_vs_window.py`, `tests/support/straight_track.py` (synthetic track builder, seeded `numpy.random.default_rng(seed)` passed explicitly)

## Exit tests (must all pass with `MODEL_MODE=replay`, no network)
From the owner directive (verbatim):
- [ ] on a straight synthetic track with sigma=15 m noise, smoothed RMSE < raw RMSE; projection error at 60 s < 30 m; determinism; features tests still pass.

Each clause as a command:
- [ ] on a straight synthetic track with sigma=15 m noise, smoothed RMSE < raw RMSE — `pytest tests/test_kalman.py::test_straight_track_rmse`.
  - The track: 2 s ticks over 600 s at 1.5 m/s, heading 045, starting at local (100, −200). Noise is N(0, 15 m) per axis from `default_rng(7)`, and `sigma_m = 15`.
  - Proves the filtered-position RMSE against truth is below the raw-reading RMSE: about 7 m against about 20 m in the coordinator's scratch prototype at q = 0.002.
  - Also asserts filtered RMSE < 0.6 × raw for seeds 1–20.
- [ ] projection error at 60 s < 30 m — `pytest tests/test_kalman.py::test_projection_60s`. Proves `project(last_state, 60)` lies within 30 m of the truth position at t_end + 60 s for seed 7 and for every seed 1–20. The scratch prototype's worst case was 26.9 m.
- [ ] determinism — `pytest tests/test_kalman.py::test_deterministic`. Proves `filter_track` and `projection_view` run twice in-process, and once each in subprocesses under `PYTHONHASHSEED=0` and `=1`, give byte-identical `canonical_json`. Also, an AST scan of `kalman.py` finds no `random`, `default_rng`, `time` or `datetime` import (§2.4).
- [ ] features tests still pass — `MODEL_MODE=replay uv run pytest tests/test_features.py tests/test_evidence.py tests/test_evidence_table.py tests/test_posterior_catalog.py`. Proves the default path leaves features, evidence and posterior catalog checks unchanged.
- [ ] `pytest tests/test_kalman.py::test_ellipse_closed_form` — proves:
  - a hand-built P with (a, b, c) = (400, 0, 100) gives semi-axes √(5.991465·400) = 48.955 m and √(5.991465·100) = 24.477 m, orientation 90° (major axis east);
  - (100, 0, 400) gives orientation 0°;
  - (250, 150, 250) gives orientation 45° with λ = 400, 100;
  - each case agrees with `numpy.linalg.eigvalsh` to 1e-9.
- [ ] `pytest tests/test_kalman.py::test_filter_basics`. Proves:
  - one reading gives `kalman_state_xyv is None`;
  - noise-free constant-velocity input converges to the true velocity within 0.01 m/s by 60 s;
  - a 40 s gap grows position σ monotonically across the gap, and the next update shrinks it;
  - P stays symmetric positive definite at every step (hypothesis over random σ in [5, 30] m and dt in [1, 10] s);
  - missing `sigma_m` falls back to `default_sigma_m / max(confidence, 0.1)`.
- [ ] `pytest tests/test_kalman.py::test_projection_view` — on the S05 catalog run at seed 11 at tick 109. Proves `projection_view` returns finite lat/lon inside the harbor bounding box, `heading_deg` in [0, 360), `semi_major_m ≥ semi_minor_m > 0`, and an `ellipse.p` of 0.95.
- [ ] integration, the replacement decision — `MODEL_MODE=replay uv run pytest tests/test_kalman_vs_window.py`. It runs S05 (seed 11) and S06 (seed 13) with `run_scenario`. At every tick where a contact has 2 or more readings in the last 120 s and a critical asset (criticality ≥ 0.7) exists, it computes the closing rate from the window fit and from the Kalman filter. Truth (test-only, through `contact_truth[cid].gt_id` → `truth_paths`) is the truth velocity projected on the truth line of sight to the same asset. Proves:
  - (a) the table of closing-rate RMSE and time-to-asset MAE (over ticks with truth time ≤ 1200 s), per scenario and per method, is written to `artifacts/wo-084/kalman_vs_window.json` as canonical JSON;
  - (b) `config/tracking.yaml` `closing_velocity_source` is `kalman` if and only if Kalman closing-rate RMSE < window RMSE on both S05 and S06 and time MAE is not worse on S05. The test fails if the flag disagrees with the evidence.
- [ ] integration, flag on — `pytest tests/test_kalman_vs_window.py::test_flag_on_catalog`. With `velocity_source="kalman"`, `build_evidence` over S01–S08 at their catalog seeds produces every catalog feature (no `catalog gap`). Only the two closing rows change source, to `extractor:closing_to_asset_kalman`, plus the ids of any rows that cite them. With `"window"`, the evidence table hash equals main's for all eight.
- [ ] rule proof §2.4/§2.6 — `pytest tests/test_kalman.py::test_no_truth_in_package`. A grep and AST scan show that `soundings/investigation/kalman.py` never names `truth_paths`, `contact_truth`, `TruthPoint` or `gt_id` (WO-012's no-truth rule), and that a kalman-sourced row names its extractor (evidence first).
- [ ] rule proof §2.2/§2.5 — an AST scan finds no `soundings.llm` or `anthropic` import in `kalman.py`. `grep -iEf config/denylist.txt soundings/investigation/kalman.py config/tracking.yaml tests/test_kalman*.py docs/work-orders/WO-084-projected-tracks.md` → empty. `make check` is green.

## Interfaces
Exposed:
- WO-084 `soundings.investigation.kalman.KalmanConfig`, `.load_kalman_config`, `.KalmanState`, `.predict`, `.update`, `.KalmanTrack`, `.filter_track`, `.Projection`, `.ProjectionEllipse`, `.project`, `.kalman_state_xyv`, `.ProjectionView`, `.projection_view`
- WO-084 `soundings.investigation.features.VelocitySource`, the `velocity_source` keyword on `closing_to_asset`, `extract_kinematics`, `contact_evidence` and `build_evidence`
- WO-084 `config/tracking.yaml` keys `kalman.*`, `closing_velocity_source`

Consumed (read on main):
- WO-012: `soundings.investigation.velocity.TrackArrays` (`t_s`, `x_m`, `y_m`, `confidence`, `sigma_m`, `tail`), `soundings.investigation.features.{track_arrays, last_fitted_state, closing_to_asset, extract_kinematics, contact_evidence, build_evidence, KIN_WINDOW_S, NOT_CLOSING_S, CLOSING_EPS_MPS, CRITICAL_ASSET_MIN, EXTRACTOR_SOURCE_PREFIX, FEATURE_CATALOG}`.
- WO-011: `soundings.sim.run.run_scenario`, `soundings.sim.types.{SimRun, TruthPoint, ContactTruth}` (truth in tests only).
- WO-010: `soundings.schemas.geo.{to_local_m, from_local_m}`, `soundings.schemas.harbor.Harbor.nearest_asset`, `soundings.schemas.track.{Track, TrackPoint.sigma_m}`, `soundings.canonical.{round6, canonical_json}`.

## Cut line
Wave rule (STATE.md, owner directive): if Wave C is not merged by 3:00 PM, drop WO-084 whole and report the hours saved (4 h). WO-086 then draws leaders from the window fit (`last_fitted_state`) and drops the ellipse and its smoke assertion; WO-085 already reads the window row.
If late, drop in this order: (1) the hypothesis SPD check is replaced by 3 fixed cases; (2) `ProjectionView` lat/lon conversion (WO-086 converts `Projection` itself); (3) `test_flag_on_catalog` is narrowed to S05 and S06. Never an exit test, and never the replacement-decision test.

## Model calls
None. Paid run needed? No.
Dependency adds (logged in DECISIONS.md per owner directive): none. numpy is already a dependency, and `filterpy` is not present, so the filter is hand-rolled.
DECISIONS.md rows this WO adds:
- the new `config/tracking.yaml` and the default q = 0.002 m²/s³ (sized from the 20-seed straight-track prototype);
- the S05/S06 comparison result and the resulting `closing_velocity_source`, with the numbers from `artifacts/wo-084/kalman_vs_window.json`.

## Rungs touched
watch (kinematic evidence). Indirectly slow and hold, through `time_to_nearest_critical_asset`, but only if the flag flips to `kalman`.

## Hard rules exercised
- §2.2: no model imports.
- §2.4: `test_deterministic`.
- §2.6: kalman rows name their extractor; truth only in tests (`test_no_truth_in_package`).
- §3: no threshold changes. A flip of the flag can move contacts across the `config/ladder.yaml` 600 s and 300 s edges, so a flip also needs the WO-043 catalog verify (`python -m soundings.ladder.verify --suite catalog`) green before merge.

## Done
PR: · Merged commit: · Test summary: · STATE.md updated: yes/no

## Notes for downstream
- WO-086: the radar scope draws `ProjectionView.proj_lat/proj_lon` and an ellipse with `semi_major_m`, `semi_minor_m` and `orientation_deg` (compass, major axis). Label it "60 s projection (95%, stress-test estimate)".
- WO-083: to use Kalman velocity for jobs, call `kalman_state_xyv(track_arrays(prefix))` and set `JobFix.velocity_source="kalman"`. Behaviour stays unchanged until then.
- WO-085: `time_to_nearest_critical_asset` follows the flag, and the queue key reads the evidence row, never the filter directly.
- Tuning q: a larger q follows S05's turns more closely but makes the straight-track projection error worse. In the prototype, q = 0.01 gave a worst case of 51 m at 60 s over 20 seeds, which fails the 30 m bar. The comparison test runs only at the configured q. Any retune needs a DECISIONS.md row and must re-pass both `test_projection_60s` and `test_kalman_vs_window.py`.
- Most of S06 is a loiter under a hull with a closing rate near 0. Expect the S06 margin there to be small. A tie (equal to 6 dp) counts as "not lower", so the flag stays `window`.
