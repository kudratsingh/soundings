"""WO-089 code half: the physics laws and their sourced config (docs/SPEC-SIM.md section 2a).

Owner directive exit tests covered here: detection probability monotone in range and in
noise; every parameter has a source string; determinism.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np
import pytest
import yaml
from pydantic import ValidationError

from soundings.physics import (
    ACOUSTIC_CLASSES,
    NOT_MODELED,
    PHYSICS_CONFIG_PATH,
    PhysicsCard,
    ambient_noise_db,
    battery_drain_min,
    camera_visibility,
    detection_probability,
    detection_probability_at,
    effective_range_m,
    hail_latency_s,
    load_physics_config,
    logistic,
    model_cards,
    transmission_loss_db,
)
from soundings.physics_view import iter_params
from soundings.schemas.denylist import find_denylisted
from soundings.schemas.geo import from_local_m, to_local_m
from soundings.schemas.harbor import load_harbor

CFG = load_physics_config()
RAW = yaml.safe_load(PHYSICS_CONFIG_PATH.read_text(encoding="utf-8"))
NL0 = CFG.base_noise_db()
SL = {c: CFG.sonar.source_level_db.for_class(c) for c in ACOUSTIC_CLASSES}
RANGES = np.arange(1.0, 2000.0 + 1e-9, 1.0)
NOISES = np.arange(40.0, 100.0 + 1e-9, 0.05)


# --- monotone ----------------------------------------------------------------------------


@pytest.mark.parametrize("cls", ACOUSTIC_CLASSES)
def test_detection_probability_non_increasing_in_range(cls: str) -> None:
    for nl in (NL0, 70.0, 90.0):
        p = [detection_probability_at(float(r), SL[cls], nl, CFG) for r in RANGES]
        assert all(b <= a for a, b in zip(p, p[1:], strict=False)), (cls, nl)
        assert p[0] > p[-1]


@pytest.mark.parametrize("cls", ACOUSTIC_CLASSES)
def test_detection_probability_non_increasing_in_noise(cls: str) -> None:
    for r in (10.0, 200.0, 400.0, 800.0):
        p = [detection_probability_at(r, SL[cls], float(nl), CFG) for nl in NOISES]
        assert all(b <= a for a, b in zip(p, p[1:], strict=False)), (cls, r)
        assert p[0] > p[-1]


def test_transmission_loss_and_logistic() -> None:
    assert transmission_loss_db(1000.0, 1.0) == pytest.approx(61.0)
    assert transmission_loss_db(0.0, 1.0) == transmission_loss_db(1.0, 1.0)
    assert logistic(0.0) == 0.5 and logistic(800.0) == 1.0 and logistic(-800.0) == 0.0
    assert detection_probability(100.0, 40.0, 40.0, 20.0, 2.0) == 0.5
    with pytest.raises(ValueError):
        detection_probability(1.0, 1.0, 1.0, 1.0, 0.0)
    with pytest.raises(ValueError):
        transmission_loss_db(-1.0, 1.0)


# --- effective range ---------------------------------------------------------------------


def test_effective_range_is_the_former_r0_at_calm_noise() -> None:
    for cls in ACOUSTIC_CLASSES:
        r = effective_range_m(SL[cls], NL0, CFG)
        assert r == pytest.approx(400.0, abs=0.01), cls
        assert CFG.cutoff_range_m(SL[cls]) == pytest.approx(800.0, abs=0.02)


@pytest.mark.parametrize("cls", ACOUSTIC_CLASSES)
def test_effective_range_inverts_the_detection_term(cls: str) -> None:
    for nl in np.arange(45.0, 75.0, 0.5):
        r = effective_range_m(SL[cls], float(nl), CFG)
        assert r > 0.0
        assert detection_probability_at(r, SL[cls], float(nl), CFG) == pytest.approx(0.5, abs=1e-6)


def test_effective_range_shrinks_with_noise_and_alpha_zero_closed_form() -> None:
    radii = [effective_range_m(SL["biologic"], float(nl), CFG) for nl in NOISES]
    assert all(b <= a for a, b in zip(radii, radii[1:], strict=False))
    assert effective_range_m(SL["biologic"], 200.0, CFG) == 0.0  # even 1 m falls short
    flat = CFG.model_copy(
        update={
            "sonar": CFG.sonar.model_copy(
                update={
                    "alpha_db_per_km": CFG.sonar.alpha_db_per_km.model_copy(update={"value": 0.0})
                }
            )
        }
    )
    e = SL["biologic"] - NL0 - CFG.sonar.detection_threshold_db.value
    assert effective_range_m(SL["biologic"], NL0, flat) == pytest.approx(10 ** (e / 20), abs=1e-6)


# --- ambient noise -----------------------------------------------------------------------


def test_ambient_noise_power_sum_and_distance_weighting() -> None:
    base = CFG.sonar.ambient_noise_base_db.value
    sea = CFG.sonar.sea_state_table[CFG.sonar.sea_state].value
    calm = ambient_noise_db(base, CFG.sonar.sea_state, [], 0.0, 0.0, CFG)
    assert calm == pytest.approx(10 * math.log10(10 ** (base / 10) + 10 ** (sea / 10)), abs=1e-6)
    node = load_harbor().sensor_nodes[0]
    x, y = to_local_m(node.lat, node.lon)
    levels = []
    for d in (3000.0, 1000.0, 300.0, 100.0, 30.0):
        lat, lon = from_local_m(x + d, y)
        levels.append(ambient_noise_db(base, 1, [(lat, lon, 5.0)], node.lat, node.lon, CFG))
    assert levels == sorted(levels) and levels[0] >= calm and levels[-1] > levels[0]
    # two vessels are louder than one; order of the fixes does not matter
    a, b = from_local_m(x + 100.0, y), from_local_m(x, y + 200.0)
    one = ambient_noise_db(base, 1, [(*a, 5.0)], node.lat, node.lon, CFG)
    two = ambient_noise_db(base, 1, [(*a, 5.0), (*b, 3.0)], node.lat, node.lon, CFG)
    rev = ambient_noise_db(base, 1, [(*b, 3.0), (*a, 5.0)], node.lat, node.lon, CFG)
    assert two > one and two == rev
    with pytest.raises(ValueError):
        ambient_noise_db(base, 99, [], node.lat, node.lon, CFG)


def test_sea_state_raises_noise() -> None:
    base = CFG.sonar.ambient_noise_base_db.value
    levels = [
        ambient_noise_db(base, s, [], 0.0, 0.0, CFG) for s in sorted(CFG.sonar.sea_state_table)
    ]
    assert levels == sorted(levels) and levels[-1] > levels[0]


# --- the other laws ----------------------------------------------------------------------


def test_hail_latency() -> None:
    assert hail_latency_s(750.0, CFG) == 1.0
    assert hail_latency_s(0.0, CFG) == 0.0
    # turnaround + travel to the cut-off stays inside the SPEC 20 s allowlist latency
    cut = CFG.cutoff_range_m(max(SL.values()))
    assert CFG.hail.modem_turnaround_s.value + hail_latency_s(cut, CFG) < 20.0


def test_camera_visibility() -> None:
    assert camera_visibility(0.0, 0.0, CFG) == 1.0
    depths = [camera_visibility(float(d), 0.0, CFG) for d in np.arange(0.0, 50.0, 0.25)]
    assert all(b <= a for a, b in zip(depths, depths[1:], strict=False)) and depths[-1] < 1.0
    turb = [camera_visibility(2.0, float(t), CFG) for t in np.arange(0.0, 5.0, 0.05)]
    assert all(b <= a for a, b in zip(turb, turb[1:], strict=False)) and turb[-1] < turb[0]
    assert camera_visibility(10.0, 0.5, CFG) == pytest.approx(
        math.exp(-CFG.camera.k_depth_per_m.value * 10.0) * math.exp(-0.5), abs=1e-6
    )


def test_battery_drain_is_cubic() -> None:
    for v in (0.25, 0.5, 1.0, 1.5, 2.0, 3.0):
        for dt in (1.0, 5.0, 60.0):
            assert battery_drain_min(2 * v, dt, CFG) == 8 * battery_drain_min(v, dt, CFG)
    assert battery_drain_min(0.0, 60.0, CFG) == 0.0
    assert battery_drain_min(2.0, 60.0, CFG) == pytest.approx(
        CFG.responder.drain_coeff.value * 8 * 60
    )
    with pytest.raises(ValueError):
        battery_drain_min(-1.0, 1.0, CFG)


# --- config ------------------------------------------------------------------------------


def test_every_parameter_has_a_source_and_the_demo_label() -> None:
    leaves = dict(iter_params(RAW))
    assert leaves, "config/physics.yaml has parameters"
    for key, node in leaves.items():
        assert str(node["source"]).strip(), key
        assert node["label"] == "demo value", key
        assert str(node["unit"]).strip(), key
        assert isinstance(node["value"], int | float), key
    assert sorted(k for k, _ in CFG.params()) == sorted(leaves)
    assert "DEMO VALUE" in PHYSICS_CONFIG_PATH.read_text(encoding="utf-8")


def test_loader_is_strict(tmp_path: Path) -> None:
    def write(data: object) -> Path:
        path = tmp_path / f"p{len(list(tmp_path.iterdir()))}.yaml"
        path.write_text(yaml.safe_dump(data), encoding="utf-8")
        return path

    extra = json.loads(json.dumps(RAW))
    extra["sonar"]["surprise"] = {"value": 1, "unit": "x", "source": "y", "label": "demo value"}
    with pytest.raises(ValidationError):
        load_physics_config(write(extra))
    nosrc = json.loads(json.dumps(RAW))
    nosrc["hail"]["sound_speed_mps"]["source"] = "  "
    with pytest.raises(ValidationError):
        load_physics_config(write(nosrc))
    label = json.loads(json.dumps(RAW))
    label["camera"]["k_depth_per_m"]["label"] = "measured"
    with pytest.raises(ValidationError):
        load_physics_config(write(label))
    missing = json.loads(json.dumps(RAW))
    del missing["responder"]
    with pytest.raises(ValidationError):
        load_physics_config(write(missing))


# --- cards -------------------------------------------------------------------------------


def test_model_cards_shapes() -> None:
    cards = model_cards()
    assert len(cards) >= 4
    assert len({c.name for c in cards}) == len(cards)
    on_cards = set()
    for card in cards:
        assert isinstance(card, PhysicsCard)
        assert card.name.strip() and card.equation.strip()
        assert card.parameters
        for p in card.parameters:
            assert p.source.strip() and p.label == "demo value"
            on_cards.add(p.key)
        for item in ("hydrodynamics", "beam patterns", "multipath", "bottom type"):
            assert item in card.not_modeled
        assert set(NOT_MODELED) <= set(card.not_modeled)
        json.dumps(card.as_dict())  # JSON-ready
        with pytest.raises(AttributeError):
            card.name = "x"  # type: ignore[misc]
    assert on_cards == set(dict(iter_params(RAW)))
    assert find_denylisted(json.dumps([c.as_dict() for c in cards])) == []


def test_determinism() -> None:
    def snapshot() -> str:
        load = load_physics_config()
        out = {
            "cards": [c.as_dict() for c in model_cards(load)],
            "p": [
                detection_probability_at(float(r), SL["biologic"], NL0, load) for r in RANGES[::7]
            ],
            "r": [effective_range_m(SL["biologic"], float(nl), load) for nl in NOISES[::11]],
            "hail": hail_latency_s(123.4, load),
            "cam": camera_visibility(3.3, 0.2, load),
            "bat": battery_drain_min(1.7, 9.0, load),
        }
        return json.dumps(out, sort_keys=True)

    assert snapshot() == snapshot()
