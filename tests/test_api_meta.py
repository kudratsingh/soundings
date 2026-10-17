"""WO-060 Part A: boundary table, honesty text and harbor map come from config."""

from __future__ import annotations

import json
from pathlib import Path

import yaml
from fastapi.testclient import TestClient

from soundings.api import create_app
from soundings.ladder.standing_orders import SIGNATURE_LABEL

REPO = Path(__file__).resolve().parent.parent
client = TestClient(create_app())


def test_boundary_matches_ladder_yaml() -> None:
    body = client.get("/api/boundary").json()
    cfg = yaml.safe_load((REPO / "config" / "ladder.yaml").read_text(encoding="utf-8"))
    assert body["rungs"] == cfg["rungs"]
    assert body["lives_on"] == cfg["lives_on"]
    expected = [
        (rung, cell, action)
        for rung in cfg["rungs"]
        for cell in ("platform_alone", "standing_orders", "person_approves")
        for action in cfg["boundary"][rung][cell]
    ]
    got = [(r["rung"], r["yaml_cell"], r["action"]) for r in body["rows"]]
    assert got == expected
    so = body["standing_orders"]
    assert so["signed"] is True and so["signer"] == "Watch Commander (demo)"
    assert so["valid_until"].startswith("2026-10-18")
    assert so["signature_label"] == SIGNATURE_LABEL


def test_about_honesty_text() -> None:
    text = json.dumps(client.get("/api/about").json(), ensure_ascii=False)
    for needle in (
        "simulated for this demonstration",
        "simplified for demonstration",
        "stress-test estimate",
        "© OpenStreetMap contributors",
        "© OpenSeaMap contributors",
        "share the likelihood tables",
        "The work stops at the handoff.",
        "30 days",
        SIGNATURE_LABEL,
        "https://operations.osmfoundation.org/policies/tiles/",
    ):
        assert needle in text, needle
    about = client.get("/api/about").json()
    sim = yaml.safe_load((REPO / "config" / "sim.yaml").read_text(encoding="utf-8"))
    assert about["honesty"]["detection_params"] == sim["detection"]


def test_harbor_serves_every_feature() -> None:
    body = client.get("/api/harbor").json()
    raw = json.loads((REPO / "config" / "harbor.geojson").read_text(encoding="utf-8"))
    assert body == raw
    kinds = {f["properties"]["kind"] for f in body["features"]}
    assert kinds == {"zone", "asset", "sensor_node"}
