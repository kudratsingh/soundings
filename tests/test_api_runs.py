"""WO-060 Part A: runs over REST and WebSocket, and no route that can steer a decision."""

from __future__ import annotations

import json
import re
from typing import Any

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from pydantic import BaseModel

from soundings.api import create_app
from soundings.sim.scenario import catalog_paths
from soundings.stream import engine as engine_mod
from soundings.stream.engine import RunEngine

FORBIDDEN_FIELDS = {"rung", "state", "threshold", "scope"}


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr(engine_mod, "_ENGINE", RunEngine())
    return TestClient(create_app())


def _drain(client: TestClient, url: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    with client.websocket_connect(url) as ws:
        while True:
            try:
                out.append(json.loads(ws.receive_text()))
            except Exception:  # the server closes the socket after run_finished
                break
    return out


def test_scenarios_are_catalog_ids_sorted(client: TestClient) -> None:
    rows = client.get("/api/scenarios").json()
    ids = [r["id"] for r in rows]
    assert ids == sorted(ids)
    assert all(re.fullmatch(r"S\d{2}", i) for i in ids)
    assert ids == sorted(p.name[:3] for p in catalog_paths())
    assert "smoke" not in ids


def test_start_and_stream_with_resume(client: TestClient) -> None:
    r = client.post("/api/runs", json={"scenario": "S01", "seed": 1, "speed": 0})
    assert r.status_code == 200
    run_id = r.json()["run_id"]
    events = _drain(client, f"/ws/runs/{run_id}")
    seqs = [e["seq"] for e in events]
    assert seqs == list(range(len(events)))
    assert events[0]["type"] == "run_started" and events[-1]["type"] == "run_finished"
    assert all(e["run_id"] == run_id for e in events)
    n = len(events) // 2
    resumed = _drain(client, f"/ws/runs/{run_id}?after={n}")
    assert resumed == events[n + 1 :]
    listed = client.get("/api/runs").json()
    assert listed[0]["run_id"] == run_id


def test_live_stream_is_paced_and_ordered(client: TestClient) -> None:
    run_id = client.post("/api/runs", json={"scenario": "S01", "seed": 1, "speed": 5000}).json()[
        "run_id"
    ]
    events = _drain(client, f"/ws/runs/{run_id}")
    seqs = [e["seq"] for e in events]
    assert seqs == list(range(len(seqs))) and events[-1]["type"] == "run_finished"


def test_bad_requests(client: TestClient) -> None:
    assert client.post("/api/runs", json={"scenario": "smoke"}).status_code == 400
    assert client.post("/api/runs", json={"scenario": "../S01"}).status_code == 400
    assert client.post("/api/runs", json={"scenario": "S99"}).status_code == 404
    r = client.post("/api/runs", json={"scenario": "S01", "toggles": ["sensor_dropout"]})
    assert r.status_code == 400
    assert client.post("/api/runs", json={"scenario": "S01", "rung": "hold"}).status_code == 422
    events = _drain(client, "/ws/runs/nope")
    assert events[0]["type"] == "error"


def _fields(model: type[BaseModel], seen: set[type[BaseModel]]) -> set[str]:
    if model in seen:
        return set()
    seen.add(model)
    names = set(model.model_fields)
    for f in model.model_fields.values():
        ann = f.annotation
        for cand in (ann, *getattr(ann, "__args__", ())):
            if isinstance(cand, type) and issubclass(cand, BaseModel):
                names |= _fields(cand, seen)
    return names


def test_no_route_sets_rung(client: TestClient) -> None:
    app = client.app
    for route in app.routes:  # type: ignore[attr-defined]
        if not isinstance(route, APIRoute) or route.body_field is None:
            continue
        model = route.body_field.type_
        if isinstance(model, type) and issubclass(model, BaseModel):
            hit = _fields(model, set()) & FORBIDDEN_FIELDS
            assert not hit, f"{route.path} accepts {hit}"
        params = {p.name for p in route.dependant.query_params}
        assert not params & FORBIDDEN_FIELDS, route.path
    spec = client.get("/openapi.json").json()
    schemas = spec.get("components", {}).get("schemas", {})
    for path, ops in spec["paths"].items():
        for op in ops.values():
            for p in op.get("parameters", []):
                assert p["name"] not in FORBIDDEN_FIELDS, path
            body = op.get("requestBody", {}).get("content", {}).get("application/json", {})
            ref = body.get("schema", {}).get("$ref")
            if ref:
                props = schemas[ref.rsplit("/", 1)[-1]].get("properties", {})
                assert not set(props) & FORBIDDEN_FIELDS, path
