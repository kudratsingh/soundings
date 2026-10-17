# WO-089 — physics-cards

Phase: 7 · Branch: `wo/089-physics-cards` · Worktree: `../soundings-worktrees/wo-089-*` · Est: 2 h · Owner: coordinator Claude / Kudrat
Depends on: WO-011 (sim, `soundings/sim/detection.py`, `config/sim.yaml`), WO-012 (`soundings/investigation/features.py`, the `visibility_for_camera` row), WO-021 (`soundings/challenge/hail.py`, `config/challenge.yaml`), WO-060 (About panel, `soundings/api_routes/meta.py`, `soundings/stream/`, `web/src/components/AboutPanel.tsx`, `HarborMap.tsx`), WO-080 (part A, PR #33, for vessel noise: `SimRun.ais_tracks`, `AisReport`), WO-088 (battery hook: the responder imports `battery_drain_min` lazily if present), WO-090 (deck: `docs/deck/template.html`, `scripts/build_deck.py`, `tests/test_deck.py`).
Directive: owner, 2026-10-17 07:50 PDT. **The owner numbered this directive WO-088 ("physics cards"). WO-088 is already the responder platform, so the coordinator renumbered it WO-089** (DECISIONS 2026-10-17 07:53).
Split: part P1 `wo/089a-physics-core` (config, `soundings/physics.py`, the sim swap, hail, camera, tests) and part P2 `wo/089b-physics-ui-deck` (API, stream, About -> Physics panel, live range rings, deck slide 10). They share the contract under Interfaces.

## Goal
The Gaussian range falloff (SPEC-SIM §2, WO-011) becomes a passive-sonar-equation detection model, and every number it uses is visible and sourced.

- Signal excess is `SE = SL - TL - NL - DT`, with `TL = 20·log10(r) + α·r`. The per-tick detection probability is `logistic(SE / k)`.
- Ambient noise `NL` rises with sea state and with nearby vessel transits, weighted by distance.
- Hail latency, camera visibility and responder battery drain each get one stated law.
- `config/physics.yaml` holds every parameter as `{value, unit, source, label: "demo value"}`.
- The About panel gets a Physics section with one card per model: the equation, its parameters with their sources, and what we do NOT model.
- The map's sensor range rings are computed live from the model, so they shrink while a vessel passes.
- The honesty line stays: detection is "simulated for this demonstration". The cards make the simulation explicit; they do not claim it is real acoustics.

## Scope
In (P1):
- `config/physics.yaml`: every parameter is `{value, unit, source, label: "demo value"}`. Sections:
  - `sonar`:
    - `source_level_db` by acoustic class (propeller tonal present, propeller tonal absent, biologic);
    - `transmission.alpha_db_per_km`;
    - `ambient_noise_base_db`;
    - `sea_state_table` (state -> dB);
    - `detection_threshold_db`;
    - `logistic_k_db`;
    - `vessel_noise {source_level_db, falloff_db_per_doubling}`.
  - `hail {sound_speed_mps: 1500}`;
  - `camera {k_depth_per_m, turbidity_default}`;
  - `responder {drain_coeff}`.

  Sources are textbook-style: Urick, *Principles of Underwater Sound* for `TL = 20 log r + α r` and typical shallow-water α; Wenz (1962) curves for ambient noise by sea state; and so on. The file header states plainly that every value is a demo value.
- `soundings/physics.py`: pure, deterministic, numpy-only or numpy-free. Signatures are under Interfaces. It includes `model_cards()`.
- The sim swap. `config/sim.yaml` gets `detection.model: sonar_equation | gaussian`. The default is `sonar_equation`, and `gaussian` is the WO-011 model kept for comparison. The detection step draws from `detection_probability_at` with the node's current NL. That NL is base + sea state + vessel transits from `SimRun.ais_tracks`. Before WO-080 part A merges, the vessel term is zero; gate on `gh pr checks 33 --watch` if needed.
- Hail. `soundings/challenge/hail.py` uses `hail_latency_s` inside a `# --- WO-089 begin/end ---` block.
- Camera. `soundings/investigation/features.py` computes the `visibility_for_camera` value with `camera_visibility`, inside a WO-089 block.
- Tests (see Exit tests). P1 writes this WO first and adds the `docs/STATE.md` WO-089 row and the DECISIONS line.

In (P2):
- `GET /api/physics` returns `{cards: [...], nodes: [{node_id, effective_range_m, nl_db}]}`. It goes in a `# --- WO-089 begin/end ---` block. Note that on main the routes live in `soundings/api_routes/` and are mounted by `soundings/api.py`.
- The run engine (`soundings/stream/`, not `soundings/ui/`, which does not exist on main) streams `sensor_ranges` per tick as `[{node_id, effective_range_m, nl_db}]`.
- `web/src`:
  - An About -> Physics panel (`AboutPanel.tsx`) with one card per model: the equation, the parameters with their sources and the "demo value" label, and "what we do NOT model".
  - Map range rings (`HarborMap.tsx`) drawn from the live `sensor_ranges`.
- The deck. In `docs/deck/template.html` and `scripts/build_deck.py`, slide 10's "simulated for this demonstration" line is replaced by the physics cards, read from `config/physics.yaml`. The deck stays at 11 slides, with no hand-typed numbers, and the deck tests are updated.

Out:
- Hydrodynamics.
- Beam patterns or array gain.
- Multipath and ray tracing.
- Bottom type and bottom loss.
- Frequency-dependent spectra: one broadband number per class.
- Real signal processing (CLAUDE.md §6).
- Changing the current field (unchanged by directive).
- Any threshold in `config/ladder.yaml` or any value in `config/likelihoods.yaml`.
- Route planning or vehicle commands of any kind.

## Spec references
- `docs/SPEC-SIM.md` §2: the model being replaced, and §2a, the WO-089 amendment.
- `docs/SPEC-INVESTIGATION.md` §8: honesty, with "simulated for this demonstration" now shown together with the equation.
- `CLAUDE.md` §2.4 (determinism), §2.6 (labeled numbers), §4 (Sim line, amended 2026-10-17) and §6.
- DECISIONS 2026-10-16 22:25 (WO-011 sim constants).

## Model details (binding for P1)
- **Per-tick probability.**
  - `p_tick = p_max · health · logistic((SL - TL(r) - NL - DT) / k)`, where `logistic(x) = 1 / (1 + e^-x)`.
  - `p_max` (0.95) and `health` multiply the logistic exactly as they do in the WO-011 model. The per-tick ceiling, the gap statistics and the sensor_dropout behaviour therefore stay unchanged.
  - This deviates from the directive's bare logistic, and DECISIONS records the deviation.
- **Range cut-off.** Nothing is detected beyond `max_range_factor · effective_range_m(SL, NL_base)`, which is 800 m at base NL. The cut-off is fixed at base NL so the false-alarm disk and the evidence row sets stay bounded.
- **Effective range** (p = 0.5 of the logistic term) solves `20·log10(r) + a·r = E`, where `E = SL - NL - DT` and `a = α/1000` dB/m.
  - Closed form: `r = (c/a) · W((a/c) · e^(E/c))` with `c = 20/ln 10`. W is the Lambert W principal branch, computed with a fixed-count Halley iteration (no scipy).
  - When `α = 0`, `r = 10^(E/20)`.
- **Tuning.** SL, DT and k are tuned so that the effective range at base NL is about the former r0 = 400 m for every class. Every node on main has `range_m: 400`.
  - Worked example: α = 1 dB/km gives TL(400 m) = 52.04 + 0.40 = 52.44 dB, so `SL - NL_base - DT = 52.44 dB`.
  - Record the tuned values and the resulting effective range in the PR.
- **Vessel noise.** Contributions are distance-weighted: `L_i = SL_v - falloff_db_per_doubling · log2(max(d_i, d_ref) / d_ref)`. They are summed in the power domain with the base and sea-state terms: `NL = 10·log10(Σ 10^(L/10))`.
  - Reports are taken from `SimRun.ais_tracks` at the tick, sorted by `ais_id`.
  - With no transits, NL equals the base (plus sea state).
- **Reading confidence.** The WO-011 `reading_confidence` uses the Gaussian falloff. Under `sonar_equation`, it is `round6(health · p_logistic(range, NL))`. If that moves a catalog outcome, follow the spec-change rule below.
- **Hail latency.**
  - The physical two-way travel time is `2r / 1500 m/s`, which is under 1.1 s inside 800 m.
  - The WO-021 `latency_s: 20` is a simulated modem turnaround, not travel time. P1 keeps it as a separate, labeled term: reply tick = `ceil((turnaround_s + 2r/c) / tick_s)`.
  - If the extra term moves a reply by a tick and changes any catalog outcome, P1 records it as a spec change or trims `turnaround_s`. Either way the choice goes in DECISIONS and the PR.
- **Camera visibility.**
  - Visibility = `visibility_default(hour) · exp(-k·depth) · exp(-turbidity)`. Fog still forces 0.
  - The row stays an `assumption`: depth comes from the track and turbidity is assumed.
  - `soundings/investigation/probes.py` already defines its own `camera_visibility(ctx, tick)`. Import the physics function under an alias.
- **Battery.** `battery_drain_min(speed_mps, dt_s, cfg) = drain_coeff · v³ · dt_s`. It goes into WO-088's responder only through a lazy import.
- **Determinism.** No wall clock and no global rng. Iteration is sorted and floats are rounded to 6 dp.

## Files expected
P1:
- `docs/work-orders/WO-089-physics-cards.md`
- `docs/STATE.md` (WO-089 row) and `docs/log/DECISIONS.md`
- `config/physics.yaml` and `config/sim.yaml` (`detection.model`)
- `soundings/physics.py` and `soundings/sim/detection.py` (WO-089 block)
- `soundings/sim/run.py` (NL per node per tick, WO-089 block)
- `soundings/challenge/hail.py` and `soundings/investigation/features.py` (WO-089 blocks)
- `tests/test_physics.py`, `tests/test_detection_model_sonar.py` and `tests/test_catalog_outcomes_physics.py`

P2:
- `soundings/api_routes/` or `soundings/api.py` (WO-089 block)
- `soundings/stream/` (WO-089 block)
- `web/src/components/AboutPanel.tsx`, `HarborMap.tsx`, `web/src/events.ts` and `web/src/generated/stream-events.schema.json`
- `docs/deck/template.html`, `scripts/build_deck.py` and `tests/test_deck.py`
- the API, stream and UI tests

## Exit tests (must all pass with `MODEL_MODE=replay`, no network)
From the owner directive (verbatim):
- [ ] detection probability monotone in range and in noise; range rings shrink during a transit in BT-01 (ring radius before/after); S07/S08 unchanged in outcome; catalog outcomes unchanged except where the report explains why; determinism; every parameter has a source string.

Extended:
- [ ] `uv run pytest tests/test_physics.py`:
  - `detection_probability_at` is non-increasing in range (dense grid 1 m..2 km) and in NL (dense grid 40..100 dB);
  - `effective_range_m` inverts it (p = 0.5·logistic term within 1e-6);
  - `hail_latency_s(750) == 1.0`;
  - `camera_visibility(0, 0) == 1` and the function is monotone in depth and turbidity;
  - battery drain scales by exactly 8× when speed doubles;
  - every parameter in `config/physics.yaml` has a non-empty `source` and `label == "demo value"`;
  - the loader is strict (an unknown key raises);
  - two calls give identical outputs.
- [ ] `uv run pytest tests/test_detection_model_sonar.py`: the WO-011 detection tests re-run under `sonar_equation` where they apply. These are monotone falloff, the gap rate within tolerance and the false-alarm rate within tolerance (10k samples), both unchanged.
- [ ] `uv run pytest tests/test_catalog_outcomes_physics.py`:
  - S07 and S08 are unchanged in outcome;
  - catalog S01..S08 × seeds 1..3 outcomes equal the baseline recorded under `gaussian` before the swap;
  - any explained exception is listed in the test with its DECISIONS line.
- [ ] Ring shrink (P2, using P1's functions): in BT-01, the ring radius of the node nearest a vessel transit is smaller at the transit's closest-approach tick than 10 minutes before it. The test prints both radii.
- [ ] Determinism: two `python -m soundings.sim` runs of S05 seed 11 and BT-01 seed 7 are byte-identical (`diff` empty).
- [ ] Deck (P2): `uv run pytest tests/test_deck.py` passes; there are still 11 slides; slide 10 carries the cards; no hand-typed numbers.
- [ ] Honesty: the About panel header still reads "simulated for this demonstration", and `bash scripts/denylist-check.sh` is clean.
- [ ] `make check` exits 0.

Spec-change rule: any catalog expected-outcome change is a spec change. It needs a DECISIONS line and the PR must explain why. Never weaken or delete an existing test.

## Interfaces
Binding for both WO-089 agents:
- `soundings.physics.load_physics_config(path=None) -> PhysicsConfig`: strict Pydantic. Each parameter model is `{value, unit, source: non-empty str, label: Literal["demo value"]}`.
- `transmission_loss_db(range_m, alpha_db_per_km) -> float`.
- `ambient_noise_db(base_db, sea_state, vessel_fixes: Sequence[tuple[lat, lon, speed_mps]], node_lat, node_lon, cfg) -> float`: power-domain sum, distance-weighted.
- `detection_probability(sl_db, tl_db, nl_db, dt_db, k_db) -> float`: `logistic((SL - TL - NL - DT) / k)`.
- `detection_probability_at(range_m, sl_db, nl_db, cfg) -> float`.
- `effective_range_m(sl_db, nl_db, cfg) -> float`: the range where p = 0.5, in closed form.
- `hail_latency_s(range_m, cfg) -> float`: `2r / sound_speed`.
- `camera_visibility(depth_m, turbidity, cfg) -> float`: `exp(-k·depth) · exp(-turbidity)`.
- `battery_drain_min(speed_mps, dt_s, cfg) -> float`: the cubic law.
- `model_cards() -> list[PhysicsCard]`, where `PhysicsCard(name, equation, parameters, not_modeled)` is frozen and JSON-ready.
- `config/sim.yaml` `detection.model: "sonar_equation" | "gaussian"`.
- P2 surfaces:
  - `GET /api/physics` returns `{cards, nodes: [{node_id, effective_range_m, nl_db}]}`;
  - the stream event `sensor_ranges: [{node_id, effective_range_m, nl_db}]` is sent per tick, sorted by `node_id`.
- Shared files are edited only inside `# --- WO-089 begin ---` / `# --- WO-089 end ---` (or `// ---`) blocks.

## Cut line
Drop these first if more than an hour late, in this order:
1. the sea-state table (one base NL);
2. the vessel-noise falloff detail (a single distance law);
3. the deck card (slide 10 keeps the honesty line and adds the equation only).

Never drop an exit test.

## Model calls
None. No paid run.

## Rungs touched
watch: detection and range rings. ask: hail latency. No ladder threshold changes.

## Hard rules exercised
- §2.4 determinism: `test_physics` determinism and the two-run sim diff.
- §2.6 labeled numbers: every parameter carries source and "demo value", and `test_physics` checks this.
- §2.1 and §2.5 vocabulary: `scripts/denylist-check.sh`.
- §2.2: physics values feed the evidence table only; nothing sets a rung.

## Done
PR: #49 (UI half, 4202721) and #57 (code half) · Merged commit: see docs/STATE.md · Test summary (code half, `make check` exit 0): 2023 passed, 8 skipped, 1 xfailed · STATE.md updated: yes

Code half (`wo/089c-physics-core-code`):
- `config/physics.yaml`: every parameter `{value, unit, source, label: demo value}`; tuned SL 130 dB (all classes), alpha 1 dB/km, DT 21.36549 dB, k 2.0 dB, calm NL 56.19331 dB, so the effective range is 400.000008 m and the cut-off 800.000016 m for every class.
- `soundings/physics.py`: the Interfaces contract plus `model_cards()` (5 cards: passive sonar detection, ambient noise, hail latency, camera visibility, responder battery drain).
- Sim swap: `config/sim.yaml` `detection.model: sonar_equation` (default) | `gaussian` (byte-identical to main: all 32 S01..S08 x {catalog, 1, 2, 3} track hashes match); `run_scenario(ais_tracks=...)` feeds per-node, per-tick NL.
- Catalog: lineage-end terminal states equal the Gaussian baseline except S04 seeds 7 and 2 and S08 seed 1, which now reach their expected state (DECISIONS); S07 and S08 at their catalog seeds unchanged.
- Hail: turnaround 18.9 s + 2r/1500 (always 10 ticks at tick_s 2); camera: hour default x exp(-k depth) x exp(-turbidity).

## Notes for downstream
- On main the About text lives in `soundings/api_routes/meta.py` (`DETECTION_HEADING`, `DETECTION_ASSUMPTIONS`). The `DETECTION_ASSUMPTIONS` lines name the Gaussian parameters, so P2 updates them inside a WO-089 block once P1's model is the default.
- With `detection.model: gaussian`, every S01..S08 track hash must stay byte-identical to main.
