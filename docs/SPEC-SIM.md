# Spec — simulator, detection model, scenario catalog

The simulator exists to produce honest contact tracks, not to model sonar. It must be seeded, tick-based, and reproducible. It emits what a detection layer would hand to the investigation: a track with gaps, noisy features, and a few acoustic feature flags.

---

## 1. Harbor map

Real geography, simplified: Port of Los Angeles main channel and inner harbor, Pier 400 approach, a fuel/LNG berth, Vincent Thomas Bridge pilings, an inner basin used by research/maintenance operators. Stored as `config/harbor.geojson`: zones (polygons with `zone_id`, `criticality`, `basin`), assets (points with `asset_id`, `criticality`), sensor nodes (points with `range_m`, `health`). Base map tiles: OpenSeaMap over OSM (free; verify tile usage policy and add attribution).

Coordinates can be approximate; label "simplified for demonstration" in the About panel.

## 2. Detection model (honest, simple)

- Detection probability per tick: `p = p_max · exp(−(range / r0)^2) · sensor_health`, with `p_max=0.95`, `r0` per node (default 400 m). Outside 2·r0 nothing is detected.
- Position noise: Gaussian, σ grows with range (σ = 5 m + 0.03·range).
- Track gaps: a detected contact is dropped for a geometric-distributed run of ticks with rate `gap_rate` (default 0.08/tick); gaps > `track_timeout` end the track and a new contact id is issued (so re-acquisition is a real problem the matcher handles).
- Biologic false alarms: Poisson arrivals per node per hour (default 6) producing short, erratic, shallow tracks with `breathing_or_click_present=true` with probability 0.7.
- Acoustic features are sampled from per-hypothesis distributions in `config/likelihoods.yaml` (the same tables the posterior uses — state this clearly: the demo sim and the scorer share tables, which is why the eval also includes "mis-specified" runs where the sim draws from perturbed tables).
- Current/wind field: a static vector field by tide phase from `config/current_field.yaml`; `drift_check` compares the track to it.

Required property tests: monotone range falloff; false-alarm rate within tolerance; gap rate within tolerance; byte-identical output per seed.

## 3. Scenario catalog v1 (`scenarios/*.yaml`)

| id | name | ground truth | expected outcome | ordinary-day or crisis |
|---|---|---|---|---|
| S01 | Filed survey, on plan | AUV with plan, lawnmower pattern in inner basin | CLEARED_AT_ASK | Tuesday |
| S02 | Filed vehicle gone silent | AUV with plan; hail unanswered; drifting | LOST_VEHICLE with search box | Tuesday |
| S03 | Permitted hull-cleaning divers | two slow shallow contacts by a berth; plan filed | CLEARED_AT_ASK + slow advisory under standing orders if a vessel approaches | Tuesday |
| S04 | Unfiled slow contact, benign | unfiled; replies to hail with a known modem id | RESOLVED_BENIGN (UNFILED_KNOWN_OPERATOR) | Tuesday |
| S05 | Unfiled contact closing on fuel berth | self-propelled, straight, tonal present, no reply | HOLD under standing orders → HANDED_OFF, threat_mass ≥ 0.6 | Harbor Night |
| S06 | Loitering under a hull | orbit pattern near a critical asset, no reply | HANDED_OFF | Harbor Night |
| S07 | Sea lion | erratic, shallow, clicks, high drift consistency | RESOLVED_BENIGN | Tuesday |
| S08 | Drifting debris | high drift consistency, no tonal | HAZARD_NOTICE | Tuesday |

Twist toggles (prepared in advance; each is a flag on any scenario): `contact_surge(n)`, `sensor_dropout(node_id)`, `network_down`, `approver_unreachable`, `ais_spoof`, `second_coordinated_contact`, `standing_orders_expired`, `contact_goes_quiet`, `fog_no_camera`.

## 4. Scenario file shape

```yaml
id: S05
name: Unfiled contact closing on fuel berth
seed: 11
duration_ticks: 600
tick_s: 2
ground_truth: UNAUTHORIZED_UUV
contacts:
  - id: gt-1
    start: {lat: 33.735, lon: -118.262, depth_m: 6}
    behavior: {type: transit, target_asset: fuel_berth_2, speed_mps: 1.0}
    acoustic: {propeller_tonal: true, tonal_band: mid, biologic_signature: false}
    registry: {filed: false, replies_to_hail: false}
toggles: []
expected:
  terminal_state: HANDED_OFF
  min_threat_mass: 0.6
  max_human_interruptions: 1
```

The `expected` block is what the investigation tests assert. A twist adds a scenario file (or a toggle) and an `expected` block, and that is the proof the twist response needs.

## 5. Judge injection

The UI can create a scenario on the fly: click a point on the map, pick a behavior and acoustic profile, pick filed/unfiled, press go. It is written as a scenario file under `scenarios/live/` with a timestamp and run through the same pipeline, so a judge's contact is reproducible afterwards.
