"""WO-089 part 2: ``GET /api/physics`` serves one card per model (equation, parameters with
value, unit, source and the "demo value" label, what is not modeled) and each sensor node's
passive-sonar range at base noise. Every number comes from ``config/physics.yaml``."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient

from soundings.api import create_app
from soundings.physics_view import NOT_MODELED_FLOOR, iter_params
from soundings.schemas.denylist import find_denylisted
from soundings.schemas.harbor import load_harbor

REPO = Path(__file__).resolve().parent.parent
PHYSICS_YAML = REPO / "config" / "physics.yaml"


@pytest.fixture(scope="module")
def body() -> dict[str, Any]:
    res = TestClient(create_app()).get("/api/physics")
    assert res.status_code == 200
    data: dict[str, Any] = res.json()
    return data


@pytest.fixture(scope="module")
def raw() -> dict[str, Any]:
    data: dict[str, Any] = yaml.safe_load(PHYSICS_YAML.read_text(encoding="utf-8"))
    return data


def test_heading_keeps_the_honesty_text(body: dict[str, Any]) -> None:
    assert "simulated for this demonstration" in body["heading"]
    assert body["label"] == "demo value"


def test_cards_have_equation_sources_and_not_modeled(
    body: dict[str, Any], raw: dict[str, Any]
) -> None:
    cards = body["cards"]
    assert len(cards) >= 4
    names = [c["name"] for c in cards]
    assert len(set(names)) == len(names)
    leaves = dict(iter_params(raw))
    for card in cards:
        assert card["name"].strip() and card["equation"].strip(), card
        assert card["parameters"], card["name"]
        for p in card["parameters"]:
            assert p["source"].strip(), p
            assert p["label"] == "demo value", p
            assert p["key"] in leaves, p
            assert p["value"] == pytest.approx(float(leaves[p["key"]]["value"]), abs=1e-6)
        for item in NOT_MODELED_FLOOR:
            assert item in card["not_modeled"], (card["name"], item)


def test_every_config_parameter_is_on_a_card(body: dict[str, Any], raw: dict[str, Any]) -> None:
    shown = {p["key"] for c in body["cards"] for p in c["parameters"]}
    for key, node in iter_params(raw):
        assert str(node["source"]).strip(), key
        assert key in shown, f"{key} is in config/physics.yaml but on no card"


def test_nodes_have_effective_ranges(body: dict[str, Any]) -> None:
    from soundings.physics import effective_range_m, load_physics_config
    from soundings.physics_view import ring_inputs

    harbor_ids = sorted(n.node_id for n in load_harbor().sensor_nodes)
    nodes = body["nodes"]
    assert [n["node_id"] for n in nodes] == harbor_ids
    inp = ring_inputs()
    cfg = load_physics_config()
    for n in nodes:
        assert n["effective_range_m"] > 0
        assert n["effective_range_m"] == pytest.approx(
            effective_range_m(inp.sl_db, n["nl_db"], cfg), abs=1e-5
        )
    # Base noise is the same at every node, so every ring is the same size before a transit.
    assert len({n["effective_range_m"] for n in nodes}) == 1


def test_payload_is_deterministic_and_clean(body: dict[str, Any]) -> None:
    again = TestClient(create_app()).get("/api/physics").json()
    assert json.dumps(again, sort_keys=True) == json.dumps(body, sort_keys=True)
    assert find_denylisted(json.dumps(body)) == []


def test_about_assumptions_follow_the_detection_model() -> None:
    from types import SimpleNamespace

    from soundings.api_routes.meta import (
        DETECTION_ASSUMPTIONS,
        SONAR_ASSUMPTIONS,
        detection_assumptions,
    )

    sonar = detection_assumptions(SimpleNamespace(model="sonar_equation"))
    assert sonar == list(SONAR_ASSUMPTIONS)
    assert any("SL - TL - NL - DT" in line for line in sonar)
    assert detection_assumptions(SimpleNamespace(model="gaussian")) == list(DETECTION_ASSUMPTIONS)
    assert detection_assumptions(object()) == list(DETECTION_ASSUMPTIONS)
